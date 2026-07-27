"""Worker MQTT de larga duración: reemplaza a projectNodeJS/index.js.

Escucha los tópicos de enrolamiento, heartbeat y reporte de estado de
despliegues que publican los agentes, y actualiza la base de datos.

Uso: python manage.py run_mqtt_worker
"""
import json
import logging

import paho.mqtt.client as mqtt
from django.conf import settings
from django.core.management.base import BaseCommand

from apps.mqtt_worker.services import (
    manejar_enrolamiento, manejar_estado_despliegue, manejar_estado_script, manejar_heartbeat,
    manejar_metricas,
)

logger = logging.getLogger(__name__)

TOPICO_ENROLAMIENTO = '/saidsof/enrolamiento/solicitar/'
TOPICO_HEARTBEAT = '/saidsof/agente/+/heartbeat/'
TOPICO_ESTADO_DESPLIEGUE = '/saidsof/agente/+/despliegue_estado/'
TOPICO_METRICAS = '/saidsof/agente/+/metricas/'
TOPICO_ESTADO_SCRIPT = '/saidsof/agente/+/script_estado/'


def _codigo_desde_topico(topic: str) -> str:
    # /saidsof/agente/{codigo}/heartbeat/  ->  {codigo}
    partes = topic.strip('/').split('/')
    return partes[2] if len(partes) > 2 else ''


class Command(BaseCommand):
    help = 'Escucha MQTT y sincroniza estaciones y despliegues en la base de datos.'

    def handle(self, *args, **options):
        conf = settings.MQTT_CONFIG
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=conf['CLIENT_ID_WORKER'])

        if conf['USERNAME']:
            client.username_pw_set(conf['USERNAME'], conf['PASSWORD'])
        if conf['USE_TLS']:
            client.tls_set()

        client.on_connect = self._on_connect
        client.on_message = self._on_message
        client.on_disconnect = self._on_disconnect

        self.stdout.write(self.style.NOTICE(f'Conectando a {conf["HOST"]}:{conf["PORT"]}...'))
        client.connect(conf['HOST'], conf['PORT'], keepalive=60)
        client.loop_forever()

    def _on_connect(self, client, userdata, flags, reason_code, properties=None):
        if reason_code != 0:
            self.stdout.write(self.style.ERROR(f'[MQTT] Falló la conexión: {reason_code}'))
            return
        self.stdout.write(self.style.SUCCESS('[MQTT] Conectado al broker'))
        client.subscribe(TOPICO_ENROLAMIENTO)
        client.subscribe(TOPICO_HEARTBEAT)
        client.subscribe(TOPICO_ESTADO_DESPLIEGUE)
        client.subscribe(TOPICO_METRICAS)
        client.subscribe(TOPICO_ESTADO_SCRIPT)

    def _on_disconnect(self, client, userdata, flags, reason_code, properties=None):
        self.stdout.write(self.style.WARNING(f'[MQTT] Desconectado ({reason_code}), reintentando...'))

    def _on_message(self, client, userdata, msg):
        try:
            payload = json.loads(msg.payload.decode('utf-8'))
        except (UnicodeDecodeError, json.JSONDecodeError):
            logger.warning('Mensaje no-JSON descartado en %s', msg.topic)
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
            elif msg.topic.startswith('/saidsof/agente/') and msg.topic.endswith('/script_estado/'):
                manejar_estado_script(_codigo_desde_topico(msg.topic), payload)
        except Exception:
            logger.exception('Error procesando mensaje de %s', msg.topic)

    def _responder_enrolamiento(self, client, payload):
        respuesta = manejar_enrolamiento(payload)
        codigo = payload.get('codigo', '')
        client.publish(f'/saidsof/enrolamiento/respuesta/{codigo}/', json.dumps(respuesta), retain=False)
