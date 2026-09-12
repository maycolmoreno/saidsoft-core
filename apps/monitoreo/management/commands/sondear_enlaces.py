"""Sondea por ICMP el enlace de cada farmacia y registra sus caídas.

**Correr desde un host que tenga ruta hacia las IP de las farmacias.** El servidor
central no la tiene (confirmado el 24-ago-2026: 100% de pérdida de ping, sin entrada en
la tabla de rutas), por eso esto NO está programado en Celery Beat — ver el docstring de
`apps.monitoreo.enlaces` y `docs/evaluacion-cresio-enlaces.md`.

    python manage.py sondear_enlaces                    # barrido completo
    python manage.py sondear_enlaces --farmacia ML001   # una sola, para probar la ruta
    python manage.py sondear_enlaces --solo-probar      # no escribe nada, solo reporta

A diferencia de los comandos que escriben en masa sobre el catálogo, este **sí escribe
por defecto**: no está creando ni borrando nada del inventario, solo registrando el
resultado de una medición, y un monitoreo que hay que confirmar a mano no monitorea. Para
verificar la ruta sin tocar la base está `--solo-probar`.
"""
from django.core.management.base import BaseCommand, CommandError

from apps.catalogo.models import Farmacia
from apps.monitoreo.enlaces import sondear_enlace, sondear_enlaces_farmacias


class Command(BaseCommand):
    help = 'Sondea por ICMP el enlace de cada farmacia y registra caídas y recuperaciones.'

    def add_arguments(self, parser):
        parser.add_argument('--farmacia', default='', help='Sondear solo esta farmacia (código).')
        parser.add_argument(
            '--solo-probar', action='store_true',
            help='Sondea e informa, sin escribir nada. Sirve para confirmar si este host tiene ruta.',
        )

    def handle(self, *args, **options):
        if options['farmacia']:
            farmacias = list(Farmacia.objects.filter(codigo=options['farmacia'].upper()))
            if not farmacias:
                raise CommandError(f'No existe la farmacia {options["farmacia"].upper()}.')
            if farmacias[0].ip_router is None:
                raise CommandError(
                    f'{farmacias[0].codigo} no tiene `ip_router` cargada. Se carga con '
                    'importar_red_farmacias_xlsx.',
                )
        else:
            farmacias = list(Farmacia.objects.filter(activa=True).exclude(ip_router__isnull=True))
            if not farmacias:
                raise CommandError(
                    'Ninguna farmacia activa tiene `ip_router` cargada: no hay nada que sondear. '
                    'Cargalas con importar_red_farmacias_xlsx.',
                )

        if options['solo_probar']:
            return self._solo_probar(farmacias)

        resumen = sondear_enlaces_farmacias(farmacias)
        if resumen['abortado']:
            self.stderr.write(self.style.ERROR(
                f'Barrido abortado: {resumen["caidas"]} de {resumen["sondeadas"]} farmacias no '
                'respondieron. Eso no son caídas simultáneas — este host no tiene ruta hacia las IP '
                'de las farmacias. No se registró nada.',
            ))
            return
        self.stdout.write(self.style.SUCCESS(
            f'{resumen["sondeadas"]} farmacia(s) sondeada(s): {resumen["activas"]} activa(s), '
            f'{resumen["caidas"]} caída(s).',
        ))

    def _solo_probar(self, farmacias):
        """Sondeo en seco: no toca la base. Para responder "¿este host llega o no?"."""
        activas = 0
        for farmacia in farmacias[:20]:
            alcanzable, latencia = sondear_enlace(str(farmacia.ip_router))
            activas += int(alcanzable)
            detalle = f'{latencia:.0f} ms' if latencia is not None else ('responde' if alcanzable else 'sin respuesta')
            estilo = self.style.SUCCESS if alcanzable else self.style.WARNING
            self.stdout.write(estilo(f'  {farmacia.codigo:<8} {farmacia.ip_router:<16} {detalle}'))
        muestra = min(len(farmacias), 20)
        self.stdout.write(f'\n{activas}/{muestra} respondieron (muestra de {len(farmacias)} con IP cargada).')
        if activas == 0:
            self.stdout.write(self.style.ERROR(
                'Ninguna respondió: este host no tiene ruta hacia las farmacias. Correr el barrido '
                'desde acá registraría caídas falsas.',
            ))
