"""Serializers de la API móvil (apps Flutter). Delegan toda mutación a services.py:
la API es una capa de transporte, no reimplementa reglas de negocio."""
from rest_framework import serializers

from apps.activos.models import Activo, Bodega, CategoriaEquipo, Colaborador, Marca
from apps.catalogo.models import Farmacia

from .models import (
    ActividadChecklist, ConsentimientoMonitoreo, EstadoGeneralEquipo, EventoMantenimiento, FirmaMantenimiento,
    ImagenMantenimiento, Mantenimiento, MantenimientoProgramado, Notificacion, ResultadoTecnico, TipoFirma,
    PrioridadMantenimiento, TipoMantenimiento, UbicacionTecnico, VisitaTecnica,
)


class ActivoResumenSerializer(serializers.ModelSerializer):
    marca = serializers.StringRelatedField()
    categoria = serializers.StringRelatedField()

    class Meta:
        model = Activo
        fields = ['id', 'codigo', 'tipo', 'marca', 'categoria', 'modelo', 'numero_serie', 'estado']


class FirmaMantenimientoSerializer(serializers.ModelSerializer):
    class Meta:
        model = FirmaMantenimiento
        fields = ['id', 'tipo_firma', 'firma_base64', 'firmado_en', 'firmado_por']
        read_only_fields = fields


class ImagenMantenimientoSerializer(serializers.ModelSerializer):
    class Meta:
        model = ImagenMantenimiento
        fields = ['id', 'imagen', 'nombre_archivo', 'tamanio_bytes', 'subido_en']
        read_only_fields = fields


class EventoMantenimientoSerializer(serializers.ModelSerializer):
    usuario = serializers.StringRelatedField()

    class Meta:
        model = EventoMantenimiento
        fields = ['id', 'tipo_evento', 'usuario', 'detalle', 'timestamp']
        read_only_fields = fields


class ChecklistItemSerializer(serializers.Serializer):
    id = serializers.IntegerField(source='item.id')
    nombre = serializers.CharField(source='item.nombre')
    orden = serializers.IntegerField(source='item.orden')
    realizada = serializers.BooleanField()


class MantenimientoListSerializer(serializers.ModelSerializer):
    cliente = serializers.StringRelatedField()
    tipo_mantenimiento = serializers.StringRelatedField()
    equipos = serializers.SerializerMethodField()
    # SLA y ubicación: son lo que el técnico necesita para decidir a qué va primero y
    # a dónde tiene que ir. Sin esto la app lista mantenimientos sin poder priorizar.
    estado_sla = serializers.CharField(read_only=True)
    limite_resolucion = serializers.DateTimeField(read_only=True)
    farmacia = serializers.SerializerMethodField()

    class Meta:
        model = Mantenimiento
        fields = [
            'id', 'descripcion', 'tipo_mantenimiento', 'tipo_origen', 'estado_interno',
            'resultado_tecnico', 'cliente', 'fecha_programada', 'fecha_cierre', 'equipos',
            'prioridad', 'estado_sla', 'limite_resolucion', 'farmacia',
        ]
        read_only_fields = fields

    def get_equipos(self, obj):
        return ActivoResumenSerializer([me.equipo for me in obj.equipos.select_related('equipo')], many=True).data

    def get_farmacia(self, obj):
        """Farmacia del equipo principal, con coordenadas para que la app pueda abrir
        la navegación. None si el equipo no está asociado a una farmacia (equipo
        administrativo, o el dato todavía sin cargar)."""
        principal = next(
            (me for me in obj.equipos.all() if me.es_principal and me.equipo_id), None,
        )
        farmacia = principal.equipo.farmacia if principal else None
        if farmacia is None:
            return None
        return {
            'id': farmacia.pk,
            'codigo': farmacia.codigo,
            'nombre': farmacia.nombre,
            'direccion': farmacia.direccion,
            'latitud': farmacia.latitud,
            'longitud': farmacia.longitud,
        }


class MantenimientoDetalleSerializer(MantenimientoListSerializer):
    firmas = FirmaMantenimientoSerializer(many=True, read_only=True)
    imagenes = ImagenMantenimientoSerializer(many=True, read_only=True)
    eventos = EventoMantenimientoSerializer(many=True, read_only=True)

    presencia_en_sitio = serializers.CharField(read_only=True)

    class Meta(MantenimientoListSerializer.Meta):
        fields = MantenimientoListSerializer.Meta.fields + [
            'snapshot_equipo', 'firmas', 'imagenes', 'eventos',
            'presencia_en_sitio', 'distancia_verificacion_metros',
        ]
        read_only_fields = fields


class MantenimientoCrearSerializer(serializers.Serializer):
    """Creación self-service desde la app móvil: el técnico se autoasigna (ver
    MantenimientoViewSet.create, que fija tecnico=request.user sin importar el payload)."""
    equipos = serializers.PrimaryKeyRelatedField(
        many=True, queryset=Activo.objects.exclude(estado=Activo.Estado.DADO_DE_BAJA),
    )
    # Opcional: un POS de farmacia no tiene custodio en el mismo sentido que un
    # equipo de oficina, y exigirlo obligaría al técnico a inventar uno para poder
    # abrir el mantenimiento del equipo que tiene delante.
    cliente = serializers.PrimaryKeyRelatedField(
        queryset=Colaborador.objects.filter(activo=True), required=False, allow_null=True, default=None,
    )
    tipo_mantenimiento = serializers.PrimaryKeyRelatedField(
        queryset=TipoMantenimiento.objects.filter(activo=True), required=False, allow_null=True, default=None,
    )
    prioridad = serializers.ChoiceField(
        choices=PrioridadMantenimiento.choices, required=False,
        default=PrioridadMantenimiento.NORMAL,
    )
    estado_general = serializers.ChoiceField(choices=EstadoGeneralEquipo.choices)
    descripcion = serializers.CharField()
    # Por defecto AHORA: el técnico que abre un mantenimiento desde el celular está
    # parado frente al equipo, no agendando para otro día.
    fecha_programada = serializers.DateTimeField(required=False)
    mantenimiento_programado = serializers.PrimaryKeyRelatedField(
        queryset=MantenimientoProgramado.objects.filter(activo=True), required=False, allow_null=True,
    )


class CerrarMantenimientoSerializer(serializers.Serializer):
    """`tiempo_real_minutos` y `estado_general` ya los aceptaba services.cerrar_
    mantenimiento y los pide el panel; faltaban acá, así que desde la app no se podían
    registrar."""
    resultado_tecnico = serializers.ChoiceField(choices=ResultadoTecnico.choices)
    tiempo_real_minutos = serializers.IntegerField(min_value=1, required=False, allow_null=True)
    estado_general = serializers.ChoiceField(
        choices=EstadoGeneralEquipo.choices, required=False, allow_blank=True, default='',
    )


class ChecklistActualizarSerializer(serializers.Serializer):
    actividad_id = serializers.PrimaryKeyRelatedField(queryset=ActividadChecklist.objects.filter(activo=True))
    realizada = serializers.BooleanField()


class FirmarMantenimientoSerializer(serializers.Serializer):
    tipo_firma = serializers.ChoiceField(choices=TipoFirma.choices)
    firma_base64 = serializers.CharField()


class ImagenAdjuntarSerializer(serializers.Serializer):
    archivo = serializers.FileField()


class ConsentimientoMonitoreoSerializer(serializers.ModelSerializer):
    class Meta:
        model = ConsentimientoMonitoreo
        fields = ['id', 'aceptado', 'version_terminos', 'timestamp']
        read_only_fields = ['id', 'timestamp']


class UbicacionTecnicoSerializer(serializers.ModelSerializer):
    class Meta:
        model = UbicacionTecnico
        fields = ['id', 'latitud', 'longitud', 'precision_metros', 'timestamp_captura']
        read_only_fields = ['id']


class UsuarioActualSerializer(serializers.Serializer):
    """Identidad + permisos del usuario autenticado (ver UsuarioActualView)."""
    id = serializers.IntegerField(read_only=True)
    username = serializers.CharField(read_only=True)
    nombre = serializers.SerializerMethodField()
    email = serializers.EmailField(read_only=True)
    es_staff = serializers.BooleanField(source='is_staff', read_only=True)
    permisos = serializers.SerializerMethodField()
    unidades_negocio = serializers.SerializerMethodField()

    def get_nombre(self, obj) -> str:
        return obj.get_full_name() or obj.username

    def get_permisos(self, obj) -> list:
        # Lista plana de codenames "app.permiso" -- los mismos que evalúa el panel.
        return sorted(obj.get_all_permissions())

    def get_unidades_negocio(self, obj) -> list:
        """Códigos de las unidades que este usuario puede ver. Lista vacía = acceso a
        todas (mismo criterio que apps.cuentas.services, donde 'sin restricción' se
        representa por ausencia de filtro, no por enumerar todo)."""
        from apps.cuentas.services import unidades_negocio_visibles, usuario_tiene_acceso_total

        if usuario_tiene_acceso_total(obj):
            return []
        return sorted(unidades_negocio_visibles(obj).values_list('codigo', flat=True))


class ActividadChecklistSerializer(serializers.ModelSerializer):
    """Catálogo global (ver ActividadChecklistView)."""
    categorias = serializers.StringRelatedField(many=True)

    class Meta:
        model = ActividadChecklist
        fields = ['id', 'nombre', 'orden', 'categorias']
        read_only_fields = fields


class ActivoMovilSerializer(ActivoResumenSerializer):
    """Equipos para la app: suma la farmacia donde está y su estación RMM si tiene."""
    farmacia = serializers.SerializerMethodField()
    estacion = serializers.CharField(source='estacion.codigo', read_only=True, default=None)
    custodio = serializers.CharField(
        source='colaborador_actual.nombre', read_only=True, default=None,
    )

    class Meta(ActivoResumenSerializer.Meta):
        fields = ActivoResumenSerializer.Meta.fields + [
            'farmacia', 'estacion', 'estado_fisico_actual', 'custodio',
        ]

    def get_farmacia(self, obj):
        if obj.farmacia_id is None:
            return None
        return {'id': obj.farmacia_id, 'codigo': obj.farmacia.codigo, 'nombre': obj.farmacia.nombre}


class NotificacionSerializer(serializers.ModelSerializer):
    class Meta:
        model = Notificacion
        fields = ['id', 'mensaje', 'url', 'leida', 'mantenimiento', 'creado_en']
        read_only_fields = ['id', 'mensaje', 'url', 'mantenimiento', 'creado_en']


class VisitaTecnicaSerializer(serializers.ModelSerializer):
    """Visitas del técnico para la app. Incluye las coordenadas de la farmacia para
    que la app pueda abrir la navegación y verificar presencia."""
    farmacia = serializers.SerializerMethodField()
    presencia_en_sitio = serializers.CharField(read_only=True)
    atrasada = serializers.BooleanField(read_only=True)

    class Meta:
        model = VisitaTecnica
        fields = [
            'id', 'estado', 'fecha_planificada', 'motivo', 'observaciones',
            'fecha_inicio', 'fecha_cierre', 'farmacia', 'presencia_en_sitio',
            'atrasada', 'distancia_verificacion_metros',
        ]
        read_only_fields = fields

    def get_farmacia(self, obj):
        f = obj.farmacia
        return {
            'id': f.pk, 'codigo': f.codigo, 'nombre': f.nombre,
            'direccion': f.direccion, 'latitud': f.latitud, 'longitud': f.longitud,
        }


class CerrarVisitaSerializer(serializers.Serializer):
    observaciones = serializers.CharField(required=False, allow_blank=True, default='')


class ActivoCrearSerializer(serializers.Serializer):
    """Alta de un equipo desde el campo.

    Se pide farmacia O bodega, igual que el alta del panel: un técnico que encuentra
    un equipo sin registrar está parado en la farmacia, no en un almacén, y exigirle
    una bodega lo obligaría a inventar una por la que el equipo nunca pasó (ver
    apps.activos.services.registrar_ingreso).
    """
    tipo = serializers.ChoiceField(choices=Activo.Tipo.choices)
    marca = serializers.PrimaryKeyRelatedField(
        queryset=Marca.objects.all(), required=False, allow_null=True, default=None,
    )
    categoria = serializers.PrimaryKeyRelatedField(
        queryset=CategoriaEquipo.objects.all(), required=False, allow_null=True, default=None,
    )
    modelo = serializers.CharField(required=False, allow_blank=True, default='')
    numero_serie = serializers.CharField(required=False, allow_blank=True, default='')
    procesador = serializers.CharField(required=False, allow_blank=True, default='')
    ram_gb = serializers.IntegerField(min_value=1, required=False, allow_null=True, default=None)
    almacenamiento_gb = serializers.IntegerField(min_value=1, required=False, allow_null=True, default=None)
    codigo_sap = serializers.CharField(required=False, allow_blank=True, default='')
    farmacia = serializers.PrimaryKeyRelatedField(
        queryset=Farmacia.objects.filter(activa=True), required=False, allow_null=True, default=None,
    )
    bodega = serializers.PrimaryKeyRelatedField(
        queryset=Bodega.objects.filter(activa=True), required=False, allow_null=True, default=None,
    )

    def validate(self, datos):
        if not datos.get('farmacia') and not datos.get('bodega'):
            raise serializers.ValidationError(
                'Indica la farmacia donde esta instalado el equipo, o la bodega donde ingresa.',
            )
        return datos


class CatalogosSerializer(serializers.Serializer):
    """Todos los catálogos en UNA respuesta.

    En una farmacia con enlace intermitente, cinco llamadas para llenar un formulario
    son cinco oportunidades de fallar; una sola se reintenta entera y es lo que la app
    puede cachear.
    """
    tipos_equipo = serializers.SerializerMethodField()
    marcas = serializers.SerializerMethodField()
    categorias = serializers.SerializerMethodField()
    tipos_mantenimiento = serializers.SerializerMethodField()
    estados_generales = serializers.SerializerMethodField()
    prioridades = serializers.SerializerMethodField()
    farmacias = serializers.SerializerMethodField()
    bodegas = serializers.SerializerMethodField()
    colaboradores = serializers.SerializerMethodField()

    def _opciones(self, choices):
        return [{'valor': v, 'etiqueta': e} for v, e in choices]

    def get_tipos_equipo(self, _):
        return self._opciones(Activo.Tipo.choices)

    def get_estados_generales(self, _):
        return self._opciones(EstadoGeneralEquipo.choices)

    def get_prioridades(self, _):
        return self._opciones(PrioridadMantenimiento.choices)

    def get_marcas(self, _):
        return [{'id': m.pk, 'nombre': m.nombre} for m in Marca.objects.order_by('nombre')]

    def get_categorias(self, _):
        return [{'id': c.pk, 'nombre': c.nombre} for c in CategoriaEquipo.objects.order_by('nombre')]

    def get_tipos_mantenimiento(self, _):
        return [
            {'id': t.pk, 'nombre': t.nombre}
            for t in TipoMantenimiento.objects.filter(activo=True).order_by('nombre')
        ]

    def get_farmacias(self, _):
        # Acotadas a lo que el usuario puede ver, igual que el panel.
        from apps.cuentas.services import scope_por_unidad_negocio

        queryset = scope_por_unidad_negocio(
            Farmacia.objects.filter(activa=True), self.context['request'].user, 'unidad_negocio',
        ).order_by('codigo')
        return [{'id': f.pk, 'codigo': f.codigo, 'nombre': f.nombre} for f in queryset]

    def get_colaboradores(self, _):
        from apps.cuentas.services import scope_opcional_por_unidad_negocio

        queryset = scope_opcional_por_unidad_negocio(
            Colaborador.objects.filter(activo=True), self.context['request'].user, 'unidad_negocio',
        ).order_by('nombre')
        return [{'id': c.pk, 'nombre': c.nombre} for c in queryset]

    def get_bodegas(self, _):
        from apps.cuentas.services import scope_opcional_por_unidad_negocio

        queryset = scope_opcional_por_unidad_negocio(
            Bodega.objects.filter(activa=True), self.context['request'].user, 'unidad_negocio',
        ).order_by('codigo')
        return [{'id': b.pk, 'codigo': b.codigo, 'nombre': b.nombre} for b in queryset]
