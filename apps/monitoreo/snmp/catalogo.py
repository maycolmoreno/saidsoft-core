"""Qué OID leer de cada tipo de dispositivo, de forma declarativa.

El criterio de éxito del diseño, y es verificable: **agregar un tipo de dispositivo nuevo
—switch, UPS, PDU— tiene que ser una entrada acá, no un archivo nuevo de código.** Por eso
las métricas son datos y no clases: una jerarquía `Printer → HP/Ricoh/Xerox` con cero
diferencias de comportamiento son tres clases vacías, que es justo el `PrinterMonitor` que
no queremos.

Tres formas de métrica, y ninguna se eligió por gusto:

- **`MetricaEscalar`** — un OID, un valor. Lo más común.
- **`MetricaIndexada`** — un OID base que se recorre y da un valor por índice, con la
  clave como plantilla (`bandeja.{indice}.nivel`). Resuelve de forma declarativa las
  tablas simples: bandejas de una impresora, interfaces de un switch, tomas de un PDU.
- **`TablaCompuesta`** — varias columnas que **solo tienen sentido juntas**, con una
  función que las interpreta. Existe por un caso concreto: la tabla de suministros de una
  impresora necesita cruzar tipo, colorante, nivel, máximo y unidad para saber que el
  índice `.1.2` de una RICOH MP C2503 es el tóner RESIDUAL y no el cian. Fingir que eso es
  declarativo sería esconder el problema.

Los OID van en numérico crudo, sin compilar MIBs. Compilar agrega un directorio de MIBs al
contenedor, latencia de arranque y una familia nueva de fallas, para resolver un problema
que no existe: los OID de Printer-MIB son fijos y conocidos.
"""
from dataclasses import dataclass, field
from typing import Callable

from .normalizar import UNIDAD_HOJAS, UNIDAD_PORCENTAJE


class Cadencia:
    """Cada cuánto vale la pena leer una métrica.

    No es una preferencia: es lo que acota el volumen. Sondear todo a la misma frecuencia
    es lo que hace que un diseño ande con 20 dispositivos y muera con 1.000 — con 10.000
    dispositivos y 20 métricas cada 5 minutos son ~57 millones de filas por día. Con estas
    tres clases, ~4 millones.
    """

    RAPIDA = 'rapida'        # ~5 min. Acá el valor ES la velocidad de detección.
    LENTA = 'lenta'          # ~60 min. El tóner no se mueve en cinco minutos.
    IDENTIDAD = 'identidad'  # diaria. La serie y el modelo no cambian nunca.


class Unidad:
    PORCENTAJE = 'porcentaje'
    CONTADOR = 'contador'
    HOJAS = 'hojas'
    ENUM = 'enum'
    TEXTO = 'texto'
    SEGUNDOS = 'segundos'
    #: Un OCTET STRING que son BITS y no texto. Se guarda en hex: pasarlo por el camino
    #: de texto lo dejaria vacio (`0x00` son bytes de control) y se perderia la diferencia
    #: entre "todo en ceros" y "no vino".
    BITMASK = 'bitmask'


@dataclass(frozen=True)
class MetricaEscalar:
    clave: str
    oid: str
    unidad: str = Unidad.TEXTO
    cadencia: str = Cadencia.LENTA
    #: Verdadero para contadores que solo crecen. Quien calcule un delta tiene que saber
    #: que un valor MENOR que el anterior no es una baja: es el equipo reiniciado o la
    #: placa cambiada, y entonces el delta no es computable (ver `_calcular_tasa` en
    #: mikrotik.py, que ya resolvió esto para los octetos del Mikrotik).
    monotono: bool = False


@dataclass(frozen=True)
class MetricaIndexada:
    #: Plantilla con `{indice}`, ej. `bandeja.{indice}.nivel`.
    clave: str
    oid: str
    unidad: str = Unidad.CONTADOR
    cadencia: str = Cadencia.LENTA


@dataclass(frozen=True)
class TablaCompuesta:
    #: Nombre interno, para los mensajes de error.
    nombre: str
    #: Los OID base que hay que recorrer, en el orden que espera `interpretar`.
    oids: tuple
    #: `(columnas: list[dict]) -> list[LecturaSnmp]`.
    interpretar: Callable
    cadencia: str = Cadencia.LENTA


@dataclass(frozen=True)
class CatalogoSnmp:
    nombre: str
    escalares: tuple = ()
    indexadas: tuple = ()
    compuestas: tuple = ()
    descripcion: str = ''

    def oids_escalares(self, cadencias=None):
        return tuple(
            m.oid for m in self.escalares if cadencias is None or m.cadencia in cadencias
        )

    def metricas_escalares(self, cadencias=None):
        return tuple(
            m for m in self.escalares if cadencias is None or m.cadencia in cadencias
        )


# =============================================================================
# OID estándar. Printer-MIB = RFC 3805, HOST-RESOURCES-MIB = RFC 2790,
# SNMPv2-MIB = RFC 3418, IF-MIB = RFC 2863.
# =============================================================================
OID_SYS_DESCR = '1.3.6.1.2.1.1.1.0'
OID_SYS_OBJECT_ID = '1.3.6.1.2.1.1.2.0'
OID_SYS_UPTIME = '1.3.6.1.2.1.1.3.0'
OID_SYS_NAME = '1.3.6.1.2.1.1.5.0'

OID_PRT_SERIE = '1.3.6.1.2.1.43.5.1.1.17.1'
OID_PRT_CONTADOR = '1.3.6.1.2.1.43.10.2.1.4.1.1'
OID_PRT_CONTADOR_UNIDAD = '1.3.6.1.2.1.43.10.2.1.3.1.1'
OID_PRT_CONSOLA = '1.3.6.1.2.1.43.16.5.1.2.1.1'

OID_SUP_CLASE = '1.3.6.1.2.1.43.11.1.1.4'
OID_SUP_TIPO = '1.3.6.1.2.1.43.11.1.1.5'
OID_SUP_DESCR = '1.3.6.1.2.1.43.11.1.1.6'
OID_SUP_UNIDAD = '1.3.6.1.2.1.43.11.1.1.7'
OID_SUP_MAXIMO = '1.3.6.1.2.1.43.11.1.1.8'
OID_SUP_NIVEL = '1.3.6.1.2.1.43.11.1.1.9'
OID_COLORANTE = '1.3.6.1.2.1.43.12.1.1.4'

OID_BANDEJA_NIVEL = '1.3.6.1.2.1.43.8.2.1.10'
OID_ALERTA_DESCR = '1.3.6.1.2.1.43.18.1.1.8'

OID_HR_DEVICE_STATUS = '1.3.6.1.2.1.25.3.2.1.5.1'
OID_HR_PRINTER_STATUS = '1.3.6.1.2.1.25.3.5.1.1.1'
#: Cuidado con éste: una RICOH MP C2503 real devolvió 0x00 **estando sin papel**
#: (8-oct-2026). Se lee para registrarlo, NUNCA como prueba de que no hay problemas.
OID_HR_ERROR_STATE = '1.3.6.1.2.1.25.3.5.1.2.1'

OID_IF_DESCR = '1.3.6.1.2.1.2.2.1.2'
OID_IF_HC_IN = '1.3.6.1.2.1.31.1.1.1.6'
OID_IF_HC_OUT = '1.3.6.1.2.1.31.1.1.1.10'
OID_IF_OPER_STATUS = '1.3.6.1.2.1.2.2.1.8'


# --- Lo que comparte cualquier dispositivo que hable SNMP --------------------
IDENTIDAD_COMUN = (
    MetricaEscalar('sistema.descripcion', OID_SYS_DESCR, Unidad.TEXTO, Cadencia.IDENTIDAD),
    MetricaEscalar('sistema.object_id', OID_SYS_OBJECT_ID, Unidad.TEXTO, Cadencia.IDENTIDAD),
    MetricaEscalar('sistema.nombre', OID_SYS_NAME, Unidad.TEXTO, Cadencia.IDENTIDAD),
    MetricaEscalar('sistema.uptime', OID_SYS_UPTIME, Unidad.SEGUNDOS, Cadencia.RAPIDA),
)


def _suministros(columnas):
    """Import diferido para que el catálogo no dependa del intérprete al importarse."""
    from .impresoras import interpretar_suministros

    return interpretar_suministros(columnas)


def _alertas(columnas):
    from .impresoras import interpretar_alertas

    return interpretar_alertas(columnas)


IMPRESORA = CatalogoSnmp(
    nombre='IMPRESORA',
    descripcion='Printer-MIB + HOST-RESOURCES. Medido contra RICOH MP C2503 (8-oct-2026).',
    escalares=IDENTIDAD_COMUN + (
        MetricaEscalar('equipo.serie', OID_PRT_SERIE, Unidad.TEXTO, Cadencia.IDENTIDAD),
        MetricaEscalar('paginas.total', OID_PRT_CONTADOR, Unidad.CONTADOR, Cadencia.LENTA,
                       monotono=True),
        # La unidad del contador se lee SIEMPRE junto con el contador. En la Ricoh medida
        # vale 8 = HOJAS, no impresiones: a doble faz una hoja son dos impresiones, y un
        # contrato que factura por clic se equivoca hasta 2x si se confunden.
        MetricaEscalar('paginas.unidad', OID_PRT_CONTADOR_UNIDAD, Unidad.ENUM, Cadencia.LENTA),
        MetricaEscalar('equipo.consola', OID_PRT_CONSOLA, Unidad.TEXTO, Cadencia.RAPIDA),
        MetricaEscalar('estado.dispositivo', OID_HR_DEVICE_STATUS, Unidad.ENUM, Cadencia.RAPIDA),
        MetricaEscalar('estado.impresora', OID_HR_PRINTER_STATUS, Unidad.ENUM, Cadencia.RAPIDA),
        MetricaEscalar('estado.errores_bitmask', OID_HR_ERROR_STATE, Unidad.BITMASK, Cadencia.RAPIDA),
    ),
    indexadas=(
        # Esta es la que de verdad sirve para una ReglaAlerta de "sin papel": es numérica
        # y por lo tanto umbralizable, a diferencia del texto de la consola. En la Ricoh
        # medida la bandeja 2 daba 0 mientras el bitmask de errores daba 0x00.
        MetricaIndexada('bandeja.{indice}.nivel', OID_BANDEJA_NIVEL, Unidad.HOJAS, Cadencia.RAPIDA),
    ),
    compuestas=(
        TablaCompuesta(
            nombre='suministros',
            oids=(OID_SUP_TIPO, OID_SUP_DESCR, OID_SUP_UNIDAD, OID_SUP_MAXIMO,
                  OID_SUP_NIVEL, OID_COLORANTE, OID_SUP_CLASE),
            interpretar=_suministros,
            cadencia=Cadencia.LENTA,
        ),
        TablaCompuesta(
            nombre='alertas',
            oids=(OID_ALERTA_DESCR,),
            interpretar=_alertas,
            cadencia=Cadencia.RAPIDA,
        ),
    ),
)

# Switch / router. Existe para probar que el diseño aguanta un tipo nuevo sin código
# nuevo: son entradas, no archivos. Los OID de IF-MIB ya los usa `mikrotik.py`.
RED = CatalogoSnmp(
    nombre='RED',
    descripcion='IF-MIB. Mismos OID que ya usa apps.monitoreo.mikrotik.',
    escalares=IDENTIDAD_COMUN,
    indexadas=(
        MetricaIndexada('interfaz.{indice}.nombre', OID_IF_DESCR, Unidad.TEXTO, Cadencia.IDENTIDAD),
        MetricaIndexada('interfaz.{indice}.estado', OID_IF_OPER_STATUS, Unidad.ENUM, Cadencia.RAPIDA),
        MetricaIndexada('interfaz.{indice}.octetos_rx', OID_IF_HC_IN, Unidad.CONTADOR, Cadencia.RAPIDA),
        MetricaIndexada('interfaz.{indice}.octetos_tx', OID_IF_HC_OUT, Unidad.CONTADOR, Cadencia.RAPIDA),
    ),
)

# Cualquier cosa que hable SNMP y de la que todavía no se sabe qué esperar. Sirve para
# descubrir: se le lee la identidad y se mira qué contesta.
GENERICO = CatalogoSnmp(
    nombre='GENERICO',
    descripcion='Solo SNMPv2-MIB. Para descubrir qué es un equipo antes de clasificarlo.',
    escalares=IDENTIDAD_COMUN,
)

CATALOGOS = {c.nombre: c for c in (IMPRESORA, RED, GENERICO)}


def catalogo_para(nombre: str) -> CatalogoSnmp:
    try:
        return CATALOGOS[nombre]
    except KeyError:
        raise ValueError(
            'No existe el catálogo SNMP "%s". Disponibles: %s.'
            % (nombre, ', '.join(sorted(CATALOGOS))),
        )


# El enterprise de sysObjectID (`1.3.6.1.4.1.<enterprise>.…`) identifica al fabricante sin
# depender de parsear sysDescr, que es texto libre y cambia entre modelos.
FABRICANTES = {
    '11': 'HP', '236': 'Samsung', '253': 'Xerox', '367': 'Ricoh', '641': 'Lexmark',
    '1248': 'Epson', '1347': 'Kyocera', '1602': 'Canon', '2435': 'Brother',
    '14988': 'MikroTik', '8072': 'net-snmp', '311': 'Microsoft',
}


def fabricante_desde_object_id(object_id) -> str:
    """El fabricante, o '' si el OID no tiene la forma de un enterprise."""
    partes = str(object_id or '').split('.')
    if len(partes) > 6 and partes[4] == '4' and partes[5] == '1':
        return FABRICANTES.get(partes[6], 'enterprise %s' % partes[6])
    return ''


__all__ = [
    'Cadencia', 'Unidad', 'MetricaEscalar', 'MetricaIndexada', 'TablaCompuesta',
    'CatalogoSnmp', 'CATALOGOS', 'catalogo_para', 'IMPRESORA', 'RED', 'GENERICO',
    'FABRICANTES', 'fabricante_desde_object_id', 'UNIDAD_HOJAS', 'UNIDAD_PORCENTAJE',
]
