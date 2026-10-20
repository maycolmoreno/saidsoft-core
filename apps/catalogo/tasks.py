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


@shared_task(name='apps.catalogo.tasks.limpiar_actualizacion_pendiente_task')
def limpiar_actualizacion_pendiente_task(estacion_id, version_reportada):
    """Borra el retenido de `actualizar_agente` de una estación que ya reportó la versión.

    La encola `manejar_heartbeat`, pero NO la ejecuta el worker MQTT: publica
    `celery_worker`, que usa la credencial del panel. La del worker no tiene permiso de
    publicar en `/saidsof/agente/+/actualizar_agente/` y EMQX descartaba el borrado en
    silencio (docs/auditoria/fase_c1_laboratorio.md, L-5): por eso quedaban retenidas
    actualizaciones ya aplicadas. Se eligió esto en vez de darle ese permiso al worker.

    Se relee la estación al ejecutar: si la versión cambió desde el latido que la encoló,
    esta tarea quedó vieja y no se toca nada. El tópico se arma acá a partir de la
    estación; la tarea no recibe tópicos.
    """
    from .models import Estacion
    from . import services

    estacion = Estacion.objects.filter(pk=estacion_id).first()
    if estacion is None:
        return 'estación inexistente: nada que limpiar'
    if estacion.version_agente != version_reportada:
        return 'la versión cambió desde el latido: se omite'
    if not services.limpiar_actualizacion_pendiente(estacion):
        # `_publicar_mqtt` ya dejó el motivo en el log (incluido un rechazo de EMQX).
        return f'no se pudo limpiar el retenido de {estacion.codigo}'
    return f'retenido de actualización limpiado en {estacion.codigo}'
