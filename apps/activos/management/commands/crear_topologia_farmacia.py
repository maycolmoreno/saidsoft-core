"""Inventaria el equipamiento SIN agente de una farmacia: router, switch, pinpad,
impresoras, teléfono.

`crear_activos_desde_rmm` ya da de alta todo lo que tiene agente. Lo que queda afuera es
justamente lo que hace falta cuando una farmacia se cae: qué hay en el rack y con qué
IP. Eso nadie lo puede reportar solo — lo carga una persona mirando las etiquetas.

Por defecto SIMULA. Los datos de red vienen de la planilla de IPs por farmacia, y lo que
no esté en ella queda VACÍO: un activo con la serie en blanco es información incompleta
y se ve como tal; uno con una serie inventada es peor que no tenerlo, porque alguien la
va a creer más adelante. El resumen final lista exactamente a qué activos hay que volver.

    python manage.py crear_topologia_farmacia --farmacia ML016
    python manage.py crear_topologia_farmacia --farmacia ML016 --datos ml016.csv --aplicar
    python manage.py crear_topologia_farmacia --farmacia ML016 --slots mikrotik,switch

El CSV de `--datos` lleva cabecera y una fila por equipo que se conozca:

    slot,ip,mac,numero_serie
    mikrotik,10.111.16.1,AA:BB:CC:DD:EE:FF,HFG1234ABCD
    switch,,,
"""
import csv

from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError

from apps.activos.services import SLOTS_POR_NOMBRE, crear_topologia_farmacia
from apps.catalogo.models import Farmacia

COLUMNAS_DATOS = ('ip', 'mac', 'numero_serie')


class Command(BaseCommand):
    help = 'Da de alta en ITAM el equipamiento sin agente de una farmacia (router, switch, pinpad, impresoras).'

    def add_arguments(self, parser):
        parser.add_argument('--farmacia', required=True, help='Código de la farmacia, ej. ML016.')
        parser.add_argument(
            '--aplicar', action='store_true',
            help='Escribe los cambios. Sin esto solo informa qué haría.',
        )
        parser.add_argument(
            '--slots',
            help='Lista separada por comas para acotar el alta. No toda farmacia tiene '
                 'teléfono IP, y asumir que sí inventaría un equipo que no existe. '
                 'Disponibles: %s.' % ', '.join(SLOTS_POR_NOMBRE),
        )
        parser.add_argument(
            '--datos',
            help='CSV con cabecera slot,ip,mac,numero_serie. Lo que no venga queda vacío.',
        )
        parser.add_argument(
            '--usuario',
            help='Usuario a atribuir en el historial del activo. Default: el primer superusuario.',
        )

    def handle(self, *args, **options):
        farmacia = Farmacia.objects.filter(codigo=options['farmacia']).first()
        if farmacia is None:
            raise CommandError('No existe la farmacia "%s".' % options['farmacia'])

        if options['usuario']:
            usuario = User.objects.filter(username=options['usuario']).first()
            if usuario is None:
                raise CommandError('No existe el usuario "%s".' % options['usuario'])
        else:
            usuario = User.objects.filter(is_superuser=True).order_by('id').first()
            if usuario is None:
                raise CommandError('No hay superusuarios; indicá uno con --usuario.')

        slots = [s.strip() for s in options['slots'].split(',')] if options['slots'] else None
        datos = self._leer_datos(options['datos']) if options['datos'] else None

        try:
            resumen = crear_topologia_farmacia(
                farmacia=farmacia, usuario=usuario, slots=slots, datos=datos,
                aplicar=options['aplicar'],
            )
        except (ValueError, ValidationError) as exc:
            # Nada se escribió: la validación corre entera antes del primer alta, para no
            # dejar la farmacia a medio inventariar por una MAC mal tipeada.
            raise CommandError(str(exc))

        for linea in resumen['creados']:
            self.stdout.write('  %s' % linea)
        for linea in resumen['omitidos']:
            self.stdout.write(self.style.WARNING('  %s' % linea))
        self.stdout.write('')

        verbo = 'Creados' if options['aplicar'] else 'Se crearían'
        self.stdout.write(self.style.SUCCESS(
            '%s: %d activo(s) en %s.' % (verbo, len(resumen['creados']), farmacia.codigo),
        ))

        if resumen['incompletos']:
            self.stdout.write('')
            self.stdout.write(self.style.WARNING('Quedan con datos pendientes de cargar:'))
            for linea in resumen['incompletos']:
                self.stdout.write(self.style.WARNING('  %s' % linea))

        if not options['aplicar']:
            self.stdout.write('')
            self.stdout.write(self.style.WARNING('Simulación: no se escribió nada. Repetí con --aplicar.'))

    def _leer_datos(self, ruta):
        try:
            with open(ruta, newline='', encoding='utf-8-sig') as archivo:
                filas = list(csv.DictReader(archivo))
        except OSError as exc:
            raise CommandError('No se pudo leer %s: %s' % (ruta, exc))

        if not filas:
            raise CommandError('%s no tiene ninguna fila de datos.' % ruta)
        if 'slot' not in (filas[0].keys() or ()):
            raise CommandError('%s no tiene la columna "slot" en la cabecera.' % ruta)

        datos = {}
        for numero, fila in enumerate(filas, start=2):
            slot = (fila.get('slot') or '').strip()
            if not slot:
                raise CommandError('Fila %d de %s: la columna "slot" está vacía.' % (numero, ruta))
            if slot in datos:
                raise CommandError('%s repite el slot "%s" (fila %d).' % (ruta, slot, numero))
            datos[slot] = {c: (fila.get(c) or '').strip() for c in COLUMNAS_DATOS}
        return datos
