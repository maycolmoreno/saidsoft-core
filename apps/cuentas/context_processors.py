"""Datos de unidad de negocio disponibles en todas las plantillas del panel, para
el selector de la barra superior (ver templates/panel/base.html)."""
from .services import unidad_negocio_activa, unidades_negocio_visibles


def unidad_negocio_contexto(request):
    if not getattr(request, 'user', None) or not request.user.is_authenticated:
        return {}
    visibles = unidades_negocio_visibles(request.user)
    return {
        # Solo se ofrece el selector si hay algo entre qué elegir.
        'unidades_negocio_selector': visibles if visibles.count() > 1 else None,
        'unidad_negocio_activa': unidad_negocio_activa(request),
    }
