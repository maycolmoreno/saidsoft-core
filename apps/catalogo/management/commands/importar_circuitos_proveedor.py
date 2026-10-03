"""Carga el nombre del circuito en el proveedor (`Farmacia.circuito_proveedor`).

El dato viene de las planillas de enlaces de CRESIO — las mismas que usaba el sistema
de monitoreo anterior (`Cresio_enlaces`), donde la columna se llama `caracteristica`.
Es el identificador que el proveedor pide al abrir un ticket ("sangregorio2-santana"),
y hasta ahora no vivía en SAIDSOFT: había que buscarlo en un Excel aparte justo cuando
una farmacia está caída y sin vender.

Acepta los formatos que existen, porque cada unidad de negocio entregó el suyo:

- CSV SAN GREGORIO: `provincia;canton;cod_sucursal;caracteristica;proveedor;ip_proveedor`
- CSV MIA:          `Provincia;Ciudad;Codigo de farmacia;Ip Provedor;Provedor;Caracteristica`
- **XLSX de operaciones** (`DATOS DE FARMACIAS.xlsx`, hojas FARMAMIA y SAN GREGORIO):
  columnas `Id de Farmacia` + `Login`.

**Sobre el XLSX, que se agregó el 3-oct-2026 y es el camino para la carga masiva.** Ese
Excel ya se importaba — `importar_red_farmacias_xlsx` lee de él ciudad, provincia, nodo,
segmento de red, tipo de enlace, backup e IP — pero **la columna del circuito nunca se
mapeó**. El circuito estuvo todo este tiempo en el mismo archivo que ya se procesaba,
en una columna que nadie leyó, mientras quedaban 149 farmacias sin circuito y el correo
de caída salía con `circuito: -`.

Se llama `Login` y no `caracteristica` porque es otra cosecha de planilla, y ese es
justamente el motivo por el que se pasó por alto: el nombre no se parece en nada al del
campo ni al de los CSV. Medido sobre el archivo real: **702 circuitos para 702
farmacias** (FARMAMIA 309 de 311 filas, SAN GREGORIO 393 de 393), sin un solo código
repetido con dos circuitos distintos.

Solo escribe `circuito_proveedor`. NO toca `segmento_red` ni `ip_router` a propósito:
esos ya están cargados y una reimportación silenciosa podría pisarlos con un dato viejo
de planilla. Idempotente: correrlo dos veces no cambia nada la segunda.

    python manage.py importar_circuitos_proveedor <archivo.csv|.xlsx> [--aplicar]
    python manage.py importar_circuitos_proveedor "docs/DATOS DE FARMACIAS(3).xlsx" --solo-faltantes

Sin `--aplicar` solo informa qué haría (las planillas de enlaces suelen traer sucursales
que ya cerraron, y conviene verlas antes de escribir).

`--solo-faltantes` rellena únicamente las farmacias que hoy NO tienen circuito y jamás
pisa una que ya lo tenga. Es el modo para una carga masiva sobre una base que ya está en
uso: la planilla puede estar más vieja que lo que alguien corrigió a mano en el admin, y
sin este modo una carga de 700 filas revierte esas correcciones sin que nadie se entere.
"""
import csv
import re

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from apps.catalogo.models import Farmacia

# Nombre de columna -> qué significa. Se prueban en orden hasta que una calce.
FORMATOS = (
    {'codigo': 'cod_sucursal', 'circuito': 'caracteristica'},
    {'codigo': 'Codigo de farmacia', 'circuito': 'Caracteristica'},
)

# Hojas del Excel de operaciones y los encabezados a buscar en cada una. Se localizan
# por TEXTO del encabezado y no por posición fija: las dos hojas tienen las columnas en
# distinto orden (`Login` es la 7 en FARMAMIA y la 6 en SAN GREGORIO), y una columna
# nueva insertada por operaciones correría los índices sin avisar. El archivo real
# además trae encabezados vacíos repetidos al final de SAN GREGORIO, así que buscar por
# nombre es también lo único que no se confunde con ellos.
HOJAS_XLSX = ('FARMAMIA', 'SAN GREGORIO')
ENCABEZADO_CODIGO = 'id de farmacia'
ENCABEZADO_CIRCUITO = 'login'

# Lo que la planilla pone en `Login` cuando no hay dato.
VACIOS = {'', 'none', '#n/a', '-', 'n/a'}

# Circuitos que NO tienen la forma `cliente-sitio-ciudad` y conviene mirar con los ojos
# antes de darlos por buenos. No se descartan —son el identificador que ese proveedor
# pide igual, y dejar la farmacia vacía es peor— pero se listan uno por uno en la salida
# para que quien corre el comando vea exactamente qué entró. Medido sobre el archivo
# real: 696 limpios, 5 con forma de número de ticket (`PID: 20843536`,
# `ID cliente: I0244711`) y 1 con dos circuitos separados por barra.
_RE_ID_DE_TICKET = re.compile(r'^(p?id|id\s+cliente)\s*:', re.IGNORECASE)

# Mismo patron que `codigo_farmacia_validator` mas el largo del campo. El Excel de
# operaciones trae filas que no son sucursales -- "PRESIDENCIA" y "SALA CRM" son dos
# reales-- y sin este filtro se reportaban como "no existen en SAIDSOFT", que invita a
# darlas de alta. No son farmacias que falten: son otra cosa que vive en la misma hoja.
_RE_CODIGO_FARMACIA = re.compile(r'^[A-Z0-9]{1,15}$')

# Cuantos codigos desconocidos se imprimen antes de cortar. Correr esto contra una base
# vacia o equivocada produce 700 lineas de una sola tirada, y entonces el aviso que
# importa -- que NINGUNO calzo, o sea que el problema es la base y no la planilla --
# queda sepultado abajo de la lista.
_MAX_DESCONOCIDAS_LISTADAS = 25


def _texto(valor) -> str:
    return '' if valor is None else str(valor).strip()


def _es_vacio(circuito: str) -> bool:
    return circuito.lower() in VACIOS


def _leer_csv(ruta) -> list[dict]:
    """Devuelve `[{'codigo': ..., 'circuito': ...}]` desde un CSV con `;`."""
    try:
        # utf-8-sig: las planillas vienen de Excel y traen BOM; sin esto la primera
        # columna se llamaría "﻿provincia" y ningún formato calzaría.
        with open(ruta, encoding='utf-8-sig', newline='') as fh:
            filas = list(csv.DictReader(fh, delimiter=';'))
    except OSError as exc:
        raise CommandError(f'No se pudo leer {ruta}: {exc}') from exc

    if not filas:
        raise CommandError('El archivo no tiene filas.')

    columnas = set(filas[0].keys())
    formato = next((f for f in FORMATOS if set(f.values()) <= columnas), None)
    if formato is None:
        raise CommandError(
            'No reconozco las columnas. Se esperaba alguno de:\n'
            + '\n'.join(f'  {f["codigo"]} + {f["circuito"]}' for f in FORMATOS)
            + f'\n  o un .xlsx con las hojas {" / ".join(HOJAS_XLSX)}'
            + f'\nEl archivo trae: {sorted(columnas)}'
        )
    return [
        {'codigo': _texto(fila.get(formato['codigo'])), 'circuito': _texto(fila.get(formato['circuito']))}
        for fila in filas
    ]


def _leer_xlsx(ruta) -> list[dict]:
    """Igual, desde el Excel de operaciones (hojas FARMAMIA y SAN GREGORIO)."""
    import openpyxl

    try:
        libro = openpyxl.load_workbook(ruta, data_only=True, read_only=True)
    except (OSError, KeyError) as exc:
        raise CommandError(f'No se pudo abrir {ruta}: {exc}') from exc

    presentes = [h for h in HOJAS_XLSX if h in libro.sheetnames]
    if not presentes:
        libro.close()
        raise CommandError(
            f'El Excel no tiene ninguna de las hojas esperadas ({" / ".join(HOJAS_XLSX)}). '
            f'Trae: {", ".join(libro.sheetnames)}'
        )

    registros = []
    for nombre in presentes:
        hoja = libro[nombre]
        filas = hoja.iter_rows(values_only=True)
        try:
            encabezados = [_texto(c).lower() for c in next(filas)]
        except StopIteration:
            continue
        try:
            i_codigo = encabezados.index(ENCABEZADO_CODIGO)
            i_circuito = encabezados.index(ENCABEZADO_CIRCUITO)
        except ValueError:
            libro.close()
            raise CommandError(
                f'La hoja "{nombre}" no tiene las columnas "{ENCABEZADO_CODIGO}" y '
                f'"{ENCABEZADO_CIRCUITO}". Trae: {[e for e in encabezados if e]}'
            ) from None

        for fila in filas:
            registros.append({
                'codigo': _texto(fila[i_codigo] if i_codigo < len(fila) else None),
                'circuito': _texto(fila[i_circuito] if i_circuito < len(fila) else None),
            })

    libro.close()
    if not registros:
        raise CommandError('El Excel no tiene filas de datos.')
    return registros


class Command(BaseCommand):
    help = 'Carga Farmacia.circuito_proveedor desde una planilla de enlaces (CSV con ";" o el XLSX de operaciones).'

    def add_arguments(self, parser):
        parser.add_argument('archivo', help='CSV separado por ";" o .xlsx de operaciones.')
        parser.add_argument(
            '--aplicar', action='store_true',
            help='Escribe los cambios. Sin esto solo informa qué haría.',
        )
        parser.add_argument(
            '--solo-faltantes', action='store_true',
            help='Rellena solo las farmacias sin circuito; nunca pisa una que ya lo tiene.',
        )

    def handle(self, *args, **options):
        ruta = options['archivo']
        registros = _leer_xlsx(ruta) if str(ruta).lower().endswith(('.xlsx', '.xlsm')) else _leer_csv(ruta)

        actualizadas = sin_cambio = sin_circuito = respetadas = 0
        desconocidas, a_revisar, cambios, no_son_codigos = [], [], [], []

        with transaction.atomic():
            for registro in registros:
                codigo = registro['codigo'].upper()
                circuito = registro['circuito']
                if not codigo or codigo.lower() in VACIOS:
                    continue
                if not _RE_CODIGO_FARMACIA.match(codigo):
                    no_son_codigos.append(codigo)
                    continue
                if _es_vacio(circuito):
                    sin_circuito += 1
                    continue

                farmacia = Farmacia.objects.filter(codigo=codigo).first()
                if farmacia is None:
                    # Sucursal de la planilla que no existe en SAIDSOFT (cerrada,
                    # renombrada, o todavía sin dar de alta). Se informa, no se crea:
                    # inventar una farmacia desde una planilla de enlaces sería adivinar
                    # su grupo y su unidad de negocio.
                    desconocidas.append(codigo)
                    continue
                if farmacia.circuito_proveedor == circuito:
                    sin_cambio += 1
                    continue
                if options['solo_faltantes'] and farmacia.circuito_proveedor:
                    # Ya tiene uno DISTINTO. Puede ser una corrección hecha a mano
                    # después de la planilla, así que en este modo se deja como está y se
                    # informa cuántas fueron: una carga masiva no debe revertir trabajo
                    # manual sin que nadie lo vea.
                    respetadas += 1
                    continue

                if _RE_ID_DE_TICKET.match(circuito) or '/' in circuito:
                    a_revisar.append((codigo, circuito))
                cambios.append((codigo, farmacia.circuito_proveedor, circuito))

                if options['aplicar']:
                    farmacia.circuito_proveedor = circuito
                    farmacia.save(update_fields=['circuito_proveedor'])
                actualizadas += 1

            if not options['aplicar']:
                transaction.set_rollback(True)

        verbo = 'Actualizadas' if options['aplicar'] else 'Se actualizarían'
        self.stdout.write(self.style.SUCCESS(f'{verbo}: {actualizadas} farmacia(s).'))
        if sin_cambio:
            self.stdout.write(f'Ya tenían el mismo circuito: {sin_cambio}.')
        if respetadas:
            self.stdout.write(
                f'Ya tenían OTRO circuito y no se tocaron (--solo-faltantes): {respetadas}.',
            )
        if sin_circuito:
            self.stdout.write(f'Filas sin circuito en la planilla: {sin_circuito}.')
        if no_son_codigos:
            self.stdout.write(
                f'Filas que no son codigos de farmacia y se ignoraron '
                f'({len(no_son_codigos)}): {", ".join(sorted(set(no_son_codigos)))}.',
            )
        if desconocidas:
            unicas = sorted(set(desconocidas))
            muestra = ', '.join(unicas[:_MAX_DESCONOCIDAS_LISTADAS])
            resto = len(unicas) - _MAX_DESCONOCIDAS_LISTADAS
            self.stdout.write(self.style.WARNING(
                f'No existen en SAIDSOFT ({len(unicas)}): {muestra}'
                + (f' ... y {resto} mas.' if resto > 0 else ''),
            ))
            if not actualizadas and not sin_cambio:
                # Ni una sola calzo. Eso no es una planilla con sucursales de baja: es
                # que se esta apuntando a la base equivocada (la local de desarrollo
                # tiene 3 farmacias de prueba, no el catalogo real).
                self.stdout.write(self.style.ERROR(
                    'NINGUN codigo de la planilla existe en esta base. Antes de mirar la '
                    'planilla, comproba contra que base estas corriendo: la local de '
                    'desarrollo no tiene el catalogo real.',
                ))
        if a_revisar:
            # Se escriben igual, pero se nombran: un circuito con forma de numero de
            # ticket o con dos valores separados por barra entra bien en el campo y mal
            # en el ticket, y quien corre esto tiene que poder verlo sin ir a buscarlo.
            self.stdout.write(self.style.WARNING(
                f'\nRevisar a mano ({len(a_revisar)}) — no tienen la forma '
                'cliente-sitio-ciudad, pero se cargan igual:',
            ))
            for codigo, circuito in sorted(a_revisar):
                self.stdout.write(f'  {codigo:<10} {circuito}')
        if not options['aplicar']:
            nuevas = sum(1 for _c, antes, _d in cambios if not antes)
            pisadas = actualizadas - nuevas
            self.stdout.write(
                f'\nDe esas {actualizadas}: {nuevas} hoy están vacías y {pisadas} cambiarían '
                'un circuito ya cargado.',
            )
            if pisadas and not options['solo_faltantes']:
                self.stdout.write(self.style.WARNING(
                    'Si no querés tocar las que ya tienen circuito, agregá --solo-faltantes.',
                ))
            self.stdout.write(self.style.WARNING('Simulación: no se escribió nada. Repetí con --aplicar.'))
