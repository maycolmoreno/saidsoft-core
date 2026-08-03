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


def resolver_estaciones(destino_tipo, *, unidad_negocio, grupos=None, farmacias=None, estaciones=None):
    """Resuelve el queryset de Estacion aprobada para un destino_tipo/grupos/farmacias/estaciones,
    siempre acotado a `unidad_negocio` (el tenant del Despliegue/EjecucionScript que llama).

    Compartido por `Despliegue.resolver_estaciones_destino` y
    `EjecucionScript` (apps/scripts): ambos envían algo a un conjunto de
    estaciones definido de la misma forma (cadena completa / grupos /
    farmacias / estaciones puntuales), así que la resolución vive una sola vez aquí.

    `unidad_negocio` es obligatorio y se aplica en la base del queryset, no solo en la
    rama "farmacias"/"estaciones": un Grupo (canal de versión) puede estar compartido
    por farmacias de varias unidades de negocio (ver apps.cumplimiento), así que
    "grupos"/"cadena" NUNCA deben devolver estaciones de un tenant distinto al del
    despliegue/ejecución que las pidió, aunque el grupo en sí sea compartido.
    """
    from apps.catalogo.models import Estacion

    aprobada = Estacion.EstadoAprobacion.APROBADA
    base = Estacion.objects.filter(estado_aprobacion=aprobada, farmacia__unidad_negocio=unidad_negocio)

    if destino_tipo == 'cadena':
        return base
    if destino_tipo == 'grupos':
        return base.filter(farmacia__grupo__in=grupos or [])
    if destino_tipo == 'farmacias':
        return base.filter(farmacia__in=farmacias or [])
    ids_estaciones = estaciones if estaciones is not None else Estacion.objects.none()
    return base.filter(pk__in=ids_estaciones)


def validar_destino_unidad_negocio(unidad_negocio, *, farmacias=None, estaciones=None):
    """Rechaza farmacias/estaciones elegidas a mano que no pertenezcan a `unidad_negocio`.

    Pensado para llamarse desde `Form.clean()` (DespliegueForm, EjecutarScriptForm) con
    los querysets de `cleaned_data`, antes de guardar — ahí `farmacias`/`estaciones` son
    instancias reales, no hace falta ir a la base de datos de nuevo.

    No valida `grupos` a propósito: un Grupo puede ser compartido entre unidades de
    negocio (ver docstring de `resolver_estaciones`), así que un grupo "ajeno" no es un
    error de por sí — `resolver_estaciones` ya se encarga de que solo aporte las
    estaciones que sí son de esta unidad de negocio.
    """
    from django.core.exceptions import ValidationError

    ajenas = [f.codigo for f in (farmacias or []) if f.unidad_negocio_id != unidad_negocio.id]
    if ajenas:
        raise ValidationError(
            f'Estas farmacias no pertenecen a {unidad_negocio.codigo}: {", ".join(ajenas)}.'
        )
    ajenas_est = [e.codigo for e in (estaciones or []) if e.farmacia.unidad_negocio_id != unidad_negocio.id]
    if ajenas_est:
        raise ValidationError(
            f'Estas estaciones no pertenecen a {unidad_negocio.codigo}: {", ".join(ajenas_est)}.'
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


# --- Clave de recuperación de BitLocker ---
# Igual que MeshCentral: función puntual, no un canal nuevo. La clave llega cifrada
# desde el agente (ver apps.mqtt_worker.services.manejar_info_equipo) y solo se
# descifra aquí, bajo demanda, para la vista que la muestra (permiso propio + auditada).

def obtener_clave_bitlocker_descifrada(estacion) -> str | None:
    """Clave de recuperación de `estacion` en texto plano, o None si no hay ninguna
    registrada (nunca se reportó, o el equipo no usa BitLocker)."""
    from apps.catalogo import crypto
    from apps.catalogo.models import ClaveRecuperacionBitLocker

    try:
        clave = estacion.clave_bitlocker
    except ClaveRecuperacionBitLocker.DoesNotExist:
        return None
    return crypto.descifrar(clave.clave_cifrada)


def url_grabaciones_meshcentral(estacion) -> str | None:
    """URL de MeshCentral para revisar las grabaciones de sesión de `estacion`, o None
    si todavía no tiene un node_id vinculado.

    A diferencia de escritorio/terminal (que sí tienen un `viewmode` fijo y verificado
    con un servidor de prueba), la lista de grabaciones no tiene un deep-link estable
    por dispositivo — abre la ficha general del equipo y desde ahí hay que entrar a la
    pestaña "Recordings" a mano. Requiere además que la grabación esté habilitada para
    el device group en el `config.json` del servidor MeshCentral (ver
    deploy/README-produccion.md) — sin eso, aunque el link abra, no habrá nada grabado.
    """
    if not estacion.meshcentral_node_id:
        return None
    conf = settings.MESHCENTRAL_CONFIG
    return f"{conf['SERVER_URL']}/index.html?node={estacion.meshcentral_node_id}"
