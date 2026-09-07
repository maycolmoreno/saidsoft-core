"""Da de alta en ITAM los equipos que el agente RMM ya conoce.

El agente reporta número de serie, procesador, RAM, disco y en qué farmacia está. Aun
así el activo había que cargarlo a mano: por eso hay estaciones enroladas reportando
hardware completo y un inventario casi vacío. A 1.800 equipos eso no es lento, es
imposible — el inventario nunca se pondría al día.

Con esto, cada agente instalado se vuelve un activo inventariado y vinculado, y el
rollout deja de ser un trabajo aparte del inventario para pasar a ser el que lo llena.

Por defecto SIMULA. Es un alta masiva sobre el inventario real: conviene ver la lista
antes de escribirla.

    python manage.py crear_activos_desde_rmm                 # simula
    python manage.py crear_activos_desde_rmm --aplicar
    python manage.py crear_activos_desde_rmm --tipo SRV --aplicar
"""
from django.contrib.auth.models import User
from django.core.management.base import BaseCommand, CommandError

from apps.activos.models import Activo
from apps.activos.services import crear_activos_desde_estaciones


class Command(BaseCommand):
    help = 'Crea activos de ITAM a partir de las estaciones aprobadas del RMM.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--aplicar', action='store_true',
            help='Escribe los cambios. Sin esto solo informa qué haría.',
        )
        parser.add_argument(
            '--tipo', default=Activo.Tipo.DESKTOP,
            choices=[t for t, _ in Activo.Tipo.choices],
            help='Tipo de activo a crear. El agente reporta hardware, no el formato del '
                 'equipo (un desktop y un servidor se ven igual desde adentro), así que '
                 'no se adivina. Default: DSK.',
        )
        parser.add_argument(
            '--usuario',
            help='Usuario a atribuir en el historial del activo. Default: el primer '
                 'superusuario.',
        )

    def handle(self, *args, **options):
        if options['usuario']:
            usuario = User.objects.filter(username=options['usuario']).first()
            if usuario is None:
                raise CommandError(f'No existe el usuario "{options["usuario"]}".')
        else:
            usuario = User.objects.filter(is_superuser=True).order_by('id').first()
            if usuario is None:
                raise CommandError('No hay superusuarios; indicá uno con --usuario.')

        resumen = crear_activos_desde_estaciones(
            usuario=usuario, tipo=options['tipo'], aplicar=options['aplicar'],
        )

        for linea in resumen['detalle']:
            self.stdout.write(f'  {linea}')
        if resumen['detalle']:
            self.stdout.write('')

        verbo = 'Creados' if options['aplicar'] else 'Se crearían'
        self.stdout.write(self.style.SUCCESS(
            f'{verbo}: {resumen["creados"]} activo(s) nuevo(s) como {options["tipo"]}.',
        ))
        if resumen['vinculados']:
            self.stdout.write(
                f'Vinculados a un activo que ya existía (misma serie): {resumen["vinculados"]}.',
            )
        if resumen['ya_vinculadas']:
            self.stdout.write(f'Estaciones que ya tenían activo: {resumen["ya_vinculadas"]}.')
        if resumen['sin_serie']:
            self.stdout.write(self.style.WARNING(
                f'Sin número de serie, omitidas: {resumen["sin_serie"]}. Sin serie no hay '
                f'forma de reconocer el equipo después ni de evitar duplicarlo.',
            ))
        if not options['aplicar']:
            self.stdout.write(self.style.WARNING('Simulación: no se escribió nada. Repetí con --aplicar.'))
