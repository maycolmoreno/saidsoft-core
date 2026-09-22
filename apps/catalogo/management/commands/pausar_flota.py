"""Freno de emergencia de la flota: pausa o reanuda el agente de muchas estaciones.

    python manage.py pausar_flota                      # simula: dice a quién frenaría
    python manage.py pausar_flota --aplicar
    python manage.py pausar_flota --reanudar --aplicar
    python manage.py pausar_flota --unidad SG --aplicar
    python manage.py pausar_flota --sin-enrolamiento --aplicar   # además corta las altas

**Por qué un comando y no solo un botón del panel.** La acción masiva publica un mensaje
MQTT y escribe una fila por estación. A ~1.800 eso son 1.800 publicaciones y 1.800
UPDATE; hacerlo dentro de un request HTTP —como hace hoy `_publicar_ejecucion` para los
scripts— depende de que gunicorn no corte a los 600 s y no deja forma de ver el avance.
Acá se ve estación por estación y se puede correr aunque el panel esté caído, que es
justamente uno de los escenarios en los que alguien querría frenar la flota.

**Lo que este freno NO hace**, y conviene tenerlo claro antes de necesitarlo:

- No apaga el agente. La estación sigue conectada y sigue latiendo, a propósito: si se
  callara del todo la verías como caída y perderías visibilidad justo durante la
  emergencia. Deja de ejecutar comandos y de reportar todo lo demás.
- No detiene el POS ni toca la venta. El agente nunca fue parte del circuito de venta.
- No llega instantáneamente a una estación apagada. El mensaje va RETENIDO, así que le
  llega apenas encienda — que es precisamente lo que un freno necesita.
- Publicar no es aplicar. Lo único que confirma el freno es que la estación lo reporte
  en su latido (`Estacion.pausa_confirmada_en`). El resumen final separa las dos cosas.
"""
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.db.models import Q
from django.utils import timezone

from apps.activos.models import UnidadNegocio
from apps.catalogo.models import Estacion
from apps.catalogo.services import enviar_pausa


class Command(BaseCommand):
    help = 'Pausa (o reanuda) el agente de las estaciones aprobadas. Simula salvo --aplicar.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--aplicar', action='store_true',
            help='Publica de verdad. Sin esto solo muestra a quién alcanzaría.',
        )
        parser.add_argument(
            '--reanudar', action='store_true',
            help='Levanta el freno en vez de ponerlo.',
        )
        parser.add_argument(
            '--unidad', default='',
            help='Código de unidad de negocio (SG/MIA/7DIAS). Vacío = todas.',
        )
        parser.add_argument(
            '--estaciones', default='',
            help='Códigos separados por coma, para frenar solo unas pocas.',
        )
        parser.add_argument(
            '--sin-enrolamiento', action='store_true',
            help='Además, deja de aceptar altas de estaciones nuevas (el interruptor de '
                 'ConfiguracionMonitoreo). Al reanudar, vuelve a habilitarlas.',
        )

    def handle(self, *args, **opciones):
        aplicar = opciones['aplicar']
        pausar = not opciones['reanudar']
        verbo = 'PAUSAR' if pausar else 'REANUDAR'

        qs = Estacion.objects.filter(
            estado_aprobacion=Estacion.EstadoAprobacion.APROBADA,
        ).select_related('farmacia__unidad_negocio').order_by('codigo')

        if opciones['unidad']:
            unidad = UnidadNegocio.objects.filter(codigo=opciones['unidad'].upper()).first()
            if unidad is None:
                self.stderr.write(f'No existe la unidad de negocio {opciones["unidad"]!r}.')
                return
            qs = qs.filter(farmacia__unidad_negocio=unidad)

        if opciones['estaciones']:
            codigos = [c.strip().upper() for c in opciones['estaciones'].split(',') if c.strip()]
            qs = qs.filter(codigo__in=codigos)

        # Las que ya están en el estado pedido no se tocan: republicar el retenido no
        # cambia nada y ensucia el resumen de lo que realmente se movió.
        objetivo = list(qs.filter(~Q(pausado=pausar)))
        ya_estaban = qs.filter(pausado=pausar).count()

        self.stdout.write(f'{verbo}: {len(objetivo)} estación(es); {ya_estaban} ya estaban así.')
        if not objetivo:
            self.stdout.write('Nada que hacer.')
            return

        if not aplicar:
            self.stdout.write(self.style.WARNING('SIMULACIÓN — no se publica nada. Repetí con --aplicar.'))
            for e in objetivo[:20]:
                viva = e.ultimo_heartbeat and e.ultimo_heartbeat > timezone.now() - timedelta(minutes=10)
                self.stdout.write(f'  {e.codigo:<14} {"reportando" if viva else "sin heartbeat (le llegará al encender)"}')
            if len(objetivo) > 20:
                self.stdout.write(f'  ... y {len(objetivo) - 20} más')
            if opciones['sin_enrolamiento']:
                self.stdout.write(f'  y {"deshabilitaría" if pausar else "habilitaría"} el alta de estaciones nuevas.')
            return

        publicadas, fallaron = 0, []
        for e in objetivo:
            if enviar_pausa(e, pausado=pausar):
                e.pausa_solicitada_en = timezone.now()
                e.save(update_fields=['pausa_solicitada_en'])
                publicadas += 1
            else:
                fallaron.append(e.codigo)

        if opciones['sin_enrolamiento']:
            from apps.monitoreo.models import ConfiguracionMonitoreo
            config = ConfiguracionMonitoreo.obtener()
            config.enrolamiento_habilitado = not pausar
            config.save(update_fields=['enrolamiento_habilitado'])
            estado = 'DESHABILITADA' if pausar else 'habilitada'
            self.stdout.write(f'Alta de estaciones nuevas: {estado}.')

        self.stdout.write(self.style.SUCCESS(f'Órdenes publicadas: {publicadas}.'))
        if fallaron:
            self.stderr.write(
                f'NO se pudo publicar a {len(fallaron)}: {", ".join(fallaron)}. '
                'Revisá la conexión al broker antes de dar el freno por puesto.',
            )

        # La distinción que importa: publicado no es aplicado.
        self.stdout.write(
            '\nLa orden va retenida. Una estación apagada la recibe al encender, así que '
            'esto todavía NO significa que la flota esté frenada.\n'
            'Confirmá con la columna "pausa confirmada" (la estación lo declara en su '
            'latido) antes de dar por hecho el freno.',
        )
