"""Sondeo SNMP a los Mikrotik de cada farmacia (Parte A del monitoreo proactivo de
ancho de banda) — ver PLAN_MODERNIZACION.md §9. Solo visibilidad en v1: no crea
Alerta ni notifica, ver docstring de apps.panel.views.enlaces.red_farmacias_lista.

No implementa apps.monitoreo.adapters.base.FuenteMonitoreo a propósito: ese puerto es
para avisar estado online/offline de un DISPOSITIVO (EstadoDispositivo, estación-
scoped) vía registrar_estado_dispositivo — esto es una serie de tiempo numérica por
SITIO (farmacia), forma distinta, forzarlo sería una abstracción que no encaja.

pysnmp >=7 es asyncio-nativo (no hay API sincrónica) — se corre un loop propio dentro
del task de Celery (síncrono) con asyncio.run(), concurrencia acotada con un
Semaphore (no ThreadPoolExecutor: no hace falta con un cliente async, mismo objetivo
de no hacer 600 sondeos secuenciales en un solo Celery Beat).

Nada de esto se configura por sitio más allá de `Farmacia.ip_router` — dos supuestos
iniciales de diseño resultaron falsos al validar contra un router real de producción
(ML006, 20-ago-2026), así que ambos se resuelven solos en vez de pedir un dato
uniforme que no lo era:
- La community SNMP no es compartida/global — es el código de la farmacia en
  minúscula (ver `_comunidad_para`).
- El nombre de la interfaz WAN tampoco es uniforme (en ML006 es "ether3_Telconet",
  no "ether1") — se resuelve por SNMP contra la ruta por defecto activa (ver
  `_resolver_indice_interfaz_wan`), no por nombre configurado.
"""
import asyncio
import logging
from datetime import timedelta

from django.conf import settings
from django.utils import timezone

from pysnmp.hlapi.v3arch.asyncio import (
    CommunityData, ContextData, ObjectIdentity, ObjectType, SnmpEngine, UdpTransportTarget,
    bulk_walk_cmd, get_cmd,
)

logger = logging.getLogger(__name__)

# IP-MIB: ipRouteIfIndex de la ruta 0.0.0.0 (destino por defecto) — da directo el
# ifIndex de la interfaz que el router está usando AHORA MISMO para salir a Internet,
# sin necesitar saber su nombre. Estándar (RFC 1213), RouterOS lo expone para
# compatibilidad aunque internamente use ipCidrRouteTable.
_OID_IP_ROUTE_IF_INDEX_DEFAULT = '1.3.6.1.2.1.4.21.1.2.0.0.0.0'

# IF-MIB: contadores de 64 bits ("HC" = high capacity), no los de 32 bits
# (ifInOctets/ifOutOctets) — un enlace con tráfico sostenido puede dar la vuelta al
# contador de 32 bits en minutos/horas; HC evita ese wraparound.
_OID_IF_HC_IN_OCTETS = '1.3.6.1.2.1.31.1.1.1.6'
_OID_IF_HC_OUT_OCTETS = '1.3.6.1.2.1.31.1.1.1.10'

# Máximo de sondeos SNMP en paralelo por corrida — a ~600 sitios, secuencial con
# timeout de 3s podría tardar hasta 30 min si muchos routers no responden, más que el
# propio intervalo del Beat (5 min). Acotado, no ilimitado, para no saturar la red
# del servidor con 600 sockets UDP simultáneos.
_MAX_SONDEOS_CONCURRENTES = 25

# ifIndex de la interfaz WAN ya resuelto por IP — se resuelve una sola vez por
# Mikrotik y se reusa en las corridas siguientes, no se repite el GET cada 5 min.
_cache_indice_interfaz: dict[str, int] = {}


def _puerto() -> int:
    return getattr(settings, 'MIKROTIK_SNMP_CONFIG', {}).get('PUERTO', 161)


def _comunidad_para(farmacia) -> str:
    """La community SNMP de cada Mikrotik es el código de la farmacia en minúscula
    (ej. "ml006" para ML006) — convención confirmada contra un router real de
    producción (20-ago-2026), no una community global compartida como se había
    asumido al diseñar esto. No hace falta cargar nada extra por sitio.

    Al habilitar SNMP en el resto de la flota, la community tiene que quedar de SOLO
    LECTURA (`/snmp community print` en RouterOS: `write-access` en `no`). El motivo es
    esta misma convención: si la community es el código de la farmacia, es adivinable
    para cualquiera que vea una pantalla del sistema, y SNMP v2c la manda en texto plano
    por la red. Con lectura, lo peor que puede pasar es que alguien vea la topología;
    con escritura, podría apagar interfaces y dejar la farmacia sin servicio.

    Este módulo nunca escribe: solo `get_cmd` y `bulk_walk_cmd`. La advertencia es sobre
    cómo se configuran los equipos, no sobre lo que hace este código."""
    return farmacia.codigo.lower()


async def _resolver_indice_interfaz_wan(ip, comunidad, puerto):
    """GET de ipRouteIfIndex para la ruta por defecto — da el ifIndex de la interfaz
    WAN sin necesitar su nombre (que no es uniforme entre sitios, ver docstring del
    módulo). Se cachea en proceso. Nunca lanza: ante cualquier error devuelve None y
    loguea (un router caído/mal configurado no debe tumbar el resto de la corrida,
    mismo criterio que apps.mqtt_worker.emqx_admin.aprovisionar_credencial_estacion).
    """
    if ip in _cache_indice_interfaz:
        return _cache_indice_interfaz[ip]
    engine = SnmpEngine()
    try:
        target = await UdpTransportTarget.create((ip, puerto), timeout=3, retries=0)
        errorIndication, errorStatus, errorIndex, varBinds = await get_cmd(
            engine, CommunityData(comunidad), target, ContextData(),
            ObjectType(ObjectIdentity(_OID_IP_ROUTE_IF_INDEX_DEFAULT)),
        )
    except Exception:
        logger.warning('Mikrotik %s: excepción resolviendo la interfaz WAN.', ip, exc_info=True)
        return None
    if errorIndication or errorStatus:
        logger.warning('Mikrotik %s: error resolviendo la interfaz WAN (%s).', ip, errorIndication or errorStatus)
        return None
    try:
        indice = int(varBinds[0][1])
    except (IndexError, ValueError, TypeError):
        logger.warning('Mikrotik %s: respuesta SNMP con forma inesperada resolviendo la interfaz WAN.', ip)
        return None
    _cache_indice_interfaz[ip] = indice
    return indice


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


async def _sondear_farmacia(farmacia, puerto):
    """(farmacia, bytes_recibidos, bytes_enviados) o None si no se pudo sondear —
    nunca lanza. Punto único que patchean los tests (evita hardware SNMP real)."""
    comunidad = _comunidad_para(farmacia)
    indice = await _resolver_indice_interfaz_wan(farmacia.ip_router, comunidad, puerto)
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
    resto de la corrida. Sin ninguna Farmacia con `ip_router` cargada, no hace nada.
    Devuelve cuántas farmacias se sondearon con éxito."""
    from apps.catalogo.models import Farmacia
    from apps.monitoreo.models import MuestraRedFarmacia

    # GenericIPAddressField normaliza '' a None al guardar (get_prep_value) — un
    # excluir aparte por '' no solo es redundante, en SQLite "columna = NULL" nunca es
    # verdadero, así que .exclude(ip_router='') con NULL de por medio termina
    # excluyendo TODAS las filas, incluidas las que sí tienen una IP real (bug real
    # encontrado probando esto a mano). __isnull=True alcanza solo.
    farmacias = list(Farmacia.objects.exclude(ip_router__isnull=True))
    if not farmacias:
        return 0
    puerto = _puerto()

    async def _sondear_todas():
        semaforo = asyncio.Semaphore(_MAX_SONDEOS_CONCURRENTES)

        async def _con_limite(farmacia):
            async with semaforo:
                return await _sondear_farmacia(farmacia, puerto)

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


def sondear_y_guardar_farmacia(farmacia) -> bool:
    """Sondea UNA farmacia por SNMP desde este servidor y persiste la muestra.

    Extraído de `sincronizar_ancho_banda_farmacias` para que el botón "Pedir lectura
    ahora" del panel use exactamente el mismo camino que la tarea periódica, en vez de
    una segunda implementación que se desincronice.

    Devuelve True si el router respondió y se guardó la muestra.

    Ojo con la primera muestra de una farmacia: se guarda con `red_*_kbps` en null,
    porque la tasa se calcula diferenciando contra la anterior y no hay ninguna. Eso no
    es un fallo — el valor aparece recién en la segunda lectura.
    """
    from apps.monitoreo.models import MuestraRedFarmacia

    resultado = asyncio.run(_sondear_farmacia(farmacia, _puerto()))
    if resultado is None:
        return False
    _f, bytes_recibidos, bytes_enviados = resultado
    rx, tx = _calcular_tasa(farmacia, bytes_recibidos, bytes_enviados)
    MuestraRedFarmacia.objects.create(
        farmacia=farmacia, bytes_recibidos=bytes_recibidos, bytes_enviados=bytes_enviados,
        red_recibido_kbps=rx, red_enviado_kbps=tx,
    )
    return True


def solicitar_sondeo_red_farmacias_via_agente() -> int:
    """Celery Beat periódico (cada 5 min, ver CELERY_BEAT_SCHEDULE): por cada
    Farmacia con `ip_router` y al menos una Estacion aprobada y en línea, le pide a
    ESA estación (misma LAN que el Mikrotik del sitio) que lo sondee por SNMP y
    reporte por MQTT -- ver apps.catalogo.services.enviar_consultar_red_farmacia y
    apps.mqtt_worker.services.manejar_red_farmacia.

    Reemplaza en la práctica a sincronizar_ancho_banda_farmacias (sondeo directo
    desde ESTE servidor): confirmado el 24-ago-2026 que el servidor no tiene ninguna
    ruta de red hacia las IPs privadas de las farmacias (100% de pérdida de ping,
    sin entrada en la tabla de rutas del host) -- esa función nunca puede funcionar
    desde acá. Una estación de la propia farmacia sí puede, porque está en la misma
    LAN que su Mikrotik. Se deja sincronizar_ancho_banda_farmacias sin borrar (no
    hace daño, solo loguea warnings) por si algún día existe una ruta VPN real y
    vuelve a tener sentido correrla en paralelo.

    Devuelve cuántas estaciones recibieron el pedido (no confirma que hayan podido
    sondear su router -- eso se ve en manejar_red_farmacia/MuestraRedFarmacia)."""
    from apps.catalogo.models import Estacion
    from apps.catalogo.services import enviar_consultar_red_farmacia

    candidatas = Estacion.objects.filter(
        estado_aprobacion=Estacion.EstadoAprobacion.APROBADA, estado_conexion=Estacion.EstadoConexion.ONLINE,
    ).exclude(farmacia__ip_router__isnull=True).select_related('farmacia').order_by('farmacia_id', 'codigo')

    farmacias_vistas = set()
    enviadas = 0
    for estacion in candidatas:
        if estacion.farmacia_id in farmacias_vistas:
            continue
        farmacias_vistas.add(estacion.farmacia_id)
        if enviar_consultar_red_farmacia(estacion, _comunidad_para(estacion.farmacia)):
            enviadas += 1
    return enviadas


# --- Identidad del equipo de borde (ver monitoreo.models.EquipoBordeFarmacia) ---
#
# Los cuatro primeros son estándar (RFC 1213) y los contesta cualquier equipo con SNMP.
# Los dos últimos son de la rama privada de Mikrotik (enterprise 14988): verificados
# contra GNB01, MC001 y MCAR3 el 15-sep-2026, los tres responden.
#
# NO se piden temperatura (14988.1.1.3.10), voltaje (…3.8) ni memoria (hrStorage):
# ninguno de los tres equipos los contesta. Son RB941/RB951, gama baja sin esos
# sensores — pedirlos sería pagar la latencia de un OID que nunca va a traer nada.
_OID_SYS_DESCR = '1.3.6.1.2.1.1.1.0'
_OID_SYS_UPTIME = '1.3.6.1.2.1.1.3.0'
_OID_SYS_NAME = '1.3.6.1.2.1.1.5.0'
_OID_MTXR_VERSION = '1.3.6.1.4.1.14988.1.1.4.4.0'
_OID_MTXR_SERIE = '1.3.6.1.4.1.14988.1.1.7.3.0'

# sysUpTime viene en centésimas de segundo (TimeTicks), no en segundos.
_CENTESIMAS_POR_SEGUNDO = 100


async def _leer_identidad(ip, comunidad, puerto):
    """`{'modelo':…, 'numero_serie':…, …}` o None. Nunca lanza.

    Los OID privados de Mikrotik se piden en el mismo GET que los estándar: si el equipo
    no los conoce devuelve "No Such Object" para esos y contesta igual los otros, así que
    un equipo que no sea Mikrotik igual entrega modelo, nombre y uptime.
    """
    engine = SnmpEngine()
    try:
        target = await UdpTransportTarget.create((ip, puerto), timeout=3, retries=0)
        errorIndication, errorStatus, _errorIndex, varBinds = await get_cmd(
            engine, CommunityData(comunidad), target, ContextData(),
            ObjectType(ObjectIdentity(_OID_SYS_DESCR)),
            ObjectType(ObjectIdentity(_OID_SYS_UPTIME)),
            ObjectType(ObjectIdentity(_OID_SYS_NAME)),
            ObjectType(ObjectIdentity(_OID_MTXR_VERSION)),
            ObjectType(ObjectIdentity(_OID_MTXR_SERIE)),
        )
    except Exception:
        logger.warning('Mikrotik %s: excepción leyendo la identidad.', ip, exc_info=True)
        return None
    if errorIndication or errorStatus:
        logger.warning('Mikrotik %s: error leyendo la identidad (%s).', ip, errorIndication or errorStatus)
        return None

    valores = [_texto_snmp(v) for _n, v in varBinds]
    if len(valores) != 5:
        logger.warning('Mikrotik %s: respuesta de identidad con forma inesperada.', ip)
        return None

    descr, uptime, nombre, version, serie = valores
    return {
        'modelo': descr,
        'uptime_segundos': int(uptime) // _CENTESIMAS_POR_SEGUNDO if uptime.isdigit() else None,
        'nombre_sistema': nombre,
        'version_routeros': version,
        'numero_serie': serie,
    }


def _texto_snmp(valor) -> str:
    """El texto de una variable SNMP, o '' si el equipo no conoce ese OID.

    pysnmp devuelve marcadores como "No Such Object currently exists at this OID" en vez
    de un error cuando el OID no existe en ese equipo: sin filtrarlos, esa frase
    terminaría guardada como el número de serie.
    """
    texto = str(valor).strip()
    if not texto or 'No Such' in texto or texto == 'No more variables left in this MIB View':
        return ''
    return texto


def sincronizar_identidad_equipos(farmacias=None) -> dict:
    """Lee la identidad de los equipos de borde y la guarda en EquipoBordeFarmacia.

    `farmacias` acota a un subconjunto; vacío = todas las que tengan `ip_router`.

    A diferencia de `sincronizar_ancho_banda_farmacias`, esto no necesita correr cada 5
    minutos: el número de serie no cambia nunca y la versión de RouterOS solo cuando
    alguien actualiza. Pensado para una corrida diaria o a pedido.

    Devuelve un resumen con qué se leyó y qué no. Un equipo que no responde no es un
    error de la corrida: puede estar apagado o sin SNMP habilitado (a 15-sep-2026, 696
    de 700 no lo tienen).
    """
    from apps.monitoreo.models import EquipoBordeFarmacia, ReinicioEquipoBorde

    if farmacias is None:
        from apps.catalogo.models import Farmacia
        farmacias = Farmacia.objects.exclude(ip_router__isnull=True).order_by('codigo')

    puerto = _puerto()
    resumen = {
        'leidos': 0, 'sin_responder': 0, 'nombres_discrepantes': [], 'versiones': {},
        'reinicios': [],
    }

    async def _todas():
        limite = asyncio.Semaphore(_MAX_SONDEOS_CONCURRENTES)

        async def _una(farmacia):
            async with limite:
                datos = await _leer_identidad(str(farmacia.ip_router), _comunidad_para(farmacia), puerto)
                return farmacia, datos

        return await asyncio.gather(*(_una(f) for f in farmacias))

    previos = {
        e.farmacia_id: e.uptime_segundos
        for e in EquipoBordeFarmacia.objects.filter(farmacia__in=farmacias)
    }

    for farmacia, datos in asyncio.run(_todas()):
        if datos is None:
            resumen['sin_responder'] += 1
            continue

        ahora = timezone.now()
        uptime = datos.get('uptime_segundos')
        anterior = previos.get(farmacia.pk)
        # Un uptime MENOR que el de la lectura anterior solo puede significar que el
        # equipo arrancó de nuevo entremedio. Es más confiable que mirar "uptime chico":
        # eso depende de cada cuánto se sondee, y con un intervalo largo un reinicio
        # pasaría desapercibido.
        if uptime is not None and anterior is not None and uptime < anterior:
            ReinicioEquipoBorde.objects.create(
                farmacia=farmacia,
                arranque_estimado=ahora - timedelta(seconds=uptime),
                uptime_previo_segundos=anterior,
                version_routeros=datos.get('version_routeros', ''),
            )
            resumen['reinicios'].append(
                '%s: arrancó hace %.0f min (venía de %.1f h encendido)'
                % (farmacia.codigo, uptime / 60, anterior / 3600),
            )

        equipo, _creado = EquipoBordeFarmacia.objects.update_or_create(
            farmacia=farmacia, defaults={**datos, 'ultima_lectura': ahora},
        )
        resumen['leidos'] += 1
        if equipo.version_routeros:
            resumen['versiones'][equipo.version_routeros] = (
                resumen['versiones'].get(equipo.version_routeros, 0) + 1
            )
        if equipo.nombre_coincide is False:
            resumen['nombres_discrepantes'].append(
                '%s: el equipo dice llamarse "%s"' % (farmacia.codigo, equipo.nombre_sistema),
            )
    return resumen


# --- Descubrimiento por tabla ARP (ver monitoreo.models.DispositivoDetectado) ---
#
# IP-MIB, ipNetToMediaPhysAddress: la tabla ARP del router, o sea IP -> MAC de todo lo
# que habló por su LAN. El índice del OID trae el ifIndex y la IP:
#
#     1.3.6.1.2.1.4.22.1.2 . 8 . 10.101.41.194   ->  0xd0ad08586165
#                            ^              ^
#                            ifIndex        IP
#
# Verificado contra MCAR3 el 15-sep-2026: devolvió 8 entradas, una del gateway del
# proveedor por ether3_TELCO y siete de la LAN.
_OID_ARP = '1.3.6.1.2.1.4.22.1.2'

# Tope por farmacia: una LAN de farmacia tiene decenas de equipos, no miles. Si un
# router devolviera muchísimo más sería una red mal segmentada (o la tabla ARP de un
# equipo que no es el de esta farmacia), y traerlo entero solo llenaría la base de ruido.
_MAX_ENTRADAS_ARP = 200


def _normalizar_mac(valor) -> str:
    """`0xd0ad08586165` -> `D0:AD:08:58:61:65`.

    Se normaliza al mismo formato que valida `Activo.mac` para que el cruce entre lo
    descubierto y lo declarado sea una comparación de texto y no una función.

    `prettyPrint()` y NO `str()`: para un OctetString de pysnmp —que es lo que llega del
    cable— `str()` devuelve los bytes decodificados (basura binaria) y solo
    `prettyPrint()` da la forma hexadecimal `0x…`. Con `str()` esto devolvía '' para
    TODAS las MAC y el descubrimiento encontraba cero equipos, sin error: la primera
    corrida en producción (15-sep-2026) leyó las 4 farmacias y registró 0 dispositivos.
    """
    texto = (valor.prettyPrint() if hasattr(valor, 'prettyPrint') else str(valor)).strip().lower()
    if texto.startswith('0x'):
        texto = texto[2:]
    crudo = texto.replace(':', '').replace('-', '').replace(' ', '')
    if len(crudo) != 12 or not all(c in '0123456789abcdef' for c in crudo):
        return ''
    return ':'.join(crudo[i:i + 2] for i in range(0, 12, 2)).upper()


def _ip_e_indice_desde_oid(oid: str):
    """`…4.22.1.2.8.10.101.41.194` -> `(8, '10.101.41.194')`, o `(None, None)`.

    El sufijo es ifIndex seguido de los cuatro octetos de la IP. Se parsea desde el final
    para no depender de cuántos componentes tenga el prefijo según cómo lo formatee
    pysnmp.
    """
    partes = str(oid).split('.')
    if len(partes) < 5:
        return None, None
    octetos = partes[-4:]
    if not all(p.isdigit() and 0 <= int(p) <= 255 for p in octetos):
        return None, None
    indice = partes[-5]
    return (int(indice) if indice.isdigit() else None), '.'.join(octetos)


async def _leer_tabla_arp(ip, comunidad, puerto):
    """`[(mac, ip, ifIndex), …]` o None si no se pudo consultar. Nunca lanza."""
    engine = SnmpEngine()
    filas = []
    try:
        target = await UdpTransportTarget.create((ip, puerto), timeout=4, retries=0)
        async for errorIndication, errorStatus, _errorIndex, varBinds in bulk_walk_cmd(
            engine, CommunityData(comunidad), target, ContextData(),
            0, 20, ObjectType(ObjectIdentity(_OID_ARP)), lexicographicMode=False,
        ):
            if errorIndication or errorStatus:
                logger.warning(
                    'Mikrotik %s: error recorriendo la tabla ARP (%s).',
                    ip, errorIndication or errorStatus,
                )
                return None
            for nombre, valor in varBinds:
                mac = _normalizar_mac(valor)
                indice, ip_vista = _ip_e_indice_desde_oid(nombre)
                if mac and ip_vista:
                    filas.append((mac, ip_vista, indice))
            if len(filas) >= _MAX_ENTRADAS_ARP:
                logger.warning(
                    'Mikrotik %s: la tabla ARP superó %d entradas, se corta.', ip, _MAX_ENTRADAS_ARP,
                )
                break
    except Exception:
        logger.warning('Mikrotik %s: excepción recorriendo la tabla ARP.', ip, exc_info=True)
        return None
    return filas


def sincronizar_dispositivos_detectados(farmacias=None) -> dict:
    """Recorre la tabla ARP de cada Mikrotik y registra lo que ve en `DispositivoDetectado`.

    Una fila por MAC y farmacia: si el equipo cambió de IP (DHCP), se actualiza la IP y
    `visto_por_ultima_vez`. Un equipo que se desconecta NO se borra — deja de
    actualizarse, que es lo que permite notar después que algo desapareció.

    Devuelve el resumen, incluidos los que no matchean con ningún `Activo` declarado:
    eso es equipo enchufado que nadie inventarió.
    """
    from apps.monitoreo.models import DispositivoDetectado

    if farmacias is None:
        from apps.catalogo.models import Farmacia
        farmacias = Farmacia.objects.exclude(ip_router__isnull=True).order_by('codigo')

    puerto = _puerto()
    resumen = {'farmacias_leidas': 0, 'sin_responder': 0, 'nuevos': 0, 'actualizados': 0, 'sin_declarar': []}

    async def _todas():
        limite = asyncio.Semaphore(_MAX_SONDEOS_CONCURRENTES)

        async def _una(farmacia):
            async with limite:
                filas = await _leer_tabla_arp(str(farmacia.ip_router), _comunidad_para(farmacia), puerto)
                return farmacia, filas

        return await asyncio.gather(*(_una(f) for f in farmacias))

    ahora = timezone.now()
    for farmacia, filas in asyncio.run(_todas()):
        if filas is None:
            resumen['sin_responder'] += 1
            continue
        resumen['farmacias_leidas'] += 1
        for mac, ip_vista, indice in filas:
            _dispositivo, creado = DispositivoDetectado.objects.update_or_create(
                farmacia=farmacia, mac=mac,
                defaults={'ip': ip_vista, 'interfaz_indice': indice, 'visto_por_ultima_vez': ahora},
            )
            resumen['nuevos' if creado else 'actualizados'] += 1

    # El cruce contra lo declarado se hace con DOS consultas y no llamando a
    # `activo_declarado` por dispositivo: a 700 farmacias con decenas de equipos cada una
    # serían miles de consultas (el mismo N+1 que la auditoría sacó de monitoreo_lista).
    from apps.activos.models import Activo

    declaradas = {
        (farmacia_id, (mac or '').upper())
        for farmacia_id, mac in Activo.objects.filter(farmacia__in=farmacias)
        .exclude(mac='').values_list('farmacia_id', 'mac')
    }
    for dispositivo in DispositivoDetectado.objects.filter(
        farmacia__in=farmacias,
    ).select_related('farmacia'):
        if (dispositivo.farmacia_id, dispositivo.mac.upper()) not in declaradas:
            resumen['sin_declarar'].append(
                '%s %s (%s)' % (dispositivo.farmacia.codigo, dispositivo.ip, dispositivo.mac),
            )
    return resumen
