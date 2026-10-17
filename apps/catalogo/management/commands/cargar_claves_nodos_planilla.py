"""Carga CIFRADAS en el panel las contraseñas de BD de los nodos, desde la planilla de red.

    python manage.py cargar_claves_nodos_planilla "DATOS DE FARMACIAS.xlsx" --usuario <username>
    python manage.py cargar_claves_nodos_planilla "DATOS DE FARMACIAS.xlsx" --usuario <username> --aplicar

No muestra ninguna contraseña y no envía nada a las estaciones. Ver
`apps.catalogo.planilla_red.cargar_claves_nodos`. Simula por defecto.
"""
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from apps.catalogo.planilla_red import PlanillaInvalida, cargar_claves_nodos, leer_claves_planilla


class Command(BaseCommand):
    help = 'Guarda cifradas en el panel las contraseñas de los nodos de la planilla (no toca estaciones).'

    def add_arguments(self, parser):
        parser.add_argument('xlsx_path')
        parser.add_argument('--usuario', required=True, help='Quién queda en la auditoría.')
        parser.add_argument('--aplicar', action='store_true', help='Escribe. Sin esto solo simula.')

    def handle(self, *args, **opciones):
        import openpyxl

        try:
            usuario = get_user_model().objects.get(username=opciones['usuario'])
        except get_user_model().DoesNotExist:
            raise CommandError(f'No existe el usuario "{opciones["usuario"]}".')
        try:
            libro = openpyxl.load_workbook(opciones['xlsx_path'], read_only=True, data_only=True)
            claves = leer_claves_planilla(libro)
        except (OSError, PlanillaInvalida) as exc:
            raise CommandError(str(exc))

        for nodo, estado in cargar_claves_nodos(claves, usuario=usuario, aplicar=opciones['aplicar']):
            self.stdout.write(f'  {nodo:<10} {estado}')
        self.stdout.write('\nNo se mostró ninguna contraseña ni se envió nada a ninguna estación.')
        if opciones['aplicar']:
            self.stdout.write(self.style.SUCCESS('Aplicado.'))
        else:
            self.stdout.write(self.style.WARNING('SIMULACIÓN — no se escribió nada. Repetí con --aplicar.'))
