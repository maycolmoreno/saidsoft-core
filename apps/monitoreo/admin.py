from django.contrib import admin

from apps.cuentas.services import scope_opcional_por_unidad_negocio, scope_por_unidad_negocio

from .models import (
    Alerta, CanalNotificacion, ConfiguracionMonitoreo, DispositivoDetectado, EquipoBordeFarmacia, EstadoDispositivo, EstadoEnlaceFarmacia, EstadoServicioPos, EventoEnlaceFarmacia, EventoMonitoreo, EventoSistemaDetectado, EventoSistemaVigilado, MuestraMetrica, MuestraRedFarmacia, PosErrorDetectado, ReglaAlerta, ServicioPosMonitoreado, VentanaMantenimiento,
)


@admin.register(MuestraMetrica)
class MuestraMetricaAdmin(admin.ModelAdmin):
    list_display = (
        'estacion', 'timestamp', 'ram_usada_pct', 'cpu_carga_pct', 'disco_usado_pct',
        'temperatura_c', 'latencia_ms', 'red_total_kbps',
    )
    list_filter = ('estacion',)
    date_hierarchy = 'timestamp'
    readonly_fields = [f.name for f in MuestraMetrica._meta.fields]

    def get_queryset(self, request):
        return scope_por_unidad_negocio(
            super().get_queryset(request), request.user, 'estacion__farmacia__unidad_negocio',
        )

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(MuestraRedFarmacia)
class MuestraRedFarmaciaAdmin(admin.ModelAdmin):
    list_display = ('farmacia', 'timestamp', 'red_recibido_kbps', 'red_enviado_kbps', 'bytes_recibidos', 'bytes_enviados')
    list_filter = ('farmacia',)
    date_hierarchy = 'timestamp'
    readonly_fields = [f.name for f in MuestraRedFarmacia._meta.fields]

    def get_queryset(self, request):
        return scope_por_unidad_negocio(
            super().get_queryset(request), request.user, 'farmacia__unidad_negocio',
        )

    def has_add_permission(self, request):
        # Solo lo escribe apps.monitoreo.mikrotik.sincronizar_ancho_banda_farmacias
        # (Celery Beat) — igual que SoftwareInstaladoDetectadoAdmin/
        # PosErrorDetectadoAdmin, no algo que se cree a mano.
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(ReglaAlerta)
class ReglaAlertaAdmin(admin.ModelAdmin):
    list_display = (
        'nombre', 'metrica', 'operador', 'umbral', 'duracion_minutos', 'severidad',
        'abre_mantenimiento', 'unidad_negocio', 'activo',
    )
    list_filter = ('unidad_negocio', 'metrica', 'severidad', 'abre_mantenimiento', 'activo')
    list_editable = ('abre_mantenimiento',)
    search_fields = ('nombre',)
    autocomplete_fields = ('unidad_negocio', 'creado_por')

    def get_queryset(self, request):
        return scope_opcional_por_unidad_negocio(super().get_queryset(request), request.user, 'unidad_negocio')

    def save_model(self, request, obj, form, change):
        if not change:
            obj.creado_por = request.user
        super().save_model(request, obj, form, change)


@admin.register(CanalNotificacion)
class CanalNotificacionAdmin(admin.ModelAdmin):
    list_display = ('tipo', 'unidad_negocio', 'destino', 'activo')
    list_filter = ('unidad_negocio', 'tipo', 'activo')
    autocomplete_fields = ('unidad_negocio', 'creado_por')

    def get_queryset(self, request):
        return scope_opcional_por_unidad_negocio(super().get_queryset(request), request.user, 'unidad_negocio')

    def save_model(self, request, obj, form, change):
        if not change:
            obj.creado_por = request.user
        super().save_model(request, obj, form, change)


@admin.register(EstadoDispositivo)
class EstadoDispositivoAdmin(admin.ModelAdmin):
    list_display = ('estacion', 'fuente', 'en_linea', 'actualizado_en')
    list_filter = ('fuente', 'en_linea')
    search_fields = ('estacion__codigo',)
    readonly_fields = [f.name for f in EstadoDispositivo._meta.fields]

    def get_queryset(self, request):
        return scope_por_unidad_negocio(
            super().get_queryset(request), request.user, 'estacion__farmacia__unidad_negocio',
        )

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(EventoMonitoreo)
class EventoMonitoreoAdmin(admin.ModelAdmin):
    list_display = ('estacion', 'fuente', 'en_linea', 'timestamp')
    list_filter = ('fuente', 'en_linea')
    search_fields = ('estacion__codigo',)
    date_hierarchy = 'timestamp'
    readonly_fields = [f.name for f in EventoMonitoreo._meta.fields]

    def get_queryset(self, request):
        return scope_por_unidad_negocio(
            super().get_queryset(request), request.user, 'estacion__farmacia__unidad_negocio',
        )

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(PosErrorDetectado)
class PosErrorDetectadoAdmin(admin.ModelAdmin):
    list_display = ('estacion', 'nivel', 'mensaje', 'cantidad_total', 'primera_vez', 'ultima_vez')
    list_filter = ('nivel',)
    search_fields = ('estacion__codigo', 'mensaje')
    autocomplete_fields = ('estacion',)

    def get_queryset(self, request):
        return scope_por_unidad_negocio(
            super().get_queryset(request), request.user, 'estacion__farmacia__unidad_negocio',
        )

    def has_add_permission(self, request):
        # Solo lo escribe el worker MQTT (manejar_pos_errores) — es un reflejo de lo
        # que el agente detectó en el log del POS, no algo que se cree a mano.
        return False


@admin.register(VentanaMantenimiento)
class VentanaMantenimientoAdmin(admin.ModelAdmin):
    list_display = ('motivo', 'unidad_negocio', 'destino_tipo', 'desde', 'hasta', 'activo')
    list_filter = ('unidad_negocio', 'destino_tipo', 'activo')
    search_fields = ('motivo',)
    autocomplete_fields = ('unidad_negocio', 'grupos', 'farmacias', 'estaciones', 'creado_por')
    readonly_fields = ('fecha_creacion',)

    def get_queryset(self, request):
        return scope_por_unidad_negocio(super().get_queryset(request), request.user, 'unidad_negocio')

    def save_model(self, request, obj, form, change):
        if not change:
            obj.creado_por = request.user
        super().save_model(request, obj, form, change)


@admin.register(Alerta)
class AlertaAdmin(admin.ModelAdmin):
    list_display = ('regla', 'estacion', 'estado', 'valor_disparador', 'abierta_en', 'resuelta_en')
    list_filter = ('estado', 'regla')
    search_fields = ('estacion__codigo', 'regla__nombre')
    autocomplete_fields = ('regla', 'estacion', 'reconocida_por')
    # El diagnóstico es de solo lectura: lo genera un modelo, no una persona. Editarlo a
    # mano dejaría un texto con pinta de automático que en realidad escribió alguien.
    readonly_fields = ('abierta_en', 'diagnostico_ia', 'diagnostico_generado_en')

    def get_queryset(self, request):
        return scope_por_unidad_negocio(
            super().get_queryset(request), request.user, 'estacion__farmacia__unidad_negocio',
        )


@admin.register(EstadoEnlaceFarmacia)
class EstadoEnlaceFarmaciaAdmin(admin.ModelAdmin):
    """Vista de respaldo mientras no exista la pantalla del panel. Solo lectura: estos
    campos los escribe `apps.monitoreo.enlaces.registrar_sondeo`, y editarlos a mano
    dejaría el panel diciendo algo distinto de lo que midió el sondeo."""

    list_display = ('farmacia', 'alcanzable', 'latencia_ms', 'fallas_consecutivas', 'ultima_verificacion')
    list_filter = ('alcanzable', 'farmacia__unidad_negocio', 'farmacia__grupo')
    search_fields = ('farmacia__codigo', 'farmacia__nombre', 'farmacia__circuito_proveedor')
    readonly_fields = (
        'farmacia', 'alcanzable', 'latencia_ms', 'fallas_consecutivas',
        'ultima_verificacion', 'ultimo_cambio_estado',
    )

    def get_queryset(self, request):
        return scope_por_unidad_negocio(
            super().get_queryset(request).select_related('farmacia__unidad_negocio'),
            request.user, 'farmacia__unidad_negocio',
        )

    def has_add_permission(self, request):
        return False


@admin.register(EventoEnlaceFarmacia)
class EventoEnlaceFarmaciaAdmin(admin.ModelAdmin):
    """El historial de caídas es la linea base de disponibilidad para discutir un SLA con
    el proveedor: se mira y se exporta, no se edita."""

    list_display = ('farmacia', 'inicio', 'fin', 'duracion_minutos', 'circuito_proveedor')
    list_filter = ('farmacia__unidad_negocio', 'farmacia__grupo')
    search_fields = ('farmacia__codigo', 'circuito_proveedor')
    date_hierarchy = 'inicio'
    readonly_fields = ('farmacia', 'inicio', 'fin', 'circuito_proveedor')

    @admin.display(description='Duración (min)')
    def duracion_minutos(self, obj):
        return obj.duracion_minutos if obj.fin else 'en curso'

    def get_queryset(self, request):
        return scope_por_unidad_negocio(
            super().get_queryset(request).select_related('farmacia__unidad_negocio'),
            request.user, 'farmacia__unidad_negocio',
        )

    def has_add_permission(self, request):
        return False


@admin.register(EquipoBordeFarmacia)
class EquipoBordeFarmaciaAdmin(admin.ModelAdmin):
    """Lo que el Mikrotik reporta de sí mismo. Solo lectura: lo escribe
    `sondear_identidad_mikrotik` por SNMP, y editarlo a mano dejaría el panel afirmando
    algo que el equipo no dijo.

    El filtro por versión de RouterOS es el que muestra la brecha de parcheo: a
    15-sep-2026, de los 4 equipos que responden SNMP, uno estaba en 6.47.7 y tres en
    6.49.17.
    """

    list_display = (
        'farmacia', 'modelo', 'numero_serie', 'version_routeros', 'nombre_coincide',
        'uptime_dias', 'ultima_lectura',
    )
    list_filter = ('version_routeros', 'modelo', 'farmacia__unidad_negocio')
    search_fields = ('farmacia__codigo', 'numero_serie', 'nombre_sistema', 'modelo')
    readonly_fields = (
        'farmacia', 'modelo', 'numero_serie', 'version_routeros', 'nombre_sistema',
        'uptime_segundos', 'ultima_lectura',
    )

    def get_queryset(self, request):
        # Ver el comentario de DispositivoDetectadoAdmin: `Farmacia.__str__` toca `grupo`.
        return scope_por_unidad_negocio(
            super().get_queryset(request).select_related('farmacia__unidad_negocio', 'farmacia__grupo'),
            request.user, 'farmacia__unidad_negocio',
        )

    def has_add_permission(self, request):
        return False

    @admin.display(description='Nombre coincide', boolean=True)
    def nombre_coincide(self, obj):
        """False acá significa que la IP cargada apunta a un equipo de OTRA farmacia."""
        return obj.nombre_coincide

    @admin.display(description='Uptime (días)', ordering='uptime_segundos')
    def uptime_dias(self, obj):
        """En días y no en segundos: 662266400 no le dice nada a nadie; "76 días" sí, y
        un valor chico repetido entre lecturas es un router reiniciándose solo."""
        if obj.uptime_segundos is None:
            return '—'
        return '%.1f' % (obj.uptime_segundos / 86400)


@admin.register(DispositivoDetectado)
class DispositivoDetectadoAdmin(admin.ModelAdmin):
    """Los equipos que el Mikrotik ve en la LAN de cada farmacia (tabla ARP).

    Es la mitad verificada del inventario: `Activo` guarda lo declarado, esto lo que hay
    realmente conectado. La columna "Declarado" cruza las dos por MAC — los que salen en
    rojo son equipos enchufados que nadie inventarió.
    """

    list_display = ('farmacia', 'ip', 'mac', 'declarado', 'visto_por_ultima_vez', 'visto_por_primera_vez')
    list_filter = ('farmacia__unidad_negocio', 'farmacia__grupo')
    search_fields = ('farmacia__codigo', 'ip', 'mac')
    readonly_fields = (
        'farmacia', 'ip', 'mac', 'interfaz_indice', 'visto_por_primera_vez', 'visto_por_ultima_vez',
    )
    date_hierarchy = 'visto_por_ultima_vez'

    def get_queryset(self, request):
        # El cruce con lo declarado se anota con un Exists y NO llamando a
        # `activo_declarado` por fila: el admin muestra 100 por página, y eso serían 100
        # consultas por cada carga del listado.
        from django.db.models import Exists, OuterRef

        from apps.activos.models import Activo

        declarado = Activo.objects.filter(
            farmacia=OuterRef('farmacia'), mac__iexact=OuterRef('mac'),
        )
        # `farmacia__grupo` además de la unidad: `Farmacia.__str__` devuelve
        # "ML001 (TRX001)", así que mostrar la farmacia en el listado toca `grupo` en
        # CADA fila. Sin esto eran 20 consultas extra por cada 20 filas — el cruce con
        # Exists ya estaba bien, el N+1 venía del __str__.
        return scope_por_unidad_negocio(
            super().get_queryset(request)
            .select_related('farmacia__unidad_negocio', 'farmacia__grupo')
            .annotate(esta_declarado=Exists(declarado)),
            request.user, 'farmacia__unidad_negocio',
        )

    def has_add_permission(self, request):
        return False

    @admin.display(description='Declarado', boolean=True, ordering='esta_declarado')
    def declarado(self, obj):
        return obj.esta_declarado


@admin.register(EstadoServicioPos)
class EstadoServicioPosAdmin(admin.ModelAdmin):
    """Los escribe el agente en cada chequeo; acá solo se miran.

    Registrado porque la ficha de /monitoreo/<pk>/ exige `monitorear_recursos`, que en
    producción tienen 2 de 10 estaciones. Un dato que solo se ve en una pantalla que casi
    nadie abre es un dato que no se ve.
    """

    list_display = (
        'estacion', 'servicio', 'disponible', 'critico', 'latencia_ms', 'endpoint',
        'ultima_verificacion',
    )
    list_filter = ('servicio', 'disponible', 'critico')
    search_fields = ('estacion__codigo', 'endpoint', 'mensaje')
    readonly_fields = [f.name for f in EstadoServicioPos._meta.fields]

    def get_queryset(self, request):
        # `Estacion.__str__` toca farmacia; sin esto son dos consultas por fila.
        return super().get_queryset(request).select_related('estacion__farmacia__grupo')

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(ConfiguracionMonitoreo)
class ConfiguracionMonitoreoAdmin(admin.ModelAdmin):
    """Los cuatro umbrales operativos, editables sin desplegar.

    Sin `add` ni `delete`: es una fila única (ver `ConfiguracionMonitoreo.save`). Dejar
    el botón de agregar invitaría a crear una segunda que nadie lee, y el operador
    cambiaría valores que no tienen efecto; dejar el de borrar permitiría quedarse sin
    configuración, aunque `obtener()` la recree.
    """

    list_display = (
        'minutos_minimos_aviso_enlace', 'fallas_consecutivas_enlace',
        'minutos_escalamiento_alerta', 'dias_ventana_top_errores', 'actualizado_en',
        'actualizado_por',
    )
    readonly_fields = ('actualizado_en', 'actualizado_por')
    fieldsets = (
        ('Enlaces de farmacia', {
            'fields': ('fallas_consecutivas_enlace', 'minutos_minimos_aviso_enlace'),
            'description': 'El barrido corre cada 2 minutos. Los sondeos fallidos deciden CUÁNDO '
                           'se declara la caída; los minutos mínimos, cuándo vale la pena avisarla.',
        }),
        ('Alertas', {'fields': ('minutos_escalamiento_alerta',)}),
        ('Consultas del bot', {'fields': ('dias_ventana_top_errores',)}),
        ('Última modificación', {'fields': ('actualizado_en', 'actualizado_por')}),
    )

    def has_add_permission(self, request):
        return not ConfiguracionMonitoreo.objects.exists()

    def has_delete_permission(self, request, obj=None):
        return False

    def changelist_view(self, request, extra_context=None):
        # Se entra directo a editar la única fila: una lista de un elemento es un clic de
        # más para llegar a lo único que se puede hacer acá.
        from django.shortcuts import redirect
        from django.urls import reverse

        configuracion = ConfiguracionMonitoreo.obtener()
        return redirect(reverse('admin:monitoreo_configuracionmonitoreo_change',
                                args=[configuracion.pk]))

    def save_model(self, request, obj, form, change):
        # Quién lo tocó explica por qué el sistema empezó a avisar más o menos que antes.
        obj.actualizado_por = request.user
        super().save_model(request, obj, form, change)


@admin.register(ServicioPosMonitoreado)
class ServicioPosMonitoreadoAdmin(admin.ModelAdmin):
    """Acá se agrega una plataforma nueva para monitorear, sin tocar código ni desplegar.

    A diferencia de los otros ModelAdmin de este módulo —que son de solo lectura porque
    los escribe el agente— este es el único donde se EDITA de verdad: es la fuente de
    verdad de qué chequea la flota.

    Guardar publica el catálogo a las estaciones (ver `publicar_catalogo_servicios_pos`).
    No hace falta un paso aparte ni esperar a nada: el mensaje queda retenido en el
    broker y una estación apagada lo recibe al encender.
    """

    list_display = ('nombre', 'clave', 'tipo', 'destino_o_descubierto', 'critico', 'activo', 'actualizado_en')
    list_filter = ('activo', 'critico', 'tipo', 'origen')
    search_fields = ('clave', 'nombre', 'destino', 'nota')
    list_editable = ('critico', 'activo')
    readonly_fields = ('actualizado_en',)
    fieldsets = (
        (None, {'fields': ('clave', 'nombre', 'activo', 'critico')}),
        ('Qué se chequea', {
            'fields': ('origen', 'tipo', 'destino', 'puerto', 'base_datos'),
            'description': (
                'Si el agente lo descubre del <code>.exe.Config</code> del POS, elegí ese '
                'origen y dejá el destino vacío. Para una IP o URL fija, "Definido acá". '
                '<strong>Nunca pongas usuario ni contraseña en el destino</strong>: esto '
                'se publica a todas las estaciones y se ve en el panel.'
            ),
        }),
        ('Para la mesa de ayuda', {'fields': ('nota', 'actualizado_en')}),
    )

    @admin.display(description='Destino', ordering='destino')
    def destino_o_descubierto(self, obj):
        if obj.origen == ServicioPosMonitoreado.Origen.CONFIG_POS:
            return 'del .exe.Config de cada POS'
        if obj.tipo == ServicioPosMonitoreado.Tipo.POSTGRES:
            return '%s:%s/%s' % (obj.destino, obj.puerto or 5432, obj.base_datos)
        return obj.destino

    def save_model(self, request, obj, form, change):
        """Publicar acá y no en una señal del modelo: una migración de datos o un test que
        cree filas no tiene por qué hablar con el broker. Lo que dispara la publicación es
        que una PERSONA haya editado el catálogo."""
        super().save_model(request, obj, form, change)
        self._publicar(request)

    def delete_model(self, request, obj):
        super().delete_model(request, obj)
        self._publicar(request)

    def delete_queryset(self, request, queryset):
        super().delete_queryset(request, queryset)
        self._publicar(request)

    def _publicar(self, request):
        from django.contrib import messages

        from .servicios_pos import publicar_catalogo_servicios_pos

        cuantos, ok = publicar_catalogo_servicios_pos()
        if ok:
            messages.info(request, f'Catálogo publicado a la flota: {cuantos} servicio(s) activo(s).')
        else:
            # Que el guardado haya funcionado y la publicación no es exactamente el modo
            # de falla silenciosa que este proyecto ya sufrió tres veces. Se avisa.
            messages.warning(
                request,
                'El catálogo se guardó pero NO se pudo publicar por MQTT: las estaciones '
                'siguen con la lista anterior. Revisá el log del panel y volvé a guardar.',
            )


@admin.register(EventoSistemaVigilado)
class EventoSistemaVigiladoAdmin(admin.ModelAdmin):
    """Acá se decide qué mira el agente en el visor de Windows, sin tocar código.

    Guardar publica el catálogo a la flota. Igual que el de servicios del POS.
    """

    list_display = ('nombre', 'log', 'proveedor', 'identificador', 'severidad', 'abre_alerta', 'activo')
    list_filter = ('activo', 'abre_alerta', 'severidad', 'log')
    search_fields = ('nombre', 'proveedor', 'nota', 'identificador')
    list_editable = ('abre_alerta', 'activo')
    fieldsets = (
        (None, {'fields': ('nombre', 'activo')}),
        ('Qué evento es', {
            'fields': ('log', 'proveedor', 'identificador'),
            'description': (
                '<strong>El proveedor no es opcional.</strong> El mismo número significa '
                'cosas distintas según quién lo emita: pedir solo <code>System 55</code> '
                'devuelve avisos de energía del procesador, no corrupción de NTFS. '
                'Sacalo del visor de Windows, columna "Origen".'
            ),
        }),
        ('Qué hacer con él', {
            'fields': ('severidad', 'abre_alerta', 'nota'),
            'description': (
                'Marcá <em>abre alerta</em> solo si amerita despertar a alguien. Una sola '
                'máquina generó 260 eventos de un mismo tipo en 30 días.'
            ),
        }),
    )

    def save_model(self, request, obj, form, change):
        super().save_model(request, obj, form, change)
        self._publicar(request)

    def delete_model(self, request, obj):
        super().delete_model(request, obj)
        self._publicar(request)

    def _publicar(self, request):
        from django.contrib import messages

        from .servicios_pos import publicar_catalogo_eventos_sistema

        cuantos, ok = publicar_catalogo_eventos_sistema()
        if ok:
            messages.info(request, f'Catálogo publicado a la flota: {cuantos} evento(s) vigilado(s).')
        else:
            messages.warning(
                request,
                'Se guardó pero NO se pudo publicar por MQTT: las estaciones siguen con la '
                'lista anterior. Revisá el log y volvé a guardar.',
            )


@admin.register(EventoSistemaDetectado)
class EventoSistemaDetectadoAdmin(admin.ModelAdmin):
    """Solo lectura: los escribe el agente. Agregados por tipo, no una fila por
    ocurrencia — ver el docstring del modelo."""

    list_display = ('estacion', 'log', 'origen', 'identificador', 'cantidad_total', 'ultima_vez')
    list_filter = ('log', 'origen', 'estacion__farmacia__unidad_negocio')
    search_fields = ('estacion__codigo', 'origen', 'ultimo_mensaje')
    readonly_fields = tuple(
        f.name for f in EventoSistemaDetectado._meta.fields if f.name != 'id'
    )
