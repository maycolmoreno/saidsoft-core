"""Importa farmacias en lote desde un CSV exportado de un Excel de inventario de red.

Pensado para el caso real: un listado de sitios con columnas de ciudad, código de
sitio y nodo de red (ej. "Ciudad", "Id de ...", "NODO"), donde el código no sigue un
rango secuencial simple (MALU1, MAM01, MB001, GP005...) y la unidad de negocio se
deduce de la primera letra del código:

    M... -> MIA      (ej. ML001, MC001, MB001)
    G... -> SG        (ej. GP005)
    dígito... -> 7DIAS (ej. 7DM02)

Ese mapeo es específico del negocio (confirmado con el usuario, 11-ago-2026) — si
aparece un prefijo nuevo, el comando lo reporta como error en vez de adivinar.

El nodo de red (columna NODO/GRUPO) se usa como código de Grupo; si el grupo no
existe todavía se crea solo (es solo un canal de versión de POS, sin implicancias de
seguridad). La unidad de negocio NO se crea sola si falta — es el límite de
aislamiento multi-tenant (ver UnidadNegocio.unidad_negocio en el modelo), así que un
código de unidad inexistente aborta esa fila con un error explícito en vez de crear
un tenant nuevo por accidente.

Uso:
    python manage.py importar_farmacias sitios.csv
    python manage.py importar_farmacias sitios.csv --dry-run
    python manage.py importar_farmacias sitios.csv --actualizar   # además de crear,
        actualiza ubicación/grupo de las farmacias que ya existían

El CSV debe tener encabezados (no importa el orden ni mayúsculas/minúsculas); se
detectan por coincidencia parcial:
    - código de farmacia: cualquier encabezado que contenga "id" o "codigo"/"código"
    - ciudad/ubicación:    cualquier encabezado que contenga "ciudad" o "ubicacion"
    - provincia:           cualquier encabezado que contenga "provincia" (opcional)
    - nodo/grupo:          cualquier encabezado que contenga "nodo" o "grupo"
Si hay columna de provincia, la ubicación queda como "Ciudad, Provincia". Columnas
como "Segmento de Red", "Tipo de Enlace", "Login" o "Backup" se ignoran: no tienen
dónde ir en el modelo Farmacia.
"""
import csv
import unicodedata

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from apps.catalogo.models import Farmacia, Grupo, UnidadNegocio

PREFIJOS_UNIDAD_NEGOCIO = {
    'M': 'MIA',
    'G': 'SG',
}


def _unidad_negocio_por_prefijo(codigo):
    if codigo[:1].isdigit():
        return '7DIAS'
    return PREFIJOS_UNIDAD_NEGOCIO.get(codigo[:1])


def _normalizar(texto):
    sin_acentos = unicodedata.normalize('NFKD', texto).encode('ascii', 'ignore').decode('ascii')
    return sin_acentos.strip().lower()


def _encontrar_columna(fieldnames, *pistas):
    for nombre in fieldnames:
        normalizado = _normalizar(nombre)
        if any(pista in normalizado for pista in pistas):
            return nombre
    return None


class Command(BaseCommand):
    help = 'Crea farmacias en lote desde un CSV (código, ciudad/ubicación, nodo/grupo).'

    def add_arguments(self, parser):
        parser.add_argument('csv_path')
        parser.add_argument('--dry-run', action='store_true', help='No escribe nada, solo muestra qué haría.')
        parser.add_argument(
            '--actualizar', action='store_true',
            help='Si la farmacia ya existe, actualiza ubicación y grupo desde el CSV en vez de omitirla.',
        )

    def handle(self, *args, **options):
        ruta = options['csv_path']
        dry_run = options['dry_run']
        actualizar = options['actualizar']

        try:
            archivo = open(ruta, newline='', encoding='utf-8-sig')
        except OSError as exc:
            raise CommandError(f'No se pudo abrir {ruta}: {exc}')

        with archivo:
            lector = csv.DictReader(archivo)
            if not lector.fieldnames:
                raise CommandError('El CSV no tiene encabezados.')

            col_codigo = _encontrar_columna(lector.fieldnames, 'id', 'codigo', 'código')
            col_ciudad = _encontrar_columna(lector.fieldnames, 'ciudad', 'ubicacion', 'ubicación')
            col_provincia = _encontrar_columna(lector.fieldnames, 'provincia')
            col_nodo = _encontrar_columna(lector.fieldnames, 'nodo', 'grupo')
            if not col_codigo or not col_nodo:
                raise CommandError(
                    f'No se detectaron las columnas necesarias en {lector.fieldnames!r}. '
                    'Hace falta al menos una columna de código (id/código) y una de nodo/grupo.',
                )
            self.stdout.write(
                f'Columnas detectadas: código={col_codigo!r}, ciudad={col_ciudad!r}, '
                f'provincia={col_provincia!r}, nodo={col_nodo!r}',
            )

            grupos_cache = {g.codigo: g for g in Grupo.objects.all()}
            unidades_cache = {u.codigo: u for u in UnidadNegocio.objects.all()}

            creadas, actualizadas, omitidas, errores = [], [], [], []

            with transaction.atomic():
                sp = transaction.savepoint()
                for fila_num, fila in enumerate(lector, start=2):
                    codigo = (fila.get(col_codigo) or '').strip().upper()
                    if not codigo:
                        continue
                    ciudad = (fila.get(col_ciudad) or '').strip() if col_ciudad else ''
                    provincia = (fila.get(col_provincia) or '').strip() if col_provincia else ''
                    ubicacion = f'{ciudad}, {provincia}' if ciudad and provincia else (ciudad or provincia)
                    nodo = (fila.get(col_nodo) or '').strip().upper()

                    if not nodo:
                        errores.append(f'fila {fila_num} ({codigo}): sin valor de nodo/grupo.')
                        continue

                    codigo_unidad = _unidad_negocio_por_prefijo(codigo)
                    if not codigo_unidad:
                        errores.append(
                            f'fila {fila_num} ({codigo}): prefijo de código sin mapeo a unidad de '
                            'negocio conocido (M->MIA, G->SG, dígito->7DIAS).',
                        )
                        continue
                    unidad = unidades_cache.get(codigo_unidad)
                    if unidad is None:
                        errores.append(
                            f'fila {fila_num} ({codigo}): la unidad de negocio {codigo_unidad} no existe '
                            'todavía en SAIDSOFT — hay que crearla primero (Admin > Catálogo > Unidades '
                            'de negocio) antes de poder importar esta fila.',
                        )
                        continue

                    grupo = grupos_cache.get(nodo)
                    if grupo is None:
                        grupo = Grupo(codigo=nodo)
                        if not dry_run:
                            grupo.save()
                        grupos_cache[nodo] = grupo
                        self.stdout.write(f'  + grupo nuevo: {nodo}')

                    existente = Farmacia.objects.filter(codigo=codigo).first()
                    if existente:
                        if actualizar:
                            existente.ubicacion = ubicacion
                            existente.grupo = grupo
                            existente.unidad_negocio = unidad
                            if not dry_run:
                                existente.save(update_fields=['ubicacion', 'grupo', 'unidad_negocio'])
                            actualizadas.append(codigo)
                        else:
                            omitidas.append(codigo)
                        continue

                    if not dry_run:
                        Farmacia.objects.create(
                            codigo=codigo, ubicacion=ubicacion, grupo=grupo, unidad_negocio=unidad,
                        )
                    creadas.append(codigo)

                if dry_run:
                    transaction.savepoint_rollback(sp)
                else:
                    transaction.savepoint_commit(sp)

        prefijo = '[DRY RUN] ' if dry_run else ''
        self.stdout.write(self.style.SUCCESS(f'{prefijo}{len(creadas)} farmacia(s) creada(s): {", ".join(creadas) or "-"}'))
        if actualizar:
            self.stdout.write(f'{prefijo}{len(actualizadas)} farmacia(s) actualizada(s): {", ".join(actualizadas) or "-"}')
        else:
            self.stdout.write(f'{prefijo}{len(omitidas)} farmacia(s) ya existían, omitida(s): {", ".join(omitidas) or "-"}')
        if errores:
            self.stdout.write(self.style.ERROR(f'{len(errores)} fila(s) con error:'))
            for err in errores:
                self.stdout.write(self.style.ERROR(f'  - {err}'))
