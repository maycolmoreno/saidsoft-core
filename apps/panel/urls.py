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
    path('estaciones/aprobar-lote/', views.estaciones_aprobar_lote, name='estaciones_aprobar_lote'),
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
    path('despliegues/<int:pk>/promover/', views.despliegue_promover, name='despliegue_promover'),

    path('auditoria/', views.auditoria_lista, name='auditoria_lista'),

    path('monitoreo/', views.monitoreo_lista, name='monitoreo_lista'),
    path('monitoreo/<int:pk>/', views.monitoreo_detalle, name='monitoreo_detalle'),
    path('monitoreo/<int:pk>/datos/', views.monitoreo_detalle_partial, name='monitoreo_detalle_partial'),

    path('reportes/', views.reportes_index, name='reportes_index'),
    path('reportes/cumplimiento.csv', views.reporte_cumplimiento_csv, name='reporte_cumplimiento_csv'),
    path('reportes/despliegue/<int:pk>.csv', views.reporte_despliegue_csv, name='reporte_despliegue_csv'),
    path('reportes/auditoria.csv', views.reporte_auditoria_csv, name='reporte_auditoria_csv'),

    # Módulo de Activos
    path('colaboradores/', views.colaboradores_lista, name='colaboradores_lista'),
    path('colaboradores/nuevo/', views.colaborador_crear, name='colaborador_crear'),

    path('ordenes-compra/', views.ordenes_compra_lista, name='ordenes_compra_lista'),
    path('ordenes-compra/nueva/', views.orden_compra_crear, name='orden_compra_crear'),
    path('ordenes-compra/<int:pk>/', views.orden_compra_detalle, name='orden_compra_detalle'),
    path('ordenes-compra/<int:pk>/recibir/', views.orden_compra_recibir, name='orden_compra_recibir'),
    path('ordenes-compra/<int:pk>/lineas/nueva/', views.orden_compra_linea_crear, name='orden_compra_linea_crear'),
    path(
        'ordenes-compra/lineas/<int:pk>/recibir/', views.orden_compra_linea_recibir,
        name='orden_compra_linea_recibir',
    ),

    path('activos/', views.activos_lista, name='activos_lista'),
    path('activos/nuevo/', views.activo_crear, name='activo_crear'),
    path('activos/<int:pk>/', views.activo_detalle, name='activo_detalle'),
    path('activos/<int:pk>/asignar/', views.activo_asignar, name='activo_asignar'),
    path('activos/<int:pk>/devolver/', views.activo_devolver, name='activo_devolver'),
    path('activos/<int:pk>/reparacion/enviar/', views.activo_reparacion_enviar, name='activo_reparacion_enviar'),
    path('activos/<int:pk>/reparacion/retorno/', views.activo_reparacion_retorno, name='activo_reparacion_retorno'),
    path('activos/<int:pk>/baja/', views.activo_baja, name='activo_baja'),
    path('activos/<int:pk>/consumible/', views.activo_consumible_entregar, name='activo_consumible_entregar'),

    path('bodegas/', views.bodegas_lista, name='bodegas_lista'),
    path('bodegas/<int:pk>/stock/ingresar/', views.bodega_stock_ingresar, name='bodega_stock_ingresar'),

    path('movimientos-inventario/', views.movimientos_inventario_lista, name='movimientos_inventario_lista'),
]
