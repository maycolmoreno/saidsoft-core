"""Carga al panel el nodo de cada farmacia desde la planilla de red de operaciones.

    python manage.py cargar_nodos_planilla "DATOS DE FARMACIAS.xlsx" --usuario <username>
    python manage.py cargar_nodos_planilla "DATOS DE FARMACIAS.xlsx" --usuario <username> \\
        --omitir MAM02,MDG02 --reporte /tmp/diferencias.csv --aplicar

Solo cambia el nodo en el PANEL y crea las farmacias que falten. NO envía la configuración
del nodo a ninguna estación. Ver `apps.catalogo.planilla_red`. Simula por defecto y exige
`--aplicar`, como el resto de los comandos que escriben (ver CLAUDE.md).
"""
import csv

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from apps.catalogo.planilla_red import PlanillaInvalida, cargar_nodos, leer_planilla

ETIQUETAS = {
    'cambia_nodo': 'Cambian de nodo en el panel',
    'crea': 'Se crean',
    'conserva_pos': 'Se conserva lo que reporta el POS (difiere de la planilla)',
    'omitida': 'Omitidas a pedido',
    'error': 'No se tocan (revisar)',
}


class Command(BaseCommand):
    help = 'Alinea el nodo de cada farmacia en el panel con la planilla de red (no toca estaciones).'

    def add_arguments(self, parser):
        parser.add_argument('xlsx_path')
        parser.add_argument('--usuario', required=True, help='Quién queda en la auditoría.')
        parser.add_argument('--omitir', default='', help='Códigos que no se crean, separados por coma.')
        parser.add_argument('--reporte', default='', help='CSV con TODAS las filas y diferencias.')
        parser.add_argument('--aplicar', action='store_true', help='Escribe. Sin esto solo simula.')

    def handle(self, *args, **opciones):
        import openpyxl

        try:
            usuario = get_user_model().objects.get(username=opciones['usuario'])
        except get_user_model().DoesNotExist:
            raise CommandError(f'No existe el usuario "{opciones["usuario"]}".')
        try:
            libro = openpyxl.load_workbook(opciones['xlsx_path'], read_only=True, data_only=True)
            filas = leer_planilla(libro)
        except (OSError, PlanillaInvalida) as exc:
            raise CommandError(str(exc))

        omitir = [c.strip() for c in opciones['omitir'].split(',') if c.strip()]
        try:
            resultado = cargar_nodos(filas, usuario=usuario, aplicar=opciones['aplicar'], omitir=omitir)
        except PlanillaInvalida as exc:
            raise CommandError(str(exc))

        self.stdout.write(f'{len(filas)} filas leídas. Sin cambios: {len(resultado.por_accion("igual"))}.')
        for accion, etiqueta in ETIQUETAS.items():
            cambios = resultado.por_accion(accion)
            if not cambios:
                continue
            self.stdout.write(f'\n{etiqueta}: {len(cambios)}')
            for c in cambios:
                detalle = f'panel {c.nodo_panel or "-"} -> planilla {c.nodo_planilla or "-"}'
                if c.nodo_pos:
                    detalle += f' | POS {c.nodo_pos}'
                self.stdout.write(f'  {c.codigo:<8} {detalle}{"  (" + c.nota + ")" if c.nota else ""}')

        if resultado.otras_diferencias:
            self.stdout.write(
                f'\nOtras diferencias de red (solo informadas, NO se aplican): '
                f'{len(resultado.otras_diferencias)} — ver el reporte.',
            )

        if opciones['reporte']:
            with open(opciones['reporte'], 'w', newline='', encoding='utf-8-sig') as archivo:
                escritor = csv.writer(archivo)
                escritor.writerow(['codigo', 'accion', 'nodo_panel_antes', 'nodo_planilla', 'nodo_pos', 'nota'])
                for c in resultado.cambios:
                    escritor.writerow([c.codigo, c.accion, c.nodo_panel, c.nodo_planilla, c.nodo_pos, c.nota])
                escritor.writerow([])
                escritor.writerow(['codigo', 'campo (solo informado)', 'en_panel', 'en_planilla'])
                escritor.writerows(resultado.otras_diferencias)
            self.stdout.write(f'Reporte: {opciones["reporte"]}')

        self.stdout.write('\nNo se envió nada a ninguna estación.')
        if not opciones['aplicar']:
            self.stdout.write(self.style.WARNING('SIMULACIÓN — no se escribió nada. Repetí con --aplicar.'))
        else:
            self.stdout.write(self.style.SUCCESS('Aplicado.'))
