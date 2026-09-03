"""Serializers de la API móvil (apps Flutter). Delegan toda mutación a services.py:
la API es una capa de transporte, no reimplementa reglas de negocio."""
from rest_framework import serializers

from apps.activos.models import Activo, Colaborador

from .models import (
    ActividadChecklist, ConsentimientoMonitoreo, EstadoGeneralEquipo, EventoMantenimiento, FirmaMantenimiento,
    ImagenMantenimiento, Mantenimiento, MantenimientoProgramado, ResultadoTecnico, TipoFirma, TipoMantenimiento,
    UbicacionTecnico,
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
    cliente = serializers.PrimaryKeyRelatedField(queryset=Colaborador.objects.filter(activo=True))
    tipo_mantenimiento = serializers.PrimaryKeyRelatedField(
        queryset=TipoMantenimiento.objects.filter(activo=True), required=False, allow_null=True, default=None,
    )
    estado_general = serializers.ChoiceField(choices=EstadoGeneralEquipo.choices)
    descripcion = serializers.CharField()
    fecha_programada = serializers.DateTimeField()
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
