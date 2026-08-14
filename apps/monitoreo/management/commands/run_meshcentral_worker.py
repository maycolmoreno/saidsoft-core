"""Worker MeshCentral de larga duración: mantiene abierta la conexión al canal de
control (control.ashx) y actualiza EstadoDispositivo(fuente=meshcentral) en tiempo real
a partir de los eventos nodeconnect que empuja el servidor — ver
apps.monitoreo.adapters.meshcentral para el protocolo.

Calco de run_mqtt_worker.py (apps/mqtt_worker/management/commands/): mismo patrón de
latido en hilo aparte + manejo de SIGTERM/SIGINT para un apagado limpio.

Uso: python manage.py run_meshcentral_worker
"""
import logging
import signal
import threading

from django.core.management.base import BaseCommand
from django.db import close_old_connections
from django.utils import timezone

from apps.monitoreo.adapters.meshcentral import AdaptadorMeshCentral
from apps.mqtt_worker.models import WorkerHeartbeat

logger = logging.getLogger(__name__)

# Fila propia en WorkerHeartbeat (mismo modelo que ya usa run_mqtt_worker, es genérico
# por `nombre` — no hace falta uno nuevo).
NOMBRE_WORKER_MESHCENTRAL = 'meshcentral_worker'
LATIDO_INTERVALO_SEGUNDOS = 30


class Command(BaseCommand):
    help = 'Escucha eventos de conectividad de MeshCentral y sincroniza EstadoDispositivo.'

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._detener = threading.Event()
        self._ws = None

    def handle(self, *args, **options):
        adaptador = AdaptadorMeshCentral()
        if not adaptador.configurado():
            # No es un error: MESHCENTRAL_API_CONFIG vacío es el default (ver settings)
            # — el cruce MQTT×MeshCentral simplemente queda inactivo hasta que se
            # complete la configuración, sin romper nada del resto del stack.
            self.stdout.write(self.style.WARNING(
                'MESHCENTRAL_API_CONFIG incompleto — run_meshcentral_worker no hace nada.',
            ))
            return

        signal.signal(signal.SIGTERM, self._manejar_apagado)
        signal.signal(signal.SIGINT, self._manejar_apagado)

        threading.Thread(target=self._latido_periodico, daemon=True).start()

        self.stdout.write(self.style.NOTICE('Conectando a MeshCentral...'))
        adaptador.escuchar_eventos(detener=self._detener, on_conectado=self._on_conectado)
        self.stdout.write(self.style.NOTICE('Worker MeshCentral detenido.'))

    def _on_conectado(self, ws):
        self._ws = ws
        self.stdout.write(self.style.SUCCESS('[MeshCentral] Conectado'))
        self._registrar_latido()

    def _registrar_latido(self):
        close_old_connections()
        WorkerHeartbeat.objects.update_or_create(
            nombre=NOMBRE_WORKER_MESHCENTRAL, defaults={'ultimo_latido': timezone.now()},
        )

    def _latido_periodico(self):
        while not self._detener.wait(LATIDO_INTERVALO_SEGUNDOS):
            self._registrar_latido()

    def _manejar_apagado(self, signum, frame):
        self.stdout.write(self.style.NOTICE('Señal de apagado recibida, cerrando conexión a MeshCentral...'))
        self._detener.set()
        if self._ws is not None:
            try:
                self._ws.close()
            except Exception:
                pass
