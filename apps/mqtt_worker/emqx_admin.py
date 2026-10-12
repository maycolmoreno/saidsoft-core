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
        # El cuerpo de la respuesta de error se logueaba en ningun lado y por eso el 400
        # `unknown_fields` del PUT de rotacion vivio semanas sin que nadie lo viera: el
        # llamador solo recibia "400" y el mensaje de EMQX, que nombra el campo sobrante,
        # se descartaba acá. bootstrap-emqx.sh sí imprime el cuerpo; este módulo no.
        detalle = ''
        try:
            detalle = e.read().decode('utf-8', 'replace')[:500]
        except Exception:
            pass
        if detalle:
            logger.warning('EMQX: HTTP %s en %s %s: %s', e.code, metodo, ruta, detalle)
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
        #
        # El cuerpo del PUT es DISTINTO al del POST: EMQX 5.8.3 rechaza "user_id" acá
        # (400 unknown_fields — el id ya va en la URL) y exige "is_superuser" explícito.
        # deploy/bootstrap-emqx.sh arregló esto el 20-ago-2026 y este módulo se quedó con
        # el body viejo, así que TODA rotación fallaba: `aprovisionar_credencial_estacion`
        # devolvía None para cualquier estación ya existente — es decir, justo en el
        # re-enrolamiento, que es el único caso en que este PUT corre.
        status = _peticion(
            'PUT', url_base, api_key, api_secret,
            f'/authentication/password_based:built_in_database/users/{username}',
            {'password': password, 'is_superuser': False},
        )
        if status not in (200, 201, 204):
            logger.warning('EMQX: HTTP %s rotando la contraseña de %s', status, username)
            return False
        return True
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


def consultar_credencial_estacion(estacion) -> dict | None:
    """¿Existe en EMQX la credencial propia de `estacion`? `{'usuario': bool, 'acl': bool}`.

    Solo lectura: dos `GET`, ninguna escritura. Devuelve `None` si `EMQX_ADMIN_CONFIG` no está
    configurado (igual convenio que el alta y la baja) y `{'usuario': None, ...}` para un
    estado que EMQX no dejó claro —ni 200 ni 404—, que no es lo mismo que "no existe".

    Existe para poder **confirmar lo que `deploy/emqx-narrow-acl-agente.sh` pide confirmar**
    antes de correrse: que ninguna estación dependa todavía de la credencial compartida. Ese
    script solo puede mirar las conexiones activas del broker y lo dice él mismo — *"una
    estación offline no aparece aquí aunque siga configurada con la credencial compartida"*—,
    así que la confirmación quedaba a ojo y el script sin correr. Esto pregunta por estación,
    esté encendida o apagada.
    """
    config = _config()
    if config is None:
        return None
    url_base, api_key, api_secret = config

    username = estacion.codigo
    try:
        usuario = _peticion(
            'GET', url_base, api_key, api_secret,
            f'/authentication/password_based:built_in_database/users/{username}',
        )
        acl = _peticion(
            'GET', url_base, api_key, api_secret,
            f'/authorization/sources/built_in_database/rules/users/{username}',
        )
    except Exception:
        logger.exception('EMQX: error consultando la credencial de %s', username)
        return {'usuario': None, 'acl': None}

    def interpretar(status):
        if status == 200:
            return True
        if status == 404:
            return False
        return None  # EMQX respondió algo que no permite concluir

    return {'usuario': interpretar(usuario), 'acl': interpretar(acl)}


def revocar_credencial_estacion(estacion) -> bool:
    """Da de baja en EMQX el usuario de `estacion` y sus reglas de ACL.

    La contracara que faltaba de `aprovisionar_credencial_estacion` (`B-1` de la auditoría
    por agentes). Sin esto, rechazar una estación en el panel la frenaba solo a nivel de
    aplicación —los manejadores descartan los datos de una estación que no está
    APROBADA— pero la credencial seguía autenticando contra el broker, que es justo la
    separación que este módulo vino a cerrar. Lo mismo al borrar una `Estacion`: el
    usuario quedaba huérfano en EMQX para siempre.

    Tres resultados, no dos, porque "no se pudo" y "no había nada que dar de baja" piden
    mensajes distintos en el panel:

    - `True`: al terminar la credencial ya no existe en EMQX. Incluye el 404, porque la
      baja es idempotente a propósito: se llama al rechazar y al borrar, y una estación que
      todavía usa la credencial compartida no tiene usuario propio.
    - `False`: EMQX respondió algo que no deja claro que se haya borrado. **Sigue
      autenticando** y hay que reintentar o ir a mano.
    - `None`: no aplica, porque `EMQX_ADMIN_CONFIG` no está configurado — entonces en esta
      instalación no hay credenciales por estación y no hay nada que revocar. Mismo
      convenio que `aprovisionar_credencial_estacion`, que devuelve None en ese caso.

    No lanza, por el mismo motivo que el alta: que EMQX no responda no puede hacer fallar
    un rechazo en el panel.
    """
    config = _config()
    if config is None:
        return None
    url_base, api_key, api_secret = config

    username = estacion.codigo
    try:
        # Primero la ACL y después el usuario. En el orden inverso quedaría, entre las dos
        # llamadas, un usuario sin reglas —que EMQX deja conectar pero sin permisos— y si
        # la segunda falla, una ACL huérfana que volvería a aplicarse si alguien recrea el
        # usuario con el mismo código. Así, si falla la segunda, lo que queda es una
        # credencial sin permisos: inofensiva.
        acl = _peticion(
            'DELETE', url_base, api_key, api_secret,
            f'/authorization/sources/built_in_database/rules/users/{username}',
        )
        usuario = _peticion(
            'DELETE', url_base, api_key, api_secret,
            f'/authentication/password_based:built_in_database/users/{username}',
        )
    except Exception:
        logger.exception('EMQX: error revocando la credencial MQTT de %s', username)
        return False

    ok = usuario in (200, 204, 404) and acl in (200, 204, 404)
    if ok:
        logger.info('EMQX: credencial MQTT de %s revocada (usuario %s, acl %s).', username, usuario, acl)
    else:
        logger.warning(
            'EMQX: no se pudo revocar la credencial de %s (usuario HTTP %s, acl HTTP %s) — '
            'sigue autenticando contra el broker.', username, usuario, acl,
        )
    return ok


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
