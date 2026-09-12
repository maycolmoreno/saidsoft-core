from django.contrib import admin

from apps.cuentas.services import scope_opcional_por_unidad_negocio

from .models import (
    Activo, Bodega, Cargo, CategoriaEquipo, Colaborador, Departamento, EventoActivo,
    Marca, MovimientoInventario, OrdenCompra, OrdenCompraDetalle, RecepcionLote, StockBodega,
    SyncCambio, SyncEjecucion, TipoConsumible, Ubicacion,
)


@admin.register(CategoriaEquipo)
class CategoriaEquipoAdmin(admin.ModelAdmin):
    list_display = ('codigo', 'nombre')
    search_fields = ('codigo', 'nombre')


@admin.register(Marca)
class MarcaAdmin(admin.ModelAdmin):
    list_display = ('nombre',)
    search_fields = ('nombre',)


@admin.register(Departamento)
class DepartamentoAdmin(admin.ModelAdmin):
    list_display = ('nombre', 'tipo', 'activo')
    list_filter = ('tipo', 'activo')
    search_fields = ('nombre',)


@admin.register(Cargo)
class CargoAdmin(admin.ModelAdmin):
    list_display = ('nombre', 'departamento', 'activo')
    list_filter = ('departamento', 'activo')
    search_fields = ('nombre',)
    autocomplete_fields = ('departamento',)


@admin.register(Ubicacion)
class UbicacionAdmin(admin.ModelAdmin):
    list_display = ('nombre', 'agencia', 'ciudad', 'departamento', 'encargado', 'activo')
    list_filter = ('departamento', 'activo', 'ciudad')
    search_fields = ('nombre', 'agencia', 'ciudad')
    autocomplete_fields = ('departamento', 'encargado')


@admin.register(Bodega)
class BodegaAdmin(admin.ModelAdmin):
    list_display = ('codigo', 'nombre', 'custodio', 'ubicacion', 'unidad_negocio', 'activa')
    search_fields = ('codigo', 'nombre')
    list_filter = ('activa', 'unidad_negocio')
    autocomplete_fields = ('unidad_negocio',)

    def get_queryset(self, request):
        return scope_opcional_por_unidad_negocio(super().get_queryset(request), request.user, 'unidad_negocio')


@admin.register(Colaborador)
class ColaboradorAdmin(admin.ModelAdmin):
    list_display = ('nombre', 'cedula', 'cargo', 'unidad_negocio', 'ubicacion', 'sucursal', 'zona', 'activo')
    search_fields = ('nombre', 'cedula', 'correo')
    list_filter = ('activo', 'unidad_negocio', 'sucursal', 'ubicacion')
    autocomplete_fields = ('cargo', 'ubicacion', 'unidad_negocio')

    def get_queryset(self, request):
        return scope_opcional_por_unidad_negocio(super().get_queryset(request), request.user, 'unidad_negocio')


@admin.register(TipoConsumible)
class TipoConsumibleAdmin(admin.ModelAdmin):
    list_display = ('codigo', 'nombre', 'stock_minimo')
    search_fields = ('codigo', 'nombre')


@admin.register(StockBodega)
class StockBodegaAdmin(admin.ModelAdmin):
    list_display = ('bodega', 'tipo_consumible', 'cantidad')
    list_filter = ('bodega',)
    autocomplete_fields = ('bodega', 'tipo_consumible')


class OrdenCompraDetalleInline(admin.TabularInline):
    model = OrdenCompraDetalle
    extra = 0
    readonly_fields = ('cantidad_recibida', 'estado', 'version')
    autocomplete_fields = ('categoria', 'marca', 'tipo_consumible')


@admin.register(OrdenCompra)
class OrdenCompraAdmin(admin.ModelAdmin):
    list_display = (
        'numero_oc', 'proveedor', 'fecha_emision', 'unidad_negocio', 'estado', 'recibido_por', 'total_activos',
    )
    list_filter = ('estado', 'unidad_negocio')
    search_fields = ('numero_oc', 'proveedor')
    autocomplete_fields = ('unidad_negocio', 'bodegas_destino')
    readonly_fields = ('version',)
    inlines = [OrdenCompraDetalleInline]

    def get_queryset(self, request):
        return scope_opcional_por_unidad_negocio(super().get_queryset(request), request.user, 'unidad_negocio')

    @admin.display(description='Activos')
    def total_activos(self, obj):
        return obj.activos.count()


@admin.register(OrdenCompraDetalle)
class OrdenCompraDetalleAdmin(admin.ModelAdmin):
    list_display = ('orden_compra', 'tipo_item', 'descripcion', 'cantidad_solicitada', 'cantidad_recibida', 'estado')
    list_filter = ('tipo_item', 'estado')
    search_fields = ('orden_compra__numero_oc', 'descripcion', 'modelo')
    autocomplete_fields = ('orden_compra', 'categoria', 'marca', 'tipo_consumible')
    readonly_fields = ('cantidad_recibida', 'estado', 'version')


@admin.register(RecepcionLote)
class RecepcionLoteAdmin(admin.ModelAdmin):
    list_display = (
        'uuid', 'orden_compra', 'orden_compra_detalle', 'numero_lote', 'cantidad_recibida',
        'bodega_destino', 'estado', 'fecha_recepcion',
    )
    list_filter = ('estado', 'tipo_item', 'bodega_destino')
    search_fields = ('numero_lote', 'orden_compra__numero_oc')
    autocomplete_fields = ('orden_compra', 'orden_compra_detalle', 'bodega_destino', 'custodio_receptor')
    readonly_fields = ('uuid', 'fecha_recepcion')


@admin.register(MovimientoInventario)
class MovimientoInventarioAdmin(admin.ModelAdmin):
    list_display = (
        'tipo_movimiento', 'tipo_consumible', 'cantidad', 'bodega_origen', 'bodega_destino',
        'realizado_por', 'fecha_efectiva',
    )
    list_filter = ('tipo_movimiento', 'bodega_origen', 'bodega_destino')
    search_fields = ('tipo_consumible__nombre', 'motivo')
    autocomplete_fields = ('tipo_consumible', 'bodega_origen', 'bodega_destino', 'orden_compra', 'recepcion_lote')
    readonly_fields = ('fecha_efectiva',)


class EventoActivoInline(admin.TabularInline):
    model = EventoActivo
    extra = 0
    readonly_fields = ('tipo_evento', 'usuario', 'detalle', 'timestamp')
    can_delete = False

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(Activo)
class ActivoAdmin(admin.ModelAdmin):
    list_display = (
        'codigo', 'tipo', 'marca', 'categoria', 'modelo', 'estado', 'estado_fisico_actual',
        'farmacia', 'ubicacion_interna', 'ip', 'bodega_actual', 'colaborador_actual',
        'unidad_negocio', 'baja_recomendada',
    )
    list_filter = (
        'tipo', 'estado', 'ubicacion_interna', 'bodega_actual', 'unidad_negocio', 'baja_recomendada',
    )
    # `ip` queda afuera a propósito: en PostgreSQL es de tipo inet y un `icontains` sobre
    # ella revienta (mismo motivo por el que enlaces_farmacias_lista castea ip_router a
    # texto para poder buscarla). `mac` sí es texto y es lo que se lee de una etiqueta.
    search_fields = ('codigo', 'numero_serie', 'marca__nombre', 'modelo', 'codigo_sap', 'mac')
    autocomplete_fields = (
        'orden_compra', 'bodega_actual', 'colaborador_actual', 'farmacia', 'marca', 'categoria', 'unidad_negocio',
        'estacion',
    )
    readonly_fields = ('codigo', 'fecha_creacion', 'ip_reportada_por_el_agente')
    inlines = [EventoActivoInline]

    def get_queryset(self, request):
        return scope_opcional_por_unidad_negocio(super().get_queryset(request), request.user, 'unidad_negocio')

    def has_delete_permission(self, request, obj=None):
        return False

    @admin.display(description='IP (reportada por el agente)')
    def ip_reportada_por_el_agente(self, obj):
        """La IP que reporta el agente, de solo lectura.

        El admin es el ÚNICO lugar del sistema donde se puede editar un Activo existente
        —el panel solo tiene acciones de ciclo de vida—, así que es acá donde hay que
        impedir que alguien cargue a mano una IP que ya viene sola.
        """
        if obj is None or not obj.estacion_id:
            return '—'
        return obj.estacion.ip_lan or f'{obj.estacion.codigo}: todavía no la reportó'

    def get_readonly_fields(self, request, obj=None):
        """`ip` y `mac` quedan bloqueados cuando el activo tiene estación vinculada.

        Bloquear en vez de ocultar: que el campo siga a la vista, vacío y deshabilitado,
        le dice al operador que el dato existe pero viene de otro lado. Ocultarlo lo
        dejaría buscándolo.
        """
        campos = list(super().get_readonly_fields(request, obj))
        if obj is not None and obj.estacion_id:
            campos += ['ip', 'mac']
        return campos


class SyncCambioInline(admin.TabularInline):
    model = SyncCambio
    extra = 0
    readonly_fields = ('tipo', 'cedula', 'colaborador', 'detalle')
    can_delete = False

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(SyncEjecucion)
class SyncEjecucionAdmin(admin.ModelAdmin):
    list_display = (
        'origen', 'ejecutado_por', 'creados', 'actualizados', 'inactivados', 'reactivados',
        'advertencias', 'ejecutado_en',
    )
    list_filter = ('origen',)
    readonly_fields = (
        'origen', 'ejecutado_por', 'creados', 'actualizados', 'inactivados', 'reactivados',
        'sin_cambios', 'advertencias', 'ejecutado_en',
    )
    inlines = [SyncCambioInline]

    def has_add_permission(self, request):
        return False
