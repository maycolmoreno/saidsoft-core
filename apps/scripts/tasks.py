from celery import shared_task

from .services import caducar_resultados_vencidos, generar_ejecuciones_vencidas


@shared_task(name='apps.scripts.tasks.generar_ejecuciones_programadas_task')
def generar_ejecuciones_programadas_task():
    """Diaria (ver CELERY_BEAT_SCHEDULE). Segura de repetir en el mismo día: el propio
    filtro por fecha_proxima_ejecucion avanza tras cada generación."""
    total = generar_ejecuciones_vencidas()
    return f'{total} ejecución(es) programada(s) generada(s).'


@shared_task(name='apps.scripts.tasks.caducar_resultados_vencidos_task')
def caducar_resultados_vencidos_task():
    """Cierra los resultados que pasaron su plazo sin que la estación contestara.

    Corre seguido y no una vez al día: una ejecución que quedó colgada es alguien
    esperando una respuesta, y descubrirlo mañana no sirve. Es barata — recorre solo los
    resultados abiertos, que en operación normal son pocos.
    """
    total = caducar_resultados_vencidos()
    return f'{total} resultado(s) cerrado(s) por vencimiento.'
