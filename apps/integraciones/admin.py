from django.contrib import admin

from apps.cuentas.services import scope_opcional_por_unidad_negocio

from .models import EventoSyncExterno, SincronizacionExterna


class EventoSyncExternoInline(admin.TabularInline):
    model = EventoSyncExterno
    extra = 0
    readonly_fields = ('estado', 'detalle', 'respuesta', 'timestamp')
    can_delete = False

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(SincronizacionExterna)
class SincronizacionExternaAdmin(admin.ModelAdmin):
    """Solo lectura: SincronizacionExterna la crea/actualiza registrar_sync_pendiente()/
    ejecutar_sync() (apps.integraciones.services), nunca el panel de admin."""

    list_display = ('conector', 'direccion', 'modelo', 'objeto_repr', 'estado', 'intentos', 'unidad_negocio', 'actualizado_en')
    list_filter = ('conector', 'direccion', 'estado', 'modelo', 'unidad_negocio')
    search_fields = ('objeto_repr', 'objeto_id')
    readonly_fields = [f.name for f in SincronizacionExterna._meta.fields]
    inlines = [EventoSyncExternoInline]

    def get_queryset(self, request):
        return scope_opcional_por_unidad_negocio(super().get_queryset(request), request.user, 'unidad_negocio')

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
