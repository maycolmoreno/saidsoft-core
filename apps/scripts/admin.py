from django.contrib import admin

from apps.cuentas.services import scope_por_unidad_negocio, scope_scripts_visibles

from .models import EjecucionScript, ResultadoEjecucionScript, Script, ScriptProgramado


@admin.register(Script)
class ScriptAdmin(admin.ModelAdmin):
    list_display = ('nombre', 'unidad_negocio', 'tipo', 'categoria', 'es_adhoc', 'activo', 'creado_por', 'fecha_modificacion')
    list_filter = ('unidad_negocio', 'tipo', 'es_adhoc', 'activo')
    search_fields = ('nombre', 'categoria')
    autocomplete_fields = ('unidad_negocio', 'creado_por')

    def get_queryset(self, request):
        return scope_scripts_visibles(super().get_queryset(request), request.user)


class ResultadoEjecucionScriptInline(admin.TabularInline):
    model = ResultadoEjecucionScript
    extra = 0
    readonly_fields = (
        'estacion', 'estado', 'exit_code', 'stdout', 'stderr', 'fecha_envio', 'fecha_inicio', 'fecha_fin',
    )

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(EjecucionScript)
class EjecucionScriptAdmin(admin.ModelAdmin):
    list_display = ('id', 'unidad_negocio', 'script', 'destino_tipo', 'estado', 'programado', 'creado_por', 'fecha_creacion')
    list_filter = ('unidad_negocio', 'destino_tipo', 'estado')
    search_fields = ('script__nombre',)
    autocomplete_fields = ('unidad_negocio', 'script', 'programado', 'grupos', 'farmacias', 'estaciones', 'creado_por')
    readonly_fields = ('contenido_snapshot', 'sha256', 'fecha_creacion')
    inlines = [ResultadoEjecucionScriptInline]

    def get_queryset(self, request):
        return scope_por_unidad_negocio(super().get_queryset(request), request.user, 'unidad_negocio')

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(ScriptProgramado)
class ScriptProgramadoAdmin(admin.ModelAdmin):
    list_display = (
        'script', 'unidad_negocio', 'destino_tipo', 'frecuencia_dias',
        'fecha_ultima_ejecucion', 'fecha_proxima_ejecucion', 'activo',
    )
    list_filter = ('unidad_negocio', 'destino_tipo', 'activo')
    search_fields = ('script__nombre',)
    autocomplete_fields = ('script', 'unidad_negocio', 'grupos', 'farmacias', 'estaciones', 'creado_por')
    readonly_fields = ('fecha_ultima_ejecucion', 'fecha_creacion')

    def get_queryset(self, request):
        return scope_por_unidad_negocio(super().get_queryset(request), request.user, 'unidad_negocio')

    def save_model(self, request, obj, form, change):
        if not change:
            obj.creado_por = request.user
        super().save_model(request, obj, form, change)
