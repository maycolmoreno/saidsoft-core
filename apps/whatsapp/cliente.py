"""Cliente de la API oficial de WhatsApp Business (Cloud API de Meta).

Solo la API oficial: las librerías que automatizan WhatsApp Web violan los términos y Meta
bloquea el número. Mismo patrón stdlib (urllib, sin `requests`) que el envío de Telegram.

**Modos** (`WHATSAPP_MODO`), para arrancar gradual como pide §10-CM:

- `apagado` (por defecto): no se envía nada y el sistema no pregunta. Los mensajes que
  entran se procesan igual, pero las respuestas no salen.
- `piloto`: el sistema solo pregunta a las farmacias de `WHATSAPP_FARMACIAS_PILOTO`.
- `activo`: a todas las que tengan celular.

`WHATSAPP_REDIRIGIR_A`, si está cargado, manda TODO lo que sale a ese número en vez del de
la farmacia: es el modo de prueba, para ver los mensajes en un celular propio antes de
escribirle a nadie.

El token nunca entra en un log.
"""
import json
import logging
import re
import urllib.error
import urllib.request

from django.conf import settings

logger = logging.getLogger(__name__)

TIMEOUT_SEGUNDOS = 10

# Límites de la API: un botón de respuesta rápida admite 20 caracteres y hay a lo sumo 3
# por mensaje; una fila de lista, 24.
MAX_BOTONES = 3
LARGO_BOTON = 20
LARGO_FILA = 24


def modo() -> str:
    valor = (getattr(settings, 'WHATSAPP_MODO', '') or 'apagado').strip().lower()
    return valor if valor in ('apagado', 'piloto', 'activo') else 'apagado'


def configurado() -> bool:
    return bool(getattr(settings, 'WHATSAPP_TOKEN', '') and getattr(settings, 'WHATSAPP_PHONE_NUMBER_ID', ''))


def farmacia_habilitada(farmacia) -> bool:
    """Si el sistema puede PREGUNTARLE a esta farmacia según el modo."""
    actual = modo()
    if actual == 'activo':
        return True
    if actual == 'piloto':
        piloto = {c.strip().upper() for c in getattr(settings, 'WHATSAPP_FARMACIAS_PILOTO', []) if c.strip()}
        return farmacia.codigo.upper() in piloto
    return False


def numero_wa(telefono: str) -> str:
    """El número en formato internacional sin '+', como lo espera y lo manda WhatsApp.

    En Ecuador los celulares se cargan como 09XXXXXXXX: el 0 se reemplaza por el 593. Un
    número que no tiene forma de celular devuelve '': mejor no escribirle que escribirle a
    un fijo o a un número a medio cargar.
    """
    digitos = re.sub(r'\D', '', telefono or '')
    if re.fullmatch(r'09\d{8}', digitos):
        return '593' + digitos[1:]
    if re.fullmatch(r'5939\d{8}', digitos):
        return digitos
    return ''


def _destino(wa: str) -> str:
    return re.sub(r'\D', '', getattr(settings, 'WHATSAPP_REDIRIGIR_A', '') or '') or wa


def _enviar(wa: str, cuerpo: dict) -> tuple[bool, str]:
    """POST a /messages. Devuelve `(enviado, id_del_mensaje_o_motivo)`. Nunca lanza: un
    WhatsApp que no sale no puede tumbar la evaluación de reglas ni el webhook."""
    if modo() == 'apagado':
        return False, 'modo apagado'
    if not configurado():
        logger.warning('WhatsApp: falta WHATSAPP_TOKEN o WHATSAPP_PHONE_NUMBER_ID, no se envía.')
        return False, 'sin configurar'
    if not wa:
        return False, 'sin número'
    version = getattr(settings, 'WHATSAPP_API_VERSION', 'v21.0')
    url = f'https://graph.facebook.com/{version}/{settings.WHATSAPP_PHONE_NUMBER_ID}/messages'
    datos = {'messaging_product': 'whatsapp', 'to': _destino(wa), **cuerpo}
    req = urllib.request.Request(
        url, data=json.dumps(datos).encode('utf-8'), method='POST',
        headers={'Authorization': f'Bearer {settings.WHATSAPP_TOKEN}', 'Content-Type': 'application/json'},
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_SEGUNDOS) as resp:
            respuesta = json.loads(resp.read().decode('utf-8') or '{}')
    except urllib.error.HTTPError as exc:
        detalle = exc.read().decode('utf-8', 'replace')[:300]
        logger.warning('WhatsApp: la API rechazó el envío a %s (HTTP %s): %s', wa, exc.code, detalle)
        return False, f'HTTP {exc.code}'
    except Exception as exc:
        logger.warning('WhatsApp: no se pudo enviar a %s: %s', wa, exc)
        return False, str(exc)[:200]
    mensajes = respuesta.get('messages') or [{}]
    return True, mensajes[0].get('id', '')


def enviar_texto(wa: str, texto: str):
    return _enviar(wa, {'type': 'text', 'text': {'body': texto[:4096]}})


def enviar_botones(wa: str, texto: str, botones: list[tuple[str, str]]):
    """Hasta 3 botones de respuesta rápida, `(id, título)`. Solo dentro de la ventana de 24
    h que abre la farmacia al escribir; fuera de ella hay que usar una plantilla."""
    return _enviar(wa, {
        'type': 'interactive',
        'interactive': {
            'type': 'button',
            'body': {'text': texto[:1024]},
            'action': {'buttons': [
                {'type': 'reply', 'reply': {'id': i, 'title': t[:LARGO_BOTON]}} for i, t in botones[:MAX_BOTONES]
            ]},
        },
    })


def enviar_lista(wa: str, texto: str, boton: str, filas: list[tuple[str, str]]):
    """Una lista desplegable, para más de 3 opciones."""
    return _enviar(wa, {
        'type': 'interactive',
        'interactive': {
            'type': 'list',
            'body': {'text': texto[:1024]},
            'action': {
                'button': boton[:LARGO_BOTON],
                'sections': [{'title': 'Opciones', 'rows': [
                    {'id': i, 'title': t[:LARGO_FILA]} for i, t in filas[:10]
                ]}],
            },
        },
    })


def enviar_plantilla(wa: str, nombre: str, parametros: list[str], payloads: list[str]):
    """Un mensaje que inicia el sistema: tiene que ser una plantilla aprobada por Meta.
    `parametros` llenan {{1}}, {{2}}… del cuerpo; `payloads` van en los botones de respuesta
    rápida de la plantilla, en orden, y vuelven en el webhook cuando la farmacia toca uno."""
    componentes = []
    if parametros:
        componentes.append({'type': 'body', 'parameters': [{'type': 'text', 'text': p} for p in parametros]})
    for indice, payload in enumerate(payloads):
        componentes.append({
            'type': 'button', 'sub_type': 'quick_reply', 'index': str(indice),
            'parameters': [{'type': 'payload', 'payload': payload}],
        })
    return _enviar(wa, {
        'type': 'template',
        'template': {
            'name': nombre,
            'language': {'code': getattr(settings, 'WHATSAPP_IDIOMA_PLANTILLA', 'es')},
            'components': componentes,
        },
    })
