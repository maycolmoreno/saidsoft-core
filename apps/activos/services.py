"""Transiciones de estado del ciclo de vida de un Activo.

Sigue el mismo patrón que apps/despliegues/services.py: la lógica de negocio
vive aquí, separada de las vistas, para que tanto el panel como el admin la
reutilicen igual.
"""
import datetime
from dataclasses import dataclass, replace

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import F, Q
from django.utils import timezone

from apps.activos.models import (
    Activo, CategoriaEquipo, EventoActivo, Marca, MovimientoInventario, OrdenCompraDetalle,
    RecepcionLote, StockBodega, UbicacionInterna,
)


class ConcurrencyError(Exception):
    """Otra recepción/actualización tocó la misma línea entre lectura y guardado; reintenta."""


def _obtener_y_bloquear_stock(bodega, tipo_consumible):
    """Crea la fila de stock si no existe y la bloquea (`select_for_update`) para el
    resto de la transacción — debe correr dentro de un `transaction.atomic()`. Sin
    esto, dos entregas/salidas simultáneas de la misma bodega podían leer el mismo
    saldo antes de que cualquiera de las dos escribiera, perdiendo una de las dos
    actualizaciones en silencio (BUG-1 de la auditoría de gobernanza, 22-ago-2026)."""
    StockBodega.objects.get_or_create(bodega=bodega, tipo_consumible=tipo_consumible)
    return StockBodega.objects.select_for_update().get(bodega=bodega, tipo_consumible=tipo_consumible)


def generar_codigo_activo(tipo: str) -> str:
    """CR-[TIPO]-[NNNN], secuencial global por tipo, nunca reinicia.

    Nota: en concurrencia alta esto puede colisionar entre dos altas simultáneas
    del mismo tipo; el `unique=True` de Activo.codigo actúa de red de seguridad
    (la segunda inserción fallaría con IntegrityError). A esta escala no hace
    falta más que eso.
    """
    ultimo = Activo.objects.filter(tipo=tipo).order_by('-codigo').first()
    ultimo_num = int(ultimo.codigo.rsplit('-', 1)[-1]) if ultimo else 0
    return f'CR-{tipo}-{ultimo_num + 1:04d}'


@transaction.atomic
def registrar_ingreso(*, tipo, marca, modelo, numero_serie, fecha_compra,
                       vencimiento_garantia, orden_compra, usuario, bodega=None,
                       categoria=None, procesador='', ram_gb=None, almacenamiento_gb=None,
                       codigo_sap='', condicion_al_recibir='', farmacia=None,
                       estado_fisico=None, ip=None, mac='', ubicacion_interna='', slot=''):
    """Da de alta un activo, ya sea que ENTRE a bodega o que ya esté instalado.

    El flujo original asumía que todo activo nace en una bodega, lo que sirve para una
    compra nueva pero no para inventariar lo que ya está funcionando en una farmacia:
    obligaba a inventar una bodega por la que el equipo nunca pasó.

    Se exige bodega O farmacia (no las dos, aunque se aceptan juntas si el equipo pasó
    por bodega y ya se despachó):

    - Con bodega y sin farmacia: queda EN_BODEGA, como siempre.
    - Con farmacia: queda ASIGNADO, que en este dominio significa "en servicio", no
      "entregado a una persona" -- un PDV no tiene colaborador en el mismo sentido que
      un equipo de oficina (ver registrar_ubicacion_farmacia).

    `estado_fisico` por defecto es NUEVO para una compra, pero un equipo que ya está
    operando no es nuevo: cuando se registra directo en farmacia se asume BUENO, y
    quien carga puede corregirlo.

    `ip`/`mac`/`ubicacion_interna`/`slot` (topología) se aceptan en el alta y no solo
    después: el momento en que alguien inventaria el switch de una farmacia es el mismo
    en que tiene la etiqueta delante. `ip` y `mac` solo aplican a equipos SIN estación
    RMM vinculada — los que la tienen reportan su IP solos (ver `Activo.clean`), y por
    eso ninguno se adivina: vacío significa "todavía no lo sabemos", nunca un
    placeholder.
    """
    if bodega is None and farmacia is None:
        raise ValueError('Indicá la bodega donde ingresa el equipo o la farmacia donde ya está instalado.')

    ya_instalado = farmacia is not None and bodega is None
    if estado_fisico is None:
        estado_fisico = Activo.EstadoFisico.BUENO if ya_instalado else Activo.EstadoFisico.NUEVO

    activo = Activo(
        tipo=tipo, marca=marca, categoria=categoria, modelo=modelo, numero_serie=numero_serie,
        procesador=procesador, ram_gb=ram_gb, almacenamiento_gb=almacenamiento_gb,
        codigo_sap=codigo_sap, condicion_al_recibir=condicion_al_recibir, farmacia=farmacia,
        ip=ip, mac=mac, ubicacion_interna=ubicacion_interna, slot=slot,
        fecha_compra=fecha_compra, vencimiento_garantia=vencimiento_garantia,
        orden_compra=orden_compra, bodega_actual=bodega,
        estado=Activo.Estado.ASIGNADO if farmacia is not None else Activo.Estado.EN_BODEGA,
        estado_fisico_actual=estado_fisico,
        # Un equipo instalado en una farmacia pertenece al cliente dueño de esa
        # farmacia: heredarlo acá es lo que hace que el aislamiento por tenant
        # signifique algo para los activos. Hasta ahora TODOS nacían con
        # unidad_negocio vacía, o sea "compartido con todos los clientes".
        unidad_negocio=farmacia.unidad_negocio if farmacia is not None else None,
    )
    activo.codigo = generar_codigo_activo(tipo)
    activo.save()
    EventoActivo.objects.create(
        activo=activo, tipo_evento=EventoActivo.TipoEvento.INGRESO, usuario=usuario,
        detalle={
            'orden_compra': orden_compra.numero_oc if orden_compra else None,
            'proveedor': orden_compra.proveedor if orden_compra else None,
            'bodega': bodega.codigo if bodega else None,
            'farmacia': farmacia.codigo if farmacia else None,
            'marca': marca.nombre if marca else None,
            'categoria': categoria.nombre if categoria else None,
            'ip': ip,
            'mac': mac,
            'ubicacion_interna': ubicacion_interna,
            'slot': slot,
        },
    )
    return activo


@transaction.atomic
def registrar_baja_recomendada(*, activo, motivo, usuario):
    """Un mantenimiento recomienda dar de baja el activo; no cambia su estado.

    Es una señal para que un humano decida — el estado real solo cambia con
    `registrar_baja`, que exige un motivo formal (MotivoBaja) y libera al
    colaborador asignado.
    """
    if activo.baja_recomendada:
        return
    activo.baja_recomendada = True
    activo.save(update_fields=['baja_recomendada'])
    EventoActivo.objects.create(
        activo=activo, tipo_evento=EventoActivo.TipoEvento.BAJA_RECOMENDADA, usuario=usuario,
        detalle={'motivo': motivo},
    )


@transaction.atomic
def registrar_asignacion(*, activo, colaborador, estado_fisico_entrega, usuario):
    if activo.estado != Activo.Estado.EN_BODEGA:
        raise ValueError('Solo se pueden asignar activos que están en bodega.')
    bodega_origen = activo.bodega_actual
    activo.colaborador_actual = colaborador
    activo.estado = Activo.Estado.ASIGNADO
    activo.estado_fisico_actual = estado_fisico_entrega
    campos = ['colaborador_actual', 'estado', 'estado_fisico_actual']
    # Hereda el cliente del colaborador — no se limpia al devolver/dar de baja: queda
    # como el último dueño conocido, útil para auditoría de a quién perteneció.
    if colaborador.unidad_negocio_id:
        activo.unidad_negocio_id = colaborador.unidad_negocio_id
        campos.append('unidad_negocio')
    activo.save(update_fields=campos)
    EventoActivo.objects.create(
        activo=activo, tipo_evento=EventoActivo.TipoEvento.ASIGNACION, usuario=usuario,
        detalle={
            'colaborador': colaborador.nombre, 'cedula': colaborador.cedula,
            # colaborador.cargo es FK a Cargo (no serializable a JSON tal cual) — se
            # asignaba el objeto directo, lo que reventaba con TypeError en cuanto el
            # colaborador tuviera un cargo asignado (no lo detectaban los tests porque
            # ninguno seteaba `cargo`).
            'cargo': colaborador.cargo.nombre if colaborador.cargo_id else None,
            'sucursal': colaborador.sucursal,
            'bodega_origen': bodega_origen.codigo if bodega_origen else None,
            'estado_fisico_entrega': estado_fisico_entrega,
        },
    )


@transaction.atomic
def registrar_ubicacion_farmacia(*, activo, farmacia, usuario):
    """Marca (o limpia) en qué farmacia está físicamente un activo -- independiente del
    ciclo de vida (bodega/asignado/reparación): un PDV no tiene "colaborador asignado"
    en el mismo sentido que un equipo de oficina, así que esto no depende de
    `registrar_asignacion`. `farmacia=None` lo vuelve a marcar como
    administrativo/oficina o en bodega, según corresponda.

    No se puede llamar sobre un activo con Estación RMM vinculada -- para esos, la
    farmacia se sincroniza sola desde la Estación (ver
    vincular_activos_por_numero_serie); permitir editarla a mano dejaría el dato
    desincronizado de lo que el propio RMM reporta.
    """
    if activo.estado == Activo.Estado.DADO_DE_BAJA:
        raise ValueError('Un activo dado de baja no puede reubicarse.')
    if activo.estacion_id:
        raise ValueError(
            'Este activo tiene una Estación RMM vinculada -- su farmacia se sincroniza '
            'sola, no se puede cambiar a mano.',
        )
    activo.farmacia = farmacia
    activo.save(update_fields=['farmacia'])
    EventoActivo.objects.create(
        activo=activo, tipo_evento=EventoActivo.TipoEvento.UBICACION_ACTUALIZADA, usuario=usuario,
        detalle={'farmacia': farmacia.codigo if farmacia else None},
    )


@transaction.atomic
def registrar_consumible_entregado(*, activo, tipo_consumible, cantidad, usuario):
    if activo.estado != Activo.Estado.ASIGNADO or not activo.colaborador_actual:
        raise ValueError('El activo debe estar asignado a un colaborador para entregarle consumibles.')
    bodega = activo.bodega_actual
    if bodega is None:
        raise ValueError('El activo no tiene bodega de origen registrada.')

    stock = _obtener_y_bloquear_stock(bodega, tipo_consumible)
    if stock.cantidad < cantidad:
        raise ValueError(
            f'Stock insuficiente de {tipo_consumible.nombre} en {bodega.codigo} '
            f'({stock.cantidad} disponibles).',
        )
    StockBodega.objects.filter(pk=stock.pk).update(cantidad=F('cantidad') - cantidad)

    EventoActivo.objects.create(
        activo=activo, tipo_evento=EventoActivo.TipoEvento.CONSUMIBLE_ENTREGADO, usuario=usuario,
        detalle={
            'tipo_consumible': tipo_consumible.nombre, 'cantidad': cantidad,
            'colaborador': activo.colaborador_actual.nombre, 'bodega': bodega.codigo,
        },
    )


@transaction.atomic
def registrar_devolucion(*, activo, estado_fisico_devolucion, usuario, requiere_reparacion=False):
    if activo.estado != Activo.Estado.ASIGNADO:
        raise ValueError('El activo no está asignado actualmente.')
    colaborador_anterior = activo.colaborador_actual
    activo.colaborador_actual = None
    activo.estado_fisico_actual = estado_fisico_devolucion
    activo.estado = Activo.Estado.EN_REPARACION if requiere_reparacion else Activo.Estado.EN_BODEGA
    activo.save(update_fields=['colaborador_actual', 'estado_fisico_actual', 'estado'])
    EventoActivo.objects.create(
        activo=activo, tipo_evento=EventoActivo.TipoEvento.DEVOLUCION, usuario=usuario,
        detalle={
            'colaborador': colaborador_anterior.nombre if colaborador_anterior else None,
            'estado_fisico_devolucion': estado_fisico_devolucion,
            'destino': activo.estado,
        },
    )


@transaction.atomic
def registrar_envio_reparacion(*, activo, motivo, detalle_motivo, usuario):
    if activo.estado == Activo.Estado.DADO_DE_BAJA:
        raise ValueError('Un activo dado de baja no puede enviarse a reparación.')
    activo.estado = Activo.Estado.EN_REPARACION
    activo.colaborador_actual = None
    activo.save(update_fields=['estado', 'colaborador_actual'])
    EventoActivo.objects.create(
        activo=activo, tipo_evento=EventoActivo.TipoEvento.ENVIO_REPARACION, usuario=usuario,
        detalle={'motivo': motivo, 'detalle': detalle_motivo},
    )


@transaction.atomic
def registrar_retorno_reparacion(*, activo, estado_fisico, usuario, proveedor_tecnico=''):
    if activo.estado != Activo.Estado.EN_REPARACION:
        raise ValueError('El activo no está en reparación.')
    activo.estado = Activo.Estado.EN_BODEGA
    activo.estado_fisico_actual = estado_fisico
    activo.save(update_fields=['estado', 'estado_fisico_actual'])
    EventoActivo.objects.create(
        activo=activo, tipo_evento=EventoActivo.TipoEvento.RETORNO_REPARACION, usuario=usuario,
        detalle={'estado_fisico': estado_fisico, 'proveedor_tecnico': proveedor_tecnico},
    )


@transaction.atomic
def registrar_baja(*, activo, motivo, detalle_motivo, usuario):
    if activo.estado == Activo.Estado.DADO_DE_BAJA:
        raise ValueError('El activo ya está dado de baja.')
    activo.estado = Activo.Estado.DADO_DE_BAJA
    activo.colaborador_actual = None
    activo.save(update_fields=['estado', 'colaborador_actual'])
    EventoActivo.objects.create(
        activo=activo, tipo_evento=EventoActivo.TipoEvento.BAJA, usuario=usuario,
        detalle={'motivo': motivo, 'detalle': detalle_motivo},
    )


@transaction.atomic
def registrar_ingreso_stock(*, bodega, tipo_consumible, cantidad):
    stock = _obtener_y_bloquear_stock(bodega, tipo_consumible)
    StockBodega.objects.filter(pk=stock.pk).update(cantidad=F('cantidad') + cantidad)
    stock.refresh_from_db(fields=['cantidad'])
    return stock


@transaction.atomic
def registrar_salida_stock(*, bodega, tipo_consumible, cantidad):
    """Descuenta stock sin destino (se consume, no se traslada) — usado por
    apps.mantenimiento al registrar un repuesto tomado de bodega."""
    if cantidad <= 0:
        raise ValueError('La cantidad a descontar debe ser mayor a cero.')
    stock = _obtener_y_bloquear_stock(bodega, tipo_consumible)
    if stock.cantidad < cantidad:
        raise ValueError(
            f'Stock insuficiente de {tipo_consumible.nombre} en {bodega.codigo} ({stock.cantidad} disponibles).',
        )
    StockBodega.objects.filter(pk=stock.pk).update(cantidad=F('cantidad') - cantidad)
    stock.refresh_from_db(fields=['cantidad'])
    return stock


def recibir_orden_compra(*, orden_compra, novedad_recepcion, usuario):
    """Recepción simple de toda la OC de una vez, sin detalle por línea.

    Sigue disponible para el flujo histórico (ingreso de activos directo desde
    una OC sin registrar líneas); las OC que sí usan `OrdenCompraDetalle` se
    reciben con `registrar_recepcion_lote`, línea por línea y hasta en varios
    lotes.
    """
    orden_compra.novedad_recepcion = novedad_recepcion
    orden_compra.estado = orden_compra.Estado.RECIBIDA
    orden_compra.recibido_por = usuario
    orden_compra.save(update_fields=['novedad_recepcion', 'estado', 'recibido_por'])


def registrar_linea_orden_compra(*, orden_compra, tipo_item, cantidad_solicitada,
                                  descripcion='', modelo='', categoria=None, marca=None,
                                  tipo_consumible=None, precio_unitario=None, unidad_medida=''):
    return OrdenCompraDetalle.objects.create(
        orden_compra=orden_compra, tipo_item=tipo_item, descripcion=descripcion, modelo=modelo,
        categoria=categoria, marca=marca, tipo_consumible=tipo_consumible,
        cantidad_solicitada=cantidad_solicitada, precio_unitario=precio_unitario, unidad_medida=unidad_medida,
    )


def _recalcular_estado_orden_compra(orden_compra):
    estados = list(orden_compra.detalles.values_list('estado', flat=True))
    if estados and all(e == OrdenCompraDetalle.Estado.COMPLETO for e in estados):
        nuevo_estado = orden_compra.Estado.RECIBIDA
    elif any(e in (OrdenCompraDetalle.Estado.PARCIAL, OrdenCompraDetalle.Estado.COMPLETO) for e in estados):
        nuevo_estado = orden_compra.Estado.RECEPCION_PARCIAL
    else:
        return
    if orden_compra.estado != nuevo_estado:
        orden_compra.estado = nuevo_estado
        orden_compra.save(update_fields=['estado'])


@transaction.atomic
def registrar_recepcion_lote(*, detalle, cantidad, bodega, usuario, custodio_receptor=None, numero_lote=''):
    """Recibe `cantidad` unidades de una línea de OC; puede llamarse varias veces (recepción parcial).

    Usa compare-and-swap manual sobre `detalle.version` en vez de una
    dependencia como django-concurrency: si otra recepción tocó la misma
    línea entre la lectura y este guardado, `ConcurrencyError` avisa al
    llamador para que refresque el objeto y reintente.
    """
    if cantidad <= 0:
        raise ValueError('La cantidad recibida debe ser mayor a cero.')
    nueva_cantidad_recibida = detalle.cantidad_recibida + cantidad
    if nueva_cantidad_recibida > detalle.cantidad_solicitada:
        raise ValueError(
            f'La recepción ({nueva_cantidad_recibida}) excede lo solicitado ({detalle.cantidad_solicitada}).',
        )
    nuevo_estado = (
        OrdenCompraDetalle.Estado.COMPLETO
        if nueva_cantidad_recibida == detalle.cantidad_solicitada
        else OrdenCompraDetalle.Estado.PARCIAL
    )

    filas = OrdenCompraDetalle.objects.filter(pk=detalle.pk, version=detalle.version).update(
        cantidad_recibida=nueva_cantidad_recibida, estado=nuevo_estado, version=F('version') + 1,
    )
    if filas == 0:
        raise ConcurrencyError(
            'La línea fue modificada por otra recepción; recarga la orden de compra e intenta de nuevo.',
        )
    detalle.refresh_from_db()

    recepcion = RecepcionLote.objects.create(
        orden_compra=detalle.orden_compra, orden_compra_detalle=detalle, numero_lote=numero_lote,
        tipo_item=detalle.tipo_item, cantidad_recibida=cantidad, bodega_destino=bodega,
        custodio_receptor=custodio_receptor, recepcionado_por=usuario,
    )

    if detalle.tipo_item == OrdenCompraDetalle.TipoItem.CONSUMIBLE and detalle.tipo_consumible:
        registrar_ingreso_stock(bodega=bodega, tipo_consumible=detalle.tipo_consumible, cantidad=cantidad)
        MovimientoInventario.objects.create(
            tipo_movimiento=MovimientoInventario.TipoMovimiento.INGRESO_CONSUMIBLE,
            tipo_consumible=detalle.tipo_consumible, cantidad=cantidad, bodega_destino=bodega,
            orden_compra=detalle.orden_compra, recepcion_lote=recepcion, realizado_por=usuario,
            motivo=f'Recepción OC {detalle.orden_compra.numero_oc}, lote {numero_lote or recepcion.uuid}',
        )

    _recalcular_estado_orden_compra(detalle.orden_compra)
    return recepcion


@transaction.atomic
def registrar_traslado_bodega(*, tipo_consumible, bodega_origen, bodega_destino, cantidad, usuario, motivo=''):
    if bodega_origen == bodega_destino:
        raise ValueError('La bodega de origen y destino no pueden ser la misma.')
    if cantidad <= 0:
        raise ValueError('La cantidad a trasladar debe ser mayor a cero.')

    StockBodega.objects.get_or_create(bodega=bodega_origen, tipo_consumible=tipo_consumible)
    StockBodega.objects.get_or_create(bodega=bodega_destino, tipo_consumible=tipo_consumible)
    # Bloquear las dos filas en un orden fijo (por bodega_id, no en el orden
    # origen/destino que use cada llamada): dos traslados concurrentes en direcciones
    # opuestas (A->B y B->A al mismo tiempo) podrían si no formar un ciclo de espera
    # (deadlock) — BUG-1 de la auditoría de gobernanza (22-ago-2026).
    filas = {
        fila.bodega_id: fila
        for fila in StockBodega.objects.select_for_update().filter(
            bodega__in=[bodega_origen, bodega_destino], tipo_consumible=tipo_consumible,
        ).order_by('bodega_id')
    }
    origen = filas[bodega_origen.pk]
    if origen.cantidad < cantidad:
        raise ValueError(
            f'Stock insuficiente de {tipo_consumible.nombre} en {bodega_origen.codigo} '
            f'({origen.cantidad} disponibles).',
        )
    StockBodega.objects.filter(pk=origen.pk).update(cantidad=F('cantidad') - cantidad)
    StockBodega.objects.filter(pk=filas[bodega_destino.pk].pk).update(cantidad=F('cantidad') + cantidad)

    return MovimientoInventario.objects.create(
        tipo_movimiento=MovimientoInventario.TipoMovimiento.TRASLADO, tipo_consumible=tipo_consumible,
        cantidad=cantidad, bodega_origen=bodega_origen, bodega_destino=bodega_destino,
        realizado_por=usuario, motivo=motivo,
    )


@transaction.atomic
def registrar_ajuste_inventario(*, bodega, tipo_consumible, cantidad_delta, motivo, usuario):
    """Corrige el stock de una bodega sin pasar por una recepción ni un traslado —
    conteo físico, merma, error de carga (BUG-3 de la auditoría de gobernanza,
    22-ago-2026: MovimientoInventario.TipoMovimiento.AJUSTE existía en las choices
    desde siempre, pero nada lo generaba nunca).

    `cantidad_delta` positivo = sobró (se encontró más de lo registrado); negativo =
    faltó (merma/pérdida) — nunca cero. `motivo` es obligatorio: a diferencia de un
    ingreso o un traslado, un ajuste no tiene ningún documento de respaldo que
    explique el cambio por sí solo.
    """
    if cantidad_delta == 0:
        raise ValueError('La cantidad del ajuste no puede ser cero.')
    if not motivo.strip():
        raise ValueError('Un ajuste de inventario necesita un motivo.')

    stock = _obtener_y_bloquear_stock(bodega, tipo_consumible)
    if stock.cantidad + cantidad_delta < 0:
        raise ValueError(
            f'El ajuste dejaría el stock de {tipo_consumible.nombre} en {bodega.codigo} en negativo '
            f'({stock.cantidad} disponibles, ajuste de {cantidad_delta}).',
        )
    StockBodega.objects.filter(pk=stock.pk).update(cantidad=F('cantidad') + cantidad_delta)

    return MovimientoInventario.objects.create(
        tipo_movimiento=MovimientoInventario.TipoMovimiento.AJUSTE, tipo_consumible=tipo_consumible,
        cantidad=cantidad_delta,
        bodega_destino=bodega if cantidad_delta > 0 else None,
        bodega_origen=bodega if cantidad_delta < 0 else None,
        realizado_por=usuario, motivo=motivo,
    )


@transaction.atomic
def anular_recepcion_lote(*, recepcion, usuario, motivo=''):
    """Revierte una RecepcionLote mal cargada (cantidad o lote equivocado) — BUG-3:
    RecepcionLote.Estado.ANULADO existía en las choices desde siempre, pero nada lo
    asignaba nunca. Retrocede OrdenCompraDetalle.cantidad_recibida/estado con el mismo
    compare-and-swap manual que registrar_recepcion_lote, y si era un consumible
    descuenta el stock que había ingresado — nunca borra la recepción original, deja
    un MovimientoInventario de ajuste a la baja como contrapartida (el kardex nunca
    borra una fila).
    """
    if recepcion.estado == RecepcionLote.Estado.ANULADO:
        raise ValueError('Esta recepción ya está anulada.')

    detalle = recepcion.orden_compra_detalle
    nueva_cantidad_recibida = detalle.cantidad_recibida - recepcion.cantidad_recibida
    nuevo_estado = (
        OrdenCompraDetalle.Estado.COMPLETO if nueva_cantidad_recibida == detalle.cantidad_solicitada
        else OrdenCompraDetalle.Estado.PARCIAL if nueva_cantidad_recibida > 0
        else OrdenCompraDetalle.Estado.PENDIENTE
    )
    filas = OrdenCompraDetalle.objects.filter(pk=detalle.pk, version=detalle.version).update(
        cantidad_recibida=nueva_cantidad_recibida, estado=nuevo_estado, version=F('version') + 1,
    )
    if filas == 0:
        raise ConcurrencyError(
            'La línea fue modificada por otra recepción; recarga la orden de compra e intenta de nuevo.',
        )
    detalle.refresh_from_db()

    if detalle.tipo_item == OrdenCompraDetalle.TipoItem.CONSUMIBLE and detalle.tipo_consumible:
        stock = _obtener_y_bloquear_stock(recepcion.bodega_destino, detalle.tipo_consumible)
        if stock.cantidad < recepcion.cantidad_recibida:
            raise ValueError(
                f'No se puede anular: ya se usó parte de ese stock ({stock.cantidad} disponibles, '
                f'se necesitan {recepcion.cantidad_recibida} para revertir la recepción).',
            )
        StockBodega.objects.filter(pk=stock.pk).update(cantidad=F('cantidad') - recepcion.cantidad_recibida)
        MovimientoInventario.objects.create(
            tipo_movimiento=MovimientoInventario.TipoMovimiento.AJUSTE, tipo_consumible=detalle.tipo_consumible,
            cantidad=-recepcion.cantidad_recibida, bodega_origen=recepcion.bodega_destino,
            recepcion_lote=recepcion, realizado_por=usuario,
            motivo=motivo or f'Anulación de recepción {recepcion.numero_lote or recepcion.uuid}',
        )

    recepcion.estado = RecepcionLote.Estado.ANULADO
    recepcion.save(update_fields=['estado'])
    _recalcular_estado_orden_compra(detalle.orden_compra)
    return recepcion


def scope_movimientos_visibles(queryset, user):
    """`MovimientoInventario` no tiene su propio `unidad_negocio` — se escopa por la(s)
    bodega(s) involucradas (`bodega_origen`/`bodega_destino`, uno de los dos puede ser
    null según el tipo de movimiento). El lado no aplicable pasa siempre; solo se exige
    que el/los lado(s) presentes sean compartidos (`unidad_negocio=None`) o visibles."""
    from apps.cuentas.services import unidades_negocio_visibles, usuario_tiene_acceso_total

    if usuario_tiene_acceso_total(user):
        return queryset
    visibles = unidades_negocio_visibles(user)
    return queryset.filter(
        Q(bodega_origen__isnull=True) | Q(bodega_origen__unidad_negocio__isnull=True) |
        Q(bodega_origen__unidad_negocio__in=visibles),
    ).filter(
        Q(bodega_destino__isnull=True) | Q(bodega_destino__unidad_negocio__isnull=True) |
        Q(bodega_destino__unidad_negocio__in=visibles),
    )


def vincular_activos_por_numero_serie() -> int:
    """Cruza `Estacion.numero_serie` (reportado por el agente RMM) contra
    `Activo.numero_serie` para vincular automáticamente el registro de ITAM con su
    identidad de red. Nunca adivina: si el número de serie no matchea con exactamente
    un Activo (0 o varios), esa estación se deja sin vincular. Idempotente — no repite
    trabajo en estaciones que ya tienen un Activo vinculado."""
    from apps.catalogo.models import Estacion

    vinculados = 0
    estaciones = Estacion.objects.exclude(numero_serie='').filter(activo_vinculado__isnull=True)
    for estacion in estaciones:
        candidatos = list(Activo.objects.filter(numero_serie__iexact=estacion.numero_serie, estacion__isnull=True))
        if len(candidatos) == 1:
            # La Estación ya sabe en qué farmacia está -- se sincroniza de una vez para
            # no depender de que alguien lo cargue a mano para equipos que sí tienen RMM.
            candidatos[0].estacion = estacion
            candidatos[0].farmacia = estacion.farmacia
            candidatos[0].save(update_fields=['estacion', 'farmacia'])
            vinculados += 1
    return vinculados


def activos_dados_de_baja_pero_conectados():
    """Un Activo marcado como dado de baja cuya Estación vinculada sigue reportando
    heartbeat — indicio de una baja mal hecha o de un número de serie duplicado."""
    from apps.catalogo.models import Estacion

    return Activo.objects.filter(
        estado=Activo.Estado.DADO_DE_BAJA, estacion__isnull=False,
        estacion__estado_conexion=Estacion.EstadoConexion.ONLINE,
    ).select_related('estacion', 'estacion__farmacia')


def activos_movidos_sin_registro():
    """Un Activo cuya unidad de negocio conocida no coincide con la de la farmacia
    donde su Estación vinculada está reportando — el equipo se movió físicamente sin
    que nadie actualizara el registro en ITAM."""
    return Activo.objects.filter(estacion__isnull=False, unidad_negocio__isnull=False).exclude(
        unidad_negocio_id=F('estacion__farmacia__unidad_negocio_id'),
    ).select_related('estacion', 'estacion__farmacia', 'unidad_negocio')


def activos_por_vencer_garantia(dias=30):
    """Activos con garantía ya vencida o por vencer dentro de `dias`. Incluye ambos
    casos (no solo "por vencer") — el panel los distingue por color según si la fecha
    ya pasó o no."""
    limite = timezone.now().date() + datetime.timedelta(days=dias)
    return Activo.objects.exclude(estado=Activo.Estado.DADO_DE_BAJA).filter(
        vencimiento_garantia__isnull=False, vencimiento_garantia__lte=limite,
    ).select_related('marca', 'bodega_actual', 'colaborador_actual').order_by('vencimiento_garantia')


def stock_bajo_minimo():
    """Filas de `StockBodega` cuya cantidad cayó debajo del `stock_minimo` configurado
    en su `TipoConsumible` (0 = ese tipo no se vigila)."""
    return StockBodega.objects.filter(
        tipo_consumible__stock_minimo__gt=0, cantidad__lt=F('tipo_consumible__stock_minimo'),
    ).select_related('bodega', 'tipo_consumible')


def crear_activos_desde_estaciones(*, usuario, tipo=Activo.Tipo.DESKTOP, aplicar=False, estaciones=None) -> dict:
    """Da de alta en ITAM los equipos que el RMM ya conoce, con el hardware que el
    agente reporta.

    Por qué existe: el agente ya sabe el número de serie, el procesador, la RAM, el
    disco y en qué farmacia está. Aun así el activo había que cargarlo a mano, uno por
    uno — por eso hay 8 estaciones enroladas y 3 activos. A 1.800 equipos eso no es
    lento, es imposible: el inventario nunca se pondría al día.

    Con esto, cada agente que se instala se convierte en un activo inventariado y
    vinculado. El rollout deja de ser un trabajo aparte del inventario y pasa a ser el
    que lo llena.

    Reglas, todas conservadoras:

    - Solo estaciones APROBADAS. Una pendiente de aprobación todavía no es un equipo
      de la flota, y crearle un activo sería inventariar algo que quizá se rechaza.
    - Solo con número de serie. Sin él no hay forma de reconocer el equipo después ni
      de evitar duplicarlo.
    - Nunca duplica: se saltea la estación que ya tiene activo vinculado, y si el
      número de serie ya existe en ITAM, VINCULA el activo existente en vez de crear
      otro. Esa es la diferencia entre poblar el inventario y ensuciarlo.
    - `tipo` no se adivina: el agente reporta hardware, no el formato del equipo
      (un desktop y un servidor se ven igual desde adentro). Por eso es un parámetro,
      con Desktop de default por ser el caso masivo en farmacia.

    `estaciones` acota el alta a un subconjunto (lo usa `apps.aperturas`, que da de alta
    la estación que acaba de enrolarse en una farmacia que abre, no la flota entera).
    Vacío = todas las aprobadas, que es el caso del comando de management.

    Devuelve un resumen con qué haría/hizo. Con `aplicar=False` no escribe nada.
    """
    from apps.catalogo.models import Estacion

    resumen = {'creados': 0, 'vinculados': 0, 'sin_serie': 0, 'ya_vinculadas': 0, 'detalle': []}

    candidatas = Estacion.objects.filter(
        estado_aprobacion=Estacion.EstadoAprobacion.APROBADA,
    ).select_related('farmacia__unidad_negocio').order_by('codigo')
    if estaciones is not None:
        candidatas = candidatas.filter(pk__in=[e.pk for e in estaciones])

    for estacion in candidatas:
        if getattr(estacion, 'activo_vinculado', None) is not None:
            resumen['ya_vinculadas'] += 1
            continue
        serie = (estacion.numero_serie or '').strip()
        if not serie:
            resumen['sin_serie'] += 1
            resumen['detalle'].append(f'{estacion.codigo}: sin número de serie, se omite')
            continue

        existente = Activo.objects.filter(numero_serie__iexact=serie).first()
        if existente is not None:
            # Ya estaba en ITAM cargado a mano: se vincula, no se duplica.
            resumen['vinculados'] += 1
            resumen['detalle'].append(f'{estacion.codigo}: vincula con {existente.codigo} (serie ya en ITAM)')
            if aplicar:
                existente.estacion = estacion
                existente.save(update_fields=['estacion'])
            continue

        resumen['creados'] += 1
        resumen['detalle'].append(
            f'{estacion.codigo}: crea activo en {estacion.farmacia.codigo} (serie {serie})',
        )
        if aplicar:
            activo = registrar_ingreso(
                tipo=tipo, marca=None, categoria=None, modelo='',
                numero_serie=serie,
                procesador=estacion.procesador or '',
                # El agente reporta MB; ITAM guarda GB.
                ram_gb=round(estacion.ram_total_mb / 1024) if estacion.ram_total_mb else None,
                almacenamiento_gb=estacion.almacenamiento_total_gb,
                fecha_compra=None, vencimiento_garantia=None, orden_compra=None,
                farmacia=estacion.farmacia, usuario=usuario,
                # Está funcionando en una farmacia: no es un equipo nuevo de caja.
                estado_fisico=Activo.EstadoFisico.BUENO,
            )
            activo.estacion = estacion
            activo.save(update_fields=['estacion'])

    return resumen


def datos_hardware_desde_estacion(numero_serie: str) -> dict | None:
    """Especificaciones que el agente RMM ya reportó para ese número de serie.

    Evita reingresar a mano lo que la estación reporta sola, y —más importante—
    evita que el inventario y el monitoreo digan cosas distintas del mismo equipo.

    Devuelve None si no hay serie, si ninguna estación coincide, o si coincide más de
    una (serie duplicada o dato sucio): en ese caso NO se adivina, mismo criterio que
    vincular_activos_por_numero_serie.

    Solo se incluyen los campos que la estación realmente tiene cargados, para no
    pisar con vacíos lo que el usuario ya haya escrito.
    """
    from apps.catalogo.models import Estacion

    serie = (numero_serie or '').strip()
    if not serie:
        return None

    coincidencias = list(Estacion.objects.filter(numero_serie__iexact=serie)[:2])
    if len(coincidencias) != 1:
        return None

    estacion = coincidencias[0]
    datos: dict = {'estacion': estacion}
    if estacion.procesador:
        datos['procesador'] = estacion.procesador
    if estacion.ram_total_mb:
        # La estación reporta MB y el activo se lleva en GB: copiar el número tal cual
        # metería "7839" en un campo de gigabytes. Se redondea al entero más cercano
        # porque el fabricante reserva algo de RAM y 7839 MB es un equipo de 8 GB.
        datos['ram_gb'] = round(estacion.ram_total_mb / 1024)
    if estacion.almacenamiento_total_gb:
        datos['almacenamiento_gb'] = estacion.almacenamiento_total_gb
    return datos


# --- Topología de infraestructura de una farmacia ---
#
# Hasta ahora el inventario solo conocía los equipos con agente RMM: las estaciones se
# dan de alta solas (`crear_activos_desde_estaciones`) y el resto de la farmacia —el
# router, el switch, las impresoras, los medianet— no existía en ningún lado. Cuando se
# cae un local, eso es justamente lo que nadie puede mirar: qué hay en el rack y con qué
# IP.
#
# El equipamiento de una farmacia sigue un patrón fijo que se repite igual en todas:
# un bloque de equipos de rol único (router, switch, VoIP, biométrico, cámaras y alarmas
# SIPAO, impresora de oficina) y, por cada estación, su impresora térmica y su medianet.
# De ahí que el catálogo no sea una lista fija sino algo que se expande según cuántas
# estaciones tenga realmente la farmacia.
#
# Lo que este módulo NO modela es el direccionamiento: subred, máscara, gateway y rango
# DHCP no viven todavía en ningún campo, y no se inventó ninguno acá. Por eso el orden
# de los bloques (que sí importa para asignar IPs) es irrelevante para este código: se
# crean activos, no direcciones.


@dataclass(frozen=True)
class SlotTopologia:
    """Un puesto del equipamiento estándar de una farmacia.

    El `slot` es la identidad del puesto dentro de la farmacia: 'mikrotik' para los de
    rol único, 'impresora_A' / 'medianet_B' para los que cuelgan de una estación. Es lo
    que hace que se pueda decir "la impresora de la caja A" — el modelo no tiene ninguna
    otra forma de saberlo, porque `Activo.estacion` significa "este activo ES esa
    estación", no "cuelga de ella".

    `categoria_codigo` mapea a `CategoriaEquipo`, que es el eje que ya usa mantenimiento
    para decidir qué checklist aplica: un MikroTik y un switch tonto comparten
    `Tipo.RED` pero no se revisan igual.
    """

    slot: str
    tipo: str
    categoria_codigo: str
    categoria_nombre: str
    ubicacion_interna: str
    marca: str = ''
    modelo: str = ''


# Equipos de los que hay exactamente uno por farmacia. Confirmados como universales por
# Ronald el 12-sep-2026: si mañana aparece una farmacia sin biométrico, el alta se acota
# con `--slots` en vez de sacarlo del catálogo.
TOPOLOGIA_ROL_UNICO = (
    SlotTopologia(
        slot='mikrotik', tipo=Activo.Tipo.RED,
        categoria_codigo='ROUTER-MIKROTIK', categoria_nombre='Router MikroTik',
        ubicacion_interna=UbicacionInterna.RACK, marca='MikroTik',
    ),
    SlotTopologia(
        slot='switch', tipo=Activo.Tipo.RED,
        categoria_codigo='SWITCH-NOADMIN', categoria_nombre='Switch no administrable',
        ubicacion_interna=UbicacionInterna.RACK, modelo='Switch 24 puertos',
    ),
    SlotTopologia(
        slot='voip', tipo=Activo.Tipo.TELEFONO,
        categoria_codigo='TELEFONO-IP', categoria_nombre='Teléfono IP',
        ubicacion_interna=UbicacionInterna.OFICINA,
    ),
    SlotTopologia(
        slot='biometrico', tipo=Activo.Tipo.BIOMETRICO,
        categoria_codigo='BIOMETRICO', categoria_nombre='Biométrico',
        ubicacion_interna=UbicacionInterna.OFICINA,
    ),
    SlotTopologia(
        slot='camaras_sipao', tipo=Activo.Tipo.CAMARA,
        categoria_codigo='CAMARAS-SIPAO', categoria_nombre='Cámaras SIPAO',
        ubicacion_interna=UbicacionInterna.RACK,
    ),
    SlotTopologia(
        slot='alarmas_sipao', tipo=Activo.Tipo.SIPAO_ALARMA,
        categoria_codigo='ALARMAS-SIPAO', categoria_nombre='Alarmas SIPAO',
        ubicacion_interna=UbicacionInterna.RACK,
    ),
    # No está en el esquema de direccionamiento fijo porque va por WiFi con DHCP: su IP
    # cambia sola y por eso `Activo.ip` no lleva unique.
    SlotTopologia(
        slot='impresora_oficina', tipo=Activo.Tipo.IMPRESORA,
        categoria_codigo='IMPRESORA-TINTA', categoria_nombre='Impresora de tinta',
        ubicacion_interna=UbicacionInterna.OFICINA, marca='Epson', modelo='L3250',
    ),
)

# Lo que cuelga de CADA estación, 1:1. El `%s` se reemplaza por el sufijo del código de
# estación (`ML016-A` -> `A`), que es la convención que ya usa todo el sistema
# (`codigo_estacion_validator`, `PerfilEstacionPlantilla.sufijo`).
TOPOLOGIA_POR_ESTACION = (
    SlotTopologia(
        slot='impresora_%s', tipo=Activo.Tipo.IMPRESORA,
        categoria_codigo='IMPRESORA-TERMICA', categoria_nombre='Impresora térmica',
        ubicacion_interna=UbicacionInterna.CAJA,
    ),
    SlotTopologia(
        slot='medianet_%s', tipo=Activo.Tipo.PINPAD,
        categoria_codigo='PINPAD-MEDIANET', categoria_nombre='Pinpad Medianet',
        ubicacion_interna=UbicacionInterna.CAJA,
    ),
)

SLOT_ESTACION = 'estacion_%s'

# Estaciones que NO llevan impresora ni medianet. BASE es el servidor de la farmacia:
# tiene su dirección en el esquema (el bloque de estaciones la incluye) pero no atiende
# público, así que no tiene ni impresora térmica ni pinpad. Confirmado contra la planilla
# real de GMI04, donde hay 5 estaciones y solo 4 impresoras y 4 medianet.
SUFIJOS_SIN_PERIFERICOS = frozenset({'BASE'})


def sufijo_de_estacion(codigo: str) -> str:
    """`ML016-A` -> `A`. El código de estación es siempre FARMACIA-SUFIJO
    (`codigo_estacion_validator`), así que partir por el primer guion es seguro."""
    return codigo.split('-', 1)[1] if '-' in codigo else codigo


def estaciones_de_topologia(farmacia):
    """Las estaciones que cuentan para armar la topología: solo las APROBADAS.

    Una pendiente de aprobación todavía no es un equipo de la flota —puede ser un
    enrolamiento que se rechaza— y darle impresora y medianet sería inventariar
    periféricos de una caja que quizá no existe. Mismo criterio que
    `crear_activos_desde_estaciones`.
    """
    from apps.catalogo.models import Estacion

    return Estacion.objects.filter(
        farmacia=farmacia, estado_aprobacion=Estacion.EstadoAprobacion.APROBADA,
    ).order_by('codigo')


def slots_de_farmacia(farmacia) -> list:
    """Expande el catálogo estándar para ESTA farmacia, según sus estaciones reales.

    Una farmacia con 2 cajas lleva 2 impresoras y 2 medianet; una con 4, cuatro de cada
    uno. El número no está fijo en ningún lado: sale de contar las estaciones aprobadas,
    salteando las de `SUFIJOS_SIN_PERIFERICOS`.
    """
    slots = list(TOPOLOGIA_ROL_UNICO)
    for estacion in estaciones_de_topologia(farmacia):
        sufijo = sufijo_de_estacion(estacion.codigo)
        if sufijo in SUFIJOS_SIN_PERIFERICOS:
            continue
        for plantilla in TOPOLOGIA_POR_ESTACION:
            slots.append(replace(plantilla, slot=plantilla.slot % sufijo))
    return slots


def _validar_dato_de_red(campo, valor):
    """Corre los validadores del propio modelo sobre un valor que vino de afuera.

    Se valida acá y no solo al guardar porque `registrar_ingreso` hace `save()` directo,
    sin `full_clean()`: una MAC mal tipeada en la planilla entraría a la base sin que
    nada chille. Reusar el campo del modelo evita tener el formato escrito dos veces.
    """
    Activo._meta.get_field(campo).clean(valor, None)


def _activos_vigentes(farmacia):
    return Activo.objects.filter(farmacia=farmacia).exclude(estado=Activo.Estado.DADO_DE_BAJA)


def _adoptar_estaciones(farmacia, aplicar, resumen):
    """Le pone slot (`estacion_A`) al Activo de cada estación, que ya existe.

    Esos activos los crea `crear_activos_desde_estaciones` a partir de lo que reporta el
    agente; acá solo se los ubica dentro del esquema de la farmacia para que el
    vocabulario de slots esté completo y `completar_datos_topologia` pueda referirse a
    ellos. No se crea ninguno: si una estación no tiene activo vinculado, ese es un
    problema de vinculación por número de serie, no de topología.
    """
    for estacion in estaciones_de_topologia(farmacia):
        activo = getattr(estacion, 'activo_vinculado', None)
        if activo is None or activo.estado == Activo.Estado.DADO_DE_BAJA:
            continue
        slot = SLOT_ESTACION % sufijo_de_estacion(estacion.codigo)
        if activo.slot == slot:
            continue
        if activo.slot:
            resumen['ambiguos'].append(
                '%s: %s ya tiene el slot "%s" y le correspondería "%s"; no se pisa'
                % (slot, activo.codigo, activo.slot, slot),
            )
            continue
        resumen['adoptados'].append('%s: %s (estación %s)' % (slot, activo.codigo, estacion.codigo))
        if aplicar:
            activo.slot = slot
            activo.save(update_fields=['slot'])


def crear_topologia_farmacia(*, farmacia, usuario, slots=None, datos=None, aplicar=False) -> dict:
    """Inventaria el equipamiento sin agente de una farmacia, expandido según sus cajas.

    Por defecto SIMULA. Es un alta sobre el inventario real y los datos los aporta una
    persona mirando las etiquetas, así que conviene ver la lista antes de escribirla.

    `slots` acota a un subconjunto (por ejemplo `['biometrico']` para la farmacia que lo
    tiene aparte). Vacío = el catálogo completo que le corresponda a esta farmacia.

    `datos` es `{slot: {'ip':…, 'mac':…, 'numero_serie':…}}` con lo que se conozca de
    verdad. **Lo que no venga queda vacío**, nunca con un valor de relleno: un activo con
    la serie en blanco es información incompleta y se ve como tal; uno con una serie
    inventada es peor que no tenerlo, porque alguien la va a creer más adelante.

    Tres resultados posibles por puesto, y la diferencia importa:

    - **creado**: no había nada de esa categoría, se da de alta.
    - **adoptado**: ya existía un activo de esa categoría sin slot y el puesto es uno
      solo, así que no hay duda de cuál es — se le pone el slot en vez de crear un
      duplicado. Es el caso del piloto de ML016, cargado antes de que existieran los
      slots.
    - **ambiguo**: hay activos sin slot de esa categoría pero más de un puesto posible
      (dos cajas, una sola impresora cargada). Cuál es cuál no se puede deducir, así que
      **no se crea ni se adopta nada** de esa categoría y se reporta para que un humano
      lo resuelva. Crear igual dejaría tres registros para dos equipos físicos.
    """
    catalogo = slots_de_farmacia(farmacia)
    por_nombre = {s.slot: s for s in catalogo}
    if slots is not None:
        desconocidos = set(slots) - set(por_nombre)
        if desconocidos:
            raise ValueError(
                'Slots que no corresponden a esta farmacia: %s. Disponibles: %s.'
                % (', '.join(sorted(desconocidos)), ', '.join(sorted(por_nombre))),
            )
        catalogo = [por_nombre[s] for s in slots]

    datos = datos or {}
    sobrantes = set(datos) - {s.slot for s in catalogo}
    if sobrantes:
        raise ValueError(
            'Hay datos para slots que no se van a crear: %s.' % ', '.join(sorted(sobrantes)),
        )

    # Validar TODO antes de escribir nada: si la tercera MAC de la planilla está mal, no
    # queremos la farmacia a medio inventariar.
    for valores in datos.values():
        if valores.get('ip'):
            _validar_dato_de_red('ip', valores['ip'])
        if valores.get('mac'):
            _validar_dato_de_red('mac', valores['mac'])

    resumen = {'creados': [], 'adoptados': [], 'omitidos': [], 'ambiguos': [], 'incompletos': []}
    _adoptar_estaciones(farmacia, aplicar, resumen)

    ocupados = set(_activos_vigentes(farmacia).exclude(slot='').values_list('slot', flat=True))
    pendientes = []
    for slot in catalogo:
        if slot.slot in ocupados:
            resumen['omitidos'].append('%s: ya está inventariado en %s' % (slot.slot, farmacia.codigo))
        else:
            pendientes.append(slot)

    # Agrupar por categoría para poder distinguir "no hay duda de cuál es" de "hay que
    # preguntarle a alguien".
    por_categoria = {}
    for slot in pendientes:
        por_categoria.setdefault(slot.categoria_codigo, []).append(slot)

    for categoria_codigo, slots_categoria in por_categoria.items():
        sin_slot = list(
            _activos_vigentes(farmacia).filter(categoria__codigo=categoria_codigo, slot=''),
        )
        if sin_slot and not (len(sin_slot) == 1 and len(slots_categoria) == 1):
            resumen['ambiguos'].append(
                '%s: %s tiene %d activo(s) sin slot de esa categoría (%s) y %d puesto(s) '
                'posible(s) (%s). No se puede deducir cuál es cuál; asignales el slot a mano '
                'y volvé a correr.'
                % (categoria_codigo, farmacia.codigo, len(sin_slot),
                   ', '.join(a.codigo for a in sin_slot), len(slots_categoria),
                   ', '.join(s.slot for s in slots_categoria)),
            )
            continue

        for slot in slots_categoria:
            valores = datos.get(slot.slot, {})
            ip = valores.get('ip') or None
            mac = valores.get('mac') or ''
            numero_serie = valores.get('numero_serie') or ''
            faltantes = [
                nombre for nombre, valor in
                (('ip', ip), ('mac', mac), ('numero_serie', numero_serie)) if not valor
            ]

            if sin_slot:
                existente = sin_slot[0]
                resumen['adoptados'].append(
                    '%s: %s (%s, ya existía sin slot)'
                    % (slot.slot, existente.codigo, slot.categoria_nombre),
                )
                if aplicar:
                    existente.slot = slot.slot
                    existente.save(update_fields=['slot'])
                if not existente.ip or not existente.mac or not existente.numero_serie:
                    resumen['incompletos'].append(
                        '%s (%s): falta %s' % (
                            existente.codigo, slot.slot,
                            ', '.join(
                                c for c, v in (('ip', existente.ip), ('mac', existente.mac),
                                               ('numero_serie', existente.numero_serie)) if not v
                            ),
                        ),
                    )
                continue

            if not aplicar:
                resumen['creados'].append(
                    '%s: crearía %s en %s' % (slot.slot, slot.categoria_nombre, farmacia.codigo),
                )
                if faltantes:
                    resumen['incompletos'].append(
                        '%s: faltaría %s' % (slot.slot, ', '.join(faltantes)),
                    )
                continue

            categoria, _ = CategoriaEquipo.objects.get_or_create(
                codigo=slot.categoria_codigo, defaults={'nombre': slot.categoria_nombre},
            )
            marca = Marca.objects.get_or_create(nombre=slot.marca)[0] if slot.marca else None
            activo = registrar_ingreso(
                tipo=slot.tipo, marca=marca, categoria=categoria, modelo=slot.modelo,
                numero_serie=numero_serie, fecha_compra=None, vencimiento_garantia=None,
                orden_compra=None, farmacia=farmacia, usuario=usuario,
                ip=ip, mac=mac, ubicacion_interna=slot.ubicacion_interna, slot=slot.slot,
            )
            resumen['creados'].append(
                '%s: %s (%s)' % (slot.slot, activo.codigo, slot.categoria_nombre),
            )
            if faltantes:
                resumen['incompletos'].append(
                    '%s (%s): falta %s' % (activo.codigo, slot.slot, ', '.join(faltantes)),
                )

    return resumen


CAMPOS_COMPLETABLES = ('ip', 'mac', 'numero_serie')


class ErroresDeCarga(Exception):
    """Una o más filas de la planilla no se pudieron aplicar. No se escribió nada."""

    def __init__(self, errores):
        self.errores = errores
        super().__init__('%d fila(s) con problemas; no se escribió nada.' % len(errores))


def completar_datos_topologia(*, filas, usuario, aplicar=False) -> dict:
    """Carga ip/mac/numero_serie en activos que YA existen, desde la planilla de IPs.

    Todo o nada: se resuelve y valida la planilla entera antes de escribir la primera
    fila, y cualquier problema aborta la corrida completa con la lista de los problemas.
    Media planilla aplicada es peor que ninguna — nadie sabría desde dónde retomar.

    **Nunca pisa un valor que ya está.** Si la planilla trae una IP distinta de la
    cargada, eso es un conflicto que resuelve una persona, no un `UPDATE`: puede ser que
    el equipo se haya movido, o que la planilla esté vieja. Si trae el mismo valor, no
    hace nada y no lo cuenta como cambio.

    Tampoco acepta ip/mac para un activo con estación vinculada: esos los reporta el
    agente (ver `Activo.clean`), y cargarlos a mano crearía una segunda versión del mismo
    dato.

    Cada activo tocado deja un `EventoActivo` con qué campos se cargaron: el historial de
    un activo es de auditoría permanente y una carga de datos de red no es menos real que
    una asignación.
    """
    from apps.catalogo.models import Farmacia

    errores = []
    planeado = []
    vistos = set()

    for numero, fila in enumerate(filas, start=1):
        codigo_farmacia = (fila.get('farmacia') or '').strip()
        slot = (fila.get('slot') or '').strip()
        etiqueta = 'fila %d (%s/%s)' % (numero, codigo_farmacia or '?', slot or '?')

        if not codigo_farmacia or not slot:
            errores.append('%s: faltan "farmacia" o "slot".' % etiqueta)
            continue
        if (codigo_farmacia, slot) in vistos:
            errores.append('%s: repetida en la planilla.' % etiqueta)
            continue
        vistos.add((codigo_farmacia, slot))

        farmacia = Farmacia.objects.filter(codigo=codigo_farmacia).first()
        if farmacia is None:
            errores.append('%s: no existe la farmacia "%s".' % (etiqueta, codigo_farmacia))
            continue

        activo = _activos_vigentes(farmacia).filter(slot=slot).first()
        if activo is None:
            errores.append(
                '%s: %s no tiene ningún activo con slot "%s". Creálo antes con '
                'crear_topologia_farmacia.' % (etiqueta, codigo_farmacia, slot),
            )
            continue

        cambios = {}
        for campo in CAMPOS_COMPLETABLES:
            valor = (fila.get(campo) or '').strip()
            if not valor:
                continue
            actual = getattr(activo, campo)
            if actual and str(actual) == valor:
                continue
            if actual:
                errores.append(
                    '%s: %s ya tiene %s=%s y la planilla trae %s. No se pisa: resolvelo a mano.'
                    % (etiqueta, activo.codigo, campo, actual, valor),
                )
                continue
            if campo in ('ip', 'mac'):
                if activo.estacion_id:
                    errores.append(
                        '%s: %s tiene la estación %s vinculada, su %s la reporta el agente.'
                        % (etiqueta, activo.codigo, activo.estacion.codigo, campo),
                    )
                    continue
                try:
                    _validar_dato_de_red(campo, valor)
                except ValidationError as exc:
                    errores.append('%s: %s inválida (%s).' % (etiqueta, campo, '; '.join(exc.messages)))
                    continue
            cambios[campo] = valor

        if cambios:
            planeado.append((activo, cambios))

    if errores:
        raise ErroresDeCarga(errores)

    resumen = {'actualizados': [], 'sin_cambios': len(filas) - len(planeado), 'incompletos': []}
    for activo, cambios in planeado:
        resumen['actualizados'].append(
            '%s (%s): %s' % (
                activo.codigo, activo.slot,
                ', '.join('%s=%s' % (c, v) for c, v in sorted(cambios.items())),
            ),
        )

    if aplicar:
        with transaction.atomic():
            for activo, cambios in planeado:
                for campo, valor in cambios.items():
                    setattr(activo, campo, valor)
                activo.save(update_fields=list(cambios))
                EventoActivo.objects.create(
                    activo=activo, tipo_evento=EventoActivo.TipoEvento.DATOS_RED_CARGADOS,
                    usuario=usuario, detalle={'slot': activo.slot, **cambios},
                )

    for activo, cambios in planeado:
        # `or cambios.get(c)` para que la simulación informe lo mismo que va a quedar
        # después de aplicar, y no los campos que esta misma planilla viene a llenar.
        faltantes = [c for c in CAMPOS_COMPLETABLES if not (getattr(activo, c) or cambios.get(c))]
        if faltantes:
            resumen['incompletos'].append(
                '%s (%s): sigue sin %s' % (activo.codigo, activo.slot, ', '.join(faltantes)),
            )
    return resumen


# --- Traducción de la planilla de IPs por farmacia ---
#
# La planilla que se mantiene aparte tiene una hoja por farmacia, con una etiqueta y una
# dirección por fila:
#
#     GMI04
#     LOGIN            sangregorio-gmi04
#     RED              10.201.7.224/27
#     MASCARA          255.255.255.224
#     GATEWAY          10.201.7.225
#     BASE             10.201.7.226
#     GMI04-ADM        10.201.7.227
#     ...
#     IMPRESORA-ADM    10.201.7.232
#     ...
#     SIPAO ALARMAS    10.201.7.245
#
# Traducirla acá y no pedirle a nadie que la transcriba a mano evita el error de tipeo en
# la transcripción, que es el más difícil de detectar: una IP mal copiada tiene forma de
# IP válida y nadie la vuelve a mirar.

# Etiquetas que describen la RED, no un equipo. No se ignoran en silencio: se saltean a
# propósito porque el direccionamiento (subred, máscara, login del proveedor) todavía no
# vive en ningún campo del modelo. Cuando exista, salen de acá.
ETIQUETAS_DE_RED = frozenset({'LOGIN', 'RED', 'MASCARA', 'MÁSCARA', 'GATEWAY DHCP', 'DHCP'})

# Etiquetas de rol único de la planilla -> slot del catálogo.
ETIQUETAS_ROL_UNICO = {
    'GATEWAY': 'mikrotik',
    'VOIP': 'voip',
    'BIOMETRICO': 'biometrico',
    'BIOMÉTRICO': 'biometrico',
    'SIPAO CAMARAS': 'camaras_sipao',
    'SIPAO CÁMARAS': 'camaras_sipao',
    'SIPAO ALARMAS': 'alarmas_sipao',
}

# Prefijos con sufijo de estación: "IMPRESORA-A" -> "impresora_A".
PREFIJOS_POR_ESTACION = {
    'IMPRESORA': 'impresora_%s',
    'MEDIANET': 'medianet_%s',
}


def traducir_planilla_ips(*, codigo_farmacia, filas):
    """Convierte las filas de la hoja de una farmacia en filas para `completar_datos_topologia`.

    `filas` son pares `(etiqueta, valor)` tal como salen de la hoja. Devuelve
    `(filas_utiles, omitidas, errores)`:

    - **filas_utiles**: lo que se puede cargar, ya con el slot del catálogo.
    - **omitidas**: lo que se saltea con su motivo — los datos de red que todavía no
      tienen dónde guardarse, y las estaciones (ver abajo).
    - **errores**: etiquetas que no se supo traducir. NO se ignoran: una fila que nadie
      mira es una dirección que queda sin cargar sin que nadie se entere.

    Las estaciones se omiten a propósito. La planilla dice qué IP *debería* tener cada
    caja; el agente reporta la que *tiene*. Cargar la primera encima de la segunda haría
    que el sistema deje de saber cuál es cuál (ver `Activo.clean`). Comparar las dos sería
    valioso —detectaría una caja que se movió de dirección— pero eso necesita un campo
    aparte para la IP planificada, que hoy no existe.
    """
    filas_utiles, omitidas, errores = [], [], []

    for numero, (etiqueta_cruda, valor_crudo) in enumerate(filas, start=1):
        etiqueta = (etiqueta_cruda or '').strip()
        valor = (valor_crudo or '').strip()
        if not etiqueta:
            continue
        clave = etiqueta.upper()

        # El título de la hoja es el código de la farmacia, sin valor al lado.
        if not valor:
            continue
        if clave in ETIQUETAS_DE_RED:
            omitidas.append('%s: dato de red, todavía no hay dónde guardarlo' % etiqueta)
            continue
        if clave in ETIQUETAS_ROL_UNICO:
            filas_utiles.append({
                'farmacia': codigo_farmacia, 'slot': ETIQUETAS_ROL_UNICO[clave],
                'ip': valor, 'mac': '', 'numero_serie': '',
            })
            continue

        prefijo, _, sufijo = clave.partition('-')
        if prefijo in PREFIJOS_POR_ESTACION and sufijo:
            filas_utiles.append({
                'farmacia': codigo_farmacia, 'slot': PREFIJOS_POR_ESTACION[prefijo] % sufijo,
                'ip': valor, 'mac': '', 'numero_serie': '',
            })
            continue

        # La estación propiamente dicha: "BASE" o "GMI04-ADM".
        if clave == 'BASE' or prefijo == codigo_farmacia.upper():
            omitidas.append('%s: es una estación, su IP la reporta el agente' % etiqueta)
            continue

        errores.append(
            'fila %d: no sé qué equipo es "%s" (%s). Agregalo a la traducción o sacalo '
            'de la hoja.' % (numero, etiqueta, valor),
        )

    return filas_utiles, omitidas, errores
