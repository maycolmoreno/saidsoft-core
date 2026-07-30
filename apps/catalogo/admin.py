from django.contrib import admin

from apps.cuentas.services import scope_por_unidad_negocio

from .models import Estacion, Farmacia, Grupo, UnidadNegocio


@admin.register(UnidadNegocio)
class UnidadNegocioAdmin(admin.ModelAdmin):
    list_display = ('codigo', 'nombre', 'activo', 'total_farmacias')
    search_fields = ('codigo', 'nombre')
    list_filter = ('activo',)

    @admin.display(description='Farmacias')
    def total_farmacias(self, obj):
        return obj.farmacias.count()


@admin.register(Grupo)
class GrupoAdmin(admin.ModelAdmin):
    list_display = ('codigo', 'nombre', 'version_objetivo', 'activo', 'total_farmacias')
    search_fields = ('codigo', 'nombre')
    list_filter = ('activo',)

    @admin.display(description='Farmacias')
    def total_farmacias(self, obj):
        return obj.farmacias.count()


@admin.register(Farmacia)
class FarmaciaAdmin(admin.ModelAdmin):
    list_display = (
        'codigo', 'nombre', 'grupo', 'unidad_negocio', 'ubicacion', 'activa',
        'fecha_apertura', 'total_estaciones',
    )
    search_fields = ('codigo', 'nombre')
    list_filter = ('grupo', 'unidad_negocio', 'activa')
    autocomplete_fields = ('grupo', 'unidad_negocio')

    def get_queryset(self, request):
        return scope_por_unidad_negocio(super().get_queryset(request), request.user, 'unidad_negocio')

    @admin.display(description='Estaciones')
    def total_estaciones(self, obj):
        return obj.estaciones.count()


@admin.register(Estacion)
class EstacionAdmin(admin.ModelAdmin):
    list_display = (
        'codigo', 'farmacia', 'estado_aprobacion', 'estado_conexion',
        'version_pos', 'desactualizada_badge', 'hostname', 'ip_lan',
        'es_cache_farmacia', 'monitorear_recursos', 'ultimo_heartbeat',
    )
    search_fields = ('codigo', 'numero_serie', 'hostname')
    list_filter = ('estado_aprobacion', 'estado_conexion', 'farmacia__grupo', 'es_cache_farmacia', 'monitorear_recursos')
    list_editable = ('es_cache_farmacia', 'monitorear_recursos')
    autocomplete_fields = ('farmacia',)
    readonly_fields = ('token_enrolamiento', 'ip_lan', 'puerto_cache', 'ultimo_heartbeat', 'fecha_creacion')

    def get_queryset(self, request):
        return scope_por_unidad_negocio(super().get_queryset(request), request.user, 'farmacia__unidad_negocio')

    @admin.display(description='¿Desactualizada?', boolean=True)
    def desactualizada_badge(self, obj):
        return obj.desactualizada
