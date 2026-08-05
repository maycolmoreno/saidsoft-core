"""Generadores de reportes exportables (CSV).

CSV en vez de PDF: es lo que se abre en Excel y se analiza, sin dependencias extra.
Cada función escribe en un objeto tipo-archivo (el HttpResponse) usando el módulo csv
estándar. Los reportes cubren los tres ejes de auditoría acordados: cumplimiento de
versión, resultado de un despliegue, y bitácora de acciones.
"""
import csv

from django.utils import timezone

from apps.auditoria.models import EventoAuditoria
from apps.catalogo.models import Estacion
from apps.cuentas.services import scope_opcional_por_unidad_negocio_activa, unidades_negocio_en_foco
from apps.despliegues.models import Despliegue, EventoDespliegue


def _escribir(salida, encabezados, filas):
    writer = csv.writer(salida)
    writer.writerow(encabezados)
    writer.writerows(filas)


def reporte_cumplimiento(salida, request, grupo_codigo=None):
    """Qué versión corre cada estación vs. la objetivo de su grupo.

    Un Grupo (canal TRX) puede estar compartido por farmacias de varias unidades de
    negocio (ver apps.catalogo.services.validar_destino_unidad_negocio), así que no
    basta con filtrar por grupo: hay que acotar además a las estaciones cuya farmacia
    es visible para `request.user`, igual que el dashboard.
    """
    estaciones = (
        Estacion.objects
        .filter(
            estado_aprobacion=Estacion.EstadoAprobacion.APROBADA,
            farmacia__unidad_negocio__in=unidades_negocio_en_foco(request),
        )
        .select_related('farmacia', 'farmacia__grupo')
        .order_by('farmacia__grupo__codigo', 'farmacia__codigo', 'codigo')
    )
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


def nombre_archivo(prefijo: str) -> str:
    return f'{prefijo}_{timezone.now():%Y%m%d_%H%M%S}.csv'
