"""Adaptador MeshCentral: cliente del canal de control (`control.ashx`) por WebSocket.

Protocolo verificado contra el código fuente del servidor (Ylianst/MeshCentral —
webserver.js, meshuser.js, meshcentral.js) y contra el servidor real de producción
(10.111.6.20:8083, 13-ago-2026: login real, `{"action":"nodes"}` trajo la estación
piloto ML016-B, `sincronizar_todo()` escribió el EstadoDispositivo correcto en la BD).
Dos bugs reales encontrados y corregidos en esa prueba:
1. **TLS**: MeshCentral autogenera su propio certificado autofirmado por instancia (no
   hay un CA fijo versionado como `deploy/certs/cert.pem` de EMQX) — sin `CA_CERT` o
   `VERIFICAR_TLS=False` en `MESHCENTRAL_API_CONFIG`, la conexión falla siempre con
   `CERTIFICATE_VERIFY_FAILED`, incluso siendo el servidor legítimo.
2. **Carrera en el login**: un `{"action":"nodes"}` mandado inmediatamente después del
   `userAuth` se pierde de forma consistente — el servidor todavía está armando la
   sesión del usuario (manda `serverinfo`/`userinfo`/`traceinfo` primero) y recién ahí
   queda listo para procesar comandos. `_solicitar_nodes` reintenta una vez si no llega
   respuesta a tiempo (ver su docstring).

Puntos clave del protocolo:

- **Auth**: conectar a `control.ashx` con el header `x-meshauth: *` (activa el modo de
  "inner auth" para clientes que no son el navegador, en vez de depender de la cookie de
  sesión) y como primer mensaje mandar
  `{"action":"userAuth","username":base64(...),"password":base64(...)}`. Si falla, el
  servidor responde `{"action":"close",...}` y cierra. No usa el token `?auth=` de la URL
  a propósito: ese es un cookie cifrado con 1h de expiración pensado para el navegador,
  no para un cliente headless de larga duración.
- **Snapshot inicial**: `{"action":"nodes"}` responde con todos los nodos visibles para
  el usuario, agrupados por meshid, cada uno con `conn` (bitmask: bit0=agente MeshAgent
  conectado, bit1=CIRA/relay) y `pwr` (power state).
- **Eventos en tiempo real**: el servidor empuja sin que se pida
  `{"action":"event","event":{"action":"nodeconnect","nodeid":...,"conn":N,"pwr":N,...}}`
  cada vez que cambia la conectividad de un nodo — de ahí que este adaptador no necesite
  polling propio, solo mantener la conexión abierta (ver run_meshcentral_worker).
- `nodeid` viaja completo (ej. "node/<domain>/<hash>"); `Estacion.meshcentral_node_id`
  guarda solo el `<hash>` final — `_id_corto()` normaliza antes de comparar.
- **Auto-vínculo por nombre** (21-ago-2026): `apps.catalogo.services.
  generar_comando_instalacion_meshcentral` instala el agente con
  `--agentName=<código de la estación>`, así que el `name` que trae cada nodo (en el
  snapshot y, cuando el evento lo incluye, también en `nodeconnect`) coincide con
  `Estacion.codigo`. `_vincular_por_nombre` usa ese match para completar
  `meshcentral_node_id` sola la primera vez que el nodo aparece — ya no hace falta
  copiarlo a mano desde la consola web salvo para estaciones instaladas antes de este
  cambio (o cuyo agente se haya nombrado distinto).
"""
import base64
import json
import logging
import threading

import websocket
from django.conf import settings
from django.utils import timezone

from apps.catalogo.models import Estacion
from apps.monitoreo.adapters.base import FuenteMonitoreo
from apps.monitoreo.models import EstadoDispositivo
from apps.monitoreo.services import registrar_estado_dispositivo

logger = logging.getLogger(__name__)

BIT_AGENTE_CONECTADO = 1


def _id_corto(nodeid: str) -> str:
    return nodeid.rsplit('/', 1)[-1] if nodeid else ''


def _en_linea(conn: int) -> bool:
    return bool(conn & BIT_AGENTE_CONECTADO)


class AdaptadorMeshCentral(FuenteMonitoreo):
    def __init__(self):
        cfg = getattr(settings, 'MESHCENTRAL_API_CONFIG', {})
        self._url = cfg.get('WS_URL', '')
        self._usuario = cfg.get('USUARIO', '')
        self._password = cfg.get('PASSWORD', '')
        self._ca_cert = cfg.get('CA_CERT', '')
        self._verificar_tls = cfg.get('VERIFICAR_TLS', True)

    def configurado(self) -> bool:
        return bool(self._url and self._usuario and self._password)

    def _sslopt(self) -> dict:
        """MeshCentral autogenera su propio certificado autofirmado por instancia (no
        hay un CA fijo versionado en el repo, a diferencia de EMQX/deploy/certs/cert.pem)
        — sin esto, la verificación TLS por defecto de Python rechaza la conexión
        siempre, incluso contra un servidor legítimo (confirmado: la primera prueba
        contra el servidor real de producción falló acá con CERTIFICATE_VERIFY_FAILED).
        Preferí `CA_CERT` (fijar el certificado real, extraído una vez del servidor) por
        sobre `VERIFICAR_TLS=False` (desactiva la verificación por completo) — ver
        MESHCENTRAL_API_CONFIG en settings."""
        if self._ca_cert:
            return {'ca_certs': self._ca_cert}
        if not self._verificar_tls:
            import ssl
            return {'cert_reqs': ssl.CERT_NONE}
        return {}

    def _autenticar_y_conectar(self):
        ws = websocket.create_connection(
            self._url, timeout=15, header=['x-meshauth: *'], sslopt=self._sslopt(),
        )
        ws.send(json.dumps({
            'action': 'userAuth',
            'username': base64.b64encode(self._usuario.encode()).decode(),
            'password': base64.b64encode(self._password.encode()).decode(),
        }))
        return ws

    def _vincular_por_nombre(self, nodeid: str, nombre: str):
        """Auto-vínculo: `generar_comando_instalacion_meshcentral` nombra el agente con
        el código de la estación (`--agentName`), así que si el nombre del nodo
        coincide con una estación que todavía no tiene `meshcentral_node_id`, se
        vincula sola — sin esto había que copiarlo a mano desde la consola de
        MeshCentral cada vez. Nunca pisa un vínculo ya existente (`meshcentral_node_id=''`
        en el filtro)."""
        if not nombre:
            return None
        estacion = Estacion.objects.filter(codigo=nombre, meshcentral_node_id='').first()
        if estacion is None:
            return None
        estacion.meshcentral_node_id = _id_corto(nodeid)
        estacion.meshcentral_vinculado_en = timezone.now()
        estacion.save(update_fields=['meshcentral_node_id', 'meshcentral_vinculado_en'])
        logger.info('MeshCentral: %s vinculada automáticamente a %s', estacion.codigo, nodeid)
        return estacion

    def _registrar_nodo(self, nodeid: str, conn: int, nombre: str = '') -> int:
        estacion = Estacion.objects.filter(meshcentral_node_id=_id_corto(nodeid)).first()
        if estacion is None:
            estacion = self._vincular_por_nombre(nodeid, nombre)
        if estacion is None:
            # Estación sin vincular todavía y sin nombre que matchee — no es un error,
            # simplemente no participa del cruce hasta que se vincule (a mano o sola).
            return 0
        registrar_estado_dispositivo(
            estacion, fuente=EstadoDispositivo.Fuente.MESHCENTRAL,
            en_linea=_en_linea(conn), detalle={'conn': conn},
        )
        return 1

    def _procesar_nodes(self, msg: dict) -> int:
        nodos = [n for lista in (msg.get('nodes') or {}).values() for n in lista]
        return sum(self._registrar_nodo(n.get('_id', ''), n.get('conn', 0), n.get('name', '')) for n in nodos)

    def _procesar_mensaje(self, msg: dict) -> None:
        if msg.get('action') != 'event':
            return
        evento = msg.get('event') or {}
        if evento.get('action') != 'nodeconnect':
            return
        self._registrar_nodo(evento.get('nodeid', ''), evento.get('conn', 0), evento.get('name', ''))

    def _solicitar_nodes(self, ws, *, reintentos: int = 2, timeout_por_intento: int = 8) -> int:
        """Pide `{"action":"nodes"}` y espera la respuesta, procesando de paso
        cualquier evento que llegue mientras tanto (no se descarta).

        Reintenta el pedido si no llega respuesta en `timeout_por_intento` segundos.
        Verificado contra el servidor real de producción (13-ago-2026): un `nodes`
        mandado inmediatamente después del `userAuth` se pierde de forma consistente —
        el servidor todavía está terminando de armar la sesión del usuario (manda tres
        mensajes propios, `serverinfo`/`userinfo`/`traceinfo`, y recién ahí queda listo
        para procesar comandos). Sin este reintento, sincronizar_todo() cuelga siempre
        en el primer llamado tras loguear.
        """
        ws.settimeout(timeout_por_intento)
        ws.send(json.dumps({'action': 'nodes'}))
        intentos_restantes = reintentos
        while True:
            try:
                msg = json.loads(ws.recv())
            except websocket.WebSocketTimeoutException:
                if intentos_restantes <= 0:
                    raise
                intentos_restantes -= 1
                ws.send(json.dumps({'action': 'nodes'}))
                continue
            if msg.get('action') == 'close':
                logger.warning('MeshCentral: autenticación rechazada (%s).', msg.get('msg'))
                return 0
            if msg.get('action') == 'nodes':
                return self._procesar_nodes(msg)
            self._procesar_mensaje(msg)

    def sincronizar_todo(self) -> int:
        """Resync completo: snapshot inicial o respaldo periódico por si se perdió un
        evento del WebSocket mientras el worker estaba caído."""
        if not self.configurado():
            return 0
        ws = None
        try:
            ws = self._autenticar_y_conectar()
            return self._solicitar_nodes(ws)
        except Exception:
            logger.exception('MeshCentral: error en sincronizar_todo')
            return 0
        finally:
            if ws is not None:
                try:
                    ws.close()
                except Exception:
                    pass

    def escuchar_eventos(self, *, detener: threading.Event, on_conectado=None) -> None:
        """Bucle de larga duración para run_meshcentral_worker: mantiene la conexión
        abierta y procesa nodeconnect en tiempo real. Reconecta con backoff simple
        (1s, 2s, 4s... hasta 60s) si se cae la conexión de verdad.

        Verificado contra el servidor real de producción (14-ago-2026): que `ws.recv()`
        expire por inactividad (nadie mandó nada, el caso normal la mayor parte del
        tiempo) NO es una conexión perdida — sin este cuidado, el bucle reconectaba
        (re-auth + resync completo) cada 8-15s indefinidamente, disfrazando el diseño de
        "push, sin polling" en un poll agresivo cada pocos segundos.
        """
        backoff = 1
        while not detener.is_set():
            ws = None
            try:
                ws = self._autenticar_y_conectar()
                if on_conectado:
                    on_conectado(ws)
                self._solicitar_nodes(ws)  # resync al (re)conectar
                backoff = 1
                # Timeout solo para poder revisar `detener` periódicamente mientras no
                # llega nada — no implica que la conexión se cayó (ver docstring).
                ws.settimeout(30)
                while not detener.is_set():
                    try:
                        raw = ws.recv()
                    except websocket.WebSocketTimeoutException:
                        continue
                    if not raw:
                        break
                    self._procesar_mensaje(json.loads(raw))
            except Exception:
                if not detener.is_set():
                    logger.exception('MeshCentral: conexión perdida, reintentando en %ss', backoff)
                    detener.wait(backoff)
                    backoff = min(backoff * 2, 60)
            finally:
                if ws is not None:
                    try:
                        ws.close()
                    except Exception:
                        pass
