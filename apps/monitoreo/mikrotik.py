"""Sondeo SNMP a los Mikrotik de cada farmacia (Parte A del monitoreo proactivo de
ancho de banda) — ver PLAN_MODERNIZACION.md §9. Solo visibilidad en v1: no crea
Alerta ni notifica, ver docstring de apps.panel.views.monitoreo.red_farmacias_lista.

No implementa apps.monitoreo.adapters.base.FuenteMonitoreo a propósito: ese puerto es
para avisar estado online/offline de un DISPOSITIVO (EstadoDispositivo, estación-
scoped) vía registrar_estado_dispositivo — esto es una serie de tiempo numérica por
SITIO (farmacia), forma distinta, forzarlo sería una abstracción que no encaja.

pysnmp >=7 es asyncio-nativo (no hay API sincrónica) — se corre un loop propio dentro
del task de Celery (síncrono) con asyncio.run(), concurrencia acotada con un
Semaphore (no ThreadPoolExecutor: no hace falta con un cliente async, mismo objetivo
de no hacer 600 sondeos secuenciales en un solo Celery Beat).
"""
import asyncio
import logging

from django.conf import settings
from django.utils import timezone

from pysnmp.hlapi.v3arch.asyncio import (
    CommunityData, ContextData, ObjectIdentity, ObjectType, SnmpEngine, UdpTransportTarget, get_cmd, walk_cmd,
)

logger = logging.getLogger(__name__)

# IF-MIB: contadores de 64 bits ("HC" = high capacity), no los de 32 bits
# (ifInOctets/ifOutOctets) — un enlace con tráfico sostenido puede dar la vuelta al
# contador de 32 bits en minutos/horas; HC evita ese wraparound.
_OID_IF_DESCR = '1.3.6.1.2.1.2.2.1.2'
_OID_IF_HC_IN_OCTETS = '1.3.6.1.2.1.31.1.1.1.6'
_OID_IF_HC_OUT_OCTETS = '1.3.6.1.2.1.31.1.1.1.10'

# Máximo de sondeos SNMP en paralelo por corrida — a ~600 sitios, secuencial con
# timeout de 3s podría tardar hasta 30 min si muchos routers no responden, más que el
# propio intervalo del Beat (5 min). Acotado, no ilimitado, para no saturar la red
# del servidor con 600 sockets UDP simultáneos.
_MAX_SONDEOS_CONCURRENTES = 25

# ifIndex ya resuelto por IP — se resuelve una sola vez por Mikrotik (vía WALK sobre
# ifDescr) y se reusa en las corridas siguientes, no se repite el walk cada 5 min.
_cache_indice_interfaz: dict[str, int] = {}


def _config():
    cfg = getattr(settings, 'MIKROTIK_SNMP_CONFIG', {})
    comunidad = cfg.get('COMUNIDAD', '')
    interfaz_wan = cfg.get('INTERFAZ_WAN', '')
    if not (comunidad and interfaz_wan):
        return None
    return comunidad, cfg.get('PUERTO', 161), interfaz_wan


async def _resolver_indice_interfaz(ip, comunidad, puerto, interfaz_wan):
    """WALK sobre ifDescr para encontrar el ifIndex de `interfaz_wan` (ej. "ether1")
    — se cachea en proceso una vez resuelto. Nunca lanza: ante cualquier error
    devuelve None y loguea (un router caído/mal configurado no debe tumbar el resto
    de la corrida, mismo criterio que
    apps.mqtt_worker.emqx_admin.aprovisionar_credencial_estacion)."""
    if ip in _cache_indice_interfaz:
        return _cache_indice_interfaz[ip]
    engine = SnmpEngine()
    try:
        target = await UdpTransportTarget.create((ip, puerto), timeout=3, retries=0)
        async for errorIndication, errorStatus, errorIndex, varBinds in walk_cmd(
            engine, CommunityData(comunidad), target, ContextData(),
            ObjectType(ObjectIdentity(_OID_IF_DESCR)),
        ):
            if errorIndication or errorStatus:
                logger.warning(
                    'Mikrotik %s: error resolviendo ifIndex (%s).', ip, errorIndication or errorStatus,
                )
                return None
            for oid, valor in varBinds:
                if str(valor) == interfaz_wan:
                    indice = int(str(oid).rsplit('.', 1)[-1])
                    _cache_indice_interfaz[ip] = indice
                    return indice
    except Exception:
        logger.warning('Mikrotik %s: excepción resolviendo ifIndex.', ip, exc_info=True)
        return None
    logger.warning('Mikrotik %s: no se encontró la interfaz "%s" (ifDescr).', ip, interfaz_wan)
    return None


async def _leer_contadores(ip, comunidad, puerto, indice):
    """GET de ifHCInOctets/ifHCOutOctets — nunca lanza, timeout corto (3s)."""
    engine = SnmpEngine()
    try:
        target = await UdpTransportTarget.create((ip, puerto), timeout=3, retries=0)
        errorIndication, errorStatus, errorIndex, varBinds = await get_cmd(
            engine, CommunityData(comunidad), target, ContextData(),
            ObjectType(ObjectIdentity(f'{_OID_IF_HC_IN_OCTETS}.{indice}')),
            ObjectType(ObjectIdentity(f'{_OID_IF_HC_OUT_OCTETS}.{indice}')),
        )
    except Exception:
        logger.warning('Mikrotik %s: excepción leyendo contadores.', ip, exc_info=True)
        return None
    if errorIndication or errorStatus:
        logger.warning('Mikrotik %s: error leyendo contadores (%s).', ip, errorIndication or errorStatus)
        return None
    try:
        return int(varBinds[0][1]), int(varBinds[1][1])
    except (IndexError, ValueError, TypeError):
        logger.warning('Mikrotik %s: respuesta SNMP con forma inesperada.', ip)
        return None


async def _sondear_farmacia(farmacia, comunidad, puerto, interfaz_wan):
    """(farmacia, bytes_recibidos, bytes_enviados) o None si no se pudo sondear —
    nunca lanza. Punto único que patchean los tests (evita hardware SNMP real)."""
    indice = await _resolver_indice_interfaz(farmacia.ip_router, comunidad, puerto, interfaz_wan)
    if indice is None:
        return None
    contadores = await _leer_contadores(farmacia.ip_router, comunidad, puerto, indice)
    if contadores is None:
        return None
    return farmacia, contadores[0], contadores[1]


def _calcular_tasa(farmacia, bytes_recibidos, bytes_enviados):
    """Diferencia contra la MuestraRedFarmacia anterior de esta farmacia (persistida
    en BD, no en memoria de proceso — a diferencia del agente de estación, este task
    de Celery puede reiniciar entre corridas). None si es la primera muestra o si el
    contador bajó respecto a la anterior (reinicio del router — no calcula una tasa
    negativa/sin sentido, se retoma normal en la próxima corrida)."""
    anterior = farmacia.muestras_red.first()  # ordering = -timestamp
    if anterior is None:
        return None, None
    elapsed = (timezone.now() - anterior.timestamp).total_seconds()
    if elapsed <= 0 or bytes_recibidos < anterior.bytes_recibidos or bytes_enviados < anterior.bytes_enviados:
        return None, None
    recibido_kbps = round((bytes_recibidos - anterior.bytes_recibidos) * 8 / 1000 / elapsed, 1)
    enviado_kbps = round((bytes_enviados - anterior.bytes_enviados) * 8 / 1000 / elapsed, 1)
    return recibido_kbps, enviado_kbps


def sincronizar_ancho_banda_farmacias() -> int:
    """Celery Beat periódico (cada 5 min, ver CELERY_BEAT_SCHEDULE): sondea por SNMP
    el Mikrotik de cada Farmacia con `ip_router` cargada y guarda una MuestraRedFarmacia
    nueva por cada una que respondió. Un router caído/sin config no interrumpe el
    resto de la corrida. Sin MIKROTIK_SNMP_CONFIG configurado, no hace nada (mismo
    criterio que el resto de los `_CONFIG` opcionales del proyecto). Devuelve cuántas
    farmacias se sondearon con éxito."""
    from apps.catalogo.models import Farmacia
    from apps.monitoreo.models import MuestraRedFarmacia

    config = _config()
    if config is None:
        return 0
    comunidad, puerto, interfaz_wan = config

    # GenericIPAddressField normaliza '' a None al guardar (get_prep_value) — un
    # excluir aparte por '' no solo es redundante, en SQLite "columna = NULL" nunca es
    # verdadero, así que .exclude(ip_router='') con NULL de por medio termina
    # excluyendo TODAS las filas, incluidas las que sí tienen una IP real (bug real
    # encontrado probando esto a mano). __isnull=True alcanza solo.
    farmacias = list(Farmacia.objects.exclude(ip_router__isnull=True))
    if not farmacias:
        return 0

    async def _sondear_todas():
        semaforo = asyncio.Semaphore(_MAX_SONDEOS_CONCURRENTES)

        async def _con_limite(farmacia):
            async with semaforo:
                return await _sondear_farmacia(farmacia, comunidad, puerto, interfaz_wan)

        return await asyncio.gather(*[_con_limite(f) for f in farmacias])

    resultados = asyncio.run(_sondear_todas())

    exitosas = 0
    for resultado in resultados:
        if resultado is None:
            continue
        farmacia, bytes_recibidos, bytes_enviados = resultado
        red_recibido_kbps, red_enviado_kbps = _calcular_tasa(farmacia, bytes_recibidos, bytes_enviados)
        MuestraRedFarmacia.objects.create(
            farmacia=farmacia, bytes_recibidos=bytes_recibidos, bytes_enviados=bytes_enviados,
            red_recibido_kbps=red_recibido_kbps, red_enviado_kbps=red_enviado_kbps,
        )
        exitosas += 1
    return exitosas
