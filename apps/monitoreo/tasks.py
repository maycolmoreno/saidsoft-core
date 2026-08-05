from celery import shared_task

from .services import purgar_metricas_antiguas


@shared_task(name='apps.monitoreo.tasks.purgar_metricas_task')
def purgar_metricas_task():
    """Diaria (ver CELERY_BEAT_SCHEDULE). Sin efecto real en producción con TimescaleDB
    (ahí la retención la maneja una política nativa) — respaldo y para SQLite en dev."""
    borradas = purgar_metricas_antiguas(dias=30)
    return f'{borradas} muestra(s) de métricas eliminada(s).'
