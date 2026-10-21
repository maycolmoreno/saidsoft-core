import io

from django import forms
from django.contrib import admin, messages
from django.core.exceptions import PermissionDenied
from django.shortcuts import redirect
from django.template.response import TemplateResponse
from django.urls import path, reverse

from apps.auditoria.models import registrar_evento
from apps.cuentas.services import scope_por_unidad_negocio

from .models import Estacion, Farmacia, Grupo, OrdenAgente, UnidadNegocio, VersionAgente
from .services import asignar_password_nodo, establecer_password_nodo, importar_farmacias_desde_csv


@admin.register(UnidadNegocio)
class UnidadNegocioAdmin(admin.ModelAdmin):
    list_display = ('codigo', 'nombre', 'activo', 'total_farmacias')
    search_fields = ('codigo', 'nombre')
    list_filter = ('activo',)

    @admin.display(description='Farmacias')
    def total_farmacias(self, obj):
        return obj.farmacias.count()


class GrupoAdminForm(forms.ModelForm):
    """La contraseña del nodo se carga acá pero NUNCA se muestra de vuelta: el campo
    arranca siempre vacío y solo se guarda si se escribe algo (dejarlo en blanco
    conserva la que ya estaba). Así el token cifrado no viaja al navegador ni queda
    en el HTML del formulario."""

    pos_password = forms.CharField(
        label='Contraseña del nodo', required=False, widget=forms.PasswordInput(render_value=False),
        help_text='Contraseña de la base de datos de este nodo (va al Contrasena del .Config del POS). '
                  'Dejar vacío para conservar la actual. Se guarda cifrada.',
    )

    class Meta:
        model = Grupo
        fields = ('codigo', 'nombre', 'version_objetivo', 'pos_servidor', 'pos_puerto', 'pos_bdd', 'activo')

    def save(self, commit=True):
        grupo = super().save(commit=commit)
        nueva = self.cleaned_data.get('pos_password')
        if nueva:
            if commit:
                # establecer_password_nodo ya hace su propio save(update_fields=...).
                establecer_password_nodo(grupo, nueva)
            else:
                # El admin llama save(commit=False) y recién guarda en save_model(). En
                # un alta el grupo todavía no tiene pk, así que escribir acá tira
                # ValueError: solo dejamos el token cifrado en la instancia y ese save()
                # posterior lo persiste.
                asignar_password_nodo(grupo, nueva)
        return grupo


@admin.register(Grupo)
class GrupoAdmin(admin.ModelAdmin):
    form = GrupoAdminForm
    list_display = (
        'codigo', 'nombre', 'version_objetivo', 'bdd_pos', 'pos_servidor', 'pos_puerto',
        'tiene_password', 'activo', 'total_farmacias',
    )
    search_fields = ('codigo', 'nombre')
    list_filter = ('activo',)

    @admin.display(description='Farmacias')
    def total_farmacias(self, obj):
        return obj.farmacias.count()

    @admin.display(description='Contraseña', boolean=True)
    def tiene_password(self, obj):
        # Solo si está cargada o no — el valor nunca se muestra.
        return bool(obj.pos_password_cifrada)


@admin.register(Farmacia)
class FarmaciaAdmin(admin.ModelAdmin):
    list_display = (
        'codigo', 'nombre', 'grupo', 'unidad_negocio', 'ubicacion', 'tipo_enlace',
        'tiene_backup', 'ip_router', 'activa', 'fecha_apertura', 'total_estaciones',
    )
    search_fields = ('codigo', 'nombre', 'segmento_red', 'ip_router')
    list_filter = ('grupo', 'unidad_negocio', 'activa', 'tipo_enlace', 'tiene_backup')
    autocomplete_fields = ('grupo', 'unidad_negocio')
    change_list_template = 'admin/catalogo/farmacia/change_list.html'

    def get_queryset(self, request):
        return scope_por_unidad_negocio(super().get_queryset(request), request.user, 'unidad_negocio')

    def save_model(self, request, obj, form, change):
        """Al destildar `activa`, limpia también el rastro del monitoreo de enlace.

        Sin esto, `activa=False` frena el sondeo pero deja viva la última caída: el
        evento sigue con `fin=None` y el estado con `alcanzable=False`, y como ya nadie
        sondea el sitio, nada lo recupera nunca. El operador destilda la casilla y la
        farmacia le sigue apareciendo caída para siempre — pasó con GP063 el
        2-oct-2026, que es por lo que esto existe.

        Va en el admin y no en una señal porque el admin es el único lugar que escribe
        `activa` (los importadores crean, no dan de baja) y este proyecto no usa señales
        en ninguna app: esconder un efecto así detrás de un `post_save` sería el primer
        caso, y hace mucho más difícil de encontrar por qué se cerró una caída. Si algún
        día otra vía da de baja farmacias, el punto a llamar es
        `apps.monitoreo.enlaces.dar_de_baja_enlace`, que es idempotente.
        """
        from apps.monitoreo.enlaces import dar_de_baja_enlace

        super().save_model(request, obj, form, change)
        if not obj.activa:
            resultado = dar_de_baja_enlace(obj)
            if resultado['caidas_cerradas']:
                messages.info(
                    request,
                    f'{obj.codigo} quedó dada de baja: se cerró su caída de enlace en curso y '
                    'dejará de contar como caída y de generar avisos.',
                )

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
    search_fields = ('codigo', 'numero_serie', 'hostname', 'mac_lan')
    list_filter = ('estado_aprobacion', 'estado_conexion', 'farmacia__grupo', 'es_cache_farmacia', 'monitorear_recursos')
    list_editable = ('es_cache_farmacia', 'monitorear_recursos')
    autocomplete_fields = ('farmacia',)
    readonly_fields = ('token_enrolamiento', 'ip_lan', 'mac_lan', 'puerto_cache', 'ultimo_heartbeat', 'fecha_creacion')
    actions = ['activar_monitoreo_recursos', 'desactivar_monitoreo_recursos']

    def get_queryset(self, request):
        return scope_por_unidad_negocio(super().get_queryset(request), request.user, 'farmacia__unidad_negocio')

    @admin.display(description='¿Desactualizada?', boolean=True)
    def desactualizada_badge(self, obj):
        return obj.desactualizada

    def _cambiar_monitoreo_recursos(self, request, queryset, *, activar):
        # list_editable ya permite tildar el flag fila por fila, pero para ~1.800
        # estaciones eso no escala — con list_filter por grupo/es_cache_farmacia se
        # puede filtrar y aplicar en lote a la selección completa.
        estaciones = list(queryset.exclude(monitorear_recursos=activar))
        for estacion in estaciones:
            registrar_evento(
                usuario=request.user,
                accion='estacion.monitoreo_activar' if activar else 'estacion.monitoreo_desactivar',
                objeto=estacion,
            )
        Estacion.objects.filter(pk__in=[e.pk for e in estaciones]).update(monitorear_recursos=activar)
        verbo = 'activado' if activar else 'desactivado'
        self.message_user(
            request,
            f'Monitoreo de recursos {verbo} en {len(estaciones)} estación(es). El agente lo aplica '
            'recién en su próximo re-enrolamiento (ver "Monitoreo de servidores" en README.md).',
        )

    @admin.action(description='Activar monitoreo de recursos (CPU/RAM/disco)')
    def activar_monitoreo_recursos(self, request, queryset):
        self._cambiar_monitoreo_recursos(request, queryset, activar=True)

    @admin.action(description='Desactivar monitoreo de recursos (CPU/RAM/disco)')
    def desactivar_monitoreo_recursos(self, request, queryset):
        self._cambiar_monitoreo_recursos(request, queryset, activar=False)

    def _revocar_en_el_broker(self, request, estaciones):
        """Da de baja en EMQX las credenciales de las estaciones que se están borrando.

        Borrar la fila dejaba el usuario huérfano en EMQX **para siempre** (`B-1` de la
        auditoría por agentes): una credencial que autentica contra el broker, con ACL
        sobre los tópicos de una estación que ya no existe y sobre los dos catálogos
        globales, y que nada en el sistema vuelve a nombrar.

        Va en el admin y no en una señal `post_delete` por el mismo criterio que
        `FarmaciaAdmin.save_model`: este proyecto no usa señales en ninguna app, y
        esconder un efecto así haría mucho más difícil entender después por qué una
        credencial dejó de funcionar. El admin es hoy la única vía que borra estaciones;
        si algún día hay otra, el punto a llamar es
        `apps.mqtt_worker.emqx_admin.revocar_credencial_estacion`, que es idempotente.
        """
        from apps.mqtt_worker.emqx_admin import revocar_credencial_estacion

        fallaron = []
        for estacion in estaciones:
            registrar_evento(usuario=request.user, accion='estacion.borrar', objeto=estacion)
            if not revocar_credencial_estacion(estacion):
                fallaron.append(estacion.codigo)
        if fallaron:
            # Importa decirlo: la fila se va igual (el borrado no se aborta por esto), así
            # que si no se avisa, queda una credencial viva que ya no tiene dueño en la BD
            # y nadie sabría que hay que ir a EMQX a mano.
            self.message_user(
                request,
                'No se pudo revocar en EMQX la credencial de: %s. Siguen autenticando contra el '
                'broker y hay que darlas de baja a mano.' % ', '.join(fallaron),
                level=messages.WARNING,
            )

    def delete_model(self, request, obj):
        self._revocar_en_el_broker(request, [obj])
        super().delete_model(request, obj)

    def delete_queryset(self, request, queryset):
        self._revocar_en_el_broker(request, list(queryset))
        super().delete_queryset(request, queryset)


@admin.register(VersionAgente)
class VersionAgenteAdmin(admin.ModelAdmin):
    list_display = ('version', 'tamanio_bytes', 'creado_por', 'fecha_creacion')
    readonly_fields = ('sha256', 'tamanio_bytes', 'creado_por', 'fecha_creacion')

    def save_model(self, request, obj, form, change):
        if not change:
            obj.creado_por = request.user
        super().save_model(request, obj, form, change)
        registrar_evento(
            usuario=request.user, accion='version_agente.crear' if not change else 'version_agente.editar',
            objeto=obj,
        )


class ResolverOrdenForm(forms.Form):
    resultado = forms.ChoiceField(
        label='Qué se verificó en la estación',
        choices=[('completada', 'Se aplicó / se ejecutó: completada'),
                 ('fallida', 'No se aplicó / no terminó bien: fallida'),
                 ('desconocido', 'No se pudo determinar: queda incierta (no cambia el resultado, sigue '
                                 'bloqueando reintentos)')],
        widget=forms.RadioSelect,
    )
    nota = forms.CharField(
        label='Nota (obligatoria)', min_length=10, max_length=1000,
        widget=forms.Textarea(attrs={'rows': 4, 'cols': 80}),
        help_text='Qué se miró y dónde: por ejemplo, "POS en 4.3.0 según el acceso remoto a la caja".',
    )


@admin.register(OrdenAgente)
class OrdenAgenteAdmin(admin.ModelAdmin):
    """Cola de órdenes a los agentes (fase 1): de solo lectura. Las órdenes las crean los
    servicios de software, despliegues y scripts; acá solo se miran, se cancelan las que el
    agente todavía no recibió y se resuelven a mano los casos inciertos."""

    list_display = (
        'creada_en', 'estacion', 'tipo', 'origen_id', 'estado', 'requiere_revision', 'entregas', 'ultimo_paso',
        'expira_en', 'resolver',
    )
    list_filter = ('estado', 'tipo', 'requiere_revision')
    search_fields = ('estacion__codigo', 'uid')
    readonly_fields = [f.name for f in OrdenAgente._meta.fields]
    actions = ['cancelar_ordenes']

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def get_queryset(self, request):
        return scope_por_unidad_negocio(
            super().get_queryset(request).select_related('estacion'), request.user, 'estacion__farmacia__unidad_negocio',
        )

    def get_urls(self):
        return [
            path('<int:object_id>/resolver/', self.admin_site.admin_view(self.resolver_view),
                 name='catalogo_ordenagente_resolver'),
        ] + super().get_urls()

    @admin.display(description='Resolver')
    def resolver(self, obj):
        from django.utils.html import format_html

        from . import ordenes

        if not ordenes.puede_resolverse(obj):
            return '—'
        return format_html('<a href="{}">Resolver…</a>', reverse('admin:catalogo_ordenagente_resolver', args=[obj.pk]))

    def resolver_view(self, request, object_id):
        """Cierre humano de UNA orden incierta (interrumpida, no entregable o marcada para
        revisión), después de verificar en la estación. Una a la vez y con nota: no hay
        resolución en lote, porque cada caso exige mirar la estación."""
        from django.db import transaction
        from django.http import Http404

        from . import ordenes

        if not request.user.has_perm('catalogo.resolver_ordenagente'):
            raise PermissionDenied
        # Por el queryset con alcance: una orden de otra unidad de negocio no existe acá.
        orden = self.get_queryset(request).filter(pk=object_id).first()
        if orden is None:
            raise Http404
        volver = reverse('admin:catalogo_ordenagente_change', args=[orden.pk])
        if not ordenes.puede_resolverse(orden):
            self.message_user(
                request, 'Esta orden no está incierta ni marcada para revisión: no se resuelve a mano.', messages.WARNING,
            )
            return redirect(volver)

        form = ResolverOrdenForm(request.POST or None)
        if request.method == 'POST' and form.is_valid():
            estado_anterior, motivo = orden.estado, orden.motivo_revision
            try:
                with transaction.atomic():
                    orden = ordenes.resolver_orden(
                        orden, resultado=form.cleaned_data['resultado'], usuario=request.user,
                        nota=form.cleaned_data['nota'],
                    )
                    registrar_evento(
                        usuario=request.user, accion='orden_agente.resolver', objeto=orden, request=request,
                        detalle={
                            'uid': str(orden.uid), 'decision': form.cleaned_data['resultado'],
                            'nota': form.cleaned_data['nota'].strip(), 'estado_anterior': estado_anterior,
                            'motivo_revision': motivo,
                        },
                    )
            except ordenes.TransicionInvalida as exc:
                # Otra persona la resolvió, o llegó un reporte del agente, mientras tanto.
                self.message_user(request, str(exc), messages.ERROR)
                return redirect(volver)
            if form.cleaned_data['resultado'] == 'desconocido':
                self.message_user(
                    request, 'Revisión registrada. La orden sigue incierta: su resultado no cambió y un reintento '
                             'sigue bloqueado hasta que se pueda determinar.', messages.WARNING,
                )
            else:
                self.message_user(
                    request, f'Orden resuelta como {orden.get_estado_display().lower()}, y el resultado de la '
                             f'operación actualizado. Ya se puede reintentar el origen.', messages.SUCCESS,
                )
            return redirect(volver)

        context = {
            **self.admin_site.each_context(request),
            'title': 'Resolver orden incierta',
            'opts': self.model._meta,
            'orden': orden,
            'form': form,
        }
        return TemplateResponse(request, 'admin/catalogo/ordenagente_resolver.html', context)

    @admin.action(description='Cancelar (solo las que el agente todavía no recibió)')
    def cancelar_ordenes(self, request, queryset):
        if not request.user.has_perm('catalogo.change_ordenagente'):
            raise PermissionDenied
        from . import ordenes

        canceladas, rechazos = 0, []
        for orden in queryset:
            try:
                ordenes.cancelar_orden(orden, usuario=request.user, motivo='desde el admin')
            except ordenes.TransicionInvalida as exc:
                rechazos.append(f'{orden.estacion.codigo} #{orden.origen_id}: {exc}')
                continue
            canceladas += 1
            registrar_evento(usuario=request.user, accion='orden_agente.cancelar', objeto=orden, request=request)
        if canceladas:
            self.message_user(request, f'{canceladas} orden(es) cancelada(s).', messages.SUCCESS)
        for rechazo in rechazos:
            self.message_user(request, rechazo, messages.WARNING)
