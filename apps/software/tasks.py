from celery import shared_task

from .services import generar_escaneos_vencidos


@shared_task(name='apps.software.tasks.generar_escaneos_programados_task')
def generar_escaneos_programados_task():
    """Diaria (ver CELERY_BEAT_SCHEDULE). Segura de repetir en el mismo día: el propio
    filtro por fecha_proxima_ejecucion avanza tras cada generación."""
    total = generar_escaneos_vencidos()
    return f'{total} escaneo(s) programado(s) disparado(s).'
