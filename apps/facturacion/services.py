"""Cálculo de endpoints facturables por unidad de negocio y período.

Ver docstring de `ActividadMensualEstacion` (models.py) para por qué existe esta app en
vez de reusar Estacion.ultimo_heartbeat o MuestraMetrica.
"""
from django.utils import timezone

from apps.catalogo.models import Estacion, UnidadNegocio
from apps.facturacion.models import ActividadMensualEstacion


def registrar_actividad_mensual(estacion: Estacion, *, cuando=None) -> None:
    """Marca que `estacion` tuvo un heartbeat este mes (idempotente).

    Se llama desde apps.mqtt_worker.services en los mismos puntos donde ya se
    actualiza Estacion.ultimo_heartbeat/estado_conexion (manejar_heartbeat y el paso
    OK de manejar_estado_despliegue) — son los dos lugares que el propio worker ya
    trata como prueba de vida del agente.
    """
    cuando = cuando or timezone.now()
    ActividadMensualEstacion.objects.get_or_create(estacion=estacion, anio=cuando.year, mes=cuando.month)


def estaciones_facturables(unidad_negocio: UnidadNegocio, anio: int, mes: int):
    """Estaciones de `unidad_negocio` con actividad registrada en anio-mes."""
    return Estacion.objects.filter(
        actividad_mensual__anio=anio, actividad_mensual__mes=mes,
        farmacia__unidad_negocio=unidad_negocio,
    ).distinct().order_by('farmacia__codigo', 'codigo')


def resumen_facturacion(unidad_negocio: UnidadNegocio, anio: int, mes: int) -> int:
    """Cantidad de endpoints facturables de `unidad_negocio` en el período anio-mes."""
    return ActividadMensualEstacion.objects.filter(
        estacion__farmacia__unidad_negocio=unidad_negocio, anio=anio, mes=mes,
    ).count()
