from celery import shared_task


@shared_task(name='apps.despliegues.tasks.retirar_retenido_despliegue_task')
def retirar_retenido_despliegue_task(estacion_id):
    """Borra el despliegue retenido de una estación que ya recibió todas sus órdenes.

    La encola `apps.mqtt_worker.services.manejar_estado_despliegue`, pero la ejecuta
    `celery_worker`, con la credencial del panel: es la que puede publicar en
    `/saidsof/agente/+/despliegue/`. Mismo esquema que
    `apps.catalogo.tasks.limpiar_actualizacion_pendiente_task`.

    Sin esta limpieza el despliegue quedaba retenido para siempre: con el retainer en RAM
    lo borraba el próximo reinicio del broker, pero con el retainer en disco se le
    volvería a entregar a la estación tras reinstalar el agente, que es cuando pierde el
    registro de lo que ya aplicó.

    La condición se evalúa al ejecutar, no al encolar: si entre el reporte y la tarea se
    publicó otro despliegue a la misma estación, su resultado está PENDIENTE y no se toca
    nada.
    """
    from apps.catalogo.models import Estacion

    from .services import retenido_ya_entregado, retirar_retenido

    estacion = Estacion.objects.filter(pk=estacion_id).first()
    if estacion is None:
        return 'estación inexistente: nada que retirar'
    if not retenido_ya_entregado(estacion):
        return f'{estacion.codigo} tiene despliegues sin confirmar: se conserva el retenido'
    if not retirar_retenido(estacion):
        # `_publicar_mqtt` ya dejó el motivo en el log (incluido un rechazo de EMQX).
        return f'no se pudo retirar el despliegue retenido de {estacion.codigo}'
    return f'despliegue retenido retirado en {estacion.codigo}'
