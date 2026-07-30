from django.contrib import admin

from apps.cuentas.services import scope_opcional_por_unidad_negocio

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

    def get_queryset(self, request):
        qs = scope_opcional_por_unidad_negocio(super().get_queryset(request), request.user, 'unidades_negocio')
        return qs.distinct()

    @admin.display(description='¿Vencida?', boolean=True)
    def vencida_badge(self, obj):
        return obj.vencida
