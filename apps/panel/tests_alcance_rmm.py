"""Primer corte de SaidSoft RMM 1.0: los módulos fuera de alcance no se exponen.

Ver docs/producto/SAIDSOFT_RMM_1_0_ALCANCE.md y apps/panel/modulos.py. Estas pruebas fijan
tres cosas:

1. Las rutas de los seis módulos diferidos están envueltas, y ninguna ruta del núcleo
   (que incluye Despliegues, la actualización del POS Farmamia/Elipsys).
2. Con la lista de producción (`PANEL_MODULOS_DIFERIDOS`), esas rutas devuelven 404 —un
   botón oculto no es una desactivación— y no aparecen en el menú ni en los enlaces
   cruzados.
3. El monitoreo y la gestión de software, con los despliegues del POS, conservan sus
   pantallas.

Con la lista vacía (desarrollo y CI) todo se comporta como antes: por eso las pruebas
existentes de esos módulos no cambian.
"""
import ast
from pathlib import Path

from django.conf import settings
from django.contrib.auth.models import Permission, User
from django.test import TestCase, override_settings
from django.urls import get_resolver, reverse

from apps.cuentas.models import PerfilUsuario
from apps.panel.modulos import MODULOS_DIFERIBLES

PRODUCCION = sorted(MODULOS_DIFERIBLES)

# Una ruta de lista por módulo: lo que alguien abriría escribiendo la URL a mano.
LISTAS_DIFERIDAS = {
    'viaticos': 'viaticos_bandeja',
    'aperturas': 'aperturas_lista',
    'cumplimiento': 'cumplimiento_lista',
    'netflow': 'netflow_farmacias',
    'mesa': 'novedades_lista',
    'scripts': 'scripts_lista',
}
# El núcleo de RMM 1.0: monitoreo y gestión de software, incluida la actualización del POS.
LISTAS_NUCLEO = [
    'dashboard', 'estaciones_lista', 'centro_monitoreo', 'alertas_lista',
    'enlaces_farmacias_lista', 'monitoreo_lista', 'aplicaciones_lista',
    'solicitudes_instalacion_lista', 'reportes_index', 'despliegues_lista',
]
# Todas las rutas de Despliegues: ninguna puede quedar envuelta.
RUTAS_DESPLIEGUES = [
    'despliegues_lista', 'despliegue_crear', 'despliegue_detalle', 'despliegue_progreso_partial',
    'despliegue_aprobar', 'despliegue_publicar', 'despliegue_pausar', 'despliegue_reanudar',
    'despliegue_promover', 'reporte_despliegue_csv',
]


def _rutas_del_panel():
    """(nombre, callback) de cada ruta con nombre del panel."""
    resolver = get_resolver()
    rutas = []
    for patron in resolver.url_patterns:
        if getattr(patron, 'namespace', None) == 'panel':
            for p in patron.url_patterns:
                if getattr(p, 'name', None):
                    rutas.append((p.name, p.callback))
    return rutas


def _lista_por_defecto_de_produccion():
    """El `default=` de PANEL_MODULOS_DIFERIDOS en config/settings/produccion.py.

    Se lee del archivo y no importando el módulo: importarlo exige las variables de entorno
    de producción. Es lo que de verdad rige al desplegar si el .env no la define."""
    fuente = (Path(settings.BASE_DIR) / 'config' / 'settings' / 'produccion.py').read_text(encoding='utf-8')
    for nodo in ast.walk(ast.parse(fuente)):
        if (isinstance(nodo, ast.Assign) and any(getattr(t, 'id', None) == 'PANEL_MODULOS_DIFERIDOS' for t in nodo.targets)):
            for palabra in nodo.value.keywords:
                if palabra.arg == 'default':
                    return ast.literal_eval(palabra.value)
    raise AssertionError('PANEL_MODULOS_DIFERIDOS no tiene default en produccion.py')


class ListaDeProduccionTests(TestCase):
    def test_despliegues_no_esta_entre_los_diferidos_por_defecto(self):
        self.assertNotIn('despliegues', _lista_por_defecto_de_produccion())
        self.assertNotIn('despliegues', MODULOS_DIFERIBLES)

    def test_produccion_difiere_exactamente_los_seis_modulos(self):
        self.assertEqual(sorted(_lista_por_defecto_de_produccion()), PRODUCCION)
        self.assertEqual(
            set(PRODUCCION), {'viaticos', 'aperturas', 'cumplimiento', 'netflow', 'mesa', 'scripts'},
        )


class RutasEnvueltasTests(TestCase):
    def test_los_seis_modulos_tienen_rutas_envueltas(self):
        envueltos = {getattr(cb, 'modulo_diferible', None) for _, cb in _rutas_del_panel()} - {None}
        self.assertEqual(envueltos, set(MODULOS_DIFERIBLES))

    def test_ninguna_ruta_del_nucleo_esta_envuelta(self):
        """Incluye las del agente: actualizar la flota y el instalador de un solo uso, que
        lo descargan las propias estaciones; y todas las de Despliegues."""
        por_nombre = dict(_rutas_del_panel())
        nucleo = LISTAS_NUCLEO + RUTAS_DESPLIEGUES + [
            'estaciones_agente', 'estaciones_agente_actualizar', 'instalador_enlace_emitir', 'instalador_descargar',
        ]
        for nombre in nucleo:
            with self.subTest(ruta=nombre):
                self.assertIsNone(getattr(por_nombre[nombre], 'modulo_diferible', None))

    def test_cada_ruta_de_los_modulos_diferidos_esta_envuelta(self):
        """Si alguien agrega una pantalla nueva a un módulo diferido y olvida envolverla,
        quedaría expuesta. Se detecta por el prefijo de la URL."""
        prefijos = {
            'viaticos/': 'viaticos', 'aperturas/': 'aperturas', 'cumplimiento/': 'cumplimiento',
            'monitoreo/netflow/': 'netflow', 'mesa/': 'mesa',
            'scripts/': 'scripts', 'ejecuciones/': 'scripts',
        }
        resolver = get_resolver()
        for patron in resolver.url_patterns:
            if getattr(patron, 'namespace', None) != 'panel':
                continue
            for p in patron.url_patterns:
                ruta = str(p.pattern)
                for prefijo, modulo in prefijos.items():
                    if ruta.startswith(prefijo):
                        with self.subTest(ruta=ruta):
                            self.assertEqual(getattr(p.callback, 'modulo_diferible', None), modulo)


class AccesoConListaDeProduccionTests(TestCase):
    def setUp(self):
        self.usuario = User.objects.create_superuser(username='alcance_rmm', password='x' * 16)
        self.client.force_login(self.usuario)

    @override_settings(PANEL_MODULOS_DIFERIDOS=PRODUCCION)
    def test_las_pantallas_diferidas_devuelven_404_aun_para_un_administrador(self):
        for modulo, nombre in LISTAS_DIFERIDAS.items():
            with self.subTest(modulo=modulo):
                self.assertEqual(self.client.get(reverse(f'panel:{nombre}')).status_code, 404)

    @override_settings(PANEL_MODULOS_DIFERIDOS=PRODUCCION)
    def test_las_acciones_diferidas_tambien_devuelven_404(self):
        """No solo las listas: una acción por POST tampoco llega a la vista."""
        acciones = [
            reverse('panel:script_ejecutar', args=[1]),
            reverse('panel:apertura_emitir_tokens', args=[1]),
            reverse('panel:novedad_accion', args=[1]),
            reverse('panel:viatico_revisar', args=[1, 'aprobar']),
        ]
        for url in acciones:
            with self.subTest(url=url):
                self.assertEqual(self.client.post(url).status_code, 404)

    @override_settings(PANEL_MODULOS_DIFERIDOS=PRODUCCION)
    def test_el_nucleo_sigue_respondiendo(self):
        for nombre in LISTAS_NUCLEO:
            with self.subTest(ruta=nombre):
                self.assertEqual(self.client.get(reverse(f'panel:{nombre}')).status_code, 200)

    @override_settings(PANEL_MODULOS_DIFERIDOS=PRODUCCION)
    def test_el_menu_no_muestra_los_modulos_diferidos_y_si_el_nucleo(self):
        html = self.client.get(reverse('panel:dashboard')).content.decode()
        for modulo, nombre in LISTAS_DIFERIDAS.items():
            if modulo == 'netflow':
                continue  # no está en el menú; su enlace vive en la pantalla de enlaces
            with self.subTest(modulo=modulo):
                self.assertNotIn(f'href="{reverse(f"panel:{nombre}")}"', html)
        for nombre in ('viaticos_mis_reportes', 'viaticos_zonas_lista', 'ejecuciones_lista', 'scripts_programados_lista'):
            self.assertNotIn(f'href="{reverse(f"panel:{nombre}")}"', html)
        for nombre in ('estaciones_lista', 'centro_monitoreo', 'alertas_lista', 'enlaces_farmacias_lista',
                       'aplicaciones_lista', 'solicitudes_instalacion_lista', 'despliegues_lista'):
            with self.subTest(nucleo=nombre):
                self.assertIn(f'href="{reverse(f"panel:{nombre}")}"', html)

    @override_settings(PANEL_MODULOS_DIFERIDOS=PRODUCCION)
    def test_los_enlaces_cruzados_de_los_diferidos_desaparecen_y_los_de_despliegues_no(self):
        enlaces = self.client.get(reverse('panel:enlaces_farmacias_lista')).content.decode()
        self.assertNotIn(reverse('panel:netflow_farmacias'), enlaces)
        reportes = self.client.get(reverse('panel:reportes_index')).content.decode()
        self.assertIn('Resultado de un despliegue', reportes)
        tablero = self.client.get(reverse('panel:dashboard_cumplimiento_partial')).content.decode()
        self.assertIn('Despliegues en curso', tablero)

    @override_settings(PANEL_MODULOS_DIFERIDOS=PRODUCCION + ['despliegues'])
    def test_un_env_viejo_que_todavia_nombre_despliegues_no_lo_oculta(self):
        """Si el .env del servidor quedó con la lista anterior, Despliegues sigue visible:
        el núcleo no se puede diferir."""
        self.assertEqual(self.client.get(reverse('panel:despliegues_lista')).status_code, 200)
        html = self.client.get(reverse('panel:dashboard')).content.decode()
        self.assertIn(f'href="{reverse("panel:despliegues_lista")}"', html)


@override_settings(PANEL_MODULOS_DIFERIDOS=PRODUCCION)
class DesplieguesParaPerfilesAutorizadosTests(TestCase):
    """Con la lista de producción, el acceso a Despliegues lo decide el permiso de siempre,
    no el corte: quien tiene `view_despliegue` entra y quien no, recibe 403 (no 404)."""

    def _usuario(self, nombre, permisos):
        usuario = User.objects.create_user(username=nombre, password='x' * 16)
        PerfilUsuario.objects.create(usuario=usuario, acceso_todas_unidades=True)
        for codigo in permisos:
            app, codename = codigo.split('.')
            usuario.user_permissions.add(Permission.objects.get(content_type__app_label=app, codename=codename))
        return usuario

    def test_un_operador_con_permiso_ve_la_lista_y_el_menu(self):
        self.client.force_login(self._usuario('operador_pos', ['despliegues.view_despliegue']))
        self.assertEqual(self.client.get(reverse('panel:despliegues_lista')).status_code, 200)
        html = self.client.get(reverse('panel:dashboard')).content.decode()
        self.assertIn(f'href="{reverse("panel:despliegues_lista")}"', html)

    def test_sin_permiso_sigue_siendo_403(self):
        self.client.force_login(self._usuario('sin_permiso', []))
        self.assertEqual(self.client.get(reverse('panel:despliegues_lista')).status_code, 403)


class SinListaTodoSigueComoAntesTests(TestCase):
    """Desarrollo y CI: la lista vacía no cambia nada."""

    def setUp(self):
        self.client.force_login(User.objects.create_superuser(username='alcance_dev', password='x' * 16))

    @override_settings(PANEL_MODULOS_DIFERIDOS=[])
    def test_las_pantallas_responden_y_el_menu_las_muestra(self):
        html = self.client.get(reverse('panel:dashboard')).content.decode()
        for modulo, nombre in LISTAS_DIFERIDAS.items():
            with self.subTest(modulo=modulo):
                self.assertNotEqual(self.client.get(reverse(f'panel:{nombre}')).status_code, 404)
                if modulo != 'netflow':
                    self.assertIn(f'href="{reverse(f"panel:{nombre}")}"', html)
