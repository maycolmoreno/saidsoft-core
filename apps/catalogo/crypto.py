"""Cifrado simétrico en reposo para datos realmente sensibles (ej. clave de
recuperación de BitLocker) — no para todo, solo para lo que amerita más que el
control de acceso de Django (alguien con acceso directo a la base de datos no
debe poder leer esto en texto plano).

Usa Fernet (AES-128-CBC + HMAC, con rotación de timestamp incluida) sobre
BITLOCKER_ENCRYPTION_KEY. Requiere una clave real en `.env` — sin default,
igual que COMANDO_HMAC_SECRET: no tiene sentido un valor de reserva inseguro
para esto.
"""
from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured


def _fernet() -> Fernet:
    clave = settings.BITLOCKER_ENCRYPTION_KEY
    try:
        return Fernet(clave.encode() if isinstance(clave, str) else clave)
    except (ValueError, TypeError) as exc:
        raise ImproperlyConfigured(
            'BITLOCKER_ENCRYPTION_KEY inválida — debe ser una clave Fernet válida '
            '(generarla con: python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())").',
        ) from exc


def cifrar(texto_plano: str) -> str:
    return _fernet().encrypt(texto_plano.encode('utf-8')).decode('utf-8')


def descifrar(token: str) -> str:
    try:
        return _fernet().decrypt(token.encode('utf-8')).decode('utf-8')
    except InvalidToken as exc:
        raise ValueError('El token cifrado no es válido o fue cifrado con otra clave.') from exc
