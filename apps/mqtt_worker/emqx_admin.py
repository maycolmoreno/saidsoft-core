"""Aprovisiona credenciales MQTT por estación en EMQX (aislamiento a nivel de broker).

Hasta ahora las ~1.800 estaciones comparten una sola credencial MQTT
(`MQTT_USERNAME_AGENTE`/`MQTT_PASSWORD_AGENTE`, sembrada por `deploy/bootstrap-emqx.sh`)
con ACL `/saidsof/#` allow-all: el aislamiento de `apps.cuentas.services` es solo a nivel
de aplicación/BD, no del broker. Este módulo le da a cada estación su propia credencial,
con ACL restringida a sus propios tópicos, aprovechando que `Estacion.codigo` ya es único
a nivel global (ver `apps.catalogo.models.Estacion.codigo`).

Usa la API HTTP de EMQX 5.x autenticada con una API Key (Basic auth, no expira) en vez del
login+Bearer que usa `bootstrap-emqx.sh` — ese es apropiado para un script one-shot; acá se
llama en caliente, en cada enrolamiento, y no vale la pena gestionar refresco de token.

`aprovisionar_credencial_estacion` nunca lanza: si `EMQX_ADMIN_CONFIG` no está configurado
o la llamada falla, loguea y devuelve `None` — el enrolamiento sigue funcionando con la
credencial compartida existente, exactamente como antes de este módulo. Esto es lo que
permite desplegar este cambio sin que `deploy/.env` de producción tenga que estar listo de
entrada.
"""
import base64
import json
import logging
import secrets
import urllib.error
import urllib.request

from django.conf import settings

logger = logging.getLogger(__name__)


def _config():
    cfg = getattr(settings, 'EMQX_ADMIN_CONFIG', {})
    url = cfg.get('URL', '')
    api_key = cfg.get('API_KEY', '')
    api_secret = cfg.get('API_SECRET', '')
    if not (url and api_key and api_secret):
        return None
    return url.rstrip('/'), api_key, api_secret


def _peticion(metodo, url_base, api_key, api_secret, ruta, cuerpo=None):
    data = json.dumps(cuerpo).encode('utf-8') if cuerpo is not None else None
    credenciales = base64.b64encode(f'{api_key}:{api_secret}'.encode('utf-8')).decode('ascii')
    req = urllib.request.Request(f'{url_base}{ruta}', data=data, method=metodo, headers={
        'Authorization': f'Basic {credenciales}',
        'Content-Type': 'application/json',
    })
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status
    except urllib.error.HTTPError as e:
        return e.code


def _crear_o_rotar_usuario(url_base, api_key, api_secret, username, password):
    status = _peticion(
        'POST', url_base, api_key, api_secret,
        '/authentication/password_based:built_in_database/users',
        {'user_id': username, 'password': password},
    )
    if status in (200, 201):
        return True
    if status == 409:
        # Ya existe (re-enrolamiento): el POST de creación no actualiza nada, hay que
        # rotar con PUT — mismo patrón que crear_usuario() en bootstrap-emqx.sh.
        status = _peticion(
            'PUT', url_base, api_key, api_secret,
            f'/authentication/password_based:built_in_database/users/{username}',
            {'user_id': username, 'password': password},
        )
        return status in (200, 201, 204)
    logger.warning('EMQX: HTTP %s creando usuario MQTT %s', status, username)
    return False


def _definir_acl(url_base, api_key, api_secret, username, reglas):
    status = _peticion(
        'PUT', url_base, api_key, api_secret,
        f'/authorization/sources/built_in_database/rules/users/{username}',
        {'username': username, 'rules': reglas},
    )
    if status in (200, 201, 204):
        return True
    logger.warning('EMQX: HTTP %s definiendo ACL de %s', status, username)
    return False


def _reglas_para(estacion):
    codigo = estacion.codigo
    reglas = [
        {'topic': f'/saidsof/agente/{codigo}/#', 'permission': 'allow', 'action': 'all'},
        {'topic': f'/saidsof/enrolamiento/respuesta/{codigo}/', 'permission': 'allow', 'action': 'subscribe'},
        # Publicar su propio enrolamiento. Faltaba, y eso dejaba a una estacion con
        # credencial propia sin poder RE-enrolarse: el broker deniega el publish en
        # silencio y el agente se queda esperando una respuesta que nunca pidio.
        #
        # Importa porque el re-enrolamiento es el unico camino para que una estacion
        # actualizada consiga su secreto HMAC propio (ver agente_prueba._on_connect y
        # apps.catalogo.services.secreto_de). Sin esta regla, la auto-reparacion solo
        # funcionaba en estaciones que todavia usaban la credencial compartida — es decir,
        # justo en las que no la necesitaban.
        {'topic': '/saidsof/enrolamiento/solicitar/', 'permission': 'allow', 'action': 'publish'},
        {'topic': '/saidsof/despliegue/global/', 'permission': 'allow', 'action': 'subscribe'},
        {'topic': '/saidsof/software/global/', 'permission': 'allow', 'action': 'subscribe'},
        # Catalogo de que servicios del POS chequear (ver apps.monitoreo.servicios_pos).
        # Global como los dos de arriba porque el catalogo es uno solo para toda la flota.
        # Sin esta regla el agente se suscribe, EMQX deniega EN SILENCIO y el catalogo no
        # llega nunca -- no hay error en ningun log, simplemente las estaciones siguen
        # chequeando la lista vieja para siempre.
        {'topic': '/saidsof/catalogo/servicios_pos/', 'permission': 'allow', 'action': 'subscribe'},
        # Catalogo de eventos de Windows a vigilar. Misma advertencia que el de arriba:
        # sin la regla el agente se suscribe y EMQX lo deniega sin decir nada.
        {'topic': '/saidsof/catalogo/eventos_sistema/', 'permission': 'allow', 'action': 'subscribe'},
    ]
    farmacia = estacion.farmacia
    reglas.append({
        'topic': f'/saidsof/despliegue/farmacia/{farmacia.codigo}/', 'permission': 'allow', 'action': 'subscribe',
    })
    reglas.append({
        'topic': f'/saidsof/software/farmacia/{farmacia.codigo}/', 'permission': 'allow', 'action': 'subscribe',
    })
    reglas.append({
        'topic': f'/saidsof/despliegue/grupo/{farmacia.grupo.codigo}/', 'permission': 'allow', 'action': 'subscribe',
    })
    reglas.append({
        'topic': f'/saidsof/software/grupo/{farmacia.grupo.codigo}/', 'permission': 'allow', 'action': 'subscribe',
    })
    return reglas


def reaplicar_acl_estacion(estacion) -> bool:
    """Reescribe la ACL de `estacion` en EMQX SIN tocar su contrasena.

    Existe porque `aprovisionar_credencial_estacion` rota la credencial, y rotarla para
    corregir una regla dejaria a la estacion sin poder conectarse hasta que se re-enrole
    — que es exactamente lo que no puede hacer si la regla que falta es la del
    enrolamiento. Este camino solo actualiza permisos.
    """
    config = getattr(settings, 'EMQX_ADMIN_CONFIG', None) or {}
    url_base, api_key, api_secret = config.get('URL'), config.get('API_KEY'), config.get('API_SECRET')
    if not (url_base and api_key and api_secret):
        return False
    try:
        return _definir_acl(url_base, api_key, api_secret, estacion.codigo, _reglas_para(estacion))
    except Exception:
        logger.exception('EMQX: error reaplicando la ACL de %s', estacion.codigo)
        return False


def aprovisionar_credencial_estacion(estacion) -> tuple[str, str] | None:
    """Crea/rota en EMQX una credencial MQTT propia de `estacion`, con ACL restringida
    a sus propios tópicos. Devuelve (username, password) en texto plano — no se persiste
    en la BD de Django, se entrega una única vez en la respuesta de enrolamiento; si se
    pierde, un re-enrolamiento la vuelve a emitir (rotándola)."""
    config = _config()
    if config is None:
        return None
    url_base, api_key, api_secret = config

    username = estacion.codigo
    password = secrets.token_urlsafe(32)

    try:
        if not _crear_o_rotar_usuario(url_base, api_key, api_secret, username, password):
            return None
        if not _definir_acl(url_base, api_key, api_secret, username, _reglas_para(estacion)):
            return None
    except Exception:
        logger.exception('EMQX: error aprovisionando credencial MQTT para %s', username)
        return None

    return username, password
