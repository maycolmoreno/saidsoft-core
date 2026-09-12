from django.urls import path

from .api_views import FarmaciasASondearView, SondeoEnlaceIngestaView

urlpatterns = [
    path('enlaces/farmacias/', FarmaciasASondearView.as_view(), name='api-enlaces-farmacias'),
    path('enlaces/sondeo/', SondeoEnlaceIngestaView.as_view(), name='api-enlaces-sondeo'),
]
