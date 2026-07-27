"""Transiciones de estado del ciclo de vida de un Activo.

Sigue el mismo patrón que apps/despliegues/services.py: la lógica de negocio
vive aquí, separada de las vistas, para que tanto el panel como el admin la
reutilicen igual.
"""
from apps.activos.models import Activo, EventoActivo, StockBodega


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
    activo.save(update_fields=['colaborador_actual', 'estado', 'estado_fisico_actual'])
    EventoActivo.objects.create(
        activo=activo, tipo_evento=EventoActivo.TipoEvento.ASIGNACION, usuario=usuario,
        detalle={
            'colaborador': colaborador.nombre, 'cedula': colaborador.cedula,
            'cargo': colaborador.cargo, 'sucursal': colaborador.sucursal,
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
    orden_compra.novedad_recepcion = novedad_recepcion
    orden_compra.estado = orden_compra.Estado.RECIBIDA
    orden_compra.recibido_por = usuario
    orden_compra.save(update_fields=['novedad_recepcion', 'estado', 'recibido_por'])
