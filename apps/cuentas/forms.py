from django import forms
from django.contrib.auth.forms import AuthenticationForm
from django_otp import match_token, user_has_device


class LoginConOTPForm(AuthenticationForm):
    """SEC-4 (auditoría 22-ago-2026): MFA opcional por usuario. Si la cuenta activó un
    dispositivo TOTP (ver apps.panel.views.mfa), este mismo formulario exige el código
    de 6 dígitos en el mismo paso del login, después de que la contraseña ya haya sido
    validada por AuthenticationForm.clean() (y contada por django-axes vía el backend).

    Un código OTP incorrecto no lo cuenta django-axes (pasa por acá, no por el backend
    de auth que axes envuelve) — pero sí lo frena el propio TOTPDevice: django-otp trae
    throttling con backoff exponencial por dispositivo (1s, 2s, 4s, 8s... tras cada
    fallo consecutivo, ver OTP_TOTP_THROTTLE_FACTOR / django_otp.models.ThrottlingMixin),
    así que la fuerza bruta sobre el código igual queda acotada sin agregar nada más acá.
    """
    otp_token = forms.CharField(
        required=False, label='Código de autenticación',
        help_text='Solo si activaste la verificación en dos pasos en tu cuenta.',
        widget=forms.TextInput(attrs={'autocomplete': 'one-time-code', 'inputmode': 'numeric', 'autofocus': False}),
    )

    def clean(self):
        cleaned_data = super().clean()
        user = self.get_user()
        if user is not None and user_has_device(user):
            token = (cleaned_data.get('otp_token') or '').strip()
            if not token or not match_token(user, token):
                raise forms.ValidationError(
                    'Código de autenticación inválido o faltante.', code='otp_invalido',
                )
        return cleaned_data
