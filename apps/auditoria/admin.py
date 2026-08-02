from django.contrib import admin

from .models import EventoAuditoria


@admin.register(EventoAuditoria)
class EventoAuditoriaAdmin(admin.ModelAdmin):
    list_display = ('timestamp', 'usuario', 'accion', 'modelo', 'objeto_repr', 'unidad_negocio', 'ip_address')
    list_filter = ('accion', 'modelo', 'unidad_negocio')
    search_fields = ('usuario__username', 'objeto_repr', 'objeto_id')
    readonly_fields = [f.name for f in EventoAuditoria._meta.fields]
    date_hierarchy = 'timestamp'

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
