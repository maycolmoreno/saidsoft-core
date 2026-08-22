import base64
import io

import qrcode
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import redirect, render
from django_otp import user_has_device
from django_otp.plugins.otp_totp.models import TOTPDevice

from apps.auditoria.models import registrar_evento


def _qr_data_uri(config_url: str) -> str:
    imagen = qrcode.make(config_url)
    buffer = io.BytesIO()
    imagen.save(buffer, format='PNG')
    return 'data:image/png;base64,' + base64.b64encode(buffer.getvalue()).decode('ascii')


@login_required
def mfa_estado(request):
    return render(request, 'panel/mfa_estado.html', {'activado': user_has_device(request.user)})


@login_required
def mfa_configurar(request):
    """Enrolamiento de TOTP (SEC-4). Opcional, por usuario — no se fuerza a ningún rol
    todavía (ver apps.cuentas.forms.LoginConOTPForm). El dispositivo se crea sin
    confirmar y no cuenta para el login (`user_has_device` filtra `confirmed=True` por
    default) hasta que el usuario prueba un código real leído de su app autenticadora."""
    if user_has_device(request.user):
        return redirect('panel:mfa_estado')

    dispositivo, _creado = TOTPDevice.objects.get_or_create(
        user=request.user, confirmed=False, defaults={'name': 'default'},
    )
    error = None
    if request.method == 'POST':
        token = request.POST.get('token', '').strip()
        if dispositivo.verify_token(token):
            dispositivo.confirmed = True
            dispositivo.save(update_fields=['confirmed'])
            registrar_evento(usuario=request.user, accion='usuario.mfa_activar', objeto=request.user, request=request)
            messages.success(request, 'Verificación en dos pasos activada.')
            return redirect('panel:mfa_estado')
        error = 'Código inválido — probá de nuevo con el código actual de tu app.'

    return render(request, 'panel/mfa_configurar.html', {
        'qr_data_uri': _qr_data_uri(dispositivo.config_url),
        # Mismo formato base32 que va adentro del QR (config_url) — para cargarlo a mano
        # en la app autenticadora si no se puede escanear el código.
        'secreto_manual': base64.b32encode(dispositivo.bin_key).decode('ascii'),
        'error': error,
    })


@login_required
def mfa_desactivar(request):
    if request.method == 'POST':
        password = request.POST.get('password', '')
        if request.user.check_password(password):
            TOTPDevice.objects.filter(user=request.user).delete()
            registrar_evento(
                usuario=request.user, accion='usuario.mfa_desactivar', objeto=request.user, request=request,
            )
            messages.success(request, 'Verificación en dos pasos desactivada.')
        else:
            messages.error(request, 'Contraseña incorrecta — no se desactivó nada.')
    return redirect('panel:mfa_estado')
