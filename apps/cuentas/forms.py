from django import forms
from django.contrib.auth.forms import AuthenticationForm
from django_otp import match_token, user_has_device

from .mfa import mfa_obligatorio, puede_enrolarse


class LoginConOTPForm(AuthenticationForm):
    """SEC-4 (auditoría 22-ago-2026): MFA por usuario. Si la cuenta activó un
    dispositivo TOTP (ver apps.panel.views.mfa), este mismo formulario exige el código
    de 6 dígitos en el mismo paso del login, después de que la contraseña ya haya sido
    validada por AuthenticationForm.clean() (y contada por django-axes vía el backend).

    Con `MFA_OBLIGATORIO` (S-07, ver apps.cuentas.mfa), una cuenta SIN dispositivo entra
    solo mientras pueda enrolarse (dentro del plazo o con una ventana individual), y el
    middleware la manda a configurarlo. Pasado el plazo, no entra.

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

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Para la plantilla: con el MFA obligatorio, el error genérico lleva una
        # indicación para quien quedó afuera por el plazo (ver panel/login.html).
        self.mfa_obligatorio = mfa_obligatorio()
        if self.mfa_obligatorio:
            self.fields['otp_token'].help_text = (
                'El código de 6 dígitos de tu app autenticadora. Si todavía no la configuraste, '
                'dejalo vacío: te la pide después de entrar, mientras esté abierto el plazo.'
            )

    def clean(self):
        cleaned_data = super().clean()
        user = self.get_user()
        if user is None:
            return cleaned_data
        if user_has_device(user):
            token = (cleaned_data.get('otp_token') or '').strip()
            if not token or not match_token(user, token):
                raise forms.ValidationError(
                    'Código de autenticación inválido o faltante.', code='otp_invalido',
                )
        elif mfa_obligatorio() and not puede_enrolarse(user):
            # La plantilla muestra el mismo mensaje genérico que para una contraseña
            # equivocada: un mensaje propio le confirmaría a quien probó que acertó.
            raise forms.ValidationError(
                'Esta cuenta no tiene configurada la verificación en dos pasos y el plazo para '
                'hacerlo venció. Pedile a un administrador que te habilite el enrolamiento.',
                code='mfa_requerido',
            )
        return cleaned_data
