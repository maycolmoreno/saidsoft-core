"""Verifica que el propio SAIDSOFT esté sano. Sale con código 1 si algo no lo está.

Por qué hace falta: el sistema vigila 700 farmacias y no había nada vigilándolo a él.
Si el worker MQTT se cuelga sin morir, Docker lo reporta "corriendo"; si Celery Beat
deja de disparar, las tareas simplemente no ocurren. En los dos casos el síntoma es
ausencia de datos, que se descubre tarde y por casualidad — alguien nota que hace rato
no llega nada.

Está pensado para correr DESDE AFUERA del stack, con un temporizador de systemd igual
que el respaldo. Esa es la diferencia que importa: una alerta generada por Celery no
sirve para avisar que Celery se cayó. Un proceso externo sí.

El código de salida es el contrato: 0 = todo bien, 1 = algo mal. Así un temporizador o
un monitor externo puede avisar sin interpretar el texto.

    python manage.py verificar_salud
    python manage.py verificar_salud --silencioso    # solo los problemas

Los umbrales salen de `apps.panel.views.dashboard` a propósito: son los mismos que ya
usa el panel para pintar de rojo. Duplicarlos acá haría que la consola y la pantalla
discreparan sobre qué es "sano".
"""
from django.core.management.base import BaseCommand, CommandError
from django.db import connection
from django.utils import timezone

from apps.mqtt_worker.management.commands.registrar_latido import NOMBRE_RESPALDO
from apps.mqtt_worker.models import WorkerHeartbeat
from apps.mqtt_worker.services import NOMBRE_WORKER_MQTT
from apps.panel.views.dashboard import RESPALDO_UMBRAL_HORAS, WORKER_MQTT_UMBRAL_SEGUNDOS

NOMBRE_WORKER_MESHCENTRAL = 'meshcentral_worker'

# Celery Beat no registra latido propio; se infiere de que las tareas periódicas estén
# escribiendo. La más frecuente corre cada 5 minutos, así que 20 sin una sola muestra
# nueva significa que el planificador no está disparando.
BEAT_UMBRAL_MINUTOS = 20

# Por debajo de esto, una purga o un respaldo pueden fallar a mitad de camino y dejar la
# base en un estado peor que el que tenían.
DISCO_LIBRE_MINIMO_PCT = 10


class Command(BaseCommand):
    help = 'Verifica la salud del propio SAIDSOFT. Código de salida 1 si algo falla.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--silencioso', action='store_true',
            help='Imprime solo los problemas. Útil para un temporizador que manda correo '
                 'solo cuando hay salida.',
        )
        parser.add_argument(
            '--solo',
            help='Verifica un único componente (ej. mqtt_worker). Para el healthcheck de '
                 'un contenedor, que debe medirse a sí mismo y no al sistema entero.',
        )

    def handle(self, *args, **options):
        silencioso = options['silencioso']
        solo = options['solo']
        problemas = []
        revisados = []

        def revisar(nombre, ok, detalle):
            revisados.append(nombre)
            # Acotar acá y no en cada _revisar_* mantiene un solo lugar donde se decide
            # qué se mira. Importa porque el healthcheck de un contenedor tiene que
            # medirse a SÍ MISMO: si el worker MQTT se marcara enfermo por un respaldo
            # atrasado, Docker lo reiniciaría en loop sin arreglar nada.
            if solo and nombre != solo:
                return
            if ok:
                if not silencioso:
                    self.stdout.write(self.style.SUCCESS('  OK      %-22s %s' % (nombre, detalle)))
            else:
                problemas.append(nombre)
                self.stdout.write(self.style.ERROR('  PROBLEMA %-21s %s' % (nombre, detalle)))

        if not silencioso:
            self.stdout.write('Salud de SAIDSOFT — %s' % timezone.localtime().strftime('%Y-%m-%d %H:%M:%S'))

        self._revisar_base(revisar)
        self._revisar_latidos(revisar)
        self._revisar_beat(revisar)
        self._revisar_disco(revisar)

        if solo and solo not in revisados:
            raise CommandError(
                'No existe el componente "%s". Verificables: %s.'
                % (solo, ', '.join(revisados)),
            )

        if problemas:
            self.stdout.write('')
            self.stdout.write(self.style.ERROR(
                '%d problema(s): %s' % (len(problemas), ', '.join(problemas)),
            ))
            # SystemExit y no CommandError: CommandError imprime "CommandError:" y sale
            # con 1 igual, pero acá el texto ya se explicó arriba y repetirlo confunde.
            raise SystemExit(1)

        if not silencioso:
            self.stdout.write('')
            self.stdout.write(self.style.SUCCESS('Todo sano.'))

    def _revisar_base(self, revisar):
        try:
            with connection.cursor() as cursor:
                cursor.execute('SELECT 1')
                cursor.fetchone()
            revisar('base de datos', True, 'responde')
        except Exception as exc:
            revisar('base de datos', False, 'no responde: %s' % exc)

    def _revisar_latidos(self, revisar):
        ahora = timezone.now()
        latidos = {h.nombre: h.ultimo_latido for h in WorkerHeartbeat.objects.all()}

        for nombre, umbral_segundos in (
            (NOMBRE_WORKER_MQTT, WORKER_MQTT_UMBRAL_SEGUNDOS),
            (NOMBRE_WORKER_MESHCENTRAL, WORKER_MQTT_UMBRAL_SEGUNDOS),
        ):
            ultimo = latidos.get(nombre)
            if ultimo is None:
                revisar(nombre, False, 'nunca registró un latido')
                continue
            atraso = (ahora - ultimo).total_seconds()
            revisar(
                nombre, atraso <= umbral_segundos,
                'último latido hace %.0f s (umbral %d s)' % (atraso, umbral_segundos),
            )

        ultimo = latidos.get(NOMBRE_RESPALDO)
        if ultimo is None:
            # Distinto de "atrasado": nunca corrió. Puede ser una instalación nueva, así
            # que se avisa sin marcarlo como problema — lo contrario llenaría de ruido el
            # primer día de cualquier despliegue.
            revisar('respaldo', True, 'sin registro todavía (¿instalación nueva?)')
        else:
            horas = (ahora - ultimo).total_seconds() / 3600
            revisar(
                'respaldo', horas <= RESPALDO_UMBRAL_HORAS,
                'último hace %.1f h (umbral %d h)' % (horas, RESPALDO_UMBRAL_HORAS),
            )

    def _revisar_beat(self, revisar):
        """Celery Beat no deja latido propio: se infiere de que sus tareas escriban.

        Se mira la muestra de red más reciente porque su tarea es la más frecuente (cada
        5 minutos). Si Beat dejara de disparar, esta tabla es la primera en quedarse
        quieta.
        """
        from apps.monitoreo.models import MuestraRedFarmacia

        ultima = MuestraRedFarmacia.objects.order_by('-timestamp').values_list('timestamp', flat=True).first()
        if ultima is None:
            revisar('celery beat', True, 'sin muestras todavía (¿ninguna farmacia sondeable?)')
            return
        minutos = (timezone.now() - ultima).total_seconds() / 60
        revisar(
            'celery beat', minutos <= BEAT_UMBRAL_MINUTOS,
            'última muestra hace %.0f min (umbral %d min)' % (minutos, BEAT_UMBRAL_MINUTOS),
        )

    def _revisar_disco(self, revisar):
        import shutil

        from django.conf import settings

        try:
            uso = shutil.disk_usage(settings.MEDIA_ROOT)
        except OSError as exc:
            revisar('disco', False, 'no se pudo medir: %s' % exc)
            return
        libre_pct = uso.free / uso.total * 100
        revisar(
            'disco', libre_pct >= DISCO_LIBRE_MINIMO_PCT,
            '%.1f%% libre de %.1f GB (mínimo %d%%)' % (
                libre_pct, uso.total / 1024 ** 3, DISCO_LIBRE_MINIMO_PCT,
            ),
        )
