from django.contrib import admin

from .models import (
    ActividadCumplimiento, ResultadoCumplimientoColaborador, ResultadoCumplimientoEstacion,
    ResultadoCumplimientoFarmacia,
)


class ResultadoEstacionInline(admin.TabularInline):
    model = ResultadoCumplimientoEstacion
    extra = 0
    autocomplete_fields = ('estacion',)


class ResultadoFarmaciaInline(admin.TabularInline):
    model = ResultadoCumplimientoFarmacia
    extra = 0
    autocomplete_fields = ('farmacia',)


class ResultadoColaboradorInline(admin.TabularInline):
    model = ResultadoCumplimientoColaborador
    extra = 0
    autocomplete_fields = ('colaborador',)


@admin.register(ActividadCumplimiento)
class ActividadCumplimientoAdmin(admin.ModelAdmin):
    list_display = ('nombre', 'tipo_objetivo', 'fecha_limite', 'vencida_badge', 'creado_por', 'fecha_creacion')
    list_filter = ('tipo_objetivo', 'unidades_negocio')
    search_fields = ('nombre',)
    autocomplete_fields = ('unidades_negocio', 'cargos')
    inlines = [ResultadoEstacionInline, ResultadoFarmaciaInline, ResultadoColaboradorInline]

    @admin.display(description='¿Vencida?', boolean=True)
    def vencida_badge(self, obj):
        return obj.vencida
