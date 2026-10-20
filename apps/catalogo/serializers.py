"""Serializers de la API de integración RMM (`/api/v1/rmm/`). Ver `apps.catalogo.api_views`.

Los campos se listan uno por uno, nunca `fields = '__all__'`: `Estacion` guarda el token
de enrolamiento, el secreto HMAC, el id de MeshCentral y la conexión al POS, y nada de
eso le sirve a un sistema de inventario. Un campo nuevo en el modelo no debe aparecer en
la API sin que alguien decida exponerlo.
"""
from rest_framework import serializers

from .models import Estacion, Farmacia


class FarmaciaRmmSerializer(serializers.ModelSerializer):
    unidad_negocio = serializers.CharField(source='unidad_negocio.codigo', read_only=True)
    grupo = serializers.CharField(source='grupo.codigo', read_only=True)

    class Meta:
        model = Farmacia
        fields = [
            'codigo', 'nombre', 'unidad_negocio', 'grupo', 'activa',
            'ciudad', 'provincia', 'direccion', 'tipo_sucursal', 'latitud', 'longitud',
        ]
        read_only_fields = fields


class EstacionRmmSerializer(serializers.ModelSerializer):
    """Identidad y características de hardware de una estación, tal como las reportó el agente.

    El vínculo con otro sistema es `codigo` (único y estable) y `numero_serie`, nunca el id
    interno de la base de datos.
    """

    farmacia = serializers.CharField(source='farmacia.codigo', read_only=True)
    unidad_negocio = serializers.CharField(source='farmacia.unidad_negocio.codigo', read_only=True)

    class Meta:
        model = Estacion
        fields = [
            'codigo', 'farmacia', 'unidad_negocio', 'hostname', 'numero_serie',
            'so_nombre', 'so_build', 'version_agente', 'version_pos',
            'procesador', 'ram_total_mb', 'almacenamiento_total_gb', 'info_equipo_fecha',
            'bitlocker_habilitado', 'ip_lan', 'mac_lan',
            'estado_conexion', 'ultimo_heartbeat', 'estado_aprobacion', 'fecha_creacion',
        ]
        read_only_fields = fields


class PerifericoRmmSerializer(serializers.Serializer):
    nombre = serializers.CharField()
    fabricante = serializers.CharField()
    clase = serializers.CharField()


class SoftwareRmmSerializer(serializers.Serializer):
    nombre = serializers.CharField()
    version = serializers.CharField()
    fabricante = serializers.CharField()


class EstacionRmmDetalleSerializer(EstacionRmmSerializer):
    """La ficha completa: además de lo del listado, periféricos USB y software instalado
    según el último escaneo de cada uno."""

    perifericos = PerifericoRmmSerializer(source='perifericos_detectados', many=True, read_only=True)
    software = SoftwareRmmSerializer(source='software_instalado', many=True, read_only=True)

    class Meta(EstacionRmmSerializer.Meta):
        fields = EstacionRmmSerializer.Meta.fields + [
            'perifericos_ultima_verificacion', 'perifericos',
            'software_instalado_ultima_verificacion', 'software',
        ]
        read_only_fields = fields
