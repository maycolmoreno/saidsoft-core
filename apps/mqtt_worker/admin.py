from django.contrib import admin

from .models import EnrolamientoRechazado, MensajeMqttFallido, WorkerHeartbeat


@admin.register(WorkerHeartbeat)
class WorkerHeartbeatAdmin(admin.ModelAdmin):
    list_display = ('nombre', 'ultimo_latido')

    def has_add_permission(self, request):
        return False


@admin.register(MensajeMqttFallido)
class MensajeMqttFallidoAdmin(admin.ModelAdmin):
    list_display = ('timestamp', 'topico', 'error', 'revisado')
    list_filter = ('revisado', 'topico')
    search_fields = ('topico', 'error', 'payload_crudo')
    actions = ['marcar_revisado']

    def has_add_permission(self, request):
        return False

    @admin.action(description='Marcar como revisado')
    def marcar_revisado(self, request, queryset):
        queryset.update(revisado=True)


@admin.register(EnrolamientoRechazado)
class EnrolamientoRechazadoAdmin(admin.ModelAdmin):
    """Mismo criterio que MensajeMqttFallido: bandeja de triage, no registro inmutable.

    La pantalla de verdad es la del panel (/estaciones/enrolamientos-rechazados/); esto
    es el acceso de administración, para borrar en masa lo ya resuelto.
    """
    list_display = ('codigo_recibido', 'hostname', 'intentos', 'ultimo_intento', 'revisado')
    list_filter = ('revisado',)
    search_fields = ('codigo_recibido', 'hostname')
