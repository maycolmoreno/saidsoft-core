"""Lógica de publicación de solicitudes de instalación de software.

Mismo patrón que apps.despliegues.services.publicar_despliegue — incluyendo las dos
correcciones que le aplicamos ahí: una sola conexión MQTT (publish.multiple, no
publish.single en loop) y no registrar el evento "publicado" ni avanzar el estado si
la publicación falla por completo (antes eso generaba auditoría falsa en despliegues).
"""
import json
import logging
from dataclasses import dataclass

import paho.mqtt.publish as mqtt_publish
from django.conf import settings
from django.utils import timezone

from .models import DestinoTipo, EstadoSolicitud, EventoInstalacion, ResultadoInstalacion, SolicitudInstalacion

logger = logging.getLogger(__name__)


@dataclass
class ResultadoPublicacion:
    total_estaciones: int
    exitoso: bool


def _topicos_para(solicitud: SolicitudInstalacion) -> list[str]:
    if solicitud.destino_tipo == DestinoTipo.CADENA:
        return ['/saidsof/software/global/']
    if solicitud.destino_tipo == DestinoTipo.GRUPOS:
        return [f'/saidsof/software/grupo/{g.codigo}/' for g in solicitud.grupos.all()]
    if solicitud.destino_tipo == DestinoTipo.FARMACIAS:
        return [f'/saidsof/software/farmacia/{f.codigo}/' for f in solicitud.farmacias.all()]
    # ESTACIONES: cada agente está suscrito a su propio tópico individual
    return [f'/saidsof/agente/{e.codigo}/software/' for e in solicitud.estaciones.all()]


def _payload(solicitud: SolicitudInstalacion) -> dict:
    va = solicitud.version_aplicacion
    return {
        'solicitud_id': solicitud.id,
        'aplicacion': va.aplicacion.nombre,
        'version': va.version,
        'accion': solicitud.accion,
        'url': settings.ARCHIVOS_BASE_URL + va.instalador.url,
        'sha256': va.sha256,
        'comando_instalacion_silenciosa': va.comando_instalacion_silenciosa,
        'comando_desinstalacion': va.comando_desinstalacion,
        'argumentos_adicionales': va.argumentos_adicionales,
        'comando_deteccion': va.aplicacion.comando_deteccion,
        # Mismo mecanismo de caché por farmacia que ya usan los despliegues: clave para
        # no saturar el enlace de datos con instaladores pesados hacia 600 farmacias.
        'usar_cache': settings.DESPLIEGUE_USAR_CACHE,
    }


def publicar_solicitud(solicitud: SolicitudInstalacion) -> ResultadoPublicacion:
    """Resuelve el destino, crea ResultadoInstalacion por estación y publica por MQTT.

    Si la publicación falla (ej. broker caído), la solicitud se queda en su estado
    actual — no avanza a PUBLICANDO ni se registra ningún EventoInstalacion "publicado".
    """
    estaciones = list(solicitud.resolver_estaciones_destino())

    resultados = [
        ResultadoInstalacion(solicitud=solicitud, estacion=estacion, estado=ResultadoInstalacion.Estado.PENDIENTE)
        for estacion in estaciones
    ]
    ResultadoInstalacion.objects.bulk_create(resultados, ignore_conflicts=True)

    payload = json.dumps(_payload(solicitud))
    topicos = _topicos_para(solicitud)

    mqtt_conf = settings.MQTT_CONFIG
    auth = None
    if mqtt_conf['USERNAME']:
        auth = {'username': mqtt_conf['USERNAME'], 'password': mqtt_conf['PASSWORD']}
    tls = None
    if mqtt_conf['USE_TLS']:
        tls = {'ca_certs': mqtt_conf['CA_CERT'] or None}

    mensajes = [{'topic': topico, 'payload': payload, 'retain': True} for topico in topicos]

    try:
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
            'No se pudo publicar la solicitud de instalación %s por MQTT (%d tópico(s) destino)',
            solicitud.id, len(topicos),
        )
        return ResultadoPublicacion(total_estaciones=len(estaciones), exitoso=False)

    EventoInstalacion.objects.bulk_create([
        EventoInstalacion(
            resultado=r, paso=EventoInstalacion.Paso.PUBLICADO, detalle=f'Tópicos: {", ".join(topicos)}',
        )
        for r in ResultadoInstalacion.objects.filter(solicitud=solicitud)
    ])

    solicitud.estado = EstadoSolicitud.PUBLICANDO
    solicitud.fecha_publicacion = timezone.now()
    solicitud.save(update_fields=['estado', 'fecha_publicacion'])

    return ResultadoPublicacion(total_estaciones=len(estaciones), exitoso=True)


def verificar_completado(solicitud: SolicitudInstalacion) -> bool:
    """Marca la solicitud como completada si ya no quedan estaciones pendientes/en curso."""
    en_curso = solicitud.resultados.exclude(
        estado__in=[ResultadoInstalacion.Estado.INSTALADO, ResultadoInstalacion.Estado.ERROR],
    ).exists()
    if not en_curso and solicitud.estado == EstadoSolicitud.PUBLICANDO:
        solicitud.estado = EstadoSolicitud.COMPLETADO
        solicitud.save(update_fields=['estado'])
        return True
    return False
