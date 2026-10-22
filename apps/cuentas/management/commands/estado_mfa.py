"""Qué cuentas activas tienen MFA y cuáles no. Solo lectura.

    python manage.py estado_mfa

Sirve para encender `MFA_OBLIGATORIO` sabiendo a quién le va a pedir que lo configure, y
para revisar después, pasado `MFA_PLAZO_ENROLAMIENTO`, quién quedó afuera.
"""
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from django.utils import timezone
from django_otp import user_has_device

from apps.cuentas.mfa import fin_del_plazo, mfa_obligatorio, puede_enrolarse


class Command(BaseCommand):
    help = 'Lista las cuentas activas con y sin MFA (solo lectura).'

    def handle(self, *args, **opciones):
        fin = fin_del_plazo()
        self.stdout.write(
            f'MFA obligatorio: {"SÍ" if mfa_obligatorio() else "no"} | plazo de enrolamiento: '
            f'{timezone.localtime(fin):%Y-%m-%d %H:%M} ' if fin else
            f'MFA obligatorio: {"SÍ" if mfa_obligatorio() else "no"} | plazo de enrolamiento: sin fecha'
        )
        con, sin = [], []
        usuarios = get_user_model().objects.filter(is_active=True).select_related('perfil').order_by('username')
        for usuario in usuarios:
            (con if user_has_device(usuario) else sin).append(usuario)
        self.stdout.write(f'\nCon MFA ({len(con)}):')
        for usuario in con:
            self.stdout.write(f'  {usuario.username}')
        self.stdout.write(f'\nSin MFA ({len(sin)}):')
        for usuario in sin:
            rol = 'superusuario' if usuario.is_superuser else ('staff' if usuario.is_staff else 'panel')
            acceso = 'puede entrar a configurarlo' if puede_enrolarse(usuario) else 'NO puede entrar (plazo vencido)'
            self.stdout.write(f'  {usuario.username:25} {rol:12} {acceso}')
