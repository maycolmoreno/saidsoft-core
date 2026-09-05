"""Carga el nombre del circuito en el proveedor (`Farmacia.circuito_proveedor`).

El dato viene de las planillas de enlaces de CRESIO — las mismas que usaba el sistema
de monitoreo anterior (`Cresio_enlaces`), donde la columna se llama `caracteristica`.
Es el identificador que el proveedor pide al abrir un ticket ("sangregorio2-santana"),
y hasta ahora no vivía en SAIDSOFT: había que buscarlo en un Excel aparte justo cuando
una farmacia está caída y sin vender.

Acepta los dos formatos que existen, porque cada unidad de negocio entregó el suyo:

- SAN GREGORIO: `provincia;canton;cod_sucursal;caracteristica;proveedor;ip_proveedor`
- MIA:          `Provincia;Ciudad;Codigo de farmacia;Ip Provedor;Provedor;Caracteristica`

Solo escribe `circuito_proveedor`. NO toca `segmento_red` ni `ip_router` a propósito:
esos ya están cargados y una reimportación silenciosa podría pisarlos con un dato viejo
de planilla. Idempotente: correrlo dos veces no cambia nada la segunda.

    python manage.py importar_circuitos_proveedor <archivo.csv> [--aplicar]

Sin `--aplicar` solo informa qué haría (los CSV de enlaces suelen traer sucursales que
ya cerraron, y conviene verlas antes de escribir).
"""
import csv

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from apps.catalogo.models import Farmacia

# Nombre de columna -> qué significa. Se prueban en orden hasta que una calce.
FORMATOS = (
    {'codigo': 'cod_sucursal', 'circuito': 'caracteristica'},
    {'codigo': 'Codigo de farmacia', 'circuito': 'Caracteristica'},
)


class Command(BaseCommand):
    help = 'Carga Farmacia.circuito_proveedor desde una planilla de enlaces (CSV con ";").'

    def add_arguments(self, parser):
        parser.add_argument('archivo', help='CSV separado por ";".')
        parser.add_argument(
            '--aplicar', action='store_true',
            help='Escribe los cambios. Sin esto solo informa qué haría.',
        )

    def handle(self, *args, **options):
        ruta = options['archivo']
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
                + f'\nEl archivo trae: {sorted(columnas)}'
            )

        actualizadas, sin_cambio, sin_circuito = 0, 0, 0
        desconocidas = []

        with transaction.atomic():
            for fila in filas:
                codigo = (fila.get(formato['codigo']) or '').strip().upper()
                circuito = (fila.get(formato['circuito']) or '').strip()
                if not codigo:
                    continue
                if not circuito:
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
        if sin_circuito:
            self.stdout.write(f'Filas sin circuito en la planilla: {sin_circuito}.')
        if desconocidas:
            self.stdout.write(self.style.WARNING(
                f'No existen en SAIDSOFT ({len(desconocidas)}): {", ".join(sorted(desconocidas))}',
            ))
        if not options['aplicar']:
            self.stdout.write(self.style.WARNING('Simulación: no se escribió nada. Repetí con --aplicar.'))
