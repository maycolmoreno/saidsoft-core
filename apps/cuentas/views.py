from django.contrib.auth import REDIRECT_FIELD_NAME
from django.contrib.auth.views import redirect_to_login
from django.shortcuts import redirect
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme


def login_admin_por_panel(request, extra_context=None):
    """Reemplaza el login de `/admin/`. El de Django pide solo usuario y contraseña, y la
    sesión es la misma que la del panel: entrar por `/admin/login/` saltaba el MFA del
    panel aunque la cuenta lo tuviera activado. Ahora el único login es el del panel
    (`LoginConOTPForm`), que vuelve al admin al terminar."""
    destino = request.GET.get(REDIRECT_FIELD_NAME) or reverse('admin:index')
    if not url_has_allowed_host_and_scheme(destino, allowed_hosts={request.get_host()}):
        destino = reverse('admin:index')
    if request.user.is_authenticated and request.user.is_staff:
        return redirect(destino)
    return redirect_to_login(destino, login_url=reverse('panel:login'))
