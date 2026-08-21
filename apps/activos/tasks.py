from celery import shared_task

from .services import vincular_activos_por_numero_serie


@shared_task(name='apps.activos.tasks.vincular_activos_por_serie_task')
def vincular_activos_por_serie_task():
    """Diaria (ver CELERY_BEAT_SCHEDULE). Idempotente: solo mira estaciones que
    todavía no tienen un Activo vinculado."""
    total = vincular_activos_por_numero_serie()
    return f'{total} activo(s) vinculado(s) por número de serie.'
