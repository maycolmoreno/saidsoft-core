from django.contrib import admin

from .models import (
    Apertura,
    EventoApertura,
    PasoApertura,
    PasoPlantilla,
    PerfilEstacionPlantilla,
    PlantillaApertura,
    TokenApertura,
)


class PerfilEstacionInline(admin.TabularInline):
    model = PerfilEstacionPlantilla
    extra = 0


class PasoPlantillaInline(admin.TabularInline):
    model = PasoPlantilla
    extra = 0


@admin.register(PlantillaApertura)
class PlantillaAperturaAdmin(admin.ModelAdmin):
    list_display = ('nombre', 'version', 'unidad_negocio', 'grupo', 'formato_farmacia', 'activa')
    list_filter = ('unidad_negocio', 'activa', 'formato_farmacia')
    search_fields = ('nombre',)
    inlines = [PerfilEstacionInline, PasoPlantillaInline]


class PasoAperturaInline(admin.TabularInline):
    model = PasoApertura
    extra = 0
    # Los pasos los materializa y actualiza apps.aperturas.services; editarlos a mano
    # desde el admin dejaría la apertura diciendo algo distinto de lo que pasó en el sitio.
    readonly_fields = ('paso_plantilla', 'estacion', 'orden', 'nombre', 'tipo', 'estado', 'detalle')
    can_delete = False


class TokenAperturaInline(admin.TabularInline):
    model = TokenApertura
    extra = 0
    # `token_hash` y `prefijo` son editable=False en el modelo; el resto se muestra pero no
    # se edita: un token se emite o se revoca desde el panel, no se toca acá.
    readonly_fields = ('perfil', 'prefijo', 'expira_en', 'usado_en', 'estacion', 'hardware_id')
    can_delete = False


@admin.register(Apertura)
class AperturaAdmin(admin.ModelAdmin):
    list_display = ('farmacia', 'plantilla', 'fecha_prevista', 'estado', 'creado_por', 'aprobado_por')
    list_filter = ('estado', 'farmacia__unidad_negocio')
    search_fields = ('farmacia__codigo', 'farmacia__nombre')
    date_hierarchy = 'fecha_prevista'
    inlines = [TokenAperturaInline, PasoAperturaInline]


@admin.register(EventoApertura)
class EventoAperturaAdmin(admin.ModelAdmin):
    list_display = ('paso', 'evento', 'timestamp')
    list_filter = ('evento',)
    readonly_fields = ('paso', 'evento', 'detalle', 'timestamp')

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        # EventoApertura.delete() lanza NotImplementedError; sin esto el admin ofrece un
        # botón de borrar que revienta con un 500 en vez de no estar.
        return False
