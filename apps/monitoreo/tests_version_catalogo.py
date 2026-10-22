"""Versión del catálogo aplicado, informada en el latido (agente 0.38).

El servidor y el agente calculan la misma versión (SHA-256 del texto `catalogo` firmado, 12
caracteres). El panel y `estado_catalogos` la comparan con la vigente para mostrar qué
estación quedó con un catálogo viejo, como pasó en H-03 sin que nada lo avisara.
"""
import json
from io import StringIO
from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth.models import User
from django.core.management import call_command
from django.test import TestCase
from django.urls import reverse

from apps.catalogo.models import Estacion, Farmacia, Grupo, UnidadNegocio
from apps.catalogo.tests_agente_instalador import _AgenteReal
from apps.mqtt_worker.services import manejar_heartbeat
from apps.monitoreo.servicios_pos import (
    envolver_firmado, estado_catalogo, payload_catalogo, publicar_catalogo_servicios_pos, serializar, version_de,
    versiones_vigentes,
)


class MismaVersionEnServidorYAgenteTests(_AgenteReal):
    def test_el_agente_calcula_la_misma_version_que_el_servidor(self):
        sobre = envolver_firmado('catalogo_servicios_pos', {'servicios': [{'clave': 'ñandú'}]})
        self.assertEqual(self.ag.version_catalogo(sobre), version_de(sobre['catalogo']))
        self.assertRegex(self.ag.version_catalogo(sobre), r'^[0-9a-f]{12}$')

    def test_sin_catalogo_firmado_no_hay_version(self):
        self.assertEqual(self.ag.version_catalogo({'servicios': []}), '')

    def _agente(self):
        agente = self.ag.AgentePrueba.__new__(self.ag.AgentePrueba)
        agente.identidad = {}
        agente.args = SimpleNamespace(codigo='LAB-VER')
        agente._guardar_identidad = lambda: None
        agente._catalogo_verificado = lambda tipo, comando, payload: json.loads(payload['catalogo'])
        return agente

    def test_al_aplicar_cada_catalogo_guarda_su_version_y_la_manda_en_el_latido(self):
        agente = self._agente()
        servicios = envolver_firmado('catalogo_servicios_pos', {'servicios': [], 'criticidad': {}, 'desactivados': []})
        eventos = envolver_firmado('catalogo_eventos_sistema', {'eventos': [{'log': 'System', 'id': 41}]})
        agente._aplicar_catalogo_servicios(servicios)
        agente._aplicar_catalogo_eventos(eventos)

        publicados = []
        agente._token = lambda: 'tok'
        agente._pausado = lambda: False
        agente._version_pos_reportable = lambda: {}
        agente._leer_config_pos = lambda: {}
        agente._red_lan = lambda: {}
        agente._publicar = lambda topico, cuerpo, qos=0: publicados.append(cuerpo)
        agente._reportar_heartbeat()
        self.assertEqual(publicados[0]['catalogos'], {
            'servicios_pos': version_de(servicios['catalogo']),
            'eventos_sistema': version_de(eventos['catalogo']),
        })


class ServidorTests(TestCase):
    def setUp(self):
        sg = UnidadNegocio.objects.get(codigo='SG')
        farmacia = Farmacia.objects.create(codigo='MLV01', grupo=Grupo.objects.create(codigo='TRXV1'), unidad_negocio=sg)
        self.estacion = Estacion.objects.create(
            codigo='MLV01-A', farmacia=farmacia, estado_aprobacion=Estacion.EstadoAprobacion.APROBADA,
        )
        self.otra = Estacion.objects.create(
            codigo='MLV01-B', farmacia=farmacia, estado_aprobacion=Estacion.EstadoAprobacion.APROBADA,
        )

    def _latido(self, **extra):
        manejar_heartbeat(self.estacion.codigo, {'token': self.estacion.token_enrolamiento, **extra})
        self.estacion.refresh_from_db()

    def test_lo_vigente_es_la_version_de_lo_que_se_publica(self):
        with patch('apps.catalogo.services._publicar_mqtt', return_value=True) as publicar:
            publicar_catalogo_servicios_pos()
        sobre = json.loads(publicar.call_args.args[1])
        self.assertEqual(versiones_vigentes()['servicios'], version_de(sobre['catalogo']))
        self.assertEqual(versiones_vigentes()['servicios'], version_de(serializar(payload_catalogo())))

    def test_el_latido_guarda_las_versiones_validas(self):
        self._latido(catalogos={'servicios_pos': 'a' * 12, 'eventos_sistema': 'b' * 12})
        self.assertEqual((self.estacion.catalogo_servicios_version, self.estacion.catalogo_eventos_version),
                         ('a' * 12, 'b' * 12))

    def test_un_agente_viejo_o_un_valor_raro_no_borra_ni_ensucia(self):
        self._latido(catalogos={'servicios_pos': 'a' * 12, 'eventos_sistema': 'b' * 12})
        self._latido()  # agente anterior a 0.38: no manda `catalogos`
        self._latido(catalogos={'servicios_pos': '<script>', 'eventos_sistema': 7})
        self._latido(catalogos='no-es-un-dict')
        self.assertEqual((self.estacion.catalogo_servicios_version, self.estacion.catalogo_eventos_version),
                         ('a' * 12, 'b' * 12))

    def test_estado_por_catalogo(self):
        vigentes = versiones_vigentes()
        self.estacion.catalogo_servicios_version = vigentes['servicios']
        self.estacion.catalogo_eventos_version = 'c' * 12
        self.assertEqual([al_dia for _, _, al_dia in estado_catalogo(self.estacion)], [True, False])
        self.assertEqual([al_dia for _, _, al_dia in estado_catalogo(self.otra)], [None, None])

    def test_el_comando_lista_las_desactualizadas(self):
        Estacion.objects.filter(pk=self.estacion.pk).update(
            catalogo_servicios_version='d' * 12, catalogo_eventos_version=versiones_vigentes()['eventos'],
        )
        salida = StringIO()
        call_command('estado_catalogos', stdout=salida)
        texto = salida.getvalue()
        self.assertIn(f"MLV01-A (tiene {'d' * 12})", texto)
        self.assertIn('sin dato (agente anterior a 0.38): 1', texto)  # MLV01-B

    def test_la_ficha_muestra_el_estado(self):
        usuario = User.objects.create_user(username='ver-catalogo', password='x', is_superuser=True, is_staff=True)
        Estacion.objects.filter(pk=self.estacion.pk).update(catalogo_servicios_version='e' * 12)
        self.client.force_login(usuario)
        resp = self.client.get(reverse('panel:estacion_info_modal', args=[self.estacion.pk]))
        self.assertContains(resp, 'desactualizado')
        self.assertContains(resp, 'sin dato')
