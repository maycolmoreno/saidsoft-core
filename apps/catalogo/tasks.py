from celery import shared_task

from .services import marcar_estaciones_offline


@shared_task(name='apps.catalogo.tasks.marcar_estaciones_offline_task')
def marcar_estaciones_offline_task():
    """Cada minuto (ver CELERY_BEAT_SCHEDULE) — mismo umbral que el comando manual.

    El umbral ya no viaja como literal acá: es
    `apps.monitoreo.umbrales.ALERTA_SIN_LATIDO_UMBRAL_MINUTOS`, el valor por defecto de
    `marcar_estaciones_offline`. Escrito a mano en este archivo era el tercer lugar donde
    vivía el concepto de "dejó de reportar", y los tres no coincidían (hallazgo D-01).
    """
    total = marcar_estaciones_offline()
    return f'{total} estación(es) marcada(s) como offline.'
