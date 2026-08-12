import io

from django.contrib import admin, messages
from django.core.exceptions import PermissionDenied
from django.shortcuts import redirect
from django.template.response import TemplateResponse
from django.urls import path, reverse

from apps.cuentas.services import scope_por_unidad_negocio

from .models import Estacion, Farmacia, Grupo, UnidadNegocio
from .services import importar_farmacias_desde_csv


@admin.register(UnidadNegocio)
class UnidadNegocioAdmin(admin.ModelAdmin):
    list_display = ('codigo', 'nombre', 'activo', 'total_farmacias')
    search_fields = ('codigo', 'nombre')
    list_filter = ('activo',)

    @admin.display(description='Farmacias')
    def total_farmacias(self, obj):
        return obj.farmacias.count()


@admin.register(Grupo)
class GrupoAdmin(admin.ModelAdmin):
    list_display = ('codigo', 'nombre', 'version_objetivo', 'activo', 'total_farmacias')
    search_fields = ('codigo', 'nombre')
    list_filter = ('activo',)

    @admin.display(description='Farmacias')
    def total_farmacias(self, obj):
        return obj.farmacias.count()


@admin.register(Farmacia)
class FarmaciaAdmin(admin.ModelAdmin):
    list_display = (
        'codigo', 'nombre', 'grupo', 'unidad_negocio', 'ubicacion', 'tipo_enlace',
        'tiene_backup', 'activa', 'fecha_apertura', 'total_estaciones',
    )
    search_fields = ('codigo', 'nombre', 'segmento_red')
    list_filter = ('grupo', 'unidad_negocio', 'activa', 'tipo_enlace', 'tiene_backup')
    autocomplete_fields = ('grupo', 'unidad_negocio')
    change_list_template = 'admin/catalogo/farmacia/change_list.html'

    def get_queryset(self, request):
        return scope_por_unidad_negocio(super().get_queryset(request), request.user, 'unidad_negocio')

    @admin.display(description='Estaciones')
    def total_estaciones(self, obj):
        return obj.estaciones.count()

    def get_urls(self):
        urls = [
            path('importar/', self.admin_site.admin_view(self.importar_view), name='catalogo_farmacia_importar'),
        ]
        return urls + super().get_urls()

    def importar_view(self, request):
        """Misma lógica que `python manage.py importar_farmacias`, para quien
        necesita cargar farmacias en lote sin acceso SSH al servidor."""
        if not self.has_add_permission(request):
            raise PermissionDenied

        resultado = None
        error = None
        if request.method == 'POST':
            dry_run = 'dry_run' in request.POST
            actualizar = 'actualizar' in request.POST
            archivo = request.FILES.get('archivo')
            if not archivo:
                error = 'Elegí un archivo CSV para subir.'
            else:
                try:
                    contenido = archivo.read().decode('utf-8-sig')
                except UnicodeDecodeError:
                    error = 'No se pudo leer el archivo como texto — ¿es un CSV válido?'
                else:
                    try:
                        resultado = importar_farmacias_desde_csv(
                            io.StringIO(contenido), dry_run=dry_run, actualizar=actualizar,
                        )
                    except ValueError as exc:
                        error = str(exc)
            if resultado and not dry_run and not error:
                total = len(resultado.creadas) + len(resultado.actualizadas)
                messages.success(request, f'Importación completa: {total} farmacia(s) creada(s)/actualizada(s).')
                return redirect(reverse('admin:catalogo_farmacia_changelist'))
        else:
            dry_run, actualizar = True, False

        context = {
            **self.admin_site.each_context(request),
            'title': 'Importar farmacias desde CSV',
            'opts': self.model._meta,
            'resultado': resultado,
            'error': error,
            'dry_run': dry_run,
            'actualizar': actualizar,
        }
        return TemplateResponse(request, 'admin/catalogo/farmacia_importar.html', context)


@admin.register(Estacion)
class EstacionAdmin(admin.ModelAdmin):
    list_display = (
        'codigo', 'farmacia', 'estado_aprobacion', 'estado_conexion',
        'version_pos', 'desactualizada_badge', 'hostname', 'ip_lan',
        'es_cache_farmacia', 'monitorear_recursos', 'ultimo_heartbeat',
    )
    search_fields = ('codigo', 'numero_serie', 'hostname')
    list_filter = ('estado_aprobacion', 'estado_conexion', 'farmacia__grupo', 'es_cache_farmacia', 'monitorear_recursos')
    list_editable = ('es_cache_farmacia', 'monitorear_recursos')
    autocomplete_fields = ('farmacia',)
    readonly_fields = ('token_enrolamiento', 'ip_lan', 'puerto_cache', 'ultimo_heartbeat', 'fecha_creacion')

    def get_queryset(self, request):
        return scope_por_unidad_negocio(super().get_queryset(request), request.user, 'farmacia__unidad_negocio')

    @admin.display(description='¿Desactualizada?', boolean=True)
    def desactualizada_badge(self, obj):
        return obj.desactualizada
