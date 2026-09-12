"""Carga ip/mac/numero_serie en activos de topología que YA existen, desde la planilla
de IPs por farmacia.

`crear_topologia_farmacia` da de alta los puestos; este comando les pone los datos
reales cuando aparecen. Están separados a propósito: crear es una decisión sobre qué
equipos existen, completar es transcribir una planilla, y mezclarlos haría que un error
de tipeo en el CSV pudiera dar de alta equipos fantasma.

Todo o nada: la planilla entera se resuelve y valida antes de escribir la primera fila.
Media planilla aplicada es peor que ninguna — nadie sabría desde dónde retomar.

Nunca pisa un valor que ya está cargado. Si la planilla trae una IP distinta, eso es un
conflicto que resuelve una persona (¿se movió el equipo, o la planilla está vieja?), no
un UPDATE silencioso.

    python manage.py completar_topologia --datos ips.csv
    python manage.py completar_topologia --datos ips.csv --aplicar

El CSV lleva cabecera; `ip`, `mac` y `numero_serie` pueden ir vacíos:

    farmacia,slot,ip,mac,numero_serie
    ML016,mikrotik,10.201.7.225,AA:BB:CC:DD:EE:01,HFG1234ABCD
    ML016,impresora_A,10.201.7.231,,
    ML016,switch,,,SW9988

Los slots que le corresponden a una farmacia salen de:

    python manage.py crear_topologia_farmacia --farmacia ML016 --listar-slots
"""
import csv

from django.contrib.auth.models import User
from django.core.management.base import BaseCommand, CommandError

from apps.activos.services import CAMPOS_COMPLETABLES, ErroresDeCarga, completar_datos_topologia

COLUMNAS = ('farmacia', 'slot') + CAMPOS_COMPLETABLES


class Command(BaseCommand):
    help = 'Carga IP/MAC/número de serie en activos de topología ya existentes, desde un CSV.'

    def add_arguments(self, parser):
        parser.add_argument('--datos', required=True, help='CSV con cabecera farmacia,slot,ip,mac,numero_serie.')
        parser.add_argument(
            '--aplicar', action='store_true',
            help='Escribe los cambios. Sin esto solo informa qué haría.',
        )
        parser.add_argument(
            '--usuario',
            help='Usuario a atribuir en el historial del activo. Default: el primer superusuario.',
        )

    def handle(self, *args, **options):
        filas = self._leer(options['datos'])

        if options['usuario']:
            usuario = User.objects.filter(username=options['usuario']).first()
            if usuario is None:
                raise CommandError('No existe el usuario "%s".' % options['usuario'])
        else:
            usuario = User.objects.filter(is_superuser=True).order_by('id').first()
            if usuario is None:
                raise CommandError('No hay superusuarios; indicá uno con --usuario.')

        try:
            resumen = completar_datos_topologia(
                filas=filas, usuario=usuario, aplicar=options['aplicar'],
            )
        except ErroresDeCarga as exc:
            for error in exc.errores:
                self.stdout.write(self.style.ERROR('  %s' % error))
            raise CommandError(
                '%d problema(s) en la planilla. No se escribió nada: corregí y volvé a correr.'
                % len(exc.errores),
            )

        for linea in resumen['actualizados']:
            self.stdout.write('  %s' % linea)
        self.stdout.write('')

        verbo = 'Actualizados' if options['aplicar'] else 'Se actualizarían'
        self.stdout.write(self.style.SUCCESS(
            '%s: %d activo(s). Sin cambios (ya tenían el dato o la fila venía vacía): %d.'
            % (verbo, len(resumen['actualizados']), resumen['sin_cambios']),
        ))

        if resumen['incompletos']:
            self.stdout.write('')
            self.stdout.write(self.style.WARNING('Siguen incompletos:'))
            for linea in resumen['incompletos']:
                self.stdout.write(self.style.WARNING('  %s' % linea))

        if not options['aplicar']:
            self.stdout.write('')
            self.stdout.write(self.style.WARNING('Simulación: no se escribió nada. Repetí con --aplicar.'))

    def _leer(self, ruta):
        try:
            with open(ruta, newline='', encoding='utf-8-sig') as archivo:
                filas = list(csv.DictReader(archivo))
        except OSError as exc:
            raise CommandError('No se pudo leer %s: %s' % (ruta, exc))

        if not filas:
            raise CommandError('%s no tiene ninguna fila de datos.' % ruta)
        faltantes = [c for c in ('farmacia', 'slot') if c not in filas[0]]
        if faltantes:
            raise CommandError(
                '%s no tiene la(s) columna(s) %s en la cabecera. Se esperan: %s.'
                % (ruta, ', '.join(faltantes), ', '.join(COLUMNAS)),
            )
        return filas
