"""Lógica de publicación de despliegues.

Se invoca desde la vista HTMX del panel (`apps/panel/views/despliegues.py::despliegue_publicar`,
también disponible como acción del admin) cuando un despliegue ya aprobado pasa a
distribuirse. No corre en el worker MQTT de larga duración: esto es una acción puntual
disparada por un humano.
"""
import json
import logging
from dataclasses import dataclass

import paho.mqtt.publish as mqtt_publish
from django.conf import settings
from django.utils import timezone

from .models import Despliegue, EventoDespliegue, ResultadoDespliegue

logger = logging.getLogger(__name__)


@dataclass
class ResultadoPublicacion:
    total_estaciones: int
    exitoso: bool


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
        # El agente intentará descargar del caché de su farmacia antes que del central
        # (best-effort: si el caché no tiene el paquete o falla, cae al central).
        'usar_cache': settings.DESPLIEGUE_USAR_CACHE,
    }


def publicar_despliegue(despliegue: Despliegue) -> ResultadoPublicacion:
    """Resuelve el destino, crea ResultadoDespliegue por estación y publica por MQTT.

    Si la publicación falla (ej. broker caído), el despliegue se queda en su estado
    actual — no avanza a PUBLICANDO ni se registra ningún EventoDespliegue "publicado".
    Antes esto se registraba igual aunque la publicación hubiera fallado por completo,
    dejando al operador viendo "publicado con éxito" sin que ninguna estación recibiera
    nada. El caller decide qué mostrar/reintentar según `ResultadoPublicacion.exitoso`.
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
    tls = None
    if mqtt_conf['USE_TLS']:
        # EMQX en producción solo expone el listener TLS (8883); sin esto, publish.multiple
        # se conecta en plano y el broker cierra la conexión.
        tls = {'ca_certs': mqtt_conf['CA_CERT'] or None}

    mensajes = [{'topic': topico, 'payload': payload, 'retain': True} for topico in topicos]

    try:
        # Una sola conexión para todos los tópicos del destino (antes: connect+disconnect
        # por tópico vía publish.single() en loop — con un destino de muchas estaciones
        # puntuales, eso bloqueaba la request HTTP con decenas de handshakes secuenciales).
        mqtt_publish.multiple(
            mensajes,
            hostname=mqtt_conf['HOST'],
            port=mqtt_conf['PORT'],
            auth=auth,
            tls=tls,
            client_id=mqtt_conf['CLIENT_ID_PANEL'],
        )
    except Exception:
        logger.exception(
            'No se pudo publicar el despliegue %s por MQTT (%d tópico(s) destino)', despliegue.id, len(topicos),
        )
        return ResultadoPublicacion(total_estaciones=len(estaciones), exitoso=False)

    EventoDespliegue.objects.bulk_create([
        EventoDespliegue(resultado=r, paso=EventoDespliegue.Paso.PUBLICADO, detalle=f'Tópicos: {", ".join(topicos)}')
        for r in ResultadoDespliegue.objects.filter(despliegue=despliegue)
    ])

    despliegue.estado = Despliegue.Estado.PUBLICANDO
    despliegue.fecha_publicacion = timezone.now()
    despliegue.save(update_fields=['estado', 'fecha_publicacion'])

    return ResultadoPublicacion(total_estaciones=len(estaciones), exitoso=True)


def evaluar_freno_automatico(despliegue: Despliegue) -> bool:
    """Si el % de estaciones en error supera el umbral configurado, pausa el despliegue.

    Devuelve True si se pausó.
    """
    if despliegue.freno_omitido:
        # El operador ya reanudó a pesar de los errores: no volver a frenar.
        return False
    # Solo cuentan las estaciones que ya salieron de PENDIENTE (recibieron el mensaje y
    # empezaron a reportar). Antes el denominador era TODOS los destinatarios: en una
    # publicación amplia (ej. toda la cadena sin pasar por anillos), miles de estaciones
    # que ni siquiera habían descargado el paquete todavía diluían el % de error de las
    # pocas que ya habían fallado, y el freno no llegaba a activarse a tiempo.
    ya_reportaron = despliegue.resultados.exclude(estado=ResultadoDespliegue.Estado.PENDIENTE)
    total = ya_reportaron.count()
    if not total:
        return False
    errores = ya_reportaron.filter(estado=ResultadoDespliegue.Estado.ERROR).count()
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
