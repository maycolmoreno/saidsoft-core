from celery import shared_task

from .services import marcar_estaciones_offline


@shared_task(name='apps.catalogo.tasks.marcar_estaciones_offline_task')
def marcar_estaciones_offline_task():
    """Cada minuto (ver CELERY_BEAT_SCHEDULE) — mismo umbral que el comando manual."""
    total = marcar_estaciones_offline(minutos=5)
    return f'{total} estación(es) marcada(s) como offline.'
