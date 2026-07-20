"""Lógica de publicación de despliegues.

Se invoca desde el panel (admin action hoy, vista HTMX en Fase 2) cuando un
despliegue ya aprobado pasa a distribuirse. No corre en el worker MQTT de
larga duración: esto es una acción puntual disparada por un humano.
"""
import json
import logging

import paho.mqtt.publish as mqtt_publish
from django.conf import settings
from django.utils import timezone

from .models import Despliegue, EventoDespliegue, ResultadoDespliegue

logger = logging.getLogger(__name__)


def _topicos_para(despliegue: Despliegue) -> list[str]:
    if despliegue.destino_tipo == Despliegue.DestinoTipo.CADENA:
        return ['/saidsof/despliegue/global/']
    if despliegue.destino_tipo == Despliegue.DestinoTipo.GRUPOS:
        return [f'/saidsof/despliegue/grupo/{g.codigo}/' for g in despliegue.grupos.all()]
    if despliegue.destino_tipo == Despliegue.DestinoTipo.FARMACIAS:
        return [f'/saidsof/despliegue/farmacia/{f.codigo}/' for f in despliegue.farmacias.all()]
    # ESTACIONES: cada agente está suscrito a su propio tópico individual
    return [f'/saidsof/agente/{e.codigo}/despliegue/' for e in despliegue.estaciones.all()]


def _payload(despliegue: Despliegue) -> dict:
    return {
        'despliegue_id': despliegue.id,
        'version': despliegue.version,
        'url': settings.ARCHIVOS_BASE_URL + despliegue.archivo.url,
        'sha256': despliegue.sha256,
        'modo_aplicacion': despliegue.modo_aplicacion,
        'ventana_fecha_hora': (
            despliegue.ventana_fecha_hora.isoformat() if despliegue.ventana_fecha_hora else None
        ),
    }


def publicar_despliegue(despliegue: Despliegue) -> int:
    """Resuelve el destino, crea ResultadoDespliegue por estación y publica por MQTT.

    Devuelve la cantidad de estaciones destinatarias.
    """
    estaciones = list(despliegue.resolver_estaciones_destino())

    resultados = [
        ResultadoDespliegue(despliegue=despliegue, estacion=estacion, estado=ResultadoDespliegue.Estado.PENDIENTE)
        for estacion in estaciones
    ]
    ResultadoDespliegue.objects.bulk_create(resultados, ignore_conflicts=True)

    payload = json.dumps(_payload(despliegue))
    topicos = _topicos_para(despliegue)

    mqtt_conf = settings.MQTT_CONFIG
    auth = None
    if mqtt_conf['USERNAME']:
        auth = {'username': mqtt_conf['USERNAME'], 'password': mqtt_conf['PASSWORD']}

    for topico in topicos:
        try:
            mqtt_publish.single(
                topico,
                payload,
                hostname=mqtt_conf['HOST'],
                port=mqtt_conf['PORT'],
                auth=auth,
                client_id=mqtt_conf['CLIENT_ID_PANEL'],
                retain=True,  # equipos apagados lo reciben al encender
            )
        except Exception:
            logger.exception('No se pudo publicar el despliegue %s en %s', despliegue.id, topico)

    EventoDespliegue.objects.bulk_create([
        EventoDespliegue(resultado=r, paso=EventoDespliegue.Paso.PUBLICADO, detalle=f'Tópicos: {", ".join(topicos)}')
        for r in ResultadoDespliegue.objects.filter(despliegue=despliegue)
    ])

    despliegue.estado = Despliegue.Estado.PUBLICANDO
    despliegue.fecha_publicacion = timezone.now()
    despliegue.save(update_fields=['estado', 'fecha_publicacion'])

    return len(estaciones)


def evaluar_freno_automatico(despliegue: Despliegue) -> bool:
    """Si el % de estaciones en error supera el umbral configurado, pausa el despliegue.

    Devuelve True si se pausó.
    """
    if despliegue.freno_omitido:
        # El operador ya reanudó a pesar de los errores: no volver a frenar.
        return False
    total = despliegue.resultados.count()
    if not total:
        return False
    errores = despliegue.resultados.filter(estado=ResultadoDespliegue.Estado.ERROR).count()
    porcentaje = (errores / total) * 100
    if porcentaje >= float(despliegue.umbral_error_pct) and despliegue.estado == Despliegue.Estado.PUBLICANDO:
        despliegue.estado = Despliegue.Estado.PAUSADO
        despliegue.save(update_fields=['estado'])
        logger.warning(
            'Despliegue %s pausado automáticamente: %.1f%% de error (umbral %.1f%%)',
            despliegue.id, porcentaje, despliegue.umbral_error_pct,
        )
        return True
    return False


def verificar_completado(despliegue: Despliegue) -> bool:
    """Marca el despliegue como completado si ya no quedan estaciones pendientes/en curso."""
    en_curso = despliegue.resultados.exclude(
        estado__in=[ResultadoDespliegue.Estado.APLICADO, ResultadoDespliegue.Estado.ERROR,
                    ResultadoDespliegue.Estado.ROLLBACK],
    ).exists()
    if not en_curso and despliegue.estado == Despliegue.Estado.PUBLICANDO:
        despliegue.estado = Despliegue.Estado.COMPLETADO
        despliegue.save(update_fields=['estado'])
        return True
    return False
