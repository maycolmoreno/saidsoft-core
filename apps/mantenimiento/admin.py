from django.contrib import admin

from .models import (
    ActividadChecklist, ActividadRealizada, EventoMantenimiento, Mantenimiento, MantenimientoEquipo,
    MantenimientoProgramado,
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


@admin.register(Mantenimiento)
class MantenimientoAdmin(admin.ModelAdmin):
    list_display = (
        'id', 'estado_interno', 'tipo_origen', 'cliente', 'tecnico', 'fecha_programada', 'resultado_tecnico',
    )
    list_filter = ('estado_interno', 'tipo_origen', 'resultado_tecnico')
    search_fields = ('descripcion', 'cliente__nombre')
    autocomplete_fields = ('cliente', 'tecnico', 'empresa', 'mantenimiento_programado', 'cerrado_por')
    readonly_fields = ('snapshot_equipo', 'fecha_creacion')
    inlines = [MantenimientoEquipoInline, ActividadRealizadaInline, EventoMantenimientoInline]

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(ActividadChecklist)
class ActividadChecklistAdmin(admin.ModelAdmin):
    list_display = ('nombre', 'orden', 'activo')
    list_filter = ('activo', 'categorias')
    search_fields = ('nombre',)
    autocomplete_fields = ('categorias',)
