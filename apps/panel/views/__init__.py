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
)
from .auditoria import auditoria_lista
from .dashboard import dashboard
from .despliegues import (
    despliegue_aprobar, despliegue_crear, despliegue_detalle, despliegue_pausar,
    despliegue_progreso_partial, despliegue_promover, despliegue_publicar, despliegue_reanudar,
    despliegues_lista,
)
from .estaciones import (
    estacion_aprobar, estacion_info_modal, estacion_info_solicitar, estacion_rechazar,
    estacion_reiniciar, estaciones_aprobar_lote, estaciones_lista, estaciones_pendientes_partial,
)
from .monitoreo import monitoreo_detalle, monitoreo_detalle_partial, monitoreo_lista
from .reportes import (
    reporte_auditoria_csv, reporte_cumplimiento_csv, reporte_despliegue_csv, reportes_index,
)

__all__ = [
    'activo_asignar', 'activo_baja', 'activo_consumible_entregar', 'activo_crear', 'activo_detalle',
    'activo_devolver', 'activo_reparacion_enviar', 'activo_reparacion_retorno', 'activos_lista',
    'auditoria_lista',
    'bodega_stock_ingresar', 'bodegas_lista',
    'colaborador_crear', 'colaboradores_lista',
    'dashboard',
    'despliegue_aprobar', 'despliegue_crear', 'despliegue_detalle', 'despliegue_pausar',
    'despliegue_progreso_partial', 'despliegue_promover', 'despliegue_publicar', 'despliegue_reanudar',
    'despliegues_lista',
    'estacion_aprobar', 'estacion_info_modal', 'estacion_info_solicitar', 'estacion_rechazar',
    'estacion_reiniciar', 'estaciones_aprobar_lote', 'estaciones_lista', 'estaciones_pendientes_partial',
    'monitoreo_detalle', 'monitoreo_detalle_partial', 'monitoreo_lista',
    'movimientos_inventario_lista',
    'orden_compra_crear', 'orden_compra_detalle', 'orden_compra_linea_crear',
    'orden_compra_linea_recibir', 'orden_compra_recibir', 'ordenes_compra_lista',
    'reporte_auditoria_csv', 'reporte_cumplimiento_csv', 'reporte_despliegue_csv', 'reportes_index',
]
