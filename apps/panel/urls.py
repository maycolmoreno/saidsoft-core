from django.contrib.auth import views as auth_views
from django.urls import path

from . import views

app_name = 'panel'

urlpatterns = [
    path('login/', auth_views.LoginView.as_view(template_name='panel/login.html'), name='login'),
    path('logout/', auth_views.LogoutView.as_view(), name='logout'),

    path('', views.dashboard, name='dashboard'),
    path('unidad-negocio/activar/', views.unidad_negocio_activar, name='unidad_negocio_activar'),

    path('estaciones/', views.estaciones_lista, name='estaciones_lista'),
    path('estaciones/pendientes/', views.estaciones_pendientes_partial, name='estaciones_pendientes_partial'),
    path('estaciones/aprobar-lote/', views.estaciones_aprobar_lote, name='estaciones_aprobar_lote'),
    path('estaciones/<int:pk>/aprobar/', views.estacion_aprobar, name='estacion_aprobar'),
    path('estaciones/<int:pk>/rechazar/', views.estacion_rechazar, name='estacion_rechazar'),
    path('estaciones/<int:pk>/reiniciar/', views.estacion_reiniciar, name='estacion_reiniciar'),
    path('estaciones/<int:pk>/info/', views.estacion_info_modal, name='estacion_info_modal'),
    path('estaciones/<int:pk>/info/solicitar/', views.estacion_info_solicitar, name='estacion_info_solicitar'),
    path('estaciones/<int:pk>/meshcentral/vincular/', views.estacion_meshcentral_vincular, name='estacion_meshcentral_vincular'),
    path('estaciones/<int:pk>/meshcentral/escritorio/', views.estacion_meshcentral_escritorio, name='estacion_meshcentral_escritorio'),
    path('estaciones/<int:pk>/meshcentral/terminal/', views.estacion_meshcentral_terminal, name='estacion_meshcentral_terminal'),
    path('estaciones/<int:pk>/meshcentral/grabaciones/', views.estacion_supervision_grabaciones, name='estacion_supervision_grabaciones'),
    path('estaciones/<int:pk>/bitlocker/clave/', views.estacion_bitlocker_ver_clave, name='estacion_bitlocker_ver_clave'),

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

    path('alertas/', views.alertas_lista, name='alertas_lista'),
    path('alertas/<int:pk>/reconocer/', views.alerta_reconocer, name='alerta_reconocer'),
    path('alertas/<int:pk>/resolver/', views.alerta_resolver, name='alerta_resolver'),
    path('alertas/reglas/', views.reglas_alerta_lista, name='reglas_alerta_lista'),
    path('alertas/reglas/nueva/', views.regla_alerta_crear, name='regla_alerta_crear'),

    path('reportes/', views.reportes_index, name='reportes_index'),
    path('reportes/cliente/', views.reporte_cliente_resumen, name='reporte_cliente_resumen'),
    path('reportes/cumplimiento.csv', views.reporte_cumplimiento_csv, name='reporte_cumplimiento_csv'),
    path('reportes/despliegue/<int:pk>.csv', views.reporte_despliegue_csv, name='reporte_despliegue_csv'),
    path('reportes/auditoria.csv', views.reporte_auditoria_csv, name='reporte_auditoria_csv'),
    path('reportes/activos.csv', views.reporte_activos_csv, name='reporte_activos_csv'),
    path('reportes/alertas.csv', views.reporte_alertas_csv, name='reporte_alertas_csv'),

    # Módulo de Activos
    path('colaboradores/', views.colaboradores_lista, name='colaboradores_lista'),
    path('colaboradores/nuevo/', views.colaborador_crear, name='colaborador_crear'),
    path('visita-tecnica/', views.visita_tecnica_lista, name='visita_tecnica_lista'),

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

    # Módulo de Mantenimiento
    path('mantenimientos/', views.mantenimientos_lista, name='mantenimientos_lista'),
    path('mantenimientos/nuevo/', views.mantenimiento_crear, name='mantenimiento_crear'),
    path('mantenimientos/<int:pk>/', views.mantenimiento_detalle, name='mantenimiento_detalle'),
    path('mantenimientos/<int:pk>/iniciar/', views.mantenimiento_iniciar, name='mantenimiento_iniciar'),
    path(
        'mantenimientos/<int:pk>/checklist/', views.mantenimiento_checklist_actualizar,
        name='mantenimiento_checklist_actualizar',
    ),
    path('mantenimientos/<int:pk>/cerrar/', views.mantenimiento_cerrar, name='mantenimiento_cerrar'),
    path('mantenimientos/<int:pk>/cancelar/', views.mantenimiento_cancelar, name='mantenimiento_cancelar'),
    path('mantenimientos/<int:pk>/firmar/', views.mantenimiento_firmar, name='mantenimiento_firmar'),
    path(
        'mantenimientos/<int:pk>/imagenes/nueva/', views.mantenimiento_imagen_adjuntar,
        name='mantenimiento_imagen_adjuntar',
    ),
    path(
        'mantenimientos/<int:pk>/repuestos/nuevo/', views.mantenimiento_repuesto_agregar,
        name='mantenimiento_repuesto_agregar',
    ),
    path(
        'mantenimientos/<int:pk>/orden-trabajo/', views.mantenimiento_orden_trabajo,
        name='mantenimiento_orden_trabajo',
    ),
    path(
        'mantenimientos/<int:pk>/informe-pdf/', views.mantenimiento_generar_informe_pdf,
        name='mantenimiento_generar_informe_pdf',
    ),
    path(
        'mantenimientos-programados/', views.mantenimientos_programados_lista,
        name='mantenimientos_programados_lista',
    ),
    path(
        'mantenimientos-programados/nuevo/', views.mantenimiento_programado_crear,
        name='mantenimiento_programado_crear',
    ),

    path('actividades-planificadas/', views.actividades_planificadas_lista, name='actividades_planificadas_lista'),
    path(
        'actividades-planificadas/nueva/', views.actividad_planificada_crear,
        name='actividad_planificada_crear',
    ),
    path(
        'actividades-planificadas/<int:pk>/completar/', views.actividad_planificada_completar,
        name='actividad_planificada_completar',
    ),

    path('notificaciones/', views.notificaciones_lista, name='notificaciones_lista'),
    path('notificaciones/<int:pk>/marcar-leida/', views.notificacion_marcar_leida, name='notificacion_marcar_leida'),

    # Módulo de Scripts (RMM)
    path('scripts/', views.scripts_lista, name='scripts_lista'),
    path('scripts/nuevo/', views.script_crear, name='script_crear'),
    path('scripts/ejecutar-adhoc/', views.script_ejecutar_adhoc, name='script_ejecutar_adhoc'),
    path('scripts/programados/', views.scripts_programados_lista, name='scripts_programados_lista'),
    path('scripts/programados/nuevo/', views.script_programado_crear, name='script_programado_crear'),
    path('scripts/<int:pk>/', views.script_detalle, name='script_detalle'),
    path('scripts/<int:pk>/ejecutar/', views.script_ejecutar, name='script_ejecutar'),
    path('ejecuciones/', views.ejecuciones_lista, name='ejecuciones_lista'),
    path('ejecuciones/<int:pk>/', views.ejecucion_detalle, name='ejecucion_detalle'),
    path('ejecuciones/<int:pk>/progreso/', views.ejecucion_progreso_partial, name='ejecucion_progreso_partial'),

    # Catálogo de software
    path('aplicaciones/', views.aplicaciones_lista, name='aplicaciones_lista'),
    path('aplicaciones/nueva/', views.aplicacion_crear, name='aplicacion_crear'),
    path('aplicaciones/<int:pk>/', views.aplicacion_detalle, name='aplicacion_detalle'),
    path('aplicaciones/<int:pk>/versiones/nueva/', views.version_crear, name='version_crear'),
    path('solicitudes-instalacion/', views.solicitudes_instalacion_lista, name='solicitudes_instalacion_lista'),
    path('solicitudes-instalacion/nueva/', views.solicitud_instalacion_crear, name='solicitud_instalacion_crear'),
    path('solicitudes-instalacion/<int:pk>/', views.solicitud_instalacion_detalle, name='solicitud_instalacion_detalle'),
    path(
        'solicitudes-instalacion/<int:pk>/progreso/', views.solicitud_instalacion_progreso_partial,
        name='solicitud_instalacion_progreso_partial',
    ),
    path(
        'solicitudes-instalacion/<int:pk>/publicar/', views.solicitud_instalacion_publicar,
        name='solicitud_instalacion_publicar',
    ),

    # Módulo de Planificación y Cumplimiento
    path('cumplimiento/', views.cumplimiento_lista, name='cumplimiento_lista'),
    path('cumplimiento/nueva/', views.cumplimiento_crear, name='cumplimiento_crear'),
    path('cumplimiento/<int:pk>/', views.cumplimiento_detalle, name='cumplimiento_detalle'),
    path(
        'cumplimiento/<int:pk>/resultados/<int:resultado_pk>/completar/',
        views.cumplimiento_resultado_completar, name='cumplimiento_resultado_completar',
    ),
]
