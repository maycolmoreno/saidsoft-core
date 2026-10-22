"""Autenticación de la API por token con vencimiento (S-06). Ver apps.cuentas.api_auth.

Módulo aparte a propósito: `REST_FRAMEWORK['DEFAULT_AUTHENTICATION_CLASSES']` apunta acá, y
DRF lo importa al definir `APIView`. Si viviera junto a la vista que entrega el token (que a
su vez importa `APIView`), la importación sería circular.
"""
from datetime import timedelta

from django.conf import settings
from django.utils import timezone
from rest_framework import exceptions
from rest_framework.authentication import TokenAuthentication


def vigencia_token() -> timedelta:
    return timedelta(days=int(getattr(settings, 'API_TOKEN_DIAS', 30)))


def token_vencido(token, ahora=None) -> bool:
    return (ahora or timezone.now()) - token.created > vigencia_token()


class TokenConVencimiento(TokenAuthentication):
    """`TokenAuthentication` de DRF que además rechaza los tokens vencidos."""

    def authenticate_credentials(self, key):
        usuario, token = super().authenticate_credentials(key)
        if token_vencido(token):
            raise exceptions.AuthenticationFailed('El token venció: pedí uno nuevo en /api/v1/auth/token/.')
        return usuario, token
