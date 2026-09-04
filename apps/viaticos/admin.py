from django.contrib import admin

from apps.cuentas.services import scope_opcional_por_unidad_negocio

from .models import AlertaViatico, ColaboradorZona, ReporteViatico


@admin.register(ColaboradorZona)
class ColaboradorZonaAdmin(admin.ModelAdmin):
    list_display = ('colaborador', 'zona_cobertura', 'farmacias_count', 'activa')
    list_filter = ('activa', 'zona_cobertura')
    search_fields = ('colaborador__nombre', 'colaborador__cedula', 'zona_cobertura')
    autocomplete_fields = ('colaborador',)
    filter_horizontal = ('farmacias_asignadas',)

    def get_queryset(self, request):
        # Mismo scoping que el panel: Colaborador.unidad_negocio es opcional
        # (None = compartido), así que aplica el criterio "compartido o del tenant".
        return scope_opcional_por_unidad_negocio(
            super().get_queryset(request), request.user, 'colaborador__unidad_negocio',
        )

    @admin.display(description='Farmacias asignadas')
    def farmacias_count(self, obj):
        return obj.farmacias_asignadas.count()


class AlertaViaticoInline(admin.TabularInline):
    model = AlertaViatico
    extra = 0
    fields = ('tipo_alerta', 'detalle', 'resuelta')
    readonly_fields = ('tipo_alerta', 'detalle')
    can_delete = False

    def has_add_permission(self, request, obj=None):
        # Las alertas las produce el servicio de validación, no una persona: cargarlas
        # a mano rompería la idempotencia de `evaluar_alertas`.
        return False


@admin.register(ReporteViatico)
class ReporteViaticoAdmin(admin.ModelAdmin):
    list_display = ('fecha', 'colaborador', 'farmacia_visitada', 'rubro', 'monto', 'estado', 'alertas_abiertas_count')
    list_filter = ('estado', 'rubro', 'fecha')
    search_fields = ('colaborador__nombre', 'colaborador__cedula', 'farmacia_visitada__codigo', 'descripcion')
    date_hierarchy = 'fecha'
    autocomplete_fields = ('colaborador', 'farmacia_visitada')
    readonly_fields = ('revisado_por', 'revisado_en', 'fecha_registro')
    inlines = [AlertaViaticoInline]

    def get_queryset(self, request):
        return scope_opcional_por_unidad_negocio(
            super().get_queryset(request).select_related('colaborador', 'farmacia_visitada'),
            request.user, 'colaborador__unidad_negocio',
        )

    @admin.display(description='Alertas')
    def alertas_abiertas_count(self, obj):
        return obj.alertas.filter(resuelta=False).count()

    def save_model(self, request, obj, form, change):
        """Un reporte cargado desde el admin corre las mismas reglas que uno cargado
        por el técnico: si no, el admin sería la puerta para saltarse el control."""
        super().save_model(request, obj, form, change)
        from . import services
        services.evaluar_alertas(obj)
