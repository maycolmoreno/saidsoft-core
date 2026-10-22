"""Firma Ed25519 de los catálogos del agente (agente 0.38, H-03 y S-11).

Hasta 0.37 los catálogos iban firmados solo con HMAC y el secreto COMPARTIDO
(`COMANDO_HMAC_SECRET`). Eso tenía dos problemas:

- H-03: desde 0.21 la instalación deja vacío el secreto compartido en el `config.txt`
  (cada estación usa el suyo, que llega al enrolarse). Esas estaciones no pueden validar
  un catálogo firmado con el compartido y se quedan con el viejo. El 22-oct-2026 eran 20
  de 63.
- S-11: quien tenga el `config.txt` de una estación vieja tiene el secreto compartido y
  puede firmar catálogos para toda la flota.

Con Ed25519 el servidor firma con una clave PRIVADA que solo tiene `web`, y el agente
verifica con la PÚBLICA, que lleva dentro del ejecutable. Una estación no guarda nada
que sirva para firmar.

La firma va en `firma_ed25519`, junto a la HMAC de siempre (los agentes anteriores a 0.38
siguen usando la HMAC). Firma el mismo mensaje que la HMAC: `comando|catalogo|timestamp`.

Sin `CATALOGO_FIRMA_PRIVADA` configurada no se agrega nada: el servidor se comporta como
antes.
"""
import base64
import logging

from django.conf import settings

logger = logging.getLogger(__name__)


def mensaje(comando: str, catalogo: str, timestamp) -> bytes:
    """Lo que se firma. Igual que `firmar_payload` del HMAC (valores unidos con "|") y
    igual que lo reconstruye el agente en `_catalogo_verificado`."""
    return '|'.join(str(v) for v in (comando, catalogo, timestamp)).encode('utf-8')


def _clave_privada():
    """La clave de `CATALOGO_FIRMA_PRIVADA` (32 bytes en base64), o None si no hay.
    Una clave mal cargada se informa en el log y no rompe la publicación: el catálogo
    sale igual, con la firma HMAC sola."""
    texto = (getattr(settings, 'CATALOGO_FIRMA_PRIVADA', '') or '').strip()
    if not texto:
        return None
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    try:
        return Ed25519PrivateKey.from_private_bytes(base64.b64decode(texto, validate=True))
    except ValueError:
        logger.error('CATALOGO_FIRMA_PRIVADA no es una clave Ed25519 válida: los catálogos salen sin firma Ed25519.')
        return None


def firmar(comando: str, catalogo: str, timestamp) -> str | None:
    """Firma en base64, o None si no hay clave configurada."""
    clave = _clave_privada()
    if clave is None:
        return None
    return base64.b64encode(clave.sign(mensaje(comando, catalogo, timestamp))).decode('ascii')


def _publica_b64(clave_privada) -> str:
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

    return base64.b64encode(
        clave_privada.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw),
    ).decode('ascii')


def clave_publica() -> str | None:
    """La pública de la clave configurada (base64), o None. Es la que va en el agente."""
    clave = _clave_privada()
    return _publica_b64(clave) if clave is not None else None


def generar() -> tuple[str, str]:
    """Un par nuevo: (privada, pública), las dos en base64."""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat

    clave = Ed25519PrivateKey.generate()
    privada = base64.b64encode(
        clave.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption()),
    ).decode('ascii')
    return privada, _publica_b64(clave)
