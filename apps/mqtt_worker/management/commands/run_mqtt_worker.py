"""Worker MQTT de larga duración: reemplaza a projectNodeJS/index.js.

Escucha los tópicos de enrolamiento, heartbeat y reporte de estado de
despliegues que publican los agentes, y actualiza la base de datos.

Uso: python manage.py run_mqtt_worker
"""
import json
import logging
import signal
import threading

import paho.mqtt.client as mqtt
from django.conf import settings
from django.core.management.base import BaseCommand
from django.utils import timezone

from apps.mqtt_worker.services import (
    NOMBRE_WORKER_MQTT, manejar_enrolamiento, manejar_estado_despliegue, manejar_estado_instalacion,
    manejar_estado_script, manejar_heartbeat, manejar_info_equipo, manejar_metricas, manejar_windows_update,
    registrar_latido_worker, registrar_mensaje_fallido,
)

logger = logging.getLogger(__name__)

TOPICO_ENROLAMIENTO = '/saidsof/enrolamiento/solicitar/'
TOPICO_HEARTBEAT = '/saidsof/agente/+/heartbeat/'
TOPICO_ESTADO_DESPLIEGUE = '/saidsof/agente/+/despliegue_estado/'
TOPICO_METRICAS = '/saidsof/agente/+/metricas/'
TOPICO_INFO_EQUIPO = '/saidsof/agente/+/info_equipo/'
TOPICO_ESTADO_SCRIPT = '/saidsof/agente/+/script_estado/'
TOPICO_ESTADO_INSTALACION = '/saidsof/agente/+/software_estado/'
TOPICO_WINDOWS_UPDATE = '/saidsof/agente/+/windows_update/'

# El latido se guarda como mucho cada N segundos (no en cada mensaje): a la
# frecuencia de heartbeat/métricas de 1.800+ estaciones, escribirlo en cada
# mensaje sería overhead de BD sin aportar nada — el dashboard solo necesita
# saber "sigue vivo hace poco", no el segundo exacto del último mensaje.
LATIDO_INTERVALO_SEGUNDOS = 30


def _codigo_desde_topico(topic: str) -> str:
    # /saidsof/agente/{codigo}/heartbeat/  ->  {codigo}
    partes = topic.strip('/').split('/')
    return partes[2] if len(partes) > 2 else ''


class Command(BaseCommand):
    help = 'Escucha MQTT y sincroniza estaciones y despliegues en la base de datos.'

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._client = None
        self._ultimo_latido_guardado = None
        self._detener = threading.Event()

    def handle(self, *args, **options):
        conf = settings.MQTT_CONFIG
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=conf['CLIENT_ID_WORKER'])
        self._client = client

        if conf['USERNAME']:
            client.username_pw_set(conf['USERNAME'], conf['PASSWORD'])
        if conf['USE_TLS']:
            client.tls_set(ca_certs=conf['CA_CERT'] or None)

        client.on_connect = self._on_connect
        client.on_message = self._on_message
        client.on_disconnect = self._on_disconnect
        # loop_forever() ya reintenta solo tras una desconexión, pero con el backoff
        # por defecto de paho (hasta 120s entre intentos) — para un worker que sostiene
        # el estado de 1.800 estaciones, un techo más bajo achica la ventana ciega tras
        # una caída de red/broker. La resuscripción a los tópicos ocurre sola: _on_connect
        # se vuelve a disparar en cada reconexión exitosa.
        client.reconnect_delay_set(min_delay=1, max_delay=30)

        # SIGTERM es lo que envía `docker stop`/un redeploy. Sin este handler, el
        # proceso muere de golpe pudiendo dejar un mensaje a medio procesar; con él,
        # loop_forever() termina su iteración actual y sale limpio.
        signal.signal(signal.SIGTERM, self._manejar_apagado)
        signal.signal(signal.SIGINT, self._manejar_apagado)

        # Latido propio en un hilo aparte, independiente de que lleguen mensajes: antes
        # solo se refrescaba desde _on_message, así que un worker sano pero sin tráfico
        # (pocas estaciones conectadas, o ninguna todavía) se veía "sin señal" en el
        # dashboard a los 90s (ver WORKER_MQTT_UMBRAL_SEGUNDOS en panel/views/dashboard.py)
        # aunque siguiera conectado al broker sin problema.
        threading.Thread(target=self._latido_periodico, daemon=True).start()

        self.stdout.write(self.style.NOTICE(f'Conectando a {conf["HOST"]}:{conf["PORT"]}...'))
        client.connect(conf['HOST'], conf['PORT'], keepalive=60)
        client.loop_forever()
        self.stdout.write(self.style.NOTICE('Worker MQTT detenido.'))

    def _latido_periodico(self):
        while not self._detener.wait(LATIDO_INTERVALO_SEGUNDOS):
            registrar_latido_worker(NOMBRE_WORKER_MQTT)

    def _manejar_apagado(self, signum, frame):
        self.stdout.write(self.style.NOTICE('Señal de apagado recibida, desconectando del broker...'))
        self._detener.set()
        if self._client is not None:
            self._client.disconnect()

    def _on_connect(self, client, userdata, flags, reason_code, properties=None):
        if reason_code != 0:
            self.stdout.write(self.style.ERROR(f'[MQTT] Falló la conexión: {reason_code}'))
            return
        self.stdout.write(self.style.SUCCESS('[MQTT] Conectado al broker'))
        client.subscribe(TOPICO_ENROLAMIENTO)
        client.subscribe(TOPICO_HEARTBEAT)
        # QoS 1 solo en los reportes de resultado: perder un heartbeat/métrica entre
        # miles no importa (QoS 0, más liviano), pero perder el "ok"/"error" de un
        # despliegue o script deja ese resultado estancado en el panel para siempre
        # — ni el agente ni el broker lo reintentan por su cuenta a QoS 0.
        client.subscribe(TOPICO_ESTADO_DESPLIEGUE, qos=1)
        client.subscribe(TOPICO_METRICAS)
        client.subscribe(TOPICO_INFO_EQUIPO)
        client.subscribe(TOPICO_ESTADO_SCRIPT, qos=1)
        client.subscribe(TOPICO_ESTADO_INSTALACION, qos=1)
        client.subscribe(TOPICO_WINDOWS_UPDATE)
        registrar_latido_worker(NOMBRE_WORKER_MQTT)

    def _on_disconnect(self, client, userdata, flags, reason_code, properties=None):
        self.stdout.write(self.style.WARNING(f'[MQTT] Desconectado ({reason_code}), reintentando...'))

    def _registrar_latido_si_corresponde(self):
        ahora = timezone.now()
        if (
            self._ultimo_latido_guardado is not None
            and (ahora - self._ultimo_latido_guardado).total_seconds() < LATIDO_INTERVALO_SEGUNDOS
        ):
            return
        registrar_latido_worker(NOMBRE_WORKER_MQTT)
        self._ultimo_latido_guardado = ahora

    def _on_message(self, client, userdata, msg):
        self._registrar_latido_si_corresponde()

        try:
            payload = json.loads(msg.payload.decode('utf-8'))
        except (UnicodeDecodeError, json.JSONDecodeError):
            logger.warning('Mensaje no-JSON descartado en %s', msg.topic)
            registrar_mensaje_fallido(
                topico=msg.topic, payload_crudo=repr(msg.payload), error='Payload no es JSON válido',
            )
            return

        try:
            if msg.topic == TOPICO_ENROLAMIENTO:
                self._responder_enrolamiento(client, payload)
            elif msg.topic.startswith('/saidsof/agente/') and msg.topic.endswith('/heartbeat/'):
                manejar_heartbeat(_codigo_desde_topico(msg.topic), payload)
            elif msg.topic.startswith('/saidsof/agente/') and msg.topic.endswith('/despliegue_estado/'):
                manejar_estado_despliegue(_codigo_desde_topico(msg.topic), payload)
            elif msg.topic.startswith('/saidsof/agente/') and msg.topic.endswith('/metricas/'):
                manejar_metricas(_codigo_desde_topico(msg.topic), payload)
            elif msg.topic.startswith('/saidsof/agente/') and msg.topic.endswith('/info_equipo/'):
                manejar_info_equipo(_codigo_desde_topico(msg.topic), payload)
            elif msg.topic.startswith('/saidsof/agente/') and msg.topic.endswith('/script_estado/'):
                manejar_estado_script(_codigo_desde_topico(msg.topic), payload)
            elif msg.topic.startswith('/saidsof/agente/') and msg.topic.endswith('/software_estado/'):
                manejar_estado_instalacion(_codigo_desde_topico(msg.topic), payload)
            elif msg.topic.startswith('/saidsof/agente/') and msg.topic.endswith('/windows_update/'):
                manejar_windows_update(_codigo_desde_topico(msg.topic), payload)
        except Exception as exc:
            # Antes esto solo quedaba en el log del proceso — fácil de no ver hasta
            # que ya importa. Ahora además queda en una cola de revisión manual.
            logger.exception('Error procesando mensaje de %s', msg.topic)
            registrar_mensaje_fallido(topico=msg.topic, payload_crudo=json.dumps(payload), error=str(exc))

    def _responder_enrolamiento(self, client, payload):
        respuesta = manejar_enrolamiento(payload)
        codigo = payload.get('codigo', '')
        client.publish(f'/saidsof/enrolamiento/respuesta/{codigo}/', json.dumps(respuesta), retain=False)
