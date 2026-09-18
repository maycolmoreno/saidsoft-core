"""Worker de larga duración que atiende las consultas por Telegram (/enlaces, /estado,
/alertas, /farmacia) — ver apps.monitoreo.telegram_bot para qué responde cada una.

**Long polling y no webhook.** Un webhook exige que Telegram alcance este servidor desde
Internet con HTTPS y certificado válido; el servidor vive en 10.111.6.20, red interna,
detrás de un nginx con certificado autofirmado. `getUpdates` invierte la dirección: es
este proceso el que sale a buscar, así que no hace falta abrir nada.

El `timeout=30` del lado de Telegram es lo que hace que esto no sea polling agresivo: la
llamada queda colgada hasta que hay un mensaje o pasan 30 segundos, así que un comando se
contesta en uno o dos segundos y en reposo son dos requests por minuto, no miles.

Calco de run_meshcentral_worker: mismo patrón de latido en hilo aparte y manejo de
SIGTERM/SIGINT para un apagado limpio.

Uso: python manage.py run_telegram_bot
"""
import json
import logging
import signal
import threading
import urllib.error
import urllib.request

from django.conf import settings
from django.core.management.base import BaseCommand

from apps.catalogo.db import cerrar_conexiones_viejas
from apps.monitoreo.telegram_bot import procesar_actualizacion
from apps.mqtt_worker.models import WorkerHeartbeat

logger = logging.getLogger(__name__)

NOMBRE_WORKER_TELEGRAM = 'telegram_bot'
LATIDO_INTERVALO_SEGUNDOS = 30

# Telegram mantiene la conexión abierta hasta este tiempo esperando un mensaje. El
# timeout del socket va más alto que el del servidor para no cortar una espera legítima.
ESPERA_LARGA_SEGUNDOS = 30
TIMEOUT_SOCKET_SEGUNDOS = ESPERA_LARGA_SEGUNDOS + 15

# Backoff ante fallos de red: no vale la pena reintentar cada 100 ms contra una API que
# no responde, ni esperar minutos cuando vuelve.
ESPERA_MINIMA_TRAS_ERROR = 2
ESPERA_MAXIMA_TRAS_ERROR = 60


class Command(BaseCommand):
    help = 'Atiende consultas de solo lectura por Telegram (/enlaces, /estado, /alertas, /farmacia).'

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._detener = threading.Event()

    def handle(self, *args, **options):
        token = getattr(settings, 'TELEGRAM_BOT_TOKEN', '')
        autorizados = getattr(settings, 'TELEGRAM_CHAT_IDS_AUTORIZADOS', [])
        if not token:
            self.stdout.write(self.style.WARNING(
                'TELEGRAM_BOT_TOKEN vacío — run_telegram_bot no hace nada.'))
            return
        if not autorizados:
            # Arrancar igual sería peor que no arrancar: el bot consumiría los mensajes
            # sin contestarle a nadie, y desde afuera parecería que está roto.
            self.stdout.write(self.style.WARNING(
                'TELEGRAM_CHAT_IDS_AUTORIZADOS vacío — nadie podría consultar, no se arranca.'))
            return

        signal.signal(signal.SIGTERM, self._manejar_apagado)
        signal.signal(signal.SIGINT, self._manejar_apagado)
        threading.Thread(target=self._latido_periodico, daemon=True).start()

        self.stdout.write(self.style.SUCCESS(
            f'Bot de Telegram escuchando ({len(autorizados)} chat(s) autorizado(s)).'))
        self._bucle(token)
        self.stdout.write(self.style.NOTICE('Bot de Telegram detenido.'))

    def _bucle(self, token):
        # `offset` confirma a Telegram qué updates ya se procesaron. Sin él, cada
        # getUpdates devolvería lo mismo para siempre y el bot contestaría el mismo
        # comando una y otra vez.
        offset = None
        espera_error = ESPERA_MINIMA_TRAS_ERROR
        while not self._detener.is_set():
            try:
                updates = self._pedir_updates(token, offset)
                espera_error = ESPERA_MINIMA_TRAS_ERROR
            except Exception:
                logger.warning('Telegram: fallo consultando updates, reintentando.', exc_info=True)
                self._detener.wait(espera_error)
                espera_error = min(espera_error * 2, ESPERA_MAXIMA_TRAS_ERROR)
                continue

            for update in updates:
                offset = update.get('update_id', 0) + 1
                # Cada consulta abre su propia conexión a la base: este proceso vive
                # días, y una conexión vieja de Postgres se cae sin avisar (mismo motivo
                # que `cerrar_conexiones_viejas` en el worker MQTT).
                cerrar_conexiones_viejas()
                procesar_actualizacion(update)

    def _pedir_updates(self, token, offset):
        # `callback_query` es obligatorio acá: Telegram NO entrega los botones del teclado
        # inline si no están en allowed_updates, y el bot se vería como si los ignorara.
        cuerpo = {
            'timeout': ESPERA_LARGA_SEGUNDOS,
            'allowed_updates': ['message', 'callback_query'],
        }
        if offset is not None:
            cuerpo['offset'] = offset
        req = urllib.request.Request(
            f'https://api.telegram.org/bot{token}/getUpdates',
            data=json.dumps(cuerpo).encode('utf-8'),
            method='POST', headers={'Content-Type': 'application/json'},
        )
        with urllib.request.urlopen(req, timeout=TIMEOUT_SOCKET_SEGUNDOS) as respuesta:
            datos = json.loads(respuesta.read().decode('utf-8'))
        return datos.get('result', []) if datos.get('ok') else []

    def _registrar_latido(self):
        cerrar_conexiones_viejas()
        from django.utils import timezone
        WorkerHeartbeat.objects.update_or_create(
            nombre=NOMBRE_WORKER_TELEGRAM, defaults={'ultimo_latido': timezone.now()},
        )

    def _latido_periodico(self):
        while not self._detener.wait(LATIDO_INTERVALO_SEGUNDOS):
            self._registrar_latido()

    def _manejar_apagado(self, signum, frame):
        self.stdout.write(self.style.NOTICE('Señal de apagado recibida, cerrando el bot...'))
        self._detener.set()
