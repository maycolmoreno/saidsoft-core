"""Alta de activo: si la estación encontrada por serie nunca mandó su hardware, se le pide
en el momento (`consultar_info`) y la pantalla vuelve a buscar sola hasta que llega.

Caso real del 22-oct-2026: GSD15-ADM se encontraba por serie, pero procesador, RAM y
almacenamiento quedaban vacíos porque nadie le había pedido la info desde su ficha.
"""
from unittest.mock import patch

from django.contrib.auth.models import Permission, User
from django.test import TestCase
from django.urls import reverse

from apps.auditoria.models import EventoAuditoria
from apps.catalogo.models import Estacion, Farmacia, Grupo, UnidadNegocio
from apps.cuentas.models import PerfilUsuario

SERIE = 'T4ARYZ0041549DJ'


class PedirHardwareTests(TestCase):
    def setUp(self):
        sg = UnidadNegocio.objects.get(codigo='SG')
        farmacia = Farmacia.objects.create(codigo='GSD15', grupo=Grupo.objects.create(codigo='TRXH1'), unidad_negocio=sg)
        self.estacion = Estacion.objects.create(
            codigo='GSD15-ADM', farmacia=farmacia, numero_serie=SERIE,
            estado_aprobacion=Estacion.EstadoAprobacion.APROBADA, estado_conexion=Estacion.EstadoConexion.ONLINE,
        )
        self.usuario = User.objects.create_user(username='u_pide_hw', password='x')
        PerfilUsuario.objects.create(usuario=self.usuario, acceso_todas_unidades=True)
        self._permisos('add_activo', 'consultar_info_estacion')
        self.client.force_login(self.usuario)
        self.url = reverse('panel:especificaciones_por_serie_partial')

    def _permisos(self, *codenames):
        self.usuario.user_permissions.add(*Permission.objects.filter(codename__in=codenames))

    def _buscar(self, **extra):
        with patch('apps.catalogo.services.enviar_comando', return_value=True) as enviar:
            resp = self.client.get(self.url, {'numero_serie': SERIE, **extra})
        return resp, enviar

    def test_sin_hardware_se_lo_pide_audita_y_queda_esperando(self):
        resp, enviar = self._buscar()
        enviar.assert_called_once_with(self.estacion, 'consultar_info')
        self.assertContains(resp, 'Se lo pedimos')
        self.assertContains(resp, 'intento=1')  # vuelve a buscar sola
        self.assertTrue(EventoAuditoria.objects.filter(accion='estacion.consultar_info').exists())

    def test_al_volver_a_mirar_no_se_vuelve_a_pedir_y_completa_cuando_llega(self):
        resp, enviar = self._buscar(intento=1)
        enviar.assert_not_called()
        self.assertContains(resp, 'intento=2')

        Estacion.objects.filter(pk=self.estacion.pk).update(
            procesador='Intel i5-12400', ram_total_mb=16231, almacenamiento_total_gb=476,
        )
        resp, enviar = self._buscar(intento=2)
        enviar.assert_not_called()
        self.assertContains(resp, 'Intel i5-12400')
        self.assertContains(resp, 'value="16"')
        self.assertContains(resp, 'Datos tomados de la estación')
        self.assertNotContains(resp, 'intento=3')

    def test_se_deja_de_esperar_despues_de_los_intentos(self):
        resp, _ = self._buscar(intento=10)
        self.assertContains(resp, 'no respondió con su hardware')
        self.assertNotContains(resp, 'hx-trigger="load delay:3s"')

    def test_apagada_no_se_pide_y_lo_dice(self):
        Estacion.objects.filter(pk=self.estacion.pk).update(estado_conexion=Estacion.EstadoConexion.OFFLINE)
        resp, enviar = self._buscar()
        enviar.assert_not_called()
        self.assertContains(resp, 'ahora está apagada')

    def test_sin_permiso_de_consultar_info_no_se_pide(self):
        self.usuario.user_permissions.remove(Permission.objects.get(codename='consultar_info_estacion'))
        resp, enviar = self._buscar()
        enviar.assert_not_called()
        self.assertContains(resp, 'no tiene permiso para pedírselo')

    def test_con_hardware_ya_cargado_no_se_pide(self):
        Estacion.objects.filter(pk=self.estacion.pk).update(procesador='Intel i3', ram_total_mb=8192)
        resp, enviar = self._buscar()
        enviar.assert_not_called()
        self.assertContains(resp, 'Datos tomados de la estación')
