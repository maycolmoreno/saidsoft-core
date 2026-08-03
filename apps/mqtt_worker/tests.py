import json
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from cryptography.fernet import Fernet
from django.test import TestCase, override_settings
from django.utils import timezone

from apps.catalogo.models import ClaveRecuperacionBitLocker, Estacion, Farmacia, Grupo, UnidadNegocio
from apps.mqtt_worker.management.commands.run_mqtt_worker import (
    TOPICO_ESTADO_DESPLIEGUE, TOPICO_ESTADO_SCRIPT, TOPICO_HEARTBEAT, Command,
)
from apps.mqtt_worker.models import MensajeMqttFallido, WorkerHeartbeat
from apps.mqtt_worker.services import (
    NOMBRE_WORKER_MQTT, manejar_info_equipo, registrar_latido_worker, registrar_mensaje_fallido,
)

BITLOCKER_KEY_TEST = Fernet.generate_key().decode()


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


@override_settings(BITLOCKER_ENCRYPTION_KEY=BITLOCKER_KEY_TEST)
class ManejarInfoEquipoBitlockerTests(TestCase):
    def setUp(self):
        grupo = Grupo.objects.create(codigo='TRX001')
        farmacia = Farmacia.objects.create(
            codigo='ML001', grupo=grupo, unidad_negocio=UnidadNegocio.objects.get(codigo='SG'),
        )
        self.estacion = Estacion.objects.create(
            codigo='ML001-A', farmacia=farmacia, estado_aprobacion=Estacion.EstadoAprobacion.APROBADA,
            token_enrolamiento='tok123',
        )

    def test_guarda_estado_de_cifrado_sin_clave(self):
        manejar_info_equipo('ML001-A', {
            'token': 'tok123', 'bitlocker_habilitado': True, 'bitlocker_metodo_proteccion': 'tpm',
        })
        self.estacion.refresh_from_db()
        self.assertTrue(self.estacion.bitlocker_habilitado)
        self.assertEqual(self.estacion.bitlocker_metodo_proteccion, 'tpm')
        self.assertFalse(ClaveRecuperacionBitLocker.objects.exists())

    def test_clave_de_recuperacion_se_guarda_cifrada_nunca_en_texto_plano(self):
        clave_real = '111111-222222-333333-444444-555555-666666-777777-888888'
        manejar_info_equipo('ML001-A', {
            'token': 'tok123', 'bitlocker_habilitado': True, 'bitlocker_metodo_proteccion': 'tpm',
            'bitlocker_clave_recuperacion': clave_real, 'bitlocker_id_protector': 'ABC-123',
        })

        fila = ClaveRecuperacionBitLocker.objects.get(estacion=self.estacion)
        self.assertNotEqual(fila.clave_cifrada, clave_real)  # nunca texto plano en la BD
        self.assertNotIn(clave_real, fila.clave_cifrada)
        self.assertEqual(fila.id_protector, 'ABC-123')

        from apps.catalogo import crypto
        self.assertEqual(crypto.descifrar(fila.clave_cifrada), clave_real)

    def test_reportar_de_nuevo_actualiza_la_clave_existente_no_duplica_fila(self):
        manejar_info_equipo('ML001-A', {'token': 'tok123', 'bitlocker_clave_recuperacion': 'clave-vieja'})
        manejar_info_equipo('ML001-A', {'token': 'tok123', 'bitlocker_clave_recuperacion': 'clave-nueva'})

        self.assertEqual(ClaveRecuperacionBitLocker.objects.filter(estacion=self.estacion).count(), 1)
        from apps.catalogo.services import obtener_clave_bitlocker_descifrada
        self.assertEqual(obtener_clave_bitlocker_descifrada(self.estacion), 'clave-nueva')

    def test_token_invalido_no_guarda_nada(self):
        manejar_info_equipo('ML001-A', {'token': 'token-equivocado', 'bitlocker_habilitado': True})
        self.estacion.refresh_from_db()
        self.assertIsNone(self.estacion.bitlocker_habilitado)
