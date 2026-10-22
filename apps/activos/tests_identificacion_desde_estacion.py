"""Alta de activo: tipo, marca y modelo salen de la estación (agente 0.38).

El agente informa en `consultar_info` el fabricante, el modelo y el formato del equipo
(PCSystemType y ChassisTypes). La búsqueda por serie los usa para completar la sección
"Identificación", sin pisar lo que el usuario ya eligió.
"""
from django.contrib.auth.models import Permission, User
from django.test import TestCase
from django.urls import reverse

from apps.activos.models import Activo, Marca
from apps.activos.services import marca_de_fabricante, tipo_de_equipo
from apps.catalogo.models import Estacion, Farmacia, Grupo, UnidadNegocio
from apps.cuentas.models import PerfilUsuario
from apps.mqtt_worker.services import manejar_info_equipo

SERIE = 'T4ARYZ0041549DJ'


class TraduccionTests(TestCase):
    def setUp(self):
        for nombre in ('DELL', 'HP', 'LENOVO', 'ASUS'):
            Marca.objects.get_or_create(nombre=nombre)

    def test_fabricante_a_marca(self):
        casos = {
            'Dell Inc.': 'DELL', 'HP': 'HP', 'Hewlett-Packard': 'HP', 'LENOVO': 'LENOVO',
            'ASUSTeK COMPUTER INC.': 'ASUS',
        }
        for fabricante, marca in casos.items():
            with self.subTest(fabricante):
                self.assertEqual(marca_de_fabricante(fabricante).nombre, marca)

    def test_fabricante_desconocido_o_de_relleno_no_se_adivina(self):
        for fabricante in ('', 'To Be Filled By O.E.M.', 'System manufacturer', 'Acme Computers'):
            with self.subTest(fabricante):
                self.assertIsNone(marca_de_fabricante(fabricante))

    def test_formato_a_tipo(self):
        casos = [
            ((1, '3'), Activo.Tipo.DESKTOP), ((2, '10'), Activo.Tipo.LAPTOP), ((None, '9'), Activo.Tipo.LAPTOP),
            ((None, '35'), Activo.Tipo.DESKTOP), ((4, '23'), Activo.Tipo.SERVIDOR), ((None, '30'), Activo.Tipo.TABLET),
            ((2, ''), Activo.Tipo.LAPTOP), ((1, '2'), Activo.Tipo.DESKTOP),  # chasis "desconocido": PCSystemType
            ((None, '1,2'), None), ((None, ''), None),
        ]
        for (sistema, chasis), tipo in casos:
            with self.subTest(sistema=sistema, chasis=chasis):
                self.assertEqual(tipo_de_equipo(sistema, chasis), tipo)


class BusquedaPorSerieTests(TestCase):
    def setUp(self):
        Marca.objects.get_or_create(nombre='DELL')
        sg = UnidadNegocio.objects.get(codigo='SG')
        farmacia = Farmacia.objects.create(codigo='GSD15', grupo=Grupo.objects.create(codigo='TRXI1'), unidad_negocio=sg)
        self.estacion = Estacion.objects.create(
            codigo='GSD15-ADM', farmacia=farmacia, numero_serie=SERIE,
            estado_aprobacion=Estacion.EstadoAprobacion.APROBADA,
        )
        usuario = User.objects.create_user(username='u_ident', password='x')
        PerfilUsuario.objects.create(usuario=usuario, acceso_todas_unidades=True)
        usuario.user_permissions.add(*Permission.objects.filter(codename__in=['add_activo', 'consultar_info_estacion']))
        self.client.force_login(usuario)
        self.url = reverse('panel:especificaciones_por_serie_partial')

    def _info(self, **payload):
        manejar_info_equipo(self.estacion.codigo, {'token': self.estacion.token_enrolamiento, **payload})
        self.estacion.refresh_from_db()

    def test_el_worker_guarda_lo_que_manda_el_agente_038(self):
        self._info(procesador='Intel i5', ram_total_mb=16231, fabricante='Dell Inc.', modelo='OptiPlex 3080',
                   tipo_sistema=1, chasis='3')
        self.assertEqual(
            (self.estacion.fabricante_equipo, self.estacion.modelo_equipo, self.estacion.tipo_sistema,
             self.estacion.chasis), ('Dell Inc.', 'OptiPlex 3080', 1, '3'),
        )
        self._info(procesador='Intel i5')  # un agente anterior no los manda: quedan
        self.assertEqual(self.estacion.modelo_equipo, 'OptiPlex 3080')

    def test_completa_tipo_marca_y_modelo_fuera_de_banda(self):
        self._info(procesador='Intel i5', ram_total_mb=16231, almacenamiento_total_gb=476, fabricante='Dell Inc.',
                   modelo='OptiPlex 3080', tipo_sistema=1, chasis='3')
        resp = self.client.get(self.url, {'numero_serie': SERIE})
        contenido = resp.content.decode()
        for campo in ('tipo', 'marca', 'modelo'):
            self.assertIn(f'id="campo-{campo}" hx-swap-oob="true"', contenido)
        self.assertIn('value="OptiPlex 3080"', contenido)
        self.assertIn(f'<option value="{Marca.objects.get(nombre="DELL").pk}" selected>', contenido)
        self.assertIn('<option value="DSK" selected>', contenido)

    def test_no_pisa_lo_que_el_usuario_ya_eligio(self):
        self._info(procesador='Intel i5', fabricante='Dell Inc.', modelo='OptiPlex 3080', tipo_sistema=1, chasis='3')
        resp = self.client.get(self.url, {'numero_serie': SERIE, 'tipo': 'SRV', 'modelo': 'escrito a mano'})
        contenido = resp.content.decode()
        self.assertNotIn('id="campo-tipo" hx-swap-oob', contenido)
        self.assertNotIn('id="campo-modelo" hx-swap-oob', contenido)
        self.assertIn('id="campo-marca" hx-swap-oob="true"', contenido)

    def test_un_fabricante_fuera_de_la_lista_se_menciona(self):
        self._info(procesador='Intel i5', fabricante='Acme Computers')
        resp = self.client.get(self.url, {'numero_serie': SERIE})
        self.assertContains(resp, 'Acme Computers')
        self.assertNotContains(resp, 'id="campo-marca" hx-swap-oob')

    def test_la_pantalla_de_alta_tiene_los_destinos(self):
        pagina = self.client.get(reverse('panel:activo_crear')).content.decode()
        for campo in ('tipo', 'marca', 'modelo', 'categoria', 'numero_serie'):
            self.assertIn(f'id="campo-{campo}"', pagina)
        self.assertIn("[name='tipo'],[name='marca'],[name='modelo']", pagina)
