"""Firma Ed25519 de los catálogos (agente 0.38): el servidor firma y el agente REAL verifica.

Punta a punta sin broker: `envolver_firmado` arma el sobre como en producción y se lo pasa
a `_catalogo_verificado` del agente cargado desde agente-prueba/agente_prueba.py.
"""
import base64
import json
from io import StringIO
from types import SimpleNamespace
from unittest.mock import patch

from django.conf import settings
from django.core.management import call_command
from django.test import SimpleTestCase, override_settings

from apps.catalogo.tests_agente_instalador import _AgenteReal
from apps.monitoreo import firma_catalogo
from apps.monitoreo.servicios_pos import envolver_firmado

PRIVADA, PUBLICA = firma_catalogo.generar()
OTRA_PRIVADA, OTRA_PUBLICA = firma_catalogo.generar()
COMANDO = 'catalogo_servicios_pos'
CONTENIDO = {'servicios': [{'clave': 'odoo', 'url': 'https://odoo.local'}]}


class ServidorTests(SimpleTestCase):
    @override_settings(CATALOGO_FIRMA_PRIVADA='')
    def test_sin_clave_el_sobre_queda_como_antes(self):
        sobre = envolver_firmado(COMANDO, CONTENIDO)
        self.assertNotIn('firma_ed25519', sobre)
        self.assertIn('firma', sobre)

    @override_settings(CATALOGO_FIRMA_PRIVADA=PRIVADA)
    def test_con_clave_firma_el_mismo_mensaje_que_la_hmac(self):
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

        sobre = envolver_firmado(COMANDO, CONTENIDO)
        mensaje = f"{COMANDO}|{sobre['catalogo']}|{sobre['timestamp']}".encode()
        Ed25519PublicKey.from_public_bytes(base64.b64decode(PUBLICA)).verify(
            base64.b64decode(sobre['firma_ed25519']), mensaje,
        )  # lanza si no verifica
        self.assertIn('firma', sobre)  # la HMAC sigue, para los agentes anteriores

    @override_settings(CATALOGO_FIRMA_PRIVADA='esto-no-es-una-clave')
    def test_una_clave_mal_cargada_no_rompe_la_publicacion(self):
        with self.assertLogs('apps.monitoreo.firma_catalogo', 'ERROR'):
            sobre = envolver_firmado(COMANDO, CONTENIDO)
        self.assertNotIn('firma_ed25519', sobre)
        self.assertIn('firma', sobre)

    @override_settings(CATALOGO_FIRMA_PRIVADA=PRIVADA)
    def test_el_comando_muestra_la_publica_en_uso(self):
        salida = StringIO()
        call_command('clave_firma_catalogos', stdout=salida)
        self.assertIn(PUBLICA, salida.getvalue())
        self.assertNotIn(PRIVADA, salida.getvalue())

    @override_settings(CATALOGO_FIRMA_PRIVADA=PRIVADA)
    def test_generar_no_cambia_la_clave_en_uso(self):
        salida = StringIO()
        call_command('clave_firma_catalogos', '--generar', stdout=salida)
        self.assertIn('CATALOGO_FIRMA_PRIVADA=', salida.getvalue())
        self.assertEqual(firma_catalogo.clave_publica(), PUBLICA)


class AgenteTests(_AgenteReal):
    def _agente(self, identidad=None, hmac_compartido=None):
        """`hmac_compartido=''` es una estación instalada desde 0.21 sin el secreto
        compartido (H-03)."""
        agente = self.ag.AgentePrueba.__new__(self.ag.AgentePrueba)
        agente.identidad = dict(identidad or {})
        agente.args = SimpleNamespace(
            codigo='LAB-CAT',
            hmac_secret=settings.COMANDO_HMAC_SECRET if hmac_compartido is None else hmac_compartido,
        )
        agente.guardados = 0

        def guardar():
            agente.guardados += 1

        agente._guardar_identidad = guardar
        return agente

    def _sobre(self, privada=PRIVADA):
        with override_settings(CATALOGO_FIRMA_PRIVADA=privada):
            return envolver_firmado(COMANDO, CONTENIDO)

    def _verificar(self, agente, sobre, claves=(PUBLICA,)):
        with patch.object(self.ag, 'CLAVES_PUBLICAS_CATALOGO', claves):
            return agente._catalogo_verificado('catálogo de prueba', COMANDO, sobre)

    def test_la_estacion_sin_secreto_compartido_acepta_el_catalogo_firmado(self):
        """H-03: hoy esa estación lo rechaza; con 0.38 lo acepta por la firma Ed25519."""
        agente = self._agente(hmac_compartido='')
        self.assertIsNone(self._verificar(agente, self._sobre(privada='')))  # solo HMAC: como hoy
        self.assertEqual(self._verificar(agente, self._sobre()), CONTENIDO)

    def test_el_primer_catalogo_firmado_activa_la_exigencia(self):
        agente = self._agente()
        self.assertEqual(self._verificar(agente, self._sobre()), CONTENIDO)
        self.assertTrue(agente.identidad['catalogo_exige_ed25519'])
        self.assertEqual(agente.guardados, 1)
        self._verificar(agente, self._sobre())
        self.assertEqual(agente.guardados, 1)  # no reescribe identidad.json en cada catálogo

    def test_exigida_ya_no_acepta_solo_hmac_ni_sin_firma(self):
        agente = self._agente(identidad={'catalogo_exige_ed25519': True})
        solo_hmac = self._sobre(privada='')
        sin_firma = {k: v for k, v in solo_hmac.items() if k != 'firma'}
        self.assertIsNone(self._verificar(agente, solo_hmac))
        self.assertIsNone(self._verificar(agente, sin_firma))

    def test_rechaza_otra_clave_un_catalogo_alterado_y_una_firma_ilegible(self):
        agente = self._agente()
        alterado = self._sobre()
        alterado['catalogo'] = json.dumps({'servicios': [{'clave': 'odoo', 'url': 'https://atacante'}]})
        ilegible = {**self._sobre(), 'firma_ed25519': '%%%'}
        for nombre, sobre in (('otra clave', self._sobre(privada=OTRA_PRIVADA)), ('alterado', alterado),
                              ('ilegible', ilegible)):
            with self.subTest(nombre):
                self.assertIsNone(self._verificar(agente, sobre))
        self.assertNotIn('catalogo_exige_ed25519', agente.identidad)

    def test_otro_comando_no_sirve_con_la_misma_firma(self):
        """El comando entra a la firma: un catálogo de servicios no vale como de eventos."""
        agente = self._agente()
        with patch.object(self.ag, 'CLAVES_PUBLICAS_CATALOGO', (PUBLICA,)):
            self.assertIsNone(agente._catalogo_verificado('x', 'catalogo_eventos_sistema', self._sobre()))

    def test_acepta_cualquier_clave_de_la_lista_para_rotar(self):
        agente = self._agente()
        self.assertEqual(self._verificar(agente, self._sobre(privada=OTRA_PRIVADA), claves=(PUBLICA, OTRA_PUBLICA)),
                         CONTENIDO)

    def test_sin_claves_en_el_agente_sigue_con_la_hmac_como_037(self):
        agente = self._agente()
        self.assertEqual(self._verificar(agente, self._sobre(), claves=()), CONTENIDO)
        self.assertNotIn('catalogo_exige_ed25519', agente.identidad)
