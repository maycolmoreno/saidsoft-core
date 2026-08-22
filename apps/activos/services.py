"""Transiciones de estado del ciclo de vida de un Activo.

Sigue el mismo patrón que apps/despliegues/services.py: la lógica de negocio
vive aquí, separada de las vistas, para que tanto el panel como el admin la
reutilicen igual.
"""
import datetime

from django.db import transaction
from django.db.models import F, Q
from django.utils import timezone

from apps.activos.models import (
    Activo, EventoActivo, MovimientoInventario, OrdenCompraDetalle, RecepcionLote, StockBodega,
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
                       vencimiento_garantia, orden_compra, bodega, usuario,
                       categoria=None, procesador='', ram_gb=None, almacenamiento_gb=None,
                       codigo_sap='', condicion_al_recibir=''):
    activo = Activo(
        tipo=tipo, marca=marca, categoria=categoria, modelo=modelo, numero_serie=numero_serie,
        procesador=procesador, ram_gb=ram_gb, almacenamiento_gb=almacenamiento_gb,
        codigo_sap=codigo_sap, condicion_al_recibir=condicion_al_recibir,
        fecha_compra=fecha_compra, vencimiento_garantia=vencimiento_garantia,
        orden_compra=orden_compra, bodega_actual=bodega,
        estado=Activo.Estado.EN_BODEGA, estado_fisico_actual=Activo.EstadoFisico.NUEVO,
    )
    activo.codigo = generar_codigo_activo(tipo)
    activo.save()
    EventoActivo.objects.create(
        activo=activo, tipo_evento=EventoActivo.TipoEvento.INGRESO, usuario=usuario,
        detalle={
            'orden_compra': orden_compra.numero_oc if orden_compra else None,
            'proveedor': orden_compra.proveedor if orden_compra else None,
            'bodega': bodega.codigo,
            'marca': marca.nombre if marca else None,
            'categoria': categoria.nombre if categoria else None,
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
            candidatos[0].estacion = estacion
            candidatos[0].save(update_fields=['estacion'])
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
