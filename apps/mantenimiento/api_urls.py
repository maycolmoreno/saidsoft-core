from django.urls import path
from rest_framework.routers import DefaultRouter

from .api_views import (
    ActividadChecklistView, ConsentimientoMonitoreoView, EquipoListView, MantenimientoViewSet,
    NotificacionConteoView, NotificacionLeerView, NotificacionListView, UbicacionTecnicoView, UsuarioActualView,
)

router = DefaultRouter()
router.register('mantenimientos', MantenimientoViewSet, basename='api-mantenimiento')

urlpatterns = router.urls + [
    path('consentimiento-monitoreo/', ConsentimientoMonitoreoView.as_view(), name='api-consentimiento-monitoreo'),
    path('ubicaciones-tecnico/', UbicacionTecnicoView.as_view(), name='api-ubicaciones-tecnico'),
    path('auth/yo/', UsuarioActualView.as_view(), name='api-auth-yo'),
    path('actividades-checklist/', ActividadChecklistView.as_view(), name='api-actividades-checklist'),
    path('equipos/', EquipoListView.as_view(), name='api-equipos'),
    path('notificaciones/', NotificacionListView.as_view(), name='api-notificaciones'),
    path('notificaciones/count/', NotificacionConteoView.as_view(), name='api-notificaciones-count'),
    path('notificaciones/<int:pk>/leer/', NotificacionLeerView.as_view(), name='api-notificaciones-leer'),
]
