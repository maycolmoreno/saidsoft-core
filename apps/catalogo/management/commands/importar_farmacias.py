"""Importa farmacias en lote desde un CSV exportado de un Excel de inventario de red.

Wrapper delgado sobre apps.catalogo.services.importar_farmacias_desde_csv — la misma
lógica la usa también la pantalla de importación del admin (/admin/catalogo/farmacia/
importar/, para quien no tiene acceso SSH al servidor). Este comando queda para
correrlo a mano.

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
        actualiza ubicación/grupo/segmento de red/tipo de enlace/backup de las
        farmacias que ya existían

El CSV debe tener encabezados (no importa el orden ni mayúsculas/minúsculas); se
detectan por coincidencia parcial:
    - código de farmacia: cualquier encabezado que contenga "id" o "codigo"/"código"
    - ciudad/ubicación:    cualquier encabezado que contenga "ciudad" o "ubicacion"
    - provincia:           cualquier encabezado que contenga "provincia" (opcional)
    - nodo/grupo:          cualquier encabezado que contenga "nodo" o "grupo"
    - segmento de red:     cualquier encabezado que contenga "segmento" (opcional)
    - tipo de enlace:      cualquier encabezado que contenga "enlace" (opcional)
    - backup:              cualquier encabezado que contenga "backup" (opcional; valores
                            tipo "NO"/"INACTIVO"/vacío/"0" se leen como sin backup,
                            cualquier otra cosa no vacía (ej. "ACTIVO") como con backup)
Si hay columna de provincia, la ubicación queda como "Ciudad, Provincia". "Login" (el
usuario del circuito ante el proveedor de internet) se ignora: es dato del proveedor,
no de SAIDSOFT.
"""
from django.core.management.base import BaseCommand, CommandError

from apps.catalogo.services import importar_farmacias_desde_csv


class Command(BaseCommand):
    help = 'Crea farmacias en lote desde un CSV (código, ciudad/ubicación, nodo/grupo).'

    def add_arguments(self, parser):
        parser.add_argument('csv_path')
        parser.add_argument('--dry-run', action='store_true', help='No escribe nada, solo muestra qué haría.')
        parser.add_argument(
            '--actualizar', action='store_true',
            help='Si la farmacia ya existe, actualiza sus datos desde el CSV en vez de omitirla.',
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
            try:
                resultado = importar_farmacias_desde_csv(archivo, dry_run=dry_run, actualizar=actualizar)
            except ValueError as exc:
                raise CommandError(str(exc))

        self.stdout.write(f'Columnas detectadas: {resultado.columnas!r}')
        for nodo in resultado.grupos_nuevos:
            self.stdout.write(f'  + grupo nuevo: {nodo}')

        prefijo = '[DRY RUN] ' if dry_run else ''
        self.stdout.write(self.style.SUCCESS(
            f'{prefijo}{len(resultado.creadas)} farmacia(s) creada(s): {", ".join(resultado.creadas) or "-"}',
        ))
        if actualizar:
            self.stdout.write(
                f'{prefijo}{len(resultado.actualizadas)} farmacia(s) actualizada(s): '
                f'{", ".join(resultado.actualizadas) or "-"}',
            )
        else:
            self.stdout.write(
                f'{prefijo}{len(resultado.omitidas)} farmacia(s) ya existían, omitida(s): '
                f'{", ".join(resultado.omitidas) or "-"}',
            )
        if resultado.errores:
            self.stdout.write(self.style.ERROR(f'{len(resultado.errores)} fila(s) con error:'))
            for err in resultado.errores:
                self.stdout.write(self.style.ERROR(f'  - {err}'))
