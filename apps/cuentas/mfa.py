"""Reglas del MFA obligatorio (S-07 de la auditoría de seguridad y estabilidad).

Hasta octubre de 2026 el MFA era opcional: lo pedía el login solo a quien había
configurado un dispositivo (`LoginConOTPForm`), y ninguna de las tres cuentas con
privilegios lo tenía. Además `/admin/` usaba el login estándar de Django, sin código, y
como la sesión es la misma, entrar por ahí abría también el panel.

Con `MFA_OBLIGATORIO`:

- **Dentro del plazo de enrolamiento** (`MFA_PLAZO_ENROLAMIENTO`, una fecha) una cuenta
  sin dispositivo entra con su contraseña y queda obligada a configurar el MFA antes de
  usar el panel (`MFAObligatorioMiddleware`).
- **Pasado el plazo** una cuenta sin dispositivo no entra. Un administrador puede abrirle
  una ventana individual con `PerfilUsuario.mfa_enrolar_hasta` (alguien que cambió de
  teléfono o que se incorporó después).

El plazo existe por un motivo de seguridad, no de comodidad: mientras alguien puede entrar
solo con contraseña para enrolarse, quien le haya robado la contraseña puede enrolar su
propio teléfono primero. Por eso tiene que ser corto y con fecha.

Sin `MFA_OBLIGATORIO` todo sigue como antes (MFA opcional), salvo `/admin/`, que desde
ahora pasa siempre por el login del panel.
"""
from datetime import datetime, time

from django.conf import settings
from django.utils import timezone
from django_otp import user_has_device


def mfa_obligatorio() -> bool:
    return bool(getattr(settings, 'MFA_OBLIGATORIO', False))


def fin_del_plazo():
    """Fin del día de `MFA_PLAZO_ENROLAMIENTO`, en la hora local. None si no hay fecha:
    sin fecha, el enrolamiento al entrar sigue abierto (no recomendado más que para
    arrancar)."""
    texto = (getattr(settings, 'MFA_PLAZO_ENROLAMIENTO', '') or '').strip()
    if not texto:
        return None
    fecha = datetime.strptime(texto, '%Y-%m-%d').date()
    return timezone.make_aware(datetime.combine(fecha, time.max))


def puede_enrolarse(usuario, ahora=None) -> bool:
    """Si una cuenta SIN dispositivo puede entrar para configurarlo."""
    ahora = ahora or timezone.now()
    fin = fin_del_plazo()
    if fin is None or ahora <= fin:
        return True
    perfil = getattr(usuario, 'perfil', None)
    excepcion = getattr(perfil, 'mfa_enrolar_hasta', None) if perfil is not None else None
    return bool(excepcion and ahora <= excepcion)


def falta_configurar(usuario) -> bool:
    """La cuenta tiene que configurar el MFA antes de seguir."""
    return mfa_obligatorio() and usuario.is_authenticated and not user_has_device(usuario)
