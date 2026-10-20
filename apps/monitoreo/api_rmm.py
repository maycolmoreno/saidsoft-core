"""API de integración RMM: alertas para otros sistemas, de solo lectura.

Es lo que reemplaza, a futuro, la llamada directa de `apps.monitoreo.services` a
`apps.mantenimiento.services.abrir_mantenimiento_desde_alerta`: un sistema de
mantenimiento separado consulta las alertas nuevas y decide él qué abrir. Ver
docs/producto/SAIDSOFT_RMM_1_0_ALCANCE.md.

**Para consultar solo lo nuevo, `desde_id`**, no una fecha. El id crece con cada alerta y no
depende de relojes: el otro sistema guarda el último id que procesó y pide los
siguientes, sin huecos ni repetidos aunque dos alertas compartan el mismo segundo. Para
enterarse de que una alerta ya conocida se resolvió, se filtra por `estado`.

Mismos permisos y alcance que la pantalla de alertas del panel (`monitoreo.view_alerta`,
por unidad de negocio). No incluye el diagnóstico por IA: es un módulo diferido.
"""
from rest_framework import generics, serializers
from rest_framework.exceptions import ValidationError

from apps.catalogo.api_views import PaginacionRmm, validar_opcion
from apps.cuentas.api_permissions import PermisoDeclarado
from apps.cuentas.services import scope_por_unidad_negocio

from .models import Alerta


class AlertaRmmSerializer(serializers.ModelSerializer):
    estacion = serializers.CharField(source='estacion.codigo', read_only=True)
    farmacia = serializers.CharField(source='estacion.farmacia.codigo', read_only=True)
    unidad_negocio = serializers.CharField(source='estacion.farmacia.unidad_negocio.codigo', read_only=True)
    regla = serializers.CharField(source='regla.nombre', read_only=True)
    metrica = serializers.CharField(source='regla.metrica', read_only=True)
    severidad = serializers.CharField(source='regla.severidad', read_only=True)
    abre_mantenimiento = serializers.BooleanField(source='regla.abre_mantenimiento', read_only=True)

    class Meta:
        model = Alerta
        fields = [
            'id', 'estacion', 'farmacia', 'unidad_negocio',
            'regla', 'metrica', 'severidad', 'abre_mantenimiento',
            'estado', 'valor_disparador', 'abierta_en', 'reconocida_en', 'resuelta_en',
        ]
        read_only_fields = fields


class AlertasRmmView(generics.ListAPIView):
    """Alertas en orden de id. Filtros: `desde_id` (las de id mayor), `estado`, `estacion`,
    `farmacia`."""

    permission_classes = [PermisoDeclarado]
    permisos_por_metodo = {'GET': ['monitoreo.view_alerta']}
    serializer_class = AlertaRmmSerializer
    pagination_class = PaginacionRmm

    def get_queryset(self):
        queryset = scope_por_unidad_negocio(
            Alerta.objects.select_related('estacion__farmacia__unidad_negocio', 'regla'),
            self.request.user, 'estacion__farmacia__unidad_negocio',
        ).order_by('id')

        parametros = self.request.query_params
        desde_id = parametros.get('desde_id', '').strip()
        if desde_id:
            if not desde_id.isdigit():
                raise ValidationError({'desde_id': 'Debe ser un número entero.'})
            queryset = queryset.filter(id__gt=int(desde_id))
        estado = validar_opcion(parametros.get('estado', '').strip(), set(Alerta.Estado.values), 'estado')
        if estado:
            queryset = queryset.filter(estado=estado)
        if estacion := parametros.get('estacion', '').strip():
            queryset = queryset.filter(estacion__codigo__iexact=estacion)
        if farmacia := parametros.get('farmacia', '').strip():
            queryset = queryset.filter(estacion__farmacia__codigo__iexact=farmacia)
        return queryset
