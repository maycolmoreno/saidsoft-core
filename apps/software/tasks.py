from celery import shared_task

from .services import generar_escaneos_vencidos


@shared_task(name='apps.software.tasks.generar_escaneos_programados_task')
def generar_escaneos_programados_task():
    """Diaria (ver CELERY_BEAT_SCHEDULE). Segura de repetir en el mismo día: el propio
    filtro por fecha_proxima_ejecucion avanza tras cada generación."""
    total = generar_escaneos_vencidos()
    return f'{total} escaneo(s) programado(s) disparado(s).'


@shared_task(name='apps.software.tasks.retirar_retenido_instalacion_task')
def retirar_retenido_instalacion_task(estacion_id):
    """Borra la instalación retenida de una estación que ya recibió todas sus órdenes.

    La encola `apps.mqtt_worker.services.manejar_estado_instalacion` y la ejecuta
    `celery_worker` con la credencial del panel. Ver
    `apps.despliegues.tasks.retirar_retenido_despliegue_task`: mismo motivo y misma
    condición, evaluada al ejecutar.
    """
    from apps.catalogo.models import Estacion

    from .services import retenido_ya_entregado, retirar_retenido

    estacion = Estacion.objects.filter(pk=estacion_id).first()
    if estacion is None:
        return 'estación inexistente: nada que retirar'
    if not retenido_ya_entregado(estacion):
        return f'{estacion.codigo} tiene instalaciones sin confirmar: se conserva el retenido'
    if not retirar_retenido(estacion):
        return f'no se pudo retirar la instalación retenida de {estacion.codigo}'
    return f'instalación retenida retirada en {estacion.codigo}'
