from django.contrib import admin

from .models import EjecucionScript, ResultadoEjecucionScript, Script


@admin.register(Script)
class ScriptAdmin(admin.ModelAdmin):
    list_display = ('nombre', 'tipo', 'categoria', 'es_adhoc', 'activo', 'creado_por', 'fecha_modificacion')
    list_filter = ('tipo', 'es_adhoc', 'activo')
    search_fields = ('nombre', 'categoria')
    autocomplete_fields = ('creado_por',)


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
    list_display = ('id', 'script', 'destino_tipo', 'estado', 'creado_por', 'fecha_creacion')
    list_filter = ('destino_tipo', 'estado')
    search_fields = ('script__nombre',)
    autocomplete_fields = ('script', 'grupos', 'farmacias', 'estaciones', 'creado_por')
    readonly_fields = ('contenido_snapshot', 'sha256', 'fecha_creacion')
    inlines = [ResultadoEjecucionScriptInline]

    def has_delete_permission(self, request, obj=None):
        return False
