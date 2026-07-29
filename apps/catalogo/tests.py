from django.test import TestCase, override_settings

from apps.catalogo.models import Estacion, Farmacia, Grupo
from apps.catalogo.services import (
    generar_comando_instalacion_meshcentral, url_escritorio_remoto_meshcentral,
    url_terminal_remoto_meshcentral,
)

MESHCENTRAL_CONFIG_TEST = {
    'SERVER_URL': 'https://mesh.test.local',
    'MESH_ID': 'abc123meshid',
    'AGENT_ARCH_ID': 4,
    'INSTALL_FLAGS': 2,
    'VIEWMODE_ESCRITORIO': '11',
    'VIEWMODE_TERMINAL': '12',
}


@override_settings(MESHCENTRAL_CONFIG=MESHCENTRAL_CONFIG_TEST)
class MeshCentralServiciosTests(TestCase):
    def setUp(self):
        grupo = Grupo.objects.create(codigo='TRX001')
        farmacia = Farmacia.objects.create(codigo='ML001', grupo=grupo)
        self.estacion = Estacion.objects.create(codigo='ML001-A', farmacia=farmacia)

    def test_generar_comando_instalacion_contiene_mesh_id_y_url(self):
        comando = generar_comando_instalacion_meshcentral(self.estacion)
        self.assertIn('mesh.test.local', comando)
        self.assertIn('meshid=abc123meshid', comando)
        self.assertIn('installflags=2', comando)

    def test_urls_remotas_none_sin_node_id(self):
        self.assertIsNone(url_escritorio_remoto_meshcentral(self.estacion))
        self.assertIsNone(url_terminal_remoto_meshcentral(self.estacion))

    def test_urls_remotas_con_node_id(self):
        self.estacion.meshcentral_node_id = 'nodeid123'
        self.estacion.save(update_fields=['meshcentral_node_id'])

        url_escritorio = url_escritorio_remoto_meshcentral(self.estacion)
        url_terminal = url_terminal_remoto_meshcentral(self.estacion)

        self.assertIn('node=nodeid123', url_escritorio)
        self.assertIn('viewmode=11', url_escritorio)
        self.assertIn('node=nodeid123', url_terminal)
        self.assertIn('viewmode=12', url_terminal)
