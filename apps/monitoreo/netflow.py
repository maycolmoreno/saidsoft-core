"""NetFlow de los Mikrotik de farmacia: QUÉ equipo consume el enlace y CONTRA QUÉ destino.

PILOTO (18-oct-2026) con ML001, ML004 y MH001 — ver PLAN_MODERNIZACION.md §10-CQ. Lo que
el piloto tiene que contestar antes de pensar en 700 farmacias es cuánto cuesta: cuánto
tráfico agrega la exportación sobre el enlace de cada farmacia (`bytes_exportacion` contra
`bytes_medidos` en `VentanaNetflowFarmacia`) y cuántas filas por día hacen falta. Hasta
tener esos números, cualquier diseño para la flota entera es adivinar.

Complementa a `apps.monitoreo.mikrotik`, no lo reemplaza: el SNMP dice CUÁNTO pasa por el
enlace (`MuestraRedFarmacia`); esto dice QUIÉN y HACIA DÓNDE. Mismo criterio de sitio que
ese módulo — la farmacia sale del router que exporta (`Farmacia.ip_router`) — y misma
forma de serie por sitio, sin forzarlo en `EstadoDispositivo`/`Alerta`.

Se guardan RESÚMENES, no flujos crudos. Cada `VENTANA_SEGUNDOS` se vuelca por farmacia el
top `MAX_FILAS_POR_VENTANA` de pares (equipo local, destino, puerto) por bytes, más una
fila "resto" con lo que quedó afuera, para que la suma siga cuadrando con el total.

La decodificación es de la librería `netflow` (v5, v9 e IPFIX, Python puro). Dos cosas de
ella que este módulo esquiva a propósito, ver `AgregadorNetflow.procesar`:
- Guarda las plantillas de v9/IPFIX en un dict por id, y los Mikrotik de todas las
  farmacias usan los mismos ids (256, 257...). Con un dict compartido, la plantilla de un
  router decodificaría los datos de otro. Va uno por exportador.
- Si recibe `bytes` que por casualidad son UTF-8 válido, los interpreta como texto
  hexadecimal y revienta. Se le pasa un `bytearray`, que no entra en esa rama.
"""
import ipaddress
import logging
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import timedelta

from django.db import transaction
from django.utils import timezone
from netflow import parse_packet
from netflow.ipfix import IPFIXTemplateNotRecognized
from netflow.utils import get_export_version
from netflow.v9 import V9TemplateNotRecognized

logger = logging.getLogger(__name__)

PUERTO_COLECTOR = 2055
VENTANA_SEGUNDOS = 300
MAX_FILAS_POR_VENTANA = 50
DIAS_RETENCION = 14

# Nombre de cada campo según la versión: (origen, destino, protocolo, puerto origen,
# puerto destino, bytes, paquetes). La librería no los unifica.
_CAMPOS_POR_VERSION = {
    5: ('IPV4_SRC_ADDR', 'IPV4_DST_ADDR', 'PROTO', 'SRC_PORT', 'DST_PORT', 'IN_OCTETS', 'IN_PACKETS'),
    9: ('IPV4_SRC_ADDR', 'IPV4_DST_ADDR', 'PROTOCOL', 'L4_SRC_PORT', 'L4_DST_PORT', 'IN_BYTES', 'IN_PKTS'),
    10: (
        'sourceIPv4Address', 'destinationIPv4Address', 'protocolIdentifier', 'sourceTransportPort',
        'destinationTransportPort', 'octetDeltaCount', 'packetDeltaCount',
    ),
}


@dataclass(frozen=True)
class Flujo:
    origen: str
    destino: str
    protocolo: int
    puerto_origen: int
    puerto_destino: int
    bytes: int
    paquetes: int


def normalizar(datos: dict, version: int) -> Flujo | None:
    """Un registro de la librería a `Flujo`, o None si no trae lo necesario (un flujo
    IPv6 no tiene los campos IPv4, por ejemplo)."""
    campos = _CAMPOS_POR_VERSION.get(version)
    if campos is None:
        return None
    try:
        origen, destino, protocolo, p_origen, p_destino, cant_bytes, paquetes = (datos[c] for c in campos)
        # v5 trae las IP como entero y v9/IPFIX como texto: ip_address acepta las dos.
        return Flujo(
            origen=str(ipaddress.ip_address(origen)), destino=str(ipaddress.ip_address(destino)),
            protocolo=int(protocolo), puerto_origen=int(p_origen), puerto_destino=int(p_destino),
            bytes=int(cant_bytes), paquetes=int(paquetes),
        )
    except (KeyError, ValueError, TypeError):
        return None


def _es_privada(ip: str) -> bool:
    return ipaddress.ip_address(ip).is_private


def red_lan(ip: str) -> ipaddress.IPv4Network | None:
    """La /24 de una IP de la LAN de la farmacia. /24 porque es la máscara de las LAN de
    farmacia que se vieron (ML008: router 10.101.22.97, cajas 10.101.22.99 a .101); si una
    farmacia tuviera otra, quedaría a medias y se vería en el reporte."""
    try:
        return ipaddress.ip_network(f'{ip}/24', strict=False)
    except ValueError:
        return None


def orientar(flujo: Flujo, redes: tuple = ()) -> tuple[str, str, int, bool] | None:
    """`(ip_local, ip_remota, puerto_remoto, es_subida)`, o None si no hay lado local.

    Con `redes` (la LAN de la farmacia), local es el lado que cae adentro. Sin ellas, el
    lado privado. Esto último no alcanza en esta red: la WAN entre farmacias y oficinas es
    toda privada, y en el primer día de ML008 (19-oct-2026) un Winbox desde una oficina
    (10.100.100.102) salió como "el equipo de la farmacia que más consume".

    Cuando los dos lados son locales (o privados, sin redes) se toma como remoto el del
    puerto más bajo: es el que da el servicio. Ningún lado local es tránsito y se descarta.
    """
    if redes:
        def es_local(ip):
            direccion = ipaddress.ip_address(ip)
            return any(direccion in red for red in redes)
    else:
        es_local = _es_privada
    origen_privado, destino_privado = es_local(flujo.origen), es_local(flujo.destino)
    if origen_privado and destino_privado:
        if flujo.puerto_origen < flujo.puerto_destino:
            return flujo.destino, flujo.origen, flujo.puerto_origen, False
        return flujo.origen, flujo.destino, flujo.puerto_destino, True
    if origen_privado:
        return flujo.origen, flujo.destino, flujo.puerto_destino, True
    if destino_privado:
        return flujo.destino, flujo.origen, flujo.puerto_origen, False
    return None


@dataclass
class _Acumulado:
    datagramas: int = 0
    bytes_exportacion: int = 0
    datagramas_sin_plantilla: int = 0
    flujos: int = 0
    flujos_descartados: int = 0
    flujos_del_router: int = 0
    bytes_medidos: int = 0
    # (ip_local, ip_remota, protocolo, puerto_remoto) -> [bytes_subida, bytes_bajada, paquetes]
    pares: dict = field(default_factory=lambda: defaultdict(lambda: [0, 0, 0]))


class AgregadorNetflow:
    """Acumula en memoria lo que llega entre dos volcados. No toca la base: eso es
    `volcar`, así esta clase se prueba sin Postgres y el colector decide cuándo escribir."""

    def __init__(self, farmacia_por_ip: dict[str, int], contexto: dict | None = None):
        self.farmacia_por_ip = farmacia_por_ip
        # farmacia_id -> (redes LAN, IP del router). Ver `contexto_de_farmacias`.
        self.contexto = contexto or {}
        # Por exportador: ver el docstring del módulo.
        self._plantillas = defaultdict(lambda: {'netflow': {}, 'ipfix': {}})
        self.reiniciar()

    def reiniciar(self):
        self.por_farmacia: dict[int, _Acumulado] = defaultdict(_Acumulado)
        self.exportadores_desconocidos: Counter = Counter()

    def procesar(self, exportador_ip: str, datos: bytes) -> None:
        farmacia_id = self.farmacia_por_ip.get(exportador_ip)
        if farmacia_id is None:
            # Un router que exporta pero que no se reconoce: o su IP de salida no es
            # `ip_router` (ver NETFLOW_EXPORTADORES en run_netflow_collector), o es alguien
            # que no tiene por qué mandar flujos acá. En los dos casos no se decodifica.
            self.exportadores_desconocidos[exportador_ip] += 1
            return
        acumulado = self.por_farmacia[farmacia_id]
        acumulado.datagramas += 1
        acumulado.bytes_exportacion += len(datos)
        try:
            version = get_export_version(datos)
            paquete = parse_packet(bytearray(datos), self._plantillas[exportador_ip])
        except (V9TemplateNotRecognized, IPFIXTemplateNotRecognized):
            # Normal los primeros minutos: el router manda la plantilla cada tanto y los
            # datos que llegan antes no se pueden leer. Se cuenta, no se reintenta.
            acumulado.datagramas_sin_plantilla += 1
            return
        except Exception:
            logger.debug('Datagrama NetFlow ilegible de %s', exportador_ip, exc_info=True)
            acumulado.datagramas_sin_plantilla += 1
            return

        redes, ips_router = self.contexto.get(farmacia_id, ((), frozenset()))
        for registro in paquete.flows:
            flujo = normalizar(registro.data, version)
            if flujo and (flujo.origen in ips_router or flujo.destino in ips_router):
                # El router en una punta: Winbox, su DNS, su propia exportación hacia el
                # servidor. No es consumo de ningún equipo de la farmacia.
                acumulado.flujos_del_router += 1
                continue
            orientado = orientar(flujo, redes) if flujo else None
            if orientado is None:
                acumulado.flujos_descartados += 1
                continue
            ip_local, ip_remota, puerto_remoto, es_subida = orientado
            fila = acumulado.pares[(ip_local, ip_remota, flujo.protocolo, puerto_remoto)]
            fila[0 if es_subida else 1] += flujo.bytes
            fila[2] += flujo.paquetes
            acumulado.flujos += 1
            acumulado.bytes_medidos += flujo.bytes


def volcar(agregador: AgregadorNetflow, inicio, fin) -> int:
    """Escribe una `VentanaNetflowFarmacia` por farmacia con su top de pares y la fila
    "resto". Devuelve cuántas ventanas escribió y deja el agregador vacío."""
    from apps.monitoreo.models import FlujoNetflowAgregado, VentanaNetflowFarmacia

    escritas = 0
    with transaction.atomic():
        for farmacia_id, acumulado in agregador.por_farmacia.items():
            ventana = VentanaNetflowFarmacia.objects.create(
                farmacia_id=farmacia_id, inicio=inicio, fin=fin,
                datagramas=acumulado.datagramas, bytes_exportacion=acumulado.bytes_exportacion,
                datagramas_sin_plantilla=acumulado.datagramas_sin_plantilla,
                flujos=acumulado.flujos, flujos_descartados=acumulado.flujos_descartados,
                flujos_del_router=acumulado.flujos_del_router,
                bytes_medidos=acumulado.bytes_medidos,
            )
            ordenados = sorted(acumulado.pares.items(), key=lambda par: par[1][0] + par[1][1], reverse=True)
            filas = [
                FlujoNetflowAgregado(
                    ventana=ventana, ip_local=ip_local, ip_remota=ip_remota, protocolo=protocolo,
                    puerto_remoto=puerto, bytes_subida=subida, bytes_bajada=bajada, paquetes=paquetes,
                )
                for (ip_local, ip_remota, protocolo, puerto), (subida, bajada, paquetes)
                in ordenados[:MAX_FILAS_POR_VENTANA]
            ]
            resto = ordenados[MAX_FILAS_POR_VENTANA:]
            if resto:
                filas.append(FlujoNetflowAgregado(
                    ventana=ventana,
                    bytes_subida=sum(v[0] for _, v in resto), bytes_bajada=sum(v[1] for _, v in resto),
                    paquetes=sum(v[2] for _, v in resto),
                ))
            FlujoNetflowAgregado.objects.bulk_create(filas)
            escritas += 1
    agregador.reiniciar()
    return escritas


def farmacias_por_ip_exportador(extra: dict[str, str] | None = None) -> dict[str, int]:
    """IP de origen de los datagramas -> id de farmacia. Por defecto `ip_router`, la
    misma IP con la que el servidor ya consulta SNMP. `extra` ({ip: codigo_farmacia})
    cubre el router que sale hacia el servidor con otra dirección."""
    from apps.catalogo.models import Farmacia

    mapa = {
        str(ip): pk for ip, pk in Farmacia.objects.exclude(ip_router__isnull=True).values_list('ip_router', 'pk')
    }
    if extra:
        por_codigo = dict(Farmacia.objects.filter(codigo__in=extra.values()).values_list('codigo', 'pk'))
        for ip, codigo in extra.items():
            if codigo in por_codigo:
                mapa[ip] = por_codigo[codigo]
            else:
                logger.warning('NETFLOW_EXPORTADORES: no existe la farmacia %s (para %s).', codigo, ip)
    return mapa


def contexto_de_farmacias(farmacia_por_ip: dict[str, int]) -> dict:
    """farmacia_id -> (redes LAN, IP del router) para las farmacias que exportan.

    La LAN sale de la /24 de `ip_router` y de la de cada `Estacion.ip_lan` de la farmacia.
    Las IP del router son `ip_router` más las direcciones desde las que exporta (que
    pueden ser otras, ver NETFLOW_EXPORTADORES): por esas sale también su tráfico propio.
    """
    from apps.catalogo.models import Estacion, Farmacia

    ids = set(farmacia_por_ip.values())
    redes = defaultdict(set)
    routers = defaultdict(set)
    for pk, ip in Farmacia.objects.filter(pk__in=ids, ip_router__isnull=False).values_list('pk', 'ip_router'):
        routers[pk].add(str(ip))
        if (red := red_lan(str(ip))) is not None:
            redes[pk].add(red)
    for pk, ip in Estacion.objects.filter(farmacia_id__in=ids, ip_lan__isnull=False).values_list('farmacia_id', 'ip_lan'):
        if (red := red_lan(str(ip))) is not None:
            redes[pk].add(red)
    for ip, pk in farmacia_por_ip.items():
        routers[pk].add(ip)
    return {pk: (tuple(redes[pk]), frozenset(routers[pk])) for pk in ids}


def purgar_netflow_antiguo(dias: int = DIAS_RETENCION) -> int:
    """Tabla común y chica (no hypertable): un DELETE alcanza. Las filas de flujos se van
    por CASCADE con su ventana."""
    from apps.monitoreo.models import VentanaNetflowFarmacia

    borradas, _ = VentanaNetflowFarmacia.objects.filter(inicio__lt=timezone.now() - timedelta(days=dias)).delete()
    return borradas
