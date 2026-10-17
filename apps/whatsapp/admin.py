from django.contrib import admin

from apps.whatsapp.models import ConversacionWhatsApp, MensajeWhatsApp, Novedad


@admin.register(Novedad)
class NovedadAdmin(admin.ModelAdmin):
    """Para consultar. Se atienden desde la bandeja del panel (Mesa de ayuda) o desde
    Telegram, que dejan rastro en la auditoría; acá no se editan."""

    list_display = ('pk', 'farmacia', 'motivo', 'causa', 'estado', 'creado_en', 'tomado_por')
    list_filter = ('estado', 'motivo', 'causa', 'origen')
    search_fields = ('farmacia__codigo', 'descripcion')
    date_hierarchy = 'creado_en'
    raw_id_fields = ('farmacia', 'evento_enlace', 'estacion', 'tomado_por')

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(MensajeWhatsApp)
class MensajeWhatsAppAdmin(admin.ModelAdmin):
    """El rastro de lo que entró y salió. Solo lectura."""

    list_display = ('creado_en', 'direccion', 'telefono', 'farmacia', 'novedad', 'error')
    list_filter = ('direccion',)
    search_fields = ('telefono', 'farmacia__codigo')
    date_hierarchy = 'creado_en'

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(ConversacionWhatsApp)
class ConversacionWhatsAppAdmin(admin.ModelAdmin):
    list_display = ('telefono', 'farmacia', 'paso', 'actualizado_en')

    def has_add_permission(self, request):
        return False
