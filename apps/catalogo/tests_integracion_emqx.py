"""Integración de la cola de órdenes contra un EMQX REAL de laboratorio.

Se saltean si no está el laboratorio. Para correrlas:

    sh deploy/lab/emqx-cola-lab.sh arrancar
    EMQX_LAB_HOST=127.0.0.1 EMQX_LAB_PUERTO=21883 EMQX_LAB_API=http://127.0.0.1:28083/api/v5 \
    EMQX_LAB_ADMIN_PASSWORD=saidsoft-lab-admin-1 EMQX_LAB_CONTENEDOR=saidsoft-lab-emqx \
        python manage.py test apps.catalogo.tests_integracion_emqx --noinput
    sh deploy/lab/emqx-cola-lab.sh detener

El broker de laboratorio tiene la misma versión, autenticación, autorización (`no_match =
deny`) y retainer en disco que producción. Los usuarios se crean acá con claves de
laboratorio. Las ACL del panel y del worker se leen de `deploy/bootstrap-emqx.sh`, y las de la
estación salen de `apps.mqtt_worker.emqx_admin._reglas_para`: son las reglas reales.

La "estación" es un cliente MQTT 5 con la credencial de `LABQ-A`; sus reportes se simulan
llamando al manejador del worker, igual que en producción los procesa `run_mqtt_worker`.
"""
import json
import os
import re
import subprocess
import threading
import time
import urllib.request
from datetime import timedelta
from pathlib import Path
from unittest import skipUnless

import paho.mqtt.client as mqtt
from django.conf import settings
from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.utils import timezone

from apps.catalogo import ordenes
from apps.catalogo.models import Estacion, Farmacia, Grupo, OrdenAgente, UnidadNegocio
from apps.catalogo.services import _publicar_mqtt
from apps.mqtt_worker.emqx_admin import _reglas_para
from apps.mqtt_worker.services import manejar_estado_instalacion
from apps.software.models import AplicacionCatalogo, DestinoTipo, SolicitudInstalacion, VersionAplicacion
from apps.software.services import publicar_solicitud

HOST = os.environ.get('EMQX_LAB_HOST', '')
PUERTO = int(os.environ.get('EMQX_LAB_PUERTO', '21883'))
API = os.environ.get('EMQX_LAB_API', '')
CLAVE_ADMIN = os.environ.get('EMQX_LAB_ADMIN_PASSWORD', '')
CONTENEDOR = os.environ.get('EMQX_LAB_CONTENEDOR', '')

E = OrdenAgente.Estado
CODIGO = 'LABQ-A'
TOPICO = f'/saidsof/agente/{CODIGO}/software/'
USUARIOS = {'lab_panel': 'lab-panel-1', 'lab_worker': 'lab-worker-1', CODIGO: 'lab-estacion-1'}


def _api(metodo, ruta, cuerpo=None, token=None):
    datos = json.dumps(cuerpo).encode() if cuerpo is not None else None
    req = urllib.request.Request(f'{API}{ruta}', data=datos, method=metodo)
    req.add_header('Content-Type', 'application/json')
    if token:
        req.add_header('Authorization', f'Bearer {token}')
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            texto = resp.read().decode()
            return resp.status, (json.loads(texto) if texto else None)
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode()


def _reglas_de_bootstrap(variable):
    texto = (Path(settings.BASE_DIR) / 'deploy' / 'bootstrap-emqx.sh').read_text(encoding='utf-8')
    bloque = re.search(r'definir_acl "\$' + variable + r"\" '(\[.*?\])'", texto, re.S)
    return json.loads(bloque.group(1))


def _config(usuario, clave):
    conf = dict(settings.MQTT_CONFIG)
    conf.update({'HOST': HOST, 'PORT': PUERTO, 'USERNAME': usuario, 'PASSWORD': clave, 'USE_TLS': False, 'CA_CERT': ''})
    return conf


class EstacionSimulada:
    """Cliente MQTT 5 con la credencial de la estación: se suscribe a su tópico y anota lo
    que recibe. `retenido` dice si el broker lo entregó como mensaje retenido."""

    def __init__(self):
        self.mensajes = []
        self._llego = threading.Event()
        self._suscrito = threading.Event()
        self.cliente = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f'lab-{CODIGO}-{time.time_ns()}',
                                   protocol=mqtt.MQTTv5)
        self.cliente.username_pw_set(CODIGO, USUARIOS[CODIGO])
        self.cliente.on_message = self._al_mensaje
        self.cliente.on_subscribe = lambda *a: self._suscrito.set()

    def _al_mensaje(self, _c, _d, msg):
        self.mensajes.append({'payload': msg.payload.decode(), 'retenido': bool(msg.retain)})
        self._llego.set()

    def conectar(self):
        self.cliente.connect(HOST, PUERTO, keepalive=30)
        self.cliente.loop_start()
        self.cliente.subscribe(TOPICO, qos=1)
        assert self._suscrito.wait(5), 'la estación no logró suscribirse'
        return self

    def esperar(self, segundos=3):
        return self._llego.wait(segundos)

    def desconectar(self):
        self.cliente.disconnect()
        self.cliente.loop_stop()


@skipUnless(HOST and API and CLAVE_ADMIN, 'requiere el EMQX de laboratorio: sh deploy/lab/emqx-cola-lab.sh arrancar')
class IntegracionEmqxTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.sg = UnidadNegocio.objects.get(codigo='SG')
        farmacia = Farmacia.objects.create(codigo='LABQ', grupo=Grupo.objects.create(codigo='LABG'), unidad_negocio=cls.sg)
        cls.estacion = Estacion.objects.create(codigo=CODIGO, farmacia=farmacia, estado_aprobacion=Estacion.EstadoAprobacion.APROBADA)
        cls.usuario = User.objects.create_user(username='lab', password='x')
        app = AplicacionCatalogo.objects.create(nombre='Lab App', creado_por=cls.usuario)
        cls.version = VersionAplicacion.objects.create(
            aplicacion=app, version='1.0', instalador=SimpleUploadedFile('lab.msi', b'x'),
            comando_instalacion_silenciosa='msiexec /i "{archivo}" /qn',
        )
        estado, cuerpo = _api('POST', '/login', {'username': 'admin', 'password': CLAVE_ADMIN})
        assert estado == 200, f'login al EMQX de laboratorio: {estado} {cuerpo}'
        token = cuerpo['token']
        reglas = {
            'lab_panel': _reglas_de_bootstrap('MQTT_USERNAME_PANEL'),
            'lab_worker': _reglas_de_bootstrap('MQTT_USERNAME_WORKER'),
            CODIGO: _reglas_para(cls.estacion),
        }
        for usuario, clave in USUARIOS.items():
            _api('POST', '/authentication/password_based:built_in_database/users',
                 {'user_id': usuario, 'password': clave}, token)
            _api('PUT', f'/authentication/password_based:built_in_database/users/{usuario}',
                 {'password': clave}, token)
            estado, cuerpo = _api('PUT', f'/authorization/sources/built_in_database/rules/users/{usuario}',
                                  {'username': usuario, 'rules': reglas[usuario]}, token)
            if estado not in (200, 204):
                estado, cuerpo = _api('POST', '/authorization/sources/built_in_database/rules/users',
                                      [{'username': usuario, 'rules': reglas[usuario]}], token)
            assert estado in (200, 204), f'ACL de {usuario}: {estado} {cuerpo}'

    def setUp(self):
        self.panel = _config('lab_panel', USUARIOS['lab_panel'])
        with override_settings(MQTT_CONFIG=self.panel):
            _publicar_mqtt(TOPICO, '', retain=True)  # ranura limpia en el broker

    def _solicitud(self):
        solicitud = SolicitudInstalacion.objects.create(
            version_aplicacion=self.version, unidad_negocio=self.sg, destino_tipo=DestinoTipo.ESTACIONES,
            creado_por=self.usuario,
        )
        solicitud.estaciones.set([self.estacion])
        return solicitud

    def _orden(self, solicitud):
        return ordenes.ultima_orden(self.estacion, 'software', solicitud.id)

    # --- Publicación retenida, aceptación del broker vs recepción de la estación ---------

    def test_aceptada_por_el_broker_no_es_recibida_y_llega_retenida_al_conectar(self):
        solicitud = self._solicitud()
        with override_settings(MQTT_CONFIG=self.panel):
            self.assertTrue(publicar_solicitud(solicitud).exitoso)  # estación desconectada
            orden = self._orden(solicitud)
            self.assertEqual(orden.estado, E.ENTREGADA)  # el broker la aceptó...
            estacion = EstacionSimulada().conectar()
            try:
                self.assertTrue(estacion.esperar(), 'la orden retenida no llegó al conectar')
            finally:
                estacion.desconectar()
        self.assertEqual(estacion.mensajes[0], {'payload': orden.payload, 'retenido': True})
        orden.refresh_from_db()
        self.assertEqual(orden.estado, E.ENTREGADA)  # ...pero recibida es solo con el acuse
        manejar_estado_instalacion(CODIGO, {'token': self.estacion.token_enrolamiento,
                                            'solicitud_id': solicitud.id, 'paso': 'recibido'})
        orden.refresh_from_db()
        self.assertEqual(orden.estado, E.RECIBIDA)

    def test_retirada_del_broker_la_estacion_ya_no_la_recibe(self):
        solicitud = self._solicitud()
        with override_settings(MQTT_CONFIG=self.panel):
            publicar_solicitud(solicitud)
            ordenes.cancelar_orden(self._orden(solicitud), usuario=self.usuario)
            estacion = EstacionSimulada().conectar()
            try:
                self.assertFalse(estacion.esperar(3), f'llegó algo después de retirar: {estacion.mensajes}')
            finally:
                estacion.desconectar()
        self.assertEqual(self._orden(solicitud).estado, E.CANCELADA)

    # --- Errores de autenticación y de ACL -------------------------------------------------

    def test_credencial_invalida(self):
        mala = _config('lab_panel', 'clave-equivocada')
        solicitud = self._solicitud()
        with override_settings(MQTT_CONFIG=mala):
            self.assertFalse(_publicar_mqtt(TOPICO, 'x', retain=True))  # MQTT 5: CONNACK rechazado
            resultado = publicar_solicitud(solicitud)
        self.assertFalse(resultado.exitoso, 'con credencial inválida la publicación no debe darse por buena')
        self.assertEqual(self._orden(solicitud).estado, E.CANCELADA)

    def test_publish_multiple_de_mqtt_3_no_se_entera_del_rechazo_de_la_acl(self):
        """El defecto D-1 que motivó el cambio de transporte, comprobado en el broker real:
        con una credencial sin permiso sobre el tópico, `paho.mqtt.publish.multiple` (MQTT
        3.1.1) termina sin excepción aunque EMQX descartó el mensaje."""
        import paho.mqtt.publish as mqtt_publish

        mqtt_publish.multiple(
            [{'topic': TOPICO, 'payload': 'no-autorizado', 'retain': True, 'qos': 1}], hostname=HOST, port=PUERTO,
            auth={'username': 'lab_worker', 'password': USUARIOS['lab_worker']}, client_id='lab-mqtt3',
        )  # no lanza
        estacion = EstacionSimulada().conectar()
        try:
            self.assertFalse(estacion.esperar(3), 'el broker no debió aceptar la publicación del worker')
        finally:
            estacion.desconectar()

    def test_acl_denegada_mqtt5_la_detecta_y_la_publicacion_no_se_da_por_buena(self):
        """Con MQTT 5 el PUBACK trae "Not authorized": la solicitud no avanza, la orden no
        queda en curso y nada se publica solo después."""
        sin_permiso = _config('lab_worker', USUARIOS['lab_worker'])
        solicitud = self._solicitud()
        with override_settings(MQTT_CONFIG=sin_permiso):
            self.assertFalse(_publicar_mqtt(TOPICO, 'x', retain=True))
            self.assertFalse(publicar_solicitud(solicitud).exitoso)
        orden = self._orden(solicitud)
        self.assertEqual(orden.estado, E.CANCELADA)
        self.assertNotIn(E.ENTREGADA, [h['estado'] for h in orden.historial])
        estacion = EstacionSimulada().conectar()
        try:
            self.assertFalse(estacion.esperar(3), 'el broker no debió aceptar la publicación del worker')
        finally:
            estacion.desconectar()

    def test_sin_conexion_al_broker(self):
        """Puerto cerrado: no se publicó nada y es seguro decirlo."""
        cerrado = _config('lab_panel', USUARIOS['lab_panel'])
        cerrado['PORT'] = PUERTO + 7
        solicitud = self._solicitud()
        with override_settings(MQTT_CONFIG=cerrado):
            self.assertFalse(publicar_solicitud(solicitud).exitoso)
        self.assertEqual(self._orden(solicitud).estado, E.CANCELADA)

    @skipUnless(CONTENEDOR, 'requiere EMQX_LAB_CONTENEDOR para congelar el broker')
    def test_corte_en_medio_de_la_publicacion_queda_incierta_y_se_recupera_con_el_mismo_payload(self):
        """El broker se congela (`docker pause`) después de aceptar la conexión y antes de
        responder el PUBLISH: no hay PUBACK. La orden queda `publicando` —ni entregada ni
        fallida— porque el mensaje puede haber quedado en el broker. La recuperación lo
        republica idéntico."""
        from unittest.mock import patch

        from apps.catalogo import services

        publicar_original = mqtt.Client.publish
        congelado = {'hecho': False}

        def congelar_y_publicar(cliente, *args, **kwargs):
            if not congelado['hecho']:
                congelado['hecho'] = True
                subprocess.run(['docker', 'pause', CONTENEDOR], check=True, timeout=30)
            return publicar_original(cliente, *args, **kwargs)

        solicitud = self._solicitud()
        try:
            with override_settings(MQTT_CONFIG=self.panel), \
                    patch.object(services, 'ESPERA_CONFIRMACION_MQTT_SEGUNDOS', 3), \
                    patch.object(mqtt.Client, 'publish', congelar_y_publicar):
                self.assertTrue(publicar_solicitud(solicitud).exitoso)
        finally:
            subprocess.run(['docker', 'unpause', CONTENEDOR], check=True, timeout=30)
        orden = self._orden(solicitud)
        self.assertEqual(orden.estado, E.PUBLICANDO)
        self.assertNotIn(E.ENTREGADA, [h['estado'] for h in orden.historial])
        time.sleep(2)  # el broker procesa lo que tenía en el buffer
        estacion = EstacionSimulada().conectar()
        quedo_en_el_broker = estacion.esperar(3)
        estacion.desconectar()
        print(f'\n  [corte] ¿el mensaje sin PUBACK quedó igual en el broker? {"sí" if quedo_en_el_broker else "no"}')

        ahora = timezone.now() + ordenes.ESPERA_CONFIRMACION + timedelta(minutes=1)
        with override_settings(MQTT_CONFIG=self.panel):
            resumen = ordenes.revisar_publicando(ahora)
        self.assertEqual((resumen['republicadas'], resumen['confirmadas']), (1, 1))
        orden.refresh_from_db()
        self.assertEqual(orden.estado, E.ENTREGADA)
        estacion = EstacionSimulada().conectar()
        try:
            self.assertTrue(estacion.esperar(), 'la republicación no llegó')
        finally:
            estacion.desconectar()
        self.assertEqual(estacion.mensajes[0]['payload'], orden.payload)  # el MISMO payload firmado
        self.assertEqual(OrdenAgente.objects.filter(estacion=self.estacion, origen_id=solicitud.id).count(), 1)

    # --- Reconexión ----------------------------------------------------------------------

    def test_reconexion_la_siguiente_orden_espera_y_llega_al_reconectar(self):
        s1, s2 = self._solicitud(), self._solicitud()
        with override_settings(MQTT_CONFIG=self.panel):
            estacion = EstacionSimulada().conectar()
            publicar_solicitud(s1)
            self.assertTrue(estacion.esperar(), 'en línea, la primera llega en vivo')
            estacion.desconectar()
            publicar_solicitud(s2)  # ranura ocupada por s1 sin acuse: espera
            self.assertEqual(self._orden(s2).estado, E.PENDIENTE)
            manejar_estado_instalacion(CODIGO, {'token': self.estacion.token_enrolamiento,
                                                'solicitud_id': s1.id, 'paso': 'recibido'})
            self.assertEqual(self._orden(s2).estado, E.ENTREGADA)  # despachada al liberar la ranura
            otra = EstacionSimulada().conectar()  # la estación vuelve
            try:
                self.assertTrue(otra.esperar(), 'la segunda no llegó al reconectar')
            finally:
                otra.desconectar()
        self.assertEqual(otra.mensajes[-1]['payload'], self._orden(s2).payload)

    # --- Persistencia tras reiniciar EMQX ------------------------------------------------

    @skipUnless(CONTENEDOR, 'requiere EMQX_LAB_CONTENEDOR para reiniciar el broker')
    def test_la_orden_retenida_sobrevive_al_reinicio_de_emqx(self):
        solicitud = self._solicitud()
        with override_settings(MQTT_CONFIG=self.panel):
            publicar_solicitud(solicitud)
        script = Path(settings.BASE_DIR) / 'deploy' / 'lab' / 'emqx-cola-lab.sh'
        subprocess.run(['sh', str(script), 'reiniciar'], check=True, timeout=120)
        estacion = None
        for _ in range(20):  # el listener puede tardar unos segundos más que `ctl status`
            try:
                estacion = EstacionSimulada().conectar()
                break
            except (OSError, AssertionError):
                time.sleep(1)
        self.assertIsNotNone(estacion, 'no se pudo reconectar al EMQX reiniciado')
        try:
            self.assertTrue(estacion.esperar(5), 'la orden retenida no sobrevivió al reinicio')
        finally:
            estacion.desconectar()
        self.assertEqual(estacion.mensajes[0]['payload'], self._orden(solicitud).payload)
