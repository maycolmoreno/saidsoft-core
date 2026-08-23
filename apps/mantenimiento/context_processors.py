"""Contador de notificaciones sin leer para el ícono de la barra superior
(ver templates/panel/base.html) -- mismo patrón que apps.cuentas.context_processors."""
from .models import Notificacion


def notificaciones_contexto(request):
    if not getattr(request, 'user', None) or not request.user.is_authenticated:
        return {}
    return {
        'notificaciones_no_leidas': Notificacion.objects.filter(usuario=request.user, leida=False).count(),
    }
