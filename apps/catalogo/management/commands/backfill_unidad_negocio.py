"""Reporta farmacias sin `unidad_negocio` asignada antes de aplicar una migración
que la vuelve obligatoria (ver apps/catalogo/migrations/0009_alter_farmacia_unidad_negocio.py).

No asigna nada automáticamente a propósito: a qué cliente pertenece una farmacia es
una decisión de negocio, no algo que se pueda inferir del código. Con `--asignar` se
puede fijar una unidad de negocio puntual a una lista de farmacias, para ir cerrando
la lista reportada sin tener que entrar al admin una por una.

    python manage.py backfill_unidad_negocio
    python manage.py backfill_unidad_negocio --asignar SG ML001 ML002
"""
from django.core.management.base import BaseCommand, CommandError

from apps.catalogo.models import Farmacia, UnidadNegocio


class Command(BaseCommand):
    help = 'Reporta (o corrige puntualmente) farmacias sin unidad_negocio asignada.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--asignar', nargs='+', metavar='CODIGO',
            help='Código de UnidadNegocio seguido de uno o más códigos de Farmacia a asignarle.',
        )

    def handle(self, *args, **options):
        if options['asignar']:
            self._asignar(options['asignar'])
            return

        pendientes = Farmacia.objects.filter(unidad_negocio__isnull=True).order_by('codigo')
        if not pendientes.exists():
            self.stdout.write(self.style.SUCCESS('Todas las farmacias tienen unidad_negocio asignada.'))
            return

        self.stdout.write(self.style.WARNING(f'{pendientes.count()} farmacia(s) sin unidad_negocio:'))
        for f in pendientes:
            self.stdout.write(f'  {f.codigo} ({f.nombre or "sin nombre"}) — grupo {f.grupo.codigo}')
        self.stdout.write(
            '\nAsignar con: python manage.py backfill_unidad_negocio --asignar '
            '<CODIGO_UNIDAD> <CODIGO_FARMACIA> [<CODIGO_FARMACIA> ...]'
        )

    def _asignar(self, args):
        codigo_unidad, *codigos_farmacia = args
        try:
            unidad = UnidadNegocio.objects.get(codigo=codigo_unidad)
        except UnidadNegocio.DoesNotExist:
            raise CommandError(f'No existe una UnidadNegocio con código "{codigo_unidad}".')

        if not codigos_farmacia:
            raise CommandError('Falta al menos un código de farmacia después del código de unidad de negocio.')

        farmacias = Farmacia.objects.filter(codigo__in=codigos_farmacia)
        encontrados = set(farmacias.values_list('codigo', flat=True))
        faltantes = set(codigos_farmacia) - encontrados
        if faltantes:
            raise CommandError(f'No existen farmacias con código: {", ".join(sorted(faltantes))}')

        actualizadas = farmacias.update(unidad_negocio=unidad)
        self.stdout.write(self.style.SUCCESS(f'{actualizadas} farmacia(s) asignada(s) a {unidad.codigo}.'))
