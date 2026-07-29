"""Comandos puntuales del panel hacia el agente de una estación (ej. reiniciar, ejecutar_script)."""
import hashlib
import hmac
import json
import logging

import paho.mqtt.publish as mqtt_publish
from django.conf import settings

logger = logging.getLogger(__name__)


def _topico_comando(estacion) -> str:
    return f'/saidsof/agente/{estacion.codigo}/comando/'


def firmar_payload(**campos) -> str:
    """HMAC-SHA256 de los valores de `campos` unidos con "|", en el orden en que se pasan.

    No se firma el JSON serializado (evita divergencias de canonicalización
    entre `json.dumps` en Python y `System.Text.Json` en el agente C#): se
    firma un string simple de campos en un orden fijo, que el agente debe
    reconstruir exactamente igual para verificar. Los kwargs preservan orden
    de inserción en Python 3.7+, así que el orden de las llamadas a
    `firmar_payload(...)` en este módulo ES el contrato con el agente.
    """
    mensaje = '|'.join(str(v) for v in campos.values())
    return hmac.new(settings.COMANDO_HMAC_SECRET.encode(), mensaje.encode(), hashlib.sha256).hexdigest()


def _publicar_comando(estacion, payload: dict) -> bool:
    mqtt_conf = settings.MQTT_CONFIG
    auth = None
    if mqtt_conf['USERNAME']:
        auth = {'username': mqtt_conf['USERNAME'], 'password': mqtt_conf['PASSWORD']}
    tls = None
    if mqtt_conf['USE_TLS']:
        tls = {'ca_certs': mqtt_conf['CA_CERT'] or None}

    try:
        mqtt_publish.single(
            _topico_comando(estacion),
            json.dumps(payload),
            hostname=mqtt_conf['HOST'],
            port=mqtt_conf['PORT'],
            auth=auth,
            tls=tls,
            client_id=mqtt_conf['CLIENT_ID_PANEL'],
            retain=False,
        )
        return True
    except Exception:
        logger.exception('No se pudo enviar el comando "%s" a %s', payload.get('comando'), estacion.codigo)
        return False


def enviar_comando(estacion, comando: str) -> bool:
    """Publica un comando puntual (ej. "reiniciar") al agente de la estación.

    No usa retain: si la estación está desconectada en el momento de enviarlo,
    el comando se pierde (correcto para "reiniciar ahora" — no queremos que un
    reinicio pendiente se dispare solo al reconectar más tarde). Devuelve True
    si se pudo publicar (no confirma que el agente lo haya recibido/aplicado).
    """
    firma = firmar_payload(comando=comando)
    return _publicar_comando(estacion, {'comando': comando, 'firma': firma})


def enviar_script(estacion, *, ejecucion_id: int, resultado_id: int, tipo_script: str,
                   contenido: str, timeout_segundos: int) -> bool:
    """Publica una orden de "ejecutar_script" al agente de la estación.

    El agente responde el progreso (recibido/ejecutando/completado/error/timeout)
    en el tópico `script_estado`, manejado por `apps.mqtt_worker.services.manejar_estado_script`.
    """
    firma = firmar_payload(
        comando='ejecutar_script', ejecucion_id=ejecucion_id, resultado_id=resultado_id,
        tipo_script=tipo_script, timeout_segundos=timeout_segundos, contenido=contenido,
    )
    return _publicar_comando(estacion, {
        'comando': 'ejecutar_script', 'ejecucion_id': ejecucion_id, 'resultado_id': resultado_id,
        'tipo_script': tipo_script, 'contenido': contenido, 'timeout_segundos': timeout_segundos,
        'firma': firma,
    })


def resolver_estaciones(destino_tipo, *, grupos=None, farmacias=None, estaciones=None):
    """Resuelve el queryset de Estacion aprobada para un destino_tipo/grupos/farmacias/estaciones.

    Compartido por `Despliegue.resolver_estaciones_destino` y
    `EjecucionScript` (apps/scripts): ambos envían algo a un conjunto de
    estaciones definido de la misma forma (cadena completa / grupos /
    farmacias / estaciones puntuales), así que la resolución vive una sola vez aquí.
    """
    from apps.catalogo.models import Estacion

    aprobada = Estacion.EstadoAprobacion.APROBADA
    if destino_tipo == 'cadena':
        return Estacion.objects.filter(estado_aprobacion=aprobada)
    if destino_tipo == 'grupos':
        return Estacion.objects.filter(farmacia__grupo__in=grupos or [], estado_aprobacion=aprobada)
    if destino_tipo == 'farmacias':
        return Estacion.objects.filter(farmacia__in=farmacias or [], estado_aprobacion=aprobada)
    return (estaciones if estaciones is not None else Estacion.objects.none()).filter(
        estado_aprobacion=aprobada,
    )


# --- Acceso remoto interactivo (MeshCentral) ---
# A diferencia de enviar_comando/enviar_script (arriba), esto NO viaja por el
# broker MQTT propio: es un canal completamente aparte (navegador -> servidor
# MeshCentral -> MeshAgent). Estas funciones solo construyen strings (un
# comando PowerShell y URLs); no hacen ninguna llamada de red.

def generar_comando_instalacion_meshcentral(estacion) -> str:
    """One-liner de PowerShell para instalar el agente de MeshCentral en `estacion`.

    Pensado para pegarse tal cual en el ejecutor ad-hoc de Scripts RMM
    (apps/scripts). Descarga el instalador silencioso (installflags=2 =
    solo servicio, sin UI) del device group configurado en MESHCENTRAL_CONFIG.
    """
    conf = settings.MESHCENTRAL_CONFIG
    url_instalador = (
        f"{conf['SERVER_URL']}/meshagents?id={conf['AGENT_ARCH_ID']}"
        f"&meshid={conf['MESH_ID']}&installflags={conf['INSTALL_FLAGS']}"
    )
    return (
        '$ruta = Join-Path $env:TEMP "meshagent.exe"; '
        f'Invoke-WebRequest -Uri "{url_instalador}" -OutFile $ruta -UseBasicParsing; '
        'Start-Process -FilePath $ruta -Wait; '
        'Remove-Item $ruta -Force -ErrorAction SilentlyContinue'
    )


def _url_dispositivo_meshcentral(estacion, viewmode: str) -> str | None:
    if not estacion.meshcentral_node_id:
        return None
    conf = settings.MESHCENTRAL_CONFIG
    return f"{conf['SERVER_URL']}/index.html?node={estacion.meshcentral_node_id}&viewmode={viewmode}"


def url_escritorio_remoto_meshcentral(estacion) -> str | None:
    """URL de MeshCentral para ver el escritorio remoto de `estacion`, o None si
    todavía no tiene un node_id vinculado."""
    return _url_dispositivo_meshcentral(estacion, settings.MESHCENTRAL_CONFIG['VIEWMODE_ESCRITORIO'])


def url_terminal_remoto_meshcentral(estacion) -> str | None:
    """URL de MeshCentral para abrir una terminal remota en `estacion`, o None si
    todavía no tiene un node_id vinculado."""
    return _url_dispositivo_meshcentral(estacion, settings.MESHCENTRAL_CONFIG['VIEWMODE_TERMINAL'])
