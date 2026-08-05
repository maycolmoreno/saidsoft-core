from celery import shared_task

from .models import SincronizacionExterna
from .services import ejecutar_sync


@shared_task(name='apps.integraciones.tasks.sincronizar_task', bind=True, max_retries=3, default_retry_delay=60)
def sincronizar_task(self, sincronizacion_id):
    """Tarea genérica de despacho: no conoce Odoo/AD/ESET, solo el registro de conectores
    (ver apps.integraciones.connectors). Un futuro conector solo necesita encolar esta
    tarea con el id de su SincronizacionExterna — no hace falta una tarea Celery por
    integración."""
    sincronizacion = SincronizacionExterna.objects.get(pk=sincronizacion_id)
    try:
        ejecutar_sync(sincronizacion)
    except Exception as exc:
        raise self.retry(exc=exc)
    return f'Sincronización #{sincronizacion_id} enviada a "{sincronizacion.conector}".'
