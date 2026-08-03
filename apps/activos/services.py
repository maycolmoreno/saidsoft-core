"""Transiciones de estado del ciclo de vida de un Activo.

Sigue el mismo patrón que apps/despliegues/services.py: la lógica de negocio
vive aquí, separada de las vistas, para que tanto el panel como el admin la
reutilicen igual.
"""
from django.db.models import F

from apps.activos.models import (
    Activo, EventoActivo, MovimientoInventario, OrdenCompraDetalle, RecepcionLote, StockBodega,
)


class ConcurrencyError(Exception):
    """Otra recepción/actualización tocó la misma línea entre lectura y guardado; reintenta."""


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


def registrar_consumible_entregado(*, activo, tipo_consumible, cantidad, usuario):
    if activo.estado != Activo.Estado.ASIGNADO or not activo.colaborador_actual:
        raise ValueError('El activo debe estar asignado a un colaborador para entregarle consumibles.')
    bodega = activo.bodega_actual
    if bodega is None:
        raise ValueError('El activo no tiene bodega de origen registrada.')

    stock, _ = StockBodega.objects.get_or_create(bodega=bodega, tipo_consumible=tipo_consumible)
    if stock.cantidad < cantidad:
        raise ValueError(
            f'Stock insuficiente de {tipo_consumible.nombre} en {bodega.codigo} '
            f'({stock.cantidad} disponibles).',
        )
    stock.cantidad -= cantidad
    stock.save(update_fields=['cantidad'])

    EventoActivo.objects.create(
        activo=activo, tipo_evento=EventoActivo.TipoEvento.CONSUMIBLE_ENTREGADO, usuario=usuario,
        detalle={
            'tipo_consumible': tipo_consumible.nombre, 'cantidad': cantidad,
            'colaborador': activo.colaborador_actual.nombre, 'bodega': bodega.codigo,
        },
    )


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


def registrar_ingreso_stock(*, bodega, tipo_consumible, cantidad):
    stock, _ = StockBodega.objects.get_or_create(bodega=bodega, tipo_consumible=tipo_consumible)
    stock.cantidad += cantidad
    stock.save(update_fields=['cantidad'])
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


def registrar_traslado_bodega(*, tipo_consumible, bodega_origen, bodega_destino, cantidad, usuario, motivo=''):
    if bodega_origen == bodega_destino:
        raise ValueError('La bodega de origen y destino no pueden ser la misma.')
    if cantidad <= 0:
        raise ValueError('La cantidad a trasladar debe ser mayor a cero.')

    origen, _ = StockBodega.objects.get_or_create(bodega=bodega_origen, tipo_consumible=tipo_consumible)
    if origen.cantidad < cantidad:
        raise ValueError(
            f'Stock insuficiente de {tipo_consumible.nombre} en {bodega_origen.codigo} '
            f'({origen.cantidad} disponibles).',
        )
    origen.cantidad -= cantidad
    origen.save(update_fields=['cantidad'])
    registrar_ingreso_stock(bodega=bodega_destino, tipo_consumible=tipo_consumible, cantidad=cantidad)

    return MovimientoInventario.objects.create(
        tipo_movimiento=MovimientoInventario.TipoMovimiento.TRASLADO, tipo_consumible=tipo_consumible,
        cantidad=cantidad, bodega_origen=bodega_origen, bodega_destino=bodega_destino,
        realizado_por=usuario, motivo=motivo,
    )
