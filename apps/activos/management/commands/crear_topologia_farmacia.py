"""Inventaria el equipamiento SIN agente de una farmacia: router, switch, VoIP,
biométrico, cámaras y alarmas SIPAO, y la impresora + medianet de cada caja.

`crear_activos_desde_rmm` ya da de alta todo lo que tiene agente. Lo que queda afuera es
justamente lo que hace falta cuando una farmacia se cae: qué hay en el rack y con qué
IP. Eso nadie lo puede reportar solo — lo carga una persona mirando las etiquetas.

El catálogo no es una lista fija: se expande según cuántas estaciones aprobadas tenga
realmente la farmacia, porque el patrón es 1 impresora y 1 medianet por caja.

Por defecto SIMULA. Los datos de red vienen de la planilla de IPs por farmacia, y lo que
no esté en ella queda VACÍO: un activo con la serie en blanco es información incompleta
y se ve como tal; uno con una serie inventada es peor que no tenerlo, porque alguien la
va a creer más adelante. Para cargarlos después está `completar_topologia`.

    python manage.py crear_topologia_farmacia --farmacia ML016
    python manage.py crear_topologia_farmacia --farmacia ML016 --aplicar
    python manage.py crear_topologia_farmacia --farmacia ML016 --slots biometrico,voip
    python manage.py crear_topologia_farmacia --farmacia ML016 --listar-slots
"""
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError

from apps.activos.services import crear_topologia_farmacia, slots_de_farmacia
from apps.catalogo.models import Farmacia


class Command(BaseCommand):
    help = 'Da de alta en ITAM el equipamiento sin agente de una farmacia (router, switch, impresoras, medianet).'

    def add_arguments(self, parser):
        parser.add_argument('--farmacia', required=True, help='Código de la farmacia, ej. ML016.')
        parser.add_argument(
            '--aplicar', action='store_true',
            help='Escribe los cambios. Sin esto solo informa qué haría.',
        )
        parser.add_argument(
            '--slots',
            help='Lista separada por comas para acotar el alta, ej. "biometrico,voip". '
                 'Vacío = el catálogo completo que le corresponde a la farmacia.',
        )
        parser.add_argument(
            '--listar-slots', action='store_true',
            help='Muestra los puestos que le corresponden a esta farmacia y termina.',
        )
        parser.add_argument(
            '--usuario',
            help='Usuario a atribuir en el historial del activo. Default: el primer superusuario.',
        )

    def handle(self, *args, **options):
        farmacia = Farmacia.objects.filter(codigo=options['farmacia']).first()
        if farmacia is None:
            raise CommandError('No existe la farmacia "%s".' % options['farmacia'])

        if options['listar_slots']:
            for slot in slots_de_farmacia(farmacia):
                self.stdout.write('  %-22s %s' % (slot.slot, slot.categoria_nombre))
            return

        if options['usuario']:
            usuario = User.objects.filter(username=options['usuario']).first()
            if usuario is None:
                raise CommandError('No existe el usuario "%s".' % options['usuario'])
        else:
            usuario = User.objects.filter(is_superuser=True).order_by('id').first()
            if usuario is None:
                raise CommandError('No hay superusuarios; indicá uno con --usuario.')

        slots = [s.strip() for s in options['slots'].split(',')] if options['slots'] else None

        try:
            resumen = crear_topologia_farmacia(
                farmacia=farmacia, usuario=usuario, slots=slots, aplicar=options['aplicar'],
            )
        except (ValueError, ValidationError) as exc:
            # Nada se escribió: la validación corre entera antes del primer alta, para no
            # dejar la farmacia a medio inventariar.
            raise CommandError(str(exc))

        for linea in resumen['creados']:
            self.stdout.write('  %s' % linea)
        for linea in resumen['adoptados']:
            self.stdout.write('  adopta  %s' % linea)
        for linea in resumen['omitidos']:
            self.stdout.write(self.style.WARNING('  %s' % linea))
        self.stdout.write('')

        verbo = 'Creados' if options['aplicar'] else 'Se crearían'
        self.stdout.write(self.style.SUCCESS(
            '%s: %d activo(s) en %s. Adoptados (ya existían sin slot): %d.'
            % (verbo, len(resumen['creados']), farmacia.codigo, len(resumen['adoptados'])),
        ))

        if resumen['ambiguos']:
            self.stdout.write('')
            self.stdout.write(self.style.ERROR('Sin resolver — no se creó ni adoptó nada de estas categorías:'))
            for linea in resumen['ambiguos']:
                self.stdout.write(self.style.ERROR('  %s' % linea))

        if resumen['incompletos']:
            self.stdout.write('')
            self.stdout.write(self.style.WARNING('Quedan con datos pendientes de cargar:'))
            for linea in resumen['incompletos']:
                self.stdout.write(self.style.WARNING('  %s' % linea))

        if not options['aplicar']:
            self.stdout.write('')
            self.stdout.write(self.style.WARNING('Simulación: no se escribió nada. Repetí con --aplicar.'))
