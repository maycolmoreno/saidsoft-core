from django.contrib import admin

from apps.cuentas.services import scope_opcional_por_unidad_negocio, scope_por_unidad_negocio

from .models import (
    Alerta, CanalNotificacion, EstadoDispositivo, EventoMonitoreo, MuestraMetrica, PosErrorDetectado, ReglaAlerta,
    VentanaMantenimiento,
)


@admin.register(MuestraMetrica)
class MuestraMetricaAdmin(admin.ModelAdmin):
    list_display = (
        'estacion', 'timestamp', 'ram_usada_pct', 'cpu_carga_pct', 'disco_usado_pct',
        'temperatura_c', 'latencia_ms',
    )
    list_filter = ('estacion',)
    date_hierarchy = 'timestamp'
    readonly_fields = [f.name for f in MuestraMetrica._meta.fields]

    def get_queryset(self, request):
        return scope_por_unidad_negocio(
            super().get_queryset(request), request.user, 'estacion__farmacia__unidad_negocio',
        )

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(ReglaAlerta)
class ReglaAlertaAdmin(admin.ModelAdmin):
    list_display = ('nombre', 'metrica', 'operador', 'umbral', 'duracion_minutos', 'severidad', 'unidad_negocio', 'activo')
    list_filter = ('unidad_negocio', 'metrica', 'severidad', 'activo')
    search_fields = ('nombre',)
    autocomplete_fields = ('unidad_negocio', 'creado_por')

    def get_queryset(self, request):
        return scope_opcional_por_unidad_negocio(super().get_queryset(request), request.user, 'unidad_negocio')

    def save_model(self, request, obj, form, change):
        if not change:
            obj.creado_por = request.user
        super().save_model(request, obj, form, change)


@admin.register(CanalNotificacion)
class CanalNotificacionAdmin(admin.ModelAdmin):
    list_display = ('tipo', 'unidad_negocio', 'destino', 'activo')
    list_filter = ('unidad_negocio', 'tipo', 'activo')
    autocomplete_fields = ('unidad_negocio', 'creado_por')

    def get_queryset(self, request):
        return scope_opcional_por_unidad_negocio(super().get_queryset(request), request.user, 'unidad_negocio')

    def save_model(self, request, obj, form, change):
        if not change:
            obj.creado_por = request.user
        super().save_model(request, obj, form, change)


@admin.register(EstadoDispositivo)
class EstadoDispositivoAdmin(admin.ModelAdmin):
    list_display = ('estacion', 'fuente', 'en_linea', 'actualizado_en')
    list_filter = ('fuente', 'en_linea')
    search_fields = ('estacion__codigo',)
    readonly_fields = [f.name for f in EstadoDispositivo._meta.fields]

    def get_queryset(self, request):
        return scope_por_unidad_negocio(
            super().get_queryset(request), request.user, 'estacion__farmacia__unidad_negocio',
        )

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(EventoMonitoreo)
class EventoMonitoreoAdmin(admin.ModelAdmin):
    list_display = ('estacion', 'fuente', 'en_linea', 'timestamp')
    list_filter = ('fuente', 'en_linea')
    search_fields = ('estacion__codigo',)
    date_hierarchy = 'timestamp'
    readonly_fields = [f.name for f in EventoMonitoreo._meta.fields]

    def get_queryset(self, request):
        return scope_por_unidad_negocio(
            super().get_queryset(request), request.user, 'estacion__farmacia__unidad_negocio',
        )

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(PosErrorDetectado)
class PosErrorDetectadoAdmin(admin.ModelAdmin):
    list_display = ('estacion', 'nivel', 'mensaje', 'cantidad_total', 'primera_vez', 'ultima_vez')
    list_filter = ('nivel',)
    search_fields = ('estacion__codigo', 'mensaje')
    autocomplete_fields = ('estacion',)

    def get_queryset(self, request):
        return scope_por_unidad_negocio(
            super().get_queryset(request), request.user, 'estacion__farmacia__unidad_negocio',
        )

    def has_add_permission(self, request):
        # Solo lo escribe el worker MQTT (manejar_pos_errores) — es un reflejo de lo
        # que el agente detectó en el log del POS, no algo que se cree a mano.
        return False


@admin.register(VentanaMantenimiento)
class VentanaMantenimientoAdmin(admin.ModelAdmin):
    list_display = ('motivo', 'unidad_negocio', 'destino_tipo', 'desde', 'hasta', 'activo')
    list_filter = ('unidad_negocio', 'destino_tipo', 'activo')
    search_fields = ('motivo',)
    autocomplete_fields = ('unidad_negocio', 'grupos', 'farmacias', 'estaciones', 'creado_por')
    readonly_fields = ('fecha_creacion',)

    def get_queryset(self, request):
        return scope_por_unidad_negocio(super().get_queryset(request), request.user, 'unidad_negocio')

    def save_model(self, request, obj, form, change):
        if not change:
            obj.creado_por = request.user
        super().save_model(request, obj, form, change)


@admin.register(Alerta)
class AlertaAdmin(admin.ModelAdmin):
    list_display = ('regla', 'estacion', 'estado', 'valor_disparador', 'abierta_en', 'resuelta_en')
    list_filter = ('estado', 'regla')
    search_fields = ('estacion__codigo', 'regla__nombre')
    autocomplete_fields = ('regla', 'estacion', 'reconocida_por')
    readonly_fields = ('abierta_en',)

    def get_queryset(self, request):
        return scope_por_unidad_negocio(
            super().get_queryset(request), request.user, 'estacion__farmacia__unidad_negocio',
        )
