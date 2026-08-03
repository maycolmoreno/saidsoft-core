from django.contrib import admin

from apps.auditoria.models import registrar_evento
from apps.cuentas.services import scope_por_unidad_negocio

from .models import (
    AplicacionCatalogo, EstadoSolicitud, EventoInstalacion, ResultadoInstalacion, SolicitudInstalacion,
    VersionAplicacion,
)
from .services import publicar_solicitud


class VersionAplicacionInline(admin.TabularInline):
    model = VersionAplicacion
    extra = 0
    readonly_fields = ('sha256', 'tamanio_bytes', 'fecha_publicacion')
    show_change_link = True


@admin.register(AplicacionCatalogo)
class AplicacionCatalogoAdmin(admin.ModelAdmin):
    list_display = ('nombre', 'fabricante', 'categoria', 'unidad_negocio', 'activo', 'creado_por')
    list_filter = ('unidad_negocio', 'activo', 'categoria')
    search_fields = ('nombre', 'fabricante')
    autocomplete_fields = ('unidad_negocio',)
    readonly_fields = ('creado_por', 'fecha_creacion')
    inlines = [VersionAplicacionInline]

    def get_queryset(self, request):
        # Reusa el mismo criterio "compartida o del tenant" que Script (unidad_negocio
        # vacía = visible para todos), no el más estricto de Despliegue.
        from apps.cuentas.services import scope_opcional_por_unidad_negocio
        return scope_opcional_por_unidad_negocio(super().get_queryset(request), request.user, 'unidad_negocio')

    def save_model(self, request, obj, form, change):
        if not change:
            obj.creado_por = request.user
        super().save_model(request, obj, form, change)
        registrar_evento(
            usuario=request.user, accion='aplicacion_catalogo.crear' if not change else 'aplicacion_catalogo.editar',
            objeto=obj,
        )


@admin.register(VersionAplicacion)
class VersionAplicacionAdmin(admin.ModelAdmin):
    list_display = ('aplicacion', 'version', 'tamanio_bytes', 'fecha_publicacion')
    search_fields = ('aplicacion__nombre', 'version')
    autocomplete_fields = ('aplicacion',)
    readonly_fields = ('sha256', 'tamanio_bytes', 'fecha_publicacion')


class ResultadoInstalacionInline(admin.TabularInline):
    model = ResultadoInstalacion
    extra = 0
    readonly_fields = ('estacion', 'estado', 'version_previa_detectada', 'version_instalada', 'fecha_actualizacion')
    can_delete = False
    show_change_link = True

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(SolicitudInstalacion)
class SolicitudInstalacionAdmin(admin.ModelAdmin):
    list_display = (
        'version_aplicacion', 'accion', 'unidad_negocio', 'destino_tipo', 'estado',
        'progreso', 'creado_por', 'fecha_creacion',
    )
    list_filter = ('unidad_negocio', 'estado', 'accion', 'destino_tipo')
    search_fields = ('version_aplicacion__aplicacion__nombre', 'version_aplicacion__version')
    autocomplete_fields = ('version_aplicacion', 'unidad_negocio', 'grupos', 'farmacias', 'estaciones')
    readonly_fields = ('creado_por', 'fecha_creacion', 'fecha_publicacion', 'estado')
    inlines = [ResultadoInstalacionInline]
    actions = ['publicar_solicitudes']

    def get_queryset(self, request):
        return scope_por_unidad_negocio(super().get_queryset(request), request.user, 'unidad_negocio')

    @admin.display(description='Progreso')
    def progreso(self, obj):
        total = obj.resultados.count()
        if not total:
            return '—'
        ok = obj.resultados.filter(estado=ResultadoInstalacion.Estado.INSTALADO).count()
        error = obj.resultados.filter(estado=ResultadoInstalacion.Estado.ERROR).count()
        return f'{ok}/{total} OK · {error} error'

    def save_model(self, request, obj, form, change):
        if not change:
            obj.creado_por = request.user
        super().save_model(request, obj, form, change)
        if not change:
            registrar_evento(usuario=request.user, accion='solicitud_instalacion.crear', objeto=obj)

    @admin.action(description='Publicar solicitudes (borrador)')
    def publicar_solicitudes(self, request, queryset):
        publicadas = 0
        fallidas = 0
        for solicitud in queryset.filter(estado=EstadoSolicitud.BORRADOR):
            resultado = publicar_solicitud(solicitud)
            registrar_evento(
                usuario=request.user, accion='solicitud_instalacion.publicar', objeto=solicitud,
                detalle={'estaciones_destino': resultado.total_estaciones, 'exitoso': resultado.exitoso},
            )
            if resultado.exitoso:
                publicadas += 1
            else:
                fallidas += 1
        if publicadas:
            self.message_user(request, f'{publicadas} solicitud(es) publicada(s) por MQTT.')
        if fallidas:
            self.message_user(
                request, f'{fallidas} solicitud(es) NO se pudieron publicar (falló el broker MQTT).',
                level='error',
            )


class EventoInstalacionInline(admin.TabularInline):
    model = EventoInstalacion
    extra = 0
    readonly_fields = ('paso', 'detalle', 'timestamp')
    can_delete = False

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(ResultadoInstalacion)
class ResultadoInstalacionAdmin(admin.ModelAdmin):
    list_display = ('solicitud', 'estacion', 'estado', 'version_previa_detectada', 'version_instalada', 'fecha_actualizacion')
    list_filter = ('estado',)
    search_fields = ('estacion__codigo', 'solicitud__version_aplicacion__aplicacion__nombre')
    autocomplete_fields = ('solicitud', 'estacion')
    inlines = [EventoInstalacionInline]

    def get_queryset(self, request):
        return scope_por_unidad_negocio(super().get_queryset(request), request.user, 'solicitud__unidad_negocio')
