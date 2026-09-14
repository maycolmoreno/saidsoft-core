"""Fija la versión objetivo del POS en los grupos que tienen farmacias.

`Grupo.version_objetivo` es contra lo que `Estacion.desactualizada` compara la versión
que reporta el agente, y de ahí sale el filtro "Solo desactualizadas" del panel. Un grupo
sin objetivo no puede tener estaciones desactualizadas: no hay contra qué comparar, y eso
es correcto — pero significa que el indicador no dice nada hasta que alguien lo cargue.

A 14-sep-2026, de los 7 grupos con farmacias solo TRX004 lo tenía (3.0.2.28); los otros 6
—unas 595 farmacias— estaban vacíos.

Por defecto SIMULA. Cargar un objetivo equivocado es peor que no cargarlo: dejaría cien
farmacias marcadas como desactualizadas para siempre, y un indicador que siempre está en
rojo se deja de mirar.

    python manage.py fijar_version_objetivo_pos --objetivo 3.0.2.28
    python manage.py fijar_version_objetivo_pos --objetivo 3.0.2.28 --aplicar
    python manage.py fijar_version_objetivo_pos --objetivo 3.0.2.28 --grupos TRX001,TRX002 --aplicar
"""
import re

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.db.models import Count

from apps.catalogo.models import Grupo

# Cuatro números separados por puntos, como los reporta Windows para un ejecutable
# (ej. 3.0.2.28). Se valida el formato para que un dedazo no quede fijado como objetivo
# de cientos de farmacias.
FORMATO_VERSION = re.compile(r'^\d+(\.\d+){1,3}$')


class Command(BaseCommand):
    help = 'Fija Grupo.version_objetivo (versión esperada del POS) en los grupos con farmacias.'

    def add_arguments(self, parser):
        # `--version` no se puede usar: BaseCommand ya lo reserva para imprimir la versión
        # de Django, y argparse rechaza el duplicado.
        parser.add_argument('--objetivo', required=True, help='Versión objetivo del POS, ej. 3.0.2.28.')
        parser.add_argument(
            '--aplicar', action='store_true',
            help='Escribe los cambios. Sin esto solo informa qué haría.',
        )
        parser.add_argument(
            '--grupos',
            help='Códigos separados por comas para acotar. Vacío = todos los grupos que '
                 'tengan al menos una farmacia.',
        )
        parser.add_argument(
            '--pisar', action='store_true',
            help='Cambia también los grupos que YA tienen un objetivo distinto. Sin esto '
                 'solo se completan los vacíos: un objetivo ya cargado pudo ponerlo alguien '
                 'que sabe algo que este comando no.',
        )

    @transaction.atomic
    def handle(self, *args, **options):
        version = options['objetivo'].strip()
        if not FORMATO_VERSION.match(version):
            raise CommandError(
                'La versión "%s" no tiene forma de versión (se esperan números separados '
                'por puntos, ej. 3.0.2.28).' % version,
            )

        grupos = Grupo.objects.annotate(n=Count('farmacias')).filter(n__gt=0).order_by('-n')
        if options['grupos']:
            pedidos = [c.strip().upper() for c in options['grupos'].split(',') if c.strip()]
            grupos = grupos.filter(codigo__in=pedidos)
            faltantes = set(pedidos) - set(grupos.values_list('codigo', flat=True))
            if faltantes:
                raise CommandError(
                    'No existen (o no tienen farmacias): %s.' % ', '.join(sorted(faltantes)),
                )

        cambiados = farmacias_alcanzadas = 0
        for grupo in grupos:
            if grupo.version_objetivo == version:
                self.stdout.write('  %-12s ya estaba en %s' % (grupo.codigo, version))
                continue
            if grupo.version_objetivo and not options['pisar']:
                self.stdout.write(self.style.WARNING(
                    '  %-12s tiene %s y se deja igual (usá --pisar para cambiarlo)'
                    % (grupo.codigo, grupo.version_objetivo),
                ))
                continue

            anterior = grupo.version_objetivo or '(vacío)'
            self.stdout.write('  %-12s %s -> %s   (%d farmacias)' % (
                grupo.codigo, anterior, version, grupo.n,
            ))
            cambiados += 1
            farmacias_alcanzadas += grupo.n
            if options['aplicar']:
                grupo.version_objetivo = version
                grupo.save(update_fields=['version_objetivo'])

        self.stdout.write('')
        verbo = 'Cambiados' if options['aplicar'] else 'Se cambiarían'
        self.stdout.write(self.style.SUCCESS(
            '%s: %d grupo(s), %d farmacia(s) alcanzadas.' % (verbo, cambiados, farmacias_alcanzadas),
        ))
        if not options['aplicar']:
            self.stdout.write(self.style.WARNING('Simulación: no se escribió nada. Repetí con --aplicar.'))
