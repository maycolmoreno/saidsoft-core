from django.contrib import admin

from .models import (
    ActividadChecklist, ActividadPlanificada, ActividadRealizada, EventoMantenimiento, FirmaMantenimiento,
    ImagenMantenimiento, Mantenimiento, MantenimientoEquipo, MantenimientoProgramado, Notificacion,
)


@admin.register(MantenimientoProgramado)
class MantenimientoProgramadoAdmin(admin.ModelAdmin):
    list_display = ('equipo', 'tecnico', 'frecuencia_dias', 'fecha_ultimo', 'fecha_proximo', 'activo')
    list_filter = ('activo',)
    search_fields = ('equipo__codigo',)
    autocomplete_fields = ('equipo', 'tecnico')


class MantenimientoEquipoInline(admin.TabularInline):
    model = MantenimientoEquipo
    extra = 0
    autocomplete_fields = ('equipo',)


class ActividadRealizadaInline(admin.TabularInline):
    model = ActividadRealizada
    extra = 0
    autocomplete_fields = ('actividad',)


class EventoMantenimientoInline(admin.TabularInline):
    model = EventoMantenimiento
    extra = 0
    readonly_fields = ('tipo_evento', 'usuario', 'detalle', 'timestamp')
    can_delete = False

    def has_add_permission(self, request, obj=None):
        return False


class FirmaMantenimientoInline(admin.TabularInline):
    model = FirmaMantenimiento
    extra = 0
    readonly_fields = ('tipo_firma', 'firma_base64', 'firmado_en', 'ip_origen', 'firmado_por')
    can_delete = False

    def has_add_permission(self, request, obj=None):
        return False


class ImagenMantenimientoInline(admin.TabularInline):
    model = ImagenMantenimiento
    extra = 0
    readonly_fields = ('nombre_archivo', 'tamanio_bytes', 'subido_en')


@admin.register(Mantenimiento)
class MantenimientoAdmin(admin.ModelAdmin):
    list_display = (
        'id', 'estado_interno', 'tipo_origen', 'cliente', 'tecnico', 'fecha_programada', 'resultado_tecnico',
    )
    list_filter = ('estado_interno', 'tipo_origen', 'resultado_tecnico')
    search_fields = ('descripcion', 'cliente__nombre')
    autocomplete_fields = ('cliente', 'tecnico', 'empresa', 'mantenimiento_programado', 'cerrado_por')
    readonly_fields = ('snapshot_equipo', 'fecha_creacion')
    inlines = [
        MantenimientoEquipoInline, ActividadRealizadaInline, FirmaMantenimientoInline,
        ImagenMantenimientoInline, EventoMantenimientoInline,
    ]

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(ActividadChecklist)
class ActividadChecklistAdmin(admin.ModelAdmin):
    list_display = ('nombre', 'orden', 'activo')
    list_filter = ('activo', 'categorias')
    search_fields = ('nombre',)
    autocomplete_fields = ('categorias',)


@admin.register(ActividadPlanificada)
class ActividadPlanificadaAdmin(admin.ModelAdmin):
    list_display = ('titulo', 'tecnico', 'prioridad', 'estado', 'fecha_inicio', 'fecha_fin')
    list_filter = ('estado', 'prioridad')
    search_fields = ('titulo', 'tecnico__username')
    autocomplete_fields = ('tecnico', 'creado_por', 'mantenimiento', 'mantenimiento_programado', 'equipo', 'ubicacion')


@admin.register(Notificacion)
class NotificacionAdmin(admin.ModelAdmin):
    list_display = ('usuario', 'mensaje', 'leida', 'creado_en')
    list_filter = ('leida',)
    search_fields = ('mensaje', 'usuario__username')
    autocomplete_fields = ('usuario', 'mantenimiento', 'actividad_planificada')
