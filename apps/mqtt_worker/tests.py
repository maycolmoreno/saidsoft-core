import json
import threading
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from cryptography.fernet import Fernet
from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.utils import timezone

from apps.catalogo.models import ClaveRecuperacionBitLocker, Estacion, Farmacia, Grupo, UnidadNegocio
from apps.despliegues.models import Despliegue, EventoDespliegue, ResultadoDespliegue
from apps.monitoreo.models import MuestraMetrica
from apps.mqtt_worker.management.commands.run_mqtt_worker import (
    TOPICO_ESTADO_DESPLIEGUE, TOPICO_ESTADO_INSTALACION, TOPICO_ESTADO_SCRIPT, TOPICO_HEARTBEAT, Command,
    _codigo_desde_topico,
)
from apps.mqtt_worker.models import MensajeMqttFallido, WorkerHeartbeat
from apps.mqtt_worker.services import (
    NOMBRE_WORKER_MQTT, manejar_enrolamiento, manejar_estado_despliegue, manejar_estado_instalacion,
    manejar_estado_script, manejar_heartbeat, manejar_info_equipo, manejar_metricas, registrar_latido_worker,
    registrar_mensaje_fallido,
)
from apps.scripts.models import EjecucionScript, ResultadoEjecucionScript, Script, TipoScript

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
    def test_suscribe_qos1_solo_en_reportes_de_resultado(self):
        cmd = Command()
        client = MagicMock()
        cmd._on_connect(client, None, {}, reason_code=0)

        llamadas = {c.args[0]: c.kwargs.get('qos', 0) for c in client.subscribe.call_args_list}
        self.assertEqual(llamadas[TOPICO_ESTADO_DESPLIEGUE], 1)
        self.assertEqual(llamadas[TOPICO_ESTADO_SCRIPT], 1)
        self.assertEqual(llamadas[TOPICO_ESTADO_INSTALACION], 1)
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


class LatidoPeriodicoTests(TestCase):
    """El latido del worker debe seguir refrescándose aunque no lleguen mensajes de
    ninguna estación (ej. pocas o ninguna estación conectada todavía) — antes solo se
    escribía desde _on_message, así que un worker sano se veía "sin señal" en el
    dashboard a los 90s sin tráfico."""

    def test_escribe_el_latido_periodicamente_sin_necesitar_mensajes(self):
        cmd = Command()
        with patch('apps.mqtt_worker.management.commands.run_mqtt_worker.LATIDO_INTERVALO_SEGUNDOS', 0.05), \
                patch('apps.mqtt_worker.management.commands.run_mqtt_worker.registrar_latido_worker') as mock_registrar:
            hilo = threading.Thread(target=cmd._latido_periodico, daemon=True)
            hilo.start()
            hilo.join(timeout=0.5)
            cmd._detener.set()
            hilo.join(timeout=1)

        mock_registrar.assert_called_with(NOMBRE_WORKER_MQTT)
        self.assertGreaterEqual(mock_registrar.call_count, 1)

    def test_sigterm_detiene_el_hilo_de_latido(self):
        cmd = Command()
        with patch('apps.mqtt_worker.management.commands.run_mqtt_worker.LATIDO_INTERVALO_SEGUNDOS', 0.05), \
                patch('apps.mqtt_worker.management.commands.run_mqtt_worker.registrar_latido_worker'):
            hilo = threading.Thread(target=cmd._latido_periodico, daemon=True)
            hilo.start()
            cmd._manejar_apagado(signum=15, frame=None)
            hilo.join(timeout=1)

        self.assertFalse(hilo.is_alive())


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

    def test_reportar_sin_cifrar_abre_alerta_si_hay_regla(self):
        from apps.monitoreo.models import Alerta, Metrica, ReglaAlerta

        usuario = User.objects.create_user(username='creador_regla_bl', password='x')
        ReglaAlerta.objects.create(
            nombre='Disco sin cifrar', metrica=Metrica.BITLOCKER_DESHABILITADO, umbral=0, creado_por=usuario,
        )

        manejar_info_equipo('ML001-A', {'token': 'tok123', 'bitlocker_habilitado': False})
        self.assertEqual(Alerta.objects.filter(estado=Alerta.Estado.ABIERTA).count(), 1)

        # Vuelve a cifrarse: la alerta se resuelve sola, no queda abierta para siempre.
        manejar_info_equipo('ML001-A', {'token': 'tok123', 'bitlocker_habilitado': True})
        self.assertEqual(Alerta.objects.filter(estado=Alerta.Estado.ABIERTA).count(), 0)
        self.assertEqual(Alerta.objects.get().estado, Alerta.Estado.RESUELTA)


class ManejarEstadoInstalacionTests(TestCase):
    def setUp(self):
        from apps.software.models import AplicacionCatalogo, DestinoTipo, SolicitudInstalacion, VersionAplicacion

        grupo = Grupo.objects.create(codigo='TRX001')
        farmacia = Farmacia.objects.create(
            codigo='ML001', grupo=grupo, unidad_negocio=UnidadNegocio.objects.get(codigo='SG'),
        )
        self.estacion = Estacion.objects.create(
            codigo='ML001-A', farmacia=farmacia, estado_aprobacion=Estacion.EstadoAprobacion.APROBADA,
            token_enrolamiento='tok123',
        )
        usuario = User.objects.create_user(username='creador_sw', password='x')
        aplicacion = AplicacionCatalogo.objects.create(nombre='Google Chrome', creado_por=usuario)
        version = VersionAplicacion.objects.create(
            aplicacion=aplicacion, version='128.0.0',
            instalador=SimpleUploadedFile('chrome.msi', b'x'),
            comando_instalacion_silenciosa='msiexec /i "{archivo}" /qn',
        )
        self.solicitud = SolicitudInstalacion.objects.create(
            version_aplicacion=version, unidad_negocio=UnidadNegocio.objects.get(codigo='SG'),
            destino_tipo=DestinoTipo.ESTACIONES, creado_por=usuario,
        )

    def test_token_invalido_no_crea_resultado(self):
        from apps.software.models import ResultadoInstalacion

        manejar_estado_instalacion('ML001-A', {
            'token': 'equivocado', 'solicitud_id': self.solicitud.id, 'paso': 'recibido',
        })
        self.assertFalse(ResultadoInstalacion.objects.exists())

    def test_paso_desconocido_no_revienta_ni_crea_nada(self):
        from apps.software.models import ResultadoInstalacion

        manejar_estado_instalacion('ML001-A', {
            'token': 'tok123', 'solicitud_id': self.solicitud.id, 'paso': 'paso_inventado',
        })
        self.assertFalse(ResultadoInstalacion.objects.exists())

    def test_reporta_instalado_actualiza_estado_y_version_y_completa_la_solicitud(self):
        from apps.software.models import EstadoSolicitud, EventoInstalacion, ResultadoInstalacion

        manejar_estado_instalacion('ML001-A', {
            'token': 'tok123', 'solicitud_id': self.solicitud.id, 'paso': 'recibido',
        })
        manejar_estado_instalacion('ML001-A', {
            'token': 'tok123', 'solicitud_id': self.solicitud.id, 'paso': 'instalado',
            'version_instalada': '128.0.0',
        })

        resultado = ResultadoInstalacion.objects.get(solicitud=self.solicitud, estacion=self.estacion)
        self.assertEqual(resultado.estado, ResultadoInstalacion.Estado.INSTALADO)
        self.assertEqual(resultado.version_instalada, '128.0.0')
        self.assertEqual(resultado.eventos.count(), 2)
        self.assertTrue(EventoInstalacion.objects.filter(paso=EventoInstalacion.Paso.INSTALADO).exists())

        # La solicitud pasa a completada porque era el único resultado y ya terminó.
        self.solicitud.estado = EstadoSolicitud.PUBLICANDO
        self.solicitud.save(update_fields=['estado'])
        manejar_estado_instalacion('ML001-A', {
            'token': 'tok123', 'solicitud_id': self.solicitud.id, 'paso': 'instalado',
            'version_instalada': '128.0.0',
        })
        self.solicitud.refresh_from_db()
        self.assertEqual(self.solicitud.estado, EstadoSolicitud.COMPLETADO)

    def test_reporta_error_guarda_el_detalle(self):
        from apps.software.models import ResultadoInstalacion

        manejar_estado_instalacion('ML001-A', {
            'token': 'tok123', 'solicitud_id': self.solicitud.id, 'paso': 'error',
            'detalle': 'msiexec devolvió código 1603',
        })
        resultado = ResultadoInstalacion.objects.get(solicitud=self.solicitud, estacion=self.estacion)
        self.assertEqual(resultado.estado, ResultadoInstalacion.Estado.ERROR)
        self.assertEqual(resultado.detalle_error, 'msiexec devolvió código 1603')


class ManejarEnrolamientoTests(TestCase):
    def setUp(self):
        self.sg = UnidadNegocio.objects.get(codigo='SG')
        self.grupo = Grupo.objects.create(codigo='TRX001')
        self.farmacia = Farmacia.objects.create(codigo='ML001', grupo=self.grupo, unidad_negocio=self.sg)

    def test_estacion_nueva_queda_pendiente_y_responde_farmacia_y_grupo(self):
        resp = manejar_enrolamiento({'codigo': 'ML001-A', 'hardware_id': 'HW1', 'hostname': 'PC1'})
        self.assertTrue(resp['aceptado'])
        self.assertEqual(resp['farmacia'], 'ML001')
        self.assertEqual(resp['grupo'], 'TRX001')
        estacion = Estacion.objects.get(codigo='ML001-A')
        self.assertEqual(estacion.estado_aprobacion, Estacion.EstadoAprobacion.PENDIENTE)
        self.assertEqual(estacion.hardware_id, 'HW1')

    def test_farmacia_inexistente_rechaza_y_no_crea_estacion(self):
        resp = manejar_enrolamiento({'codigo': 'XXX999-A', 'hardware_id': 'HW1'})
        self.assertFalse(resp['aceptado'])
        self.assertFalse(Estacion.objects.filter(codigo='XXX999-A').exists())

    def test_reenrolamiento_mismo_hardware_acepta_y_devuelve_el_token_existente(self):
        estacion = Estacion.objects.create(codigo='ML001-A', farmacia=self.farmacia, hardware_id='HW1')
        resp = manejar_enrolamiento({'codigo': 'ML001-A', 'hardware_id': 'HW1'})
        self.assertTrue(resp['aceptado'])
        self.assertEqual(resp['token'], estacion.token_enrolamiento)

    def test_reenrolamiento_hardware_distinto_rechaza_posible_suplantacion(self):
        Estacion.objects.create(codigo='ML001-A', farmacia=self.farmacia, hardware_id='HW1')
        resp = manejar_enrolamiento({'codigo': 'ML001-A', 'hardware_id': 'HW-OTRO'})
        self.assertFalse(resp['aceptado'])

    def test_trust_on_first_use_fija_hardware_id_si_no_tenia(self):
        Estacion.objects.create(codigo='ML001-A', farmacia=self.farmacia)
        resp = manejar_enrolamiento({'codigo': 'ML001-A', 'hardware_id': 'HW-NUEVO'})
        self.assertTrue(resp['aceptado'])
        self.assertEqual(Estacion.objects.get(codigo='ML001-A').hardware_id, 'HW-NUEVO')


class ManejarHeartbeatTests(TestCase):
    def setUp(self):
        sg = UnidadNegocio.objects.get(codigo='SG')
        grupo = Grupo.objects.create(codigo='TRX001')
        farmacia = Farmacia.objects.create(codigo='ML001', grupo=grupo, unidad_negocio=sg)
        self.estacion = Estacion.objects.create(
            codigo='ML001-A', farmacia=farmacia, estado_aprobacion=Estacion.EstadoAprobacion.APROBADA,
        )

    def test_heartbeat_valido_pone_online_y_actualiza_version(self):
        manejar_heartbeat(self.estacion.codigo, {
            'token': self.estacion.token_enrolamiento, 'version_pos': '4.2.1', 'version_agente': '1.0.0',
        })
        self.estacion.refresh_from_db()
        self.assertEqual(self.estacion.estado_conexion, Estacion.EstadoConexion.ONLINE)
        self.assertEqual(self.estacion.version_pos, '4.2.1')
        self.assertIsNotNone(self.estacion.ultimo_heartbeat)

    def test_token_invalido_no_actualiza_nada(self):
        manejar_heartbeat(self.estacion.codigo, {'token': 'malo', 'version_pos': '9.9.9'})
        self.estacion.refresh_from_db()
        self.assertNotEqual(self.estacion.version_pos, '9.9.9')

    def test_estacion_no_aprobada_no_se_actualiza(self):
        self.estacion.estado_aprobacion = Estacion.EstadoAprobacion.PENDIENTE
        self.estacion.save(update_fields=['estado_aprobacion'])
        manejar_heartbeat(self.estacion.codigo, {'token': self.estacion.token_enrolamiento, 'version_pos': '9.9.9'})
        self.estacion.refresh_from_db()
        self.assertNotEqual(self.estacion.version_pos, '9.9.9')
        self.assertNotEqual(self.estacion.estado_conexion, Estacion.EstadoConexion.ONLINE)


class ManejarEstadoDespliegueTests(TestCase):
    def setUp(self):
        self.sg = UnidadNegocio.objects.get(codigo='SG')
        grupo = Grupo.objects.create(codigo='TRX001')
        farmacia = Farmacia.objects.create(codigo='ML001', grupo=grupo, unidad_negocio=self.sg)
        self.estacion = Estacion.objects.create(
            codigo='ML001-A', farmacia=farmacia, estado_aprobacion=Estacion.EstadoAprobacion.APROBADA,
        )
        usuario = User.objects.create_user(username='u', password='x')
        self.despliegue = Despliegue.objects.create(
            version='4.3.0', archivo=SimpleUploadedFile('pkg.zip', b'x'),
            modo_aplicacion=Despliegue.ModoAplicacion.INMEDIATO, destino_tipo=Despliegue.DestinoTipo.ESTACIONES,
            unidad_negocio=self.sg, estado=Despliegue.Estado.PUBLICANDO, umbral_error_pct=50,
            creado_por=usuario,
        )

    def _reportar(self, paso, **extra):
        manejar_estado_despliegue(self.estacion.codigo, {
            'token': self.estacion.token_enrolamiento, 'despliegue_id': self.despliegue.id,
            'paso': paso, **extra,
        })

    def test_ok_actualiza_version_pos_y_completa_el_resultado(self):
        self._reportar(EventoDespliegue.Paso.OK, version_nueva='4.3.0')
        resultado = ResultadoDespliegue.objects.get(despliegue=self.despliegue, estacion=self.estacion)
        self.assertEqual(resultado.estado, ResultadoDespliegue.Estado.APLICADO)
        self.estacion.refresh_from_db()
        self.assertEqual(self.estacion.version_pos, '4.3.0')
        self.assertTrue(EventoDespliegue.objects.filter(resultado=resultado, paso=EventoDespliegue.Paso.OK).exists())

    def test_ok_de_la_unica_estacion_completa_el_despliegue(self):
        self._reportar(EventoDespliegue.Paso.OK, version_nueva='4.3.0')
        self.despliegue.refresh_from_db()
        self.assertEqual(self.despliegue.estado, Despliegue.Estado.COMPLETADO)

    def test_error_guarda_detalle_y_dispara_el_freno_automatico(self):
        self._reportar(EventoDespliegue.Paso.ERROR, detalle='falló la copia')
        resultado = ResultadoDespliegue.objects.get(despliegue=self.despliegue, estacion=self.estacion)
        self.assertEqual(resultado.estado, ResultadoDespliegue.Estado.ERROR)
        self.assertEqual(resultado.detalle_error, 'falló la copia')
        self.despliegue.refresh_from_db()
        self.assertEqual(self.despliegue.estado, Despliegue.Estado.PAUSADO)  # 100% error >= umbral 50%

    def test_paso_desconocido_no_crea_resultado(self):
        self._reportar('paso_inventado')
        self.assertFalse(ResultadoDespliegue.objects.filter(despliegue=self.despliegue).exists())

    def test_estacion_no_aprobada_se_ignora(self):
        self.estacion.estado_aprobacion = Estacion.EstadoAprobacion.PENDIENTE
        self.estacion.save(update_fields=['estado_aprobacion'])
        self._reportar(EventoDespliegue.Paso.RECIBIDO)
        self.assertFalse(ResultadoDespliegue.objects.filter(despliegue=self.despliegue).exists())


class ManejarInfoEquipoHardwareTests(TestCase):
    def setUp(self):
        sg = UnidadNegocio.objects.get(codigo='SG')
        grupo = Grupo.objects.create(codigo='TRX001')
        farmacia = Farmacia.objects.create(codigo='ML001', grupo=grupo, unidad_negocio=sg)
        self.estacion = Estacion.objects.create(
            codigo='ML001-A', farmacia=farmacia, estado_aprobacion=Estacion.EstadoAprobacion.APROBADA,
        )

    def test_actualiza_datos_de_hardware(self):
        manejar_info_equipo(self.estacion.codigo, {
            'token': self.estacion.token_enrolamiento, 'procesador': 'Intel i5', 'ram_total_mb': 16384,
            'almacenamiento_total_gb': 512,
        })
        self.estacion.refresh_from_db()
        self.assertEqual(self.estacion.procesador, 'Intel i5')
        self.assertEqual(self.estacion.ram_total_mb, 16384)
        self.assertIsNotNone(self.estacion.info_equipo_fecha)


class ManejarEstadoScriptTests(TestCase):
    def setUp(self):
        sg = UnidadNegocio.objects.get(codigo='SG')
        grupo = Grupo.objects.create(codigo='TRX001')
        farmacia = Farmacia.objects.create(codigo='ML001', grupo=grupo, unidad_negocio=sg)
        self.estacion = Estacion.objects.create(
            codigo='ML001-A', farmacia=farmacia, estado_aprobacion=Estacion.EstadoAprobacion.APROBADA,
        )
        usuario = User.objects.create_user(username='u', password='x')
        script = Script.objects.create(
            nombre='test', tipo=TipoScript.POWERSHELL, contenido='echo hola', creado_por=usuario,
        )
        self.ejecucion = EjecucionScript.objects.create(
            script=script, contenido_snapshot=script.contenido, unidad_negocio=sg,
            destino_tipo=EjecucionScript.DestinoTipo.ESTACIONES, creado_por=usuario,
        )
        self.resultado = ResultadoEjecucionScript.objects.create(ejecucion=self.ejecucion, estacion=self.estacion)

    def test_completado_guarda_salida_y_recalcula_estado_de_la_ejecucion(self):
        manejar_estado_script(self.estacion.codigo, {
            'token': self.estacion.token_enrolamiento, 'resultado_id': self.resultado.pk,
            'estado': ResultadoEjecucionScript.Estado.COMPLETADO, 'exit_code': 0, 'stdout': 'ok',
        })
        self.resultado.refresh_from_db()
        self.assertEqual(self.resultado.estado, ResultadoEjecucionScript.Estado.COMPLETADO)
        self.assertEqual(self.resultado.exit_code, 0)
        self.assertEqual(self.resultado.stdout, 'ok')
        self.ejecucion.refresh_from_db()
        self.assertEqual(self.ejecucion.estado, EjecucionScript.Estado.COMPLETADO)

    def test_resultado_inexistente_no_lanza_excepcion(self):
        manejar_estado_script(self.estacion.codigo, {
            'token': self.estacion.token_enrolamiento, 'resultado_id': 999999,
            'estado': ResultadoEjecucionScript.Estado.COMPLETADO,
        })


class ManejarMetricasTests(TestCase):
    def setUp(self):
        sg = UnidadNegocio.objects.get(codigo='SG')
        grupo = Grupo.objects.create(codigo='TRX001')
        farmacia = Farmacia.objects.create(codigo='ML001', grupo=grupo, unidad_negocio=sg)
        self.estacion = Estacion.objects.create(
            codigo='ML001-A', farmacia=farmacia, estado_aprobacion=Estacion.EstadoAprobacion.APROBADA,
            monitorear_recursos=True,
        )

    def test_crea_muestra_metrica(self):
        manejar_metricas(self.estacion.codigo, {
            'token': self.estacion.token_enrolamiento, 'cpu_carga_pct': 42.5, 'ram_total': 8192, 'ram_usada': 4096,
        })
        muestra = MuestraMetrica.objects.get(estacion=self.estacion)
        self.assertEqual(muestra.cpu_carga_pct, 42.5)

    def test_token_invalido_no_crea_nada(self):
        manejar_metricas(self.estacion.codigo, {'token': 'malo', 'cpu_carga_pct': 99})
        self.assertFalse(MuestraMetrica.objects.filter(estacion=self.estacion).exists())


class CodigoDesdeTopicoTests(TestCase):
    def test_extrae_el_codigo_de_estacion(self):
        self.assertEqual(_codigo_desde_topico('/saidsof/agente/ML001-A/heartbeat/'), 'ML001-A')

    def test_topico_corto_devuelve_vacio(self):
        self.assertEqual(_codigo_desde_topico('/saidsof/'), '')


class OnMessageDispatchTests(TestCase):
    """Cubre el enrutamiento por tópico de Command._on_message (run_mqtt_worker.py)
    con los handlers reales (sin mockear), incluida la respuesta de enrolamiento."""

    def setUp(self):
        sg = UnidadNegocio.objects.get(codigo='SG')
        grupo = Grupo.objects.create(codigo='TRX001')
        farmacia = Farmacia.objects.create(codigo='ML001', grupo=grupo, unidad_negocio=sg)
        self.estacion = Estacion.objects.create(
            codigo='ML001-A', farmacia=farmacia, estado_aprobacion=Estacion.EstadoAprobacion.APROBADA,
        )
        self.command = Command()

    def test_heartbeat_dispatcha_a_manejar_heartbeat(self):
        msg = _msg('/saidsof/agente/ML001-A/heartbeat/', {
            'token': self.estacion.token_enrolamiento, 'version_pos': '4.2.1',
        })
        self.command._on_message(MagicMock(), None, msg)
        self.estacion.refresh_from_db()
        self.assertEqual(self.estacion.version_pos, '4.2.1')

    def test_excepcion_del_handler_no_se_propaga(self):
        # 'despliegue_id' ausente + paso válido: el handler intentará crear un
        # ResultadoDespliegue con despliegue_id=None y fallará por FK — el catch-all
        # de _on_message debe absorberlo, no tumbar el worker.
        msg = _msg('/saidsof/agente/ML001-A/despliegue_estado/', {
            'token': self.estacion.token_enrolamiento, 'paso': EventoDespliegue.Paso.RECIBIDO,
        })
        self.command._on_message(MagicMock(), None, msg)  # no debe lanzar

    def test_enrolamiento_publica_respuesta_por_el_topico_correcto(self):
        client = MagicMock()
        msg = _msg('/saidsof/enrolamiento/solicitar/', {'codigo': 'ML001-B', 'hardware_id': 'HW2'})
        self.command._on_message(client, None, msg)
        client.publish.assert_called_once()
        topico_publicado = client.publish.call_args[0][0]
        self.assertEqual(topico_publicado, '/saidsof/enrolamiento/respuesta/ML001-B/')
