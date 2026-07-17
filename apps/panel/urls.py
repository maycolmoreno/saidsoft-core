from django.contrib.auth import views as auth_views
from django.urls import path

from . import views

app_name = 'panel'

urlpatterns = [
    path('login/', auth_views.LoginView.as_view(template_name='panel/login.html'), name='login'),
    path('logout/', auth_views.LogoutView.as_view(), name='logout'),

    path('', views.dashboard, name='dashboard'),

    path('estaciones/', views.estaciones_lista, name='estaciones_lista'),
    path('estaciones/pendientes/', views.estaciones_pendientes_partial, name='estaciones_pendientes_partial'),
    path('estaciones/<int:pk>/aprobar/', views.estacion_aprobar, name='estacion_aprobar'),
    path('estaciones/<int:pk>/rechazar/', views.estacion_rechazar, name='estacion_rechazar'),

    path('despliegues/', views.despliegues_lista, name='despliegues_lista'),
    path('despliegues/nuevo/', views.despliegue_crear, name='despliegue_crear'),
    path('despliegues/<int:pk>/', views.despliegue_detalle, name='despliegue_detalle'),
    path('despliegues/<int:pk>/progreso/', views.despliegue_progreso_partial, name='despliegue_progreso_partial'),
    path('despliegues/<int:pk>/aprobar/', views.despliegue_aprobar, name='despliegue_aprobar'),
    path('despliegues/<int:pk>/publicar/', views.despliegue_publicar, name='despliegue_publicar'),
    path('despliegues/<int:pk>/pausar/', views.despliegue_pausar, name='despliegue_pausar'),
    path('despliegues/<int:pk>/reanudar/', views.despliegue_reanudar, name='despliegue_reanudar'),

    path('auditoria/', views.auditoria_lista, name='auditoria_lista'),
]
