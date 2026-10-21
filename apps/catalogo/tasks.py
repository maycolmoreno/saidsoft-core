import logging

from celery import shared_task

from .services import marcar_estaciones_offline

logger = logging.getLogger(__name__)


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


@shared_task(name='apps.catalogo.tasks.despachar_ordenes_task')
def despachar_ordenes_task(estacion_id=None):
    """Publica la siguiente orden de las ranuras libres de una estación (o de todas). La
    encola el worker MQTT cuando una orden deja de estar entregada; corre acá por la
    credencial del panel. Ver apps.catalogo.ordenes.despachar."""
    from . import ordenes

    total = ordenes.despachar(estacion_ids=None if estacion_id is None else [estacion_id])
    return f'{total} orden(es) despachada(s)'


@shared_task(name='apps.catalogo.tasks.procesar_ordenes_task')
def procesar_ordenes_task():
    """Cada minuto (CELERY_BEAT_SCHEDULE): caduca lo vencido, recupera lo publicado sin
    confirmar, reentrega lo que una estación viva no acusó, marca lo interrumpido y despacha
    lo pendiente. Una sola pasada a la vez en todo el sistema (advisory lock de PostgreSQL,
    ver `ordenes.procesar`): si la anterior todavía corre, esta no hace nada.

    Un error transitorio de la base (caída, failover, conexión cortada) no se reintenta acá:
    se cierra la conexión —lo que además libera el cerrojo— y la pasada siguiente, un minuto
    después, retoma desde la base. Lo que quedó a medias está en `publicando` y se recupera."""
    from django.db import InterfaceError, OperationalError, connection

    from . import ordenes

    try:
        return ordenes.procesar()
    except (OperationalError, InterfaceError):
        logger.warning('Error transitorio de la base en la cola de órdenes: se retoma en la próxima pasada',
                       exc_info=True)
        connection.close()
        return {'error_transitorio': True}
