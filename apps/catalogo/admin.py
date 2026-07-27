from django.contrib import admin

from .models import Estacion, Farmacia, Grupo


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
    list_display = ('codigo', 'nombre', 'grupo', 'ubicacion', 'activa', 'total_estaciones')
    search_fields = ('codigo', 'nombre')
    list_filter = ('grupo', 'activa')
    autocomplete_fields = ('grupo',)

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

    @admin.display(description='¿Desactualizada?', boolean=True)
    def desactualizada_badge(self, obj):
        return obj.desactualizada
