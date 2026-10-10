"""Transporte SNMP: leer escalares y recorrer tablas. Nada más.

No sabe de impresoras, ni de catálogos, ni de la base de datos. Dos funciones, y las dos
devuelven `None` cuando el equipo no contestó en vez de lanzar: a este nivel "no contestó"
es un resultado, no una excepción — quien llama decide si eso es una caída, un SNMP
apagado o una community equivocada.

**Reusa `_motor_snmp` y `_texto_snmp` de `apps.monitoreo.mikrotik` a propósito**, en vez de
abrir su propio engine o convertir valores a mano. Los dos existen por bugs medidos contra
equipos reales:

- `_motor_snmp` cierra el `SnmpEngine` por cualquier camino de salida. pysnmp 7 no libera
  el socket UDP cuando el engine queda colgado: se filtraba **un descriptor por consulta**,
  ~8.400 por hora. Cualquier código que abra su propio engine reintroduce esa fuga.
- `_texto_snmp` filtra los marcadores que pysnmp devuelve como VALOR —"No Such Object
  currently exists at this OID"— y no como error. Sin ese filtro, esa frase se guarda como
  número de serie.

Son privados de otro módulo y se importan igual. Es la opción conservadora: extraerlos a
un módulo compartido obliga a tocar `mikrotik.py`, que hoy es la fuente del ancho de banda
de 252 de 270 farmacias. Mover dos funciones de ahí es un cambio que merece su propio
commit y su propia verificación, no ser el efecto colateral de agregar impresoras.
"""
import logging

from apps.monitoreo.mikrotik import _motor_snmp, _texto_snmp

logger = logging.getLogger(__name__)

#: Tres segundos es lo que ya está validado contra equipos de producción en `mikrotik.py`.
TIMEOUT_SEGUNDOS = 3
#: Un reintento, y no cero como el Mikrotik: una impresora en ahorro de energía tarda en
#: despertar, y un router no duerme. Medido: la RICOH MP C2503 contesta a la primera, pero
#: el margen cuesta un paquete y evita falsos negativos.
REINTENTOS = 1
#: Cuántas filas de una tabla se traen por pedido. Mismo valor que usa el recorrido de ARP.
FILAS_POR_PEDIDO = 20
#: Tope por tabla. Una tabla que no termina es un equipo con un agente SNMP roto, y sin
#: tope eso cuelga el ciclo entero. Mismo criterio que `_MAX_ENTRADAS_ARP`.
MAX_FILAS_POR_TABLA = 200


async def leer_escalares(ip, comunidad, oids, puerto=161, timeout=TIMEOUT_SEGUNDOS):
    """`{oid: texto}` con lo que contestó, o `None` si el equipo no contestó.

    Todos los OID van en una sola consulta: si el equipo no conoce alguno, devuelve
    "No Such Object" **para ése** y contesta igual los demás. Por eso no hace falta una
    consulta por OID, y por eso un equipo que no es impresora igual entrega su identidad.
    """
    from pysnmp.hlapi.v3arch.asyncio import (
        CommunityData, ContextData, ObjectIdentity, ObjectType, UdpTransportTarget, get_cmd,
    )

    if not oids:
        return {}
    with _motor_snmp() as engine:
        try:
            destino = await UdpTransportTarget.create(
                (ip, puerto), timeout=timeout, retries=REINTENTOS,
            )
            indicacion, estado, _indice, enlaces = await get_cmd(
                engine, CommunityData(comunidad), destino, ContextData(),
                *(ObjectType(ObjectIdentity(oid)) for oid in oids),
            )
        except Exception:
            logger.warning('SNMP %s: excepción leyendo escalares.', ip, exc_info=True)
            return None
        if indicacion or estado:
            logger.info('SNMP %s: sin respuesta (%s).', ip, indicacion or estado)
            return None
        return {oid: _texto_snmp(valor) for oid, (_nombre, valor) in zip(oids, enlaces)}


async def recorrer_tablas(ip, comunidad, bases, puerto=161, timeout=TIMEOUT_SEGUNDOS + 1):
    """Una lista de `{indice: texto}`, en el orden de `bases`. `None` si no contestó.

    El índice es lo que queda del OID después del base, que es lo que identifica la fila:
    para la tabla de suministros eso es `markerIndex.supplyIndex` ('1.1', '1.2', …).

    Una tabla que falla a mitad del recorrido devuelve lo que alcanzó a leer y sigue con la
    siguiente, en vez de descartar todo: media tabla de suministros sirve más que ninguna,
    y quien interpreta ya tiene que tolerar columnas incompletas.
    """
    from pysnmp.hlapi.v3arch.asyncio import (
        CommunityData, ContextData, ObjectIdentity, ObjectType, UdpTransportTarget,
        bulk_walk_cmd,
    )

    if not bases:
        return []
    resultado = []
    with _motor_snmp() as engine:
        try:
            destino = await UdpTransportTarget.create(
                (ip, puerto), timeout=timeout, retries=REINTENTOS,
            )
        except Exception:
            logger.warning('SNMP %s: excepción abriendo el destino.', ip, exc_info=True)
            return None

        for base in bases:
            filas = {}
            try:
                async for indicacion, estado, _indice, enlaces in bulk_walk_cmd(
                    engine, CommunityData(comunidad), destino, ContextData(),
                    0, FILAS_POR_PEDIDO, ObjectType(ObjectIdentity(base)),
                    lexicographicMode=False,
                ):
                    if indicacion or estado:
                        logger.info(
                            'SNMP %s: el recorrido de %s se cortó (%s).',
                            ip, base, indicacion or estado,
                        )
                        break
                    for nombre, valor in enlaces:
                        texto = _texto_snmp(valor)
                        if texto:
                            filas[str(nombre)[len(base) + 1:]] = texto
                    if len(filas) >= MAX_FILAS_POR_TABLA:
                        logger.warning(
                            'SNMP %s: la tabla %s superó %d filas, se corta.',
                            ip, base, MAX_FILAS_POR_TABLA,
                        )
                        break
            except Exception:
                logger.warning('SNMP %s: excepción recorriendo %s.', ip, base, exc_info=True)
            resultado.append(filas)
    return resultado


async def leer_octetos(ip, comunidad, oid, puerto=161, timeout=TIMEOUT_SEGUNDOS):
    """Los bytes crudos de un OCTET STRING, o `None`.

    Existe para `hrPrinterDetectedErrorState`, que es un BITS: convertirlo a texto con
    `str()` da caracteres no imprimibles y pierde la información. Quien lo lea tiene que
    mirar los bytes — y tener en cuenta que `0x00` NO prueba que no haya problemas (ver
    `impresoras.bitmask_sin_informacion`).
    """
    from pysnmp.hlapi.v3arch.asyncio import (
        CommunityData, ContextData, ObjectIdentity, ObjectType, UdpTransportTarget, get_cmd,
    )

    with _motor_snmp() as engine:
        try:
            destino = await UdpTransportTarget.create(
                (ip, puerto), timeout=timeout, retries=REINTENTOS,
            )
            indicacion, estado, _indice, enlaces = await get_cmd(
                engine, CommunityData(comunidad), destino, ContextData(),
                ObjectType(ObjectIdentity(oid)),
            )
            if indicacion or estado or not enlaces:
                return None
            return bytes(enlaces[0][1].asOctets())
        except Exception:
            logger.warning('SNMP %s: excepción leyendo octetos de %s.', ip, oid, exc_info=True)
            return None
