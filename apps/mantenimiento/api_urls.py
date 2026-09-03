from django.urls import path
from rest_framework.routers import DefaultRouter

from .api_views import (
    ActividadChecklistView, ActivoCrearView, CatalogosView, ConsentimientoMonitoreoView, EquipoListView,
    MantenimientoViewSet,
    NotificacionConteoView, NotificacionLeerView, NotificacionListView, UbicacionTecnicoView, UsuarioActualView,
    VisitaTecnicaViewSet,
)

router = DefaultRouter()
router.register('mantenimientos', MantenimientoViewSet, basename='api-mantenimiento')
router.register('visitas', VisitaTecnicaViewSet, basename='api-visita')

urlpatterns = router.urls + [
    path('consentimiento-monitoreo/', ConsentimientoMonitoreoView.as_view(), name='api-consentimiento-monitoreo'),
    path('ubicaciones-tecnico/', UbicacionTecnicoView.as_view(), name='api-ubicaciones-tecnico'),
    path('auth/yo/', UsuarioActualView.as_view(), name='api-auth-yo'),
    path('actividades-checklist/', ActividadChecklistView.as_view(), name='api-actividades-checklist'),
    path('equipos/', EquipoListView.as_view(), name='api-equipos'),
    path('equipos/nuevo/', ActivoCrearView.as_view(), name='api-equipo-crear'),
    path('catalogos/', CatalogosView.as_view(), name='api-catalogos'),
    path('notificaciones/', NotificacionListView.as_view(), name='api-notificaciones'),
    path('notificaciones/count/', NotificacionConteoView.as_view(), name='api-notificaciones-count'),
    path('notificaciones/<int:pk>/leer/', NotificacionLeerView.as_view(), name='api-notificaciones-leer'),
]
