"""Lógica de publicación de solicitudes de instalación de software.

Mismo patrón que apps.despliegues.services.publicar_despliegue — incluyendo las dos
correcciones que le aplicamos ahí: una sola conexión MQTT (publish.multiple, no
publish.single en loop) y no registrar el evento "publicado" ni avanzar el estado si
la publicación falla por completo (antes eso generaba auditoría falsa en despliegues).
"""
import json
import logging
import time
from dataclasses import dataclass
from datetime import timedelta

import paho.mqtt.publish as mqtt_publish
from django.conf import settings
from django.utils import timezone

from apps.catalogo.services import firmar_payload

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
    # SEC-1 (auditoría 22-ago-2026): mismo hueco y mismo fix que apps.despliegues.services
    # ._payload — este mensaje también le dice al agente qué instalador correr, y no
    # llevaba ninguna firma. Ver ese docstring para el razonamiento completo.
    va = solicitud.version_aplicacion
    timestamp = int(time.time())
    campos = {
        'solicitud_id': solicitud.id,
        'aplicacion': va.aplicacion.nombre,
        'version': va.version,
        'accion': solicitud.accion,
        # rstrip('/') por el mismo motivo que en apps/despliegues/services.py: con
        # barra final quedaba '//media/...' y el agente recibía un 404.
        'url': settings.ARCHIVOS_BASE_URL.rstrip('/') + va.instalador.url,
        'sha256': va.sha256,
        'comando_instalacion_silenciosa': va.comando_instalacion_silenciosa,
        'comando_desinstalacion': va.comando_desinstalacion,
        'argumentos_adicionales': va.argumentos_adicionales,
        'comando_deteccion': va.aplicacion.comando_deteccion,
    }
    firma = firmar_payload(comando='instalar_software', **campos, timestamp=timestamp)
    return {
        **campos,
        'timestamp': timestamp,
        # Mismo mecanismo de caché por farmacia que ya usan los despliegues: clave para
        # no saturar el enlace de datos con instaladores pesados hacia 600 farmacias.
        'usar_cache': settings.DESPLIEGUE_USAR_CACHE,
        'firma': firma,
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


def generar_escaneo_programado(*, programado) -> int:
    """Dispara "consultar_software_instalado" a cada estación resuelta de un
    InventarioProgramado vencido, y avanza sus fechas. Mismo patrón que
    apps.scripts.services.generar_ejecucion_programada, pero sin crear un registro de
    "ejecución" intermedio: no hay Script de por medio, solo el comando fijo — el
    resultado de cada escaneo llega solo (o no) por el canal ya existente
    (manejar_software_instalado) cuando cada agente responda.

    Devuelve la cantidad de estaciones a las que se les envió el comando (no confirma
    que lo hayan recibido — igual que enviar_comando en general).
    """
    from apps.catalogo.services import enviar_comando, resolver_estaciones

    estaciones = resolver_estaciones(
        programado.destino_tipo, unidad_negocio=programado.unidad_negocio,
        grupos=programado.grupos.all(), farmacias=programado.farmacias.all(),
        estaciones=programado.estaciones.all(),
    )
    enviados = 0
    for estacion in estaciones:
        if enviar_comando(estacion, 'consultar_software_instalado'):
            enviados += 1

    hoy = timezone.now().date()
    programado.fecha_ultima_ejecucion = hoy
    programado.fecha_proxima_ejecucion = hoy + timedelta(days=programado.frecuencia_dias)
    programado.save(update_fields=['fecha_ultima_ejecucion', 'fecha_proxima_ejecucion'])
    return enviados


def generar_escaneos_vencidos() -> int:
    """Recorre los InventarioProgramado vencidos (fecha_proxima_ejecucion <= hoy) y
    dispara el escaneo de cada uno. La llaman tanto el comando manual
    (`generar_escaneos_programados`) como la tarea periódica de Celery."""
    from django.db import transaction

    from apps.auditoria.models import registrar_evento

    from .models import InventarioProgramado

    with transaction.atomic():
        hoy = timezone.now().date()
        vencidos = InventarioProgramado.objects.filter(activo=True, fecha_proxima_ejecucion__lte=hoy)
        total = 0
        for programado in vencidos:
            enviados = generar_escaneo_programado(programado=programado)
            registrar_evento(
                usuario=programado.creado_por, accion='inventario_programado.disparar', objeto=programado,
                detalle={'estaciones_notificadas': enviados},
            )
            total += 1
    return total


def estaciones_desactualizadas(aplicacion):
    """QuerySet de SoftwareInstaladoDetectado donde el inventario (R7) detectó
    `aplicacion` instalada con una versión que no coincide con
    `aplicacion.version_mas_reciente_conocida`. Vacío si esa aplicación no tiene
    versión cargada (no se vigila).

    Match por nombre con `icontains` (no exacto): el nombre real del programa en el
    registro de Windows no siempre coincide letra por letra con el nombre del
    catálogo (ej. "Google Chrome" vs "Google Chrome (64-bit)") — limitación de v1,
    aceptada explícitamente, mismo criterio que la deduplicación por mensaje exacto
    de PosErrorDetectado.

    No es comparación semántica de versiones (mayor/menor): solo "no coincide con la
    última conocida" — comparar versiones de forma genérica y confiable (¿"9.5.1" es
    mayor o menor que "9.10"?) es un problema mayor, fuera de alcance de v1."""
    from .models import SoftwareInstaladoDetectado

    if not aplicacion.version_mas_reciente_conocida:
        return SoftwareInstaladoDetectado.objects.none()
    return SoftwareInstaladoDetectado.objects.filter(
        nombre__icontains=aplicacion.nombre,
    ).exclude(version=aplicacion.version_mas_reciente_conocida)
