from django.contrib import admin

from .models import PerfilUsuario


@admin.register(PerfilUsuario)
class PerfilUsuarioAdmin(admin.ModelAdmin):
    list_display = ('usuario', 'colaborador', 'acceso_todas_unidades', 'telegram_chat_id')
    search_fields = ('usuario__username', 'usuario__email', 'telegram_chat_id')
    autocomplete_fields = ('usuario', 'colaborador')
    # `telegram_chat_id` en la lista y no solo en el detalle: es el campo que decide
    # quién puede accionar desde Telegram, así que quién lo tiene cargado tiene que
    # poder mirarse de un vistazo, sin abrir perfil por perfil.
    list_filter = ('acceso_todas_unidades',)
