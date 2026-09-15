"""Carga el ancho contratado de las farmacias, agrupando por proveedor.

`Farmacia.ancho_contratado_mbps` es el denominador que convierte el consumo en algo
interpretable: sin él se puede mostrar "831 kbps" pero no si eso es el 8% del enlace o
el 80%.

Se agrupa por proveedor porque es como está contratado: a 15-sep-2026, 579 farmacias son
TELCONET y 109 PUNTO NET —688 de 700— con 10 Mbps en la mayoría. Las 12 restantes
(FIBROMARK, ETAPA, CLARO, GONET, CORVINET) no se tocan hasta saber cuánto tienen.

Por defecto SIMULA. Y **no pisa** un valor ya cargado salvo que se lo pida: si alguien
cargó 20 Mbps en una farmacia puntual, sabe algo que este comando no.

    python manage.py fijar_ancho_contratado --mbps 10 --proveedores TELCONET,"PUNTO NET"
    python manage.py fijar_ancho_contratado --mbps 10 --proveedores TELCONET --aplicar
    python manage.py fijar_ancho_contratado --mbps 20 --farmacias GNB01,MC001 --aplicar
"""
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from apps.catalogo.models import Farmacia

# Un enlace de farmacia va de unos pocos megas a algunas decenas. El tope no es una regla
# técnica sino una red contra el dedazo: cargar 1000 en vez de 10 dejaría a esa farmacia
# mostrando "0,08% de uso" para siempre y nadie lo miraría dos veces.
MBPS_MAXIMO_RAZONABLE = 1000


class Command(BaseCommand):
    help = 'Fija Farmacia.ancho_contratado_mbps, agrupando por proveedor o por códigos puntuales.'

    def add_arguments(self, parser):
        parser.add_argument('--mbps', required=True, type=int, help='Megabits contratados, ej. 10.')
        parser.add_argument(
            '--proveedores',
            help='Valores de tipo_enlace separados por comas, ej. \'TELCONET,PUNTO NET\'.',
        )
        parser.add_argument('--farmacias', help='Códigos separados por comas, para casos puntuales.')
        parser.add_argument(
            '--aplicar', action='store_true',
            help='Escribe los cambios. Sin esto solo informa qué haría.',
        )
        parser.add_argument(
            '--pisar', action='store_true',
            help='Cambia también las que YA tienen un valor distinto. Sin esto solo se completan '
                 'las vacías: un valor cargado pudo ponerlo alguien que conoce ese contrato.',
        )

    @transaction.atomic
    def handle(self, *args, **options):
        mbps = options['mbps']
        if mbps <= 0 or mbps > MBPS_MAXIMO_RAZONABLE:
            raise CommandError(
                '%s Mbps no parece un ancho contratado razonable (se esperan entre 1 y %d).'
                % (mbps, MBPS_MAXIMO_RAZONABLE),
            )
        if not options['proveedores'] and not options['farmacias']:
            raise CommandError(
                'Indicá --proveedores o --farmacias. Sin filtro esto tocaría las 700 de una, '
                'incluidas las que tienen otro contrato.',
            )

        farmacias = Farmacia.objects.all()
        if options['proveedores']:
            valores = [p.strip().upper() for p in options['proveedores'].split(',') if p.strip()]
            farmacias = farmacias.filter(tipo_enlace__in=valores)
            if not farmacias.exists():
                raise CommandError(
                    'Ninguna farmacia tiene tipo_enlace en %s. Valores cargados hoy: %s.'
                    % (valores, ', '.join(sorted(
                        v for v in Farmacia.objects.values_list('tipo_enlace', flat=True).distinct() if v
                    ))),
                )
        if options['farmacias']:
            codigos = [c.strip().upper() for c in options['farmacias'].split(',') if c.strip()]
            farmacias = farmacias.filter(codigo__in=codigos)
            faltantes = set(codigos) - set(farmacias.values_list('codigo', flat=True))
            if faltantes:
                raise CommandError('No existen: %s.' % ', '.join(sorted(faltantes)))

        ya_estaban = farmacias.filter(ancho_contratado_mbps=mbps).count()
        con_otro = farmacias.exclude(ancho_contratado_mbps=mbps).exclude(
            ancho_contratado_mbps__isnull=True,
        )
        vacias = farmacias.filter(ancho_contratado_mbps__isnull=True)

        self.stdout.write('Alcanzadas: %d farmacia(s).' % farmacias.count())
        self.stdout.write('  ya en %d Mbps: %d' % (mbps, ya_estaban))
        self.stdout.write('  sin dato, se completan: %d' % vacias.count())
        if con_otro.exists():
            etiqueta = 'se cambian' if options['pisar'] else 'se DEJAN como están'
            self.stdout.write(self.style.WARNING(
                '  con otro valor cargado (%s): %d' % (etiqueta, con_otro.count()),
            ))
            for f in con_otro[:10]:
                self.stdout.write(self.style.WARNING(
                    '      %-8s tiene %s Mbps' % (f.codigo, f.ancho_contratado_mbps),
                ))

        a_cambiar = vacias if not options['pisar'] else farmacias.exclude(ancho_contratado_mbps=mbps)
        cuantas = a_cambiar.count()
        if options['aplicar']:
            a_cambiar.update(ancho_contratado_mbps=mbps)

        self.stdout.write('')
        verbo = 'Actualizadas' if options['aplicar'] else 'Se actualizarían'
        self.stdout.write(self.style.SUCCESS('%s: %d farmacia(s) a %d Mbps.' % (verbo, cuantas, mbps)))
        if not options['aplicar']:
            self.stdout.write(self.style.WARNING('Simulación: no se escribió nada. Repetí con --aplicar.'))
