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
from apps.despliegues.models import Despliegue, EventoDespliegue, ResultadoDespliegue


def _escribir(salida, encabezados, filas):
    writer = csv.writer(salida)
    writer.writerow(encabezados)
    writer.writerows(filas)


def reporte_cumplimiento(salida, grupo_codigo=None):
    """Qué versión corre cada estación vs. la objetivo de su grupo."""
    estaciones = (
        Estacion.objects
        .filter(estado_aprobacion=Estacion.EstadoAprobacion.APROBADA)
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


def reporte_auditoria(salida, desde=None, hasta=None):
    """Bitácora de acciones sobre el panel en un rango de fechas."""
    eventos = EventoAuditoria.objects.select_related('usuario').order_by('timestamp')
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
            ev.ip_address or '',
        ]
        for ev in eventos
    ]
    _escribir(salida, ['fecha', 'usuario', 'accion', 'objeto', 'ip'], filas)


def nombre_archivo(prefijo: str) -> str:
    return f'{prefijo}_{timezone.now():%Y%m%d_%H%M%S}.csv'
