"""`leer(ip, comunidad, catalogo)` -> `[LecturaSnmp]`. El único punto de entrada.

Una función, no una jerarquía de clases. El catálogo decide qué pedir, el cliente lo pide,
los intérpretes le ponen nombre. Agregar un tipo de dispositivo es una entrada en
`catalogo.py` y nada más — eso es lo que se verifica, no lo que se promete.

Qué hace con las tres formas de métrica:

- **escalares**: todas en una sola consulta. Un OID que el equipo no conoce vuelve vacío y
  no arrastra a los demás.
- **indexadas**: recorre el OID base y produce una lectura por índice, con la clave
  resuelta de la plantilla (`bandeja.{indice}.nivel` -> `bandeja.1.2.nivel`).
- **compuestas**: recorre todas sus columnas y se las pasa a la función que declara el
  catálogo. Es el caso de la tabla de suministros, donde las columnas solo tienen sentido
  juntas.

Devuelve `None` —no una lista vacía— cuando el equipo no contestó los escalares. La
distinción importa: lista vacía significa "contestó y no publica nada de esto", `None`
significa "no hubo con quién hablar", y esas dos cosas se arreglan en lugares distintos.
"""
import logging

from .catalogo import Cadencia, Unidad
from .cliente import leer_escalares, recorrer_tablas
from .normalizar import LecturaSnmp, interpretar_nivel, nombre_de_unidad

logger = logging.getLogger(__name__)

#: Las unidades cuyo valor se interpreta como número. El resto queda como texto: un
#: `sysDescr` o el texto de la consola no son medidas, y convertirlos a float sería forzar
#: una forma que no tienen.
UNIDADES_NUMERICAS = {Unidad.PORCENTAJE, Unidad.CONTADOR, Unidad.HOJAS, Unidad.SEGUNDOS}


async def leer(ip, comunidad, catalogo, puerto=161, cadencias=None):
    """Lee `catalogo` de `ip` y devuelve lecturas normalizadas, o `None`.

    `cadencias` limita qué métricas se piden (ver `Cadencia`). Es lo que permite que el
    tóner se lea cada hora y el estado cada cinco minutos **sin dos catálogos**: es el
    mismo catálogo, filtrado. Sin filtro, se lee todo.
    """
    lecturas = []

    escalares = catalogo.metricas_escalares(cadencias)
    if escalares:
        crudos = await leer_escalares(ip, comunidad, [m.oid for m in escalares], puerto)
        if crudos is None:
            return None
        lecturas.extend(_normalizar_escalares(escalares, crudos))

    lecturas.extend(await _leer_indexadas(ip, comunidad, catalogo, puerto, cadencias))
    lecturas.extend(await _leer_compuestas(ip, comunidad, catalogo, puerto, cadencias))
    return lecturas


def por_clave(lecturas) -> dict:
    """`{clave: LecturaSnmp}`. Comodidad para quien busca una métrica puntual.

    Si dos lecturas comparten clave gana la última, que es lo que ya hace un dict — pero
    eso sería un error del catálogo o del armado de claves, no algo esperable, así que se
    avisa en vez de taparlo.
    """
    indice = {}
    for lectura in lecturas:
        if lectura.clave in indice:
            logger.warning('Dos lecturas con la clave "%s": se queda la última.', lectura.clave)
        indice[lectura.clave] = lectura
    return indice


# --- internos ---------------------------------------------------------------------

def _normalizar_escalares(metricas, crudos):
    for metrica in metricas:
        bruto = crudos.get(metrica.oid, '')
        if bruto == '':
            # El equipo no conoce ese OID. Se omite en vez de guardar un vacío: una
            # métrica ausente no es lo mismo que una métrica en cero.
            continue
        if metrica.unidad in UNIDADES_NUMERICAS:
            valor, crudo = interpretar_nivel(bruto, unidad=None)
            yield LecturaSnmp(
                clave=metrica.clave, valor=valor, crudo=crudo,
                unidad=metrica.unidad, texto='',
            )
        else:
            yield LecturaSnmp(clave=metrica.clave, unidad=metrica.unidad, texto=str(bruto))


async def _leer_indexadas(ip, comunidad, catalogo, puerto, cadencias):
    metricas = [
        m for m in catalogo.indexadas if cadencias is None or m.cadencia in cadencias
    ]
    if not metricas:
        return []
    tablas = await recorrer_tablas(ip, comunidad, [m.oid for m in metricas], puerto)
    if tablas is None:
        return []

    lecturas = []
    for metrica, filas in zip(metricas, tablas):
        for indice, bruto in sorted(filas.items()):
            clave = metrica.clave.format(indice=indice)
            if metrica.unidad in UNIDADES_NUMERICAS:
                valor, crudo = interpretar_nivel(bruto, unidad=None)
                lecturas.append(LecturaSnmp(
                    clave=clave, valor=valor, crudo=crudo, unidad=metrica.unidad,
                ))
            else:
                lecturas.append(LecturaSnmp(clave=clave, unidad=metrica.unidad, texto=str(bruto)))
    return lecturas


async def _leer_compuestas(ip, comunidad, catalogo, puerto, cadencias):
    lecturas = []
    for tabla in catalogo.compuestas:
        if cadencias is not None and tabla.cadencia not in cadencias:
            continue
        columnas = await recorrer_tablas(ip, comunidad, list(tabla.oids), puerto)
        if columnas is None:
            continue
        try:
            lecturas.extend(tabla.interpretar(columnas))
        except Exception:
            # Un intérprete que falla no puede llevarse el resto de la lectura: el tóner
            # sirve aunque las alertas no se hayan podido interpretar.
            logger.warning(
                'SNMP %s: falló el intérprete de la tabla "%s".', ip, tabla.nombre, exc_info=True,
            )
    return lecturas


__all__ = ['leer', 'por_clave', 'Cadencia', 'nombre_de_unidad']
