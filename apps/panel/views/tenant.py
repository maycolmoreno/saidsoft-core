from django.contrib.auth.decorators import login_required
from django.shortcuts import redirect
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST

from apps.cuentas.services import SESSION_KEY_UNIDAD_ACTIVA, unidades_negocio_visibles


@login_required
@require_POST
def unidad_negocio_activar(request):
    """Fija (o limpia, con valor vacío) la unidad de negocio activa en sesión —
    ver apps.cuentas.services.unidad_negocio_activa. Puramente de presentación:
    no otorga acceso a nada que el usuario no pudiera ver ya."""
    id_elegido = request.POST.get('unidad_negocio', '')
    if id_elegido and unidades_negocio_visibles(request.user).filter(pk=id_elegido).exists():
        request.session[SESSION_KEY_UNIDAD_ACTIVA] = id_elegido
    else:
        request.session.pop(SESSION_KEY_UNIDAD_ACTIVA, None)

    referer = request.META.get('HTTP_REFERER')
    if referer and url_has_allowed_host_and_scheme(referer, allowed_hosts={request.get_host()}, require_https=request.is_secure()):
        return redirect(referer)
    return redirect('panel:dashboard')
