"""Generadores de reportes exportables (CSV).

CSV en vez de PDF: es lo que se abre en Excel y se analiza, sin dependencias extra.
Cada función escribe en un objeto tipo-archivo (el HttpResponse) usando el módulo csv
estándar. Cubren cumplimiento de versión, resultado de un despliegue, activos, alertas
y bitácora de acciones. Todos salvo auditoría aceptan `unidad_negocio` para acotar a un
solo cliente (ver "Reportes por cliente"); auditoría queda deliberadamente sin escopar
— es polimórfica (EventoAuditoria no tiene FK real al objeto auditado) y es una
herramienta de cumplimiento interno, no algo que se le entregue a un cliente.
"""
import csv

from django.utils import timezone

from apps.activos.models import Activo
from apps.auditoria.models import EventoAuditoria
from apps.catalogo.models import Estacion
from apps.cuentas.services import scope_opcional_por_unidad_negocio_activa
from apps.despliegues.models import Despliegue, EventoDespliegue, ResultadoDespliegue
from apps.facturacion.services import estaciones_facturables
from apps.monitoreo.models import Alerta


def _escribir(salida, encabezados, filas):
    writer = csv.writer(salida)
    writer.writerow(encabezados)
    writer.writerows(filas)


def reporte_cumplimiento(salida, unidades_negocio=None, grupo_codigo=None):
    """Qué versión corre cada estación vs. la objetivo de su grupo.

    `unidades_negocio`: iterable de UnidadNegocio a las que acotar (una sola, para el
    reporte de un cliente puntual, o varias, para "todas las que este usuario puede
    ver"). `None` = sin acotar por tenant — el llamador es responsable de solo pasar
    `None` cuando quien pide el reporte tiene acceso a todo (ver
    apps.panel.views.reportes.reporte_cumplimiento_csv).
    """
    estaciones = (
        Estacion.objects
        .filter(estado_aprobacion=Estacion.EstadoAprobacion.APROBADA)
        .select_related('farmacia', 'farmacia__grupo')
        .order_by('farmacia__grupo__codigo', 'farmacia__codigo', 'codigo')
    )
    if unidades_negocio is not None:
        estaciones = estaciones.filter(farmacia__unidad_negocio__in=unidades_negocio)
    if grupo_codigo:
        estaciones = estaciones.filter(farmacia__grupo__codigo=grupo_codigo)

    filas = []
    for e in estaciones:
        objetivo = e.farmacia.grupo.version_objetivo
        filas.append([
            e.farmacia.grupo.codigo,
            e.farmacia.codigo,
            e.codigo,
            e.version_pos or '',
            objetivo or '',
            'sí' if e.desactualizada else 'no',
            e.get_estado_conexion_display(),
            e.ultimo_heartbeat.strftime('%Y-%m-%d %H:%M:%S') if e.ultimo_heartbeat else '',
        ])
    _escribir(salida, [
        'grupo', 'farmacia', 'estacion', 'version_pos', 'version_objetivo',
        'desactualizada', 'conexion', 'ultimo_heartbeat',
    ], filas)


def reporte_despliegue(salida, despliegue: Despliegue):
    """Resultado por estación de un despliegue, con los timestamps clave de su línea de tiempo."""
    resultados = (
        despliegue.resultados
        .select_related('estacion', 'estacion__farmacia', 'estacion__farmacia__grupo')
        .prefetch_related('eventos')
        .order_by('estacion__codigo')
    )
    filas = []
    for r in resultados:
        eventos = {ev.paso: ev.timestamp for ev in r.eventos.all()}
        def ts(paso):
            t = eventos.get(paso)
            return t.strftime('%Y-%m-%d %H:%M:%S') if t else ''
        filas.append([
            r.estacion.farmacia.grupo.codigo,
            r.estacion.farmacia.codigo,
            r.estacion.codigo,
            r.get_estado_display(),
            r.version_previa or '',
            r.version_nueva or '',
            r.detalle_error or '',
            ts(EventoDespliegue.Paso.RECIBIDO),
            ts(EventoDespliegue.Paso.APLICADO),
            ts(EventoDespliegue.Paso.OK),
        ])
    _escribir(salida, [
        'grupo', 'farmacia', 'estacion', 'estado', 'version_previa', 'version_nueva',
        'detalle_error', 'recibido', 'aplicado', 'ok',
    ], filas)


def reporte_auditoria(salida, request, desde=None, hasta=None):
    """Bitácora de acciones sobre el panel en un rango de fechas, acotada a lo que
    `request.user` puede ver (mismo aislamiento por unidad de negocio que la lista)."""
    eventos = scope_opcional_por_unidad_negocio_activa(
        EventoAuditoria.objects.select_related('usuario', 'unidad_negocio'), request, 'unidad_negocio',
    ).order_by('timestamp')
    if desde:
        eventos = eventos.filter(timestamp__gte=desde)
    if hasta:
        eventos = eventos.filter(timestamp__lte=hasta)

    filas = [
        [
            ev.timestamp.strftime('%Y-%m-%d %H:%M:%S'),
            ev.usuario.username if ev.usuario else 'sistema',
            ev.accion,
            ev.objeto_repr,
            ev.unidad_negocio.codigo if ev.unidad_negocio else 'global',
            ev.ip_address or '',
        ]
        for ev in eventos
    ]
    _escribir(salida, ['fecha', 'usuario', 'accion', 'objeto', 'unidad_negocio', 'ip'], filas)


def reporte_activos(salida, unidad_negocio):
    """Inventario actual de una unidad de negocio: qué activos tiene, en qué estado."""
    activos = (
        Activo.objects
        .filter(unidad_negocio=unidad_negocio)
        .select_related('marca', 'categoria', 'bodega_actual', 'colaborador_actual')
        .order_by('codigo')
    )
    filas = [
        [
            a.codigo,
            a.get_tipo_display(),
            a.marca.nombre if a.marca else '',
            a.modelo or '',
            a.numero_serie or '',
            a.get_estado_display(),
            a.bodega_actual.codigo if a.bodega_actual else '',
            a.colaborador_actual.nombre if a.colaborador_actual else '',
            a.fecha_compra.strftime('%Y-%m-%d') if a.fecha_compra else '',
            a.vencimiento_garantia.strftime('%Y-%m-%d') if a.vencimiento_garantia else '',
        ]
        for a in activos
    ]
    _escribir(salida, [
        'codigo', 'tipo', 'marca', 'modelo', 'numero_serie', 'estado',
        'bodega', 'colaborador_actual', 'fecha_compra', 'vencimiento_garantia',
    ], filas)


def reporte_alertas(salida, unidad_negocio, desde=None, hasta=None):
    """Alertas de las estaciones de una unidad de negocio en un rango de fechas."""
    alertas = (
        Alerta.objects
        .filter(estacion__farmacia__unidad_negocio=unidad_negocio)
        .select_related('regla', 'estacion')
        .order_by('-abierta_en')
    )
    if desde:
        alertas = alertas.filter(abierta_en__gte=desde)
    if hasta:
        alertas = alertas.filter(abierta_en__lte=hasta)

    filas = [
        [
            a.regla.nombre,
            a.regla.get_severidad_display(),
            a.estacion.codigo,
            a.valor_disparador,
            a.get_estado_display(),
            a.abierta_en.strftime('%Y-%m-%d %H:%M:%S'),
            a.resuelta_en.strftime('%Y-%m-%d %H:%M:%S') if a.resuelta_en else '',
        ]
        for a in alertas
    ]
    _escribir(salida, [
        'regla', 'severidad', 'estacion', 'valor', 'estado', 'abierta_en', 'resuelta_en',
    ], filas)


def reporte_facturacion(salida, unidad_negocio, anio: int, mes: int):
    """Endpoints facturables de una unidad de negocio en un período (año-mes): toda
    estación que tuvo al menos un heartbeat ese mes calendario. Ver docstring de
    apps.facturacion.models.ActividadMensualEstacion sobre el criterio y por qué no se
    puede reconstruir para meses previos a que se activó este registro."""
    filas = [
        [
            e.farmacia.grupo.codigo,
            e.farmacia.codigo,
            e.codigo,
            e.hostname or '',
        ]
        for e in estaciones_facturables(unidad_negocio, anio, mes).select_related('farmacia', 'farmacia__grupo')
    ]
    _escribir(salida, ['grupo', 'farmacia', 'estacion', 'hostname'], filas)


def reporte_software_instalado(salida, unidad_negocio, nombre_filtro: str = ''):
    """Qué estaciones tienen instalado qué software, según el último escaneo de cada
    una (comando "consultar_software_instalado") — el valor real del feature: compliance
    de licenciamiento y detectar software no autorizado. `nombre_filtro` acota a
    programas cuyo nombre lo contiene (ej. "Chrome"), sin acotar por defecto."""
    from apps.software.models import SoftwareInstaladoDetectado

    detectados = (
        SoftwareInstaladoDetectado.objects
        .filter(estacion__farmacia__unidad_negocio=unidad_negocio)
        .select_related('estacion', 'estacion__farmacia', 'estacion__farmacia__grupo')
        .order_by('nombre', 'estacion__codigo')
    )
    if nombre_filtro:
        detectados = detectados.filter(nombre__icontains=nombre_filtro)

    filas = [
        [
            d.nombre, d.version, d.fabricante,
            d.estacion.farmacia.grupo.codigo, d.estacion.farmacia.codigo, d.estacion.codigo,
            d.detectado_en.strftime('%Y-%m-%d %H:%M:%S'),
        ]
        for d in detectados
    ]
    _escribir(salida, [
        'programa', 'version', 'fabricante', 'grupo', 'farmacia', 'estacion', 'detectado_en',
    ], filas)


def nombre_archivo(prefijo: str) -> str:
    return f'{prefijo}_{timezone.now():%Y%m%d_%H%M%S}.csv'
