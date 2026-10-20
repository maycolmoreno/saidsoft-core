"""Rutas de la API de integración RMM (`/api/v1/rmm/`), de solo lectura.

Las alertas viven en `apps.monitoreo.api_rmm`; se montan acá para que todo lo que un
sistema externo consume de SaidSoft quede bajo un mismo prefijo.
"""
from django.urls import path

from apps.monitoreo.api_rmm import AlertasRmmView

from .api_views import EstacionesRmmView, EstacionRmmDetalleView, FarmaciasRmmView

urlpatterns = [
    path('estaciones/', EstacionesRmmView.as_view(), name='api-rmm-estaciones'),
    path('estaciones/<str:codigo>/', EstacionRmmDetalleView.as_view(), name='api-rmm-estacion'),
    path('farmacias/', FarmaciasRmmView.as_view(), name='api-rmm-farmacias'),
    path('alertas/', AlertasRmmView.as_view(), name='api-rmm-alertas'),
]
