from django.contrib import admin

from apps.auditoria.models import registrar_evento
from apps.cuentas.services import scope_por_unidad_negocio

from .models import Despliegue, EventoDespliegue, ResultadoDespliegue
from .services import publicar_despliegue


class ResultadoDespliegueInline(admin.TabularInline):
    model = ResultadoDespliegue
    extra = 0
    readonly_fields = ('estacion', 'estado', 'version_previa', 'version_nueva', 'fecha_actualizacion')
    can_delete = False
    show_change_link = True

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(Despliegue)
class DespliegueAdmin(admin.ModelAdmin):
    list_display = (
        'version', 'unidad_negocio', 'destino_tipo', 'modo_aplicacion', 'estado',
        'progreso', 'creado_por', 'fecha_creacion', 'fecha_publicacion',
    )
    list_filter = ('unidad_negocio', 'estado', 'destino_tipo', 'modo_aplicacion')
    search_fields = ('version', 'descripcion')
    autocomplete_fields = ('unidad_negocio', 'grupos', 'farmacias', 'estaciones')
    readonly_fields = ('sha256', 'creado_por', 'aprobado_por', 'fecha_creacion', 'fecha_publicacion', 'estado')
    inlines = [ResultadoDespliegueInline]
    actions = ['aprobar_despliegues', 'publicar_despliegues']

    def get_queryset(self, request):
        return scope_por_unidad_negocio(super().get_queryset(request), request.user, 'unidad_negocio')

    @admin.display(description='Progreso')
    def progreso(self, obj):
        total = obj.resultados.count()
        if not total:
            return '—'
        ok = obj.resultados.filter(estado=ResultadoDespliegue.Estado.APLICADO).count()
        error = obj.resultados.filter(estado=ResultadoDespliegue.Estado.ERROR).count()
        return f'{ok}/{total} OK · {error} error'

    def save_model(self, request, obj, form, change):
        if not change:
            obj.creado_por = request.user
        super().save_model(request, obj, form, change)
        registrar_evento(
            usuario=request.user,
            accion='despliegue.crear' if not change else 'despliegue.editar',
            objeto=obj,
        )

    @admin.action(description='Aprobar despliegues seleccionados (segundo par de ojos)')
    def aprobar_despliegues(self, request, queryset):
        aprobados = 0
        for despliegue in queryset.filter(estado=Despliegue.Estado.PENDIENTE_APROBACION):
            if despliegue.creado_por_id == request.user.id:
                self.message_user(
                    request,
                    f'{despliegue}: quien crea el despliegue no puede aprobarlo (regla de cuatro ojos).',
                    level='error',
                )
                continue
            despliegue.estado = Despliegue.Estado.APROBADO
            despliegue.aprobado_por = request.user
            despliegue.save(update_fields=['estado', 'aprobado_por'])
            registrar_evento(usuario=request.user, accion='despliegue.aprobar', objeto=despliegue)
            aprobados += 1
        if aprobados:
            self.message_user(request, f'{aprobados} despliegue(s) aprobado(s).')

    @admin.action(description='Publicar despliegues aprobados')
    def publicar_despliegues(self, request, queryset):
        publicados = 0
        fallidos = 0
        for despliegue in queryset.filter(estado=Despliegue.Estado.APROBADO):
            resultado = publicar_despliegue(despliegue)
            registrar_evento(
                usuario=request.user, accion='despliegue.publicar', objeto=despliegue,
                detalle={'estaciones_destino': resultado.total_estaciones, 'exitoso': resultado.exitoso},
            )
            if resultado.exitoso:
                publicados += 1
            else:
                fallidos += 1
        if publicados:
            self.message_user(request, f'{publicados} despliegue(s) publicado(s) por MQTT.')
        if fallidos:
            self.message_user(
                request, f'{fallidos} despliegue(s) NO se pudieron publicar (falló el broker MQTT).',
                level='error',
            )


class EventoDespliegueInline(admin.TabularInline):
    model = EventoDespliegue
    extra = 0
    readonly_fields = ('paso', 'detalle', 'timestamp')
    can_delete = False

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(ResultadoDespliegue)
class ResultadoDespliegueAdmin(admin.ModelAdmin):
    list_display = ('despliegue', 'estacion', 'estado', 'version_previa', 'version_nueva', 'fecha_actualizacion')
    list_filter = ('estado', 'despliegue')
    search_fields = ('estacion__codigo', 'despliegue__version')
    autocomplete_fields = ('despliegue', 'estacion')
    inlines = [EventoDespliegueInline]

    def get_queryset(self, request):
        return scope_por_unidad_negocio(super().get_queryset(request), request.user, 'despliegue__unidad_negocio')
