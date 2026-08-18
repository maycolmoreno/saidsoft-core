"""Vistas del panel HTMX, divididas por dominio de negocio.

Este paquete reemplaza al antiguo módulo único `views.py` (crecía sin límite
a medida que se agregaban módulos de negocio). Cada submódulo cubre un
dominio; `apps/panel/urls.py` sigue haciendo `from . import views` y
llamando `views.nombre_funcion`, por lo que todo lo público de cada
submódulo se re-exporta aquí.
"""

from .activos import (
    activo_asignar, activo_baja, activo_consumible_entregar, activo_crear, activo_detalle,
    activo_devolver, activo_reparacion_enviar, activo_reparacion_retorno, activos_lista,
    bodega_stock_ingresar, bodegas_lista, colaborador_crear, colaboradores_lista,
    movimientos_inventario_lista, orden_compra_crear, orden_compra_detalle,
    orden_compra_linea_crear, orden_compra_linea_recibir, orden_compra_recibir, ordenes_compra_lista,
    visita_tecnica_lista,
)
from .alertas import (
    alerta_reconocer, alerta_resolver, alertas_lista, pos_errores_flota, regla_alerta_crear, reglas_alerta_lista,
)
from .auditoria import auditoria_lista
from .cumplimiento import (
    cumplimiento_crear, cumplimiento_detalle, cumplimiento_lista, cumplimiento_resultado_completar,
)
from .dashboard import dashboard
from .despliegues import (
    despliegue_aprobar, despliegue_crear, despliegue_detalle, despliegue_pausar,
    despliegue_progreso_partial, despliegue_promover, despliegue_publicar, despliegue_reanudar,
    despliegues_lista,
)
from .estaciones import (
    estacion_aprobar, estacion_bitlocker_ver_clave, estacion_info_modal, estacion_info_solicitar,
    estacion_meshcentral_escritorio, estacion_meshcentral_terminal, estacion_meshcentral_vincular,
    estacion_rechazar, estacion_reiniciar, estacion_software_instalado_solicitar, estacion_supervision_grabaciones,
    estacion_windows_update_solicitar, estaciones_aprobar_lote, estaciones_lista, estaciones_pendientes_partial,
)
from .mantenimiento import (
    actividad_planificada_completar, actividad_planificada_crear, actividades_planificadas_lista,
    mantenimiento_cancelar, mantenimiento_cerrar, mantenimiento_checklist_actualizar, mantenimiento_crear,
    mantenimiento_detalle, mantenimiento_firmar, mantenimiento_generar_informe_pdf, mantenimiento_iniciar,
    mantenimiento_imagen_adjuntar, mantenimiento_orden_trabajo, mantenimiento_programado_crear,
    mantenimiento_repuesto_agregar, mantenimientos_lista, mantenimientos_programados_lista,
    notificacion_marcar_leida, notificaciones_lista,
)
from .monitoreo import (
    monitoreo_detalle, monitoreo_detalle_partial, monitoreo_lista, ventana_mantenimiento_crear,
    ventanas_mantenimiento_lista,
)
from .reportes import (
    reporte_activos_csv, reporte_alertas_csv, reporte_auditoria_csv, reporte_cliente_resumen,
    reporte_cumplimiento_csv, reporte_despliegue_csv, reporte_facturacion_csv, reporte_software_instalado_csv,
    reportes_index,
)
from .scripts import (
    ejecucion_detalle, ejecucion_progreso_partial, ejecuciones_lista, script_crear, script_detalle,
    script_ejecutar, script_ejecutar_adhoc, script_programado_crear, scripts_lista, scripts_programados_lista,
)
from .software import (
    aplicacion_crear, aplicacion_detalle, aplicaciones_lista, solicitud_instalacion_crear,
    solicitud_instalacion_detalle, solicitud_instalacion_progreso_partial, solicitud_instalacion_publicar,
    solicitudes_instalacion_lista, version_crear,
)
from .tenant import unidad_negocio_activar

__all__ = [
    'activo_asignar', 'activo_baja', 'activo_consumible_entregar', 'activo_crear', 'activo_detalle',
    'activo_devolver', 'activo_reparacion_enviar', 'activo_reparacion_retorno', 'activos_lista',
    'alerta_reconocer', 'alerta_resolver', 'alertas_lista', 'pos_errores_flota', 'regla_alerta_crear',
    'reglas_alerta_lista',
    'auditoria_lista',
    'bodega_stock_ingresar', 'bodegas_lista',
    'colaborador_crear', 'colaboradores_lista',
    'cumplimiento_crear', 'cumplimiento_detalle', 'cumplimiento_lista', 'cumplimiento_resultado_completar',
    'dashboard',
    'despliegue_aprobar', 'despliegue_crear', 'despliegue_detalle', 'despliegue_pausar',
    'despliegue_progreso_partial', 'despliegue_promover', 'despliegue_publicar', 'despliegue_reanudar',
    'despliegues_lista',
    'estacion_aprobar', 'estacion_info_modal', 'estacion_info_solicitar',
    'estacion_bitlocker_ver_clave',
    'estacion_meshcentral_escritorio', 'estacion_meshcentral_terminal', 'estacion_meshcentral_vincular',
    'estacion_rechazar', 'estacion_supervision_grabaciones',
    'estacion_reiniciar', 'estacion_windows_update_solicitar', 'estacion_software_instalado_solicitar',
    'estaciones_aprobar_lote', 'estaciones_lista', 'estaciones_pendientes_partial',
    'actividad_planificada_completar', 'actividad_planificada_crear', 'actividades_planificadas_lista',
    'mantenimiento_cancelar', 'mantenimiento_cerrar', 'mantenimiento_checklist_actualizar',
    'mantenimiento_crear', 'mantenimiento_detalle', 'mantenimiento_firmar', 'mantenimiento_generar_informe_pdf',
    'mantenimiento_iniciar', 'mantenimiento_imagen_adjuntar', 'mantenimiento_orden_trabajo',
    'mantenimiento_programado_crear', 'mantenimiento_repuesto_agregar', 'mantenimientos_lista',
    'mantenimientos_programados_lista', 'notificacion_marcar_leida', 'notificaciones_lista',
    'monitoreo_detalle', 'monitoreo_detalle_partial', 'monitoreo_lista',
    'ventana_mantenimiento_crear', 'ventanas_mantenimiento_lista',
    'movimientos_inventario_lista',
    'orden_compra_crear', 'orden_compra_detalle', 'orden_compra_linea_crear',
    'orden_compra_linea_recibir', 'orden_compra_recibir', 'ordenes_compra_lista',
    'reporte_activos_csv', 'reporte_alertas_csv', 'reporte_auditoria_csv', 'reporte_cliente_resumen',
    'reporte_cumplimiento_csv', 'reporte_despliegue_csv', 'reporte_facturacion_csv', 'reporte_software_instalado_csv',
    'reportes_index',
    'ejecucion_detalle', 'ejecucion_progreso_partial', 'ejecuciones_lista', 'script_crear',
    'script_detalle', 'script_ejecutar', 'script_ejecutar_adhoc', 'script_programado_crear',
    'scripts_lista', 'scripts_programados_lista',
    'aplicacion_crear', 'aplicacion_detalle', 'aplicaciones_lista', 'solicitud_instalacion_crear',
    'solicitud_instalacion_detalle', 'solicitud_instalacion_progreso_partial', 'solicitud_instalacion_publicar',
    'solicitudes_instalacion_lista', 'version_crear',
    'unidad_negocio_activar',
    'visita_tecnica_lista',
]
