import csv
import io

from django.conf import settings
from datetime import date, timedelta
from unittest.mock import patch

from cryptography.fernet import Fernet
from django.contrib.auth.models import Permission, User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from django_otp.oath import totp
from django_otp.plugins.otp_totp.models import TOTPDevice

from apps.activos.models import (
    Activo, Bodega, Colaborador, MovimientoInventario, OrdenCompra, OrdenCompraDetalle, RecepcionLote,
    StockBodega, TipoConsumible,
)
from apps.auditoria.models import EventoAuditoria, registrar_evento
from apps.catalogo import crypto
from apps.catalogo.models import (
    ClaveRecuperacionBitLocker, Estacion, Farmacia, Grupo, PerifericoDetectado, UnidadNegocio, VersionAgente,
)
from apps.cuentas.models import PerfilUsuario
from apps.cumplimiento.models import (
    ActividadCumplimiento, ResultadoCumplimientoEstacion, TipoObjetivoCumplimiento,
)
from apps.despliegues.models import Despliegue
from apps.mantenimiento.models import (
    EstadoGeneralEquipo, Mantenimiento, PrioridadMantenimiento, TipoMantenimiento, VisitaTecnica,
)
from apps.monitoreo.models import (
    Alerta, Metrica, MuestraMetrica, MuestraRedFarmacia, PosErrorDetectado, ReglaAlerta, VentanaMantenimiento,
)
from apps.scripts.models import EjecucionScript, Script, ScriptProgramado, TipoScript
from apps.software.models import SoftwareInstaladoDetectado


class EstacionMeshCentralTests(TestCase):
    def setUp(self):
        grupo = Grupo.objects.create(codigo='TRX001')
        farmacia = Farmacia.objects.create(
            codigo='ML001', grupo=grupo, unidad_negocio=UnidadNegocio.objects.get(codigo='SG'),
        )
        self.estacion = Estacion.objects.create(
            codigo='ML001-A', farmacia=farmacia,
            estado_aprobacion=Estacion.EstadoAprobacion.APROBADA,
            estado_conexion=Estacion.EstadoConexion.ONLINE,
        )

        ver_estacion = Permission.objects.get(content_type__app_label='catalogo', codename='view_estacion')

        self.usuario_sin_permiso = User.objects.create_user(username='sin_permiso', password='x')
        PerfilUsuario.objects.create(usuario=self.usuario_sin_permiso, acceso_todas_unidades=True)
        self.usuario_sin_permiso.user_permissions.add(ver_estacion)

        self.usuario_con_permiso = User.objects.create_user(username='con_permiso', password='x')
        PerfilUsuario.objects.create(usuario=self.usuario_con_permiso, acceso_todas_unidades=True)
        permiso = Permission.objects.get(content_type__app_label='catalogo', codename='acceso_remoto_estacion')
        self.usuario_con_permiso.user_permissions.add(permiso, ver_estacion)

    def test_vincular_requiere_permiso(self):
        self.client.force_login(self.usuario_sin_permiso)
        resp = self.client.post(
            reverse('panel:estacion_meshcentral_vincular', args=[self.estacion.pk]),
            {'meshcentral_node_id': 'nodeid123'},
        )
        self.assertEqual(resp.status_code, 403)

    def test_vincular_guarda_node_id_y_audita(self):
        self.client.force_login(self.usuario_con_permiso)
        resp = self.client.post(
            reverse('panel:estacion_meshcentral_vincular', args=[self.estacion.pk]),
            {'meshcentral_node_id': 'nodeid123'},
        )
        self.assertEqual(resp.status_code, 200)

        self.estacion.refresh_from_db()
        self.assertEqual(self.estacion.meshcentral_node_id, 'nodeid123')
        self.assertIsNotNone(self.estacion.meshcentral_vinculado_en)
        self.assertTrue(
            EventoAuditoria.objects.filter(accion='estacion.meshcentral_vincular').exists(),
        )

    def test_abrir_escritorio_redirige_y_audita_con_node_id(self):
        self.estacion.meshcentral_node_id = 'nodeid123'
        self.estacion.save(update_fields=['meshcentral_node_id'])

        self.client.force_login(self.usuario_con_permiso)
        resp = self.client.post(
            reverse('panel:estacion_meshcentral_escritorio', args=[self.estacion.pk]),
        )

        self.assertEqual(resp.status_code, 302)
        self.assertIn('node=nodeid123', resp.url)
        self.assertTrue(
            EventoAuditoria.objects.filter(accion='estacion.meshcentral_abrir_escritorio').exists(),
        )

    def test_abrir_escritorio_sin_node_id_no_audita(self):
        self.client.force_login(self.usuario_con_permiso)
        resp = self.client.post(
            reverse('panel:estacion_meshcentral_escritorio', args=[self.estacion.pk]),
        )

        self.assertRedirects(resp, reverse('panel:estaciones_lista'))
        self.assertFalse(
            EventoAuditoria.objects.filter(accion='estacion.meshcentral_abrir_escritorio').exists(),
        )

    def test_modal_muestra_seccion_solo_con_permiso(self):
        url = reverse('panel:estacion_info_modal', args=[self.estacion.pk])

        self.client.force_login(self.usuario_sin_permiso)
        resp = self.client.get(url)
        self.assertNotContains(resp, 'Acceso remoto (MeshCentral)')

        self.client.force_login(self.usuario_con_permiso)
        resp = self.client.get(url)
        self.assertContains(resp, 'Acceso remoto (MeshCentral)')
        self.assertContains(resp, 'No vinculado')

        self.estacion.meshcentral_node_id = 'nodeid123'
        self.estacion.save(update_fields=['meshcentral_node_id'])
        resp = self.client.get(url)
        self.assertContains(resp, 'Vinculado')


class EstacionSupervisionGrabacionesTests(TestCase):
    """Auditoría por grabación (revisión posterior) es un permiso separado de
    acceso_remoto_estacion (soporte en vivo): quien puede uno no necesariamente puede el otro."""

    def setUp(self):
        grupo = Grupo.objects.create(codigo='TRX001')
        farmacia = Farmacia.objects.create(
            codigo='ML001', grupo=grupo, unidad_negocio=UnidadNegocio.objects.get(codigo='SG'),
        )
        self.estacion = Estacion.objects.create(
            codigo='ML001-A', farmacia=farmacia, meshcentral_node_id='nodeid123',
            estado_aprobacion=Estacion.EstadoAprobacion.APROBADA,
        )

        self.usuario_soporte = User.objects.create_user(username='soporte_vivo', password='x')
        PerfilUsuario.objects.create(usuario=self.usuario_soporte, acceso_todas_unidades=True)
        self.usuario_soporte.user_permissions.add(
            Permission.objects.get(content_type__app_label='catalogo', codename='acceso_remoto_estacion'),
        )

        self.usuario_auditor = User.objects.create_user(username='auditor_grab', password='x')
        PerfilUsuario.objects.create(usuario=self.usuario_auditor, acceso_todas_unidades=True)
        self.usuario_auditor.user_permissions.add(
            Permission.objects.get(content_type__app_label='catalogo', codename='supervision_auditoria_estacion'),
        )

    def test_tener_acceso_remoto_no_alcanza_para_ver_grabaciones(self):
        self.client.force_login(self.usuario_soporte)
        resp = self.client.post(reverse('panel:estacion_supervision_grabaciones', args=[self.estacion.pk]))
        self.assertEqual(resp.status_code, 403)

    def test_auditor_no_puede_abrir_soporte_en_vivo(self):
        self.client.force_login(self.usuario_auditor)
        resp = self.client.post(reverse('panel:estacion_meshcentral_escritorio', args=[self.estacion.pk]))
        self.assertEqual(resp.status_code, 403)

    def test_auditor_puede_ver_grabaciones_y_queda_auditado(self):
        self.client.force_login(self.usuario_auditor)
        resp = self.client.post(reverse('panel:estacion_supervision_grabaciones', args=[self.estacion.pk]))
        self.assertEqual(resp.status_code, 302)
        self.assertIn('node=nodeid123', resp.url)
        self.assertTrue(
            EventoAuditoria.objects.filter(accion='estacion.supervision_grabacion_ver').exists(),
        )


@override_settings(BITLOCKER_ENCRYPTION_KEY=Fernet.generate_key().decode())
class EstacionBitlockerClaveTests(TestCase):
    """ver_clave_bitlocker es un permiso propio, más sensible que los otros dos de
    acceso a estaciones (con la clave se descifra el disco): nadie lo hereda."""

    def setUp(self):
        self.sg = UnidadNegocio.objects.get(codigo='SG')
        self.mia = UnidadNegocio.objects.get(codigo='MIA')
        grupo = Grupo.objects.create(codigo='TRX001')
        farmacia_sg = Farmacia.objects.create(codigo='ML001', grupo=grupo, unidad_negocio=self.sg)
        self.estacion = Estacion.objects.create(
            codigo='ML001-A', farmacia=farmacia_sg, bitlocker_habilitado=True,
            estado_aprobacion=Estacion.EstadoAprobacion.APROBADA,
        )
        ClaveRecuperacionBitLocker.objects.create(
            estacion=self.estacion, clave_cifrada=crypto.cifrar('111111-222222-333333'),
        )

        self.usuario_soporte = User.objects.create_user(username='soporte_bl', password='x')
        PerfilUsuario.objects.create(usuario=self.usuario_soporte, acceso_todas_unidades=True)
        self.usuario_soporte.user_permissions.add(
            Permission.objects.get(content_type__app_label='catalogo', codename='acceso_remoto_estacion'),
        )

        self.usuario_bl = User.objects.create_user(username='ve_bitlocker', password='x')
        PerfilUsuario.objects.create(usuario=self.usuario_bl, acceso_todas_unidades=True)
        self.usuario_bl.user_permissions.add(
            Permission.objects.get(content_type__app_label='catalogo', codename='ver_clave_bitlocker'),
        )

        self.usuario_mia = User.objects.create_user(username='user_mia_bl', password='x')
        PerfilUsuario.objects.create(usuario=self.usuario_mia).unidades_negocio.add(self.mia)
        self.usuario_mia.user_permissions.add(
            Permission.objects.get(content_type__app_label='catalogo', codename='ver_clave_bitlocker'),
        )

    def test_acceso_remoto_no_alcanza_para_ver_la_clave(self):
        self.client.force_login(self.usuario_soporte)
        resp = self.client.post(reverse('panel:estacion_bitlocker_ver_clave', args=[self.estacion.pk]))
        self.assertEqual(resp.status_code, 403)

    def test_usuario_con_permiso_ve_la_clave_y_queda_auditado(self):
        self.client.force_login(self.usuario_bl)
        resp = self.client.post(reverse('panel:estacion_bitlocker_ver_clave', args=[self.estacion.pk]))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, '111111-222222-333333')
        self.assertTrue(EventoAuditoria.objects.filter(accion='estacion.bitlocker_clave_ver').exists())

    def test_otro_tenant_con_el_permiso_no_puede_verla(self):
        self.client.force_login(self.usuario_mia)
        resp = self.client.post(reverse('panel:estacion_bitlocker_ver_clave', args=[self.estacion.pk]))
        self.assertEqual(resp.status_code, 403)

    def test_sin_clave_registrada_no_audita_ni_revienta(self):
        estacion_sin_clave = Estacion.objects.create(
            codigo='ML001-B', farmacia=self.estacion.farmacia, bitlocker_habilitado=True,
            estado_aprobacion=Estacion.EstadoAprobacion.APROBADA,
        )
        self.client.force_login(self.usuario_bl)
        resp = self.client.post(reverse('panel:estacion_bitlocker_ver_clave', args=[estacion_sin_clave.pk]))
        self.assertEqual(resp.status_code, 200)
        self.assertNotContains(resp, '111111-222222-333333')
        self.assertFalse(EventoAuditoria.objects.filter(accion='estacion.bitlocker_clave_ver').exists())


class EstacionWindowsUpdateSolicitarTests(TestCase):
    def setUp(self):
        grupo = Grupo.objects.create(codigo='TRX001')
        farmacia = Farmacia.objects.create(
            codigo='ML001', grupo=grupo, unidad_negocio=UnidadNegocio.objects.get(codigo='SG'),
        )
        self.estacion = Estacion.objects.create(
            codigo='ML001-A', farmacia=farmacia,
            estado_aprobacion=Estacion.EstadoAprobacion.APROBADA,
            estado_conexion=Estacion.EstadoConexion.ONLINE,
        )
        self.usuario = User.objects.create_user(username='u', password='x')
        PerfilUsuario.objects.create(usuario=self.usuario, acceso_todas_unidades=True)
        self.usuario.user_permissions.add(
            Permission.objects.get(content_type__app_label='catalogo', codename='escanear_actualizaciones_estacion'),
        )
        self.client.force_login(self.usuario)

    def test_sin_permiso_devuelve_403(self):
        sin_permiso = User.objects.create_user(username='u_sin_wu', password='x')
        PerfilUsuario.objects.create(usuario=sin_permiso, acceso_todas_unidades=True)
        self.client.force_login(sin_permiso)
        resp = self.client.post(reverse('panel:estacion_windows_update_solicitar', args=[self.estacion.pk]))
        self.assertEqual(resp.status_code, 403)

    def test_envia_el_comando_y_audita(self):
        with patch('apps.panel.views.estaciones.enviar_comando', return_value=True) as mock_enviar:
            resp = self.client.post(reverse('panel:estacion_windows_update_solicitar', args=[self.estacion.pk]))
        self.assertEqual(resp.status_code, 200)
        mock_enviar.assert_called_once_with(self.estacion, 'escanear_actualizaciones')
        self.assertTrue(EventoAuditoria.objects.filter(accion='estacion.escanear_actualizaciones').exists())

    def test_estacion_no_aprobada_no_envia_ni_audita(self):
        self.estacion.estado_aprobacion = Estacion.EstadoAprobacion.PENDIENTE
        self.estacion.save(update_fields=['estado_aprobacion'])
        with patch('apps.panel.views.estaciones.enviar_comando', return_value=True) as mock_enviar:
            resp = self.client.post(reverse('panel:estacion_windows_update_solicitar', args=[self.estacion.pk]))
        self.assertEqual(resp.status_code, 200)
        mock_enviar.assert_not_called()
        self.assertFalse(EventoAuditoria.objects.filter(accion='estacion.escanear_actualizaciones').exists())

    def test_fallo_de_publish_no_audita(self):
        with patch('apps.panel.views.estaciones.enviar_comando', return_value=False):
            resp = self.client.post(reverse('panel:estacion_windows_update_solicitar', args=[self.estacion.pk]))
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(EventoAuditoria.objects.filter(accion='estacion.escanear_actualizaciones').exists())


class EstacionSoftwareInstaladoSolicitarTests(TestCase):
    """Mismo permiso que 'Actualizar ahora' (consultar_info_estacion) — no es una
    acción de riesgo, así que reusa el permiso en vez de uno propio."""

    def setUp(self):
        grupo = Grupo.objects.create(codigo='TRX001')
        farmacia = Farmacia.objects.create(
            codigo='ML001', grupo=grupo, unidad_negocio=UnidadNegocio.objects.get(codigo='SG'),
        )
        self.estacion = Estacion.objects.create(
            codigo='ML001-A', farmacia=farmacia,
            estado_aprobacion=Estacion.EstadoAprobacion.APROBADA,
            estado_conexion=Estacion.EstadoConexion.ONLINE,
        )
        self.usuario = User.objects.create_user(username='u_sw', password='x')
        PerfilUsuario.objects.create(usuario=self.usuario, acceso_todas_unidades=True)
        self.usuario.user_permissions.add(
            Permission.objects.get(content_type__app_label='catalogo', codename='consultar_info_estacion'),
            Permission.objects.get(content_type__app_label='catalogo', codename='view_estacion'),
        )
        self.client.force_login(self.usuario)

    def test_sin_permiso_devuelve_403(self):
        sin_permiso = User.objects.create_user(username='u_sw_sin', password='x')
        PerfilUsuario.objects.create(usuario=sin_permiso, acceso_todas_unidades=True)
        self.client.force_login(sin_permiso)
        resp = self.client.post(reverse('panel:estacion_software_instalado_solicitar', args=[self.estacion.pk]))
        self.assertEqual(resp.status_code, 403)

    def test_envia_el_comando_y_audita(self):
        with patch('apps.panel.views.estaciones.enviar_comando', return_value=True) as mock_enviar:
            resp = self.client.post(reverse('panel:estacion_software_instalado_solicitar', args=[self.estacion.pk]))
        self.assertEqual(resp.status_code, 200)
        mock_enviar.assert_called_once_with(self.estacion, 'consultar_software_instalado')
        self.assertTrue(EventoAuditoria.objects.filter(accion='estacion.consultar_software_instalado').exists())

    def test_estacion_no_aprobada_no_envia_ni_audita(self):
        self.estacion.estado_aprobacion = Estacion.EstadoAprobacion.PENDIENTE
        self.estacion.save(update_fields=['estado_aprobacion'])
        with patch('apps.panel.views.estaciones.enviar_comando', return_value=True) as mock_enviar:
            resp = self.client.post(reverse('panel:estacion_software_instalado_solicitar', args=[self.estacion.pk]))
        self.assertEqual(resp.status_code, 200)
        mock_enviar.assert_not_called()

    def test_modal_muestra_lo_detectado_en_el_ultimo_escaneo(self):
        SoftwareInstaladoDetectado.objects.create(estacion=self.estacion, nombre='Google Chrome', version='118.0')
        self.estacion.software_instalado_ultima_verificacion = timezone.now()
        self.estacion.save(update_fields=['software_instalado_ultima_verificacion'])
        resp = self.client.get(reverse('panel:estacion_info_modal', args=[self.estacion.pk]))
        self.assertContains(resp, 'Google Chrome')
        self.assertContains(resp, '118.0')


class EstacionPerifericosSolicitarTests(TestCase):
    """Mismo permiso que 'Actualizar ahora' (consultar_info_estacion) — no es una
    acción de riesgo, así que reusa el permiso en vez de uno propio."""

    def setUp(self):
        grupo = Grupo.objects.create(codigo='TRX001')
        farmacia = Farmacia.objects.create(
            codigo='ML001', grupo=grupo, unidad_negocio=UnidadNegocio.objects.get(codigo='SG'),
        )
        self.estacion = Estacion.objects.create(
            codigo='ML001-A', farmacia=farmacia,
            estado_aprobacion=Estacion.EstadoAprobacion.APROBADA,
            estado_conexion=Estacion.EstadoConexion.ONLINE,
        )
        self.usuario = User.objects.create_user(username='u_perif', password='x')
        PerfilUsuario.objects.create(usuario=self.usuario, acceso_todas_unidades=True)
        self.usuario.user_permissions.add(
            Permission.objects.get(content_type__app_label='catalogo', codename='consultar_info_estacion'),
            Permission.objects.get(content_type__app_label='catalogo', codename='view_estacion'),
        )
        self.client.force_login(self.usuario)

    def test_sin_permiso_devuelve_403(self):
        sin_permiso = User.objects.create_user(username='u_perif_sin', password='x')
        PerfilUsuario.objects.create(usuario=sin_permiso, acceso_todas_unidades=True)
        self.client.force_login(sin_permiso)
        resp = self.client.post(reverse('panel:estacion_perifericos_solicitar', args=[self.estacion.pk]))
        self.assertEqual(resp.status_code, 403)

    def test_envia_el_comando_y_audita(self):
        with patch('apps.panel.views.estaciones.enviar_comando', return_value=True) as mock_enviar:
            resp = self.client.post(reverse('panel:estacion_perifericos_solicitar', args=[self.estacion.pk]))
        self.assertEqual(resp.status_code, 200)
        mock_enviar.assert_called_once_with(self.estacion, 'consultar_perifericos')
        self.assertTrue(EventoAuditoria.objects.filter(accion='estacion.consultar_perifericos').exists())

    def test_estacion_no_aprobada_no_envia_ni_audita(self):
        self.estacion.estado_aprobacion = Estacion.EstadoAprobacion.PENDIENTE
        self.estacion.save(update_fields=['estado_aprobacion'])
        with patch('apps.panel.views.estaciones.enviar_comando', return_value=True) as mock_enviar:
            resp = self.client.post(reverse('panel:estacion_perifericos_solicitar', args=[self.estacion.pk]))
        self.assertEqual(resp.status_code, 200)
        mock_enviar.assert_not_called()

    def test_modal_muestra_lo_detectado_en_el_ultimo_escaneo(self):
        PerifericoDetectado.objects.create(estacion=self.estacion, nombre='Teclado USB', clase='Keyboard', device_id='USB\\1')
        self.estacion.perifericos_ultima_verificacion = timezone.now()
        self.estacion.save(update_fields=['perifericos_ultima_verificacion'])
        resp = self.client.get(reverse('panel:estacion_info_modal', args=[self.estacion.pk]))
        self.assertContains(resp, 'Teclado USB')
        self.assertContains(resp, 'Keyboard')


class EstacionPowerPlanModalTests(TestCase):
    def test_modal_muestra_el_plan_de_energia_reportado(self):
        grupo = Grupo.objects.create(codigo='TRX001')
        farmacia = Farmacia.objects.create(
            codigo='ML001', grupo=grupo, unidad_negocio=UnidadNegocio.objects.get(codigo='SG'),
        )
        estacion = Estacion.objects.create(
            codigo='ML001-A', farmacia=farmacia, estado_aprobacion=Estacion.EstadoAprobacion.APROBADA,
            power_plan_actual='Alto rendimiento',
        )
        usuario = User.objects.create_user(username='u_power', password='x')
        PerfilUsuario.objects.create(usuario=usuario, acceso_todas_unidades=True)
        usuario.user_permissions.add(
            Permission.objects.get(content_type__app_label='catalogo', codename='view_estacion'),
        )
        self.client.force_login(usuario)

        resp = self.client.get(reverse('panel:estacion_info_modal', args=[estacion.pk]))
        self.assertContains(resp, 'Alto rendimiento')


class EstacionPosErroresModalTests(TestCase):
    def test_modal_muestra_los_errores_del_pos_detectados(self):
        grupo = Grupo.objects.create(codigo='TRX001')
        farmacia = Farmacia.objects.create(
            codigo='ML001', grupo=grupo, unidad_negocio=UnidadNegocio.objects.get(codigo='SG'),
        )
        estacion = Estacion.objects.create(
            codigo='ML001-A', farmacia=farmacia, estado_aprobacion=Estacion.EstadoAprobacion.APROBADA,
        )
        PosErrorDetectado.objects.create(
            estacion=estacion, mensaje='no existe la relación X', nivel='ERROR', cantidad_total=42,
        )
        usuario = User.objects.create_user(username='u_pos_modal', password='x')
        PerfilUsuario.objects.create(usuario=usuario, acceso_todas_unidades=True)
        usuario.user_permissions.add(
            Permission.objects.get(content_type__app_label='catalogo', codename='view_estacion'),
        )
        self.client.force_login(usuario)

        resp = self.client.get(reverse('panel:estacion_info_modal', args=[estacion.pk]))
        self.assertContains(resp, 'no existe la relación X')
        self.assertContains(resp, 'x42')

    def test_sin_errores_no_muestra_la_seccion(self):
        grupo = Grupo.objects.create(codigo='TRX001')
        farmacia = Farmacia.objects.create(
            codigo='ML001', grupo=grupo, unidad_negocio=UnidadNegocio.objects.get(codigo='SG'),
        )
        estacion = Estacion.objects.create(
            codigo='ML001-A', farmacia=farmacia, estado_aprobacion=Estacion.EstadoAprobacion.APROBADA,
        )
        usuario = User.objects.create_user(username='u_pos_modal2', password='x')
        PerfilUsuario.objects.create(usuario=usuario, acceso_todas_unidades=True)
        usuario.user_permissions.add(
            Permission.objects.get(content_type__app_label='catalogo', codename='view_estacion'),
        )
        self.client.force_login(usuario)

        resp = self.client.get(reverse('panel:estacion_info_modal', args=[estacion.pk]))
        self.assertNotContains(resp, 'Errores del POS')


class MonitoreoDiscoTests(TestCase):
    """El disco es la tercera métrica agregada al monitoreo continuo (junto a CPU/RAM,
    que ya reportaba el pipeline servidor sin que el agente real las emitiera — ver
    PLAN_MODERNIZACION.md §9, fase R8): confirma que la tarjeta/gráfico nuevos
    aparecen en ambas vistas."""

    def setUp(self):
        grupo = Grupo.objects.create(codigo='TRX001')
        farmacia = Farmacia.objects.create(
            codigo='ML001', grupo=grupo, unidad_negocio=UnidadNegocio.objects.get(codigo='SG'),
        )
        self.estacion = Estacion.objects.create(
            codigo='ML001-A', farmacia=farmacia, estado_aprobacion=Estacion.EstadoAprobacion.APROBADA,
            monitorear_recursos=True,
        )
        MuestraMetrica.objects.create(estacion=self.estacion, disco_total_gb=200.0, disco_libre_gb=20.0)
        usuario = User.objects.create_user(username='u_disco', password='x')
        PerfilUsuario.objects.create(usuario=usuario, acceso_todas_unidades=True)
        usuario.user_permissions.add(
            Permission.objects.get(content_type__app_label='monitoreo', codename='view_muestrametrica'),
        )
        self.client.force_login(usuario)

    def test_lista_muestra_pct_de_disco_usado(self):
        resp = self.client.get(reverse('panel:monitoreo_lista'))
        # (200-20)/200 = 90% — coma decimal por locale es-EC, no punto.
        self.assertContains(resp, '90,0')

    def test_detalle_muestra_grafico_y_libres_de_total(self):
        resp = self.client.get(reverse('panel:monitoreo_detalle_partial', args=[self.estacion.pk]))
        self.assertContains(resp, '90,0')
        self.assertContains(resp, '20,0 GB libres de 200,0 GB')


class MonitoreoRedTests(TestCase):
    """Consumo de red por estación (extiende R8, mismo patrón que MonitoreoDiscoTests
    de arriba) — confirma que la tarjeta/gráfico nuevos aparecen en ambas vistas."""

    def setUp(self):
        grupo = Grupo.objects.create(codigo='TRX001')
        farmacia = Farmacia.objects.create(
            codigo='ML001', grupo=grupo, unidad_negocio=UnidadNegocio.objects.get(codigo='SG'),
        )
        self.estacion = Estacion.objects.create(
            codigo='ML001-A', farmacia=farmacia, estado_aprobacion=Estacion.EstadoAprobacion.APROBADA,
            monitorear_recursos=True,
        )
        MuestraMetrica.objects.create(estacion=self.estacion, red_recibido_kbps=1200.0, red_enviado_kbps=300.5)
        usuario = User.objects.create_user(username='u_red', password='x')
        PerfilUsuario.objects.create(usuario=usuario, acceso_todas_unidades=True)
        usuario.user_permissions.add(
            Permission.objects.get(content_type__app_label='monitoreo', codename='view_muestrametrica'),
        )
        self.client.force_login(usuario)

    def test_lista_muestra_kbps_totales(self):
        resp = self.client.get(reverse('panel:monitoreo_lista'))
        # 1200.0 + 300.5 = 1500.5 — coma decimal por locale es-EC, no punto.
        self.assertContains(resp, '1500,5')

    def test_detalle_muestra_grafico_y_desglose_bajada_subida(self):
        resp = self.client.get(reverse('panel:monitoreo_detalle_partial', args=[self.estacion.pk]))
        self.assertContains(resp, '1500,5')
        self.assertContains(resp, '1200,0')
        self.assertContains(resp, '300,5')


class RedFarmaciasListaTests(TestCase):
    """Consumo de red por FARMACIA (Parte A, SNMP a Mikrotik) — solo visibilidad,
    ver docstring de apps.panel.views.monitoreo.red_farmacias_lista."""

    def setUp(self):
        self.sg = UnidadNegocio.objects.get(codigo='SG')
        self.mia = UnidadNegocio.objects.get(codigo='MIA')
        grupo = Grupo.objects.create(codigo='TRX001')
        self.farmacia_sg = Farmacia.objects.create(
            codigo='ML001', grupo=grupo, unidad_negocio=self.sg, ip_router='10.0.1.1',
        )
        self.farmacia_sin_ip = Farmacia.objects.create(codigo='ML002', grupo=grupo, unidad_negocio=self.sg)
        usuario = User.objects.create_user(username='u_red_farmacia', password='x')
        PerfilUsuario.objects.create(usuario=usuario, acceso_todas_unidades=True)
        usuario.user_permissions.add(
            Permission.objects.get(content_type__app_label='monitoreo', codename='view_muestraredfarmacia'),
        )
        self.client.force_login(usuario)

    def test_farmacia_sin_ip_router_no_aparece(self):
        resp = self.client.get(reverse('panel:red_farmacias_lista'))
        self.assertNotContains(resp, 'ML002')

    def test_farmacia_con_ip_router_pero_nunca_sondeada_muestra_sin_dato(self):
        resp = self.client.get(reverse('panel:red_farmacias_lista'))
        self.assertContains(resp, 'ML001')
        self.assertContains(resp, 'sin dato')

    def test_muestra_el_consumo_de_la_ultima_muestra(self):
        MuestraRedFarmacia.objects.create(
            farmacia=self.farmacia_sg, bytes_recibidos=1000, bytes_enviados=500,
            red_recibido_kbps=1200.0, red_enviado_kbps=300.5,
        )
        resp = self.client.get(reverse('panel:red_farmacias_lista'))
        self.assertContains(resp, '1500,5kb/s')  # coma decimal por locale es-EC

    def test_no_mezcla_farmacias_de_otro_tenant(self):
        grupo2 = Grupo.objects.create(codigo='TRX002')
        Farmacia.objects.create(codigo='MAM01', grupo=grupo2, unidad_negocio=self.mia, ip_router='10.0.2.1')

        usuario_sg_only = User.objects.create_user(username='u_sg_only_red', password='x')
        PerfilUsuario.objects.create(usuario=usuario_sg_only).unidades_negocio.add(self.sg)
        usuario_sg_only.user_permissions.add(
            Permission.objects.get(content_type__app_label='monitoreo', codename='view_muestraredfarmacia'),
        )
        self.client.force_login(usuario_sg_only)

        resp = self.client.get(reverse('panel:red_farmacias_lista'))
        self.assertNotContains(resp, 'MAM01')


class EstacionMesaDeAyudaVsSoporteTecnicoTests(TestCase):
    """Los cuatro permisos que separan mesa de ayuda (diagnóstico) de soporte técnico
    (acciones de riesgo): consultar_info/aprobar/reiniciar/escanear_actualizaciones.
    `seed_permisos` es quien arma los dos Groups; acá se prueba el gating en sí."""

    def setUp(self):
        grupo = Grupo.objects.create(codigo='TRX001')
        farmacia = Farmacia.objects.create(
            codigo='ML001', grupo=grupo, unidad_negocio=UnidadNegocio.objects.get(codigo='SG'),
        )
        self.estacion = Estacion.objects.create(
            codigo='ML001-A', farmacia=farmacia,
            estado_aprobacion=Estacion.EstadoAprobacion.PENDIENTE,
            estado_conexion=Estacion.EstadoConexion.ONLINE,
        )

        self.mesa_de_ayuda = User.objects.create_user(username='mesa_ayuda', password='x')
        PerfilUsuario.objects.create(usuario=self.mesa_de_ayuda, acceso_todas_unidades=True)
        self.mesa_de_ayuda.user_permissions.add(
            Permission.objects.get(content_type__app_label='catalogo', codename='consultar_info_estacion'),
        )

        self.soporte_tecnico = User.objects.create_user(username='soporte_tec', password='x')
        PerfilUsuario.objects.create(usuario=self.soporte_tecnico, acceso_todas_unidades=True)
        for codename in ('consultar_info_estacion', 'aprobar_estacion', 'reiniciar_estacion', 'view_estacion'):
            self.soporte_tecnico.user_permissions.add(
                Permission.objects.get(content_type__app_label='catalogo', codename=codename),
            )

    def test_mesa_de_ayuda_puede_pedir_info_pero_no_aprobar_ni_reiniciar(self):
        self.client.force_login(self.mesa_de_ayuda)
        resp = self.client.post(reverse('panel:estacion_info_solicitar', args=[self.estacion.pk]))
        self.assertEqual(resp.status_code, 200)

        resp = self.client.post(reverse('panel:estacion_aprobar', args=[self.estacion.pk]))
        self.assertEqual(resp.status_code, 403)

        self.estacion.estado_aprobacion = Estacion.EstadoAprobacion.APROBADA
        self.estacion.save(update_fields=['estado_aprobacion'])
        resp = self.client.post(reverse('panel:estacion_reiniciar', args=[self.estacion.pk]))
        self.assertEqual(resp.status_code, 403)

    def test_soporte_tecnico_puede_aprobar_rechazar_y_reiniciar(self):
        self.client.force_login(self.soporte_tecnico)

        resp = self.client.post(reverse('panel:estacion_aprobar', args=[self.estacion.pk]))
        self.assertEqual(resp.status_code, 200)
        self.estacion.refresh_from_db()
        self.assertEqual(self.estacion.estado_aprobacion, Estacion.EstadoAprobacion.APROBADA)

        with patch('apps.panel.views.estaciones.enviar_comando', return_value=True):
            resp = self.client.post(reverse('panel:estacion_reiniciar', args=[self.estacion.pk]))
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(EventoAuditoria.objects.filter(accion='estacion.reiniciar').exists())

        resp = self.client.post(reverse('panel:estacion_rechazar', args=[self.estacion.pk]))
        self.assertEqual(resp.status_code, 200)
        self.estacion.refresh_from_db()
        self.assertEqual(self.estacion.estado_aprobacion, Estacion.EstadoAprobacion.RECHAZADA)

    def test_sin_ningun_permiso_no_puede_hacer_nada_de_esto(self):
        sin_permiso = User.objects.create_user(username='sin_nada', password='x')
        PerfilUsuario.objects.create(usuario=sin_permiso, acceso_todas_unidades=True)
        self.client.force_login(sin_permiso)

        self.assertEqual(
            self.client.post(reverse('panel:estacion_info_solicitar', args=[self.estacion.pk])).status_code, 403,
        )
        self.assertEqual(
            self.client.post(reverse('panel:estacion_aprobar', args=[self.estacion.pk])).status_code, 403,
        )
        self.assertEqual(
            self.client.post(reverse('panel:estaciones_aprobar_lote'), {'estacion_ids': [self.estacion.pk]}).status_code,
            403,
        )


class EstacionActualizarAgenteTests(TestCase):
    """Actualización remota del agente desde el panel: mismo permiso propio
    (catalogo.actualizar_agente_estacion) que las demás acciones de riesgo sobre una
    estación (aprobar/reiniciar) — ver seed_permisos.py."""

    def setUp(self):
        self.sg = UnidadNegocio.objects.get(codigo='SG')
        grupo = Grupo.objects.create(codigo='TRX001')
        farmacia = Farmacia.objects.create(codigo='ML001', grupo=grupo, unidad_negocio=self.sg)
        self.estacion = Estacion.objects.create(
            codigo='ML001-A', farmacia=farmacia, estado_aprobacion=Estacion.EstadoAprobacion.APROBADA,
            estado_conexion=Estacion.EstadoConexion.ONLINE, version_agente='agente-prueba-0.1',
        )
        creador = User.objects.create_user(username='creador_va', password='x')
        self.version = VersionAgente.objects.create(
            version='agente-prueba-0.2', ejecutable=SimpleUploadedFile('agente.exe', b'contenido-falso'),
            creado_por=creador,
        )

        self.sin_permiso = User.objects.create_user(username='sin_permiso_va', password='x')
        PerfilUsuario.objects.create(usuario=self.sin_permiso, acceso_todas_unidades=True)

        self.con_permiso = User.objects.create_user(username='con_permiso_va', password='x')
        PerfilUsuario.objects.create(usuario=self.con_permiso, acceso_todas_unidades=True)
        self.con_permiso.user_permissions.add(
            Permission.objects.get(content_type__app_label='catalogo', codename='actualizar_agente_estacion'),
            Permission.objects.get(content_type__app_label='catalogo', codename='view_estacion'),
        )

    def test_sin_el_permiso_devuelve_403(self):
        self.client.force_login(self.sin_permiso)
        resp = self.client.post(reverse('panel:estacion_actualizar_agente_solicitar', args=[self.estacion.pk]))
        self.assertEqual(resp.status_code, 403)

    def test_con_el_permiso_envia_la_actualizacion(self):
        self.client.force_login(self.con_permiso)
        with patch('apps.panel.views.estaciones.enviar_actualizacion_agente', return_value=True) as mock_enviar:
            resp = self.client.post(reverse('panel:estacion_actualizar_agente_solicitar', args=[self.estacion.pk]))
        self.assertEqual(resp.status_code, 200)
        mock_enviar.assert_called_once_with(self.estacion, self.version)
        self.assertTrue(EventoAuditoria.objects.filter(accion='estacion.actualizar_agente').exists())

    def test_sin_ninguna_version_de_agente_cargada_no_envia_nada(self):
        self.version.delete()
        self.client.force_login(self.con_permiso)
        with patch('apps.panel.views.estaciones.enviar_actualizacion_agente') as mock_enviar:
            resp = self.client.post(reverse('panel:estacion_actualizar_agente_solicitar', args=[self.estacion.pk]))
        self.assertEqual(resp.status_code, 200)
        mock_enviar.assert_not_called()

    def test_estacion_offline_no_envia_nada(self):
        self.estacion.estado_conexion = Estacion.EstadoConexion.OFFLINE
        self.estacion.save(update_fields=['estado_conexion'])
        self.client.force_login(self.con_permiso)
        with patch('apps.panel.views.estaciones.enviar_actualizacion_agente') as mock_enviar:
            self.client.post(reverse('panel:estacion_actualizar_agente_solicitar', args=[self.estacion.pk]))
        mock_enviar.assert_not_called()


class CumplimientoViewsTests(TestCase):
    def setUp(self):
        self.usuario = User.objects.create_user(username='u', password='x')
        PerfilUsuario.objects.create(usuario=self.usuario, acceso_todas_unidades=True)
        self.usuario.user_permissions.add(
            Permission.objects.get(content_type__app_label='cumplimiento', codename='view_actividadcumplimiento'),
            Permission.objects.get(content_type__app_label='cumplimiento', codename='add_actividadcumplimiento'),
            Permission.objects.get(content_type__app_label='cumplimiento', codename='change_actividadcumplimiento'),
        )
        self.client.force_login(self.usuario)

        self.sg = UnidadNegocio.objects.get(codigo='SG')
        grupo = Grupo.objects.create(codigo='TRX001')
        farmacia = Farmacia.objects.create(codigo='ML001', grupo=grupo, unidad_negocio=self.sg)
        self.estacion = Estacion.objects.create(
            codigo='ML001-A', farmacia=farmacia, estado_aprobacion=Estacion.EstadoAprobacion.APROBADA,
        )

    def test_lista_responde_200(self):
        resp = self.client.get(reverse('panel:cumplimiento_lista'))
        self.assertEqual(resp.status_code, 200)

    def test_crear_genera_resultados_y_redirige_a_detalle(self):
        resp = self.client.post(reverse('panel:cumplimiento_crear'), {
            'nombre': 'AD (Directorio Activo)',
            'unidades_negocio': [self.sg.pk],
            'tipo_objetivo': TipoObjetivoCumplimiento.ESTACIONES,
            'fecha_limite': '2026-10-30',
        })
        actividad = ActividadCumplimiento.objects.get(nombre='AD (Directorio Activo)')
        self.assertRedirects(resp, reverse('panel:cumplimiento_detalle', args=[actividad.pk]))
        self.assertTrue(
            ResultadoCumplimientoEstacion.objects.filter(actividad=actividad, estacion=self.estacion).exists(),
        )
        self.assertTrue(EventoAuditoria.objects.filter(accion='cumplimiento.crear').exists())

    def test_detalle_muestra_objetivo_y_marcar_completado(self):
        actividad = ActividadCumplimiento.objects.create(
            nombre='AD', tipo_objetivo=TipoObjetivoCumplimiento.ESTACIONES,
            fecha_limite=date(2026, 10, 30), creado_por=self.usuario,
        )
        actividad.unidades_negocio.add(self.sg)
        resultado = ResultadoCumplimientoEstacion.objects.create(actividad=actividad, estacion=self.estacion)

        resp = self.client.get(reverse('panel:cumplimiento_detalle', args=[actividad.pk]))
        self.assertContains(resp, 'ML001-A')

        resp = self.client.post(
            reverse('panel:cumplimiento_resultado_completar', args=[actividad.pk, resultado.pk]),
        )
        self.assertRedirects(resp, reverse('panel:cumplimiento_detalle', args=[actividad.pk]))
        resultado.refresh_from_db()
        self.assertEqual(resultado.estado, 'completado')


class MantenimientoCrearViewTests(TestCase):
    def setUp(self):
        self.usuario = User.objects.create_user(username='u', password='x')
        self.usuario.user_permissions.add(
            Permission.objects.get(content_type__app_label='mantenimiento', codename='add_mantenimiento'),
            Permission.objects.get(content_type__app_label='mantenimiento', codename='view_mantenimiento'),
        )
        self.client.force_login(self.usuario)
        self.cliente = Colaborador.objects.create(nombre='Ana', cedula='9999')
        self.equipo = Activo.objects.create(
            codigo='CR-DSK-0099', tipo=Activo.Tipo.DESKTOP, colaborador_actual=self.cliente,
        )
        self.tipo_preventivo = TipoMantenimiento.objects.get(codigo='preventivo')

    def _post_valido(self):
        return self.client.post(reverse('panel:mantenimiento_crear'), {
            'equipos': [self.equipo.pk],
            'cliente': self.cliente.pk,
            'tipo_mantenimiento': self.tipo_preventivo.pk,
            'estado_general': EstadoGeneralEquipo.OPERATIVO,
            'prioridad': PrioridadMantenimiento.NORMAL,
            'descripcion': 'Revisión de rutina',
            'fecha_programada': '2026-10-01T09:00',
        })

    def test_crea_y_redirige(self):
        resp = self._post_valido()
        mantenimiento = Mantenimiento.objects.get(descripcion='Revisión de rutina')
        self.assertRedirects(resp, reverse('panel:mantenimiento_detalle', args=[mantenimiento.pk]))

    def test_rechaza_segundo_mantenimiento_para_el_mismo_equipo_sin_500(self):
        self._post_valido()
        resp = self._post_valido()
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'Ya hay un mantenimiento abierto')

    def test_partial_equipos_por_cliente_devuelve_solo_los_del_cliente(self):
        otro_cliente = Colaborador.objects.create(nombre='Luis', cedula='9998')
        Activo.objects.create(codigo='CR-DSK-0098', tipo=Activo.Tipo.DESKTOP, colaborador_actual=otro_cliente)
        resp = self.client.get(
            reverse('panel:equipos_por_cliente_partial'), {'cliente': self.cliente.pk},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'CR-DSK-0099')
        self.assertNotContains(resp, 'CR-DSK-0098')

    def test_partial_sin_criterio_no_devuelve_equipos(self):
        # Sin busqueda ni custodio no se listan cientos de equipos: el <select>
        # quedaria inmanejable. El texto explica que hay que buscar.
        resp = self.client.get(reverse('panel:equipos_por_cliente_partial'))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'Busca por codigo')

    def test_partial_de_cliente_de_otro_tenant_da_403(self):
        from apps.catalogo.models import UnidadNegocio
        from apps.cuentas.models import PerfilUsuario

        sg = UnidadNegocio.objects.get(codigo='SG')
        mia = UnidadNegocio.objects.get(codigo='MIA')
        PerfilUsuario.objects.create(usuario=self.usuario).unidades_negocio.add(mia)
        cliente_sg = Colaborador.objects.create(nombre='Otro', cedula='9997', unidad_negocio=sg)
        resp = self.client.get(reverse('panel:equipos_por_cliente_partial'), {'cliente': cliente_sg.pk})
        self.assertEqual(resp.status_code, 403)


class MantenimientoApiCrearTests(TestCase):
    def setUp(self):
        from rest_framework.authtoken.models import Token
        self.tecnico = User.objects.create_user(username='tec', password='x')
        self.token = Token.objects.create(user=self.tecnico)
        self.equipo = Activo.objects.create(codigo='CR-DSK-0100', tipo=Activo.Tipo.DESKTOP)
        self.cliente = Colaborador.objects.create(nombre='Beto', cedula='8888')
        self.tipo_correctivo = TipoMantenimiento.objects.get(codigo='correctivo')

    def _payload(self):
        return {
            'equipos': [self.equipo.pk],
            'cliente': self.cliente.pk,
            'tipo_mantenimiento': self.tipo_correctivo.pk,
            'estado_general': EstadoGeneralEquipo.NO_OPERATIVO,
            'descripcion': 'Falla de pantalla',
            'fecha_programada': '2026-10-01T09:00:00Z',
        }

    def test_crear_sin_token_devuelve_401(self):
        resp = self.client.post('/api/v1/mantenimientos/', self._payload())
        self.assertEqual(resp.status_code, 401)

    def test_crear_autoasigna_al_tecnico_del_token(self):
        resp = self.client.post(
            '/api/v1/mantenimientos/', self._payload(),
            HTTP_AUTHORIZATION=f'Token {self.token.key}',
        )
        self.assertEqual(resp.status_code, 201)
        mantenimiento = Mantenimiento.objects.get(descripcion='Falla de pantalla')
        self.assertEqual(mantenimiento.tecnico, self.tecnico)
        self.assertEqual(mantenimiento.estado_general, EstadoGeneralEquipo.NO_OPERATIVO)

    def test_crear_rechaza_si_equipo_ya_tiene_uno_abierto(self):
        self.client.post('/api/v1/mantenimientos/', self._payload(), HTTP_AUTHORIZATION=f'Token {self.token.key}')
        resp = self.client.post(
            '/api/v1/mantenimientos/', self._payload(), HTTP_AUTHORIZATION=f'Token {self.token.key}',
        )
        self.assertEqual(resp.status_code, 400)


class MultiTenantAislamientoTests(TestCase):
    """R1: un usuario restringido a una unidad de negocio no debe ver ni poder
    accionar sobre datos de otra, ni siquiera forzando el ID por URL."""

    def setUp(self):
        self.sg = UnidadNegocio.objects.get(codigo='SG')
        self.mia = UnidadNegocio.objects.get(codigo='MIA')
        grupo = Grupo.objects.create(codigo='TRX001')
        self.farmacia_sg = Farmacia.objects.create(codigo='ML001', grupo=grupo, unidad_negocio=self.sg)
        self.farmacia_mia = Farmacia.objects.create(codigo='MAM01', grupo=grupo, unidad_negocio=self.mia)
        self.estacion_sg = Estacion.objects.create(
            codigo='ML001-A', farmacia=self.farmacia_sg, estado_aprobacion=Estacion.EstadoAprobacion.APROBADA,
        )
        self.estacion_mia = Estacion.objects.create(
            codigo='MAM01-A', farmacia=self.farmacia_mia, estado_aprobacion=Estacion.EstadoAprobacion.APROBADA,
        )

        self.usuario_sg = User.objects.create_user(username='user_sg', password='x')
        PerfilUsuario.objects.create(usuario=self.usuario_sg).unidades_negocio.add(self.sg)
        self.usuario_sg.user_permissions.add(
            Permission.objects.get(content_type__app_label='catalogo', codename='view_estacion'),
        )
        self.client.force_login(self.usuario_sg)

    def test_lista_de_estaciones_no_muestra_las_de_otro_tenant(self):
        resp = self.client.get(reverse('panel:estaciones_lista'))
        self.assertContains(resp, 'ML001-A')
        self.assertNotContains(resp, 'MAM01-A')

    def test_forzar_id_de_estacion_de_otro_tenant_devuelve_403(self):
        resp = self.client.get(reverse('panel:estacion_info_modal', args=[self.estacion_mia.pk]))
        self.assertEqual(resp.status_code, 403)

    def test_acceso_total_ve_ambos_tenants(self):
        soporte = User.objects.create_user(username='soporte', password='x', is_superuser=True, is_staff=True)
        self.client.force_login(soporte)
        resp = self.client.get(reverse('panel:estaciones_lista'))
        self.assertContains(resp, 'ML001-A')
        self.assertContains(resp, 'MAM01-A')


class DespliegueMultiTenantTests(TestCase):
    """Prueba el validar_destino_unidad_negocio de DespliegueForm de punta a punta
    (POST real al panel), no solo la función de servicio en aislamiento."""

    def setUp(self):
        self.sg = UnidadNegocio.objects.get(codigo='SG')
        self.mia = UnidadNegocio.objects.get(codigo='MIA')
        grupo = Grupo.objects.create(codigo='TRX001')
        self.farmacia_sg = Farmacia.objects.create(codigo='ML001', grupo=grupo, unidad_negocio=self.sg)
        self.farmacia_mia = Farmacia.objects.create(codigo='MAM01', grupo=grupo, unidad_negocio=self.mia)

        # acceso_todas_unidades=True: ambas farmacias son opciones válidas del campo,
        # así que si el envío se rechaza es por validar_destino_unidad_negocio (el
        # cruce unidad_negocio vs. farmacias elegidas), no por el recorte del queryset.
        self.usuario = User.objects.create_user(username='soporte2', password='x')
        PerfilUsuario.objects.create(usuario=self.usuario, acceso_todas_unidades=True)
        self.usuario.user_permissions.add(
            Permission.objects.get(content_type__app_label='despliegues', codename='add_despliegue'),
            Permission.objects.get(content_type__app_label='despliegues', codename='view_despliegue'),
        )
        self.client.force_login(self.usuario)

    def test_crear_despliegue_rechaza_farmacia_de_otra_unidad_negocio(self):
        archivo = SimpleUploadedFile('pkg.zip', b'contenido-falso')
        resp = self.client.post(reverse('panel:despliegue_crear'), {
            'unidad_negocio': self.sg.pk,
            'version': '1.0.0',
            'archivo': archivo,
            'modo_aplicacion': Despliegue.ModoAplicacion.INMEDIATO,
            'destino_tipo': Despliegue.DestinoTipo.FARMACIAS,
            'farmacias': [self.farmacia_mia.pk],
            'umbral_error_pct': '10',
        })
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(Despliegue.objects.filter(version='1.0.0').exists())

    def test_crear_despliegue_con_farmacia_del_mismo_tenant_funciona(self):
        archivo = SimpleUploadedFile('pkg.zip', b'contenido-falso')
        resp = self.client.post(reverse('panel:despliegue_crear'), {
            'unidad_negocio': self.sg.pk,
            'version': '1.0.0',
            'archivo': archivo,
            'modo_aplicacion': Despliegue.ModoAplicacion.INMEDIATO,
            'destino_tipo': Despliegue.DestinoTipo.FARMACIAS,
            'farmacias': [self.farmacia_sg.pk],
            'umbral_error_pct': '10',
        })
        despliegue = Despliegue.objects.get(version='1.0.0')
        self.assertRedirects(resp, reverse('panel:despliegue_detalle', args=[despliegue.pk]))


class DespliegueAprobarPermisoTests(TestCase):
    """AC-2 de la auditoría de gobernanza (22-ago-2026): la regla de cuatro ojos
    verificaba que el aprobador no fuera el autor, pero no exigía ningún permiso —
    cualquier segundo usuario autenticado contaba como "los cuatro ojos". Ahora hace
    falta el permiso despliegues.aprobar_despliegue, además de no ser el creador."""

    def setUp(self):
        self.sg = UnidadNegocio.objects.get(codigo='SG')
        creador = User.objects.create_user(username='creador_desp', password='x')
        PerfilUsuario.objects.create(usuario=creador, acceso_todas_unidades=True)
        self.despliegue = Despliegue.objects.create(
            version='1.0.0', archivo=SimpleUploadedFile('pkg.zip', b'contenido'),
            unidad_negocio=self.sg, destino_tipo=Despliegue.DestinoTipo.CADENA,
            modo_aplicacion=Despliegue.ModoAplicacion.INMEDIATO,
            estado=Despliegue.Estado.PENDIENTE_APROBACION, creado_por=creador,
        )

        self.usuario_sin_permiso = User.objects.create_user(username='sin_permiso', password='x')
        PerfilUsuario.objects.create(usuario=self.usuario_sin_permiso, acceso_todas_unidades=True)

        self.usuario_con_permiso = User.objects.create_user(username='con_permiso', password='x')
        PerfilUsuario.objects.create(usuario=self.usuario_con_permiso, acceso_todas_unidades=True)
        self.usuario_con_permiso.user_permissions.add(
            Permission.objects.get(content_type__app_label='despliegues', codename='aprobar_despliegue'),
            Permission.objects.get(content_type__app_label='despliegues', codename='view_despliegue'),
        )

    def test_usuario_sin_el_permiso_no_puede_aprobar(self):
        self.client.force_login(self.usuario_sin_permiso)
        resp = self.client.post(reverse('panel:despliegue_aprobar', args=[self.despliegue.pk]))
        self.assertEqual(resp.status_code, 403)
        self.despliegue.refresh_from_db()
        self.assertEqual(self.despliegue.estado, Despliegue.Estado.PENDIENTE_APROBACION)

    def test_usuario_con_el_permiso_puede_aprobar(self):
        self.client.force_login(self.usuario_con_permiso)
        resp = self.client.post(reverse('panel:despliegue_aprobar', args=[self.despliegue.pk]))
        self.assertRedirects(resp, reverse('panel:despliegue_detalle', args=[self.despliegue.pk]))
        self.despliegue.refresh_from_db()
        self.assertEqual(self.despliegue.estado, Despliegue.Estado.APROBADO)

    def test_el_creador_con_el_permiso_igual_no_puede_aprobar_el_propio(self):
        creador = self.despliegue.creado_por
        creador.user_permissions.add(
            Permission.objects.get(content_type__app_label='despliegues', codename='aprobar_despliegue'),
            Permission.objects.get(content_type__app_label='despliegues', codename='view_despliegue'),
        )
        self.client.force_login(creador)
        resp = self.client.post(reverse('panel:despliegue_aprobar', args=[self.despliegue.pk]))
        self.assertRedirects(resp, reverse('panel:despliegue_detalle', args=[self.despliegue.pk]))
        self.despliegue.refresh_from_db()
        self.assertEqual(self.despliegue.estado, Despliegue.Estado.PENDIENTE_APROBACION)


class EjecucionScriptAprobarPermisoTests(TestCase):
    """AC-3 de la auditoría de gobernanza (22-ago-2026): ejecutar un script contra toda
    la cadena/grupos/farmacias no tenía ninguna aprobación de un segundo usuario, a
    diferencia de un Despliegue al mismo destino. Mismo patrón que DespliegueAprobarPermisoTests:
    permiso scripts.aprobar_ejecucionscript + regla de cuatro ojos (creador != aprobador)."""

    def setUp(self):
        self.sg = UnidadNegocio.objects.get(codigo='SG')
        self.creador = User.objects.create_user(username='creador_ejec', password='x')
        PerfilUsuario.objects.create(usuario=self.creador, acceso_todas_unidades=True)
        script = Script.objects.create(
            nombre='Actualizar winget', tipo=TipoScript.POWERSHELL, contenido='winget upgrade --all',
            creado_por=self.creador,
        )
        self.ejecucion = EjecucionScript.objects.create(
            script=script, contenido_snapshot=script.contenido, destino_tipo=EjecucionScript.DestinoTipo.CADENA,
            unidad_negocio=self.sg, estado=EjecucionScript.Estado.PENDIENTE_APROBACION, creado_por=self.creador,
        )

        self.usuario_sin_permiso = User.objects.create_user(username='sin_permiso_ejec', password='x')
        PerfilUsuario.objects.create(usuario=self.usuario_sin_permiso, acceso_todas_unidades=True)

        self.usuario_con_permiso = User.objects.create_user(username='con_permiso_ejec', password='x')
        PerfilUsuario.objects.create(usuario=self.usuario_con_permiso, acceso_todas_unidades=True)
        self.usuario_con_permiso.user_permissions.add(
            Permission.objects.get(content_type__app_label='scripts', codename='aprobar_ejecucionscript'),
            Permission.objects.get(content_type__app_label='scripts', codename='view_ejecucionscript'),
        )

    def test_usuario_sin_el_permiso_no_puede_aprobar(self):
        self.client.force_login(self.usuario_sin_permiso)
        resp = self.client.post(reverse('panel:ejecucion_aprobar', args=[self.ejecucion.pk]))
        self.assertEqual(resp.status_code, 403)
        self.ejecucion.refresh_from_db()
        self.assertEqual(self.ejecucion.estado, EjecucionScript.Estado.PENDIENTE_APROBACION)

    def test_usuario_con_el_permiso_puede_aprobar_y_publica_la_ejecucion(self):
        self.client.force_login(self.usuario_con_permiso)
        resp = self.client.post(reverse('panel:ejecucion_aprobar', args=[self.ejecucion.pk]))
        self.assertRedirects(resp, reverse('panel:ejecucion_detalle', args=[self.ejecucion.pk]))
        self.ejecucion.refresh_from_db()
        self.assertNotEqual(self.ejecucion.estado, EjecucionScript.Estado.PENDIENTE_APROBACION)
        self.assertEqual(self.ejecucion.aprobado_por, self.usuario_con_permiso)

    def test_el_creador_con_el_permiso_igual_no_puede_aprobar_la_propia(self):
        self.creador.user_permissions.add(
            Permission.objects.get(content_type__app_label='scripts', codename='aprobar_ejecucionscript'),
            Permission.objects.get(content_type__app_label='scripts', codename='view_ejecucionscript'),
        )
        self.client.force_login(self.creador)
        resp = self.client.post(reverse('panel:ejecucion_aprobar', args=[self.ejecucion.pk]))
        self.assertRedirects(resp, reverse('panel:ejecucion_detalle', args=[self.ejecucion.pk]))
        self.ejecucion.refresh_from_db()
        self.assertEqual(self.ejecucion.estado, EjecucionScript.Estado.PENDIENTE_APROBACION)


class DespliegueVistasPermisoTests(TestCase):
    """AC-1 de la auditoría de gobernanza (22-ago-2026): 9 de las 9 vistas de
    despliegues.py no pedían ningún permiso, solo sesión iniciada. Cubre que un
    usuario sin rol quede afuera de las acciones básicas (listar/crear/publicar)."""

    def setUp(self):
        self.sg = UnidadNegocio.objects.get(codigo='SG')
        self.usuario = User.objects.create_user(username='sin_rol_desp', password='x')
        PerfilUsuario.objects.create(usuario=self.usuario, acceso_todas_unidades=True)
        otro = User.objects.create_user(username='otro_creador_desp', password='x')
        self.despliegue = Despliegue.objects.create(
            version='1.0.0', archivo=SimpleUploadedFile('pkg.zip', b'contenido'),
            unidad_negocio=self.sg, destino_tipo=Despliegue.DestinoTipo.CADENA,
            modo_aplicacion=Despliegue.ModoAplicacion.INMEDIATO, estado=Despliegue.Estado.APROBADO,
            creado_por=otro,
        )
        self.client.force_login(self.usuario)

    def test_lista_sin_permiso_devuelve_403(self):
        self.assertEqual(self.client.get(reverse('panel:despliegues_lista')).status_code, 403)

    def test_crear_sin_permiso_devuelve_403(self):
        self.assertEqual(self.client.get(reverse('panel:despliegue_crear')).status_code, 403)

    def test_detalle_sin_permiso_devuelve_403(self):
        resp = self.client.get(reverse('panel:despliegue_detalle', args=[self.despliegue.pk]))
        self.assertEqual(resp.status_code, 403)

    def test_publicar_sin_permiso_devuelve_403(self):
        resp = self.client.post(reverse('panel:despliegue_publicar', args=[self.despliegue.pk]))
        self.assertEqual(resp.status_code, 403)
        self.despliegue.refresh_from_db()
        self.assertEqual(self.despliegue.estado, Despliegue.Estado.APROBADO)


class AlertaMultiTenantTests(TestCase):
    def setUp(self):
        self.sg = UnidadNegocio.objects.get(codigo='SG')
        self.mia = UnidadNegocio.objects.get(codigo='MIA')
        grupo = Grupo.objects.create(codigo='TRX001')
        farmacia_sg = Farmacia.objects.create(codigo='ML001', grupo=grupo, unidad_negocio=self.sg)
        self.estacion_sg = Estacion.objects.create(
            codigo='ML001-A', farmacia=farmacia_sg, estado_aprobacion=Estacion.EstadoAprobacion.APROBADA,
        )
        creador = User.objects.create_user(username='creador_regla', password='x')
        self.regla_sg = ReglaAlerta.objects.create(
            nombre='Privada SG', metrica=Metrica.CPU_CARGA_PCT, umbral=90,
            unidad_negocio=self.sg, creado_por=creador,
        )
        self.alerta = Alerta.objects.create(regla=self.regla_sg, estacion=self.estacion_sg, valor_disparador=95)

        self.usuario_mia = User.objects.create_user(username='user_mia', password='x')
        PerfilUsuario.objects.create(usuario=self.usuario_mia).unidades_negocio.add(self.mia)
        self.usuario_mia.user_permissions.add(
            Permission.objects.get(content_type__app_label='monitoreo', codename='view_alerta'),
        )
        self.client.force_login(self.usuario_mia)

    def test_usuario_de_otro_tenant_no_ve_la_alerta_en_el_listado(self):
        resp = self.client.get(reverse('panel:alertas_lista'))
        self.assertNotContains(resp, 'Privada SG')

    def test_usuario_de_otro_tenant_no_puede_resolverla(self):
        resp = self.client.post(reverse('panel:alerta_resolver', args=[self.alerta.pk]))
        self.assertEqual(resp.status_code, 403)
        self.alerta.refresh_from_db()
        self.assertEqual(self.alerta.estado, Alerta.Estado.ABIERTA)


class AlertasAgrupadasTests(TestCase):
    """Rollup por regla (M1a): 'cuántas estaciones tienen esta alerta activa ahora' en
    vez de una fila por estación — ver apps.panel.views.alertas.alertas_lista."""

    def setUp(self):
        self.sg = UnidadNegocio.objects.get(codigo='SG')
        grupo = Grupo.objects.create(codigo='TRX001')
        farmacia = Farmacia.objects.create(codigo='ML001', grupo=grupo, unidad_negocio=self.sg)
        self.estacion_a = Estacion.objects.create(
            codigo='ML001-A', farmacia=farmacia, estado_aprobacion=Estacion.EstadoAprobacion.APROBADA,
        )
        self.estacion_b = Estacion.objects.create(
            codigo='ML001-B', farmacia=farmacia, estado_aprobacion=Estacion.EstadoAprobacion.APROBADA,
        )
        self.usuario = User.objects.create_user(username='u_agrup', password='x')
        PerfilUsuario.objects.create(usuario=self.usuario, acceso_todas_unidades=True)
        self.usuario.user_permissions.add(
            Permission.objects.get(content_type__app_label='monitoreo', codename='view_alerta'),
        )
        self.client.force_login(self.usuario)
        self.regla = ReglaAlerta.objects.create(
            nombre='CPU alta', metrica=Metrica.CPU_CARGA_PCT, umbral=90, creado_por=self.usuario,
        )

    def test_agrupa_por_regla_contando_estaciones_distintas(self):
        Alerta.objects.create(regla=self.regla, estacion=self.estacion_a, valor_disparador=95)
        Alerta.objects.create(regla=self.regla, estacion=self.estacion_b, valor_disparador=96)
        resp = self.client.get(reverse('panel:alertas_lista'), {'vista': 'agrupada'})
        self.assertEqual(resp.status_code, 200)
        agrupadas = resp.context['agrupadas']
        self.assertEqual(len(agrupadas), 1)
        self.assertEqual(agrupadas[0]['regla_id'], self.regla.pk)
        self.assertEqual(agrupadas[0]['n_estaciones'], 2)

    def test_dos_alertas_de_la_misma_estacion_cuentan_una_sola_vez(self):
        Alerta.objects.create(regla=self.regla, estacion=self.estacion_a, valor_disparador=95)
        # Una segunda alerta "resuelta" no debería sumar una estación extra ni duplicar.
        Alerta.objects.create(
            regla=self.regla, estacion=self.estacion_a, valor_disparador=91, estado=Alerta.Estado.RESUELTA,
        )
        resp = self.client.get(reverse('panel:alertas_lista'), {'vista': 'agrupada'})
        self.assertEqual(resp.context['agrupadas'][0]['n_estaciones'], 1)

    def test_alertas_resueltas_no_aparecen_en_el_rollup(self):
        Alerta.objects.create(
            regla=self.regla, estacion=self.estacion_a, valor_disparador=95, estado=Alerta.Estado.RESUELTA,
        )
        resp = self.client.get(reverse('panel:alertas_lista'), {'vista': 'agrupada'})
        self.assertEqual(list(resp.context['agrupadas']), [])
        self.assertContains(resp, 'Sin alertas activas.')

    def test_no_mezcla_estaciones_de_otro_tenant_en_el_conteo(self):
        mia = UnidadNegocio.objects.get(codigo='MIA')
        grupo2 = Grupo.objects.create(codigo='TRX002')
        farmacia_mia = Farmacia.objects.create(codigo='MAM01', grupo=grupo2, unidad_negocio=mia)
        estacion_mia = Estacion.objects.create(
            codigo='MAM01-A', farmacia=farmacia_mia, estado_aprobacion=Estacion.EstadoAprobacion.APROBADA,
        )
        Alerta.objects.create(regla=self.regla, estacion=self.estacion_a, valor_disparador=95)
        Alerta.objects.create(regla=self.regla, estacion=estacion_mia, valor_disparador=95)

        usuario_sg_only = User.objects.create_user(username='u_sg_only', password='x')
        PerfilUsuario.objects.create(usuario=usuario_sg_only).unidades_negocio.add(self.sg)
        usuario_sg_only.user_permissions.add(
            Permission.objects.get(content_type__app_label='monitoreo', codename='view_alerta'),
        )
        self.client.force_login(usuario_sg_only)

        resp = self.client.get(reverse('panel:alertas_lista'), {'vista': 'agrupada'})
        self.assertEqual(resp.context['agrupadas'][0]['n_estaciones'], 1)

    def test_link_de_regla_normal_apunta_a_la_lista_filtrada(self):
        Alerta.objects.create(regla=self.regla, estacion=self.estacion_a, valor_disparador=95)
        resp = self.client.get(reverse('panel:alertas_lista'), {'vista': 'agrupada'})
        self.assertContains(resp, f'?regla={self.regla.pk}')

    def test_regla_pos_errores_linkea_al_rollup_por_mensaje(self):
        regla_pos = ReglaAlerta.objects.create(
            nombre='Errores del POS', metrica=Metrica.POS_ERRORES, umbral=1, creado_por=self.usuario,
        )
        Alerta.objects.create(regla=regla_pos, estacion=self.estacion_a, valor_disparador=3)
        resp = self.client.get(reverse('panel:alertas_lista'), {'vista': 'agrupada'})
        self.assertContains(resp, reverse('panel:pos_errores_flota'))

    def test_filtro_por_regla_en_vista_plana(self):
        otra_regla = ReglaAlerta.objects.create(
            nombre='RAM alta', metrica=Metrica.RAM_USADA_PCT, umbral=90, creado_por=self.usuario,
        )
        Alerta.objects.create(regla=self.regla, estacion=self.estacion_a, valor_disparador=95)
        Alerta.objects.create(regla=otra_regla, estacion=self.estacion_b, valor_disparador=95)
        resp = self.client.get(reverse('panel:alertas_lista'), {'regla': self.regla.pk})
        self.assertContains(resp, 'CPU alta')
        self.assertNotContains(resp, 'RAM alta')


class PosErroresFlotaTests(TestCase):
    """Rollup por mensaje exacto (M1b): distingue qué error puntual afecta a cuántas
    estaciones, algo que agrupar solo por regla no puede responder — ver
    apps.panel.views.alertas.pos_errores_flota."""

    def setUp(self):
        self.sg = UnidadNegocio.objects.get(codigo='SG')
        grupo = Grupo.objects.create(codigo='TRX001')
        farmacia = Farmacia.objects.create(codigo='ML001', grupo=grupo, unidad_negocio=self.sg)
        self.estacion_a = Estacion.objects.create(
            codigo='ML001-A', farmacia=farmacia, estado_aprobacion=Estacion.EstadoAprobacion.APROBADA,
        )
        self.estacion_b = Estacion.objects.create(
            codigo='ML001-B', farmacia=farmacia, estado_aprobacion=Estacion.EstadoAprobacion.APROBADA,
        )
        usuario = User.objects.create_user(username='u_pos_flota', password='x')
        PerfilUsuario.objects.create(usuario=usuario, acceso_todas_unidades=True)
        usuario.user_permissions.add(
            Permission.objects.get(content_type__app_label='monitoreo', codename='view_poserrordetectado'),
        )
        self.client.force_login(usuario)

    def test_agrupa_por_mensaje_exacto_entre_estaciones(self):
        PosErrorDetectado.objects.create(
            estacion=self.estacion_a, mensaje='no existe la relación X', cantidad_total=40,
        )
        PosErrorDetectado.objects.create(
            estacion=self.estacion_b, mensaje='no existe la relación X', cantidad_total=2,
        )
        resp = self.client.get(reverse('panel:pos_errores_flota'))
        filas = resp.context['filas']
        self.assertEqual(len(filas), 1)
        self.assertEqual(filas[0]['n_estaciones'], 2)
        self.assertEqual(filas[0]['total'], 42)

    def test_categoria_negocio_no_aparece_en_el_rollup(self):
        PosErrorDetectado.objects.create(
            estacion=self.estacion_a, mensaje='VENTA SIN LOTE: x',
            categoria=PosErrorDetectado.Categoria.NEGOCIO, cantidad_total=100,
        )
        resp = self.client.get(reverse('panel:pos_errores_flota'))
        self.assertEqual(list(resp.context['filas']), [])

    def test_ordenado_por_mas_estaciones_afectadas_primero(self):
        PosErrorDetectado.objects.create(estacion=self.estacion_a, mensaje='error raro', cantidad_total=1)
        PosErrorDetectado.objects.create(estacion=self.estacion_a, mensaje='error comun', cantidad_total=1)
        PosErrorDetectado.objects.create(estacion=self.estacion_b, mensaje='error comun', cantidad_total=1)
        resp = self.client.get(reverse('panel:pos_errores_flota'))
        mensajes = [f['mensaje'] for f in resp.context['filas']]
        self.assertEqual(mensajes[0], 'error comun')

    def test_filtro_por_texto(self):
        PosErrorDetectado.objects.create(estacion=self.estacion_a, mensaje='timeout de conexión', cantidad_total=1)
        PosErrorDetectado.objects.create(estacion=self.estacion_a, mensaje='columna faltante', cantidad_total=1)
        resp = self.client.get(reverse('panel:pos_errores_flota'), {'q': 'timeout'})
        mensajes = [f['mensaje'] for f in resp.context['filas']]
        self.assertEqual(mensajes, ['timeout de conexión'])

    def test_no_mezcla_estaciones_de_otro_tenant(self):
        mia = UnidadNegocio.objects.get(codigo='MIA')
        grupo2 = Grupo.objects.create(codigo='TRX002')
        farmacia_mia = Farmacia.objects.create(codigo='MAM01', grupo=grupo2, unidad_negocio=mia)
        estacion_mia = Estacion.objects.create(
            codigo='MAM01-A', farmacia=farmacia_mia, estado_aprobacion=Estacion.EstadoAprobacion.APROBADA,
        )
        PosErrorDetectado.objects.create(estacion=self.estacion_a, mensaje='error compartido', cantidad_total=1)
        PosErrorDetectado.objects.create(estacion=estacion_mia, mensaje='error compartido', cantidad_total=1)

        usuario_sg_only = User.objects.create_user(username='u_sg_only_pos', password='x')
        PerfilUsuario.objects.create(usuario=usuario_sg_only).unidades_negocio.add(self.sg)
        usuario_sg_only.user_permissions.add(
            Permission.objects.get(content_type__app_label='monitoreo', codename='view_poserrordetectado'),
        )
        self.client.force_login(usuario_sg_only)

        resp = self.client.get(reverse('panel:pos_errores_flota'))
        self.assertEqual(resp.context['filas'][0]['n_estaciones'], 1)


class VentanaMantenimientoPanelTests(TestCase):
    """CRUD del panel (M2) + el aviso "en mantenimiento hasta" en el modal de la
    estación (mismo patrón de destino/permiso que ScriptProgramado)."""

    def setUp(self):
        self.sg = UnidadNegocio.objects.get(codigo='SG')
        self.mia = UnidadNegocio.objects.get(codigo='MIA')
        grupo = Grupo.objects.create(codigo='TRX001')
        self.farmacia_sg = Farmacia.objects.create(codigo='ML001', grupo=grupo, unidad_negocio=self.sg)
        self.farmacia_mia = Farmacia.objects.create(codigo='MAM01', grupo=grupo, unidad_negocio=self.mia)
        self.estacion_sg = Estacion.objects.create(
            codigo='ML001-A', farmacia=self.farmacia_sg, estado_aprobacion=Estacion.EstadoAprobacion.APROBADA,
        )
        creador = User.objects.create_user(username='creador_ventana', password='x')
        self.ventana_sg = VentanaMantenimiento.objects.create(
            unidad_negocio=self.sg, destino_tipo=VentanaMantenimiento.DestinoTipo.CADENA,
            desde=timezone.now() - timedelta(hours=1), hasta=timezone.now() + timedelta(hours=1),
            motivo='Ventana privada SG', creado_por=creador,
        )

        self.usuario_mia = User.objects.create_user(username='user_mia_vm', password='x')
        PerfilUsuario.objects.create(usuario=self.usuario_mia).unidades_negocio.add(self.mia)
        self.usuario_mia.user_permissions.add(
            Permission.objects.get(content_type__app_label='monitoreo', codename='view_ventanamantenimiento'),
        )

    def test_usuario_de_otro_tenant_no_ve_la_ventana(self):
        self.client.force_login(self.usuario_mia)
        resp = self.client.get(reverse('panel:ventanas_mantenimiento_lista'))
        self.assertEqual(resp.status_code, 200)
        self.assertNotContains(resp, 'Ventana privada SG')

    def test_sin_permiso_no_puede_crear(self):
        self.client.force_login(self.usuario_mia)
        self.assertEqual(self.client.get(reverse('panel:ventana_mantenimiento_crear')).status_code, 403)

    def test_operador_rmm_puede_crear(self):
        operador = User.objects.create_user(username='operador_rmm_vm', password='x')
        PerfilUsuario.objects.create(usuario=operador, acceso_todas_unidades=True)
        operador.user_permissions.add(
            Permission.objects.get(content_type__app_label='monitoreo', codename='add_ventanamantenimiento'),
            Permission.objects.get(content_type__app_label='monitoreo', codename='view_ventanamantenimiento'),
        )
        self.client.force_login(operador)
        resp = self.client.post(reverse('panel:ventana_mantenimiento_crear'), {
            'unidad_negocio': self.sg.pk,
            'destino_tipo': VentanaMantenimiento.DestinoTipo.FARMACIAS,
            'farmacias': [self.farmacia_sg.pk],
            'desde': '2026-09-01T22:00',
            'hasta': '2026-09-02T02:00',
            'motivo': 'Reinicio masivo nocturno',
            'activo': 'on',
        })
        self.assertRedirects(resp, reverse('panel:ventanas_mantenimiento_lista'))
        self.assertTrue(VentanaMantenimiento.objects.filter(motivo='Reinicio masivo nocturno').exists())

    def test_rechaza_farmacia_de_otra_unidad_negocio(self):
        operador = User.objects.create_user(username='operador_rmm_vm2', password='x')
        PerfilUsuario.objects.create(usuario=operador, acceso_todas_unidades=True)
        operador.user_permissions.add(
            Permission.objects.get(content_type__app_label='monitoreo', codename='add_ventanamantenimiento'),
        )
        self.client.force_login(operador)
        resp = self.client.post(reverse('panel:ventana_mantenimiento_crear'), {
            'unidad_negocio': self.sg.pk,
            'destino_tipo': VentanaMantenimiento.DestinoTipo.FARMACIAS,
            'farmacias': [self.farmacia_mia.pk],
            'desde': '2026-09-01T22:00',
            'hasta': '2026-09-02T02:00',
            'motivo': 'No debería crearse',
            'activo': 'on',
        })
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(VentanaMantenimiento.objects.filter(motivo='No debería crearse').exists())

    def test_modal_de_estacion_muestra_aviso_de_mantenimiento(self):
        usuario_sg = User.objects.create_user(username='user_sg_vm', password='x')
        PerfilUsuario.objects.create(usuario=usuario_sg).unidades_negocio.add(self.sg)
        usuario_sg.user_permissions.add(
            Permission.objects.get(content_type__app_label='catalogo', codename='view_estacion'),
        )
        self.client.force_login(usuario_sg)
        resp = self.client.get(reverse('panel:estacion_info_modal', args=[self.estacion_sg.pk]))
        self.assertContains(resp, 'En mantenimiento hasta')

    def test_modal_de_estacion_sin_ventana_activa_no_muestra_aviso(self):
        self.ventana_sg.hasta = timezone.now() - timedelta(minutes=5)
        self.ventana_sg.save(update_fields=['hasta'])
        usuario_sg = User.objects.create_user(username='user_sg_vm2', password='x')
        PerfilUsuario.objects.create(usuario=usuario_sg).unidades_negocio.add(self.sg)
        usuario_sg.user_permissions.add(
            Permission.objects.get(content_type__app_label='catalogo', codename='view_estacion'),
        )
        self.client.force_login(usuario_sg)
        resp = self.client.get(reverse('panel:estacion_info_modal', args=[self.estacion_sg.pk]))
        self.assertNotContains(resp, 'En mantenimiento hasta')


class TendenciaFlotaTests(TestCase):
    """M5: series semanales a nivel de flota (alertas por severidad, recursos
    promedio) + top de errores del POS actual (sin tendencia — PosErrorDetectado no
    guarda cuándo ocurrió cada reporte, solo un contador de por vida, ver docstring de
    apps.panel.views.monitoreo.tendencia_flota)."""

    def setUp(self):
        self.sg = UnidadNegocio.objects.get(codigo='SG')
        self.mia = UnidadNegocio.objects.get(codigo='MIA')
        grupo = Grupo.objects.create(codigo='TRX001')
        self.farmacia_sg = Farmacia.objects.create(codigo='ML001', grupo=grupo, unidad_negocio=self.sg)
        self.estacion = Estacion.objects.create(
            codigo='ML001-A', farmacia=self.farmacia_sg, estado_aprobacion=Estacion.EstadoAprobacion.APROBADA,
            monitorear_recursos=True,
        )
        self.usuario = User.objects.create_user(username='u_tendencia', password='x')
        PerfilUsuario.objects.create(usuario=self.usuario, acceso_todas_unidades=True)
        self.usuario.user_permissions.add(
            Permission.objects.get(content_type__app_label='monitoreo', codename='view_alerta'),
        )
        self.client.force_login(self.usuario)

    def test_cuenta_alertas_abiertas_de_esta_semana_por_severidad(self):
        regla_warning = ReglaAlerta.objects.create(
            nombre='CPU alta', metrica=Metrica.CPU_CARGA_PCT, umbral=90,
            severidad=ReglaAlerta.Severidad.WARNING, creado_por=self.usuario,
        )
        regla_critical = ReglaAlerta.objects.create(
            nombre='Disco lleno', metrica=Metrica.DISCO_USADO_PCT, umbral=95,
            severidad=ReglaAlerta.Severidad.CRITICAL, creado_por=self.usuario,
        )
        Alerta.objects.create(regla=regla_warning, estacion=self.estacion, valor_disparador=95)
        Alerta.objects.create(regla=regla_critical, estacion=self.estacion, valor_disparador=97)

        resp = self.client.get(reverse('panel:tendencia_flota'))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.context['g_abiertas_warning'].ultimo_valor, 1)
        self.assertEqual(resp.context['g_abiertas_critical'].ultimo_valor, 1)
        self.assertEqual(resp.context['total_abiertas_periodo'], 2)

    def test_alerta_vieja_sigue_en_el_periodo_pero_no_en_la_semana_actual(self):
        regla = ReglaAlerta.objects.create(
            nombre='CPU alta', metrica=Metrica.CPU_CARGA_PCT, umbral=90, creado_por=self.usuario,
        )
        alerta = Alerta.objects.create(regla=regla, estacion=self.estacion, valor_disparador=95)
        Alerta.objects.filter(pk=alerta.pk).update(abierta_en=timezone.now() - timedelta(weeks=3))

        resp = self.client.get(reverse('panel:tendencia_flota'))
        self.assertEqual(resp.context['g_abiertas_warning'].ultimo_valor, 0)
        self.assertEqual(resp.context['total_abiertas_periodo'], 1)

    def test_promedio_de_cpu_de_la_flota_esta_semana(self):
        MuestraMetrica.objects.create(estacion=self.estacion, cpu_carga_pct=80)
        MuestraMetrica.objects.create(estacion=self.estacion, cpu_carga_pct=60)
        resp = self.client.get(reverse('panel:tendencia_flota'))
        self.assertEqual(resp.context['g_cpu'].ultimo_valor, 70)

    def test_promedio_de_red_de_la_flota_esta_semana(self):
        MuestraMetrica.objects.create(estacion=self.estacion, red_recibido_kbps=1000, red_enviado_kbps=200)
        MuestraMetrica.objects.create(estacion=self.estacion, red_recibido_kbps=2000, red_enviado_kbps=400)
        resp = self.client.get(reverse('panel:tendencia_flota'))
        # promedio recibido=(1000+2000)/2=1500, enviado=(200+400)/2=300 -> total 1800
        self.assertEqual(resp.context['g_red'].ultimo_valor, 1800)

    def test_sin_muestras_esta_semana_no_muestra_el_promedio_de_una_semana_vieja(self):
        # Regresión: construir_grafico().ultimo_valor es el último valor NO NULO de la
        # serie, no necesariamente el de la semana actual — si la semana actual no
        # tiene muestras, no debe mostrarse el promedio de una semana vieja rotulado
        # como "Esta semana".
        vieja = MuestraMetrica.objects.create(estacion=self.estacion, cpu_carga_pct=55)
        MuestraMetrica.objects.filter(pk=vieja.pk).update(timestamp=timezone.now() - timedelta(weeks=3))

        resp = self.client.get(reverse('panel:tendencia_flota'))
        self.assertIsNone(resp.context['cpu_semana_actual'])
        self.assertEqual(resp.context['g_cpu'].ultimo_valor, 55)  # el gráfico sí sigue mostrando el dato viejo
        self.assertContains(resp, 'Sin datos esta semana')
        self.assertNotContains(resp, 'Esta semana: 55')

    def test_estacion_no_monitoreada_no_afecta_el_promedio(self):
        otra = Estacion.objects.create(
            codigo='ML001-B', farmacia=self.farmacia_sg, estado_aprobacion=Estacion.EstadoAprobacion.APROBADA,
            monitorear_recursos=False,
        )
        MuestraMetrica.objects.create(estacion=self.estacion, cpu_carga_pct=80)
        MuestraMetrica.objects.create(estacion=otra, cpu_carga_pct=0)
        resp = self.client.get(reverse('panel:tendencia_flota'))
        self.assertEqual(resp.context['g_cpu'].ultimo_valor, 80)

    def test_top_errores_del_pos_reusa_la_misma_agregacion_que_pos_errores_flota(self):
        PosErrorDetectado.objects.create(estacion=self.estacion, mensaje='no existe la relación X', cantidad_total=40)
        resp = self.client.get(reverse('panel:tendencia_flota'))
        top = resp.context['top_pos_errores']
        self.assertEqual(len(top), 1)
        self.assertEqual(top[0]['mensaje'], 'no existe la relación X')

    def test_no_mezcla_alertas_de_otro_tenant(self):
        grupo2 = Grupo.objects.create(codigo='TRX002')
        farmacia_mia = Farmacia.objects.create(codigo='MAM01', grupo=grupo2, unidad_negocio=self.mia)
        estacion_mia = Estacion.objects.create(
            codigo='MAM01-A', farmacia=farmacia_mia, estado_aprobacion=Estacion.EstadoAprobacion.APROBADA,
        )
        creador_mia = User.objects.create_user(username='creador_mia_tendencia', password='x')
        regla_mia = ReglaAlerta.objects.create(
            nombre='Privada MIA', metrica=Metrica.CPU_CARGA_PCT, umbral=90,
            unidad_negocio=self.mia, creado_por=creador_mia,
        )
        Alerta.objects.create(regla=regla_mia, estacion=estacion_mia, valor_disparador=95)

        usuario_sg_only = User.objects.create_user(username='u_sg_only_tendencia', password='x')
        PerfilUsuario.objects.create(usuario=usuario_sg_only).unidades_negocio.add(self.sg)
        usuario_sg_only.user_permissions.add(
            Permission.objects.get(content_type__app_label='monitoreo', codename='view_alerta'),
        )
        self.client.force_login(usuario_sg_only)

        resp = self.client.get(reverse('panel:tendencia_flota'))
        self.assertEqual(resp.context['total_abiertas_periodo'], 0)


class ScriptProgramadoMultiTenantTests(TestCase):
    def setUp(self):
        self.sg = UnidadNegocio.objects.get(codigo='SG')
        self.mia = UnidadNegocio.objects.get(codigo='MIA')
        creador = User.objects.create_user(username='creador_prog', password='x')
        self.script = Script.objects.create(
            nombre='Actualizar winget', tipo=TipoScript.POWERSHELL, contenido='winget upgrade --all',
            creado_por=creador,
        )
        self.programado_sg = ScriptProgramado.objects.create(
            script=self.script, unidad_negocio=self.sg, destino_tipo=EjecucionScript.DestinoTipo.CADENA,
            frecuencia_dias=7, fecha_proxima_ejecucion=date(2026, 12, 1), creado_por=creador,
        )

        self.usuario_mia = User.objects.create_user(username='user_mia2', password='x')
        PerfilUsuario.objects.create(usuario=self.usuario_mia).unidades_negocio.add(self.mia)
        self.usuario_mia.user_permissions.add(
            Permission.objects.get(content_type__app_label='scripts', codename='add_scriptprogramado'),
            Permission.objects.get(content_type__app_label='scripts', codename='view_scriptprogramado'),
        )
        self.client.force_login(self.usuario_mia)

    def test_usuario_de_otro_tenant_no_ve_la_programacion(self):
        resp = self.client.get(reverse('panel:scripts_programados_lista'))
        self.assertEqual(resp.status_code, 200)
        self.assertNotContains(resp, self.script.nombre)

    def test_form_no_ofrece_unidad_de_negocio_ajena(self):
        resp = self.client.get(reverse('panel:script_programado_crear'))
        self.assertNotContains(resp, '>SG</option>')
        self.assertContains(resp, '>MIA</option>')


class ScriptsRequierenPermisoDeOperadorRmmTests(TestCase):
    """Crear/ejecutar scripts (código arbitrario en la flota) es la superficie de riesgo
    que separa el rol "Mesa de Ayuda" del rol "Soporte Técnico" — mesa de ayuda no la
    tiene por defecto (ver seed_permisos.py)."""

    def setUp(self):
        self.sg = UnidadNegocio.objects.get(codigo='SG')
        creador = User.objects.create_user(username='creador_gate', password='x')
        self.script = Script.objects.create(
            nombre='Actualizar winget', tipo=TipoScript.POWERSHELL, contenido='winget upgrade --all',
            unidad_negocio=self.sg, creado_por=creador,
        )

        self.sin_permiso = User.objects.create_user(username='sin_permiso_scripts', password='x')
        PerfilUsuario.objects.create(usuario=self.sin_permiso, acceso_todas_unidades=True)

        self.operador = User.objects.create_user(username='operador_rmm', password='x')
        PerfilUsuario.objects.create(usuario=self.operador, acceso_todas_unidades=True)
        for app_label, codename in [
            ('scripts', 'add_script'), ('scripts', 'add_ejecucionscript'), ('scripts', 'add_scriptprogramado'),
        ]:
            self.operador.user_permissions.add(
                Permission.objects.get(content_type__app_label=app_label, codename=codename),
            )

    def test_sin_permiso_no_puede_crear_ni_ejecutar_scripts(self):
        self.client.force_login(self.sin_permiso)
        self.assertEqual(self.client.get(reverse('panel:script_crear')).status_code, 403)
        self.assertEqual(self.client.get(reverse('panel:script_ejecutar', args=[self.script.pk])).status_code, 403)
        self.assertEqual(self.client.get(reverse('panel:script_ejecutar_adhoc')).status_code, 403)
        self.assertEqual(self.client.get(reverse('panel:script_programado_crear')).status_code, 403)

    def test_operador_rmm_si_puede(self):
        self.client.force_login(self.operador)
        self.assertEqual(self.client.get(reverse('panel:script_crear')).status_code, 200)
        self.assertEqual(self.client.get(reverse('panel:script_ejecutar', args=[self.script.pk])).status_code, 200)
        self.assertEqual(self.client.get(reverse('panel:script_ejecutar_adhoc')).status_code, 200)
        self.assertEqual(self.client.get(reverse('panel:script_programado_crear')).status_code, 200)


class ActivosMultiTenantTests(TestCase):
    """RBAC fast-follow: activos/colaboradores heredan/declaran unidad_negocio pero
    hasta ahora nada escopaba las vistas del panel por ella."""

    def setUp(self):
        self.sg = UnidadNegocio.objects.get(codigo='SG')
        self.mia = UnidadNegocio.objects.get(codigo='MIA')
        self.colaborador_sg = Colaborador.objects.create(nombre='Ana SG', cedula='1001', unidad_negocio=self.sg)
        self.colaborador_compartido = Colaborador.objects.create(nombre='Beto RRHH', cedula='1002')
        self.activo_sg = Activo.objects.create(
            codigo='CR-DSK-0010', tipo=Activo.Tipo.DESKTOP, unidad_negocio=self.sg,
        )
        self.activo_compartido = Activo.objects.create(codigo='CR-DSK-0011', tipo=Activo.Tipo.DESKTOP)

        self.usuario_mia = User.objects.create_user(username='user_mia_activos', password='x')
        PerfilUsuario.objects.create(usuario=self.usuario_mia).unidades_negocio.add(self.mia)
        self.usuario_mia.user_permissions.add(
            Permission.objects.get(content_type__app_label='activos', codename='view_colaborador'),
            Permission.objects.get(content_type__app_label='activos', codename='view_activo'),
        )
        self.client.force_login(self.usuario_mia)

    def test_colaboradores_lista_oculta_los_de_otro_tenant_pero_muestra_compartidos(self):
        resp = self.client.get(reverse('panel:colaboradores_lista'))
        self.assertNotContains(resp, 'Ana SG')
        self.assertContains(resp, 'Beto RRHH')

    def test_activos_lista_oculta_los_de_otro_tenant_pero_muestra_compartidos(self):
        resp = self.client.get(reverse('panel:activos_lista'))
        self.assertNotContains(resp, 'CR-DSK-0010')
        self.assertContains(resp, 'CR-DSK-0011')

    def test_forzar_detalle_de_activo_de_otro_tenant_devuelve_403(self):
        resp = self.client.get(reverse('panel:activo_detalle', args=[self.activo_sg.pk]))
        self.assertEqual(resp.status_code, 403)

    def test_activo_compartido_es_accesible(self):
        resp = self.client.get(reverse('panel:activo_detalle', args=[self.activo_compartido.pk]))
        self.assertEqual(resp.status_code, 200)


class ActivosVistasPermisoTests(TestCase):
    """AC-1 de la auditoría de gobernanza (22-ago-2026): las 22 vistas de activos.py
    solo pedían sesión iniciada. Cubre que un usuario sin rol quede afuera."""

    def setUp(self):
        self.usuario = User.objects.create_user(username='sin_rol_act', password='x')
        PerfilUsuario.objects.create(usuario=self.usuario, acceso_todas_unidades=True)
        self.activo = Activo.objects.create(codigo='CR-DSK-0099', tipo=Activo.Tipo.DESKTOP)
        self.client.force_login(self.usuario)

    def test_lista_sin_permiso_devuelve_403(self):
        self.assertEqual(self.client.get(reverse('panel:activos_lista')).status_code, 403)

    def test_crear_sin_permiso_devuelve_403(self):
        self.assertEqual(self.client.get(reverse('panel:activo_crear')).status_code, 403)

    def test_detalle_sin_permiso_devuelve_403(self):
        resp = self.client.get(reverse('panel:activo_detalle', args=[self.activo.pk]))
        self.assertEqual(resp.status_code, 403)

    def test_baja_sin_permiso_devuelve_403(self):
        resp = self.client.post(reverse('panel:activo_baja', args=[self.activo.pk]))
        self.assertEqual(resp.status_code, 403)
        self.activo.refresh_from_db()
        self.assertNotEqual(self.activo.estado, Activo.Estado.DADO_DE_BAJA)


class ActivoUbicarFarmaciaViewTests(TestCase):
    """Cómo diferenciar un equipo de farmacia de uno administrativo para equipos sin
    agente RMM (impresoras, monitores) -- 23-ago-2026."""

    def setUp(self):
        grupo = Grupo.objects.create(codigo='TRX001', version_objetivo='4.2.1')
        self.sg = UnidadNegocio.objects.get(codigo='SG')
        self.farmacia = Farmacia.objects.create(codigo='ML001', grupo=grupo, unidad_negocio=self.sg)
        self.activo = Activo.objects.create(codigo='CR-IMP-0001', tipo=Activo.Tipo.IMPRESORA, unidad_negocio=self.sg)

        self.usuario = User.objects.create_user(username='u_ubicar_farmacia', password='x')
        PerfilUsuario.objects.create(usuario=self.usuario, acceso_todas_unidades=True)
        self.usuario.user_permissions.add(
            Permission.objects.get(content_type__app_label='activos', codename='change_activo'),
            Permission.objects.get(content_type__app_label='activos', codename='view_activo'),
        )
        self.client.force_login(self.usuario)

    def test_asigna_farmacia_y_redirige(self):
        resp = self.client.post(
            reverse('panel:activo_ubicar_farmacia', args=[self.activo.pk]), {'farmacia': self.farmacia.pk},
        )
        self.assertRedirects(resp, reverse('panel:activo_detalle', args=[self.activo.pk]))
        self.activo.refresh_from_db()
        self.assertEqual(self.activo.farmacia, self.farmacia)

    def test_dejar_vacio_lo_vuelve_administrativo(self):
        self.activo.farmacia = self.farmacia
        self.activo.save(update_fields=['farmacia'])
        self.client.post(reverse('panel:activo_ubicar_farmacia', args=[self.activo.pk]), {'farmacia': ''})
        self.activo.refresh_from_db()
        self.assertIsNone(self.activo.farmacia)

    def test_rechaza_si_esta_dado_de_baja(self):
        self.activo.estado = Activo.Estado.DADO_DE_BAJA
        self.activo.save(update_fields=['estado'])
        resp = self.client.post(
            reverse('panel:activo_ubicar_farmacia', args=[self.activo.pk]), {'farmacia': self.farmacia.pk},
        )
        self.assertRedirects(resp, reverse('panel:activo_detalle', args=[self.activo.pk]))
        self.activo.refresh_from_db()
        self.assertIsNone(self.activo.farmacia)

    def test_rechaza_si_tiene_estacion_rmm_vinculada(self):
        estacion = Estacion.objects.create(
            codigo='ML001-A', farmacia=self.farmacia, estado_aprobacion=Estacion.EstadoAprobacion.APROBADA,
        )
        self.activo.estacion = estacion
        self.activo.save(update_fields=['estacion'])
        resp = self.client.get(reverse('panel:activo_ubicar_farmacia', args=[self.activo.pk]))
        self.assertRedirects(resp, reverse('panel:activo_detalle', args=[self.activo.pk]))

    def test_lista_filtra_por_ubicacion(self):
        self.activo.farmacia = self.farmacia
        self.activo.save(update_fields=['farmacia'])
        administrativo = Activo.objects.create(
            codigo='CR-DSK-0055', tipo=Activo.Tipo.DESKTOP, unidad_negocio=self.sg,
        )
        resp = self.client.get(reverse('panel:activos_lista'), {'ubicacion': 'farmacia'})
        self.assertContains(resp, 'CR-IMP-0001')
        self.assertNotContains(resp, 'CR-DSK-0055')

        resp = self.client.get(reverse('panel:activos_lista'), {'ubicacion': 'administrativo'})
        self.assertContains(resp, 'CR-DSK-0055')
        self.assertNotContains(resp, 'CR-IMP-0001')


class BodegaAjusteStockViewTests(TestCase):
    """BUG-3 de la auditoría de gobernanza (22-ago-2026): botón nuevo para
    MovimientoInventario.TipoMovimiento.AJUSTE, mismo permiso que ingresar stock."""

    def setUp(self):
        self.bodega = Bodega.objects.create(codigo='BOD-A')
        self.tipo_consumible = TipoConsumible.objects.create(codigo='MOUSE', nombre='Mouse USB')
        StockBodega.objects.create(bodega=self.bodega, tipo_consumible=self.tipo_consumible, cantidad=10)

        self.sin_permiso = User.objects.create_user(username='sin_permiso_ajuste', password='x')
        PerfilUsuario.objects.create(usuario=self.sin_permiso, acceso_todas_unidades=True)

        self.con_permiso = User.objects.create_user(username='con_permiso_ajuste', password='x')
        PerfilUsuario.objects.create(usuario=self.con_permiso, acceso_todas_unidades=True)
        self.con_permiso.user_permissions.add(
            Permission.objects.get(content_type__app_label='activos', codename='change_stockbodega'),
            Permission.objects.get(content_type__app_label='activos', codename='view_bodega'),
        )

    def test_sin_permiso_devuelve_403(self):
        self.client.force_login(self.sin_permiso)
        resp = self.client.post(reverse('panel:bodega_ajuste_stock', args=[self.bodega.pk]), {
            'tipo_consumible': self.tipo_consumible.pk, 'cantidad_delta': -3, 'motivo': 'merma',
        })
        self.assertEqual(resp.status_code, 403)

    def test_con_permiso_registra_el_ajuste(self):
        self.client.force_login(self.con_permiso)
        resp = self.client.post(reverse('panel:bodega_ajuste_stock', args=[self.bodega.pk]), {
            'tipo_consumible': self.tipo_consumible.pk, 'cantidad_delta': -3, 'motivo': 'Merma en conteo físico',
        })
        self.assertRedirects(resp, reverse('panel:bodegas_lista'))
        self.assertEqual(StockBodega.objects.get(bodega=self.bodega, tipo_consumible=self.tipo_consumible).cantidad, 7)
        self.assertTrue(EventoAuditoria.objects.filter(accion='stock.ajustar').exists())

    def test_sin_motivo_no_registra_nada(self):
        self.client.force_login(self.con_permiso)
        resp = self.client.post(reverse('panel:bodega_ajuste_stock', args=[self.bodega.pk]), {
            'tipo_consumible': self.tipo_consumible.pk, 'cantidad_delta': -3, 'motivo': '',
        })
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(StockBodega.objects.get(bodega=self.bodega, tipo_consumible=self.tipo_consumible).cantidad, 10)


class RecepcionLoteAnularViewTests(TestCase):
    """BUG-3: revertir una recepción mal cargada desde el panel."""

    def setUp(self):
        self.bodega = Bodega.objects.create(codigo='BOD-A')
        self.tipo_consumible = TipoConsumible.objects.create(codigo='MOUSE', nombre='Mouse USB')
        self.oc = OrdenCompra.objects.create(numero_oc='OC-0001', proveedor='ACME', fecha_emision=date(2026, 1, 1))
        self.detalle = OrdenCompraDetalle.objects.create(
            orden_compra=self.oc, tipo_item=OrdenCompraDetalle.TipoItem.CONSUMIBLE,
            tipo_consumible=self.tipo_consumible, cantidad_solicitada=10,
        )
        from apps.activos.services import registrar_recepcion_lote
        self.recepcion = registrar_recepcion_lote(
            detalle=self.detalle, cantidad=6, bodega=self.bodega, usuario=None,
        )

        self.sin_permiso = User.objects.create_user(username='sin_permiso_anular', password='x')
        PerfilUsuario.objects.create(usuario=self.sin_permiso, acceso_todas_unidades=True)

        self.con_permiso = User.objects.create_user(username='con_permiso_anular', password='x')
        PerfilUsuario.objects.create(usuario=self.con_permiso, acceso_todas_unidades=True)
        self.con_permiso.user_permissions.add(
            Permission.objects.get(content_type__app_label='activos', codename='change_recepcionlote'),
            Permission.objects.get(content_type__app_label='activos', codename='view_ordencompra'),
        )

    def test_sin_permiso_devuelve_403(self):
        self.client.force_login(self.sin_permiso)
        resp = self.client.post(reverse('panel:recepcion_lote_anular', args=[self.recepcion.pk]))
        self.assertEqual(resp.status_code, 403)

    def test_con_permiso_anula_la_recepcion(self):
        self.client.force_login(self.con_permiso)
        resp = self.client.post(
            reverse('panel:recepcion_lote_anular', args=[self.recepcion.pk]), {'motivo': 'Cantidad mal cargada'},
        )
        self.assertRedirects(resp, reverse('panel:orden_compra_detalle', args=[self.oc.pk]))
        self.recepcion.refresh_from_db()
        self.assertEqual(self.recepcion.estado, RecepcionLote.Estado.ANULADO)
        self.assertTrue(EventoAuditoria.objects.filter(accion='recepcion_lote.anular').exists())

    def test_no_se_puede_anular_dos_veces_desde_el_panel(self):
        self.client.force_login(self.con_permiso)
        self.client.post(reverse('panel:recepcion_lote_anular', args=[self.recepcion.pk]))
        resp = self.client.post(reverse('panel:recepcion_lote_anular', args=[self.recepcion.pk]))
        self.assertRedirects(resp, reverse('panel:orden_compra_detalle', args=[self.oc.pk]))


class BodegaOrdenCompraMultiTenantTests(TestCase):
    """Bodega/OrdenCompra/MovimientoInventario no tenían ningún campo unidad_negocio —
    cualquier usuario autenticado veía las bodegas, compras y kardex de las tres
    unidades de negocio sin filtro. Mismo criterio "compartida o del tenant" que
    Activo/Colaborador/Script."""

    def setUp(self):
        self.sg = UnidadNegocio.objects.get(codigo='SG')
        self.mia = UnidadNegocio.objects.get(codigo='MIA')

        self.bodega_sg = Bodega.objects.create(codigo='BOD-SG', unidad_negocio=self.sg)
        self.bodega_compartida = Bodega.objects.create(codigo='BOD-CENTRAL')

        self.oc_sg = OrdenCompra.objects.create(
            numero_oc='OC-SG-0001', proveedor='Proveedor SG', fecha_emision=date(2026, 1, 1),
            unidad_negocio=self.sg,
        )
        self.oc_compartida = OrdenCompra.objects.create(
            numero_oc='OC-GLOBAL-0001', proveedor='Proveedor Central', fecha_emision=date(2026, 1, 1),
        )

        tipo_consumible = TipoConsumible.objects.create(codigo='MOUSE', nombre='Mouse USB')
        self.movimiento_sg = MovimientoInventario.objects.create(
            tipo_movimiento=MovimientoInventario.TipoMovimiento.INGRESO_CONSUMIBLE,
            tipo_consumible=tipo_consumible, cantidad=1, bodega_destino=self.bodega_sg,
        )

        self.usuario_mia = User.objects.create_user(username='user_mia_bodegas', password='x')
        PerfilUsuario.objects.create(usuario=self.usuario_mia).unidades_negocio.add(self.mia)
        self.usuario_mia.user_permissions.add(
            Permission.objects.get(content_type__app_label='activos', codename='view_bodega'),
            Permission.objects.get(content_type__app_label='activos', codename='view_ordencompra'),
            Permission.objects.get(content_type__app_label='activos', codename='view_movimientoinventario'),
        )
        self.client.force_login(self.usuario_mia)

    def test_bodegas_lista_oculta_la_de_otro_tenant_pero_muestra_compartida(self):
        resp = self.client.get(reverse('panel:bodegas_lista'))
        self.assertNotContains(resp, 'BOD-SG')
        self.assertContains(resp, 'BOD-CENTRAL')

    def test_forzar_ingreso_de_stock_en_bodega_de_otro_tenant_devuelve_403(self):
        resp = self.client.get(reverse('panel:bodega_stock_ingresar', args=[self.bodega_sg.pk]))
        self.assertEqual(resp.status_code, 403)

    def test_ordenes_compra_lista_oculta_la_de_otro_tenant_pero_muestra_compartida(self):
        resp = self.client.get(reverse('panel:ordenes_compra_lista'))
        self.assertNotContains(resp, 'OC-SG-0001')
        self.assertContains(resp, 'OC-GLOBAL-0001')

    def test_forzar_detalle_de_oc_de_otro_tenant_devuelve_403(self):
        resp = self.client.get(reverse('panel:orden_compra_detalle', args=[self.oc_sg.pk]))
        self.assertEqual(resp.status_code, 403)

    def test_oc_compartida_es_accesible(self):
        resp = self.client.get(reverse('panel:orden_compra_detalle', args=[self.oc_compartida.pk]))
        self.assertEqual(resp.status_code, 200)

    def test_movimientos_lista_no_muestra_kardex_de_otro_tenant(self):
        resp = self.client.get(reverse('panel:movimientos_inventario_lista'))
        self.assertNotContains(resp, 'BOD-SG')


class ActivosAvisosTests(TestCase):
    """/activos/avisos/ — panel de visibilidad v1 (sin correo): garantías, stock bajo
    y anomalías red↔activo. Cubre que renderiza y que respeta el tenant scoping."""

    def setUp(self):
        self.sg = UnidadNegocio.objects.get(codigo='SG')
        self.mia = UnidadNegocio.objects.get(codigo='MIA')

        self.activo_garantia_sg = Activo.objects.create(
            codigo='CR-DSK-0030', tipo=Activo.Tipo.DESKTOP, unidad_negocio=self.sg,
            vencimiento_garantia=date.today() - timedelta(days=1),
        )
        self.activo_garantia_compartido = Activo.objects.create(
            codigo='CR-DSK-0031', tipo=Activo.Tipo.DESKTOP,
            vencimiento_garantia=date.today() - timedelta(days=1),
        )

        bodega_sg = Bodega.objects.create(codigo='BOD-AVISOS-SG', unidad_negocio=self.sg)
        tipo_consumible = TipoConsumible.objects.create(codigo='TONER', nombre='Tóner', stock_minimo=5)
        from apps.activos.models import StockBodega
        StockBodega.objects.create(bodega=bodega_sg, tipo_consumible=tipo_consumible, cantidad=1)

        self.usuario_mia = User.objects.create_user(username='user_mia_avisos', password='x')
        PerfilUsuario.objects.create(usuario=self.usuario_mia).unidades_negocio.add(self.mia)
        self.usuario_mia.user_permissions.add(
            Permission.objects.get(content_type__app_label='activos', codename='view_activo'),
        )
        self.client.force_login(self.usuario_mia)

    def test_renderiza_200(self):
        resp = self.client.get(reverse('panel:activos_avisos'))
        self.assertEqual(resp.status_code, 200)

    def test_garantias_oculta_la_de_otro_tenant_pero_muestra_compartida(self):
        resp = self.client.get(reverse('panel:activos_avisos'))
        self.assertNotContains(resp, 'CR-DSK-0030')
        self.assertContains(resp, 'CR-DSK-0031')

    def test_stock_bajo_no_muestra_bodega_de_otro_tenant(self):
        resp = self.client.get(reverse('panel:activos_avisos'))
        self.assertNotContains(resp, 'BOD-AVISOS-SG')


class MantenimientoMultiTenantTests(TestCase):
    def setUp(self):
        self.sg = UnidadNegocio.objects.get(codigo='SG')
        self.mia = UnidadNegocio.objects.get(codigo='MIA')
        colaborador_sg = Colaborador.objects.create(nombre='Ana SG', cedula='2001', unidad_negocio=self.sg)
        self.mantenimiento_sg = Mantenimiento.objects.create(
            cliente=colaborador_sg, fecha_programada=timezone.now(),
        )

        self.usuario_mia = User.objects.create_user(username='user_mia_mant', password='x')
        PerfilUsuario.objects.create(usuario=self.usuario_mia).unidades_negocio.add(self.mia)
        self.usuario_mia.user_permissions.add(
            Permission.objects.get(content_type__app_label='mantenimiento', codename='view_mantenimiento'),
        )
        self.client.force_login(self.usuario_mia)

    def test_lista_no_muestra_mantenimiento_de_otro_tenant(self):
        resp = self.client.get(reverse('panel:mantenimientos_lista'))
        self.assertNotContains(resp, f'#{self.mantenimiento_sg.pk}')

    def test_forzar_detalle_devuelve_403(self):
        resp = self.client.get(reverse('panel:mantenimiento_detalle', args=[self.mantenimiento_sg.pk]))
        self.assertEqual(resp.status_code, 403)


class CumplimientoMultiTenantTests(TestCase):
    def setUp(self):
        self.sg = UnidadNegocio.objects.get(codigo='SG')
        self.mia = UnidadNegocio.objects.get(codigo='MIA')
        creador = User.objects.create_user(username='creador_cump', password='x')
        self.actividad_sg = ActividadCumplimiento.objects.create(
            nombre='Actividad SG', tipo_objetivo=TipoObjetivoCumplimiento.ESTACIONES,
            fecha_limite=date(2026, 12, 31), creado_por=creador,
        )
        self.actividad_sg.unidades_negocio.add(self.sg)

        self.usuario_mia = User.objects.create_user(username='user_mia_cump', password='x')
        PerfilUsuario.objects.create(usuario=self.usuario_mia).unidades_negocio.add(self.mia)
        self.usuario_mia.user_permissions.add(
            Permission.objects.get(content_type__app_label='cumplimiento', codename='view_actividadcumplimiento'),
        )
        self.client.force_login(self.usuario_mia)

    def test_lista_no_muestra_actividad_de_otro_tenant(self):
        resp = self.client.get(reverse('panel:cumplimiento_lista'))
        self.assertNotContains(resp, 'Actividad SG')

    def test_forzar_detalle_devuelve_403(self):
        resp = self.client.get(reverse('panel:cumplimiento_detalle', args=[self.actividad_sg.pk]))
        self.assertEqual(resp.status_code, 403)


class AuditoriaMultiTenantTests(TestCase):
    """La bitácora de auditoría quedó fuera del primer rollout de tenancy: mostraba a
    cualquier usuario logueado las acciones de todos los clientes. Cubre tanto que
    registrar_evento() derive el tenant del objeto auditado, como que la lista/CSV
    respeten ese aislamiento igual que el resto del panel."""

    def setUp(self):
        self.sg = UnidadNegocio.objects.get(codigo='SG')
        self.mia = UnidadNegocio.objects.get(codigo='MIA')
        self.activo_sg = Activo.objects.create(codigo='CR-DSK-0020', tipo=Activo.Tipo.DESKTOP, unidad_negocio=self.sg)
        self.evento_sg = registrar_evento(usuario=None, accion='activo.ingreso', objeto=self.activo_sg)
        self.evento_global = registrar_evento(usuario=None, accion='orden_compra.crear', objeto=None)

        self.usuario_mia = User.objects.create_user(username='user_mia_audit', password='x')
        PerfilUsuario.objects.create(usuario=self.usuario_mia).unidades_negocio.add(self.mia)
        self.usuario_mia.user_permissions.add(
            Permission.objects.get(content_type__app_label='auditoria', codename='view_eventoauditoria'),
        )
        self.client.force_login(self.usuario_mia)

    def test_registrar_evento_deriva_unidad_negocio_del_objeto(self):
        self.assertEqual(self.evento_sg.unidad_negocio, self.sg)
        self.assertIsNone(self.evento_global.unidad_negocio)

    def test_lista_oculta_evento_de_otro_tenant_pero_muestra_globales(self):
        resp = self.client.get(reverse('panel:auditoria_lista'))
        self.assertNotContains(resp, 'CR-DSK-0020')
        self.assertContains(resp, 'orden_compra.crear')

    def test_csv_oculta_evento_de_otro_tenant(self):
        resp = self.client.get(reverse('panel:reporte_auditoria_csv'))
        contenido = resp.content.decode('utf-8-sig')
        self.assertNotIn('CR-DSK-0020', contenido)
        self.assertIn('orden_compra.crear', contenido)

    def test_sin_el_permiso_view_eventoauditoria_devuelve_403(self):
        # AC-1 de la auditoría de gobernanza (22-ago-2026): antes cualquier usuario
        # logueado, sin importar su rol, podía leer la bitácora completa.
        sin_permiso = User.objects.create_user(username='sin_permiso_audit', password='x')
        PerfilUsuario.objects.create(usuario=sin_permiso).unidades_negocio.add(self.mia)
        self.client.force_login(sin_permiso)
        resp = self.client.get(reverse('panel:auditoria_lista'))
        self.assertEqual(resp.status_code, 403)


class ReporteCumplimientoMultiTenantTests(TestCase):
    """Un Grupo (canal TRX) puede estar compartido por farmacias de varias unidades de
    negocio (a diferencia de Despliegue, que sí es de una sola unidad_negocio): el CSV
    de cumplimiento de versión filtraba solo por grupo, sin acotar además al tenant del
    usuario, así que pedir un grupo compartido exponía las estaciones de otro cliente."""

    def setUp(self):
        self.sg = UnidadNegocio.objects.get(codigo='SG')
        self.mia = UnidadNegocio.objects.get(codigo='MIA')
        grupo = Grupo.objects.create(codigo='TRX001')
        farmacia_sg = Farmacia.objects.create(codigo='ML001', grupo=grupo, unidad_negocio=self.sg)
        farmacia_mia = Farmacia.objects.create(codigo='MAM01', grupo=grupo, unidad_negocio=self.mia)
        Estacion.objects.create(
            codigo='ML001-A', farmacia=farmacia_sg, estado_aprobacion=Estacion.EstadoAprobacion.APROBADA,
        )
        Estacion.objects.create(
            codigo='MAM01-A', farmacia=farmacia_mia, estado_aprobacion=Estacion.EstadoAprobacion.APROBADA,
        )

        self.usuario_sg = User.objects.create_user(username='user_sg_reportes', password='x')
        PerfilUsuario.objects.create(usuario=self.usuario_sg).unidades_negocio.add(self.sg)
        self.usuario_sg.user_permissions.add(
            Permission.objects.get(content_type__app_label='cumplimiento', codename='view_actividadcumplimiento'),
        )
        self.client.force_login(self.usuario_sg)

    def test_csv_pidiendo_el_grupo_compartido_no_incluye_la_estacion_de_otro_tenant(self):
        resp = self.client.get(reverse('panel:reporte_cumplimiento_csv'), {'grupo': 'TRX001'})
        contenido = resp.content.decode('utf-8-sig')
        self.assertIn('ML001-A', contenido)
        self.assertNotIn('MAM01-A', contenido)

    def test_csv_sin_filtro_de_grupo_tampoco_incluye_otro_tenant(self):
        resp = self.client.get(reverse('panel:reporte_cumplimiento_csv'))
        contenido = resp.content.decode('utf-8-sig')
        self.assertIn('ML001-A', contenido)
        self.assertNotIn('MAM01-A', contenido)

    def test_selector_de_grupos_ofrece_el_grupo_compartido_una_sola_vez(self):
        resp = self.client.get(reverse('panel:reportes_index'))
        self.assertContains(resp, '>TRX001<', count=1)


class DashboardTests(TestCase):
    def setUp(self):
        usuario = User.objects.create_user(username='u_dashboard', password='x')
        PerfilUsuario.objects.create(usuario=usuario, acceso_todas_unidades=True)
        self.client.force_login(usuario)

    def test_dashboard_con_latido_de_worker_reciente_no_revienta(self):
        from apps.mqtt_worker.models import WorkerHeartbeat
        from apps.mqtt_worker.services import NOMBRE_WORKER_MQTT

        WorkerHeartbeat.objects.create(nombre=NOMBRE_WORKER_MQTT, ultimo_latido=timezone.now())
        resp = self.client.get(reverse('panel:dashboard'))
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.context['worker_mqtt_activo'])


class SoftwarePanelMultiTenantTests(TestCase):
    """Catálogo de software (Fase 2, panel HTMX): mismo criterio "compartida o del
    tenant" que ya usa Script, y misma verificación de acceso puntual que el resto
    del panel para no dejar forzar un ID de otro cliente por URL."""

    def setUp(self):
        from apps.software.models import AplicacionCatalogo, DestinoTipo, SolicitudInstalacion, VersionAplicacion

        self.sg = UnidadNegocio.objects.get(codigo='SG')
        self.mia = UnidadNegocio.objects.get(codigo='MIA')
        creador = User.objects.create_user(username='creador_sw_panel', password='x')

        self.app_privada_sg = AplicacionCatalogo.objects.create(
            nombre='ERP Interno SG', unidad_negocio=self.sg, creado_por=creador,
        )
        self.app_compartida = AplicacionCatalogo.objects.create(nombre='Google Chrome', creado_por=creador)
        self.version_compartida = VersionAplicacion.objects.create(
            aplicacion=self.app_compartida, version='128.0.0',
            instalador=SimpleUploadedFile('chrome.msi', b'x'),
            comando_instalacion_silenciosa='msiexec /i "{archivo}" /qn',
        )
        self.solicitud_sg = SolicitudInstalacion.objects.create(
            version_aplicacion=self.version_compartida, unidad_negocio=self.sg,
            destino_tipo=DestinoTipo.CADENA, creado_por=creador,
        )

        self.usuario_mia = User.objects.create_user(username='user_mia_sw', password='x')
        PerfilUsuario.objects.create(usuario=self.usuario_mia).unidades_negocio.add(self.mia)
        self.usuario_mia.user_permissions.add(
            Permission.objects.get(content_type__app_label='software', codename='view_aplicacioncatalogo'),
            Permission.objects.get(content_type__app_label='software', codename='view_solicitudinstalacion'),
            Permission.objects.get(content_type__app_label='software', codename='add_solicitudinstalacion'),
        )
        self.client.force_login(self.usuario_mia)

    def test_catalogo_oculta_app_privada_de_otro_tenant_pero_muestra_compartida(self):
        resp = self.client.get(reverse('panel:aplicaciones_lista'))
        self.assertNotContains(resp, 'ERP Interno SG')
        self.assertContains(resp, 'Google Chrome')

    def test_forzar_detalle_de_app_privada_de_otro_tenant_devuelve_403(self):
        resp = self.client.get(reverse('panel:aplicacion_detalle', args=[self.app_privada_sg.pk]))
        self.assertEqual(resp.status_code, 403)

    def test_app_compartida_es_accesible(self):
        resp = self.client.get(reverse('panel:aplicacion_detalle', args=[self.app_compartida.pk]))
        self.assertEqual(resp.status_code, 200)

    def test_lista_de_solicitudes_no_muestra_la_de_otro_tenant(self):
        resp = self.client.get(reverse('panel:solicitudes_instalacion_lista'))
        self.assertNotContains(resp, f'#{self.solicitud_sg.pk}')

    def test_forzar_detalle_de_solicitud_de_otro_tenant_devuelve_403(self):
        resp = self.client.get(reverse('panel:solicitud_instalacion_detalle', args=[self.solicitud_sg.pk]))
        self.assertEqual(resp.status_code, 403)

    def test_forzar_publicar_solicitud_de_otro_tenant_devuelve_403(self):
        resp = self.client.post(reverse('panel:solicitud_instalacion_publicar', args=[self.solicitud_sg.pk]))
        self.assertEqual(resp.status_code, 403)
        self.solicitud_sg.refresh_from_db()
        self.assertEqual(self.solicitud_sg.estado, 'borrador')

    def test_form_de_nueva_solicitud_no_ofrece_unidad_de_negocio_ajena(self):
        resp = self.client.get(reverse('panel:solicitud_instalacion_crear'))
        self.assertNotContains(resp, '>SG</option>')
        self.assertContains(resp, '>MIA</option>')


class SoftwareDesactualizadoListaTests(TestCase):
    """/aplicaciones/desactualizadas/ (R7 + catálogo): mismo criterio de tenant que
    el resto del panel de software — una aplicación compartida es visible para
    cualquier tenant, pero las estaciones desactualizadas que muestra deben quedar
    acotadas a las farmacias del tenant activo del usuario."""

    def setUp(self):
        from apps.software.models import AplicacionCatalogo, SoftwareInstaladoDetectado

        self.sg = UnidadNegocio.objects.get(codigo='SG')
        self.mia = UnidadNegocio.objects.get(codigo='MIA')
        grupo = Grupo.objects.create(codigo='TRX001', version_objetivo='4.2.1')
        self.farmacia_sg = Farmacia.objects.create(codigo='ML001', grupo=grupo, unidad_negocio=self.sg)
        self.farmacia_mia = Farmacia.objects.create(codigo='MC001', grupo=grupo, unidad_negocio=self.mia)

        creador = User.objects.create_user(username='creador_sw_desact', password='x')
        self.app_vigilada = AplicacionCatalogo.objects.create(
            nombre='Google Chrome', creado_por=creador, version_mas_reciente_conocida='128.0.0',
        )
        self.app_sin_vigilar = AplicacionCatalogo.objects.create(nombre='7-Zip', creado_por=creador)

        self.estacion_sg = Estacion.objects.create(
            codigo='ML001-A', farmacia=self.farmacia_sg, estado_aprobacion=Estacion.EstadoAprobacion.APROBADA,
        )
        self.estacion_mia = Estacion.objects.create(
            codigo='MC001-A', farmacia=self.farmacia_mia, estado_aprobacion=Estacion.EstadoAprobacion.APROBADA,
        )
        SoftwareInstaladoDetectado.objects.create(estacion=self.estacion_sg, nombre='Google Chrome', version='118.0')
        SoftwareInstaladoDetectado.objects.create(estacion=self.estacion_mia, nombre='Google Chrome', version='120.0')

        self.usuario_mia = User.objects.create_user(username='user_mia_desact', password='x')
        PerfilUsuario.objects.create(usuario=self.usuario_mia).unidades_negocio.add(self.mia)
        self.usuario_mia.user_permissions.add(
            Permission.objects.get(content_type__app_label='software', codename='view_aplicacioncatalogo'),
        )
        self.client.force_login(self.usuario_mia)

    def test_renderiza_200(self):
        resp = self.client.get(reverse('panel:software_desactualizado_lista'))
        self.assertEqual(resp.status_code, 200)

    def test_app_sin_version_conocida_no_aparece_en_las_filas(self):
        resp = self.client.get(reverse('panel:software_desactualizado_lista'))
        nombres = [f['aplicacion'].nombre for f in resp.context['filas']]
        self.assertNotIn('7-Zip', nombres)
        self.assertIn('Google Chrome', nombres)

    def test_no_muestra_estacion_desactualizada_de_otro_tenant(self):
        resp = self.client.get(reverse('panel:software_desactualizado_lista'))
        self.assertNotContains(resp, 'ML001-A')
        self.assertContains(resp, 'MC001-A')

    def test_total_estaciones_desactualizadas_solo_cuenta_el_tenant_activo(self):
        resp = self.client.get(reverse('panel:software_desactualizado_lista'))
        self.assertEqual(resp.context['total_estaciones_desactualizadas'], 1)


class ReportesPorClienteTests(TestCase):
    def setUp(self):
        self.sg = UnidadNegocio.objects.get(codigo='SG')
        self.mia = UnidadNegocio.objects.get(codigo='MIA')
        grupo = Grupo.objects.create(codigo='TRX001', version_objetivo='4.2.1')
        self.farmacia_sg = Farmacia.objects.create(codigo='ML001', grupo=grupo, unidad_negocio=self.sg)
        self.farmacia_mia = Farmacia.objects.create(codigo='MAM01', grupo=grupo, unidad_negocio=self.mia)
        self.estacion_sg = Estacion.objects.create(
            codigo='ML001-A', farmacia=self.farmacia_sg, estado_aprobacion=Estacion.EstadoAprobacion.APROBADA,
            version_pos='4.2.1',
        )
        self.estacion_mia = Estacion.objects.create(
            codigo='MAM01-A', farmacia=self.farmacia_mia, estado_aprobacion=Estacion.EstadoAprobacion.APROBADA,
        )

        self.usuario_mia = User.objects.create_user(username='user_mia_reportes', password='x')
        PerfilUsuario.objects.create(usuario=self.usuario_mia).unidades_negocio.add(self.mia)
        self.usuario_mia.user_permissions.add(
            Permission.objects.get(content_type__app_label='cumplimiento', codename='view_actividadcumplimiento'),
        )
        self.client.force_login(self.usuario_mia)

    def test_reporte_cumplimiento_service_filtra_por_unidad(self):
        from apps.panel.reportes import reporte_cumplimiento
        salida = io.StringIO()
        reporte_cumplimiento(salida, unidades_negocio=[self.mia])
        salida.seek(0)
        codigos = [fila[2] for fila in list(csv.reader(salida))[1:]]
        self.assertIn('MAM01-A', codigos)
        self.assertNotIn('ML001-A', codigos)

    def test_reporte_activos_service_filtra_por_unidad(self):
        from apps.panel.reportes import reporte_activos
        Activo.objects.create(codigo='CR-DSK-9001', tipo=Activo.Tipo.DESKTOP, unidad_negocio=self.mia)
        Activo.objects.create(codigo='CR-DSK-9002', tipo=Activo.Tipo.DESKTOP, unidad_negocio=self.sg)
        salida = io.StringIO()
        reporte_activos(salida, self.mia)
        salida.seek(0)
        codigos = [fila[0] for fila in list(csv.reader(salida))[1:]]
        self.assertIn('CR-DSK-9001', codigos)
        self.assertNotIn('CR-DSK-9002', codigos)

    def test_reporte_alertas_service_filtra_por_unidad(self):
        from apps.panel.reportes import reporte_alertas
        regla = ReglaAlerta.objects.create(
            nombre='CPU alta', metrica=Metrica.CPU_CARGA_PCT, umbral=90, creado_por=self.usuario_mia,
        )
        Alerta.objects.create(regla=regla, estacion=self.estacion_mia, valor_disparador=95)
        Alerta.objects.create(regla=regla, estacion=self.estacion_sg, valor_disparador=95)
        salida = io.StringIO()
        reporte_alertas(salida, self.mia)
        salida.seek(0)
        estaciones = [fila[2] for fila in list(csv.reader(salida))[1:]]
        self.assertIn('MAM01-A', estaciones)
        self.assertNotIn('ML001-A', estaciones)

    def test_reporte_software_instalado_service_filtra_por_unidad_y_nombre(self):
        from apps.panel.reportes import reporte_software_instalado
        SoftwareInstaladoDetectado.objects.create(estacion=self.estacion_mia, nombre='Google Chrome', version='118.0')
        SoftwareInstaladoDetectado.objects.create(estacion=self.estacion_mia, nombre='7-Zip', version='23.01')
        SoftwareInstaladoDetectado.objects.create(estacion=self.estacion_sg, nombre='Google Chrome', version='118.0')

        salida = io.StringIO()
        reporte_software_instalado(salida, self.mia)
        salida.seek(0)
        filas = list(csv.reader(salida))[1:]
        self.assertEqual({fila[5] for fila in filas}, {'MAM01-A'})
        self.assertEqual({fila[0] for fila in filas}, {'Google Chrome', '7-Zip'})

        salida = io.StringIO()
        reporte_software_instalado(salida, self.mia, nombre_filtro='chrome')
        salida.seek(0)
        filas = list(csv.reader(salida))[1:]
        self.assertEqual(len(filas), 1)
        self.assertEqual(filas[0][0], 'Google Chrome')

    def test_reporte_mantenimiento_service_filtra_por_unidad(self):
        from apps.panel.reportes import reporte_mantenimiento

        colaborador_mia = Colaborador.objects.create(nombre='Ana MIA', cedula='9001', unidad_negocio=self.mia)
        colaborador_sg = Colaborador.objects.create(nombre='Luis SG', cedula='9002', unidad_negocio=self.sg)
        Mantenimiento.objects.create(
            cliente=colaborador_mia, descripcion='Falla MIA', fecha_programada=timezone.now(),
        )
        Mantenimiento.objects.create(
            cliente=colaborador_sg, descripcion='Falla SG', fecha_programada=timezone.now(),
        )
        salida = io.StringIO()
        reporte_mantenimiento(salida, self.mia)
        salida.seek(0)
        filas = list(csv.reader(salida))[1:]
        self.assertEqual(len(filas), 1)
        self.assertEqual(filas[0][3], 'Ana MIA')

    def test_reporte_mantenimiento_csv_requiere_permiso(self):
        resp = self.client.get(reverse('panel:reporte_mantenimiento_csv'), {'unidad_negocio': self.mia.pk})
        self.assertEqual(resp.status_code, 403)
        self.usuario_mia.user_permissions.add(
            Permission.objects.get(content_type__app_label='mantenimiento', codename='view_mantenimiento'),
        )
        resp = self.client.get(reverse('panel:reporte_mantenimiento_csv'), {'unidad_negocio': self.mia.pk})
        self.assertEqual(resp.status_code, 200)

    def test_reporte_cliente_resumen_incluye_kpis_de_mantenimiento(self):
        colaborador_mia = Colaborador.objects.create(nombre='Ana MIA', cedula='9003', unidad_negocio=self.mia)
        mantenimiento = Mantenimiento.objects.create(
            cliente=colaborador_mia, descripcion='Falla', fecha_programada=timezone.now(),
        )
        from apps.mantenimiento.services import cerrar_mantenimiento
        from apps.mantenimiento.models import ResultadoTecnico
        cerrar_mantenimiento(mantenimiento=mantenimiento, resultado_tecnico=ResultadoTecnico.REPARADO, usuario=self.usuario_mia)

        resp = self.client.get(reverse('panel:reporte_cliente_resumen'), {'unidad_negocio': self.mia.pk})
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'Mantenimientos del período')

    def test_reporte_cumplimiento_csv_sin_parametro_no_muestra_todo(self):
        resp = self.client.get(reverse('panel:reporte_cumplimiento_csv'))
        contenido = resp.content.decode('utf-8-sig')
        self.assertIn('MAM01-A', contenido)
        self.assertNotIn('ML001-A', contenido)

    def test_reporte_cliente_resumen_de_otro_tenant_404(self):
        # _resolver_unidad_negocio busca dentro del queryset ya escopado a lo visible
        # (get_object_or_404(visibles, ...)) — para un tenant ajeno eso es un 404, no
        # un 403: no confirma que la unidad de negocio exista, simplemente no aparece.
        resp = self.client.get(reverse('panel:reporte_cliente_resumen'), {'unidad_negocio': self.sg.pk})
        self.assertEqual(resp.status_code, 404)

    def test_reporte_cliente_resumen_propio_no_filtra_datos_ajenos(self):
        resp = self.client.get(reverse('panel:reporte_cliente_resumen'), {'unidad_negocio': self.mia.pk})
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, self.mia.codigo)
        self.assertNotContains(resp, 'ML001')


class BloqueoPorIntentosFallidosTests(TestCase):
    """SEC-3 de la auditoría de gobernanza (22-ago-2026): antes /login/ aceptaba fuerza
    bruta ilimitada. django-axes bloquea la CUENTA (no la IP, ver AXES_LOCKOUT_PARAMETERS
    en settings — varias estaciones de una farmacia salen por la misma IP/NAT) tras
    AXES_FAILURE_LIMIT intentos fallidos, incluso si el siguiente intento trae la
    contraseña correcta."""

    def setUp(self):
        self.usuario = User.objects.create_user(username='con_password_valida', password='ContrasenaValida123!')

    def _intentar_login(self, password):
        return self.client.post(reverse('panel:login'), {
            'username': 'con_password_valida', 'password': password,
        })

    def test_bloquea_la_cuenta_tras_el_limite_de_intentos_fallidos(self):
        for _ in range(settings.AXES_FAILURE_LIMIT):
            self._intentar_login('contraseña-incorrecta')
        # Ya bloqueada: ni con la contraseña correcta entra — django-axes corta acá
        # con 429 (Too Many Requests), sin llegar a evaluar la contraseña.
        resp = self._intentar_login('ContrasenaValida123!')
        self.assertEqual(resp.status_code, 429)
        self.assertFalse(resp.wsgi_request.user.is_authenticated)

    def test_no_bloquea_antes_de_alcanzar_el_limite(self):
        for _ in range(settings.AXES_FAILURE_LIMIT - 1):
            self._intentar_login('contraseña-incorrecta')
        resp = self._intentar_login('ContrasenaValida123!')
        self.assertEqual(resp.status_code, 302)


class ExpiracionDeSesionTests(TestCase):
    """SEC-5 de la auditoría de gobernanza (22-ago-2026): antes no había ningún timeout
    de sesión configurado — Django usaba su default (2 semanas, sobrevive el cierre del
    navegador). Ahora es un timeout por INACTIVIDAD de 8 horas (SESSION_SAVE_EVERY_REQUEST
    hace que cada request activo la extienda, así que no es un límite absoluto de sesión)."""

    def test_settings_de_expiracion_configuradas(self):
        self.assertEqual(settings.SESSION_COOKIE_AGE, 60 * 60 * 8)
        self.assertTrue(settings.SESSION_SAVE_EVERY_REQUEST)
        self.assertTrue(settings.SESSION_EXPIRE_AT_BROWSER_CLOSE)

    def test_la_sesion_real_expira_segun_session_cookie_age(self):
        # Con SESSION_EXPIRE_AT_BROWSER_CLOSE=True la cookie en sí no lleva Max-Age (se
        # borra al cerrar el navegador, a propósito) — lo que hay que verificar es la
        # expiración del lado servidor, que sigue gobernada por SESSION_COOKIE_AGE.
        User.objects.create_user(username='u_sesion', password='ContrasenaValida123!')
        resp = self.client.post(reverse('panel:login'), {'username': 'u_sesion', 'password': 'ContrasenaValida123!'})
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(self.client.session.get_expiry_age(), settings.SESSION_COOKIE_AGE)


class MfaTests(TestCase):
    """SEC-4 de la auditoría de gobernanza (22-ago-2026): MFA opcional por usuario
    (TOTP, django-otp) — ver apps.cuentas.forms.LoginConOTPForm y
    apps.panel.views.mfa. Sin dispositivo confirmado, el login sigue exactamente igual
    que antes (regresión cero para quien no lo activó)."""

    def setUp(self):
        self.usuario = User.objects.create_user(username='u_mfa', password='ContrasenaValida123!')

    def _login(self, **extra):
        datos = {'username': 'u_mfa', 'password': 'ContrasenaValida123!'}
        datos.update(extra)
        return self.client.post(reverse('panel:login'), datos)

    def test_sin_dispositivo_el_login_funciona_igual_que_antes(self):
        resp = self._login()
        self.assertEqual(resp.status_code, 302)

    def test_mfa_estado_requiere_login(self):
        resp = self.client.get(reverse('panel:mfa_estado'))
        self.assertEqual(resp.status_code, 302)  # redirige a login, no 200

    def test_configurar_crea_dispositivo_sin_confirmar_y_no_afecta_el_login(self):
        self.client.force_login(self.usuario)
        resp = self.client.get(reverse('panel:mfa_configurar'))
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(TOTPDevice.objects.filter(user=self.usuario, confirmed=False).exists())

        self.client.logout()
        resp = self._login()  # un dispositivo sin confirmar no debe exigir código
        self.assertEqual(resp.status_code, 302)

    def test_confirmar_con_codigo_valido_activa_el_dispositivo(self):
        self.client.force_login(self.usuario)
        self.client.get(reverse('panel:mfa_configurar'))
        dispositivo = TOTPDevice.objects.get(user=self.usuario)
        codigo = str(totp(dispositivo.bin_key)).zfill(6)

        resp = self.client.post(reverse('panel:mfa_configurar'), {'token': codigo})
        self.assertRedirects(resp, reverse('panel:mfa_estado'))
        dispositivo.refresh_from_db()
        self.assertTrue(dispositivo.confirmed)
        self.assertTrue(EventoAuditoria.objects.filter(accion='usuario.mfa_activar', usuario=self.usuario).exists())

    def test_confirmar_con_codigo_invalido_no_activa(self):
        self.client.force_login(self.usuario)
        self.client.get(reverse('panel:mfa_configurar'))
        resp = self.client.post(reverse('panel:mfa_configurar'), {'token': '000000'})
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(TOTPDevice.objects.get(user=self.usuario).confirmed)

    def test_con_dispositivo_confirmado_sin_codigo_el_login_falla(self):
        TOTPDevice.objects.create(user=self.usuario, confirmed=True, name='default')
        resp = self._login()  # sin otp_token
        self.assertEqual(resp.status_code, 200)  # re-renderiza el form, no redirige
        self.assertFalse(resp.wsgi_request.user.is_authenticated)

    def test_con_dispositivo_confirmado_codigo_incorrecto_el_login_falla(self):
        TOTPDevice.objects.create(user=self.usuario, confirmed=True, name='default')
        resp = self._login(otp_token='000000')
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(resp.wsgi_request.user.is_authenticated)

    def test_con_dispositivo_confirmado_codigo_correcto_el_login_funciona(self):
        # Dispositivo propio (no compartido con los otros dos tests de esta clase):
        # TOTPDevice throttlea con backoff exponencial tras un intento fallido (1s por
        # default, ver django_otp.models.ThrottlingMixin) — un código correcto justo
        # después de uno incorrecto en el mismo dispositivo fallaría por el throttle,
        # no por el código en sí.
        dispositivo = TOTPDevice.objects.create(user=self.usuario, confirmed=True, name='default')
        codigo = str(totp(dispositivo.bin_key)).zfill(6)
        resp = self._login(otp_token=codigo)
        self.assertEqual(resp.status_code, 302)

    def test_desactivar_exige_confirmar_password(self):
        TOTPDevice.objects.create(user=self.usuario, confirmed=True, name='default')
        self.client.force_login(self.usuario)

        self.client.post(reverse('panel:mfa_desactivar'), {'password': 'incorrecta'})
        self.assertTrue(TOTPDevice.objects.filter(user=self.usuario).exists())

        self.client.post(reverse('panel:mfa_desactivar'), {'password': 'ContrasenaValida123!'})
        self.assertFalse(TOTPDevice.objects.filter(user=self.usuario).exists())
        self.assertTrue(EventoAuditoria.objects.filter(accion='usuario.mfa_desactivar', usuario=self.usuario).exists())


@override_settings(BITLOCKER_ENCRYPTION_KEY=Fernet.generate_key().decode())
class FarmaciaAplicarNodoPosTests(TestCase):
    """Reapuntar el POS reescribe config de producción: exige change_farmacia, no el
    permiso de diagnóstico. Aplica a la farmacia entera (el balanceo mueve sitios
    completos, dejar media farmacia en cada nodo sería incoherente)."""

    def setUp(self):
        self.grupo = Grupo.objects.create(codigo='TRX004', pos_servidor='192.168.112.3', pos_puerto='5433')
        from apps.catalogo.services import establecer_password_nodo
        establecer_password_nodo(self.grupo, 'clave-nodo')
        farmacia = Farmacia.objects.create(
            codigo='ML001', grupo=self.grupo, unidad_negocio=UnidadNegocio.objects.get(codigo='SG'),
        )
        comun = dict(
            farmacia=farmacia, estado_aprobacion=Estacion.EstadoAprobacion.APROBADA,
            estado_conexion=Estacion.EstadoConexion.ONLINE,
        )
        self.estacion = Estacion.objects.create(codigo='ML001-A', **comun)
        self.hermana = Estacion.objects.create(codigo='ML001-B', **comun)
        self.usuario = User.objects.create_user(username='u_nodo', password='x')
        PerfilUsuario.objects.create(usuario=self.usuario, acceso_todas_unidades=True)
        self.usuario.user_permissions.add(
            Permission.objects.get(content_type__app_label='catalogo', codename='change_farmacia'),
            Permission.objects.get(content_type__app_label='catalogo', codename='view_estacion'),
        )
        self.client.force_login(self.usuario)

    def _url(self):
        return reverse('panel:farmacia_aplicar_nodo_pos', args=[self.estacion.pk])

    def test_sin_permiso_devuelve_403(self):
        sin = User.objects.create_user(username='u_nodo_sin', password='x')
        PerfilUsuario.objects.create(usuario=sin, acceso_todas_unidades=True)
        self.client.force_login(sin)
        self.assertEqual(self.client.post(self._url()).status_code, 403)

    def test_envia_a_todas_las_estaciones_de_la_farmacia(self):
        with patch('apps.panel.views.estaciones.enviar_configurar_nodo_pos', return_value=True) as mock_enviar:
            resp = self.client.post(self._url())
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(mock_enviar.call_count, 2)
        enviadas = {c.args[0].codigo for c in mock_enviar.call_args_list}
        self.assertEqual(enviadas, {'ML001-A', 'ML001-B'})
        # En minúscula: el nombre de la base en el .Config es sensible a mayúsculas y
        # `codigo` siempre es MAYÚSCULA, así que se manda `bdd_pos`, no `codigo`.
        self.assertEqual(mock_enviar.call_args.kwargs['bdd'], 'trx004')

    def test_nodo_sin_password_no_envia_nada(self):
        # Sin contraseña el POS no podría conectarse al nodo nuevo: mejor no tocar
        # nada y avisar, que dejarlo a medio configurar.
        from apps.catalogo.services import establecer_password_nodo
        establecer_password_nodo(self.grupo, '')
        with patch('apps.panel.views.estaciones.enviar_configurar_nodo_pos') as mock_enviar:
            resp = self.client.post(self._url())
        self.assertEqual(resp.status_code, 200)
        mock_enviar.assert_not_called()

    def test_nodo_sin_servidor_no_envia_nada(self):
        self.grupo.pos_servidor = ''
        self.grupo.save(update_fields=['pos_servidor'])
        with patch('apps.panel.views.estaciones.enviar_configurar_nodo_pos') as mock_enviar:
            resp = self.client.post(self._url())
        self.assertEqual(resp.status_code, 200)
        mock_enviar.assert_not_called()

    def test_estacion_offline_no_recibe_el_comando(self):
        self.hermana.estado_conexion = Estacion.EstadoConexion.OFFLINE
        self.hermana.save(update_fields=['estado_conexion'])
        with patch('apps.panel.views.estaciones.enviar_configurar_nodo_pos', return_value=True) as mock_enviar:
            self.client.post(self._url())
        self.assertEqual(mock_enviar.call_count, 1)
        self.assertEqual(mock_enviar.call_args.args[0].codigo, 'ML001-A')


class VisitaTecnicaPanelTests(TestCase):
    """La vista reemplazó al reporte muerto que agrupaba por activos.Ubicacion."""

    def setUp(self):
        sg = UnidadNegocio.objects.get(codigo='SG')
        grupo = Grupo.objects.create(codigo='TRX001')
        self.farmacia = Farmacia.objects.create(
            codigo='ML001', grupo=grupo, unidad_negocio=sg, latitud=-2.170998, longitud=-79.922359,
        )
        self.usuario = User.objects.create_user(username='u_vis', password='x')
        PerfilUsuario.objects.create(usuario=self.usuario, acceso_todas_unidades=True)
        for codename in ('view_visitatecnica', 'add_visitatecnica', 'change_visitatecnica'):
            self.usuario.user_permissions.add(
                Permission.objects.get(content_type__app_label='mantenimiento', codename=codename),
            )
        self.client.force_login(self.usuario)

    def test_lista_responde_200(self):
        self.assertEqual(self.client.get(reverse('panel:visita_tecnica_lista')).status_code, 200)

    def test_crear_planifica_la_visita(self):
        resp = self.client.post(reverse('panel:visita_tecnica_crear'), {
            'farmacia': self.farmacia.pk,
            'tecnico': self.usuario.pk,
            'fecha_planificada': '2026-09-15',
            'motivo': 'relevamiento',
        })
        self.assertEqual(resp.status_code, 302)
        visita = VisitaTecnica.objects.get()
        self.assertEqual(visita.farmacia, self.farmacia)
        self.assertEqual(visita.estado, VisitaTecnica.Estado.PLANIFICADA)

    def test_flujo_iniciar_y_cerrar(self):
        visita = VisitaTecnica.objects.create(
            farmacia=self.farmacia, tecnico=self.usuario, fecha_planificada='2026-09-15',
        )
        self.client.post(reverse('panel:visita_tecnica_accion', args=[visita.pk, 'iniciar']))
        visita.refresh_from_db()
        self.assertEqual(visita.estado, VisitaTecnica.Estado.EN_CURSO)

        self.client.post(reverse('panel:visita_tecnica_accion', args=[visita.pk, 'cerrar']),
                         {'observaciones': 'sin novedades'})
        visita.refresh_from_db()
        self.assertEqual(visita.estado, VisitaTecnica.Estado.REALIZADA)
        self.assertEqual(visita.observaciones, 'sin novedades')

    def test_sin_permiso_devuelve_403(self):
        sin = User.objects.create_user(username='u_vis_sin', password='x')
        PerfilUsuario.objects.create(usuario=sin, acceso_todas_unidades=True)
        self.client.force_login(sin)
        self.assertEqual(self.client.get(reverse('panel:visita_tecnica_lista')).status_code, 403)


class EspecificacionesPorSerieTests(TestCase):
    """El alta de activo completa las especificaciones desde la estación que ya
    reporta esa serie, sin pisar lo que el usuario haya escrito."""

    def setUp(self):
        sg = UnidadNegocio.objects.get(codigo='SG')
        grupo = Grupo.objects.create(codigo='TRX001')
        farmacia = Farmacia.objects.create(codigo='ML001', grupo=grupo, unidad_negocio=sg)
        Estacion.objects.create(
            codigo='ML001-A', farmacia=farmacia, numero_serie='MXL8192898',
            procesador='Intel i3-1115G4', ram_total_mb=7839, almacenamiento_total_gb=446,
        )
        self.usuario = User.objects.create_user(username='u_specs', password='x')
        PerfilUsuario.objects.create(usuario=self.usuario, acceso_todas_unidades=True)
        self.usuario.user_permissions.add(
            Permission.objects.get(content_type__app_label='activos', codename='add_activo'),
        )
        self.client.force_login(self.usuario)

    def _url(self):
        return reverse('panel:especificaciones_por_serie_partial')

    def test_completa_desde_la_estacion(self):
        resp = self.client.get(self._url(), {'numero_serie': 'MXL8192898'})
        self.assertEqual(resp.status_code, 200)
        contenido = resp.content.decode()
        self.assertIn('Intel i3-1115G4', contenido)
        # RAM convertida de MB a GB, no el numero crudo.
        self.assertIn('value="8"', contenido)
        self.assertNotIn('value="7839"', contenido)
        self.assertIn('ML001-A', contenido)

    def test_no_pisa_lo_que_el_usuario_escribio(self):
        resp = self.client.get(self._url(), {
            'numero_serie': 'MXL8192898',
            'procesador': 'Procesador cargado a mano',
            'ram_gb': '16',
        })
        contenido = resp.content.decode()
        self.assertIn('Procesador cargado a mano', contenido)
        self.assertIn('value="16"', contenido)
        # El disco, que el usuario no toco, si se completa.
        self.assertIn('value="446"', contenido)

    def test_serie_desconocida_avisa_que_no_hubo_resultados(self):
        # Sin este aviso, no encontrar nada se ve igual que la carga inicial y el
        # boton "Buscar" parece no haber hecho nada.
        resp = self.client.get(self._url(), {'numero_serie': 'NO-EXISTE'})
        self.assertEqual(resp.status_code, 200)
        contenido = resp.content.decode()
        self.assertIn('No se encontro', contenido.replace('ó', 'o'))
        self.assertIn('NO-EXISTE', contenido)
        self.assertNotIn('ML001-A', contenido)

    def test_sin_buscar_todavia_no_muestra_ningun_aviso(self):
        # Carga inicial del formulario: no se busco nada, no hay nada que avisar.
        resp = self.client.get(self._url())
        contenido = resp.content.decode().replace('ó', 'o')
        self.assertNotIn('No se encontro', contenido)
        self.assertNotIn('Datos tomados de la estacion', contenido.replace('ó', 'o'))

    def test_sin_permiso_devuelve_403(self):
        sin = User.objects.create_user(username='u_specs_sin', password='x')
        PerfilUsuario.objects.create(usuario=sin, acceso_todas_unidades=True)
        self.client.force_login(sin)
        self.assertEqual(self.client.get(self._url()).status_code, 403)


class BuscarEquiposParaMantenimientoTests(TestCase):
    """El alta de mantenimiento se puebla BUSCANDO. Antes exigia elegir el custodio,
    lo que dejaba fuera a los POS de farmacia -- que no tienen -- y obligaba a saber
    de quien es el equipo cuando lo que se tiene a mano es la farmacia o la serie."""

    def setUp(self):
        sg = UnidadNegocio.objects.get(codigo='SG')
        grupo = Grupo.objects.create(codigo='TRX001')
        self.farmacia = Farmacia.objects.create(
            codigo='MAM06', grupo=grupo, unidad_negocio=sg, nombre='FARMACIAS MIA',
        )
        self.custodio = Colaborador.objects.create(
            nombre='Wellington Alvarez', cedula='1314821941', unidad_negocio=sg,
        )
        self.equipo = Activo.objects.create(
            codigo='CR-DSK-9100', tipo=Activo.Tipo.DESKTOP, numero_serie='BB3VJD3',
            modelo='OptiPlex', farmacia=self.farmacia, colaborador_actual=self.custodio,
        )
        self.otro = Activo.objects.create(
            codigo='CR-LAP-9200', tipo=Activo.Tipo.LAPTOP, numero_serie='ZZZ999',
        )
        self.usuario = User.objects.create_user(username='u_buscar', password='x')
        PerfilUsuario.objects.create(usuario=self.usuario, acceso_todas_unidades=True)
        self.usuario.user_permissions.add(
            Permission.objects.get(content_type__app_label='mantenimiento', codename='add_mantenimiento'),
        )
        self.client.force_login(self.usuario)

    def _buscar(self, **params):
        return self.client.get(
            reverse('panel:equipos_por_cliente_partial'), params,
        ).content.decode()

    def test_sin_criterio_no_lista_nada_y_lo_explica(self):
        # Volcar cientos de equipos en un <select> no ayuda a nadie.
        html = self._buscar()
        self.assertNotIn('CR-DSK-9100', html)
        self.assertIn('Busca por codigo', html)

    def test_busca_por_codigo_de_farmacia(self):
        html = self._buscar(buscar='MAM06')
        self.assertIn('CR-DSK-9100', html)
        self.assertNotIn('CR-LAP-9200', html)

    def test_busca_por_nombre_de_farmacia(self):
        self.assertIn('CR-DSK-9100', self._buscar(buscar='FARMACIAS MIA'))

    def test_busca_por_cedula_del_custodio(self):
        self.assertIn('CR-DSK-9100', self._buscar(buscar='1314821941'))

    def test_busca_por_nombre_del_custodio(self):
        self.assertIn('CR-DSK-9100', self._buscar(buscar='Wellington'))

    def test_busca_por_serie_y_por_codigo_del_equipo(self):
        self.assertIn('CR-DSK-9100', self._buscar(buscar='BB3VJD3'))
        self.assertIn('CR-LAP-9200', self._buscar(buscar='ZZZ999'))

    def test_sin_coincidencias_lo_dice(self):
        html = self._buscar(buscar='NOEXISTE')
        self.assertIn('Ningun equipo coincide', html)

    def test_no_lista_equipos_dados_de_baja(self):
        self.equipo.estado = Activo.Estado.DADO_DE_BAJA
        self.equipo.save(update_fields=['estado'])
        self.assertNotIn('CR-DSK-9100', self._buscar(buscar='MAM06'))

    def test_se_puede_crear_sin_custodio(self):
        # Un POS de farmacia no tiene custodio; exigirlo obligaba a inventar uno.
        resp = self.client.post(reverse('panel:mantenimiento_crear'), {
            'equipos': [self.equipo.pk],
            'prioridad': PrioridadMantenimiento.ALTA,
            'estado_general': EstadoGeneralEquipo.NO_OPERATIVO,
            'descripcion': 'No enciende',
            'fecha_programada': '2026-10-01T09:00',
        })
        self.assertEqual(resp.status_code, 302, getattr(resp, 'context', {}))
        m = Mantenimiento.objects.get()
        self.assertIsNone(m.cliente)
        self.assertEqual(m.prioridad, PrioridadMantenimiento.ALTA)
