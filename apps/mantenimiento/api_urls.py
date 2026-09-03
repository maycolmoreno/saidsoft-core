from django.urls import path
from rest_framework.routers import DefaultRouter

from .api_views import (
    ConsentimientoMonitoreoView, MantenimientoViewSet, UbicacionTecnicoView, UsuarioActualView,
)

router = DefaultRouter()
router.register('mantenimientos', MantenimientoViewSet, basename='api-mantenimiento')

urlpatterns = router.urls + [
    path('consentimiento-monitoreo/', ConsentimientoMonitoreoView.as_view(), name='api-consentimiento-monitoreo'),
    path('ubicaciones-tecnico/', UbicacionTecnicoView.as_view(), name='api-ubicaciones-tecnico'),
    path('auth/yo/', UsuarioActualView.as_view(), name='api-auth-yo'),
]
