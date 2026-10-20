"""API de integración RMM: estaciones y farmacias para otros sistemas, de solo lectura.

Existe para que el inventario y el mantenimiento puedan vivir en un proyecto aparte
(ver docs/producto/SAIDSOFT_RMM_1_0_ALCANCE.md) y seguir usando lo que el agente ya
reporta —serial, procesador, RAM, disco, periféricos, software— sin volver a pedírselo
a las estaciones ni copiar tablas.

- **Solo lectura.** Las vistas declaran únicamente GET; cualquier otro método lo rechaza
  `PermisoDeclarado`, que deniega lo que no está declarado.
- **Mismos permisos y mismo alcance que el panel.** `catalogo.view_estacion` /
  `catalogo.view_farmacia`, filtrados a las unidades de negocio del usuario. El sistema
  externo entra con un usuario propio y su token (`/api/v1/auth/token/`), así que se le
  puede acotar o revocar sin tocar a nadie más.
- **Lo que no es visible devuelve 404, no 403**: no se confirma que exista una estación de
  otro cliente.
"""
from rest_framework import generics
from rest_framework.exceptions import ValidationError
from rest_framework.pagination import PageNumberPagination

from apps.cuentas.services import scope_por_unidad_negocio
from apps.cuentas.api_permissions import PermisoDeclarado

from .models import Estacion, Farmacia
from .serializers import EstacionRmmDetalleSerializer, EstacionRmmSerializer, FarmaciaRmmSerializer


class PaginacionRmm(PageNumberPagination):
    """La flota son ~700 farmacias y algunas estaciones más: con 200 por página una
    sincronización completa son pocas llamadas, y el máximo acota una respuesta enorme."""

    page_size = 200
    page_size_query_param = 'por_pagina'
    max_page_size = 500


def validar_opcion(valor, opciones, nombre):
    """Un filtro con un valor que no existe se rechaza, en vez de devolver una lista vacía
    que el otro sistema interpretaría como "no hay ninguna"."""
    if valor and valor not in opciones:
        raise ValidationError({nombre: f'Valor inválido. Opciones: {", ".join(sorted(opciones))}.'})
    return valor


class EstacionesRmmView(generics.ListAPIView):
    """Listado de estaciones.

    Filtros: `farmacia`, `unidad_negocio` (códigos), `numero_serie`, `hostname` (exactos, sin
    distinguir mayúsculas), `estado_conexion`.
    """

    permission_classes = [PermisoDeclarado]
    permisos_por_metodo = {'GET': ['catalogo.view_estacion']}
    serializer_class = EstacionRmmSerializer
    pagination_class = PaginacionRmm

    def get_queryset(self):
        queryset = scope_por_unidad_negocio(
            Estacion.objects.select_related('farmacia__unidad_negocio'),
            self.request.user, 'farmacia__unidad_negocio',
        ).order_by('codigo')

        parametros = self.request.query_params
        if farmacia := parametros.get('farmacia', '').strip():
            queryset = queryset.filter(farmacia__codigo__iexact=farmacia)
        if unidad := parametros.get('unidad_negocio', '').strip():
            queryset = queryset.filter(farmacia__unidad_negocio__codigo__iexact=unidad)
        if serie := parametros.get('numero_serie', '').strip():
            queryset = queryset.filter(numero_serie__iexact=serie)
        if hostname := parametros.get('hostname', '').strip():
            queryset = queryset.filter(hostname__iexact=hostname)
        estado = validar_opcion(
            parametros.get('estado_conexion', '').strip(), set(Estacion.EstadoConexion.values), 'estado_conexion',
        )
        if estado:
            queryset = queryset.filter(estado_conexion=estado)
        return queryset


class EstacionRmmDetalleView(generics.RetrieveAPIView):
    """Ficha de una estación por su código, con periféricos y software instalado."""

    permission_classes = [PermisoDeclarado]
    permisos_por_metodo = {'GET': ['catalogo.view_estacion']}
    serializer_class = EstacionRmmDetalleSerializer
    lookup_field = 'codigo'

    def get_queryset(self):
        return scope_por_unidad_negocio(
            Estacion.objects.select_related('farmacia__unidad_negocio').prefetch_related(
                'perifericos_detectados', 'software_instalado',
            ),
            self.request.user, 'farmacia__unidad_negocio',
        )


class FarmaciasRmmView(generics.ListAPIView):
    """Listado de farmacias. Filtros: `unidad_negocio` (código), `activa` (true/false)."""

    permission_classes = [PermisoDeclarado]
    permisos_por_metodo = {'GET': ['catalogo.view_farmacia']}
    serializer_class = FarmaciaRmmSerializer
    pagination_class = PaginacionRmm

    def get_queryset(self):
        queryset = scope_por_unidad_negocio(
            Farmacia.objects.select_related('unidad_negocio', 'grupo'),
            self.request.user, 'unidad_negocio',
        ).order_by('codigo')

        parametros = self.request.query_params
        if unidad := parametros.get('unidad_negocio', '').strip():
            queryset = queryset.filter(unidad_negocio__codigo__iexact=unidad)
        activa = validar_opcion(parametros.get('activa', '').strip().lower(), {'true', 'false'}, 'activa')
        if activa:
            queryset = queryset.filter(activa=(activa == 'true'))
        return queryset
