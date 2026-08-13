from django.contrib import admin

from apps.cuentas.services import scope_por_unidad_negocio

from .models import ActividadMensualEstacion


@admin.register(ActividadMensualEstacion)
class ActividadMensualEstacionAdmin(admin.ModelAdmin):
    list_display = ('estacion', 'anio', 'mes', 'primer_heartbeat_en')
    list_filter = ('anio', 'mes')
    search_fields = ('estacion__codigo',)
    readonly_fields = [f.name for f in ActividadMensualEstacion._meta.fields]

    def get_queryset(self, request):
        return scope_por_unidad_negocio(
            super().get_queryset(request), request.user, 'estacion__farmacia__unidad_negocio',
        )

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False
