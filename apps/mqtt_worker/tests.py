import json
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from django.test import TestCase
from django.utils import timezone

from apps.mqtt_worker.management.commands.run_mqtt_worker import (
    TOPICO_ESTADO_DESPLIEGUE, TOPICO_ESTADO_SCRIPT, TOPICO_HEARTBEAT, Command,
)
from apps.mqtt_worker.models import MensajeMqttFallido, WorkerHeartbeat
from apps.mqtt_worker.services import NOMBRE_WORKER_MQTT, registrar_latido_worker, registrar_mensaje_fallido


def _msg(topic, payload):
    """Imita lo mínimo de un paho.MQTTMessage que el Command usa: .topic y .payload (bytes)."""
    return SimpleNamespace(topic=topic, payload=json.dumps(payload).encode('utf-8'))


class ServiciosHeartbeatYFallidosTests(TestCase):
    def test_registrar_latido_worker_crea_y_actualiza(self):
        registrar_latido_worker(NOMBRE_WORKER_MQTT)
        primero = WorkerHeartbeat.objects.get(nombre=NOMBRE_WORKER_MQTT)

        registrar_latido_worker(NOMBRE_WORKER_MQTT)
        self.assertEqual(WorkerHeartbeat.objects.filter(nombre=NOMBRE_WORKER_MQTT).count(), 1)
        segundo = WorkerHeartbeat.objects.get(nombre=NOMBRE_WORKER_MQTT)
        self.assertGreaterEqual(segundo.ultimo_latido, primero.ultimo_latido)

    def test_registrar_mensaje_fallido_crea_fila_no_revisada(self):
        registrar_mensaje_fallido(topico='/x/y/', payload_crudo='{}', error='boom')
        fallido = MensajeMqttFallido.objects.get()
        self.assertEqual(fallido.topico, '/x/y/')
        self.assertFalse(fallido.revisado)


class OnConnectTests(TestCase):
    def test_suscribe_qos1_solo_en_despliegue_y_script_estado(self):
        cmd = Command()
        client = MagicMock()
        cmd._on_connect(client, None, {}, reason_code=0)

        llamadas = {c.args[0]: c.kwargs.get('qos', 0) for c in client.subscribe.call_args_list}
        self.assertEqual(llamadas[TOPICO_ESTADO_DESPLIEGUE], 1)
        self.assertEqual(llamadas[TOPICO_ESTADO_SCRIPT], 1)
        self.assertEqual(llamadas[TOPICO_HEARTBEAT], 0)

    def test_conectar_registra_latido(self):
        cmd = Command()
        cmd._on_connect(MagicMock(), None, {}, reason_code=0)
        self.assertTrue(WorkerHeartbeat.objects.filter(nombre=NOMBRE_WORKER_MQTT).exists())

    def test_reason_code_distinto_de_cero_no_suscribe_ni_registra_latido(self):
        cmd = Command()
        client = MagicMock()
        cmd._on_connect(client, None, {}, reason_code=1)
        client.subscribe.assert_not_called()
        self.assertFalse(WorkerHeartbeat.objects.exists())


class OnMessageTests(TestCase):
    def test_payload_no_json_se_registra_como_fallido(self):
        cmd = Command()
        msg = SimpleNamespace(topic=TOPICO_HEARTBEAT.replace('+', 'ML001-A'), payload=b'no es json')
        cmd._on_message(MagicMock(), None, msg)

        fallido = MensajeMqttFallido.objects.get()
        self.assertEqual(fallido.topico, msg.topic)
        self.assertIn('JSON', fallido.error)

    def test_excepcion_del_handler_se_registra_como_fallido_no_revienta_el_worker(self):
        cmd = Command()
        topic = '/saidsof/agente/ML001-A/heartbeat/'
        msg = _msg(topic, {'token': 'x'})

        with patch(
            'apps.mqtt_worker.management.commands.run_mqtt_worker.manejar_heartbeat',
            side_effect=ValueError('fallo simulado'),
        ):
            cmd._on_message(MagicMock(), None, msg)  # no debe propagar la excepción

        fallido = MensajeMqttFallido.objects.get()
        self.assertEqual(fallido.topico, topic)
        self.assertIn('fallo simulado', fallido.error)

    def test_mensaje_valido_no_genera_fallido(self):
        cmd = Command()
        topic = '/saidsof/agente/ML001-A/heartbeat/'
        msg = _msg(topic, {'token': 'inexistente'})  # token no coincide con ninguna Estacion: se ignora, no revienta

        with patch('apps.mqtt_worker.management.commands.run_mqtt_worker.manejar_heartbeat') as mock_handler:
            cmd._on_message(MagicMock(), None, msg)

        mock_handler.assert_called_once_with('ML001-A', {'token': 'inexistente'})
        self.assertFalse(MensajeMqttFallido.objects.exists())

    def test_latido_se_registra_al_recibir_mensaje(self):
        cmd = Command()
        msg = _msg('/saidsof/agente/ML001-A/heartbeat/', {'token': 'x'})
        with patch('apps.mqtt_worker.management.commands.run_mqtt_worker.manejar_heartbeat'):
            cmd._on_message(MagicMock(), None, msg)
        self.assertTrue(WorkerHeartbeat.objects.filter(nombre=NOMBRE_WORKER_MQTT).exists())

    def test_latido_no_se_reescribe_antes_del_intervalo(self):
        cmd = Command()
        cmd._ultimo_latido_guardado = timezone.now()
        msg = _msg('/saidsof/agente/ML001-A/heartbeat/', {'token': 'x'})

        with patch('apps.mqtt_worker.management.commands.run_mqtt_worker.manejar_heartbeat'):
            cmd._on_message(MagicMock(), None, msg)

        # No se creó fila porque el guard de intervalo evitó la escritura.
        self.assertFalse(WorkerHeartbeat.objects.exists())

    def test_latido_se_reescribe_pasado_el_intervalo(self):
        cmd = Command()
        cmd._ultimo_latido_guardado = timezone.now() - timedelta(seconds=60)
        msg = _msg('/saidsof/agente/ML001-A/heartbeat/', {'token': 'x'})

        with patch('apps.mqtt_worker.management.commands.run_mqtt_worker.manejar_heartbeat'):
            cmd._on_message(MagicMock(), None, msg)

        self.assertTrue(WorkerHeartbeat.objects.filter(nombre=NOMBRE_WORKER_MQTT).exists())


class ApagadoOrdenadoTests(TestCase):
    def test_sigterm_desconecta_el_cliente(self):
        cmd = Command()
        cliente_falso = MagicMock()
        cmd._client = cliente_falso

        cmd._manejar_apagado(signum=15, frame=None)

        cliente_falso.disconnect.assert_called_once()

    def test_sigterm_sin_cliente_conectado_no_revienta(self):
        cmd = Command()
        cmd._client = None
        cmd._manejar_apagado(signum=15, frame=None)  # no debe lanzar
