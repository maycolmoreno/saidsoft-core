"""Tokens de la API con MFA y vencimiento (S-05 y S-06 de la auditoría de seguridad).

Hasta octubre de 2026 `/api/v1/auth/token/` era `obtain_auth_token` de DRF: usuario y
contraseña alcanzaban, aunque la cuenta tuviera MFA en el panel (S-05), y el token no
vencía nunca (S-06). La API da acceso a lo mismo que el panel con los permisos de la cuenta
(la app móvil de los técnicos y la API RMM de solo lectura), así que era otra puerta sin MFA.

- **Pedir un token** exige el código TOTP a la cuenta que tiene MFA, con las mismas reglas
  que el login del panel. Con `MFA_OBLIGATORIO`, una cuenta sin MFA no obtiene token: lo
  configura primero en el panel (la API no puede enrolar a nadie).
- **Todo token vence** a los `API_TOKEN_DIAS` días de creado. Uno vencido responde 401; pedir
  otro con usuario, contraseña y código lo renueva.

Las cuentas de servicio (por ejemplo la integración de inventario) no usan este endpoint:
su token lo crea un administrador en Admin → Tokens, y vence igual. Para renovarlo se borra
y se crea otro.
"""
from django_otp import match_token, user_has_device
from rest_framework import serializers
from rest_framework.authtoken.models import Token
from rest_framework.authtoken.serializers import AuthTokenSerializer
from rest_framework.authtoken.views import ObtainAuthToken
from rest_framework.response import Response

from .autenticacion_api import token_vencido
from .mfa import mfa_obligatorio


class TokenConMFASerializer(AuthTokenSerializer):
    otp_token = serializers.CharField(
        label='Código de autenticación', required=False, allow_blank=True, write_only=True,
    )

    def validate(self, attrs):
        # La contraseña la valida DRF con `authenticate(request=...)`, así que django-axes
        # sigue contando los intentos fallidos igual que en el login del panel.
        attrs = super().validate(attrs)
        usuario = attrs['user']
        if user_has_device(usuario):
            codigo = (attrs.get('otp_token') or '').strip()
            if not codigo or not match_token(usuario, codigo):
                raise serializers.ValidationError('Código de autenticación inválido o faltante.', code='otp_invalido')
        elif mfa_obligatorio():
            raise serializers.ValidationError(
                'La verificación en dos pasos es obligatoria: configurala en el panel antes de usar la API.',
                code='mfa_requerido',
            )
        return attrs


class ObtenerTokenConMFA(ObtainAuthToken):
    """Entrega el token de la cuenta (lo crea si no tiene, lo renueva si venció)."""

    serializer_class = TokenConMFASerializer

    def post(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data, context={'request': request})
        serializer.is_valid(raise_exception=True)
        usuario = serializer.validated_data['user']
        token, _ = Token.objects.get_or_create(user=usuario)
        if token_vencido(token):
            token.delete()
            token = Token.objects.create(user=usuario)
        return Response({'token': token.key})
