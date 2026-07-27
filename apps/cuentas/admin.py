from django.contrib import admin

from .models import PerfilUsuario


@admin.register(PerfilUsuario)
class PerfilUsuarioAdmin(admin.ModelAdmin):
    list_display = ('usuario', 'colaborador', 'fcm_token')
    search_fields = ('usuario__username', 'usuario__email')
    autocomplete_fields = ('usuario', 'colaborador')
