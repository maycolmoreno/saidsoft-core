"""Lectura SNMP genérica: un cliente, un catálogo declarativo y un normalizador.

Cómo encaja, y por qué no es una jerarquía de clases:

    catalogo.py     qué OID leer de cada tipo de dispositivo, como DATOS
         │
    cliente.py      leer_escalares() / recorrer_tablas()  — transporte, nada más
         │
    impresoras.py   las columnas de Printer-MIB -> lecturas con nombre
    normalizar.py   LecturaSnmp + las reglas de nivel/unidad/centinelas
         │
         ▼
    `leer(ip, comunidad, catalogo)` -> [LecturaSnmp]

El criterio de éxito es verificable: **agregar un switch o una UPS tiene que ser una
entrada en `catalogo.py`, no un archivo nuevo.** `RED` ya está ahí como prueba de eso.

Nada de este paquete toca la base de datos ni Celery: son funciones, y por eso las reglas
—que son las que es fácil equivocar— se prueban con la salida real de un equipo guardada
como fixture, sin necesidad de tener la impresora prendida.

Las dos trampas que un equipo real obligó a corregir, y que tienen prueba:

1. El índice de la tabla de suministros **no es CMYK** (en una RICOH MP C2503, `.1.2` es
   el tóner residual). Ver `impresoras.interpretar_suministros`.
2. `prtMarkerSuppliesLevel` **no es un porcentaje**: `-1/-2/-3` son centinelas de RFC 3805.
   Ver `normalizar.interpretar_nivel`.

Ver `docs/auditoria-snmp.md` para la auditoría completa y la medición del 8-oct-2026.
"""
from .catalogo import (
    CATALOGOS,
    GENERICO,
    IMPRESORA,
    RED,
    Cadencia,
    CatalogoSnmp,
    MetricaEscalar,
    MetricaIndexada,
    TablaCompuesta,
    Unidad,
    catalogo_para,
    fabricante_desde_object_id,
)
from .cliente import leer_escalares, leer_octetos, recorrer_tablas
from .lector import leer
from .normalizar import CENTINELAS, LecturaSnmp, interpretar_nivel, nombre_de_unidad

__all__ = [
    'leer',
    'LecturaSnmp',
    'CATALOGOS', 'catalogo_para', 'IMPRESORA', 'RED', 'GENERICO',
    'CatalogoSnmp', 'MetricaEscalar', 'MetricaIndexada', 'TablaCompuesta',
    'Cadencia', 'Unidad',
    'leer_escalares', 'recorrer_tablas', 'leer_octetos',
    'interpretar_nivel', 'nombre_de_unidad', 'CENTINELAS',
    'fabricante_desde_object_id',
]
