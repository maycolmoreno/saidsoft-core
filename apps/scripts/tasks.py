from celery import shared_task

from .services import generar_ejecuciones_vencidas


@shared_task(name='apps.scripts.tasks.generar_ejecuciones_programadas_task')
def generar_ejecuciones_programadas_task():
    """Diaria (ver CELERY_BEAT_SCHEDULE). Segura de repetir en el mismo día: el propio
    filtro por fecha_proxima_ejecucion avanza tras cada generación."""
    total = generar_ejecuciones_vencidas()
    return f'{total} ejecución(es) programada(s) generada(s).'
