"""Sondea por ICMP el enlace de cada farmacia y registra sus caídas.

**Correr desde un host que tenga ruta hacia las IP de las farmacias.** El servidor
central no la tiene (confirmado el 24-ago-2026: 100% de pérdida de ping, sin entrada en
la tabla de rutas), por eso esto NO está programado en Celery Beat — ver el docstring de
`apps.monitoreo.enlaces` y `docs/evaluacion-cresio-enlaces.md`.

    python manage.py sondear_enlaces                    # un barrido y sale
    python manage.py sondear_enlaces --intervalo 60     # queda sondeando (como un servicio)
    python manage.py sondear_enlaces --farmacia ML001   # una sola, para probar la ruta
    python manage.py sondear_enlaces --solo-probar      # no escribe nada, solo reporta

A diferencia de los comandos que escriben en masa sobre el catálogo, este **sí escribe
por defecto**: no está creando ni borrando nada del inventario, solo registrando el
resultado de una medición, y un monitoreo que hay que confirmar a mano no monitorea. Para
verificar la ruta sin tocar la base está `--solo-probar`.
"""
import signal
import time

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

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
        parser.add_argument(
            '--intervalo', type=int, default=0,
            help='Segundos entre barridos. Con esto el comando NO termina: queda sondeando hasta que '
                 'se lo interrumpe (Ctrl+C) o recibe SIGTERM. Es la forma de dejarlo corriendo como '
                 'servicio en el host con ruta. Sin esto hace un solo barrido y sale.',
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

        if options['intervalo']:
            return self._bucle(farmacias, options['intervalo'])

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

    def _bucle(self, farmacias, intervalo):
        """Sondea cada `intervalo` segundos hasta que lo interrumpan.

        Es lo que convierte esto en monitoreo y no en una foto: `Cresio_enlaces` corría
        exactamente así (`run_monitor.py`, ciclo de ~60 s). Se usa un bucle propio en vez
        de Celery Beat porque el worker de Celery vive en el servidor central, que es
        justamente el host que NO tiene ruta.

        Relee las farmacias en cada vuelta: un proceso que queda semanas corriendo tiene
        que enterarse de las farmacias nuevas y de los cambios de IP sin que nadie lo
        reinicie.
        """
        from apps.catalogo.models import Farmacia

        self._parar = False

        def _detener(sig, frame):
            self.stdout.write('\nSeñal recibida, terminando el barrido en curso...')
            self._parar = True

        for s in (signal.SIGINT, signal.SIGTERM, getattr(signal, 'SIGBREAK', None)):
            if s is not None:
                try:
                    signal.signal(s, _detener)
                except (ValueError, OSError):
                    # signal() falla fuera del hilo principal; no es motivo para no sondear.
                    pass

        codigos = [f.codigo for f in farmacias]
        self.stdout.write(f'Sondeando {len(codigos)} enlace(s) cada {intervalo}s. Ctrl+C para terminar.')
        while not self._parar:
            frescas = list(Farmacia.objects.filter(codigo__in=codigos).exclude(ip_router__isnull=True))
            resumen = sondear_enlaces_farmacias(frescas)
            marca = timezone.localtime().strftime('%H:%M:%S')
            if resumen['abortado']:
                self.stderr.write(self.style.ERROR(
                    f'[{marca}] Barrido abortado: {resumen["caidas"]}/{resumen["sondeadas"]} sin responder. '
                    'Este host no tiene ruta. No se registró nada.',
                ))
            else:
                estilo = self.style.WARNING if resumen['caidas'] else self.style.SUCCESS
                self.stdout.write(estilo(
                    f'[{marca}] {resumen["activas"]} activo(s), {resumen["caidas"]} caído(s).',
                ))
            # Espera troceada para que Ctrl+C no tarde un ciclo entero en cortar.
            for _ in range(intervalo):
                if self._parar:
                    break
                time.sleep(1)
        self.stdout.write('Sondeo detenido.')

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
