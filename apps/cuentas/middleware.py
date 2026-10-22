from django.http import HttpResponse
from django.shortcuts import redirect
from django.urls import reverse

from .mfa import falta_configurar

# Rutas que una cuenta sin MFA puede usar mientras lo configura: entrar, salir, la propia
# configuración, y lo que no es del panel (estáticos, archivos de los agentes, la API, que
# autentica por token y no por sesión, y el webhook de WhatsApp).
PREFIJOS_LIBRES = ('/static/', '/media/', '/api/', '/whatsapp/webhook/')
VISTAS_LIBRES = ('panel:login', 'panel:logout', 'panel:mfa_configurar', 'panel:mfa_estado')


class MFAObligatorioMiddleware:
    """Con `MFA_OBLIGATORIO`, una sesión sin dispositivo TOTP solo puede configurarlo:
    cualquier otra página del panel o del admin lleva a la configuración (S-07; ver
    apps.cuentas.mfa). Que haya llegado a tener sesión ya lo decidió el login (plazo o
    ventana individual); acá solo se impide usarla antes de enrolarse."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        user = getattr(request, 'user', None)
        if user is not None and not request.path.startswith(PREFIJOS_LIBRES) and falta_configurar(user):
            libres = {reverse(nombre) for nombre in VISTAS_LIBRES}
            if request.path not in libres:
                destino = reverse('panel:mfa_configurar')
                if request.headers.get('HX-Request'):
                    # Un fragmento htmx no puede seguir un redirect a una página completa.
                    return HttpResponse(status=204, headers={'HX-Redirect': destino})
                return redirect(destino)
        return self.get_response(request)
