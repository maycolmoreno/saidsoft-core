from django.contrib import admin

from .models import Activo, Bodega, Colaborador, EventoActivo, OrdenCompra, StockBodega, TipoConsumible


@admin.register(Bodega)
class BodegaAdmin(admin.ModelAdmin):
    list_display = ('codigo', 'nombre', 'custodio', 'ubicacion', 'activa')
    search_fields = ('codigo', 'nombre')
    list_filter = ('activa',)


@admin.register(Colaborador)
class ColaboradorAdmin(admin.ModelAdmin):
    list_display = ('nombre', 'cedula', 'cargo', 'sucursal', 'zona', 'activo')
    search_fields = ('nombre', 'cedula')
    list_filter = ('activo', 'sucursal')


@admin.register(TipoConsumible)
class TipoConsumibleAdmin(admin.ModelAdmin):
    list_display = ('codigo', 'nombre')
    search_fields = ('codigo', 'nombre')


@admin.register(StockBodega)
class StockBodegaAdmin(admin.ModelAdmin):
    list_display = ('bodega', 'tipo_consumible', 'cantidad')
    list_filter = ('bodega',)
    autocomplete_fields = ('bodega', 'tipo_consumible')


@admin.register(OrdenCompra)
class OrdenCompraAdmin(admin.ModelAdmin):
    list_display = ('numero_oc', 'proveedor', 'fecha_emision', 'estado', 'recibido_por', 'total_activos')
    list_filter = ('estado',)
    search_fields = ('numero_oc', 'proveedor')
    autocomplete_fields = ('bodegas_destino',)

    @admin.display(description='Activos')
    def total_activos(self, obj):
        return obj.activos.count()


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
        'codigo', 'tipo', 'marca', 'modelo', 'estado', 'estado_fisico_actual',
        'bodega_actual', 'colaborador_actual',
    )
    list_filter = ('tipo', 'estado', 'bodega_actual')
    search_fields = ('codigo', 'numero_serie', 'marca', 'modelo')
    autocomplete_fields = ('orden_compra', 'bodega_actual', 'colaborador_actual')
    readonly_fields = ('codigo', 'fecha_creacion')
    inlines = [EventoActivoInline]

    def has_delete_permission(self, request, obj=None):
        return False
