import csv
import json
import re
from decimal import Decimal
import io

from django.conf import settings
from datetime import date, timedelta
from unittest.mock import patch

from cryptography.fernet import Fernet
from django.contrib.auth.models import Permission, User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.test import RequestFactory, TestCase, override_settings
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
from apps.mqtt_worker.models import WorkerHeartbeat
from apps.cumplimiento.models import (
    ActividadCumplimiento, ResultadoCumplimientoEstacion, TipoObjetivoCumplimiento,
)
from apps.despliegues.models import Despliegue
from apps.mantenimiento.models import (
    EstadoGeneralEquipo, ImagenMantenimiento, Mantenimiento, PrioridadMantenimiento, TipoMantenimiento, VisitaTecnica,
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
    """Consumo de red por FARMACIA (SNMP a Mikrotik), ahora integrado a la pantalla de
    enlaces: eran dos listados de las MISMAS farmacias y se fusionaron el 11-sep-2026
    (ver el docstring de apps.panel.views.enlaces.red_farmacias_lista).

    Estas pruebas se reapuntaron en vez de borrarse: lo que verifican —que una farmacia
    sin IP no aparezca, el aislamiento por tenant y que se muestre el consumo— sigue
    siendo válido, solo cambió dónde se ve.
    """

    def setUp(self):
        self.sg = UnidadNegocio.objects.get(codigo='SG')
        self.mia = UnidadNegocio.objects.get(codigo='MIA')
        grupo = Grupo.objects.create(codigo='TRX001')
        self.farmacia_sg = Farmacia.objects.create(
            codigo='ML001', grupo=grupo, unidad_negocio=self.sg, ip_router='10.0.1.1',
        )
        self.farmacia_sin_ip = Farmacia.objects.create(codigo='ML002', grupo=grupo, unidad_negocio=self.sg)
        self.usuario = User.objects.create_user(username='u_red_farmacia', password='x')
        PerfilUsuario.objects.create(usuario=self.usuario, acceso_todas_unidades=True)
        self.usuario.user_permissions.add(
            Permission.objects.get(content_type__app_label='monitoreo', codename='view_muestraredfarmacia'),
            Permission.objects.get(content_type__app_label='monitoreo', codename='view_estadoenlacefarmacia'),
        )
        self.client.force_login(self.usuario)

    def test_la_url_vieja_redirige_a_la_pantalla_fusionada(self):
        self.assertRedirects(
            self.client.get(reverse('panel:red_farmacias_lista')),
            reverse('panel:enlaces_farmacias_lista'),
        )

    def test_farmacia_sin_ip_router_no_aparece(self):
        resp = self.client.get(reverse('panel:enlaces_farmacias_lista'))
        self.assertNotContains(resp, 'ML002')

    def test_farmacia_con_ip_router_pero_nunca_sondeada_aparece_sin_consumo(self):
        resp = self.client.get(reverse('panel:enlaces_farmacias_lista'))
        self.assertContains(resp, 'ML001')
        # Sin SNMP habilitado en el router no hay consumo, y el listado no lo convierte en
        # un cero que se leería como "no consume nada". Desde el 26-oct-2026 el consumo no
        # tiene columna: está en el detalle de la farmacia, que explica cómo habilitar SNMP.
        fila = next(f for f in resp.context['filas'] if f['farmacia'].codigo == 'ML001')
        self.assertIsNone(fila['total_kbps'])
        self.assertNotContains(resp, '0 kbps')

    def test_muestra_el_consumo_de_la_ultima_muestra(self):
        MuestraRedFarmacia.objects.create(
            farmacia=self.farmacia_sg, bytes_recibidos=1000, bytes_enviados=500,
            red_recibido_kbps=1200.0, red_enviado_kbps=300.5,
        )
        resp = self.client.get(reverse('panel:enlaces_farmacias_lista'))
        self.assertEqual(resp.context['filas'][0]['total_kbps'], 1500.5)
        self.assertEqual(resp.context['con_ancho_banda'], 1)

    def test_no_mezcla_farmacias_de_otro_tenant(self):
        grupo2 = Grupo.objects.create(codigo='TRX002')
        Farmacia.objects.create(codigo='MAM01', grupo=grupo2, unidad_negocio=self.mia, ip_router='10.0.2.1')

        usuario_sg_only = User.objects.create_user(username='u_sg_only_red', password='x')
        PerfilUsuario.objects.create(usuario=usuario_sg_only).unidades_negocio.add(self.sg)
        usuario_sg_only.user_permissions.add(
            Permission.objects.get(content_type__app_label='monitoreo', codename='view_estadoenlacefarmacia'),
        )
        self.client.force_login(usuario_sg_only)

        resp = self.client.get(reverse('panel:enlaces_farmacias_lista'))
        self.assertNotContains(resp, 'MAM01')


class AvisoDePendientesSigueAlClienteActivoTests(TestCase):
    """F-12: el aviso amarillo de /estaciones/ ignoraba el selector de cliente.

    Su marco (`estaciones_lista`) acota con `scope_por_unidad_negocio_activa` y el parcial
    usaba `scope_por_unidad_negocio` a secas, asi que con un cliente fijado la tabla mostraba
    uno y el aviso de arriba contaba y listaba las pendientes de todos: dos conjuntos
    distintos en la misma pantalla.

    No se confunde con los contadores de las tarjetas, que son globales respecto de los
    FILTROS de la URL pero se calculan sobre `base`, que ya paso por `_activa`.
    """

    def setUp(self):
        self.sg = UnidadNegocio.objects.get(codigo='SG')
        self.mia = UnidadNegocio.objects.get(codigo='MIA')
        grupo = Grupo.objects.create(codigo='TRX912')
        self.pendiente_sg = Estacion.objects.create(
            codigo='ML912-A', estado_aprobacion=Estacion.EstadoAprobacion.PENDIENTE,
            farmacia=Farmacia.objects.create(codigo='ML912', grupo=grupo, unidad_negocio=self.sg),
        )
        self.pendiente_mia = Estacion.objects.create(
            codigo='MM912-A', estado_aprobacion=Estacion.EstadoAprobacion.PENDIENTE,
            farmacia=Farmacia.objects.create(codigo='MM912', grupo=grupo, unidad_negocio=self.mia),
        )
        self.usuario = User.objects.create_user(username='u_pend_912', password='x')
        PerfilUsuario.objects.create(usuario=self.usuario, acceso_todas_unidades=True)
        self.usuario.user_permissions.add(
            Permission.objects.get(content_type__app_label='catalogo', codename='view_estacion'),
        )
        self.client.force_login(self.usuario)

    def _fijar_cliente(self, unidad):
        from apps.cuentas.services import SESSION_KEY_UNIDAD_ACTIVA

        sesion = self.client.session
        sesion[SESSION_KEY_UNIDAD_ACTIVA] = unidad.pk
        sesion.save()

    def test_sin_cliente_fijado_se_ven_las_pendientes_de_todos(self):
        resp = self.client.get(reverse('panel:estaciones_pendientes_partial'))

        self.assertContains(resp, 'ML912-A')
        self.assertContains(resp, 'MM912-A')

    def test_con_un_cliente_fijado_el_aviso_solo_cuenta_las_de_ese_cliente(self):
        self._fijar_cliente(self.sg)

        resp = self.client.get(reverse('panel:estaciones_pendientes_partial'))

        self.assertContains(resp, 'ML912-A')
        self.assertNotContains(resp, 'MM912-A')

    def test_la_pantalla_completa_no_se_contradice_a_si_misma(self):
        """Lo que el usuario ve: el aviso y la tabla, en la misma respuesta, hablando del
        mismo conjunto."""
        self._fijar_cliente(self.mia)

        resp = self.client.get(reverse('panel:estaciones_lista'))

        self.assertContains(resp, 'MM912-A')
        self.assertNotContains(resp, 'ML912-A')


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


class AprobarUnaEstacionQueVeniaRechazadaAvisaTests(TestCase):
    """Volver de un rechazo no es gratis, y el panel ahora lo dice (§10-BV).

    Desde §10-BL rechazar revoca la credencial MQTT en EMQX. Si después se aprueba, el
    `identidad.json` del equipo conserva una credencial que ya no existe, el agente no cae a
    la compartida y el script que borraría ese archivo viaja por MQTT — así que no le llega.
    La estación puede quedar muda y el síntoma no apunta a la causa: nadie piensa que no
    conecta *porque la aprobaron*.

    El arreglo de fondo es del agente (0.34, §10-BV). Esto no arregla: avisa. Medido el
    12-oct-2026, nunca se rechazó una estación, así que lo que cambia es que la primera vez
    no se diagnostique a ciegas.
    """

    def setUp(self):
        self.sg = UnidadNegocio.objects.get(codigo='SG')
        grupo = Grupo.objects.create(codigo='TRX001')
        farmacia = Farmacia.objects.create(codigo='ML001', grupo=grupo, unidad_negocio=self.sg)
        self.estacion = Estacion.objects.create(codigo='ML001-A', farmacia=farmacia)
        self.usuario = User.objects.create_user(username='u_aprueba', password='x')
        PerfilUsuario.objects.create(usuario=self.usuario, acceso_todas_unidades=True)
        for codename in ('aprobar_estacion', 'view_estacion'):
            self.usuario.user_permissions.add(
                Permission.objects.get(content_type__app_label='catalogo', codename=codename),
            )
        self.client.force_login(self.usuario)

    def _aprobar(self):
        return self.client.post(reverse('panel:estacion_aprobar', args=[self.estacion.pk]))

    def test_aprobar_una_pendiente_no_avisa_nada(self):
        """El caso normal —y el único que ocurre hoy— no se ensucia con una advertencia."""
        resp = self._aprobar()

        self.assertEqual(resp.status_code, 200)
        self.assertNotContains(resp, 'venía RECHAZADA')
        self.estacion.refresh_from_db()
        self.assertEqual(self.estacion.estado_aprobacion, Estacion.EstadoAprobacion.APROBADA)

    def test_aprobar_una_rechazada_avisa_que_puede_no_volver_sola(self):
        self.estacion.estado_aprobacion = Estacion.EstadoAprobacion.RECHAZADA
        self.estacion.save(update_fields=['estado_aprobacion'])

        resp = self._aprobar()

        self.assertContains(resp, 'venía RECHAZADA')
        self.assertContains(resp, 'identidad.json')
        self.estacion.refresh_from_db()
        self.assertEqual(self.estacion.estado_aprobacion, Estacion.EstadoAprobacion.APROBADA)

    def test_queda_en_la_auditoria_de_donde_venia(self):
        """Si una estación aprobada no vuelve, lo primero que hay que poder contestar es si
        venía de un rechazo."""
        self.estacion.estado_aprobacion = Estacion.EstadoAprobacion.RECHAZADA
        self.estacion.save(update_fields=['estado_aprobacion'])

        self._aprobar()

        evento = EventoAuditoria.objects.filter(accion='estacion.aprobar').latest('timestamp')
        self.assertTrue(evento.detalle['venia_rechazada'])

    def test_la_transicion_no_se_escribe_en_la_vista(self):
        """`D-5`: la vista hace permisos, scoping, auditoría y el aviso; la transición es del
        servicio. Si alguien vuelve a escribir el campo acá, esto lo marca."""
        import inspect

        from apps.panel.views import estaciones_aprobacion

        fuente = inspect.getsource(estaciones_aprobacion)
        self.assertNotIn('estado_aprobacion = Estacion.EstadoAprobacion', fuente)
        self.assertIn('aprobar_estacion(estacion)', fuente)


class RechazarEstacionCortaElAccesoAlBrokerTests(TestCase):
    """Rechazar desde el panel tiene que revocar la credencial MQTT (`B-1`).

    Antes escribía `estado_aprobacion = RECHAZADA` y nada más: la estación seguía
    autenticando contra EMQX con ACL sobre sus tópicos y sobre los dos catálogos globales,
    y cada reintento de enrolamiento le entregaba una contraseña nueva. El rechazo era de
    aplicación, no de broker.
    """

    def setUp(self):
        self.sg = UnidadNegocio.objects.get(codigo='SG')
        grupo = Grupo.objects.create(codigo='TRX001')
        farmacia = Farmacia.objects.create(codigo='ML001', grupo=grupo, unidad_negocio=self.sg)
        self.estacion = Estacion.objects.create(codigo='ML001-A', farmacia=farmacia)
        self.usuario = User.objects.create_user(username='u_rechaza', password='x')
        PerfilUsuario.objects.create(usuario=self.usuario, acceso_todas_unidades=True)
        for codename in ('aprobar_estacion', 'view_estacion'):
            self.usuario.user_permissions.add(
                Permission.objects.get(content_type__app_label='catalogo', codename=codename),
            )
        self.client.force_login(self.usuario)

    def test_rechazar_revoca_la_credencial_de_esa_estacion(self):
        with patch(
            'apps.mqtt_worker.emqx_admin.revocar_credencial_estacion', return_value=True,
        ) as revocar:
            resp = self.client.post(reverse('panel:estacion_rechazar', args=[self.estacion.pk]))

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(revocar.call_args.args[0].pk, self.estacion.pk)
        self.estacion.refresh_from_db()
        self.assertEqual(self.estacion.estado_aprobacion, Estacion.EstadoAprobacion.RECHAZADA)

    def test_queda_registrado_en_la_auditoria_si_se_revoco(self):
        """El rechazo ya se auditaba; lo que faltaba era saber si el acceso al broker se
        cortó de verdad, que es la parte que puede fallar."""
        with patch('apps.mqtt_worker.emqx_admin.revocar_credencial_estacion', return_value=True):
            self.client.post(reverse('panel:estacion_rechazar', args=[self.estacion.pk]))

        evento = EventoAuditoria.objects.get(accion='estacion.rechazar')
        self.assertTrue(evento.detalle['credencial_mqtt_revocada'])

    def test_si_emqx_no_responde_lo_dice_en_pantalla(self):
        """Lo peor sería un rechazo que se ve exitoso y deja la estación conectada. El
        aviso va en el partial porque esta vista responde por HTMX: un `messages.error`
        no se vería hasta la próxima navegación completa."""
        with patch('apps.mqtt_worker.emqx_admin.revocar_credencial_estacion', return_value=False):
            resp = self.client.post(reverse('panel:estacion_rechazar', args=[self.estacion.pk]))

        self.assertContains(resp, 'no se pudo revocar')
        self.assertContains(resp, 'ML001-A')

    def test_sin_credenciales_por_estacion_no_avisa_de_un_fallo_que_no_ocurrio(self):
        """`None` es "no aplica": sin `EMQX_ADMIN_CONFIG` esta instalación no usa
        credenciales por estación."""
        with patch('apps.mqtt_worker.emqx_admin.revocar_credencial_estacion', return_value=None):
            resp = self.client.post(reverse('panel:estacion_rechazar', args=[self.estacion.pk]))

        self.assertNotContains(resp, 'no se pudo revocar')


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


class EstacionesAgenteTandasTests(TestCase):
    """Rollout del agente desde el panel, por tandas (Estaciones → Actualizar agente).

    Lo que tiene que ser cierto: mientras ninguna estación reporta la versión nueva, la tanda
    es un canario de 3 (un build malo deja sin agente a cada estación que lo recibe); después,
    tandas de hasta 15. Y nunca una orden a una estación de otra unidad de negocio.
    """

    ENVIAR = 'apps.panel.views.agente_flota.enviar_actualizacion_agente'

    def setUp(self):
        self.sg = UnidadNegocio.objects.get(codigo='SG')
        self.mia = UnidadNegocio.objects.get(codigo='MIA')
        grupo = Grupo.objects.create(codigo='TRX001')
        self.farmacia_sg = Farmacia.objects.create(codigo='ML001', grupo=grupo, unidad_negocio=self.sg)
        self.farmacia_mia = Farmacia.objects.create(codigo='MM001', grupo=grupo, unidad_negocio=self.mia)
        self.estaciones = [
            Estacion.objects.create(
                codigo=f'ML001-{letra}', farmacia=self.farmacia_sg, version_agente='agente-prueba-0.34',
                estado_aprobacion=Estacion.EstadoAprobacion.APROBADA, ultimo_heartbeat=timezone.now(),
            )
            for letra in 'ABCDE'
        ]
        self.de_mia = Estacion.objects.create(
            codigo='MM001-A', farmacia=self.farmacia_mia, version_agente='agente-prueba-0.34',
            estado_aprobacion=Estacion.EstadoAprobacion.APROBADA,
        )
        self.pendiente = Estacion.objects.create(
            codigo='ML001-Z', farmacia=self.farmacia_sg, version_agente='agente-prueba-0.34',
            estado_aprobacion=Estacion.EstadoAprobacion.PENDIENTE,
        )
        self.version = VersionAgente.objects.create(
            version='agente-prueba-0.35', ejecutable=SimpleUploadedFile('agente.exe', b'MZ-falso'),
            creado_por=User.objects.create_user(username='creador_tandas', password='x'),
        )
        permisos = [
            Permission.objects.get(content_type__app_label='catalogo', codename='actualizar_agente_estacion'),
            Permission.objects.get(content_type__app_label='catalogo', codename='view_estacion'),
        ]
        self.operador = User.objects.create_user(username='operador_tandas', password='x')
        PerfilUsuario.objects.create(usuario=self.operador, acceso_todas_unidades=True)
        self.operador.user_permissions.add(*permisos)
        self.operador_sg = User.objects.create_user(username='operador_sg_tandas', password='x')
        PerfilUsuario.objects.create(usuario=self.operador_sg).unidades_negocio.add(self.sg)
        self.operador_sg.user_permissions.add(*permisos)

    def _enviar(self, estaciones, usuario=None):
        self.client.force_login(usuario or self.operador)
        with patch(self.ENVIAR, return_value=True) as enviar:
            resp = self.client.post(
                reverse('panel:estaciones_agente_actualizar'),
                {'estacion_ids': [e.pk for e in estaciones]},
            )
        return resp, enviar

    def test_sin_el_permiso_devuelve_403(self):
        sin_permiso = User.objects.create_user(username='sin_permiso_tandas', password='x')
        PerfilUsuario.objects.create(usuario=sin_permiso, acceso_todas_unidades=True)
        self.client.force_login(sin_permiso)
        self.assertEqual(self.client.get(reverse('panel:estaciones_agente')).status_code, 403)
        self.assertEqual(self.client.post(reverse('panel:estaciones_agente_actualizar')).status_code, 403)

    def test_lista_las_aprobadas_que_faltan_y_avisa_el_canario(self):
        self.client.force_login(self.operador)
        resp = self.client.get(reverse('panel:estaciones_agente'))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'ML001-A')
        self.assertContains(resp, 'MM001-A')
        self.assertNotContains(resp, 'ML001-Z')  # pendiente de aprobación
        self.assertContains(resp, 'canario de 3')
        # La primera versión tenía el botón de enviar al final de 79 filas y con `class="btn"`
        # sin variante (transparente): estaba, pero nadie lo veía.
        self.assertContains(resp, 'class="btn primary" id="enviar-tanda"')
        self.assertContains(resp, 'Marcar las primeras 3')

    def test_el_canario_no_acepta_mas_de_3(self):
        resp, enviar = self._enviar(self.estaciones[:4])
        self.assertRedirects(resp, reverse('panel:estaciones_agente'))
        enviar.assert_not_called()

    def test_el_canario_envia_y_audita_cada_estacion(self):
        _, enviar = self._enviar(self.estaciones[:3])
        self.assertEqual(enviar.call_count, 3)
        self.assertEqual(
            EventoAuditoria.objects.filter(accion='estacion.actualizar_agente').count(), 3,
        )

    def test_con_la_version_ya_probada_la_tanda_pasa_a_15(self):
        self.estaciones[0].version_agente = 'agente-prueba-0.35'
        self.estaciones[0].save(update_fields=['version_agente'])
        _, enviar = self._enviar(self.estaciones[1:5])
        self.assertEqual(enviar.call_count, 4)

    def test_no_reenvia_a_la_que_ya_reporta_la_version(self):
        self.estaciones[0].version_agente = 'agente-prueba-0.35'
        self.estaciones[0].save(update_fields=['version_agente'])
        _, enviar = self._enviar(self.estaciones[:2])
        self.assertEqual(enviar.call_count, 1)

    def test_no_envia_a_una_estacion_de_otra_unidad(self):
        """El scope se aplica sobre el usuario: un id de otra unidad en el POST se ignora."""
        _, enviar = self._enviar([self.estaciones[0], self.de_mia], usuario=self.operador_sg)
        enviar.assert_called_once()
        self.assertEqual(enviar.call_args.args[0], self.estaciones[0])

    def test_un_id_que_no_es_numero_no_da_500(self):
        self.client.force_login(self.operador)
        with patch(self.ENVIAR) as enviar:
            resp = self.client.post(reverse('panel:estaciones_agente_actualizar'), {'estacion_ids': ['1 OR 1=1']})
        self.assertEqual(resp.status_code, 302)
        enviar.assert_not_called()

    def test_sin_version_cargada_no_envia_nada(self):
        self.version.delete()
        self.client.force_login(self.operador)
        self.assertContains(self.client.get(reverse('panel:estaciones_agente')), 'No hay ninguna versión')
        _, enviar = self._enviar(self.estaciones[:1])
        enviar.assert_not_called()

    def test_el_comando_usa_el_mismo_criterio(self):
        """`actualizar_agente_flota` y la pantalla comparten `estaciones_para_actualizar_agente`:
        el comando no debe ver una estación pendiente ni las que ya están al día."""
        self.estaciones[0].version_agente = 'agente-prueba-0.35'
        self.estaciones[0].save(update_fields=['version_agente'])
        salida = io.StringIO()
        call_command('actualizar_agente_flota', stdout=salida)
        self.assertIn('Alcance: 5 estación(es); 1 ya la reportan.', salida.getvalue())


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
        # Desde el 26-sep-2026 la API exige los mismos codenames que el panel (ver
        # apps/mantenimiento/api_permissions.py); antes alcanzaba con el token.
        self.tecnico.user_permissions.add(
            Permission.objects.get(content_type__app_label='mantenimiento', codename='add_mantenimiento'),
            Permission.objects.get(content_type__app_label='mantenimiento', codename='view_mantenimiento'),
        )
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
        self.assertContains(resp, 'Sin alertas abiertas')

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
        # El rollup por mensaje exige su propio permiso (`pos_errores_flota`), que no viene con
        # view_alerta: desde que la celda lo respeta, sin esto el enlace no se renderiza — que
        # es justo lo que F-07 vino a arreglar.
        self.usuario.user_permissions.add(
            Permission.objects.get(
                content_type__app_label='monitoreo', codename='view_poserrordetectado',
            ),
        )
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
    apps.panel.views.tendencia.tendencia_flota)."""

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

    def _indicador(self, resp, clave):
        """El indicador de una métrica en la respuesta.

        El contexto dejó de exponer `g_cpu` / `cpu_semana_actual` sueltos: cada métrica
        es ahora un `Indicador` (valor actual + estado + umbral + gráfico), el mismo
        objeto que pinta la tarjeta. Se busca por clave para que estas pruebas sigan
        verificando el número que ve el usuario y no una variable intermedia.
        """
        indicadores = list(resp.context['indicadores_alertas']) + list(resp.context['indicadores_recursos'])
        por_clave = {i.clave: i for i in indicadores}
        self.assertIn(clave, por_clave, 'la pantalla no muestra la métrica %s' % clave)
        return por_clave[clave]

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
        self.assertEqual(self._indicador(resp, 'abiertas_warning').valor, 1)
        self.assertEqual(self._indicador(resp, 'abiertas_critical').valor, 1)
        self.assertEqual(resp.context['total_abiertas_periodo'], 2)

    def test_alerta_vieja_sigue_en_el_periodo_pero_no_en_la_semana_actual(self):
        regla = ReglaAlerta.objects.create(
            nombre='CPU alta', metrica=Metrica.CPU_CARGA_PCT, umbral=90, creado_por=self.usuario,
        )
        alerta = Alerta.objects.create(regla=regla, estacion=self.estacion, valor_disparador=95)
        Alerta.objects.filter(pk=alerta.pk).update(abierta_en=timezone.now() - timedelta(weeks=3))

        resp = self.client.get(reverse('panel:tendencia_flota'))
        self.assertEqual(self._indicador(resp, 'abiertas_warning').valor, 0)
        self.assertEqual(resp.context['total_abiertas_periodo'], 1)

    def test_promedio_de_cpu_de_la_flota_esta_semana(self):
        MuestraMetrica.objects.create(estacion=self.estacion, cpu_carga_pct=80)
        MuestraMetrica.objects.create(estacion=self.estacion, cpu_carga_pct=60)
        resp = self.client.get(reverse('panel:tendencia_flota'))
        self.assertEqual(self._indicador(resp, 'cpu_carga_pct').valor, 70)

    def test_promedio_de_red_de_la_flota_esta_semana(self):
        MuestraMetrica.objects.create(estacion=self.estacion, red_recibido_kbps=1000, red_enviado_kbps=200)
        MuestraMetrica.objects.create(estacion=self.estacion, red_recibido_kbps=2000, red_enviado_kbps=400)
        resp = self.client.get(reverse('panel:tendencia_flota'))
        # promedio recibido=(1000+2000)/2=1500, enviado=(200+400)/2=300 -> total 1800
        self.assertEqual(self._indicador(resp, 'red_total_kbps').valor, 1800)

    def test_sin_muestras_esta_semana_no_muestra_el_promedio_de_una_semana_vieja(self):
        # Regresión: construir_grafico().ultimo_valor es el último valor NO NULO de la
        # serie, no necesariamente el de la semana actual — si la semana actual no
        # tiene muestras, no debe mostrarse el promedio de una semana vieja rotulado
        # como "Esta semana".
        MuestraMetrica.objects.create(
            estacion=self.estacion, cpu_carga_pct=55, timestamp=timezone.now() - timedelta(weeks=3),
        )

        resp = self.client.get(reverse('panel:tendencia_flota'))
        cpu = self._indicador(resp, 'cpu_carga_pct')
        self.assertIsNone(cpu.valor)
        self.assertEqual(cpu.grafico.ultimo_valor, 55)  # el gráfico sí sigue mostrando el dato viejo
        self.assertContains(resp, 'Sin datos esta semana')
        self.assertNotContains(resp, '>55<')

    def test_estacion_no_monitoreada_no_afecta_el_promedio(self):
        otra = Estacion.objects.create(
            codigo='ML001-B', farmacia=self.farmacia_sg, estado_aprobacion=Estacion.EstadoAprobacion.APROBADA,
            monitorear_recursos=False,
        )
        MuestraMetrica.objects.create(estacion=self.estacion, cpu_carga_pct=80)
        MuestraMetrica.objects.create(estacion=otra, cpu_carga_pct=0)
        resp = self.client.get(reverse('panel:tendencia_flota'))
        self.assertEqual(self._indicador(resp, 'cpu_carga_pct').valor, 80)

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


class RetornoDeReparacionSegunRolTests(TestCase):
    """F-02: quien puede ENVIAR a reparacion no siempre puede cerrar el mantenimiento.

    `activo_reparacion_enviar` exige `activos.change_activo` y el cierre exige
    `mantenimiento.change_mantenimiento`. Bodeguero tiene el primero y ninguno del modulo de
    Mantenimiento (seed_permisos.py), asi que mandaba el equipo a reparar y el unico boton de
    vuelta le daba 403: el activo quedaba en 'en_reparacion' sin salida desde su rol.

    El precedente del rol Tecnico se resolvio sumandole los permisos; aca no, porque cerrar
    implica checklist y firma. La ficha dice quien cierra en vez de ofrecer un boton muerto.
    """

    def setUp(self):
        from apps.mantenimiento.models import MantenimientoEquipo

        self.bodeguero = User.objects.create_user(username='bodeguero_f02', password='x')
        PerfilUsuario.objects.create(usuario=self.bodeguero, acceso_todas_unidades=True)
        # El set real del rol, de seed_permisos: activos sin nada de mantenimiento.
        for codename in ('view_activo', 'change_activo'):
            self.bodeguero.user_permissions.add(
                Permission.objects.get(content_type__app_label='activos', codename=codename),
            )
        self.activo = Activo.objects.create(
            codigo='CR-DSK-0F02', tipo=Activo.Tipo.DESKTOP, estado=Activo.Estado.EN_REPARACION,
        )
        colaborador = Colaborador.objects.create(nombre='Ana F02', cedula='9F02')
        self.mantenimiento = Mantenimiento.objects.create(
            cliente=colaborador, descripcion='Pantalla rota', fecha_programada=timezone.now(),
        )
        MantenimientoEquipo.objects.create(
            mantenimiento=self.mantenimiento, equipo=self.activo, es_principal=True,
        )
        self.client.force_login(self.bodeguero)

    def _detalle(self):
        return self.client.get(reverse('panel:activo_detalle', args=[self.activo.pk]))

    def test_sin_permiso_de_mantenimiento_no_se_ofrece_el_cierre(self):
        resp = self._detalle()

        self.assertNotContains(
            resp, reverse('panel:mantenimiento_cerrar', args=[self.mantenimiento.pk]),
            msg_prefix='ese boton da 403 para este rol: ofrecerlo es un callejon sin salida',
        )

    def test_en_su_lugar_se_dice_quien_cierra(self):
        """Quitar el boton sin explicar nada deja la ficha muda frente a un activo trabado."""
        self.assertContains(self._detalle(), 'lo hace quien lo atiende')

    def test_tampoco_se_le_ofrece_el_retorno_directo(self):
        """El camino legado no cierra el mantenimiento vinculado: lo dejaria abierto sin
        checklist ni firma. Cambiar el callejon por eso seria cambiarlo por una
        inconsistencia de datos."""
        self.assertNotContains(
            self._detalle(), reverse('panel:activo_reparacion_retorno', args=[self.activo.pk]),
        )

    def test_con_el_permiso_el_boton_sigue_estando(self):
        self.bodeguero.user_permissions.add(
            Permission.objects.get(
                content_type__app_label='mantenimiento', codename='change_mantenimiento',
            ),
        )

        self.assertContains(
            self._detalle(), reverse('panel:mantenimiento_cerrar', args=[self.mantenimiento.pk]),
        )


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

    def test_lista_filtra_por_farmacia(self):
        """El filtro que faltaba. "Solo farmacia (PDV)" separa PDV de administrativo, pero
        con 700 farmacias no servía para la pregunta que uno hace de verdad: qué tiene
        ESTA farmacia. Era el caso de la carga de topología — mirar los activos de ML016
        obligaba a pasar páginas."""
        otra = Farmacia.objects.create(
            codigo='ML099', grupo=self.farmacia.grupo, unidad_negocio=self.sg,
        )
        self.activo.farmacia = self.farmacia
        self.activo.save(update_fields=['farmacia'])
        Activo.objects.create(
            codigo='CR-DSK-0077', tipo=Activo.Tipo.DESKTOP, unidad_negocio=self.sg, farmacia=otra,
        )

        resp = self.client.get(reverse('panel:activos_lista'), {'farmacia': self.farmacia.codigo})
        self.assertContains(resp, 'CR-IMP-0001')
        self.assertNotContains(resp, 'CR-DSK-0077')

    def test_el_filtro_de_farmacia_tambien_busca_por_nombre(self):
        """Nadie recuerda 700 códigos: buscar "machala" tiene que encontrar MMAC1."""
        machala = Farmacia.objects.create(
            codigo='MMAC1', grupo=self.farmacia.grupo, unidad_negocio=self.sg,
            nombre='Machala Centro',
        )
        Activo.objects.create(
            codigo='CR-DSK-0088', tipo=Activo.Tipo.DESKTOP, unidad_negocio=self.sg, farmacia=machala,
        )
        resp = self.client.get(reverse('panel:activos_lista'), {'farmacia': 'machala'})
        self.assertContains(resp, 'CR-DSK-0088')

    def test_el_filtro_de_farmacia_no_rompe_el_aislamiento_entre_clientes(self):
        """Lo que no puede pasar nunca: que un filtro de texto sea una forma de llegar a
        activos de otra unidad de negocio. El scope se aplica antes que el filtro, así que
        buscar el código exacto de una farmacia ajena no devuelve nada.

        Hace falta un usuario ACOTADO: el del resto de la clase tiene
        `acceso_todas_unidades`, y con ese la prueba pasaría por el motivo equivocado.
        """
        mia = UnidadNegocio.objects.get(codigo='MIA')  # sembrada por migración, igual que SG
        farmacia_ajena = Farmacia.objects.create(
            codigo='XX001', grupo=Grupo.objects.create(codigo='TRX999'), unidad_negocio=mia,
        )
        Activo.objects.create(
            codigo='CR-DSK-0099', tipo=Activo.Tipo.DESKTOP, unidad_negocio=mia,
            farmacia=farmacia_ajena,
        )

        acotado = User.objects.create_user(username='u_solo_sg', password='x')
        PerfilUsuario.objects.create(usuario=acotado).unidades_negocio.add(self.sg)
        acotado.user_permissions.add(
            Permission.objects.get(content_type__app_label='activos', codename='view_activo'),
        )
        self.client.force_login(acotado)

        resp = self.client.get(reverse('panel:activos_lista'), {'farmacia': 'XX001'})
        self.assertNotContains(resp, 'CR-DSK-0099')

    def test_sin_filtro_de_farmacia_se_ven_todos(self):
        self.activo.farmacia = self.farmacia
        self.activo.save(update_fields=['farmacia'])
        resp = self.client.get(reverse('panel:activos_lista'), {'farmacia': ''})
        self.assertContains(resp, 'CR-IMP-0001')


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


class SaludEnLaBarraSuperiorTests(TestCase):
    """La barra superior dice el estado REAL de la plataforma, en todas las pantallas.

    Hasta el 10-oct-2026 era un texto fijo —"Todos los sistemas operativos"— con un LED
    verde pintado a mano en `panel/base.html`, para todos los usuarios y sin comprobar
    nada: se mostraba igual mientras el aviso rojo de "Worker MQTT sin señal" estaba más
    abajo en la misma pantalla, contradiciéndola (hallazgo D-06 de
    docs/auditoria/centro-monitoreo/).

    Lo que estas pruebas fijan es lo que hace que se le pueda creer: que el estado se
    calcule, que no se pinte de verde lo que no se puede comprobar, y que decirlo no
    cueste una consulta por componente en cada página del panel.
    """

    def setUp(self):
        self.usuario = User.objects.create_user(username='u_barra', password='x')
        PerfilUsuario.objects.create(usuario=self.usuario, acceso_todas_unidades=True)
        self.usuario.user_permissions.add(
            Permission.objects.get(content_type__app_label='catalogo', codename='view_estacion'),
            Permission.objects.get(content_type__app_label='monitoreo', codename='view_alerta'),
        )
        self.client.force_login(self.usuario)

    def _latido(self, nombre, hace_segundos=1, conectado=None):
        from apps.mqtt_worker.models import WorkerHeartbeat

        WorkerHeartbeat.objects.update_or_create(
            nombre=nombre,
            defaults={
                'ultimo_latido': timezone.now() - timedelta(seconds=hace_segundos),
                'conectado': conectado,
            },
        )

    def _sano(self):
        """Todo verificado: los workers latiendo Y conectados, Beat vivo, respaldo reciente.

        `conectado=True` no es un detalle del montaje: desde la Fase D un worker que late
        sin reportar su conexión al broker queda en ámbar a propósito —"sin verificar"— y
        eso no es "todo sano". Es el caso de un worker anterior a ese campo, y tiene su
        propia prueba más abajo.
        """
        self._latido('mqtt_worker', conectado=True)
        self._latido('meshcentral_worker', conectado=True)
        self._latido('celery_beat')
        self._latido('respaldo')
        self._latido('respaldo_externo')

    def test_nunca_afirma_que_todo_esta_operativo(self):
        """La frase vieja no vuelve: afirmaba algo sobre TODO, incluida la flota, que
        esta barra no mira."""
        self._sano()
        resp = self.client.get(reverse('panel:dashboard'))
        self.assertNotContains(resp, 'Todos los sistemas operativos')

    def test_con_todo_sano_lo_dice_y_nombra_su_alcance(self):
        self._sano()
        resp = self.client.get(reverse('panel:dashboard'))
        self.assertEqual(resp.context['salud_plataforma']['estado'], 'ok')
        self.assertContains(resp, 'Plataforma verificada')

    def test_con_el_worker_caido_la_barra_queda_degradada(self):
        """Es el caso que el texto fijo tapaba: el aviso rojo estaba en la pantalla y la
        barra seguía diciendo que todo estaba bien."""
        self._sano()
        self._latido('mqtt_worker', hace_segundos=600, conectado=True)
        resp = self.client.get(reverse('panel:dashboard'))
        self.assertEqual(resp.context['salud_plataforma']['estado'], 'critical')
        self.assertContains(resp, 'Plataforma degradada')
        self.assertContains(resp, 'Worker MQTT')

    def test_la_barra_esta_en_todas_las_pantallas_del_panel(self):
        """No es un adorno del tablero: el valor de este indicador es que esté donde sea
        que el operador estuviera mirando cuando algo se cayó."""
        self._sano()
        self._latido('mqtt_worker', hace_segundos=600, conectado=True)
        for nombre in ('panel:estaciones_lista', 'panel:alertas_lista', 'panel:notificaciones_lista'):
            with self.subTest(pantalla=nombre):
                self.assertContains(self.client.get(reverse(nombre)), 'Plataforma degradada')

    def test_sin_quien_reporte_la_conexion_el_broker_no_se_pinta_de_verde(self):
        """Un componente que no se puede comprobar va en gris con la palabra "sin
        verificar": que falte la comprobación es información útil, y pintarlo verde sería
        afirmar algo que nadie midió.

        No cuenta como problema, porque una barra permanentemente en rojo informa tan poco
        como una permanentemente en verde.
        """
        # Nadie reportó su conexión: es un worker anterior al campo `conectado`.
        self._latido('mqtt_worker')
        self._latido('meshcentral_worker')
        self._latido('celery_beat')
        self._latido('respaldo')

        resp = self.client.get(reverse('panel:dashboard'))
        salud = resp.context['salud_plataforma']
        self.assertContains(resp, 'sin verificar')

        broker = [c for c in salud['componentes'] if c.clave == 'broker_mqtt']
        self.assertEqual(len(broker), 1)
        self.assertEqual(broker[0].estado, 'sin_dato')
        self.assertFalse(broker[0].verificable)

    def test_un_worker_que_late_sin_reportar_conexion_no_dispara_el_aviso_rojo(self):
        """El aviso afirma que NO se están procesando despliegues ni latidos, y con un
        worker vivo eso es falso. Pasaría en la ventana de cualquier despliegue, con el
        worker viejo corriendo y el web ya nuevo."""
        self._latido('mqtt_worker')  # late, conectado=None
        self._latido('meshcentral_worker', conectado=True)
        self._latido('celery_beat')
        self._latido('respaldo')

        resp = self.client.get(reverse('panel:dashboard'))
        self.assertTrue(resp.context['worker_mqtt_activo'])
        self.assertNotContains(resp, 'Worker MQTT sin señal')
        # Pero la barra lo dice, en ámbar: se comprobó a medias.
        self.assertEqual(resp.context['salud_plataforma']['estado'], 'warning')

    def test_un_worker_desconectado_del_broker_si_dispara_el_aviso_rojo(self):
        """Es el estado que antes se veía en verde: el proceso vive y no llega nada."""
        self._sano()
        self._latido('mqtt_worker', conectado=False)

        resp = self.client.get(reverse('panel:dashboard'))
        self.assertFalse(resp.context['worker_mqtt_activo'])
        self.assertContains(resp, 'Worker MQTT sin señal')

    def test_el_estado_no_se_comunica_solo_con_color(self):
        """Bajo deuteranopía verde y ámbar se acercan. Mismo criterio que
        `panel/_estado_chip.html`: color, forma Y palabra."""
        self._sano()
        self._latido('mqtt_worker', hace_segundos=600, conectado=True)
        resp = self.client.get(reverse('panel:dashboard'))
        contenido = resp.content.decode()
        # La palabra viaja en el HTML, no en una hoja de estilos.
        self.assertIn('Plataforma degradada', contenido)

    def test_cuesta_una_sola_consulta(self):
        """Corre en TODAS las páginas del panel, así que el presupuesto es una consulta.

        Se mide el context processor directamente y no la diferencia entre dos cargas de
        página: esa resta se mueve sola por cosas que no tienen nada que ver con la barra
        —la sesión se reescribe en unas peticiones y no en otras— y una prueba de
        rendimiento que falla por azar se acaba borrando.

        Un componente nuevo que necesite su propia consulta hace fallar esto, y eso es
        exactamente lo que tiene que pasar: la barra se dibuja en cada pantalla.
        """
        from django.test import RequestFactory

        from apps.panel.context_processors import salud_plataforma_contexto

        self._sano()
        peticion = RequestFactory().get('/')
        peticion.user = self.usuario

        with self.assertNumQueries(1):
            contexto = salud_plataforma_contexto(peticion)

        self.assertEqual(contexto['salud_plataforma']['estado'], 'ok')

    def test_sin_sesion_no_cuesta_ninguna_consulta(self):
        """`base.html` solo dibuja la barra para un usuario autenticado: la pantalla de
        login no debe pagar una consulta por un elemento que no se dibuja."""
        from django.contrib.auth.models import AnonymousUser
        from django.test import RequestFactory

        from apps.panel.context_processors import salud_plataforma_contexto

        peticion = RequestFactory().get('/')
        peticion.user = AnonymousUser()

        with self.assertNumQueries(0):
            self.assertEqual(salud_plataforma_contexto(peticion), {})


class DashboardZonaDeConfianzaTests(TestCase):
    """La Zona 0 del tablero: qué se comprobó, y de cuándo es el dato.

    El tablero no tenía forma de decir de cuándo era lo que mostraba (hallazgo D-07): un
    tablero en verde con datos viejos es peor que no tener tablero, y el barrido de
    enlaces ABORTA completo si falla el 80 % o más —protección correcta— sin escribir
    nada, así que la pantalla seguía mostrando los últimos estados buenos como si fueran
    de ahora. Pasó: 4,7 h sin un solo sondeo el 2-oct-2026.

    Las cuatro señales de frescura salen de datos que ya se persistían y que no se veían
    en ninguna pantalla del panel.
    """

    def setUp(self):
        self.usuario = User.objects.create_user(username='u_zona0', password='x')
        PerfilUsuario.objects.create(usuario=self.usuario, acceso_todas_unidades=True)
        self.client.force_login(self.usuario)

    def test_sin_ninguna_estacion_el_latido_de_la_flota_no_se_pinta_de_verde(self):
        """"Nadie reportó nunca" no es lo mismo que "todo al día"."""
        resp = self.client.get(reverse('panel:dashboard'))
        self.assertIsNone(resp.context['ultimo_latido_flota'])
        self.assertTrue(resp.context['latido_flota_vencido'])
        self.assertContains(resp, 'ninguna estación reportó todavía')

    def test_muestra_el_ultimo_latido_recibido_de_la_flota(self):
        """Es la señal más barata que prueba que la cadena agente -> broker -> worker ->
        base sigue entregando. El latido del worker no lo prueba: lo escribe un hilo
        propio aunque no llegue un solo mensaje.
        """
        grupo = Grupo.objects.create(codigo='TRXZ0')
        farmacia = Farmacia.objects.create(
            codigo='ZZ001', grupo=grupo, unidad_negocio=UnidadNegocio.objects.get(codigo='SG'),
        )
        Estacion.objects.create(
            codigo='ZZ001-A', farmacia=farmacia,
            estado_aprobacion=Estacion.EstadoAprobacion.APROBADA,
            ultimo_heartbeat=timezone.now(),
        )
        resp = self.client.get(reverse('panel:dashboard'))
        self.assertIsNotNone(resp.context['ultimo_latido_flota'])
        self.assertFalse(resp.context['latido_flota_vencido'])

    def test_un_latido_viejo_de_la_flota_queda_marcado_como_vencido(self):
        grupo = Grupo.objects.create(codigo='TRXZ1')
        farmacia = Farmacia.objects.create(
            codigo='ZZ002', grupo=grupo, unidad_negocio=UnidadNegocio.objects.get(codigo='SG'),
        )
        Estacion.objects.create(
            codigo='ZZ002-A', farmacia=farmacia,
            estado_aprobacion=Estacion.EstadoAprobacion.APROBADA,
            ultimo_heartbeat=timezone.now() - timedelta(hours=3),
        )
        resp = self.client.get(reverse('panel:dashboard'))
        self.assertTrue(resp.context['latido_flota_vencido'])

    def test_los_mensajes_mqtt_fallidos_se_ven_en_el_panel(self):
        """La cola de lo que el worker recibió y NO pudo procesar existía desde que existe
        el worker y solo se podía mirar desde el admin de Django (hallazgo D-11). Es la
        prueba directa de "recibe pero no procesa"."""
        from apps.mqtt_worker.models import MensajeMqttFallido

        MensajeMqttFallido.objects.create(
            topico='/saidsof/agente/ZZ001-A/heartbeat/', error='Payload no es JSON válido',
        )
        MensajeMqttFallido.objects.create(topico='/otro/', error='x', revisado=True)

        resp = self.client.get(reverse('panel:dashboard'))
        self.assertEqual(resp.context['mqtt_fallidos'], 1)
        self.assertContains(resp, 'Mensajes MQTT fallidos')

    def test_los_enrolamientos_rechazados_son_alcanzables_desde_el_tablero(self):
        """La pantalla existe desde el 27-sep-2026 y NINGUNA plantilla la enlazaba: para
        llegar había que escribir la URL a mano (hallazgo D-10). Es el caso de "se instaló
        el agente, el técnico se fue, y la estación nunca apareció en el panel"."""
        from apps.mqtt_worker.models import EnrolamientoRechazado

        EnrolamientoRechazado.objects.create(codigo_recibido='MLO16-A', hostname='CAJA1')

        resp = self.client.get(reverse('panel:dashboard'))
        self.assertEqual(resp.context['enrolamientos_rechazados'], 1)
        self.assertContains(resp, reverse('panel:enrolamientos_rechazados_lista'))

    def test_la_frescura_del_sondeo_de_enlaces_se_declara(self):
        """Sin sondeos, el bloque no puede afirmar que la conectividad esté bien."""
        resp = self.client.get(reverse('panel:dashboard'))
        self.assertTrue(resp.context['sondeo_enlaces_vencido'])
        self.assertContains(resp, 'ninguna farmacia sondeada')


class DashboardJerarquiaTests(TestCase):
    """Lo urgente va antes que lo que se planifica.

    El orden era: avisos, 5 KPI, matriz de cumplimiento (a todo el ancho), enlaces
    caídos, alertas, estaciones, despliegues. O sea que lo primero que se leía era el
    cumplimiento de versión del POS —planificación de despliegues, que cambia de día en
    día— y una farmacia sin vender quedaba en el tercio inferior (hallazgo U-2).
    """

    def setUp(self):
        self.usuario = User.objects.create_user(username='u_jerarquia', password='x')
        PerfilUsuario.objects.create(usuario=self.usuario, acceso_todas_unidades=True)
        self.client.force_login(self.usuario)
        self.sg = UnidadNegocio.objects.get(codigo='SG')
        self.grupo = Grupo.objects.create(codigo='TRXJ1', version_objetivo='4.2.1')
        self.farmacia = Farmacia.objects.create(
            codigo='JJ001', grupo=self.grupo, unidad_negocio=self.sg, ip_router='10.80.0.1',
            tipo_enlace='TELCONET',
        )

    def _posicion(self, contenido, texto):
        indice = contenido.find(texto)
        self.assertNotEqual(indice, -1, 'No se encontró %r en la pantalla' % texto)
        return indice

    def test_la_confianza_en_el_dato_va_antes_que_los_indicadores(self):
        """Si el dato está viejo, todo lo que sigue se lee con esa salvedad. Verlo después
        es verlo tarde."""
        contenido = self.client.get(reverse('panel:dashboard')).content.decode()
        self.assertLess(
            self._posicion(contenido, 'Confianza en el dato'),
            self._posicion(contenido, 'Estaciones en línea'),
        )

    def test_el_cumplimiento_va_despues_de_las_incidencias(self):
        from apps.monitoreo.models import EstadoEnlaceFarmacia, EventoEnlaceFarmacia

        EstadoEnlaceFarmacia.objects.create(
            farmacia=self.farmacia, alcanzable=False, respondio_alguna_vez=True,
            fallas_consecutivas=3, ultima_verificacion=timezone.now(),
        )
        EventoEnlaceFarmacia.objects.create(
            farmacia=self.farmacia, inicio=timezone.now() - timedelta(hours=2),
        )

        contenido = self.client.get(reverse('panel:dashboard')).content.decode()
        self.assertLess(
            self._posicion(contenido, 'Lo que duele ahora'),
            self._posicion(contenido, 'Cumplimiento por grupo'),
        )

    def test_el_cumplimiento_sigue_disponible_pero_plegado_y_diferido(self):
        """No se quitó nada: se movió al final, cerrado, y se carga al abrirse.

        `<details>` es nativo y accesible con teclado, sin una línea de JavaScript. Lo que
        la portada ya NO paga son los 5 Count(DISTINCT) de la matriz: el contenido llega
        cuando alguien la abre (hallazgo R-4).
        """
        contenido = self.client.get(reverse('panel:dashboard')).content.decode()
        self.assertIn('Cumplimiento por grupo', contenido)
        self.assertIn('<details', contenido)
        self.assertIn(reverse('panel:dashboard_cumplimiento_partial'), contenido)
        self.assertNotIn('TRXJ1', contenido, 'la matriz no debería venir en la carga inicial')

        # Y al abrirlo, está completa.
        abierto = self.client.get(reverse('panel:dashboard_cumplimiento_partial'))
        self.assertContains(abierto, 'TRXJ1')

    def test_el_tablero_no_ofrece_publicar_software_como_accion_principal(self):
        """La acción destacada de un tablero de monitoreo no es publicar un despliegue
        (hallazgo U-9). Sigue existiendo en la pantalla de Despliegues."""
        contenido = self.client.get(reverse('panel:dashboard')).content.decode()
        self.assertNotIn('+ Nuevo despliegue', contenido)

    def test_no_repite_el_total_de_estaciones_en_dos_tarjetas(self):
        """"Estaciones en línea 55/66" y "Total de estaciones 66" eran cinco tarjetas para
        tres datos (hallazgo U-3)."""
        contenido = self.client.get(reverse('panel:dashboard')).content.decode()
        self.assertNotIn('Total de estaciones', contenido)


class TableroRefrescoYCostoTests(TestCase):
    """El tablero se mantiene solo, dice de cuándo es el dato, y cuesta menos que antes.

    Lo que había: una pantalla estática, sin auto-refresco y sin ninguna marca de hora, que
    además pagaba en cada carga los 5 `Count(DISTINCT)` de la matriz de cumplimiento —la
    tabla menos urgente de la pantalla— y repetía el mismo filtro dos veces para contar y
    para listar (hallazgos D-07, R-1 y R-4).

    Estas pruebas fijan el presupuesto. Un `assertNumQueries` que falla al agregar una
    consulta es molesto a propósito: es la única forma de que el coste de esta pantalla no
    crezca sin que nadie lo note, y hasta hoy había UNO en todo el archivo.
    """

    def setUp(self):
        self.sg = UnidadNegocio.objects.get(codigo='SG')
        self.grupo = Grupo.objects.create(codigo='TRXREF', version_objetivo='4.2.1')
        self.farmacia = Farmacia.objects.create(
            codigo='RF001', grupo=self.grupo, unidad_negocio=self.sg, ip_router='10.70.0.1',
            tipo_enlace='TELCONET',
        )
        Estacion.objects.create(
            codigo='RF001-A', farmacia=self.farmacia,
            estado_aprobacion=Estacion.EstadoAprobacion.APROBADA,
            ultimo_heartbeat=timezone.now(),
        )
        self.usuario = User.objects.create_user(username='u_refresco', password='x')
        PerfilUsuario.objects.create(usuario=self.usuario, acceso_todas_unidades=True)
        self.client.force_login(self.usuario)

    def test_el_interior_se_refresca_solo(self):
        from apps.panel.views.dashboard import SEGUNDOS_REFRESCO_TABLERO

        resp = self.client.get(reverse('panel:dashboard'))
        self.assertContains(resp, reverse('panel:dashboard_vivo_partial'))
        self.assertContains(resp, 'every %ds' % SEGUNDOS_REFRESCO_TABLERO)

    def test_el_polling_no_es_mas_rapido_que_el_backend(self):
        """Pollear cada 10 s un dato que se recalcula cada 60 mostraría seis veces lo
        mismo, multiplicado por cada persona que deja la pantalla abierta. Mismo criterio
        que el Centro, que ya tiene su propia versión de esta prueba."""
        from apps.panel.views.dashboard import SEGUNDOS_REFRESCO_TABLERO

        self.assertGreaterEqual(SEGUNDOS_REFRESCO_TABLERO, 60)

    def test_dice_la_hora_del_calculo_y_la_de_cada_dato(self):
        """Son dos relojes distintos y los dos tienen que estar visibles: "calculado" dice
        cuándo corrió la consulta, y cada fila de la Zona 0 dice cuándo llegó SU dato."""
        resp = self.client.get(reverse('panel:dashboard'))
        self.assertContains(resp, 'Calculado')
        self.assertIsNotNone(resp.context['calculado_en'])
        self.assertContains(resp, 'Último latido de la flota')

    def test_avisa_si_un_refresco_falla_en_vez_de_dejar_el_dato_viejo_sin_marca(self):
        """HTMX, ante un error, deja el contenido anterior tal cual. Sin este aviso el
        operador sigue mirando datos viejos creyéndolos actuales — el mismo defecto que
        `fuente_desactualizada` resuelve para los datos y que nadie había resuelto para el
        transporte."""
        resp = self.client.get(reverse('panel:dashboard'))
        self.assertContains(resp, 'Sin contacto con el servidor')
        self.assertContains(resp, 'htmx:responseError')
        self.assertContains(resp, 'htmx:sendError')

    def test_la_carga_inicial_no_paga_la_matriz_de_cumplimiento(self):
        """Era lo más caro de la pantalla y estaba en la primera posición. Ahora se carga
        cuando alguien abre el bloque."""
        self.client.get(reverse('panel:dashboard'))  # calienta sesión y permisos

        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        with CaptureQueriesContext(connection) as consultas:
            self.client.get(reverse('panel:dashboard'))

        sql = ' '.join(q['sql'] for q in consultas.captured_queries)
        self.assertNotIn('"grupo"', sql, 'la matriz de cumplimiento se sigue calculando en la portada')

    def test_el_parcial_vivo_cuesta_menos_que_la_pagina_completa(self):
        """Es el punto del patrón marco + parcial: a un refresco por minuto por persona,
        mandar la navegación, el CSS y la cabecera cada vez es trabajo puro."""
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        self.client.get(reverse('panel:dashboard'))
        self.client.get(reverse('panel:dashboard_vivo_partial'))

        with CaptureQueriesContext(connection) as completa:
            self.client.get(reverse('panel:dashboard'))
        with CaptureQueriesContext(connection) as parcial:
            self.client.get(reverse('panel:dashboard_vivo_partial'))

        self.assertLessEqual(
            len(parcial), len(completa),
            'el refresco no puede costar más que la carga completa',
        )

    def test_los_dos_parciales_son_de_solo_lectura(self):
        """Coherente con el Centro y con el bot: una pantalla que se mira de reojo no
        puede tener una acción destructiva a un clic."""
        for nombre in ('panel:dashboard_vivo_partial', 'panel:dashboard_cumplimiento_partial'):
            with self.subTest(parcial=nombre):
                self.assertIn(self.client.post(reverse(nombre)).status_code, (403, 405))

    def test_el_parcial_no_arrastra_la_navegacion(self):
        """Si devolviera la página entera, el refresco anidaría el panel dentro de sí
        mismo cada minuto."""
        resp = self.client.get(reverse('panel:dashboard_vivo_partial'))
        contenido = resp.content.decode()
        self.assertNotIn('<aside class="sidebar"', contenido)
        self.assertIn('Confianza en el dato', contenido)

    def test_el_parcial_respeta_el_alcance_por_cliente(self):
        """El refresco pasa por la misma puerta que la página: un parcial que se olvide
        del scope es una fuga de datos de otro cliente cada 60 segundos."""
        mia = UnidadNegocio.objects.get(codigo='MIA')
        otra = Farmacia.objects.create(codigo='RF999', grupo=self.grupo, unidad_negocio=mia)
        Estacion.objects.create(
            codigo='RF999-A', farmacia=otra,
            estado_aprobacion=Estacion.EstadoAprobacion.APROBADA,
        )

        solo_sg = User.objects.create_user(username='u_refresco_sg', password='x')
        PerfilUsuario.objects.create(usuario=solo_sg, acceso_todas_unidades=False).unidades_negocio.add(self.sg)
        self.client.force_login(solo_sg)

        resp = self.client.get(reverse('panel:dashboard_vivo_partial'))
        self.assertNotContains(resp, 'RF999')
        self.assertEqual(resp.context['flota']['total'], 1)

    def test_exige_sesion(self):
        self.client.logout()
        for nombre in ('panel:dashboard_vivo_partial', 'panel:dashboard_cumplimiento_partial'):
            with self.subTest(parcial=nombre):
                resp = self.client.get(reverse(nombre))
                self.assertEqual(resp.status_code, 302)
                self.assertIn(reverse('panel:login'), resp['Location'])


class DashboardTableroTests(TestCase):
    """Las dos listas del tablero: alertas abiertas y estaciones que no reportan.

    El dashboard decía "8 alertas abiertas" y "8/8 en línea" sin decir cuáles, así que
    terminaba a media pantalla y obligaba a salir a otras dos pantallas para empezar a
    entender. Lo que estas pruebas fijan es el recorte, que es donde está el riesgo:
    que no se filtre una estación de otro cliente y que una pendiente de aprobación no
    cuente como caída.
    """

    def setUp(self):
        self.sg = UnidadNegocio.objects.get(codigo='SG')
        self.mia = UnidadNegocio.objects.get(codigo='MIA')
        grupo = Grupo.objects.create(codigo='TRX900')
        self.farmacia_sg = Farmacia.objects.create(codigo='ML900', grupo=grupo, unidad_negocio=self.sg)
        self.farmacia_mia = Farmacia.objects.create(codigo='MM900', grupo=grupo, unidad_negocio=self.mia)

        self.viva = Estacion.objects.create(
            codigo='ML900-A', farmacia=self.farmacia_sg,
            estado_aprobacion=Estacion.EstadoAprobacion.APROBADA,
            ultimo_heartbeat=timezone.now(),
        )
        self.callada = Estacion.objects.create(
            codigo='ML900-B', farmacia=self.farmacia_sg,
            estado_aprobacion=Estacion.EstadoAprobacion.APROBADA,
            ultimo_heartbeat=timezone.now() - timedelta(hours=3),
        )
        self.nunca_reporto = Estacion.objects.create(
            codigo='ML900-C', farmacia=self.farmacia_sg,
            estado_aprobacion=Estacion.EstadoAprobacion.APROBADA,
        )
        self.pendiente = Estacion.objects.create(
            codigo='ML900-D', farmacia=self.farmacia_sg,
            estado_aprobacion=Estacion.EstadoAprobacion.PENDIENTE,
        )
        self.de_otro_cliente = Estacion.objects.create(
            codigo='MM900-A', farmacia=self.farmacia_mia,
            estado_aprobacion=Estacion.EstadoAprobacion.APROBADA,
        )

        self.usuario = User.objects.create_user(username='u_tablero', password='x')
        PerfilUsuario.objects.create(usuario=self.usuario).unidades_negocio.add(self.sg)
        self.client.force_login(self.usuario)

    def _codigos_sin_reportar(self):
        resp = self.client.get(reverse('panel:dashboard'))
        self.assertEqual(resp.status_code, 200)
        return [e.codigo for e in resp.context['sin_reportar']], resp

    def test_lista_las_calladas_y_no_la_que_reporta(self):
        codigos, _ = self._codigos_sin_reportar()
        self.assertIn('ML900-B', codigos)
        self.assertIn('ML900-C', codigos)
        self.assertNotIn('ML900-A', codigos)

    def test_una_pendiente_de_aprobacion_no_cuenta_como_caida(self):
        """Todavía no le toca reportar, y ya tiene su propio aviso arriba."""
        codigos, _ = self._codigos_sin_reportar()
        self.assertNotIn('ML900-D', codigos)

    def test_no_se_filtra_una_estacion_de_otro_cliente(self):
        codigos, resp = self._codigos_sin_reportar()
        self.assertNotIn('MM900-A', codigos)
        self.assertNotContains(resp, 'MM900-A')

    def test_la_que_nunca_reporto_va_primero(self):
        """Una aprobada sin un solo latido es una instalación fallida, no una caída."""
        codigos, _ = self._codigos_sin_reportar()
        self.assertEqual(codigos[0], 'ML900-C')

    def test_el_total_cuenta_todas_aunque_se_muestren_seis(self):
        for n in range(8):
            Estacion.objects.create(
                codigo='ML900-X%d' % n, farmacia=self.farmacia_sg,
                estado_aprobacion=Estacion.EstadoAprobacion.APROBADA,
            )
        resp = self.client.get(reverse('panel:dashboard'))
        self.assertEqual(resp.context['total_sin_reportar'], 10)
        self.assertEqual(len(resp.context['sin_reportar']), 6)

    def test_las_alertas_entran_en_la_cola_sin_las_resueltas_ni_las_de_otro_cliente(self):
        """Antes esto miraba `context['alertas_recientes']`, la lista por dominio que el
        tablero tenía al lado de la de enlaces. Desde el 10-oct-2026 las dos se fusionaron
        en la cola de incidencias, porque una farmacia con el enlace caído aparecía en las
        dos Y en "estaciones sin reportar" por un único hecho.

        El recorte que se vigila es el mismo de siempre, y es donde está el riesgo: que no
        se cuele una alerta resuelta ni una de un cliente al que el usuario no tiene
        acceso.
        """
        regla = ReglaAlerta.objects.create(
            nombre='CPU sostenida', metrica=Metrica.CPU_CARGA_PCT,
            operador=ReglaAlerta.Operador.GTE, umbral=90, duracion_minutos=5,
            severidad=ReglaAlerta.Severidad.CRITICAL, creado_por=self.usuario,
        )
        Alerta.objects.create(regla=regla, estacion=self.callada, valor_disparador=95)
        Alerta.objects.create(
            regla=regla, estacion=self.callada, valor_disparador=99, estado=Alerta.Estado.RESUELTA,
        )
        Alerta.objects.create(regla=regla, estacion=self.de_otro_cliente, valor_disparador=97)

        resp = self.client.get(reverse('panel:dashboard'))
        incidencias = list(resp.context['incidencias'])
        self.assertEqual(len(incidencias), 1)
        self.assertEqual(incidencias[0].farmacia.codigo, 'ML900')
        self.assertContains(resp, 'CPU sostenida')
        self.assertNotContains(resp, 'MM900')


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

    def _permitir_resumen_cliente(self):
        """El permiso que `reporte_cliente_resumen` exige desde el 10-oct-2026."""
        self.usuario_mia.user_permissions.add(
            Permission.objects.get(
                content_type__app_label='facturacion', codename='view_actividadmensualestacion',
            ),
        )

    def test_reporte_cliente_resumen_incluye_kpis_de_mantenimiento(self):
        self._permitir_resumen_cliente()
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

    def test_reporte_cliente_resumen_requiere_permiso(self):
        """Era la unica vista de este modulo abierta a cualquier autenticado (F-01).

        Los otros seis reportes ya exigian el permiso de su dominio, asi que el criterio
        existia y a esta se le habia pasado. `verificar_acceso` no cubria el hueco: acota de
        que cliente son los datos, no quien tiene derecho al informe — y el informe trae los
        endpoints facturables del mes, que no salen de ningun otro lado del panel.
        """
        resp = self.client.get(reverse('panel:reporte_cliente_resumen'), {'unidad_negocio': self.mia.pk})
        self.assertEqual(resp.status_code, 403)

        self._permitir_resumen_cliente()
        resp = self.client.get(reverse('panel:reporte_cliente_resumen'), {'unidad_negocio': self.mia.pk})
        self.assertEqual(resp.status_code, 200)

    def test_el_indice_no_ofrece_el_resumen_a_quien_no_puede_abrirlo(self):
        """La pantalla existe para encontrar reportes: ofrecer uno que da 403 es un camino
        cortado, no una restriccion."""
        resp = self.client.get(reverse('panel:reportes_index'))
        self.assertNotContains(resp, reverse('panel:reporte_cliente_resumen'))

        self._permitir_resumen_cliente()
        resp = self.client.get(reverse('panel:reportes_index'))
        self.assertContains(resp, reverse('panel:reporte_cliente_resumen'))

    def test_reporte_cumplimiento_csv_sin_parametro_no_muestra_todo(self):
        resp = self.client.get(reverse('panel:reporte_cumplimiento_csv'))
        contenido = resp.content.decode('utf-8-sig')
        self.assertIn('MAM01-A', contenido)
        self.assertNotIn('ML001-A', contenido)

    def test_reporte_cliente_resumen_de_otro_tenant_404(self):
        # _resolver_unidad_negocio busca dentro del queryset ya escopado a lo visible
        # (get_object_or_404(visibles, ...)) — para un tenant ajeno eso es un 404, no
        # un 403: no confirma que la unidad de negocio exista, simplemente no aparece.
        self._permitir_resumen_cliente()
        resp = self.client.get(reverse('panel:reporte_cliente_resumen'), {'unidad_negocio': self.sg.pk})
        self.assertEqual(resp.status_code, 404)

    def test_reporte_cliente_resumen_propio_no_filtra_datos_ajenos(self):
        self._permitir_resumen_cliente()
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

    def test_el_conteo_de_mantenimientos_no_cuesta_una_consulta_por_fila(self):
        """F-15: la columna salia de `{{ v.mantenimientos_generados.count }}`, que sobre un
        manager inverso emite un SELECT COUNT(*) por acceso — 25 consultas extra por carga con
        la paginacion, y otras 25 por cada cambio de pagina.

        Se afirma lo que importa y no un numero exacto: que el costo NO crezca con las filas.
        Un numero fijo se rompe con cualquier consulta que se agregue a la pantalla por otro
        motivo, y entonces el test deja de hablar del N+1.
        """
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        from apps.mantenimiento.models import VisitaTecnica

        def crear(n):
            for i in range(n):
                VisitaTecnica.objects.create(
                    farmacia=self.farmacia, tecnico=self.usuario,
                    fecha_planificada=date(2026, 9, 15), motivo='relevamiento %d' % i,
                )

        crear(1)
        with CaptureQueriesContext(connection) as con_una:
            self.client.get(reverse('panel:visita_tecnica_lista'))

        crear(9)
        with CaptureQueriesContext(connection) as con_diez:
            self.client.get(reverse('panel:visita_tecnica_lista'))

        self.assertEqual(
            len(con_diez.captured_queries), len(con_una.captured_queries),
            'el costo de la pantalla crece con las filas: volvio el N+1',
        )

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


class MenuLateralSegunPermisosTests(TestCase):
    """El menú listaba las 20 secciones a cualquiera con sesión iniciada: al entrar
    daban 403 y parecía una falla del sistema, no una restricción. Ahora cada ítem
    (y el encabezado de su sección) va detrás del mismo permiso que exige la vista."""

    def _usuario(self, nombre, *permisos):
        usuario = User.objects.create_user(username=nombre, password='x')
        PerfilUsuario.objects.create(usuario=usuario, acceso_todas_unidades=True)
        for etiqueta, codename in permisos:
            usuario.user_permissions.add(
                Permission.objects.get(content_type__app_label=etiqueta, codename=codename),
            )
        return usuario

    def test_solo_aparece_la_seccion_que_el_usuario_puede_abrir(self):
        self.client.force_login(
            self._usuario('u_menu_mant', ('mantenimiento', 'view_mantenimiento')),
        )
        html = self.client.get(reverse('panel:mantenimientos_lista')).content.decode()

        self.assertIn('<span class="lbl">Mantenimientos</span>', html)
        # Sin el permiso correspondiente no se dibuja ni el ítem ni el encabezado de
        # la sección, que quedaría suelto sobre nada.
        self.assertNotIn('<span class="lbl">Estaciones</span>', html)
        self.assertNotIn('<span class="lbl">Inventario</span>', html)
        self.assertNotIn('>Activos<', html)
        self.assertNotIn('>Operación<', html)

    def test_lo_comun_a_todos_no_se_oculta(self):
        """Dashboard y Notificaciones son de cualquier usuario con sesión: no dependen
        de permiso sobre ningún modelo, así que filtrarlos dejaría el menú vacío."""
        self.client.force_login(
            self._usuario('u_menu_basico', ('mantenimiento', 'view_mantenimiento')),
        )
        html = self.client.get(reverse('panel:mantenimientos_lista')).content.decode()

        self.assertIn('<span class="lbl">Dashboard</span>', html)
        self.assertIn('<span class="lbl">Notificaciones</span>', html)

    def test_con_todos_los_permisos_se_ve_todo(self):
        usuario = self._usuario('u_menu_super')
        usuario.is_superuser = True
        usuario.save(update_fields=['is_superuser'])
        self.client.force_login(usuario)
        html = self.client.get(reverse('panel:mantenimientos_lista')).content.decode()

        for etiqueta in ('Estaciones', 'Inventario', 'Mantenimientos', 'Bodegas y stock'):
            self.assertIn(f'<span class="lbl">{etiqueta}</span>', html)


class ViaticosPanelTests(TestCase):
    """Bandeja de aprobación: permisos, aislamiento por tenant y el control de que una
    alerta de zona/tope no se pueda aprobar en un clic."""

    def setUp(self):
        from apps.viaticos import services as viaticos_services
        from apps.viaticos.models import ColaboradorZona, ReporteViatico, RubroViatico

        self.sg = UnidadNegocio.objects.get(codigo='SG')
        self.mia_un = UnidadNegocio.objects.get(codigo='MIA')
        grupo = Grupo.objects.create(codigo='TRX001', version_objetivo='4.2.1')
        self.farmacia_sg = Farmacia.objects.create(codigo='ML006', grupo=grupo, unidad_negocio=self.sg)
        self.farmacia_ajena = Farmacia.objects.create(codigo='ML099', grupo=grupo, unidad_negocio=self.sg)

        self.tecnico = Colaborador.objects.create(
            nombre='Ana Pérez', cedula='0912345678', unidad_negocio=self.sg,
        )
        zona = ColaboradorZona.objects.create(colaborador=self.tecnico, zona_cobertura='Machala Norte')
        zona.farmacias_asignadas.add(self.farmacia_sg)

        # Reporte con las dos alertas que exigen justificación para aprobarse.
        self.reporte = ReporteViatico.objects.create(
            colaborador=self.tecnico, fecha=date(2026, 9, 10), farmacia_visitada=self.farmacia_ajena,
            rubro=RubroViatico.HOSPEDAJE, monto=Decimal('45.00'),
        )
        viaticos_services.evaluar_alertas(self.reporte)

        self.coordinador = self._usuario(
            'coordinador_v', True, 'view_reporteviatico', 'change_reporteviatico',
        )

    def _usuario(self, nombre, acceso_total, *codenames, unidades=()):
        usuario = User.objects.create_user(username=nombre, password='x')
        perfil = PerfilUsuario.objects.create(usuario=usuario, acceso_todas_unidades=acceso_total)
        for unidad in unidades:
            perfil.unidades_negocio.add(unidad)
        for codename in codenames:
            usuario.user_permissions.add(
                Permission.objects.get(content_type__app_label='viaticos', codename=codename),
            )
        return usuario

    def test_bandeja_sin_permiso_devuelve_403(self):
        self.client.force_login(self._usuario('sin_permiso_v', True))
        self.assertEqual(self.client.get(reverse('panel:viaticos_bandeja')).status_code, 403)

    def test_bandeja_muestra_el_reporte_y_sus_alertas(self):
        self.client.force_login(self.coordinador)
        html = self.client.get(reverse('panel:viaticos_bandeja')).content.decode()
        self.assertIn('Ana Pérez', html)
        self.assertIn('Fuera de zona', html)
        self.assertIn('Excede el tope', html)

    def test_htmx_devuelve_solo_la_tabla(self):
        """El filtro refresca la tabla; si devolviera la página entera, HTMX anidaría
        un <html> dentro del div y rompería el layout."""
        self.client.force_login(self.coordinador)
        html = self.client.get(
            reverse('panel:viaticos_bandeja'), HTTP_HX_REQUEST='true',
        ).content.decode()
        self.assertNotIn('<html', html)
        self.assertIn('Ana Pérez', html)

    def test_filtro_solo_alertas(self):
        from apps.viaticos.models import ReporteViatico, RubroViatico
        limpio = ReporteViatico.objects.create(
            colaborador=self.tecnico, fecha=date(2026, 9, 11), farmacia_visitada=self.farmacia_sg,
            rubro=RubroViatico.ALIMENTACION, monto=Decimal('4.00'),
        )
        self.client.force_login(self.coordinador)
        html = self.client.get(
            reverse('panel:viaticos_bandeja'), {'solo_alertas': '1'}, HTTP_HX_REQUEST='true',
        ).content.decode()
        self.assertIn(reverse('panel:viatico_detalle', args=[self.reporte.pk]), html)
        self.assertNotIn(reverse('panel:viatico_detalle', args=[limpio.pk]), html)

    def test_aprobar_sin_justificacion_no_cambia_el_estado(self):
        from apps.viaticos.models import EstadoReporteViatico
        self.client.force_login(self.coordinador)
        self.client.post(
            reverse('panel:viatico_revisar', args=[self.reporte.pk, 'aprobar']), {'comentario': ''},
        )
        self.reporte.refresh_from_db()
        self.assertEqual(self.reporte.estado, EstadoReporteViatico.PENDIENTE)

    def test_aprobar_con_justificacion_funciona(self):
        from apps.viaticos.models import EstadoReporteViatico
        self.client.force_login(self.coordinador)
        self.client.post(
            reverse('panel:viatico_revisar', args=[self.reporte.pk, 'aprobar']),
            {'comentario': 'Único hotel disponible por feriado.'},
        )
        self.reporte.refresh_from_db()
        self.assertEqual(self.reporte.estado, EstadoReporteViatico.APROBADO)
        self.assertEqual(self.reporte.revisado_por, self.coordinador)

    def test_revisar_sin_permiso_de_cambio_devuelve_403(self):
        self.client.force_login(self._usuario('solo_lectura_v', True, 'view_reporteviatico'))
        resp = self.client.post(
            reverse('panel:viatico_revisar', args=[self.reporte.pk, 'aprobar']), {'comentario': 'x'},
        )
        self.assertEqual(resp.status_code, 403)

    def test_otra_unidad_de_negocio_no_ve_el_reporte(self):
        de_mia = self._usuario(
            'coord_mia_v', False, 'view_reporteviatico', 'change_reporteviatico', unidades=[self.mia_un],
        )
        self.client.force_login(de_mia)
        html = self.client.get(reverse('panel:viaticos_bandeja')).content.decode()
        self.assertNotIn('Ana Pérez', html)

    def test_forzar_el_pk_por_url_desde_otro_tenant_da_403(self):
        de_mia = self._usuario('coord_mia_v2', False, 'view_reporteviatico', unidades=[self.mia_un])
        self.client.force_login(de_mia)
        resp = self.client.get(reverse('panel:viatico_detalle', args=[self.reporte.pk]))
        self.assertEqual(resp.status_code, 403)

    def test_csv_del_consolidado_respeta_el_tenant(self):
        de_mia = self._usuario('coord_mia_v3', False, 'view_reporteviatico', unidades=[self.mia_un])
        self.client.force_login(de_mia)
        resp = self.client.get(reverse('panel:viaticos_consolidado_csv'), {'mes': '2026-09'})
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn('Ana Pérez', resp.content.decode('utf-8'))

    def test_csv_del_consolidado_trae_los_totales(self):
        self.client.force_login(self.coordinador)
        resp = self.client.get(reverse('panel:viaticos_consolidado_csv'), {'mes': '2026-09'})
        self.assertEqual(resp['Content-Type'], 'text/csv; charset=utf-8')
        self.assertIn(
            'attachment; filename="viaticos_consolidado_2026-09.csv"', resp['Content-Disposition'],
        )
        cuerpo = resp.content.decode('utf-8')
        self.assertIn('Ana Pérez', cuerpo)
        self.assertIn('45.00', cuerpo)

    def test_alta_sin_colaborador_vinculado_lo_dice(self):
        """Un usuario sin Colaborador no tiene a nombre de quién cargar el gasto: se
        explica qué falta en vez de guardar algo sin dueño."""
        suelto = self._usuario('suelto_v', True, 'add_reporteviatico', 'view_reporteviatico')
        self.client.force_login(suelto)
        resp = self.client.get(reverse('panel:viatico_crear'))
        self.assertEqual(resp.status_code, 409)
        self.assertIn('no está vinculado a un colaborador', resp.content.decode())

    def _tecnico_logueado(self, nombre):
        usuario = self._usuario(nombre, True, 'add_reporteviatico', 'view_reporteviatico')
        self.tecnico.usuario = usuario
        self.tecnico.save(update_fields=['usuario'])
        self.client.force_login(usuario)
        return usuario

    def test_alta_registra_a_nombre_del_usuario_logueado(self):
        from apps.viaticos.models import ReporteViatico, RubroViatico
        self._tecnico_logueado('tecnico_alta_v')
        resp = self.client.post(reverse('panel:viatico_crear'), {
            'fecha': '2026-09-12', 'farmacia_visitada': self.farmacia_sg.pk,
            'rubro': RubroViatico.ALIMENTACION, 'monto': '4.00',
            'origen': '', 'destino': '', 'descripcion': 'Almuerzo', 'total_factura': '',
        })
        self.assertEqual(resp.status_code, 302, getattr(resp, 'context', {}))
        creado = ReporteViatico.objects.get(fecha=date(2026, 9, 12))
        self.assertEqual(creado.colaborador, self.tecnico)

    def test_movilizacion_sin_origen_destino_no_se_guarda_desde_el_panel(self):
        from apps.viaticos.models import ReporteViatico, RubroViatico
        self._tecnico_logueado('tecnico_mov_v')
        resp = self.client.post(reverse('panel:viatico_crear'), {
            'fecha': '2026-09-12', 'farmacia_visitada': self.farmacia_sg.pk,
            'rubro': RubroViatico.MOVILIZACION, 'monto': '10.00',
            'origen': '', 'destino': '', 'descripcion': '', 'total_factura': '',
        })
        self.assertEqual(resp.status_code, 200)  # vuelve al formulario con errores
        self.assertFalse(ReporteViatico.objects.filter(fecha=date(2026, 9, 12)).exists())

    def test_el_buscador_de_farmacias_no_se_sirve_a_quien_no_puede_cargar(self):
        """F-17: el parcial pedia `view_reporteviatico` y sus dos marcos `add_`.

        No es un subconjunto: son dos codenames independientes y un grupo puede dar uno sin
        el otro, asi que fallaba en los dos sentidos. En este —el de la fuga— alguien con
        solo `view_` podia pedirlo por URL y sacar codigo, nombre y ubicacion de hasta 100
        farmacias sin tener acceso a ningun formulario donde ese widget viva.
        """
        solo_lectura = self._usuario('viat_solo_view', True, 'view_reporteviatico')
        self.client.force_login(solo_lectura)

        resp = self.client.get(
            reverse('panel:viaticos_farmacias_partial'), {'buscar_farmacia': 'ML006'},
        )

        self.assertEqual(resp.status_code, 403)
        self.assertNotIn('ML006', resp.content.decode())

    def test_buscar_farmacias_exige_termino(self):
        """Sin término no se devuelven 700 opciones: es lo que dejaba la pantalla
        inusable en el alta de mantenimiento.

        Lo pide el técnico y no el coordinador: este parcial repuebla el buscador del
        formulario de carga, que solo se sirve en `viatico_crear` y `viatico_editar`
        (`add_reporteviatico`). El coordinador tiene view/change y ninguna pantalla donde ese
        widget aparezca — antes el test lo usaba porque alcanzaba para un 200, no porque
        represente el flujo.
        """
        self._tecnico_logueado('tecnico_busca_v')
        vacio = self.client.get(reverse('panel:viaticos_farmacias_partial')).content.decode()
        self.assertNotIn('ML006', vacio)
        con_termino = self.client.get(
            reverse('panel:viaticos_farmacias_partial'), {'buscar_farmacia': 'ML006'},
        ).content.decode()
        self.assertIn('ML006', con_termino)


class VentanaEmergenteAltaTests(TestCase):
    """Un mismo formulario servido como página completa o como ventana emergente.

    Las 31 vistas que usan `panel/accion_form.html` no saben que el modal existe:
    lo deciden el context processor (de qué hereda la plantilla) y el middleware
    (qué hacer con el redirect al guardar). Estas pruebas fijan ese contrato,
    incluida la parte que MÁS fácil se rompe: que las otras pantallas que ya
    usaban htmx sigan recibiendo su redirect de siempre.
    """

    def setUp(self):
        self.usuario = User.objects.create_user(username='u_modal', password='x')
        PerfilUsuario.objects.create(usuario=self.usuario, acceso_todas_unidades=True)
        self.usuario.user_permissions.add(
            Permission.objects.get(content_type__app_label='activos', codename='add_colaborador'),
            Permission.objects.get(content_type__app_label='activos', codename='view_colaborador'),
        )
        self.client.force_login(self.usuario)
        self.url = reverse('panel:colaborador_crear')

    def test_get_sin_htmx_devuelve_la_pagina_completa(self):
        resp = self.client.get(self.url)
        self.assertEqual(resp.status_code, 200)
        cuerpo = resp.content.decode()
        self.assertIn('<!DOCTYPE html>', cuerpo)
        self.assertIn('Centro de Operaciones', cuerpo)   # la barra lateral sigue ahí
        self.assertNotIn('modal-card', cuerpo)

    def test_get_con_htmx_devuelve_solo_el_fragmento_del_modal(self):
        resp = self.client.get(self.url, headers={'hx-request': 'true'})
        self.assertEqual(resp.status_code, 200)
        cuerpo = resp.content.decode()
        self.assertIn('modal-card', cuerpo)
        self.assertIn('Guardar y cargar otro', cuerpo)
        # Sin esto se estaría metiendo una página entera (barra lateral incluida)
        # dentro del <dialog> de la página que ya está abierta.
        self.assertNotIn('<!DOCTYPE html>', cuerpo)
        self.assertNotIn('Centro de Operaciones', cuerpo)

    def test_restaurar_historial_no_cuenta_como_modal(self):
        """Tocar "atrás" repinta desde la caché de htmx: es navegación, no modal."""
        resp = self.client.get(self.url, headers={
            'hx-request': 'true', 'hx-history-restore-request': 'true',
        })
        self.assertIn('<!DOCTYPE html>', resp.content.decode())

    def test_guardar_en_el_modal_responde_204_con_el_evento(self):
        resp = self.client.post(
            self.url, {'nombre': 'Ana Pérez', 'cedula': '0102030405'},
            headers={'hx-request': 'true', 'hx-target': 'modal-form-content'},
        )
        self.assertEqual(resp.status_code, 204)
        self.assertTrue(Colaborador.objects.filter(nombre='Ana Pérez').exists())
        evento = json.loads(resp.headers['HX-Trigger'])
        self.assertEqual(
            evento['saidsoft:guardado']['url'], reverse('panel:colaboradores_lista'),
        )

    def test_htmx_fuera_del_modal_conserva_su_redirect(self):
        """Regresión: el panel ya usaba htmx antes de que existiera el modal.

        El modal de información de estación y la bandeja de viáticos disparan sus
        propias peticiones htmx. Si el middleware convirtiera CUALQUIER redirect
        de CUALQUIER petición htmx, esas pantallas dejarían de funcionar sin que
        nadie tocara su código.
        """
        resp = self.client.post(
            self.url, {'nombre': 'Beto Ruiz', 'cedula': '0102030406'},
            headers={'hx-request': 'true', 'hx-target': 'otro-destino'},
        )
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp['Location'], reverse('panel:colaboradores_lista'))
        self.assertNotIn('HX-Trigger', resp.headers)

    def test_sin_htmx_el_alta_sigue_redirigiendo(self):
        resp = self.client.post(self.url, {'nombre': 'Caro Díaz', 'cedula': '0102030407'})
        self.assertRedirects(resp, reverse('panel:colaboradores_lista'))

    def test_formulario_invalido_vuelve_a_pintar_el_modal_con_el_error(self):
        resp = self.client.post(
            self.url, {'nombre': ''},
            headers={'hx-request': 'true', 'hx-target': 'modal-form-content'},
        )
        self.assertEqual(resp.status_code, 200)
        cuerpo = resp.content.decode()
        self.assertIn('modal-card', cuerpo)
        self.assertIn('field-error', cuerpo)
        self.assertNotIn('<!DOCTYPE html>', cuerpo)
        self.assertFalse(Colaborador.objects.exists())

    def test_marcar_cargar_otro_sobrevive_a_un_error_de_validacion(self):
        """Desmarcarla sola obligaría a re-tildarla en cada error, cargando en tanda."""
        resp = self.client.post(
            self.url, {'nombre': '', 'seguir_cargando': '1'},
            headers={'hx-request': 'true', 'hx-target': 'modal-form-content'},
        )
        plano = ' '.join(resp.content.decode().split())
        self.assertIn('name="seguir_cargando" value="1" checked', plano)

    def test_la_lista_ofrece_el_alta_como_ventana_emergente(self):
        resp = self.client.get(reverse('panel:colaboradores_lista'))
        cuerpo = resp.content.decode()
        self.assertIn('data-modal', cuerpo)
        # El href real se conserva: Ctrl+clic y un navegador sin JS siguen
        # llevando al formulario de página completa.
        self.assertIn(f'href="{self.url}" data-modal', cuerpo)


class SaludRespaldoDashboardTests(TestCase):
    """El panel vigila la flota; esto lo hace vigilarse a sí mismo.

    El 6 y el 7-sep-2026 no hubo respaldo — el servidor estuvo apagado el fin de
    semana y el cron de las 02:00 nunca existió a esa hora — y nadie se enteró en
    dos días. La franja del dashboard convierte eso en algo que se lee al entrar.
    """

    def setUp(self):
        self.usuario = User.objects.create_user(username='u_respaldo', password='x')
        PerfilUsuario.objects.create(usuario=self.usuario, acceso_todas_unidades=True)
        self.client.force_login(self.usuario)

    def test_sin_ningun_respaldo_avisa(self):
        """Es el estado de un servidor donde el timer nunca se instaló."""
        resp = self.client.get(reverse('panel:dashboard'))
        self.assertFalse(resp.context['respaldo_al_dia'])
        self.assertIsNone(resp.context['respaldo_ultimo'])
        self.assertContains(resp, 'No hay ningún respaldo registrado todavía')

    def test_respaldo_reciente_no_avisa(self):
        WorkerHeartbeat.objects.create(nombre='respaldo', ultimo_latido=timezone.now())
        resp = self.client.get(reverse('panel:dashboard'))
        self.assertTrue(resp.context['respaldo_al_dia'])
        self.assertNotContains(resp, 'Sin respaldo al día')

    def test_respaldo_de_ayer_todavia_no_avisa(self):
        """El respaldo corre a las 02:00: a las 25h aún entra en la gracia de 26h.

        Sin esa gracia, un arranque tardío tras un corte de energía haría gritar al
        panel justo cuando el timer ya corrió su ejecución atrasada.
        """
        WorkerHeartbeat.objects.create(
            nombre='respaldo', ultimo_latido=timezone.now() - timedelta(hours=25),
        )
        resp = self.client.get(reverse('panel:dashboard'))
        self.assertTrue(resp.context['respaldo_al_dia'])

    def test_respaldo_de_hace_dos_dias_avisa(self):
        """El caso real del 7-sep-2026."""
        WorkerHeartbeat.objects.create(
            nombre='respaldo', ultimo_latido=timezone.now() - timedelta(hours=48),
        )
        resp = self.client.get(reverse('panel:dashboard'))
        self.assertFalse(resp.context['respaldo_al_dia'])
        self.assertContains(resp, 'Sin respaldo al día')
        self.assertContains(resp, 'hace 48 h')


class RegistrarLatidoCommandTests(TestCase):
    """`backup.sh` corre en el HOST, fuera del stack: este comando es su puerta al ORM."""

    def test_crea_y_luego_actualiza_la_fila(self):
        call_command('registrar_latido', 'respaldo')
        primero = WorkerHeartbeat.objects.get(nombre='respaldo').ultimo_latido

        call_command('registrar_latido', 'respaldo')
        self.assertEqual(WorkerHeartbeat.objects.filter(nombre='respaldo').count(), 1)
        self.assertGreaterEqual(WorkerHeartbeat.objects.get(nombre='respaldo').ultimo_latido, primero)

    def test_no_pisa_el_latido_de_otro_worker(self):
        WorkerHeartbeat.objects.create(
            nombre='mqtt_worker', ultimo_latido=timezone.now() - timedelta(hours=5),
        )
        call_command('registrar_latido', 'respaldo')
        self.assertEqual(WorkerHeartbeat.objects.count(), 2)


class ArchivosMantenimientoProtegidosTests(TestCase):
    """Las fotos e informes de mantenimiento ya no se sirven sin sesión.

    Hasta el 7-sep-2026, `/media/mantenimiento/` lo servía nginx con `alias` y sin
    autenticación: cualquiera con acceso a la red o a la VPN podía bajarse las fotos
    tomadas dentro de las farmacias y los informes firmados conociendo la ruta.
    A diferencia de `/media/despliegues|agente|software/`, que SÍ son públicos a
    propósito porque los agentes bajan de ahí sin credenciales.
    """

    def setUp(self):
        self.sg = UnidadNegocio.objects.get(codigo='SG')
        self.mia = UnidadNegocio.objects.get(codigo='MIA')
        colaborador_sg = Colaborador.objects.create(
            nombre='Cliente SG', cedula='0900000001', unidad_negocio=self.sg,
        )
        self.mantenimiento = Mantenimiento.objects.create(
            cliente=colaborador_sg, fecha_programada=timezone.now(),
        )
        self.imagen = ImagenMantenimiento.objects.create(
            mantenimiento=self.mantenimiento,
            imagen=SimpleUploadedFile('evidencia.jpg', b'\xff\xd8\xff\xe0fake-jpeg', content_type='image/jpeg'),
            nombre_archivo='evidencia.jpg',
        )
        self.url_imagen = reverse('panel:mantenimiento_imagen', args=[self.imagen.pk])

        self.permiso = Permission.objects.get(
            content_type__app_label='mantenimiento', codename='view_mantenimiento',
        )

    def _usuario(self, username, unidad=None, con_permiso=True):
        user = User.objects.create_user(username=username, password='x')
        perfil = PerfilUsuario.objects.create(usuario=user, acceso_todas_unidades=unidad is None)
        if unidad is not None:
            perfil.unidades_negocio.add(unidad)
        if con_permiso:
            user.user_permissions.add(self.permiso)
        return user

    def test_sin_sesion_no_entrega_el_archivo(self):
        resp = self.client.get(self.url_imagen)
        self.assertEqual(resp.status_code, 302)
        self.assertIn('/login/', resp['Location'])

    def test_con_sesion_pero_sin_permiso_da_403(self):
        self.client.force_login(self._usuario('u_sin_perm_img', con_permiso=False))
        self.assertEqual(self.client.get(self.url_imagen).status_code, 403)

    def test_de_otro_cliente_da_403(self):
        """El listado ya filtra por cliente, pero alguien podría forzar el id por URL."""
        self.client.force_login(self._usuario('u_mia_img', unidad=self.mia))
        self.assertEqual(self.client.get(self.url_imagen).status_code, 403)

    # En producción `SERVIR_MEDIA_CON_NGINX=True` y la vista devuelve un HttpResponse
    # con X-Accel-Redirect, sin `streaming_content`. La prueba de esa rama ya declara
    # su override; a esta le faltaba el inverso y solo pasaba con settings de dev.
    @override_settings(SERVIR_MEDIA_CON_NGINX=False)
    def test_con_permiso_entrega_el_archivo(self):
        self.client.force_login(self._usuario('u_ok_img'))
        resp = self.client.get(self.url_imagen)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(b''.join(resp.streaming_content), b'\xff\xd8\xff\xe0fake-jpeg')

    @override_settings(SERVIR_MEDIA_CON_NGINX=True)
    def test_detras_de_nginx_delega_los_bytes_al_proxy(self):
        """Django decide el permiso; nginx manda los bytes.

        Sin esto, un informe con fotos de varios MB ocuparía un worker de gunicorn
        todo lo que durase la descarga — el problema que config/urls.py ya
        documentaba para /media/ y por el que existe el proxy.
        """
        self.client.force_login(self._usuario('u_nginx_img'))
        resp = self.client.get(self.url_imagen)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp['X-Accel-Redirect'], f'/media/{self.imagen.imagen.name}')
        self.assertEqual(resp.content, b'')
        # Sin Content-Type propio: lo resuelve nginx por la extensión real. Si Django
        # mandara text/html, el navegador intentaría renderizar el JPEG como página.
        self.assertNotIn('Content-Type', resp.headers)

    def test_informe_inexistente_da_404_y_no_un_500(self):
        self.client.force_login(self._usuario('u_informe_vacio'))
        resp = self.client.get(reverse('panel:mantenimiento_informe', args=[self.mantenimiento.pk]))
        self.assertEqual(resp.status_code, 404)

    # --- El informe PDF, por la vista protegida ---
    #
    # La vista existía y comprobaba el tenant, pero solo estaba cubierta por el caso
    # "todavía no se generó" (404). Faltaba el que de verdad importa: que el informe
    # de un cliente no se pueda bajar forzando el id desde la sesión de otro.

    def _con_informe(self):
        self.mantenimiento.informe_pdf.save(
            f'mantenimiento_{self.mantenimiento.pk}.pdf',
            SimpleUploadedFile('informe.pdf', b'%PDF-1.4 fake', content_type='application/pdf'),
            save=True,
        )
        return reverse('panel:mantenimiento_informe', args=[self.mantenimiento.pk])

    def test_informe_sin_sesion_no_se_entrega(self):
        resp = self.client.get(self._con_informe())
        self.assertEqual(resp.status_code, 302)
        self.assertIn('/login/', resp['Location'])

    def test_informe_de_otro_cliente_da_403(self):
        """El informe lleva firmas y fotos de dentro de la farmacia de UN cliente."""
        url = self._con_informe()
        self.client.force_login(self._usuario('u_mia_informe', unidad=self.mia))
        self.assertEqual(self.client.get(url).status_code, 403)

    @override_settings(SERVIR_MEDIA_CON_NGINX=False)
    def test_informe_con_acceso_al_cliente_se_entrega(self):
        url = self._con_informe()
        self.client.force_login(self._usuario('u_sg_informe', unidad=self.sg))
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(b''.join(resp.streaming_content), b'%PDF-1.4 fake')

    # --- La ruta cruda de /media/, cerrada en el propio URLconf ---
    #
    # nginx ya declara `location /media/mantenimiento/ internal`, pero esa mitad de la
    # defensa vive en otra capa y Django no se entera de si sigue ahí: con settings de
    # producción sin proxy delante, o tras perder ese bloque en un cambio de
    # configuración, el catch-all de /media/ volvía a entregar informes y fotos a
    # cualquiera. El pk es secuencial y la carpeta es año/mes, así que la ruta se
    # adivina sin esfuerzo.

    def test_la_ruta_cruda_del_informe_no_se_sirve_ni_con_sesion(self):
        self._con_informe()
        self.client.force_login(self._usuario('u_media_crudo'))
        resp = self.client.get(f'/media/{self.mantenimiento.informe_pdf.name}')
        self.assertEqual(resp.status_code, 404)

    def test_la_ruta_cruda_de_la_imagen_no_se_sirve(self):
        resp = self.client.get(f'/media/{self.imagen.imagen.name}')
        self.assertEqual(resp.status_code, 404)

    def test_el_resto_de_media_sigue_siendo_publico(self):
        """Los agentes bajan de /media/ los paquetes de despliegue SIN credenciales.

        Cerrar /media/mantenimiento/ no puede cerrar el resto: un 404 acá dejaría a
        las ~1.800 estaciones sin poder descargar su propio ejecutable ni los .zip.
        Se comprueba con un archivo real en MEDIA_ROOT porque un 404 de "no existe" y
        uno de "ruta protegida" se ven iguales desde fuera.
        """
        publico = settings.MEDIA_ROOT / 'despliegues'
        publico.mkdir(parents=True, exist_ok=True)
        archivo = publico / 'paquete-prueba-acceso-publico.zip'
        archivo.write_bytes(b'PK\x03\x04 paquete')
        try:
            resp = self.client.get('/media/despliegues/paquete-prueba-acceso-publico.zip')
            self.assertEqual(resp.status_code, 200)
            self.assertEqual(b''.join(resp.streaming_content), b'PK\x03\x04 paquete')
        finally:
            archivo.unlink()

    def test_la_ficha_ya_no_expone_la_ruta_cruda_de_media(self):
        self.client.force_login(self._usuario('u_ficha_img'))
        cuerpo = self.client.get(
            reverse('panel:mantenimiento_detalle', args=[self.mantenimiento.pk]),
        ).content.decode()
        self.assertIn(self.url_imagen, cuerpo)
        self.assertNotIn('/media/mantenimiento/', cuerpo)

    def test_la_plantilla_del_pdf_sigue_usando_la_ruta_de_disco(self):
        """xhtml2pdf resuelve /media/ al sistema de archivos con su link_callback.

        Si esa plantilla se cambiara a la URL protegida, el renderizador recibiría una
        ruta que su callback no traduce y el informe saldría sin las fotos —
        silenciosamente, porque pisa no falla por una imagen que no encuentra.
        """
        plantilla = (settings.BASE_DIR / 'templates' / 'panel' / 'mantenimiento_informe_pdf.html').read_text(
            encoding='utf-8',
        )
        self.assertIn('img.imagen.url', plantilla)
        self.assertNotIn("panel:mantenimiento_imagen", plantilla)


class EstadoVacioTests(TestCase):
    """Una lista vacía tiene que decir cuál de los dos vacíos es.

    "Sin colaboradores" y "tu filtro no devolvió nada" se veían idénticos (una línea
    gris centrada) y piden lo contrario: cargar el primer registro en un caso, quitar
    el filtro en el otro. Ofrecer "cargar el primero" cuando en realidad hay 300
    registros tapados por un filtro manda al usuario a duplicar datos.

    Lo decide `panel/_estado_vacio.html` con el tag `hay_filtros`; estas pruebas fijan
    ese contrato y los dos casos que más fácil se rompen: la paginación (que no es un
    filtro) y Alertas (donde `?todas=1` AMPLÍA lo que se ve, no lo acota).
    """

    def setUp(self):
        self.usuario = User.objects.create_user(username='u_vacio', password='x')
        PerfilUsuario.objects.create(usuario=self.usuario, acceso_todas_unidades=True)
        self.usuario.user_permissions.add(
            Permission.objects.get(content_type__app_label='activos', codename='view_colaborador'),
            Permission.objects.get(content_type__app_label='activos', codename='add_colaborador'),
            Permission.objects.get(content_type__app_label='monitoreo', codename='view_alerta'),
            Permission.objects.get(content_type__app_label='activos', codename='view_activo'),
        )
        self.client.force_login(self.usuario)
        self.url = reverse('panel:colaboradores_lista')

    def test_lista_vacia_sin_filtros_invita_a_cargar_el_primero(self):
        cuerpo = self.client.get(self.url).content.decode()
        self.assertIn('Todavía no hay colaboradores', cuerpo)
        # El mismo destino que el botón del encabezado, para que sea un solo camino.
        self.assertIn(reverse('panel:colaborador_crear'), cuerpo)
        self.assertNotIn('Nada coincide con este filtro', cuerpo)

    def test_lista_vacia_por_un_filtro_ofrece_quitarlo_y_no_dar_de_alta(self):
        """Se prueba sobre Activos porque es una lista que de verdad filtra por GET."""
        cuerpo = self.client.get(
            reverse('panel:activos_lista'), {'estado': 'de_baja'},
        ).content.decode()
        self.assertIn('Nada coincide con este filtro', cuerpo)
        self.assertIn('Quitar filtros', cuerpo)
        self.assertNotIn('El inventario de activos está vacío', cuerpo)

    def test_un_filtro_vacio_no_cuenta_como_filtro(self):
        """Un <select> en su opción "todos" manda `?estado=`: la lista es la completa."""
        cuerpo = self.client.get(reverse('panel:activos_lista'), {'estado': ''}).content.decode()
        self.assertIn('El inventario de activos está vacío', cuerpo)
        self.assertNotIn('Nada coincide con este filtro', cuerpo)

    def test_la_paginacion_no_cuenta_como_filtro(self):
        cuerpo = self.client.get(self.url, {'pagina': '2'}).content.decode()
        self.assertIn('Todavía no hay colaboradores', cuerpo)
        self.assertNotIn('Nada coincide con este filtro', cuerpo)

    def test_en_alertas_ver_todas_no_se_lee_como_un_filtro(self):
        """`?todas=1` amplía: ofrecer "quitar filtros" ahí sería ofrecer ver menos."""
        cuerpo = self.client.get(reverse('panel:alertas_lista'), {'todas': '1'}).content.decode()
        self.assertIn('Sin alertas abiertas', cuerpo)
        self.assertNotIn('Nada coincide con este filtro', cuerpo)

    def test_sin_permiso_de_alta_no_se_ofrece_el_boton(self):
        """Las listas que condicionan su botón de alta lo condicionan también acá."""
        sin_permiso = User.objects.create_user(username='u_vacio_ro', password='x')
        PerfilUsuario.objects.create(usuario=sin_permiso, acceso_todas_unidades=True)
        sin_permiso.user_permissions.add(
            Permission.objects.get(content_type__app_label='viaticos', codename='view_colaboradorzona'),
        )
        self.client.force_login(sin_permiso)
        cuerpo = self.client.get(reverse('panel:viaticos_zonas_lista')).content.decode()
        self.assertIn('Todavía no hay zonas asignadas', cuerpo)
        self.assertNotIn(reverse('panel:viaticos_zona_crear'), cuerpo)


class ComentariosDePlantillaTests(TestCase):
    """Ningún `{# ... #}` puede abarcar más de una línea.

    El `tag_re` de Django no usa `re.DOTALL`, así que un `{# ... #}` multilínea no es
    un comentario: es **texto** y sale renderizado en la página. Ya pasó una vez
    (§10-O del plan, 6-ago-2026) y volvió a colarse en el trabajo del 7-sep: para el
    10-sep había 9, entre ellos uno en `base.html` (o sea en toda página autenticada),
    uno en el dashboard y dos en `_base_modal.html`, que metían ~950 bytes de texto
    suelto dentro del `<dialog>` de cada formulario de alta.

    Esta prueba existe porque es una regresión invisible para el resto de la suite:
    la página sigue devolviendo 200 y nada falla — solo se ve mal.
    """

    def test_ningun_comentario_de_plantilla_abarca_varias_lineas(self):
        import re

        culpables = []
        for ruta in sorted((settings.BASE_DIR / 'templates').rglob('*.html')):
            contenido = ruta.read_text(encoding='utf-8')
            for apertura in re.finditer(r'\{#', contenido):
                resto = contenido[apertura.start():]
                fin_de_linea = resto.find('\n')
                cierre = resto.find('#}')
                if cierre == -1 or (fin_de_linea != -1 and cierre > fin_de_linea):
                    linea = contenido[:apertura.start()].count('\n') + 1
                    culpables.append('%s:%d' % (ruta.name, linea))

        self.assertEqual(
            culpables, [],
            'Comentarios multilínea que se van a renderizar como texto visible: %s. '
            'Usá {%% comment %%}...{%% endcomment %%}.' % ', '.join(culpables),
        )


class DialogosCentradosTests(TestCase):
    """Todo `<dialog>` del panel tiene que reafirmar su centrado.

    El preflight de Tailwind pone `margin:0` en todos los elementos, `<dialog>`
    incluido, y eso anula el `margin:auto` con el que el navegador centra un diálogo
    abierto con `showModal()`: queda pegado arriba a la izquierda. Ya pasó dos veces —
    se arregló para `#modal-info` (ficha de estación) y el arreglo nunca se le aplicó a
    `.modal`, así que **todo formulario de alta** se abrió descentrado desde que existe
    la ventana emergente (7-sep) hasta que el usuario lo reportó (10-sep).

    No hay forma de que la suite lo vea renderizando: es CSS. Así que se verifica el
    CSS fuente, que es donde vive la decisión.
    """

    def test_los_dialogos_reafirman_position_inset_y_margin(self):
        css = (settings.BASE_DIR / 'static_src' / 'components.css').read_text(encoding='utf-8')
        import re

        for selector in ['dialog#modal-info', '.modal']:
            bloque = re.search(
                r'(?:^|\n)' + re.escape(selector) + r'\s*\{(.*?)\}', css, re.S,
            )
            self.assertIsNotNone(bloque, 'No se encontró la regla de %s' % selector)
            cuerpo = bloque.group(1)
            for declaracion in ['position:fixed', 'inset:0', 'margin:auto']:
                self.assertIn(
                    declaracion, cuerpo.replace(' ', ''),
                    '%s no declara %s: se va a abrir pegado arriba a la izquierda '
                    'por el margin:0 del preflight de Tailwind.' % (selector, declaracion),
                )


class AperturaPanelTests(TestCase):
    """Vistas del panel de aperturas (`apps/panel/views/aperturas.py`)."""

    def setUp(self):
        from apps.aperturas.models import PasoPlantilla, PerfilEstacionPlantilla, PlantillaApertura, TipoPaso

        self.sg = UnidadNegocio.objects.get(codigo='SG')
        self.mia = UnidadNegocio.objects.get(codigo='MIA')
        grupo = Grupo.objects.create(codigo='TRX001', version_objetivo='4.2.1')
        self.farmacia = Farmacia.objects.create(codigo='ML099', grupo=grupo, unidad_negocio=self.sg)

        self.creador = self._usuario('ap_creador', ['view_apertura', 'add_apertura', 'change_apertura'])
        self.aprobador = self._usuario(
            'ap_aprobador',
            ['view_apertura', 'change_apertura', 'aprobar_apertura', 'emitir_token_apertura'],
        )
        self.miron = self._usuario('ap_miron', ['view_apertura'])

        self.plantilla = PlantillaApertura.objects.create(
            nombre='Mostrador', unidad_negocio=self.sg, creado_por=self.creador,
        )
        self.perfil = PerfilEstacionPlantilla.objects.create(
            plantilla=self.plantilla, sufijo='A', rol=PerfilEstacionPlantilla.Rol.CAJA,
        )
        self.paso_manual = PasoPlantilla.objects.create(
            plantilla=self.plantilla, orden=10, nombre='Alta en AD', tipo=TipoPaso.MANUAL,
        )

    def _usuario(self, username, codenames, acceso_total=True, unidades=None):
        usuario = User.objects.create_user(username=username, password='x')
        perfil = PerfilUsuario.objects.create(usuario=usuario, acceso_todas_unidades=acceso_total)
        if unidades:
            perfil.unidades_negocio.set(unidades)
        for codename in codenames:
            usuario.user_permissions.add(
                Permission.objects.get(content_type__app_label='aperturas', codename=codename),
            )
        return usuario

    def _apertura(self, aprobada=True):
        from apps.aperturas.services import aprobar_apertura, crear_apertura

        apertura = crear_apertura(
            farmacia=self.farmacia, plantilla=self.plantilla,
            fecha_prevista=timezone.localdate(), usuario=self.creador,
        )
        if aprobada:
            aprobar_apertura(apertura=apertura, usuario=self.aprobador)
        return apertura

    def test_lista_y_detalle_cargan(self):
        apertura = self._apertura()
        self.client.force_login(self.miron)
        self.assertEqual(self.client.get(reverse('panel:aperturas_lista')).status_code, 200)
        resp = self.client.get(reverse('panel:apertura_detalle', args=[apertura.pk]))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'ML099')

    def test_crear_rechaza_farmacia_con_apertura_vigente(self):
        self._apertura(aprobada=False)
        self.client.force_login(self.creador)
        resp = self.client.post(reverse('panel:apertura_crear'), {
            'farmacia': self.farmacia.pk, 'plantilla': self.plantilla.pk,
            'fecha_prevista': timezone.localdate().isoformat(),
        })
        self.assertEqual(resp.status_code, 200)
        # El form ni la ofrece: la excluye del queryset, así que falla la validación del
        # campo antes de llegar al servicio.
        self.assertTrue(resp.context['form'].errors)

    def test_aprobar_respeta_los_cuatro_ojos(self):
        from apps.aperturas.models import Apertura

        apertura = self._apertura(aprobada=False)
        creador_que_aprueba = self._usuario('ap_ambos', ['view_apertura', 'aprobar_apertura'])
        apertura.creado_por = creador_que_aprueba
        apertura.save(update_fields=['creado_por'])

        self.client.force_login(creador_que_aprueba)
        self.client.post(reverse('panel:apertura_aprobar', args=[apertura.pk]))
        apertura.refresh_from_db()
        self.assertEqual(apertura.estado, Apertura.Estado.PENDIENTE_APROBACION)

    def test_emitir_tokens_exige_permiso_propio(self):
        """`emitir_token_apertura` está separado de `aprobar_apertura`: ver el secreto en
        claro es una capacidad distinta de autorizar la apertura."""
        apertura = self._apertura()
        self.client.force_login(self.creador)
        resp = self.client.post(reverse('panel:apertura_emitir_tokens', args=[apertura.pk]))
        self.assertEqual(resp.status_code, 403)
        self.assertFalse(apertura.tokens.exists())

    def test_el_token_en_claro_se_muestra_una_sola_vez(self):
        """Regresión de §10-Z. El valor en claro sale en la respuesta de emisión y en
        NINGÚN otro lado: el detalle solo puede mostrar el prefijo."""
        apertura = self._apertura()
        self.client.force_login(self.aprobador)

        resp = self.client.post(reverse('panel:apertura_emitir_tokens', args=[apertura.pk]))
        self.assertEqual(resp.status_code, 200)
        token = apertura.tokens.get()
        plano = resp.context['emitidos'][0][1]
        self.assertContains(resp, plano)

        detalle = self.client.get(reverse('panel:apertura_detalle', args=[apertura.pk]))
        self.assertNotContains(detalle, plano)
        self.assertContains(detalle, token.prefijo)

        # Reenviar el POST no emite otro para el mismo perfil (ni filtra el anterior).
        otra = self.client.post(reverse('panel:apertura_emitir_tokens', args=[apertura.pk]))
        self.assertEqual(apertura.tokens.count(), 1)
        self.assertEqual(otra.status_code, 302)

    def test_completar_paso_manual(self):
        from apps.aperturas.models import PasoApertura

        apertura = self._apertura()
        paso = apertura.pasos.get()
        self.client.force_login(self.creador)
        self.client.post(
            reverse('panel:apertura_paso_completar', args=[apertura.pk, paso.pk]),
            {'detalle': 'cuenta creada en el dominio'},
        )
        paso.refresh_from_db()
        self.assertEqual(paso.estado, PasoApertura.Estado.COMPLETADO)
        self.assertIn('cuenta creada', paso.detalle)

    def test_cancelar_revoca_los_tokens_sin_usar(self):
        """Cancelar sin revocar sería cosmético: cada token sin usar todavía sirve para
        enrolar una estación ya aprobada."""
        from apps.aperturas.models import Apertura

        apertura = self._apertura()
        self.client.force_login(self.aprobador)
        self.client.post(reverse('panel:apertura_emitir_tokens', args=[apertura.pk]))
        self.client.post(reverse('panel:apertura_cancelar', args=[apertura.pk]))

        apertura.refresh_from_db()
        self.assertEqual(apertura.estado, Apertura.Estado.CANCELADA)
        self.assertTrue(apertura.tokens.get().revocado)

    def test_aislamiento_por_unidad_de_negocio(self):
        apertura = self._apertura()
        ajeno = self._usuario('ap_ajeno', ['view_apertura'], acceso_total=False, unidades=[self.mia])
        self.client.force_login(ajeno)
        self.assertNotContains(self.client.get(reverse('panel:aperturas_lista')), 'ML099')
        self.assertEqual(
            self.client.get(reverse('panel:apertura_detalle', args=[apertura.pk])).status_code, 403,
        )

    def test_el_fragmento_de_pasos_sincroniza_y_lista_faltantes(self):
        apertura = self._apertura()
        self.client.force_login(self.miron)
        resp = self.client.get(reverse('panel:apertura_pasos_partial', args=[apertura.pk]))
        self.assertEqual(resp.status_code, 200)
        # La caja -A todavía no se enroló: la apertura no puede cerrarse y hay que decirlo.
        self.assertEqual([p.sufijo for p in resp.context['faltantes']], ['A'])

    def test_aprobar_registra_un_solo_evento_de_auditoria(self):
        """La vista y el servicio registraban la misma aprobación cada uno por su lado, y
        la auditoría terminaba con dos filas idénticas por aprobación."""
        apertura = self._apertura(aprobada=False)
        self.client.force_login(self.aprobador)
        self.client.post(reverse('panel:apertura_aprobar', args=[apertura.pk]))

        eventos = EventoAuditoria.objects.filter(accion='apertura.aprobar', objeto_id=str(apertura.pk))
        self.assertEqual(eventos.count(), 1)


class EnlacesFarmaciasPanelTests(TestCase):
    """Vista /monitoreo/enlaces/ (apps/panel/views/enlaces.py)."""

    def setUp(self):
        self.sg = UnidadNegocio.objects.get(codigo='SG')
        self.mia = UnidadNegocio.objects.get(codigo='MIA')
        grupo = Grupo.objects.create(codigo='TRX001')
        self.viva = Farmacia.objects.create(
            codigo='ML001', grupo=grupo, unidad_negocio=self.sg, ip_router='192.168.102.1',
        )
        self.caida = Farmacia.objects.create(
            codigo='ML002', grupo=grupo, unidad_negocio=self.sg, ip_router='192.168.102.2',
            circuito_proveedor='sangregorio2-santana',
        )
        # Sin ip_router: no se le puede hacer ping, no debe aparecer.
        Farmacia.objects.create(codigo='ML003', grupo=grupo, unidad_negocio=self.sg)

        self.usuario = User.objects.create_user(username='ver_enlaces', password='x')
        PerfilUsuario.objects.create(usuario=self.usuario, acceso_todas_unidades=True)
        self.usuario.user_permissions.add(
            Permission.objects.get(content_type__app_label='monitoreo', codename='view_estadoenlacefarmacia'),
        )

    def _sondear(self, farmacia, alcanzable, veces=1, latencia=None):
        from apps.monitoreo.enlaces import registrar_sondeo
        for _ in range(veces):
            registrar_sondeo(farmacia, alcanzable, latencia)

    def _caer(self, farmacia):
        """Deja la farmacia CAÍDA de verdad: primero responde, después deja de hacerlo.

        El sondeo exitoso inicial no es decorativo. Desde que el estado distingue
        "nunca respondió" de "se cayó" (ver EstadoEnlaceFarmacia.nunca_respondio), una
        farmacia que solo acumula fallos sin haber respondido jamás NO cuenta como
        caída: es un sitio que el monitoreo no alcanza, y el panel lo separa a propósito.
        """
        from apps.monitoreo.models import EstadoEnlaceFarmacia

        self._sondear(farmacia, True, latencia=20.0)
        self._sondear(farmacia, False, veces=EstadoEnlaceFarmacia.UMBRAL_FALLAS_CONSECUTIVAS)

    def test_exige_permiso(self):
        sin_permiso = User.objects.create_user(username='sin_enlaces', password='x')
        PerfilUsuario.objects.create(usuario=sin_permiso, acceso_todas_unidades=True)
        self.client.force_login(sin_permiso)
        self.assertEqual(self.client.get(reverse('panel:enlaces_farmacias_lista')).status_code, 403)

    def test_cuenta_activos_caidos_y_sin_sondear(self):
        from apps.monitoreo.models import EstadoEnlaceFarmacia

        self._sondear(self.viva, True, latencia=21.0)
        self._caer(self.caida)

        self.client.force_login(self.usuario)
        resp = self.client.get(reverse('panel:enlaces_farmacias_lista'))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.context['activas'], 1)
        self.assertEqual(resp.context['caidas'], 1)
        # ML003 no tiene ip_router: ni siquiera entra en la lista.
        self.assertEqual(resp.context['total'], 2)
        self.assertEqual(resp.context['sin_sondear'], 0)

    def test_las_caidas_van_primero(self):
        """Es lo que el operador vino a ver; si quedan mezcladas entre 704 filas, no sirve."""
        from apps.monitoreo.models import EstadoEnlaceFarmacia

        self._sondear(self.viva, True, latencia=21.0)
        self._caer(self.caida)

        self.client.force_login(self.usuario)
        resp = self.client.get(reverse('panel:enlaces_farmacias_lista'))
        self.assertEqual(resp.context['filas'][0]['farmacia'], self.caida)

    def test_avisa_cuando_nadie_sondeo_todavia(self):
        """Sin este aviso, una tabla toda en "sin sondear" parece una pantalla rota en vez
        de "falta que corra el barrido". El texto vive en el bloque de información del
        monitoreo, que el rediseño del 12-sep-2026 dejó colapsado pero presente en el HTML."""
        self.client.force_login(self.usuario)
        resp = self.client.get(reverse('panel:enlaces_farmacias_lista'))
        self.assertEqual(resp.context['sin_sondear'], 2)
        self.assertContains(resp, 'sondear_enlaces')

    def test_la_caida_se_ve_con_su_circuito_al_filtrar_caidos(self):
        """El circuito es lo que el proveedor pide al abrir el ticket: tenerlo a la vista
        evita ir a buscarlo a una planilla mientras la farmacia está sin vender.

        Hasta el 26-oct-2026 esto lo mostraba una lista aparte, "Caídas en curso"; el
        rediseño la quitó porque repetía lo que da la tarjeta "Caídos ahora", y el circuito
        quedó en la fila de la tabla, debajo del código."""
        self._caer(self.caida)
        self.client.force_login(self.usuario)
        resp = self.client.get(reverse('panel:enlaces_farmacias_lista'), {'estado': 'caidos'})
        self.assertEqual([f['farmacia'] for f in resp.context['filas']], [self.caida])
        self.assertContains(resp, 'sangregorio2-santana')

    def test_aislamiento_por_unidad_de_negocio(self):
        ajeno = User.objects.create_user(username='ajeno_enlaces', password='x')
        perfil = PerfilUsuario.objects.create(usuario=ajeno, acceso_todas_unidades=False)
        perfil.unidades_negocio.set([self.mia])
        ajeno.user_permissions.add(
            Permission.objects.get(content_type__app_label='monitoreo', codename='view_estadoenlacefarmacia'),
        )
        self.client.force_login(ajeno)
        resp = self.client.get(reverse('panel:enlaces_farmacias_lista'))
        self.assertEqual(resp.context['total'], 0)
        self.assertNotContains(resp, 'ML001')


class EnlaceFarmaciaModalTests(TestCase):
    """Ventana emergente de una farmacia en /monitoreo/enlaces/, con su consumo."""

    def setUp(self):
        self.sg = UnidadNegocio.objects.get(codigo='SG')
        self.mia = UnidadNegocio.objects.get(codigo='MIA')
        grupo = Grupo.objects.create(codigo='TRX001')
        self.farmacia = Farmacia.objects.create(
            codigo='ML001', grupo=grupo, unidad_negocio=self.sg, ip_router='192.168.102.1',
            circuito_proveedor='sangregorio2-santana',
        )
        self.usuario = User.objects.create_user(username='ver_modal', password='x')
        PerfilUsuario.objects.create(usuario=self.usuario, acceso_todas_unidades=True)
        self.usuario.user_permissions.add(
            Permission.objects.get(content_type__app_label='monitoreo', codename='view_estadoenlacefarmacia'),
            Permission.objects.get(content_type__app_label='catalogo', codename='consultar_info_estacion'),
        )
        self.client.force_login(self.usuario)

    def _estacion_online(self):
        return Estacion.objects.create(
            codigo='ML001-A', farmacia=self.farmacia,
            estado_aprobacion=Estacion.EstadoAprobacion.APROBADA,
            estado_conexion=Estacion.EstadoConexion.ONLINE,
        )

    def test_el_modal_muestra_estado_y_circuito(self):
        from apps.monitoreo.enlaces import registrar_sondeo

        registrar_sondeo(self.farmacia, True, 28.0)
        resp = self.client.get(reverse('panel:enlace_farmacia_modal', args=[self.farmacia.pk]))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'ML001')
        self.assertContains(resp, 'sangregorio2-santana')
        self.assertContains(resp, '28')

    def test_el_grafico_se_dibuja_como_svg_y_no_como_repr(self):
        """construir_grafico devuelve un objeto con los puntos, no markup. Imprimirlo
        directo en la plantilla renderiza su repr de Python —"Grafico(puntos='4.0,40.3
        ...')"— en medio del modal. Pasó de verdad el 11-sep-2026: se ve mal pero no
        rompe nada, así que la suite no lo notaba."""
        from apps.monitoreo.models import MuestraRedFarmacia

        for rx in (100.0, 200.0):
            MuestraRedFarmacia.objects.create(
                farmacia=self.farmacia, bytes_recibidos=1, bytes_enviados=1,
                red_recibido_kbps=rx, red_enviado_kbps=10.0,
            )
        resp = self.client.get(reverse('panel:enlace_farmacia_modal', args=[self.farmacia.pk]))
        self.assertContains(resp, '<polyline')
        self.assertNotContains(resp, 'Grafico(')

    def test_ofrece_pedir_lectura_aunque_no_haya_agente(self):
        """Invertido el 12-sep-2026: antes el boton se deshabilitaba sin una estacion en
        linea, porque se daba por sentado que solo el agente podia leer el Mikrotik. El
        servidor si lo alcanza por SNMP (comprobado contra GNB01), asi que el boton se
        ofrece siempre y el agente quedo como respaldo."""
        resp = self.client.get(reverse('panel:enlace_farmacia_modal', args=[self.farmacia.pk]))
        self.assertIsNone(resp.context['estacion_sondeadora'])
        self.assertContains(resp, 'Pedir lectura ahora')
        self.assertNotContains(resp, 'disabled')

    @patch('apps.catalogo.services.enviar_consultar_red_farmacia', return_value=True)
    def test_pedir_lectura_le_habla_a_la_estacion_de_la_propia_farmacia(self, mock_enviar):
        estacion = self._estacion_online()
        resp = self.client.post(reverse('panel:enlace_farmacia_solicitar', args=[self.farmacia.pk]))
        self.assertEqual(resp.status_code, 200)
        mock_enviar.assert_called_once()
        # La comunidad SNMP es el código de la farmacia en minúscula (ver mikrotik.py).
        self.assertEqual(mock_enviar.call_args[0][0], estacion)
        self.assertEqual(mock_enviar.call_args[0][1], 'ml001')

    @patch('apps.catalogo.services.enviar_consultar_red_farmacia', return_value=True)
    def test_pedir_lectura_queda_auditado(self, _mock):
        self._estacion_online()
        self.client.post(reverse('panel:enlace_farmacia_solicitar', args=[self.farmacia.pk]))
        self.assertTrue(EventoAuditoria.objects.filter(accion='farmacia.consultar_red').exists())

    @patch('apps.monitoreo.mikrotik.sondear_y_guardar_farmacia', return_value=False)
    def test_si_no_hay_snmp_ni_agente_dice_como_habilitarlo(self, _mock):
        """El mensaje tiene que ser accionable: sin SNMP y sin agente, lo que falta es
        habilitar SNMP en el router, y con que comunidad."""
        resp = self.client.post(reverse('panel:enlace_farmacia_solicitar', args=[self.farmacia.pk]))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'ml001')
        self.assertContains(resp, 'probar_snmp_farmacia')

    @patch('apps.monitoreo.mikrotik.sondear_y_guardar_farmacia', return_value=True)
    def test_intenta_snmp_directo_antes_que_el_agente(self, mock_snmp):
        """El orden importa: el SNMP directo cubre las ~700 farmacias y deja el dato
        guardado antes de responder; pedirselo al agente solo funciona donde hay uno y
        el dato llega despues por MQTT."""
        self._estacion_online()
        with patch('apps.catalogo.services.enviar_consultar_red_farmacia') as mock_agente:
            self.client.post(reverse('panel:enlace_farmacia_solicitar', args=[self.farmacia.pk]))
        mock_snmp.assert_called_once()
        mock_agente.assert_not_called()

    def test_solicitar_exige_permiso(self):
        sin_permiso = User.objects.create_user(username='modal_pelado', password='x')
        PerfilUsuario.objects.create(usuario=sin_permiso, acceso_todas_unidades=True)
        sin_permiso.user_permissions.add(
            Permission.objects.get(content_type__app_label='monitoreo', codename='view_estadoenlacefarmacia'),
        )
        self.client.force_login(sin_permiso)
        resp = self.client.post(reverse('panel:enlace_farmacia_solicitar', args=[self.farmacia.pk]))
        self.assertEqual(resp.status_code, 403)

    def test_aislamiento_por_unidad_de_negocio(self):
        ajeno = User.objects.create_user(username='modal_ajeno', password='x')
        perfil = PerfilUsuario.objects.create(usuario=ajeno, acceso_todas_unidades=False)
        perfil.unidades_negocio.set([self.mia])
        ajeno.user_permissions.add(
            Permission.objects.get(content_type__app_label='monitoreo', codename='view_estadoenlacefarmacia'),
        )
        self.client.force_login(ajeno)
        resp = self.client.get(reverse('panel:enlace_farmacia_modal', args=[self.farmacia.pk]))
        self.assertEqual(resp.status_code, 403)

    def test_la_pantalla_vieja_redirige_a_la_nueva(self):
        """Eran dos listados de las mismas 700 farmacias; se fusionaron. La URL vieja se
        conserva porque hay enlaces guardados apuntando ahí."""
        resp = self.client.get(reverse('panel:red_farmacias_lista'))
        self.assertRedirects(resp, reverse('panel:enlaces_farmacias_lista'))

    def test_el_listado_trae_el_ancho_de_banda_sin_consultar_por_fila(self):
        from apps.monitoreo.models import MuestraRedFarmacia

        MuestraRedFarmacia.objects.create(
            farmacia=self.farmacia, bytes_recibidos=1000, bytes_enviados=500,
            red_recibido_kbps=120.0, red_enviado_kbps=30.0,
        )
        resp = self.client.get(reverse('panel:enlaces_farmacias_lista'))
        self.assertEqual(resp.context['con_ancho_banda'], 1)
        self.assertEqual(resp.context['filas'][0]['total_kbps'], 150.0)


class EnlacesListaFiltrosYPaginacionTests(TestCase):
    """Filtros, buscador y paginación server-side de /monitoreo/enlaces/.

    Todo del lado del servidor a propósito: a 700 farmacias (y subiendo), traer la tabla
    entera y filtrar en el navegador manda todo el listado en cada carga para mostrar las
    pocas filas que importan.
    """

    def setUp(self):
        from apps.monitoreo.enlaces import registrar_sondeo
        from apps.monitoreo.models import EstadoEnlaceFarmacia, MuestraRedFarmacia

        self.sg = UnidadNegocio.objects.get(codigo='SG')
        self.g1 = Grupo.objects.create(codigo='TRX001')
        self.g2 = Grupo.objects.create(codigo='TRX002')

        # 30 farmacias para que haya más de una página (25 por página).
        self.farmacias = []
        for i in range(1, 31):
            grupo = self.g1 if i <= 20 else self.g2
            self.farmacias.append(Farmacia.objects.create(
                codigo=f'ML{i:03d}', grupo=grupo, unidad_negocio=self.sg,
                ip_router=f'10.20.30.{i}',
                circuito_proveedor='telconet-norte' if i <= 5 else '',
            ))
        # Sin IP: no se puede sondear, no debe aparecer nunca.
        Farmacia.objects.create(codigo='ML999', grupo=self.g1, unidad_negocio=self.sg)

        registrar_sondeo(self.farmacias[0], True, 15.0)   # ML001 activa
        # ML002 responde primero y DESPUÉS se cae: una caída real implica que alguna vez
        # estuvo arriba. Sin ese primer sondeo exitoso el estado sería "nunca respondió",
        # que el panel cuenta aparte (ver EstadoEnlaceFarmacia.nunca_respondio).
        registrar_sondeo(self.farmacias[1], True, 18.0)
        for _ in range(EstadoEnlaceFarmacia.UMBRAL_FALLAS_CONSECUTIVAS):
            registrar_sondeo(self.farmacias[1], False, None)  # ML002 caída
        MuestraRedFarmacia.objects.create(
            farmacia=self.farmacias[0], bytes_recibidos=1, bytes_enviados=1,
            red_recibido_kbps=100.0, red_enviado_kbps=50.0,
        )

        self.usuario = User.objects.create_user(username='u_enlaces_ui', password='x')
        PerfilUsuario.objects.create(usuario=self.usuario, acceso_todas_unidades=True)
        self.usuario.user_permissions.add(
            Permission.objects.get(content_type__app_label='monitoreo', codename='view_estadoenlacefarmacia'),
        )
        self.client.force_login(self.usuario)
        self.url = reverse('panel:enlaces_farmacias_lista')

    def test_pagina_25_y_no_trae_las_700(self):
        resp = self.client.get(self.url)
        self.assertEqual(len(resp.context['filas']), 25)
        self.assertEqual(resp.context['pagina'].paginator.count, 30)
        self.assertEqual(len(self.client.get(self.url, {'pagina': 2}).context['filas']), 5)

    def test_los_kpi_son_del_total_no_de_la_pagina(self):
        """Si el contador dijera 25 porque la página trae 25, el número dejaría de servir
        para decidir si hay que actuar."""
        resp = self.client.get(self.url)
        self.assertEqual(resp.context['total'], 30)
        self.assertEqual(resp.context['activas'], 1)
        self.assertEqual(resp.context['caidas'], 1)
        self.assertEqual(resp.context['sin_sondear'], 28)
        self.assertEqual(resp.context['con_ancho_banda'], 1)

    def test_los_caidos_van_primero(self):
        resp = self.client.get(self.url)
        self.assertEqual(resp.context['filas'][0]['farmacia'].codigo, 'ML002')

    def test_filtra_por_estado(self):
        resp = self.client.get(self.url, {'estado': 'caidos'})
        self.assertEqual([f['farmacia'].codigo for f in resp.context['filas']], ['ML002'])
        self.assertEqual(self.client.get(self.url, {'estado': 'activos'}).context['pagina'].paginator.count, 1)
        self.assertEqual(self.client.get(self.url, {'estado': 'sin_sondear'}).context['pagina'].paginator.count, 28)

    def test_filtra_por_grupo(self):
        resp = self.client.get(self.url, {'grupo': 'TRX002'})
        self.assertEqual(resp.context['pagina'].paginator.count, 10)

    def test_filtra_por_disponibilidad_de_trafico(self):
        self.assertEqual(self.client.get(self.url, {'trafico': 'con'}).context['pagina'].paginator.count, 1)
        self.assertEqual(self.client.get(self.url, {'trafico': 'sin'}).context['pagina'].paginator.count, 29)

    def test_busca_por_codigo_circuito_e_ip(self):
        self.assertEqual(self.client.get(self.url, {'q': 'ML007'}).context['pagina'].paginator.count, 1)
        self.assertEqual(self.client.get(self.url, {'q': 'telconet'}).context['pagina'].paginator.count, 5)
        # La IP es `inet` en PostgreSQL: sin el cast a texto esta consulta reventaría en
        # producción aunque pase en SQLite.
        self.assertEqual(self.client.get(self.url, {'q': '10.20.30.7'}).context['pagina'].paginator.count, 1)

    def test_los_filtros_se_combinan(self):
        resp = self.client.get(self.url, {'grupo': 'TRX001', 'estado': 'caidos'})
        self.assertEqual([f['farmacia'].codigo for f in resp.context['filas']], ['ML002'])

    def test_la_paginacion_conserva_los_filtros(self):
        """Sin esto, pasar de página descarta el filtro y el operador vuelve a las 700."""
        resp = self.client.get(self.url, {'grupo': 'TRX001', 'pagina': 1})
        self.assertIn('grupo=TRX001', resp.context['query_filtros'])
        self.assertNotIn('pagina', resp.context['query_filtros'])

    def test_farmacia_sin_ip_nunca_aparece(self):
        resp = self.client.get(self.url, {'q': 'ML999'})
        self.assertEqual(resp.context['pagina'].paginator.count, 0)

    def test_solo_ofrece_los_grupos_que_existen(self):
        resp = self.client.get(self.url)
        self.assertEqual([g.codigo for g in resp.context['grupos']], ['TRX001', 'TRX002'])

    def test_las_caidas_en_curso_no_empujan_la_tabla(self):
        """154 caídas abiertas empujaban la tabla fuera de la pantalla. Desde el 26-oct-2026
        no hay lista aparte: la tarjeta cuenta y su filtro las muestra en la tabla."""
        resp = self.client.get(self.url)
        self.assertNotContains(resp, 'Caídas en curso')
        self.assertEqual(resp.context['caidas'], 1)
        self.assertEqual(
            resp.context['caidas'],
            len(self.client.get(self.url, {'estado': 'caidos'}).context['filas']),
        )


class UmbralesDeRecursosTests(TestCase):
    """Los colores de CPU/RAM/disco salen de un solo cálculo.

    Auditoría del 12-sep-2026: los seis umbrales estaban escritos como literales en las
    DOS vistas que los usan (`monitoreo_lista` y `monitoreo_detalle_partial`). Ajustar
    uno y olvidar el otro pintaba la misma estación de un color en la lista y de otro en
    su ficha, sin que nada fallara. Estas pruebas fijan que no puedan divergir.
    """

    def setUp(self):
        grupo = Grupo.objects.create(codigo='TRX001')
        farmacia = Farmacia.objects.create(
            codigo='ML001', grupo=grupo, unidad_negocio=UnidadNegocio.objects.get(codigo='SG'),
        )
        self.estacion = Estacion.objects.create(
            codigo='ML001-ADM', farmacia=farmacia, monitorear_recursos=True,
            estado_aprobacion=Estacion.EstadoAprobacion.APROBADA,
        )
        self.usuario = User.objects.create_user(username='u_umbrales', password='x')
        PerfilUsuario.objects.create(usuario=self.usuario, acceso_todas_unidades=True)
        self.usuario.user_permissions.add(
            Permission.objects.get(content_type__app_label='monitoreo', codename='view_muestrametrica'),
        )
        self.client.force_login(self.usuario)

    def _muestra(self, cpu, ram_pct, disco_pct):
        return MuestraMetrica.objects.create(
            estacion=self.estacion, cpu_carga_pct=cpu,
            ram_total=1000, ram_usada=int(ram_pct * 10),
            disco_total_gb=100.0, disco_libre_gb=100.0 - disco_pct,
        )

    def _colores_de_ambas_pantallas(self):
        lista = self.client.get(reverse('panel:monitoreo_lista'))
        detalle = self.client.get(reverse('panel:monitoreo_detalle_partial', args=[self.estacion.pk]))
        claves = ('estado_cpu', 'estado_ram', 'estado_disco')
        return (
            {k: lista.context['tarjetas'][0][k] for k in claves},
            {k: detalle.context[k] for k in claves},
        )

    def test_la_lista_y_el_detalle_pintan_igual_la_misma_estacion(self):
        """Es la prueba central: mismo dato, mismo color en las dos pantallas."""
        for cpu, ram, disco in ((10, 10, 10), (78, 85, 88), (95, 96, 98)):
            with self.subTest(cpu=cpu):
                MuestraMetrica.objects.all().delete()
                self._muestra(cpu, ram, disco)
                en_lista, en_detalle = self._colores_de_ambas_pantallas()
                self.assertEqual(en_lista, en_detalle)

    def test_los_tres_niveles_se_clasifican_donde_corresponde(self):
        from apps.panel.views.monitoreo import estados_de_recursos

        self.assertEqual(estados_de_recursos(self._muestra(10, 10, 10))['estado_cpu'], 'ok')
        MuestraMetrica.objects.all().delete()
        self.assertEqual(estados_de_recursos(self._muestra(78, 10, 10))['estado_cpu'], 'warning')
        MuestraMetrica.objects.all().delete()
        self.assertEqual(estados_de_recursos(self._muestra(95, 10, 10))['estado_cpu'], 'critical')

    def test_sin_muestra_no_inventa_un_color(self):
        """null no es cero: una estación que nunca reportó no está "en verde"."""
        from apps.panel.views.monitoreo import estados_de_recursos

        self.assertEqual(
            estados_de_recursos(None),
            {'estado_cpu': 'sin_dato', 'estado_ram': 'sin_dato', 'estado_disco': 'sin_dato'},
        )

    def test_cambiar_un_umbral_cambia_las_dos_pantallas_a_la_vez(self):
        """El objetivo real del cambio: que no exista un segundo camino al color. Si
        alguien vuelve a poner literales en una vista, esta prueba falla.

        Se sustituye el umbral en `apps.panel.indicadores` y no en la vista: desde el
        rediseño de gráficas (19-sep-2026) el color del número y la línea de umbral que
        se dibuja sobre su sparkline salen los dos de ahí. Que las vistas NO redefinan
        los umbrales lo sigue vigilando
        `DivisionDeVistasTests.test_los_umbrales_de_color_viven_en_un_solo_lugar`.
        """
        from apps.panel import indicadores

        self._muestra(50, 10, 10)  # con los umbrales reales, 50% de CPU es 'ok'
        with patch.object(indicadores, 'UMBRAL_CPU_WARNING_PCT', 40):
            en_lista, en_detalle = self._colores_de_ambas_pantallas()
        self.assertEqual(en_lista['estado_cpu'], 'warning')
        self.assertEqual(en_detalle['estado_cpu'], 'warning')


class EstacionesListaPaginacionTests(TestCase):
    """Paginación y filtros de /estaciones/ (prioridad 2 de la auditoría del 12-sep-2026).

    Se paginó ANTES del rollout del agente a propósito: hoy son 8 estaciones y no duele,
    pero el parque son ~1.800. Paginar una lista de 8 filas es barato; hacerlo cuando ya
    hay 1.800 en producción, no.
    """

    def setUp(self):
        self.sg = UnidadNegocio.objects.get(codigo='SG')
        self.grupo = Grupo.objects.create(codigo='TRX001', version_objetivo='4.2.1')
        # Grupo sin versión objetivo: sus estaciones NUNCA cuentan como desactualizadas.
        self.grupo_sin_objetivo = Grupo.objects.create(codigo='TRX002', version_objetivo='')
        farmacia = Farmacia.objects.create(codigo='ML001', grupo=self.grupo, unidad_negocio=self.sg)
        self.otra = Farmacia.objects.create(
            codigo='ML002', grupo=self.grupo_sin_objetivo, unidad_negocio=self.sg,
        )

        for i in range(1, 31):
            Estacion.objects.create(
                codigo=f'ML001-{i:02d}', farmacia=farmacia,
                estado_aprobacion=Estacion.EstadoAprobacion.APROBADA,
                # 10 en la versión objetivo, 20 atrasadas (una de ellas sin reportar).
                version_pos='4.2.1' if i <= 10 else ('' if i == 11 else '4.1.0'),
            )
        Estacion.objects.create(
            codigo='ML002-01', farmacia=self.otra,
            estado_aprobacion=Estacion.EstadoAprobacion.APROBADA, version_pos='9.9.9',
        )

        self.usuario = User.objects.create_user(username='u_est_pag', password='x')
        PerfilUsuario.objects.create(usuario=self.usuario, acceso_todas_unidades=True)
        self.usuario.user_permissions.add(
            Permission.objects.get(content_type__app_label='catalogo', codename='view_estacion'),
        )
        self.client.force_login(self.usuario)
        self.url = reverse('panel:estaciones_lista')

    def test_pagina_de_a_25(self):
        resp = self.client.get(self.url)
        self.assertEqual(len(resp.context['estaciones']), 25)
        self.assertEqual(resp.context['pagina'].paginator.count, 31)
        self.assertEqual(len(self.client.get(self.url, {'pagina': 2}).context['estaciones']), 6)

    def test_el_filtro_de_desactualizadas_se_resuelve_en_la_base(self):
        """Antes era una comprensión de lista sobre el queryset: traía la tabla entera a
        memoria y dejaba sin efecto la paginación. El resultado tiene que ser el mismo que
        daba la property `Estacion.desactualizada`."""
        resp = self.client.get(self.url, {'desactualizadas': '1'})
        codigos = set(Estacion.objects.filter(pk__in=[e.pk for e in resp.context['pagina'].object_list]).values_list('codigo', flat=True))
        esperado = {e.codigo for e in Estacion.objects.all() if e.desactualizada}
        # La página trae 20; el total del paginador es el universo filtrado.
        self.assertEqual(resp.context['pagina'].paginator.count, len(esperado))
        self.assertTrue(codigos.issubset(esperado))

    def test_una_estacion_que_nunca_reporto_cuenta_como_desactualizada(self):
        """version_pos='' contra un objetivo definido es "atrasada", no "sin dato" —
        mismo criterio que la property original."""
        sin_reportar = Estacion.objects.get(codigo='ML001-11')
        self.assertTrue(sin_reportar.desactualizada)
        resp = self.client.get(self.url, {'desactualizadas': '1', 'pagina': 1})
        todos = list(resp.context['pagina'].paginator.object_list.values_list('codigo', flat=True))
        self.assertIn('ML001-11', todos)

    def test_un_grupo_sin_version_objetivo_nunca_esta_desactualizado(self):
        resp = self.client.get(self.url, {'desactualizadas': '1'})
        todos = list(resp.context['pagina'].paginator.object_list.values_list('codigo', flat=True))
        self.assertNotIn('ML002-01', todos)

    def test_la_paginacion_conserva_los_filtros(self):
        resp = self.client.get(self.url, {'grupo': 'TRX001', 'pagina': 2})
        self.assertIn('grupo=TRX001', resp.context['query_filtros'])
        self.assertNotIn('pagina', resp.context['query_filtros'])


class MonitoreoListaConsultasTests(TestCase):
    """La lista de servidores no hace una consulta por servidor.

    Era `estacion.metricas.first()` dentro del bucle. Con 4 servidores no se notaba, pero
    `monitorear_recursos` se activa en el servidor de cada farmacia: a escala de rollout
    son ~700 consultas por carga.
    """

    def setUp(self):
        grupo = Grupo.objects.create(codigo='TRX001')
        farmacia = Farmacia.objects.create(
            codigo='ML001', grupo=grupo, unidad_negocio=UnidadNegocio.objects.get(codigo='SG'),
        )
        self.estaciones = []
        for i in range(1, 11):
            e = Estacion.objects.create(
                codigo=f'ML001-{i:02d}', farmacia=farmacia, monitorear_recursos=True,
                estado_aprobacion=Estacion.EstadoAprobacion.APROBADA,
            )
            self.estaciones.append(e)
            for cpu in (10.0, 20.0, float(30 + i)):  # la última es la que debe mostrarse
                MuestraMetrica.objects.create(
                    estacion=e, cpu_carga_pct=cpu, ram_total=1000, ram_usada=100,
                    disco_total_gb=100.0, disco_libre_gb=90.0,
                )

        usuario = User.objects.create_user(username='u_mon_q', password='x')
        PerfilUsuario.objects.create(usuario=usuario, acceso_todas_unidades=True)
        usuario.user_permissions.add(
            Permission.objects.get(content_type__app_label='monitoreo', codename='view_muestrametrica'),
        )
        self.client.force_login(usuario)

    def test_el_numero_de_consultas_no_crece_con_los_servidores(self):
        """Lo que se fija no es un número exacto sino que sea CONSTANTE: con 10 servidores
        tiene que hacer las mismas consultas que con 2."""
        with self.assertNumQueries(self._consultas_con(2)):
            self.client.get(reverse('panel:monitoreo_lista'))

    def _consultas_con(self, cuantos):
        Estacion.objects.filter(monitorear_recursos=True).exclude(
            pk__in=[e.pk for e in self.estaciones[:cuantos]],
        ).update(monitorear_recursos=False)
        from django.test.utils import CaptureQueriesContext
        from django.db import connection
        with CaptureQueriesContext(connection) as ctx:
            self.client.get(reverse('panel:monitoreo_lista'))
        Estacion.objects.filter(farmacia__codigo='ML001').update(monitorear_recursos=True)
        return len(ctx)

    def test_muestra_la_lectura_mas_reciente_de_cada_servidor(self):
        resp = self.client.get(reverse('panel:monitoreo_lista'))
        por_codigo = {t['estacion'].codigo: t['ultima'] for t in resp.context['tarjetas']}
        for i, e in enumerate(self.estaciones, start=1):
            self.assertEqual(por_codigo[e.codigo].cpu_carga_pct, float(30 + i))

    def test_un_servidor_sin_muestras_no_rompe_la_lista(self):
        Estacion.objects.create(
            codigo='ML001-99', farmacia=self.estaciones[0].farmacia, monitorear_recursos=True,
            estado_aprobacion=Estacion.EstadoAprobacion.APROBADA,
        )
        resp = self.client.get(reverse('panel:monitoreo_lista'))
        vacia = [t for t in resp.context['tarjetas'] if t['estacion'].codigo == 'ML001-99'][0]
        self.assertIsNone(vacia['ultima'])
        self.assertEqual(vacia['estado_cpu'], 'sin_dato')


class ResumenDeProgresoTests(TestCase):
    """Aritmética de la barra de progreso (`apps.panel.progreso`).

    Estaba duplicada entre despliegues y software, y —hallazgo de la auditoría— **sin
    ninguna prueba**: el refactor que la unificó no tenía red. Estas pruebas la fijan,
    incluidos los casos borde que un cálculo ingenuo se come.
    """

    def setUp(self):
        self.sg = UnidadNegocio.objects.get(codigo='SG')
        grupo = Grupo.objects.create(codigo='TRX001')
        self.farmacia = Farmacia.objects.create(codigo='ML001', grupo=grupo, unidad_negocio=self.sg)
        self.usuario = User.objects.create_user(username='u_prog', password='x')
        self.despliegue = Despliegue.objects.create(
            version='4.2.1', archivo=SimpleUploadedFile('p.zip', b'x'),
            modo_aplicacion=Despliegue.ModoAplicacion.INMEDIATO, unidad_negocio=self.sg,
            destino_tipo=Despliegue.DestinoTipo.ESTACIONES, creado_por=self.usuario,
        )

    def _resultado(self, codigo, estado):
        from apps.despliegues.models import ResultadoDespliegue

        estacion = Estacion.objects.create(
            codigo=codigo, farmacia=self.farmacia,
            estado_aprobacion=Estacion.EstadoAprobacion.APROBADA,
        )
        return ResultadoDespliegue.objects.create(
            despliegue=self.despliegue, estacion=estacion, estado=estado,
        )

    def _resumen(self):
        from apps.despliegues.models import ResultadoDespliegue
        from apps.panel.progreso import resumen_de_progreso

        estados = ResultadoDespliegue.Estado
        return resumen_de_progreso(
            self.despliegue.resultados.all(), estados,
            estado_ok=estados.APLICADO,
            estados_error=(estados.ERROR, estados.ROLLBACK),
            etiqueta_completados='aplicados',
            estados_visibles=(estados.PENDIENTE, estados.DESCARGANDO, estados.APLICANDO),
        )

    def test_sin_resultados_no_divide_por_cero(self):
        """Un envío recién creado todavía no publicó a ninguna estación."""
        resumen = self._resumen()
        self.assertEqual((resumen.total, resumen.completados, resumen.pct_completado), (0, 0, 0))

    def test_cuenta_y_calcula_el_porcentaje(self):
        from apps.despliegues.models import ResultadoDespliegue

        estados = ResultadoDespliegue.Estado
        self._resultado('ML001-A', estados.APLICADO)
        self._resultado('ML001-B', estados.APLICADO)
        self._resultado('ML001-C', estados.PENDIENTE)
        self._resultado('ML001-D', estados.DESCARGANDO)

        resumen = self._resumen()
        self.assertEqual(resumen.total, 4)
        self.assertEqual(resumen.completados, 2)
        self.assertEqual(resumen.pct_completado, 50)

    def test_el_rollback_cuenta_como_error(self):
        """La estación quedó sin la versión nueva, aunque el agente haya sabido volver
        atrás sin romper el POS. Contarlo como "no error" escondería el fracaso."""
        from apps.despliegues.models import ResultadoDespliegue

        estados = ResultadoDespliegue.Estado
        self._resultado('ML001-A', estados.ERROR)
        self._resultado('ML001-B', estados.ROLLBACK)
        self._resultado('ML001-C', estados.APLICADO)

        self.assertEqual(self._resumen().errores, 2)

    def test_el_desglose_respeta_el_orden_pedido(self):
        from apps.despliegues.models import ResultadoDespliegue

        estados = ResultadoDespliegue.Estado
        self._resultado('ML001-A', estados.PENDIENTE)
        self._resultado('ML001-B', estados.APLICANDO)

        etiquetas = [p['etiqueta'] for p in self._resumen().desglose]
        self.assertEqual(etiquetas, ['Pendiente', 'Descargando', 'Aplicando'])

    def test_la_instalacion_no_cuenta_rollback(self):
        """Software solo tiene ERROR: pasarle los mismos estados que a despliegues
        reventaría. Cada envío declara los suyos."""
        from apps.panel.progreso import resumen_de_progreso
        from apps.software.models import ResultadoInstalacion

        estados = ResultadoInstalacion.Estado
        resumen = resumen_de_progreso(
            ResultadoInstalacion.objects.none(), estados,
            estado_ok=estados.INSTALADO, estados_error=(estados.ERROR,),
            etiqueta_completados='instalados',
            estados_visibles=(estados.PENDIENTE, estados.DESCARGANDO, estados.INSTALANDO),
        )
        self.assertEqual(resumen.etiqueta_completados, 'instalados')
        self.assertEqual([p['etiqueta'] for p in resumen.desglose],
                         ['Pendiente', 'Descargando', 'Instalando'])

    def test_la_vista_de_despliegue_renderiza_el_progreso(self):
        from apps.despliegues.models import ResultadoDespliegue

        self._resultado('ML001-A', ResultadoDespliegue.Estado.APLICADO)
        PerfilUsuario.objects.create(usuario=self.usuario, acceso_todas_unidades=True)
        self.usuario.user_permissions.add(
            Permission.objects.get(content_type__app_label='despliegues', codename='view_despliegue'),
        )
        self.client.force_login(self.usuario)
        resp = self.client.get(
            reverse('panel:despliegue_progreso_partial', args=[self.despliegue.pk]),
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.context['progreso'].pct_completado, 100)
        self.assertContains(resp, '1 / 1 aplicados')


class ActivoTopologiaEnElPanelTests(TestCase):
    """La columna "Puesto / IP" del listado y la tarjeta "Red y puesto" de la ficha.

    Hasta ahora los campos de topología existían en el modelo pero solo se veían desde el
    admin de Django, o sea que el trabajo era invisible para cualquiera que no fuera
    administrador.
    """

    def setUp(self):
        grupo = Grupo.objects.create(codigo='TRX016')
        self.sg = UnidadNegocio.objects.get(codigo='SG')
        self.farmacia = Farmacia.objects.create(codigo='ML016', grupo=grupo, unidad_negocio=self.sg)
        self.estacion = Estacion.objects.create(
            codigo='ML016-A', farmacia=self.farmacia, ip_lan='10.201.7.229',
            estado_aprobacion=Estacion.EstadoAprobacion.APROBADA,
        )
        self.mikrotik = Activo.objects.create(
            codigo='CR-NET-0001', tipo=Activo.Tipo.RED, unidad_negocio=self.sg,
            farmacia=self.farmacia, slot='mikrotik', ip='10.201.7.225',
            mac='AA:BB:CC:DD:EE:01', ubicacion_interna='rack', estado=Activo.Estado.ASIGNADO,
        )
        self.caja = Activo.objects.create(
            codigo='CR-DSK-0006', tipo=Activo.Tipo.DESKTOP, unidad_negocio=self.sg,
            farmacia=self.farmacia, slot='estacion_A', estacion=self.estacion,
            estado=Activo.Estado.ASIGNADO,
        )

        self.usuario = User.objects.create_user(username='u_topo_panel', password='x')
        PerfilUsuario.objects.create(usuario=self.usuario, acceso_todas_unidades=True)
        self.usuario.user_permissions.add(
            Permission.objects.get(content_type__app_label='activos', codename='view_activo'),
        )
        self.client.force_login(self.usuario)

    def test_el_listado_muestra_el_puesto_y_la_ip(self):
        resp = self.client.get(reverse('panel:activos_lista'))
        self.assertContains(resp, 'mikrotik')
        self.assertContains(resp, '10.201.7.225')

    def test_la_ip_de_un_activo_con_estacion_sale_del_agente(self):
        """Regla de Fase A: con estación vinculada manda `Estacion.ip_lan`, que es la
        única que se actualiza sola."""
        resp = self.client.get(reverse('panel:activos_lista'))
        self.assertContains(resp, '10.201.7.229')

    def test_el_listado_no_hace_una_consulta_por_activo(self):
        """`ip_efectiva` lee `estacion.ip_lan`: sin select_related sería una consulta por
        fila — el mismo N+1 que la auditoría sacó de monitoreo_lista. Lo que se fija no es
        el número de consultas sino que no crezca al agregar activos."""
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        with CaptureQueriesContext(connection) as ctx:
            self.client.get(reverse('panel:activos_lista'))
        con_dos_activos = len(ctx.captured_queries)

        for i in range(4):
            estacion = Estacion.objects.create(
                codigo='ML016-%s' % 'BCDE'[i], farmacia=self.farmacia,
                ip_lan='10.201.7.%d' % (240 + i),
                estado_aprobacion=Estacion.EstadoAprobacion.APROBADA,
            )
            Activo.objects.create(
                codigo='CR-DSK-01%02d' % i, tipo=Activo.Tipo.DESKTOP, unidad_negocio=self.sg,
                farmacia=self.farmacia, estacion=estacion, estado=Activo.Estado.ASIGNADO,
            )

        with CaptureQueriesContext(connection) as ctx:
            self.client.get(reverse('panel:activos_lista'))
        self.assertEqual(len(ctx.captured_queries), con_dos_activos)

    def test_la_ficha_muestra_de_donde_salio_la_ip(self):
        """Un dato que reporta el agente y uno tipeado a mano no se tratan igual, así que
        la ficha dice cuál es cuál."""
        resp = self.client.get(reverse('panel:activo_detalle', args=[self.caja.pk]))
        self.assertContains(resp, 'Red y puesto')
        self.assertContains(resp, 'estacion_A')
        self.assertContains(resp, 'agente')

        resp = self.client.get(reverse('panel:activo_detalle', args=[self.mikrotik.pk]))
        self.assertContains(resp, 'manual')
        self.assertContains(resp, 'AA:BB:CC:DD:EE:01')
        self.assertContains(resp, 'Rack')

    def test_la_ficha_avisa_cuando_falta_cargar_la_ip(self):
        sin_datos = Activo.objects.create(
            codigo='CR-BIO-0001', tipo=Activo.Tipo.BIOMETRICO, unidad_negocio=self.sg,
            farmacia=self.farmacia, slot='biometrico', estado=Activo.Estado.ASIGNADO,
        )
        resp = self.client.get(reverse('panel:activo_detalle', args=[sin_datos.pk]))
        self.assertContains(resp, 'falta cargar')

    def test_un_activo_de_oficina_no_muestra_la_tarjeta_de_red(self):
        """Una laptop en bodega no pertenece a ningún esquema de farmacia: mostrarle
        "puesto: sin asignar" sería ruido en todas las fichas del inventario."""
        laptop = Activo.objects.create(
            codigo='CR-LAP-0099', tipo=Activo.Tipo.LAPTOP, unidad_negocio=self.sg,
        )
        resp = self.client.get(reverse('panel:activo_detalle', args=[laptop.pk]))
        self.assertNotContains(resp, 'Red y puesto')


class EstacionesRelojEnElPanelTests(TestCase):
    """Columna "Reloj" y su filtro en el listado de estaciones.

    El objetivo del filtro es poder responder "¿a cuántas les pasa?" sin mirar de a una.
    Con 8 estaciones da igual; con ~1.800 es la diferencia entre verlo y no verlo.
    """

    def setUp(self):
        grupo = Grupo.objects.create(codigo='TRX001')
        self.sg = UnidadNegocio.objects.get(codigo='SG')
        self.farmacia = Farmacia.objects.create(codigo='ML001', grupo=grupo, unidad_negocio=self.sg)

        self.en_hora = self._estacion('ML001-A', desfase_reloj_segundos=0, offset_utc_minutos=-300)
        self.corrida = self._estacion('ML001-B', desfase_reloj_segundos=45, offset_utc_minutos=-300)
        self.incomunicada = self._estacion('ML001-C', desfase_reloj_segundos=-400, offset_utc_minutos=-300)
        self.zona_mala = self._estacion('ML001-D', desfase_reloj_segundos=2, offset_utc_minutos=-180)
        # Mismo huso que Ecuador (UTC-5) pero otra región: si el filtro solo mirara el
        # offset, esta pasaría desapercibida. Es el caso real de ML016-B (14-sep-2026).
        self.zona_mexico = self._estacion(
            'ML001-F', desfase_reloj_segundos=5, offset_utc_minutos=-300,
            zona_horaria='Eastern Standard Time (Mexico)',
        )
        self.sin_dato = self._estacion('ML001-E')

        self.usuario = User.objects.create_user(username='u_reloj', password='x')
        PerfilUsuario.objects.create(usuario=self.usuario, acceso_todas_unidades=True)
        self.usuario.user_permissions.add(
            Permission.objects.get(content_type__app_label='catalogo', codename='view_estacion'),
        )
        self.client.force_login(self.usuario)

    def _estacion(self, codigo, **extra):
        return Estacion.objects.create(
            codigo=codigo, farmacia=self.farmacia,
            estado_aprobacion=Estacion.EstadoAprobacion.APROBADA, **extra,
        )

    def _codigos(self, **filtros):
        resp = self.client.get(reverse('panel:estaciones_lista'), filtros)
        self.assertEqual(resp.status_code, 200)
        return {e.codigo for e in resp.context['estaciones']}

    def test_sin_filtro_salen_todas(self):
        self.assertEqual(len(self._codigos()), 6)

    def test_filtra_las_que_ya_no_reciben_comandos(self):
        self.assertEqual(self._codigos(reloj='incomunicado'), {'ML001-C'})

    def test_el_filtro_de_reloj_corrido_incluye_a_las_incomunicadas(self):
        """Una estación con 400 s de desfase también tiene el reloj corrido: el filtro
        más amplio no puede esconder al caso más grave."""
        self.assertEqual(self._codigos(reloj='desincronizado'), {'ML001-B', 'ML001-C'})

    def test_filtra_por_zona_horaria_sin_mirar_el_desfase(self):
        """ML001-D tiene el reloj casi perfecto y la región mal: son problemas distintos.
        ML001-F tiene el offset correcto y la región de México — el filtro tiene que
        atrapar a las dos, o la segunda queda invisible para siempre."""
        self.assertEqual(self._codigos(reloj='zona'), {'ML001-D', 'ML001-F'})

    def test_la_estacion_sin_dato_no_aparece_en_ningun_filtro(self):
        """Todavía no reportó (agente viejo o nunca conectó). No se la acusa de nada."""
        for filtro in ('incomunicado', 'desincronizado', 'zona'):
            self.assertNotIn('ML001-E', self._codigos(reloj=filtro))

    def test_la_columna_distingue_en_hora_de_sin_dato(self):
        """0 segundos es el valor perfecto, no "sin dato": si la plantilla lo tratara como
        vacío, la estación mejor sincronizada se vería igual que una que nunca reportó."""
        resp = self.client.get(reverse('panel:estaciones_lista'), {'estado_conexion': ''})
        contenido = resp.content.decode()
        self.assertIn('en hora', contenido)
        self.assertIn('sin comandos', contenido)

    def test_el_filtro_no_trae_la_tabla_entera_a_memoria(self):
        """El filtro corre en base, no evaluando las properties en Python: si no, la
        paginación no serviría de nada (mismo problema que tuvo `desactualizadas`)."""
        resp = self.client.get(reverse('panel:estaciones_lista'), {'reloj': 'incomunicado'})
        self.assertEqual(resp.context['pagina'].paginator.count, 1)


class EstacionesTarjetasPulsablesTests(TestCase):
    """Los indicadores del listado de estaciones filtran al pulsarlos, como en enlaces.

    Lo que cuidan estas pruebas no es que el número se vea, sino que **el número y la
    lista digan lo mismo**. Una tarjeta que dice 3 y al pulsarla muestra 2 filas es peor
    que no tener tarjeta: el operador deja de confiar en toda la pantalla.
    """

    def setUp(self):
        self.sg = UnidadNegocio.objects.get(codigo='SG')
        # El grupo tiene versión objetivo: sin eso nada cuenta como desactualizado.
        grupo = Grupo.objects.create(codigo='TRX900', version_objetivo='2.5.0')
        self.farmacia = Farmacia.objects.create(codigo='ML900', grupo=grupo, unidad_negocio=self.sg)

        E = Estacion.EstadoConexion
        self.viva = self._estacion('ML900-A', estado_conexion=E.ONLINE, version_pos='2.5.0')
        self.caida = self._estacion('ML900-B', estado_conexion=E.OFFLINE, version_pos='2.5.0')
        self.vieja = self._estacion('ML900-C', estado_conexion=E.ONLINE, version_pos='2.4.1')
        self.sorda = self._estacion(
            'ML900-D', estado_conexion=E.ONLINE, version_pos='2.5.0', desfase_reloj_segundos=-400,
        )
        self.sin_instalar = self._estacion('ML900-E', estado_conexion=E.NUNCA_CONECTADA, version_pos='2.5.0')

        self.usuario = User.objects.create_user(username='u_tarjetas', password='x')
        PerfilUsuario.objects.create(usuario=self.usuario, acceso_todas_unidades=True)
        self.usuario.user_permissions.add(
            Permission.objects.get(content_type__app_label='catalogo', codename='view_estacion'),
        )
        self.client.force_login(self.usuario)

    def _estacion(self, codigo, **extra):
        return Estacion.objects.create(
            codigo=codigo, farmacia=self.farmacia,
            estado_aprobacion=Estacion.EstadoAprobacion.APROBADA, **extra,
        )

    def _get(self, **filtros):
        resp = self.client.get(reverse('panel:estaciones_lista'), filtros)
        self.assertEqual(resp.status_code, 200)
        return resp

    def test_los_contadores_cuentan_lo_que_hay(self):
        kpi = self._get().context['kpi']
        self.assertEqual(kpi['total'], 5)
        self.assertEqual(kpi['online'], 3)
        self.assertEqual(kpi['offline'], 1)
        self.assertEqual(kpi['nunca'], 1)
        self.assertEqual(kpi['desactualizadas'], 1)
        self.assertEqual(kpi['incomunicadas'], 1)

    def test_cada_tarjeta_muestra_exactamente_lo_que_contaba(self):
        """El caso que justifica toda la clase: si el contador y el filtro usaran
        criterios distintos, esto los separa."""
        casos = [
            ({'estado_conexion': 'online'}, 'online'),
            ({'estado_conexion': 'offline'}, 'offline'),
            ({'estado_conexion': 'nunca_conectada'}, 'nunca'),
            ({'desactualizadas': '1'}, 'desactualizadas'),
            ({'reloj': 'incomunicado'}, 'incomunicadas'),
        ]
        for filtros, clave in casos:
            with self.subTest(filtros=filtros):
                resp = self._get(**filtros)
                self.assertEqual(
                    resp.context['pagina'].paginator.count, resp.context['kpi'][clave],
                    f'la tarjeta "{clave}" no muestra lo que cuenta',
                )

    def test_los_contadores_no_se_achican_al_filtrar(self):
        """Se calculan sobre el conjunto completo: "1 fuera de línea" tiene que seguir
        diciendo 1 aunque estés viendo solo las que están en línea."""
        kpi = self._get(estado_conexion='online').context['kpi']
        self.assertEqual(kpi['total'], 5)
        self.assertEqual(kpi['offline'], 1)

    def test_la_tarjeta_activa_ofrece_quitar_el_filtro(self):
        """Pulsar dos veces la misma tarjeta vuelve a todas: si el enlace repitiera el
        filtro, no habría forma de salir sin editar la URL a mano."""
        contenido = self._get(estado_conexion='offline').content.decode()
        self.assertIn('quitar filtro', contenido)
        self.assertIn('href="?"', contenido)

    def test_respeta_el_alcance_por_unidad_de_negocio(self):
        """Los contadores salen del mismo queryset ya acotado por tenant. Si contaran
        sobre `Estacion.objects` filtrarían bien y contarían de más."""
        otra = UnidadNegocio.objects.exclude(pk=self.sg.pk).first()
        grupo_ajeno = Grupo.objects.create(codigo='TRX901', version_objetivo='2.5.0')
        ajena = Farmacia.objects.create(codigo='MI901', grupo=grupo_ajeno, unidad_negocio=otra)
        Estacion.objects.create(
            codigo='MI901-A', farmacia=ajena, version_pos='2.5.0',
            estado_conexion=Estacion.EstadoConexion.OFFLINE,
            estado_aprobacion=Estacion.EstadoAprobacion.APROBADA,
        )
        sin_alcance = User.objects.create_user(username='u_solo_sg', password='x')
        PerfilUsuario.objects.create(usuario=sin_alcance, acceso_todas_unidades=False)
        sin_alcance.perfil.unidades_negocio.add(self.sg)
        sin_alcance.user_permissions.add(
            Permission.objects.get(content_type__app_label='catalogo', codename='view_estacion'),
        )
        self.client.force_login(sin_alcance)
        kpi = self._get().context['kpi']
        self.assertEqual(kpi['total'], 5)
        self.assertEqual(kpi['offline'], 1)


class TendenciaFlotaConsultasTests(TestCase):
    """Las series de `tendencia_flota` salen de 3 consultas fijas, no de 4 por semana.

    Antes el bucle de 12 semanas hacía tres `.count()` y un `.aggregate()` por vuelta:
    **48 consultas**, y crecían con la ventana — pasar a 26 semanas las habría duplicado.
    La auditoría del 12-sep-2026 lo contó como 36 porque sumó solo los `.count()`.

    Estas pruebas cuidan dos cosas distintas: que el número de consultas no dependa de
    cuántos datos haya, y —más importante— que los valores sigan siendo **exactamente**
    los mismos. Una optimización que cambia los números en silencio es peor que la
    lentitud que venía a arreglar.
    """

    def setUp(self):
        self.sg = UnidadNegocio.objects.get(codigo='SG')
        grupo = Grupo.objects.create(codigo='TRX001')
        self.farmacia = Farmacia.objects.create(codigo='ML001', grupo=grupo, unidad_negocio=self.sg)
        self.estacion = Estacion.objects.create(
            codigo='ML001-A', farmacia=self.farmacia,
            estado_aprobacion=Estacion.EstadoAprobacion.APROBADA, monitorear_recursos=True,
        )
        self.usuario = User.objects.create_user(username='u_tend_q', password='x')
        PerfilUsuario.objects.create(usuario=self.usuario, acceso_todas_unidades=True)
        self.usuario.user_permissions.add(
            Permission.objects.get(content_type__app_label='monitoreo', codename='view_alerta'),
        )
        self.client.force_login(self.usuario)

        self.regla_warning = ReglaAlerta.objects.create(
            nombre='CPU alta', metrica=Metrica.CPU_CARGA_PCT, umbral=90,
            severidad=ReglaAlerta.Severidad.WARNING, creado_por=self.usuario,
        )
        self.regla_critical = ReglaAlerta.objects.create(
            nombre='Disco lleno', metrica=Metrica.DISCO_USADO_PCT, umbral=95,
            severidad=ReglaAlerta.Severidad.CRITICAL, creado_por=self.usuario,
        )

    def _sembrar(self, semanas_atras, cuantas=1):
        """Alertas y métricas repartidas en varias semanas, con cantidades DISTINTAS por
        día: si todos los días tuvieran las mismas muestras, promediar promedios diarios
        daría igual que el promedio semanal y la prueba de equivalencia no probaría nada.
        """
        ahora = timezone.now()
        for semana in range(semanas_atras):
            momento = ahora - timedelta(weeks=semana, days=1)
            for i in range(cuantas + semana):
                # Una regla por alerta (misma métrica y severidad que las dos de setUp): la
                # base ya no admite dos alertas activas de la misma regla en la misma
                # estación (CR-2), y lo que se mide acá es la severidad, no la regla.
                base = self.regla_warning if i % 2 else self.regla_critical
                regla = ReglaAlerta.objects.create(
                    nombre=f'{base.nombre} {semana}-{i}', metrica=base.metrica, umbral=base.umbral,
                    severidad=base.severidad, creado_por=self.usuario,
                )
                alerta = Alerta.objects.create(regla=regla, estacion=self.estacion, valor_disparador=95)
                Alerta.objects.filter(pk=alerta.pk).update(
                    abierta_en=momento, resuelta_en=momento if i % 3 == 0 else None,
                )
                MuestraMetrica.objects.create(
                    estacion=self.estacion, cpu_carga_pct=50 + i, ram_total=8000,
                    ram_usada=4000 + i * 100, disco_total_gb=500, disco_libre_gb=200 + i,
                    red_recibido_kbps=1000 + i, red_enviado_kbps=200 + i,
                    timestamp=momento,
                )

    def _series(self):
        from apps.panel.views.tendencia import _semanas_recientes, _series_semanales

        return _series_semanales(
            _semanas_recientes(12),
            Alerta.objects.all(),
            MuestraMetrica.objects.filter(estacion=self.estacion),
        )

    def _consultas(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        with CaptureQueriesContext(connection) as ctx:
            self.client.get(reverse('panel:tendencia_flota'))
        return len(ctx.captured_queries)

    def test_las_consultas_no_crecen_con_los_datos(self):
        """Lo que se fija no es el número exacto —depende de la sesión, los permisos y el
        scoping— sino que no dependa de cuántas semanas tengan datos."""
        self._sembrar(semanas_atras=2, cuantas=2)
        con_dos_semanas = self._consultas()

        self._sembrar(semanas_atras=10, cuantas=3)
        self.assertEqual(self._consultas(), con_dos_semanas)

    def test_los_valores_son_identicos_a_los_del_calculo_semana_por_semana(self):
        """Compara contra una reimplementación del bucle viejo: tres `.count()` y un
        `.aggregate()` por semana. Es la garantía de que la optimización no movió ningún
        número."""
        from django.db.models import Avg

        from apps.panel.views.tendencia import _semanas_recientes

        self._sembrar(semanas_atras=6, cuantas=2)
        series = self._series()

        semanas = _semanas_recientes(12)
        alertas = Alerta.objects.all()
        metricas = MuestraMetrica.objects.filter(estacion=self.estacion)

        for i, (inicio, fin) in enumerate(semanas):
            de_la_semana = alertas.filter(abierta_en__date__gte=inicio, abierta_en__date__lt=fin)
            self.assertEqual(
                series['abiertas_warning'][i],
                de_la_semana.filter(regla__severidad=ReglaAlerta.Severidad.WARNING).count(),
                'semana %d: abiertas warning' % i,
            )
            self.assertEqual(
                series['abiertas_critical'][i],
                de_la_semana.filter(regla__severidad=ReglaAlerta.Severidad.CRITICAL).count(),
                'semana %d: abiertas critical' % i,
            )
            self.assertEqual(
                series['resueltas'][i],
                alertas.filter(resuelta_en__date__gte=inicio, resuelta_en__date__lt=fin).count(),
                'semana %d: resueltas' % i,
            )

            agregado = metricas.filter(timestamp__date__gte=inicio, timestamp__date__lt=fin).aggregate(
                cpu=Avg('cpu_carga_pct'), ram_total=Avg('ram_total'), ram_usada=Avg('ram_usada'),
                disco_total=Avg('disco_total_gb'), disco_libre=Avg('disco_libre_gb'),
                red_recibido=Avg('red_recibido_kbps'), red_enviado=Avg('red_enviado_kbps'),
            )
            self._igual(series['cpu'][i], agregado['cpu'], 'semana %d: cpu' % i)
            self._igual(
                series['ram'][i],
                100 * agregado['ram_usada'] / agregado['ram_total'] if agregado['ram_total'] else None,
                'semana %d: ram' % i,
            )
            self._igual(
                series['disco'][i],
                100 * (agregado['disco_total'] - agregado['disco_libre']) / agregado['disco_total']
                if agregado['disco_total'] else None,
                'semana %d: disco' % i,
            )

    def _igual(self, obtenido, esperado, mensaje):
        if esperado is None:
            self.assertIsNone(obtenido, mensaje)
        else:
            self.assertAlmostEqual(obtenido, esperado, places=6, msg=mensaje)

    def test_una_semana_sin_datos_sigue_dando_none_y_no_cero(self):
        """Un hueco en la serie no es "0% de CPU": es "no hubo muestras". Confundirlos
        dibujaría una caída a cero que nunca pasó."""
        self.assertTrue(all(v is None for v in self._series()['cpu']))


class AuditoriaPaginadaTests(TestCase):
    """El listado de auditoría cortaba con `[:200]`.

    No era una optimización sino una pérdida: al llegar a 257 eventos en producción
    (14-sep-2026) había 57 que existían y no se podían ver desde ninguna parte de la
    aplicación, sin aviso. Un registro de auditoría que esconde filas en silencio deja de
    servir como registro, y es la única tabla que solo crece.
    """

    def setUp(self):
        self.usuario = User.objects.create_user(username='u_audit_pag', password='x')
        PerfilUsuario.objects.create(usuario=self.usuario, acceso_todas_unidades=True)
        self.usuario.user_permissions.add(
            Permission.objects.get(content_type__app_label='auditoria', codename='view_eventoauditoria'),
        )
        self.client.force_login(self.usuario)

    def _sembrar(self, cuantos):
        EventoAuditoria.objects.bulk_create([
            EventoAuditoria(usuario=self.usuario, accion='prueba.%d' % i, objeto_repr='obj %d' % i)
            for i in range(cuantos)
        ])
        # Marcas de tiempo DISTINTAS: `timestamp` es auto_now_add y el `ordering` del
        # modelo no desempata por `pk`, así que sembrar todo en el mismo instante deja el
        # orden indefinido y la paginación podría repetir o saltear filas entre páginas
        # (es lo que advierte el docstring de `paginar`). Con el slice viejo esto no se
        # notaba porque nunca había una segunda página.
        ahora = timezone.now()
        for i, evento in enumerate(EventoAuditoria.objects.order_by('pk')):
            EventoAuditoria.objects.filter(pk=evento.pk).update(
                timestamp=ahora - timedelta(seconds=i),
            )

    def test_el_evento_numero_201_se_puede_alcanzar(self):
        """La prueba que justifica el cambio: con el slice viejo esta fila era invisible
        para siempre."""
        self._sembrar(210)
        total = EventoAuditoria.objects.count()
        vistos = 0
        pagina = 1
        while True:
            resp = self.client.get(reverse('panel:auditoria_lista'), {'pagina': pagina})
            self.assertEqual(resp.status_code, 200)
            vistos += len(resp.context['eventos'])
            if not resp.context['pagina'].has_next():
                break
            pagina += 1
            self.assertLess(pagina, 50, 'el paginador no termina')
        self.assertEqual(vistos, total)

    def test_una_pagina_no_trae_la_tabla_entera(self):
        self._sembrar(60)
        resp = self.client.get(reverse('panel:auditoria_lista'))
        self.assertLessEqual(len(resp.context['eventos']), 25)
        self.assertEqual(resp.context['pagina'].paginator.count, EventoAuditoria.objects.count())

    def test_el_mas_reciente_sigue_primero(self):
        """El orden importa más que en otros listados: un registro de auditoría se lee
        de lo último hacia atrás."""
        self._sembrar(30)
        reciente = EventoAuditoria.objects.create(usuario=self.usuario, accion='ultima.accion')
        resp = self.client.get(reverse('panel:auditoria_lista'))
        self.assertEqual(resp.context['eventos'][0].pk, reciente.pk)

    def test_dos_eventos_del_mismo_instante_no_se_pisan_entre_paginas(self):
        """`timestamp` es auto_now_add y dos eventos del mismo tick dejaban el orden
        indefinido: al paginar, una fila podía aparecer dos veces y otra ninguna. Con el
        `[:200]` que tenía la vista nunca había segunda página y no se notaba.

        Se siembra TODO en el mismo instante a propósito — es el caso que el desempate
        por `pk` viene a resolver."""
        from django.utils import timezone as tz

        EventoAuditoria.objects.bulk_create([
            EventoAuditoria(usuario=self.usuario, accion='mismo.tick.%d' % i) for i in range(60)
        ])
        instante = tz.now()
        EventoAuditoria.objects.update(timestamp=instante)

        vistos, pagina = [], 1
        while True:
            resp = self.client.get(reverse('panel:auditoria_lista'), {'pagina': pagina})
            vistos += [e.pk for e in resp.context['eventos']]
            if not resp.context['pagina'].has_next():
                break
            pagina += 1
        self.assertEqual(len(vistos), len(set(vistos)), 'hay filas repetidas entre páginas')
        self.assertEqual(len(vistos), EventoAuditoria.objects.count(), 'se perdieron filas')


class ActivosPaginadosTests(TestCase):
    """`activos_lista` se pagina con 18 filas en producción y no con 1.800.

    El parque son ~1.800 estaciones más sus periféricos. Paginar una lista corta es
    barato; hacerlo sobre una tabla ya grande, no — es el mismo criterio con el que se
    paginó estaciones_lista antes del rollout del agente.
    """

    def setUp(self):
        self.sg = UnidadNegocio.objects.get(codigo='SG')
        self.usuario = User.objects.create_user(username='u_act_pag', password='x')
        PerfilUsuario.objects.create(usuario=self.usuario, acceso_todas_unidades=True)
        self.usuario.user_permissions.add(
            Permission.objects.get(content_type__app_label='activos', codename='view_activo'),
        )
        self.client.force_login(self.usuario)
        Activo.objects.bulk_create([
            Activo(codigo='CR-DSK-%04d' % i, tipo=Activo.Tipo.DESKTOP, unidad_negocio=self.sg)
            for i in range(60)
        ])

    def test_una_pagina_no_trae_todo(self):
        resp = self.client.get(reverse('panel:activos_lista'))
        self.assertLessEqual(len(resp.context['activos']), 25)
        self.assertEqual(resp.context['pagina'].paginator.count, 60)

    def test_el_filtro_sobrevive_al_cambio_de_pagina(self):
        """`query_filtros` lleva los parámetros SIN `pagina`: sin eso, pasar de página
        descarta el filtro y el operador vuelve al listado completo."""
        resp = self.client.get(reverse('panel:activos_lista'), {'tipo': 'DSK', 'pagina': 2})
        self.assertIn('tipo=DSK', resp.context['query_filtros'])
        self.assertNotIn('pagina', resp.context['query_filtros'])

    def test_el_conteo_respeta_el_filtro(self):
        Activo.objects.create(codigo='CR-LAP-9001', tipo=Activo.Tipo.LAPTOP, unidad_negocio=self.sg)
        resp = self.client.get(reverse('panel:activos_lista'), {'tipo': 'LAP'})
        self.assertEqual(resp.context['pagina'].paginator.count, 1)


class EscalaDelGraficoDeConsumoTests(TestCase):
    """El gráfico de consumo se escala al ancho CONTRATADO, no al pico de la ventana.

    Con autoescala, un pico de 3.481 kbps llenaba todo el alto y se leía como saturación
    — sobre un enlace de 10 Mbps es un 35%. Reportado mirando la pantalla real el
    15-sep-2026: "no me indica cuál es el rango que está midiendo".

    Es el mismo problema que el error de unidades del mismo día: el número era correcto y
    la presentación llevaba a la conclusión opuesta.
    """

    def setUp(self):
        self.sg = UnidadNegocio.objects.get(codigo='SG')
        grupo = Grupo.objects.create(codigo='TRX001')
        self.farmacia = Farmacia.objects.create(
            codigo='ML001', grupo=grupo, unidad_negocio=self.sg, ip_router='10.1.1.1',
        )
        self.usuario = User.objects.create_user(username='u_escala', password='x')
        PerfilUsuario.objects.create(usuario=self.usuario, acceso_todas_unidades=True)
        self.usuario.user_permissions.add(
            Permission.objects.get(
                content_type__app_label='monitoreo', codename='view_estadoenlacefarmacia',
            ),
        )
        self.client.force_login(self.usuario)

    def _muestras(self, *valores):
        from apps.monitoreo.models import MuestraRedFarmacia

        for valor in valores:
            MuestraRedFarmacia.objects.create(
                farmacia=self.farmacia, bytes_recibidos=0, bytes_enviados=0,
                red_recibido_kbps=valor, red_enviado_kbps=0,
            )

    def _modal(self):
        return self.client.get(reverse('panel:enlace_farmacia_modal', args=[self.farmacia.pk]))

    def test_el_eje_es_el_ancho_contratado(self):
        self.farmacia.ancho_contratado_mbps = 10
        self.farmacia.save(update_fields=['ancho_contratado_mbps'])
        self._muestras(600, 3481, 700)

        resp = self._modal()
        self.assertEqual(resp.context['escala_kbps'], 10000)
        self.assertEqual(resp.context['g_red'].max_valor, 10000)

    def test_el_pico_real_se_informa_aparte_del_eje(self):
        """El tope del eje y el pico de la serie son cosas distintas: sin el segundo no se
        sabe cuánto llegó a usarse de verdad."""
        self.farmacia.ancho_contratado_mbps = 10
        self.farmacia.save(update_fields=['ancho_contratado_mbps'])
        self._muestras(600, 3481, 700)

        resp = self._modal()
        self.assertEqual(resp.context['pico_kbps'], 3481)
        self.assertContains(resp, '10 Mbps contratados')

    def test_sin_contratado_vuelve_a_la_autoescala_y_lo_dice(self):
        """Un gráfico sin eje declarado sirve para ver la forma, no para juzgar el uso —
        y la pantalla tiene que decirlo en vez de dejar que se lea mal."""
        self._muestras(600, 3481, 700)

        resp = self._modal()
        self.assertIsNone(resp.context['escala_kbps'])
        self.assertEqual(resp.context['g_red'].max_valor, 3481)
        self.assertContains(resp, 'Escala automática')

    def test_avisa_cuando_el_pico_supera_lo_contratado(self):
        """El gráfico recorta arriba, así que si no se dice con un número el exceso queda
        invisible."""
        self.farmacia.ancho_contratado_mbps = 2
        self.farmacia.save(update_fields=['ancho_contratado_mbps'])
        self._muestras(500, 3481)

        resp = self._modal()
        self.assertContains(resp, 'superó lo contratado')

    def test_sin_muestras_no_rompe(self):
        resp = self._modal()
        self.assertEqual(resp.status_code, 200)
        self.assertIsNone(resp.context['pico_kbps'])


class DivisionDeVistasTests(TestCase):
    """Que la división de `views/activos.py` y `views/monitoreo.py` no se deshaga sola.

    Los dos archivos habían llegado a 852 y 673 líneas, el segundo mezclando tres
    dominios. Se dividieron en siete. Sin una prueba, vuelven a crecer: cada vista nueva
    se agrega al archivo que ya existe porque es el camino de menor resistencia.
    """

    LIMITE_LINEAS = 550

    def _modulos(self):
        from pathlib import Path

        from django.conf import settings

        return sorted((Path(settings.BASE_DIR) / 'apps' / 'panel' / 'views').glob('*.py'))

    def test_ningun_modulo_de_vistas_pasa_el_limite(self):
        """550 líneas no es un número sagrado: es "más grande que esto y cuesta entrar".
        Si un módulo lo pasa, la respuesta es dividirlo, no subir el límite."""
        grandes = [
            '%s (%d líneas)' % (m.name, len(m.read_text(encoding='utf-8').splitlines()))
            for m in self._modulos()
            if len(m.read_text(encoding='utf-8').splitlines()) > self.LIMITE_LINEAS
        ]
        self.assertEqual(grandes, [], 'módulos de vistas demasiado grandes: %s' % ', '.join(grandes))

    def test_los_umbrales_de_color_viven_en_un_solo_lugar(self):
        """Al dividir, las cinco constantes de umbral quedaron copiadas en los tres
        archivos nuevos. Un umbral escrito tres veces se desincroniza, y el síntoma es
        peor que un error: la misma estación se ve amarilla en una pantalla y verde en
        otra, sin que nada falle."""
        import re

        patron = re.compile(r'^(RED_FARMACIA_UMBRAL|UMBRAL_(CPU|RAM|DISCO))', re.MULTILINE)
        con_definicion = [
            m.name for m in self._modulos() if patron.search(m.read_text(encoding='utf-8'))
        ]
        self.assertEqual(
            con_definicion, [],
            'los umbrales tienen que importarse de apps/panel/umbrales.py, no redefinirse: %s'
            % ', '.join(con_definicion),
        )

    def test_las_vistas_publicas_se_siguen_exportando_igual(self):
        """`urls.py` hace `views.nombre_funcion`: si la división se llevara un nombre,
        la URL rompe recién al visitarla, no al importar."""
        from apps.panel import views

        for nombre in (
            'activos_lista', 'bodegas_lista', 'ordenes_compra_lista', 'colaboradores_lista',
            'monitoreo_lista', 'tendencia_flota', 'enlaces_farmacias_lista',
        ):
            self.assertTrue(hasattr(views, nombre), 'falta %s en apps.panel.views' % nombre)


class ServiciosPosEnElPanelTests(TestCase):
    """La tarjeta de servicios del POS en la ficha de la estacion y el indicador de flota.

    La ficha ya se refresca sola por HTMX cada 10s, asi que la tarjeta se actualiza sin
    codigo nuevo: alcanza con que el partial la incluya.
    """

    def setUp(self):
        self.sg = UnidadNegocio.objects.get(codigo='SG')
        grupo = Grupo.objects.create(codigo='TRX001')
        self.farmacia = Farmacia.objects.create(codigo='ML001', grupo=grupo, unidad_negocio=self.sg)
        self.estacion = Estacion.objects.create(
            codigo='ML001-A', farmacia=self.farmacia, monitorear_recursos=True,
            estado_aprobacion=Estacion.EstadoAprobacion.APROBADA,
        )
        self.usuario = User.objects.create_user(username='u_panel_svc', password='x')
        PerfilUsuario.objects.create(usuario=self.usuario, acceso_todas_unidades=True)
        self.usuario.user_permissions.add(
            Permission.objects.get(
                content_type__app_label='monitoreo', codename='view_muestrametrica',
            ),
        )
        self.client.force_login(self.usuario)

    def _estado(self, servicio='pg_local', disponible=True, critico=True, **extra):
        from apps.monitoreo.models import EstadoServicioPos

        campos = {
            'disponible': disponible, 'critico': critico,
            'latencia_ms': 42 if disponible else None,
            'mensaje': 'PostgreSQL 160006' if disponible else 'connection refused',
            'endpoint': '10.0.0.5:5432/pos',
            'ultima_verificacion': timezone.now(),
            'ultima_respuesta': timezone.now() if disponible else None,
        }
        campos.update(extra)
        return EstadoServicioPos.objects.create(
            estacion=self.estacion, servicio=servicio, **campos,
        )

    def _ficha(self):
        from django.urls import reverse

        return self.client.get(
            reverse('panel:monitoreo_detalle_partial', args=[self.estacion.pk]),
        )

    def test_la_ficha_muestra_el_servicio_que_responde(self):
        self._estado('pg_local')
        resp = self._ficha()
        self.assertContains(resp, 'PostgreSQL local')
        self.assertContains(resp, '42 ms')
        self.assertContains(resp, '10.0.0.5:5432/pos')

    def test_la_ficha_muestra_el_servicio_caido_con_su_mensaje(self):
        """El mensaje crudo ES el diagnostico: distingue "no hay ruta" de "clave
        rechazada", que llevan a lugares distintos."""
        self._estado('pg_central', disponible=False, critico=False)
        resp = self._ficha()
        self.assertContains(resp, 'PostgreSQL central')
        self.assertContains(resp, 'connection refused')

    def test_un_chequeo_viejo_se_muestra_como_sin_verificar_y_no_como_caido(self):
        """Si la estacion se apago, nadie esta midiendo. Pintarlo de rojo mandaria a
        alguien a revisar algo que nadie comprobo."""
        from apps.monitoreo.models import EstadoServicioPos

        estado = self._estado('pg_local', disponible=False)
        EstadoServicioPos.objects.filter(pk=estado.pk).update(
            ultima_verificacion=timezone.now() - timedelta(hours=5),
        )
        resp = self._ficha()
        self.assertContains(resp, 'sin verificar')

    def test_una_estacion_sin_muestras_de_recursos_igual_muestra_sus_servicios(self):
        """Son dos chequeos con su propio intervalo: la tarjeta va fuera del bloque que
        depende de que exista una MuestraMetrica."""
        self._estado('odoo', critico=False)
        resp = self._ficha()
        self.assertContains(resp, 'Odoo')

    def test_la_ficha_no_filtra_credenciales(self):
        self._estado('pg_local', endpoint='10.0.0.5:5432/pos')
        resp = self._ficha()
        self.assertNotContains(resp, 'Contrasena')
        self.assertNotContains(resp, 'password')

    def test_el_listado_de_flota_avisa_cuantos_servicios_estan_caidos(self):
        from django.urls import reverse

        self._estado('pg_local', disponible=False)
        self._estado('odoo', disponible=False, critico=False)
        resp = self.client.get(reverse('panel:monitoreo_lista'))
        self.assertContains(resp, '2 sin responder')

    def test_el_listado_ignora_los_chequeos_viejos(self):
        """Una estacion apagada no genera un rojo en la vista de flota."""
        from django.urls import reverse

        from apps.monitoreo.models import EstadoServicioPos

        estado = self._estado('pg_local', disponible=False)
        EstadoServicioPos.objects.filter(pk=estado.pk).update(
            ultima_verificacion=timezone.now() - timedelta(hours=5),
        )
        resp = self.client.get(reverse('panel:monitoreo_lista'))
        self.assertNotContains(resp, 'sin responder')

    def test_el_listado_no_consulta_una_vez_por_estacion(self):
        """N+1: la vista de flota muestra todas las estaciones monitoreadas, y una
        consulta por tarjeta es el mismo problema que la auditoria ya saco de esta
        pantalla."""
        from django.db import connection
        from django.test.utils import CaptureQueriesContext
        from django.urls import reverse

        self._estado('pg_local', disponible=False)
        with CaptureQueriesContext(connection) as ctx:
            self.client.get(reverse('panel:monitoreo_lista'))
        con_una = len(ctx.captured_queries)

        for i in range(5):
            otra = Estacion.objects.create(
                codigo='ML001-X%d' % i, farmacia=self.farmacia, monitorear_recursos=True,
                estado_aprobacion=Estacion.EstadoAprobacion.APROBADA,
            )
            from apps.monitoreo.models import EstadoServicioPos
            EstadoServicioPos.objects.create(
                estacion=otra, servicio='pg_local', disponible=False, critico=True,
                ultima_verificacion=timezone.now(),
            )
        with CaptureQueriesContext(connection) as ctx:
            self.client.get(reverse('panel:monitoreo_lista'))
        self.assertEqual(len(ctx.captured_queries), con_una)


class ServiciosPosEnLaFichaDeEstacionTests(TestCase):
    """Los servicios del POS en /estaciones/, que es la ficha que cubre TODAS.

    La primera tarjeta se puso en /monitoreo/<pk>/, que exige `monitorear_recursos`: en
    produccion lo tienen 2 de 10 estaciones, y ninguna de ellas es la que reporta
    servicios. El dato se recolectaba y no se veia — el mismo patron que la auditoria
    marco como deuda.
    """

    def setUp(self):
        sg = UnidadNegocio.objects.get(codigo='SG')
        grupo = Grupo.objects.create(codigo='TRX001')
        farmacia = Farmacia.objects.create(codigo='ML001', grupo=grupo, unidad_negocio=sg)
        # Sin monitorear_recursos a proposito: es el caso que no se veia.
        self.estacion = Estacion.objects.create(
            codigo='ML001-A', farmacia=farmacia, monitorear_recursos=False,
            estado_aprobacion=Estacion.EstadoAprobacion.APROBADA,
        )
        self.usuario = User.objects.create_user(username='u_ficha_svc', password='x')
        PerfilUsuario.objects.create(usuario=self.usuario, acceso_todas_unidades=True)
        self.usuario.user_permissions.add(
            Permission.objects.get(
                content_type__app_label='catalogo', codename='view_estacion',
            ),
        )
        self.client.force_login(self.usuario)

    def _estado(self, servicio, disponible, critico):
        from apps.monitoreo.models import EstadoServicioPos

        return EstadoServicioPos.objects.create(
            estacion=self.estacion, servicio=servicio, disponible=disponible, critico=critico,
            latencia_ms=42 if disponible else None,
            mensaje='PostgreSQL 160014' if disponible else 'urlopen error timed out',
            endpoint='192.168.111.6:5433/hub_111_6',
            ultima_verificacion=timezone.now(),
            ultima_respuesta=timezone.now() if disponible else None,
        )

    def _ficha(self):
        from django.urls import reverse

        return self.client.get(reverse('panel:estacion_info_modal', args=[self.estacion.pk]))

    def test_una_estacion_sin_monitorear_recursos_igual_muestra_sus_servicios(self):
        """El caso real: ML017-B reporta servicios y no tiene el flag."""
        self._estado('pg_local', True, True)
        resp = self._ficha()
        self.assertContains(resp, 'PostgreSQL local')
        self.assertContains(resp, '42 ms')

    def test_muestra_el_servicio_caido_con_su_mensaje(self):
        self._estado('odoo', False, False)
        resp = self._ficha()
        self.assertContains(resp, 'Odoo')
        self.assertContains(resp, 'urlopen error timed out')

    def test_distingue_critico_de_no_critico(self):
        self._estado('pg_local', False, True)
        self._estado('odoo', False, False)
        resp = self._ficha()
        self.assertContains(resp, 'crítico')

    def test_sin_servicios_reportados_no_aparece_la_seccion(self):
        """Una estacion con un agente viejo no muestra una seccion vacia."""
        resp = self._ficha()
        self.assertNotContains(resp, 'Servicios que consume el POS')

    def test_la_ficha_no_filtra_credenciales(self):
        self._estado('pg_local', True, True)
        resp = self._ficha()
        self.assertNotContains(resp, 'Contrasena')
        self.assertNotContains(resp, 'password')


class EnlacesKpiFiltrablesTests(TestCase):
    """Las tarjetas de KPI tienen que llevar al listado de lo que cuentan.

    Pedido del usuario (17-sep-2026), con sus palabras: "me dice que tengo 11 enlaces
    caidos pero no se cuales son". El numero estaba a la vista y para llegar al detalle
    habia que ir al select de abajo y repetir a mano lo que la tarjeta ya sabia.
    """

    def setUp(self):
        from apps.monitoreo.enlaces import registrar_sondeo
        from apps.monitoreo.models import EstadoEnlaceFarmacia

        self.sg = UnidadNegocio.objects.get(codigo='SG')
        grupo = Grupo.objects.create(codigo='TRX903')
        self.viva = Farmacia.objects.create(
            codigo='TSTK01', grupo=grupo, unidad_negocio=self.sg, ip_router='10.0.1.1')
        self.caida = Farmacia.objects.create(
            codigo='TSTK02', grupo=grupo, unidad_negocio=self.sg, ip_router='10.0.1.2')
        self.nunca = Farmacia.objects.create(
            codigo='TSTK03', grupo=grupo, unidad_negocio=self.sg, ip_router='10.0.1.3')

        registrar_sondeo(self.viva, True, 10.0)
        registrar_sondeo(self.caida, True, 11.0)
        for _ in range(EstadoEnlaceFarmacia.UMBRAL_FALLAS_CONSECUTIVAS):
            registrar_sondeo(self.caida, False, None)
            registrar_sondeo(self.nunca, False, None)

        self.usuario = User.objects.create_user(username='kpi_enlaces', password='x')
        PerfilUsuario.objects.create(usuario=self.usuario, acceso_todas_unidades=True)
        self.usuario.user_permissions.add(
            Permission.objects.get(content_type__app_label='monitoreo',
                                   codename='view_estadoenlacefarmacia'),
        )
        self.client.force_login(self.usuario)

    def test_la_tarjeta_de_caidos_enlaza_al_filtro_de_caidos(self):
        respuesta = self.client.get(reverse('panel:enlaces_farmacias_lista'))
        self.assertContains(respuesta, 'href="?estado=caidos"')

    def test_el_filtro_de_caidos_muestra_solo_la_caida_real(self):
        url = reverse('panel:enlaces_farmacias_lista')
        respuesta = self.client.get(url, {'estado': 'caidos'})
        codigos = [f['farmacia'].codigo for f in respuesta.context['filas']]
        self.assertEqual(codigos, ['TSTK02'])

    def test_el_filtro_de_nunca_respondieron_muestra_solo_esa(self):
        url = reverse('panel:enlaces_farmacias_lista')
        respuesta = self.client.get(url, {'estado': 'nunca'})
        codigos = [f['farmacia'].codigo for f in respuesta.context['filas']]
        self.assertEqual(codigos, ['TSTK03'])

    def test_la_tarjeta_activa_ofrece_quitar_el_filtro(self):
        """Pulsarla de nuevo vuelve al total, en vez de dejar al operador atrapado."""
        url = reverse('panel:enlaces_farmacias_lista')
        respuesta = self.client.get(url, {'estado': 'caidos'})
        self.assertContains(respuesta, 'quitar filtro')

    def test_el_numero_de_la_tarjeta_coincide_con_lo_que_lista_su_filtro(self):
        """Si el KPI dice 1 y el filtro muestra 3, la tarjeta miente."""
        url = reverse('panel:enlaces_farmacias_lista')
        sin_filtro = self.client.get(url)
        for clave, filtro in (('caidas', 'caidos'), ('nunca_respondieron', 'nunca'),
                              ('activas', 'activos')):
            filtrado = self.client.get(url, {'estado': filtro})
            self.assertEqual(
                sin_filtro.context[clave], len(filtrado.context['filas']),
                f'el KPI "{clave}" no coincide con lo que lista ?estado={filtro}',
            )


class ActividadPlanificadaPorUnidadTests(TestCase):
    """La agenda de un cliente no se ve ni se cierra desde otro.

    El modelo no tenia unidad_negocio, asi que la lista mostraba las de TODOS los
    clientes y completar aceptaba cualquier id. Hoy no hay fuga real (0 actividades y
    los 3 usuarios con el permiso tienen acceso global), pero se arma sola el dia que el
    modulo se use con un usuario acotado.
    """

    def setUp(self):
        from apps.cuentas.models import PerfilUsuario
        from apps.mantenimiento.models import ActividadPlanificada

        self.sg = UnidadNegocio.objects.get(codigo='SG')
        self.mia = UnidadNegocio.objects.get(codigo='MIA')
        self.tecnico = User.objects.create_user(username='tec_act', password='x')

        self.de_sg = ActividadPlanificada.objects.create(
            tecnico=self.tecnico, creado_por=self.tecnico, titulo='Trabajo de SG',
            tipo_actividad='revision', fecha_inicio=date(2026, 9, 18), fecha_fin=date(2026, 9, 19),
            unidad_negocio=self.sg,
        )
        self.de_mia = ActividadPlanificada.objects.create(
            tecnico=self.tecnico, creado_por=self.tecnico, titulo='Trabajo de MIA',
            tipo_actividad='revision', fecha_inicio=date(2026, 9, 18), fecha_fin=date(2026, 9, 19),
            unidad_negocio=self.mia,
        )
        self.interna = ActividadPlanificada.objects.create(
            tecnico=self.tecnico, creado_por=self.tecnico, titulo='Trabajo interno',
            tipo_actividad='revision', fecha_inicio=date(2026, 9, 18), fecha_fin=date(2026, 9, 19),
        )

        self.usuario_sg = User.objects.create_user(username='solo_sg', password='x')
        perfil = PerfilUsuario.objects.create(usuario=self.usuario_sg, acceso_todas_unidades=False)
        perfil.unidades_negocio.add(self.sg)
        for codename in ('view_actividadplanificada', 'change_actividadplanificada'):
            self.usuario_sg.user_permissions.add(
                Permission.objects.get(content_type__app_label='mantenimiento', codename=codename),
            )

    def test_un_usuario_de_sg_no_ve_la_actividad_de_mia(self):
        self.client.force_login(self.usuario_sg)
        respuesta = self.client.get(reverse('panel:actividades_planificadas_lista'))
        titulos = [a.titulo for a in respuesta.context['actividades']]
        self.assertIn('Trabajo de SG', titulos)
        self.assertNotIn('Trabajo de MIA', titulos)

    def test_las_actividades_internas_las_ve_todo_el_mundo(self):
        """Sin unidad = interna. El scope estricto las habria ocultado a todos."""
        self.client.force_login(self.usuario_sg)
        respuesta = self.client.get(reverse('panel:actividades_planificadas_lista'))
        titulos = [a.titulo for a in respuesta.context['actividades']]
        self.assertIn('Trabajo interno', titulos)

    def test_no_se_puede_completar_la_actividad_de_otra_unidad(self):
        """Lo que antes alcanzaba con cambiar el id en la URL."""
        self.client.force_login(self.usuario_sg)
        respuesta = self.client.post(
            reverse('panel:actividad_planificada_completar', args=[self.de_mia.pk]),
            {'tiempo_real_minutos': 30},
        )
        self.assertIn(respuesta.status_code, (403, 404))
        self.de_mia.refresh_from_db()
        self.assertNotEqual(self.de_mia.estado, self.de_mia.Estado.COMPLETADA)

    def test_si_se_puede_completar_la_propia(self):
        self.client.force_login(self.usuario_sg)
        self.client.post(
            reverse('panel:actividad_planificada_completar', args=[self.de_sg.pk]),
            {'tiempo_real_minutos': 30},
        )
        self.de_sg.refresh_from_db()
        self.assertEqual(self.de_sg.estado, self.de_sg.Estado.COMPLETADA)

    def test_el_formulario_no_ofrece_clientes_ajenos(self):
        from apps.mantenimiento.forms import ActividadPlanificadaForm

        form = ActividadPlanificadaForm(user=self.usuario_sg)
        codigos = set(form.fields['unidad_negocio'].queryset.values_list('codigo', flat=True))
        self.assertIn('SG', codigos)
        self.assertNotIn('MIA', codigos)

class CentroMonitoreoTests(TestCase):
    """La pantalla de triage que la mesa de ayuda deja abierta todo el turno.

    Solo lectura y consolidada: no reemplaza las pantallas de detalle, las enlaza.
    """

    def setUp(self):
        from apps.cuentas.models import PerfilUsuario
        from apps.monitoreo.models import Alerta, EstadoServicioPos, Metrica, ReglaAlerta

        self.sg = UnidadNegocio.objects.get(codigo='SG')
        self.mia = UnidadNegocio.objects.get(codigo='MIA')

        grupo = Grupo.objects.create(codigo='TRX940')
        self.farmacia_sg = Farmacia.objects.create(
            codigo='ML940', grupo=grupo, unidad_negocio=self.sg, ip_router='10.1.1.254',
            # El proveedor va en `tipo_enlace`; el circuito es el identificador del
            # ticket y tiene forma `cliente-sitio-ciudad`. Esta fixture decia
            # `circuito_proveedor='telconet-ml940'`, que es justo la confusion que el
            # agrupado arrastraba: el primer segmento del circuito es la CADENA.
            tipo_enlace='TELCONET',
            circuito_proveedor='sangregorio-ml940-portoviejo',
        )
        self.farmacia_mia = Farmacia.objects.create(
            codigo='MM940', grupo=grupo, unidad_negocio=self.mia, ip_router='10.2.2.254',
        )
        self.estacion_sg = Estacion.objects.create(
            codigo='ML940-A', farmacia=self.farmacia_sg,
            estado_aprobacion=Estacion.EstadoAprobacion.APROBADA,
        )
        self.estacion_mia = Estacion.objects.create(
            codigo='MM940-A', farmacia=self.farmacia_mia,
            estado_aprobacion=Estacion.EstadoAprobacion.APROBADA,
        )

        autor = User.objects.create_user(username='autor_centro', password='x')
        self.regla_critica = ReglaAlerta.objects.create(
            nombre='Disco lleno', metrica=Metrica.DISCO_USADO_PCT, umbral=95,
            duracion_minutos=0, severidad=ReglaAlerta.Severidad.CRITICAL, creado_por=autor,
        )
        self.regla_warning = ReglaAlerta.objects.create(
            nombre='CPU saturada', metrica=Metrica.CPU_CARGA_PCT, umbral=90,
            duracion_minutos=0, severidad=ReglaAlerta.Severidad.WARNING, creado_por=autor,
        )
        Alerta.objects.create(regla=self.regla_critica, estacion=self.estacion_sg, valor_disparador=99)
        Alerta.objects.create(regla=self.regla_warning, estacion=self.estacion_sg, valor_disparador=95)
        Alerta.objects.create(regla=self.regla_critica, estacion=self.estacion_mia, valor_disparador=99)

        # Mesa de ayuda: el rol real, con el permiso que el seed le da.
        self.mesa = User.objects.create_user(username='mesa', password='x')
        self.mesa.user_permissions.add(
            Permission.objects.get(content_type__app_label='monitoreo', codename='view_alerta'),
        )
        PerfilUsuario.objects.create(usuario=self.mesa, acceso_todas_unidades=True)

        # Alguien autenticado pero SIN el rol.
        self.curioso = User.objects.create_user(username='curioso', password='x')
        PerfilUsuario.objects.create(usuario=self.curioso, acceso_todas_unidades=True)

    def _datos(self, usuario=None):
        self.client.force_login(usuario or self.mesa)
        return self.client.get(reverse('panel:centro_monitoreo_partial'))

    # --- permisos ---

    def test_la_mesa_de_ayuda_entra(self):
        self.client.force_login(self.mesa)
        self.assertEqual(self.client.get(reverse('panel:centro_monitoreo')).status_code, 200)

    def test_un_autenticado_sin_el_rol_NO_entra(self):
        """El resto del panel no está abierto a cualquier usuario logueado y esta
        pantalla tampoco: muestra el mapa completo de qué está caído y dónde."""
        self.client.force_login(self.curioso)
        self.assertEqual(self.client.get(reverse('panel:centro_monitoreo')).status_code, 403)

    def test_sin_loguearse_redirige_al_login(self):
        resp = self.client.get(reverse('panel:centro_monitoreo'))
        self.assertEqual(resp.status_code, 302)
        self.assertIn('/login/', resp.url)

    def test_el_parcial_tambien_esta_protegido(self):
        """Si solo se protegiera el marco, el dato se pediría directo por la URL del
        parcial y la restricción sería decorativa."""
        self.client.force_login(self.curioso)
        self.assertEqual(self.client.get(reverse('panel:centro_monitoreo_partial')).status_code, 403)

    def test_el_seed_le_da_a_mesa_de_ayuda_el_permiso_que_la_pantalla_exige(self):
        """Si no, el rol existiría y su pantalla le quedaría cerrada — que es justo lo
        que había pasado con 'Operador RMM'."""
        from django.contrib.auth.models import Group

        call_command('seed_permisos')
        codenames = set(
            Group.objects.get(name='Mesa de Ayuda').permissions.values_list('codename', flat=True)
        )
        self.assertIn('view_alerta', codenames)

    # --- filtro por unidad de negocio ---

    def test_un_usuario_acotado_a_SG_no_ve_las_alertas_de_MIA(self):
        from apps.cuentas.models import PerfilUsuario

        solo_sg = User.objects.create_user(username='solo_sg_centro', password='x')
        solo_sg.user_permissions.add(
            Permission.objects.get(content_type__app_label='monitoreo', codename='view_alerta'),
        )
        perfil = PerfilUsuario.objects.create(usuario=solo_sg, acceso_todas_unidades=False)
        perfil.unidades_negocio.add(self.sg)

        resp = self._datos(solo_sg)

        self.assertContains(resp, 'ML940-A')
        self.assertNotContains(resp, 'MM940-A')
        self.assertEqual(resp.context['r']['alertas_criticas'], 1)

    def test_con_acceso_total_se_ven_las_dos_unidades(self):
        resp = self._datos()
        self.assertEqual(resp.context['r']['alertas_criticas'], 2)

    # --- enlaces de cada fila ---
    #
    # El destino de estas filas no es decoracion: es el unico camino desde el triage hasta
    # la pantalla donde la caida se acciona. Apuntaban siempre a /monitoreo/<pk>/, que
    # exige `monitorear_recursos`, mientras que las listas de este tablero no lo filtran
    # —ni deben: una caja POS con un servicio caido tiene que verse— asi que cada fila de
    # una estacion no monitoreada era un 404. Se reporto sobre /monitoreo/16/.

    def _permitir(self, app_label, codename, usuario=None):
        (usuario or self.mesa).user_permissions.add(
            Permission.objects.get(content_type__app_label=app_label, codename=codename),
        )

    def _hrefs_internos(self, resp):
        return [h for h in re.findall(r'href="([^"]+)"', resp.content.decode()) if h.startswith('/')]

    def test_la_fila_de_una_estacion_sin_monitoreo_NO_enlaza_a_la_ficha_de_monitoreo(self):
        """El default de `monitorear_recursos` es False: es el caso comun, no el raro."""
        self.assertFalse(self.estacion_sg.monitorear_recursos)
        # El listado de estaciones exige su propio permiso, que el rol de triage no trae.
        self._permitir('catalogo', 'view_estacion')

        hrefs = self._hrefs_internos(self._datos())

        self.assertNotIn(
            reverse('panel:monitoreo_detalle', args=[self.estacion_sg.pk]), hrefs,
            'la ficha de monitoreo rechaza esta estacion: enlazarla es mandar a un 404',
        )
        self.assertIn(
            '%s?q=%s' % (reverse('panel:estaciones_lista'), self.estacion_sg.codigo), hrefs,
        )

    def test_la_fila_de_una_estacion_monitoreada_SI_enlaza_a_su_ficha(self):
        self.estacion_sg.monitorear_recursos = True
        self.estacion_sg.save(update_fields=['monitorear_recursos'])
        self._permitir('monitoreo', 'view_muestrametrica')

        self.assertIn(
            reverse('panel:monitoreo_detalle', args=[self.estacion_sg.pk]),
            self._hrefs_internos(self._datos()),
        )

    def test_el_triage_puro_ve_el_codigo_sin_enlaces_que_no_puede_abrir(self):
        """F-08: la puerta de esta pantalla es `monitoreo.view_alerta`, y sus enlaces van a
        pantallas con OTROS permisos — la ficha de recursos, el listado de estaciones, los
        enlaces de farmacias. Para un rol de triage puro los tres respondian 403.

        Lo que no cambia es el dato: el codigo de la estacion sigue a la vista, porque dice
        QUE equipo esta en problemas y eso es para lo que se mira el tablero.
        """
        self.estacion_sg.monitorear_recursos = True
        self.estacion_sg.save(update_fields=['monitorear_recursos'])

        resp = self._datos()
        hrefs = self._hrefs_internos(resp)

        self.assertContains(resp, 'ML940-A')
        self.assertNotIn(reverse('panel:monitoreo_detalle', args=[self.estacion_sg.pk]), hrefs)
        self.assertNotIn(reverse('panel:estaciones_lista'), [h.split('?')[0] for h in hrefs])
        self.assertNotIn(reverse('panel:enlaces_farmacias_lista'), hrefs)

    def test_ningun_enlace_del_tablero_termina_en_404(self):
        """Lo que el caso puntual no cubre: se siguen TODOS los enlaces que el tablero
        genera, con un usuario que tiene todos los permisos, y ninguno puede faltar.

        Con permisos completos un 403 tampoco es aceptable, pero lo que se afirma es lo
        que rompio: el destino existe.
        """
        admin = User.objects.create_superuser(username='admin_centro', password='x')
        self.client.force_login(admin)
        resp = self.client.get(reverse('panel:centro_monitoreo_partial'))

        rotos = [h for h in self._hrefs_internos(resp) if self.client.get(h).status_code == 404]

        self.assertEqual(rotos, [], 'enlaces del Centro de Monitoreo que dan 404')

    # --- ventanas de mantenimiento ---

    def test_una_ventana_activa_saca_la_alerta_de_criticas_y_se_muestra_aparte(self):
        """Lo importante no es que la ventana se vea, sino que lo cubierto por ella NO
        aparezca como incidente: mezclarlos entrena a la mesa de ayuda a ignorar la
        pantalla.

        La alerta no se abre siquiera —`abrir_o_mantener_alerta` la silencia— así que lo
        que se verifica es el estado resultante con la ventana puesta.
        """
        from apps.monitoreo.models import Alerta, VentanaMantenimiento
        from apps.monitoreo.services import abrir_o_mantener_alerta

        Alerta.objects.all().delete()
        ventana = VentanaMantenimiento.objects.create(
            motivo='Cambio de switch', unidad_negocio=self.sg,
            destino_tipo='estaciones', desde=timezone.now() - timedelta(minutes=5),
            hasta=timezone.now() + timedelta(hours=1), activo=True,
            creado_por=self.regla_critica.creado_por,
        )
        ventana.estaciones.add(self.estacion_sg)

        creada = abrir_o_mantener_alerta(self.regla_critica, self.estacion_sg, 99)

        self.assertIsNone(creada, 'la ventana tiene que silenciar la alerta')
        resp = self._datos()
        self.assertEqual(resp.context['r']['alertas_criticas'], 0)
        self.assertEqual(len(resp.context['criticas']), 0)
        self.assertIn(ventana, resp.context['ventanas'])
        self.assertContains(resp, 'Cambio de switch')

    def test_una_ventana_de_otra_unidad_no_aparece(self):
        from apps.cuentas.models import PerfilUsuario
        from apps.monitoreo.models import VentanaMantenimiento

        VentanaMantenimiento.objects.create(
            motivo='Mantenimiento de MIA', unidad_negocio=self.mia, destino_tipo='cadena',
            desde=timezone.now() - timedelta(minutes=5), hasta=timezone.now() + timedelta(hours=1),
            activo=True, creado_por=self.regla_critica.creado_por,
        )
        solo_sg = User.objects.create_user(username='solo_sg_v', password='x')
        solo_sg.user_permissions.add(
            Permission.objects.get(content_type__app_label='monitoreo', codename='view_alerta'),
        )
        perfil = PerfilUsuario.objects.create(usuario=solo_sg, acceso_todas_unidades=False)
        perfil.unidades_negocio.add(self.sg)

        self.assertEqual(len(self._datos(solo_sg).context['ventanas']), 0)

    # --- contenido y acotado ---

    def test_los_enlaces_caidos_se_agrupan_por_proveedor(self):
        """Reusa `_agrupar_por_proveedor`, la misma del correo: es como se abre el
        ticket, un reclamo por proveedor y no uno por farmacia."""
        from apps.monitoreo.models import EstadoEnlaceFarmacia, EventoEnlaceFarmacia

        EstadoEnlaceFarmacia.objects.create(
            farmacia=self.farmacia_sg, alcanzable=False, respondio_alguna_vez=True,
            ultima_verificacion=timezone.now(),
        )
        EventoEnlaceFarmacia.objects.create(
            farmacia=self.farmacia_sg, inicio=timezone.now() - timedelta(minutes=30),
            circuito_proveedor='sangregorio-ml940-portoviejo',
        )

        resp = self._datos()

        self.assertIn('TELCONET', resp.context['enlaces_por_proveedor'])
        self.assertEqual(resp.context['r']['enlaces_caidos'], 1)

    def test_un_enlace_que_nunca_respondio_no_cuenta_como_caido(self):
        """No es una caída sino configuración pendiente; contarlo infla el tablero con
        algo que nadie va a resolver hoy. Mismo criterio que el resto del sistema."""
        from apps.monitoreo.models import EstadoEnlaceFarmacia

        EstadoEnlaceFarmacia.objects.create(
            farmacia=self.farmacia_sg, alcanzable=False, respondio_alguna_vez=False,
            ultima_verificacion=timezone.now(),
        )
        self.assertEqual(self._datos().context['r']['enlaces_caidos'], 0)

    def test_los_servicios_del_pos_se_separan_por_criticidad(self):
        from apps.monitoreo.models import EstadoServicioPos

        ahora = timezone.now()
        EstadoServicioPos.objects.create(
            estacion=self.estacion_sg, servicio='pg_local', disponible=False, critico=True,
            ultima_verificacion=ahora, ultima_respuesta=ahora - timedelta(hours=1),
        )
        EstadoServicioPos.objects.create(
            estacion=self.estacion_sg, servicio='recargas_soap', disponible=False, critico=False,
            ultima_verificacion=ahora, ultima_respuesta=ahora - timedelta(hours=1),
        )

        resp = self._datos()

        self.assertEqual(resp.context['r']['pos_criticos'], 1)
        self.assertEqual(resp.context['r']['pos_no_criticos'], 1)

    def test_es_de_solo_lectura(self):
        """Coherente con el bot de Telegram: una pantalla que se mira de reojo no puede
        tener una acción destructiva a un clic."""
        self.client.force_login(self.mesa)
        for url in (reverse('panel:centro_monitoreo'), reverse('panel:centro_monitoreo_partial')):
            self.assertIn(self.client.post(url).status_code, (403, 405))

    def test_enlaza_al_detalle_en_vez_de_reemplazarlo(self):
        """La fila lleva a la pantalla donde la caida se acciona; no la reimplementa aca.

        Se enciende `monitorear_recursos` porque ese es el unico caso en que el destino es
        la ficha de monitoreo: sin el flag la ficha responde 404 y la fila enlaza al
        listado de estaciones (ver los tests de enlaces mas arriba).
        """
        self.estacion_sg.monitorear_recursos = True
        self.estacion_sg.save(update_fields=['monitorear_recursos'])
        # Los dos destinos piden permisos que esta pantalla no exige (ver los tests de abajo).
        self._permitir('monitoreo', 'view_muestrametrica')
        self._permitir('monitoreo', 'view_estadoenlacefarmacia')

        resp = self._datos()
        self.assertContains(resp, reverse('panel:monitoreo_detalle', args=[self.estacion_sg.pk]))
        self.assertContains(resp, reverse('panel:enlaces_farmacias_lista'))

    def test_marca_el_dato_viejo_en_vez_de_mostrar_todo_verde(self):
        """Un tablero en verde con datos congelados es peor que no tener tablero."""
        resp = self._datos()
        self.assertTrue(resp.context['stale_enlaces'], 'sin muestras de red, el bloque es viejo')
        self.assertContains(resp, 'dato posiblemente viejo')

    def test_el_polling_no_es_mas_rapido_que_el_backend(self):
        """Pollear cada 10 s un dato que se recalcula cada 60 mostraría seis veces lo
        mismo, multiplicado por cada agente que deja la pantalla abierta."""
        from apps.panel.views.monitoreo import SEGUNDOS_REFRESCO_CENTRO

        self.assertGreaterEqual(SEGUNDOS_REFRESCO_CENTRO, 60)
        self.client.force_login(self.mesa)
        self.assertContains(
            self.client.get(reverse('panel:centro_monitoreo')),
            f'every {SEGUNDOS_REFRESCO_CENTRO}s',
        )


class ResumenOperacionCompartidoTests(TestCase):
    """El bot de Telegram y el Centro tienen que contestar lo mismo, porque ahora sale del
    mismo servicio. Hasta el 19-sep-2026 cada uno tenía su copia de las agregaciones."""

    def test_el_bot_usa_el_servicio_y_no_su_propia_copia(self):
        from apps.monitoreo.telegram_bot import _comando_estado

        with patch('apps.monitoreo.services.resumen_operacion') as resumen:
            resumen.return_value = {
                'estaciones_total': 10, 'estaciones_en_linea': 7, 'estaciones_fuera': 3,
                'alertas_criticas': 2, 'alertas_advertencias': 5, 'enlaces_caidos': 4,
                'pos_criticos': 1, 'pos_no_criticos': 3, 'activos_sin_sondeo': 0,
                'ultimo_sondeo_red': None, 'calculado_en': timezone.now(),
            }
            salida = _comando_estado()

        resumen.assert_called_once()
        self.assertIn('7/10', salida)
        self.assertIn('2 crítica(s), 5 advertencia(s)', salida)
        self.assertIn('Enlaces caídos: 4', salida)
        self.assertIn('Servicios del POS sin responder: 4', salida)  # 1 critico + 3 no


class GraficoConUmbralesTests(TestCase):
    """La matemática de `construir_grafico` cuando hay umbrales que dibujar.

    Es lo que hace que una gráfica se entienda sin abrir el detalle (rediseño del
    19-sep-2026, PLAN_MODERNIZACION §10-AK): si la línea de umbral no cae donde
    corresponde, el gráfico miente con más convicción que el que no la tenía.
    """

    def _grafico(self, valores, **kwargs):
        from apps.monitoreo.graficos import construir_grafico

        return construir_grafico(valores, **kwargs)

    def test_el_umbral_cae_en_la_altura_que_le_toca(self):
        """Con eje 0–100 y alto 64 (pad 4), el 50% tiene que quedar justo al medio."""
        g = self._grafico([10, 20, 30], escala_fija=100, marcas=[(50, 'warning', '50%')])
        self.assertEqual(len(g.marcas), 1)
        self.assertAlmostEqual(g.marcas[0].y, 32.0, places=1)
        self.assertAlmostEqual(g.marcas[0].pct_y, 50.0, places=1)
        self.assertTrue(g.marcas[0].dentro)

    def test_sin_umbral_configurado_no_se_dibuja_ninguna_linea(self):
        """`None` es "no hay umbral", no "umbral en cero": una línea en el piso diría
        que todo está mal."""
        g = self._grafico([10, 20], escala_fija=100, marcas=[(None, 'warning', '')])
        self.assertEqual(g.marcas, [])

    def test_el_eje_se_estira_para_que_entre_el_umbral(self):
        """Con autoescala pura, una serie tranquila llena igual la tarjeta y el umbral
        queda fuera del dibujo — justo el caso en que el gráfico tiene que mostrar
        cuánto margen queda."""
        g = self._grafico([10, 12, 11], marcas=[(20, 'critical', '20')])
        self.assertGreater(g.max_valor, 20)
        self.assertTrue(g.marcas[0].dentro)

    def test_un_umbral_lejisimos_no_aplasta_la_curva(self):
        """Una regla de latencia en 500 ms sobre una ventana que no pasa de 40: estirar
        el eje dejaría la curva pegada al piso y el gráfico no mostraría nada. Se marca
        fuera de escala y la tarjeta lo dice con un número."""
        g = self._grafico([30, 40, 35], marcas=[(500, 'critical', '500 ms')])
        self.assertEqual(g.max_valor, 40)
        self.assertFalse(g.marcas[0].dentro)

    def test_el_ultimo_punto_trae_su_posicion_en_porcentaje(self):
        """El marcador del valor actual se posiciona con HTML sobre el SVG, porque el
        SVG va estirado (`preserveAspectRatio="none"`) y deformaría un `<circle>`."""
        g = self._grafico([0, 50, 100], escala_fija=100)
        self.assertAlmostEqual(g.ultimo_pct_y, 6.25, places=2)   # y = pad = 4 sobre 64
        self.assertGreater(g.ultimo_pct_x, 95)

    def test_la_tendencia_compara_contra_el_promedio_de_la_ventana(self):
        g = self._grafico([10, 10, 10, 90], escala_fija=100)
        self.assertEqual(g.tendencia, 'sube')
        g = self._grafico([90, 90, 90, 10], escala_fija=100)
        self.assertEqual(g.tendencia, 'baja')

    def test_una_oscilacion_chica_no_mueve_la_flecha(self):
        """Con muestras cada 30 s, una flecha que se da vuelta en cada refresco es
        ruido: por debajo del 2% de la escala la tendencia es 'estable'."""
        g = self._grafico([50, 50, 50, 51], escala_fija=100)
        self.assertEqual(g.tendencia, 'estable')

    def test_una_serie_vacia_no_rompe(self):
        g = self._grafico([None, None], escala_fija=100, marcas=[(75, 'warning', '75%')])
        self.assertEqual(g.puntos, '')
        self.assertIsNone(g.ultimo_valor)
        self.assertEqual(g.marcas, [])


class UmbralesDesdeReglaAlertaTests(TestCase):
    """El umbral que se dibuja es el que abre la alerta, no una segunda verdad.

    Hasta el 19-sep-2026 el panel coloreaba con las constantes de `umbrales.py` (CPU
    75/90) mientras el motor de alertas usaba la `ReglaAlerta` configurada: con la regla
    en 85, la tarjeta se ponía amarilla antes que el sistema y no había forma de saber
    cuál de los dos números era el bueno.
    """

    def setUp(self):
        self.sg = UnidadNegocio.objects.get(codigo='SG')
        self.mia = UnidadNegocio.objects.get(codigo='MIA')
        self.autor = User.objects.create_user(username='u_reglas_umbral', password='x')

    def _regla(self, **kwargs):
        datos = dict(
            nombre='r', metrica=Metrica.CPU_CARGA_PCT, umbral=85,
            severidad=ReglaAlerta.Severidad.WARNING, creado_por=self.autor,
        )
        datos.update(kwargs)
        return ReglaAlerta.objects.create(**datos)

    def test_la_regla_activa_pisa_el_defecto_del_panel(self):
        from apps.panel.indicadores import estado_de
        from apps.panel.umbrales import umbrales_de_reglas

        # Sin reglas cargadas manda el defecto del panel (CPU: advertencia en 75).
        self.assertEqual(estado_de('cpu_carga_pct', 65), 'ok')
        self._regla(umbral=60)
        reglas = umbrales_de_reglas([self.sg])
        self.assertEqual(estado_de('cpu_carga_pct', 65, reglas), 'warning')

    def test_con_varias_unidades_a_la_vista_gana_el_limite_que_dispara_primero(self):
        """El panel muestra las dos juntas: pintar de verde algo que ya abrió alerta para
        uno de los clientes es peor que pintar de amarillo algo que para el otro todavía
        está bien."""
        from apps.panel.umbrales import umbrales_de_reglas

        self._regla(umbral=80, unidad_negocio=self.sg)
        self._regla(umbral=60, unidad_negocio=self.mia)
        reglas = umbrales_de_reglas([self.sg, self.mia])
        self.assertEqual(reglas['cpu_carga_pct']['warning'], 60)

    def test_una_regla_apagada_no_pinta_nada(self):
        from apps.panel.umbrales import umbrales_de_reglas

        self._regla(umbral=60, activo=False)
        self.assertEqual(umbrales_de_reglas([self.sg]), {})

    def test_una_regla_de_otro_cliente_no_se_cuela(self):
        from apps.panel.umbrales import umbrales_de_reglas

        self._regla(umbral=60, unidad_negocio=self.mia)
        self.assertEqual(umbrales_de_reglas([self.sg]), {})

    def test_las_reglas_globales_aplican_siempre(self):
        from apps.panel.umbrales import umbrales_de_reglas

        self._regla(umbral=60, unidad_negocio=None)
        self.assertEqual(umbrales_de_reglas([self.sg])['cpu_carga_pct']['warning'], 60)

    def test_una_regla_menor_o_igual_se_ignora(self):
        """Las tarjetas de recursos son medidores donde MÁS es peor. Una regla "≤"
        necesitaría el coloreado invertido; mezclarla pintaría al revés."""
        from apps.panel.umbrales import umbrales_de_reglas

        self._regla(umbral=60, operador=ReglaAlerta.Operador.LTE)
        self.assertEqual(umbrales_de_reglas([self.sg]), {})

    def test_una_metrica_sin_regla_ni_defecto_no_se_juzga(self):
        """Latencia y red por estación no tienen defecto. Pintarlas de verde afirmaría
        que están bien sobre una comparación que nadie hizo."""
        from apps.panel.indicadores import estado_de

        self.assertEqual(estado_de('latencia_ms', 4000), 'sin_umbral')

    def test_el_limite_del_enlace_sale_del_ancho_contratado_cuando_se_conoce(self):
        from apps.panel.umbrales import (
            RED_FARMACIA_UMBRAL_CRITICAL_KBPS, RED_FARMACIA_UMBRAL_WARNING_KBPS,
            limites_de_enlace,
        )

        self.assertEqual(limites_de_enlace(10), (7000, 9000))  # 70% y 90% de 10 Mbps
        self.assertEqual(
            limites_de_enlace(None),
            (RED_FARMACIA_UMBRAL_WARNING_KBPS, RED_FARMACIA_UMBRAL_CRITICAL_KBPS),
        )


class IndicadoresDeMonitoreoTests(TestCase):
    """Las pantallas de monitoreo, vistas como "¿se entiende sin abrir nada?"."""

    def setUp(self):
        self.sg = UnidadNegocio.objects.get(codigo='SG')
        grupo = Grupo.objects.create(codigo='TRX001')
        self.farmacia = Farmacia.objects.create(
            codigo='ML001', grupo=grupo, unidad_negocio=self.sg,
            ip_router='10.20.30.1', ancho_contratado_mbps=10,
        )
        self.estacion = Estacion.objects.create(
            codigo='ML001-SRV', farmacia=self.farmacia, monitorear_recursos=True,
            estado_aprobacion=Estacion.EstadoAprobacion.APROBADA,
        )
        self.usuario = User.objects.create_user(username='u_indicadores', password='x')
        PerfilUsuario.objects.create(usuario=self.usuario, acceso_todas_unidades=True)
        self.usuario.user_permissions.add(
            Permission.objects.get(content_type__app_label='monitoreo', codename='view_muestrametrica'),
            Permission.objects.get(content_type__app_label='monitoreo', codename='view_estadoenlacefarmacia'),
        )
        self.client.force_login(self.usuario)

    def _muestra(self, cpu=50, ram_pct=50, disco_pct=50, latencia=20, rx=100, hace=None):
        # `hace` (un timedelta) se aplica al crear y no con un UPDATE posterior: estas
        # tablas son hypertables (migración 0035) y mover una fila de chunk falla.
        return MuestraMetrica.objects.create(
            estacion=self.estacion, cpu_carga_pct=cpu,
            ram_total=1000, ram_usada=int(ram_pct * 10),
            disco_total_gb=100.0, disco_libre_gb=100.0 - disco_pct,
            latencia_ms=latencia, red_recibido_kbps=rx, red_enviado_kbps=0,
            timestamp=timezone.now() - (hace or timedelta()),
        )

    def _indicadores(self, resp):
        return {i.clave: i for i in resp.context['indicadores']}

    def test_el_detalle_dibuja_el_umbral_de_cada_recurso(self):
        self._muestra(cpu=95)
        resp = self.client.get(reverse('panel:monitoreo_detalle_partial', args=[self.estacion.pk]))
        cpu = self._indicadores(resp)['cpu_carga_pct']
        self.assertEqual(cpu.estado, 'critical')
        # Dos marcas dibujadas —advertencia y crítico— y las dos dentro del eje 0–100.
        self.assertEqual([m.nivel for m in cpu.grafico.marcas], ['warning', 'critical'])
        self.assertTrue(all(m.dentro for m in cpu.grafico.marcas))

    def test_la_latencia_sin_regla_se_muestra_sin_juzgarla(self):
        self._muestra(latencia=900)
        resp = self.client.get(reverse('panel:monitoreo_detalle_partial', args=[self.estacion.pk]))
        latencia = self._indicadores(resp)['latencia_ms']
        self.assertEqual(latencia.estado, 'sin_umbral')
        self.assertEqual(latencia.grafico.marcas, [])
        self.assertContains(resp, 'Sin regla de alerta activa')

    def test_con_regla_de_latencia_la_tarjeta_pasa_a_juzgarla(self):
        ReglaAlerta.objects.create(
            nombre='Latencia', metrica=Metrica.LATENCIA_MS, umbral=200,
            severidad=ReglaAlerta.Severidad.CRITICAL, creado_por=self.usuario,
        )
        self._muestra(latencia=900)
        resp = self.client.get(reverse('panel:monitoreo_detalle_partial', args=[self.estacion.pk]))
        self.assertEqual(self._indicadores(resp)['latencia_ms'].estado, 'critical')

    def test_el_valor_grande_es_el_de_la_ultima_muestra_no_el_de_la_ventana(self):
        """La ventana del sparkline del listado son 20 minutos; la última muestra puede
        ser más vieja si la estación se apagó. El número tiene que seguir siendo el que
        muestra el resto de la pantalla."""
        self._muestra(cpu=77, hace=timedelta(hours=4))
        resp = self.client.get(reverse('panel:monitoreo_lista'))
        tarjeta = resp.context['tarjetas'][0]
        indicadores = {i.clave: i for i in tarjeta['indicadores']}
        self.assertEqual(indicadores['cpu_carga_pct'].valor, 77)
        # ...y el sparkline, que solo mira la ventana, queda vacío en vez de inventar.
        self.assertEqual(indicadores['cpu_carga_pct'].grafico.puntos, '')

    def test_el_color_del_numero_y_el_del_punto_del_grafico_son_el_mismo(self):
        """El punto final del sparkline se pinta con `indicador.estado`, el mismo que
        colorea el valor: no puede haber un número rojo sobre un punto verde."""
        self._muestra(cpu=95)
        resp = self.client.get(reverse('panel:monitoreo_detalle_partial', args=[self.estacion.pk]))
        self.assertEqual(self._indicadores(resp)['cpu_carga_pct'].estado, resp.context['estado_cpu'])

    def test_la_lista_trae_las_series_en_una_sola_consulta(self):
        """Los sparklines del listado no pueden ser una consulta por tarjeta: la
        pantalla muestra todas las estaciones monitoreadas."""
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        self._muestra()
        with CaptureQueriesContext(connection) as ctx:
            self.client.get(reverse('panel:monitoreo_lista'))
        con_una = len(ctx.captured_queries)

        for i in range(4):
            otra = Estacion.objects.create(
                codigo='ML001-X%d' % i, farmacia=self.farmacia, monitorear_recursos=True,
                estado_aprobacion=Estacion.EstadoAprobacion.APROBADA,
            )
            MuestraMetrica.objects.create(
                estacion=otra, cpu_carga_pct=30, ram_total=1000, ram_usada=300,
                disco_total_gb=100.0, disco_libre_gb=60.0,
            )
        with CaptureQueriesContext(connection) as ctx:
            self.client.get(reverse('panel:monitoreo_lista'))
        self.assertEqual(len(ctx.captured_queries), con_una)

    def test_las_series_del_listado_calculan_los_mismos_porcentajes_que_el_modelo(self):
        """`series_por_estacion` no instancia modelos (a 1.800 equipos son 18.000 objetos
        por carga), así que repite la fórmula de las properties de MuestraMetrica. Si una
        de las dos cambia, esta prueba lo dice."""
        from apps.panel.indicadores import series_por_estacion

        muestra = self._muestra(cpu=33, ram_pct=41, disco_pct=62, rx=17)
        series = series_por_estacion([self.estacion.pk])[self.estacion.pk]
        self.assertEqual(series['ram_usada_pct'][-1], muestra.ram_usada_pct)
        self.assertEqual(series['disco_usado_pct'][-1], muestra.disco_usado_pct)
        self.assertEqual(series['red_total_kbps'][-1], muestra.red_total_kbps)

    def test_ningun_porcentaje_sale_con_coma_decimal_en_un_atributo(self):
        """Regresión del bug que tenía la barra de consumo del modal de enlaces: el
        proyecto corre en es-EC, Django imprime 12.4 como "12,4" y dentro de
        `style="width:…%"` eso es CSS inválido, así que el elemento no se dibujaba."""
        import re

        MuestraRedFarmacia.objects.create(
            farmacia=self.farmacia, bytes_recibidos=1, bytes_enviados=1,
            red_recibido_kbps=1234.5, red_enviado_kbps=200.5,
        )
        MuestraRedFarmacia.objects.create(
            farmacia=self.farmacia, bytes_recibidos=2, bytes_enviados=2,
            red_recibido_kbps=1500.5, red_enviado_kbps=210.5,
        )
        self._muestra(cpu=42.7, ram_pct=63.3, disco_pct=71.9, latencia=18.4, rx=812.3)

        paginas = [
            self.client.get(reverse('panel:monitoreo_lista')),
            self.client.get(reverse('panel:monitoreo_detalle_partial', args=[self.estacion.pk])),
            self.client.get(reverse('panel:enlaces_farmacias_lista')),
            self.client.get(reverse('panel:enlace_farmacia_modal', args=[self.farmacia.pk])),
        ]
        # Un número con coma decimal dentro de width/left/top de un style inline.
        sospechoso = re.compile(r'(?:width|left|top)\s*:\s*[0-9]+,[0-9]')
        for resp in paginas:
            self.assertEqual(resp.status_code, 200)
            hallados = sospechoso.findall(resp.content.decode('utf-8'))
            self.assertEqual(hallados, [], 'CSS inválido por localización: %s' % hallados)

    def test_el_consumo_del_enlace_usa_un_solo_criterio_de_color(self):
        """El número y la barra de abajo salían de umbrales distintos —absoluto en kbps
        contra % del contratado—, así que la misma lectura se veía verde arriba y roja
        abajo."""
        MuestraRedFarmacia.objects.create(
            farmacia=self.farmacia, bytes_recibidos=1, bytes_enviados=1,
            red_recibido_kbps=9500.0, red_enviado_kbps=0.0,
        )
        resp = self.client.get(reverse('panel:enlace_farmacia_modal', args=[self.farmacia.pk]))
        # 9.500 kbps sobre 10 Mbps contratados = 95%: crítico por el % del contratado,
        # aunque esté por debajo del umbral absoluto de 15.000 kbps.
        self.assertEqual(resp.context['estado_bw'], 'critical')
        self.assertEqual(resp.context['consumo'].estado, 'critical')

class ProveedorRealVsCircuitoTests(TestCase):
    """El proveedor sale de `tipo_enlace`, NO del primer segmento del circuito.

    Medido el 20-sep-2026 sobre las 700 farmacias: el circuito tiene la forma
    `cliente-sitio-ciudad` (`sangregorio-7deagosto-buenafe`), así que cortarlo en el
    primer guion devuelve la CADENA. El agrupado "por proveedor" venía produciendo 164
    bloques llamados `sangregorio`, `sangregorio61`, `sg278` — inútiles para reclamarle
    a nadie. `tipo_enlace` sí es el ISP y está al 100%.
    """

    def setUp(self):
        self.sg = UnidadNegocio.objects.get(codigo='SG')
        self.grupo = Grupo.objects.create(codigo='TRX960')

    def _farmacia(self, codigo, *, tipo_enlace='TELCONET', circuito='', ip='10.9.9.1'):
        from apps.catalogo.models import Farmacia

        return Farmacia.objects.create(
            codigo=codigo, grupo=self.grupo, unidad_negocio=self.sg,
            tipo_enlace=tipo_enlace, circuito_proveedor=circuito, ip_router=ip,
        )

    def test_el_proveedor_es_el_ISP_y_no_la_cadena(self):
        from apps.monitoreo.enlaces import nombre_proveedor

        farmacia = self._farmacia('ML960', tipo_enlace='TELCONET',
                                  circuito='sangregorio-7deagosto-buenafe')
        self.assertEqual(nombre_proveedor(farmacia), 'TELCONET')
        self.assertNotEqual(nombre_proveedor(farmacia), 'sangregorio')

    def test_una_farmacia_sin_proveedor_cargado_lo_dice_en_vez_de_mentir(self):
        from apps.monitoreo.enlaces import nombre_proveedor

        farmacia = self._farmacia('ML961', tipo_enlace='', circuito='sangregorio-x-y')
        self.assertEqual(nombre_proveedor(farmacia), '(proveedor sin cargar)')

    def test_el_correo_de_caidas_agrupa_por_ISP(self):
        """Un reclamo por proveedor es como se abre el ticket. Antes salían tantos
        bloques como sitios, porque cada circuito empieza distinto."""
        from apps.monitoreo.enlaces import _agrupar_por_proveedor
        from apps.monitoreo.models import EventoEnlaceFarmacia

        ahora = timezone.now()
        eventos = []
        for i, (codigo, isp) in enumerate([
            ('ML962', 'TELCONET'), ('ML963', 'TELCONET'), ('ML964', 'PUNTO NET'),
        ]):
            f = self._farmacia(codigo, tipo_enlace=isp,
                               circuito=f'sangregorio{i}-sitio{i}-ciudad', ip=f'10.9.9.{10 + i}')
            eventos.append(EventoEnlaceFarmacia.objects.create(
                farmacia=f, inicio=ahora, circuito_proveedor=f.circuito_proveedor,
            ))

        grupos = _agrupar_por_proveedor(eventos)

        self.assertEqual(set(grupos), {'TELCONET', 'PUNTO NET'})
        self.assertEqual(len(grupos['TELCONET']), 2)


class CaidasSimultaneasTests(TestCase):
    """Clasificar una caída como "es este sitio", "es el proveedor" o "el que dejó de ver
    fue el servidor".

    Es una INFERENCIA por correlación temporal, no una lectura de nada. No dice si fue
    corte de luz: eso hoy no se puede saber (ver el comentario de
    `clasificar_caidas_simultaneas`).
    """

    def setUp(self):
        self.sg = UnidadNegocio.objects.get(codigo='SG')
        self.grupo = Grupo.objects.create(codigo='TRX970')
        self.ahora = timezone.now()

    def _caida(self, codigo, isp, *, minutos_atras=0, ip_sufijo=1):
        from apps.catalogo.models import Farmacia
        from apps.monitoreo.models import EventoEnlaceFarmacia

        f = Farmacia.objects.create(
            codigo=codigo, grupo=self.grupo, unidad_negocio=self.sg,
            tipo_enlace=isp, ip_router=f'10.8.8.{ip_sufijo}',
        )
        return EventoEnlaceFarmacia.objects.create(
            farmacia=f, inicio=self.ahora - timedelta(minutes=minutos_atras),
        )

    def _clasificar(self, eventos):
        from apps.monitoreo.enlaces import clasificar_caidas_simultaneas

        return clasificar_caidas_simultaneas(eventos)

    def test_una_caida_sola_es_del_sitio(self):
        evento = self._caida('ML970', 'TELCONET', ip_sufijo=1)
        clase, _ = self._clasificar([evento])[evento.farmacia_id]
        self.assertEqual(clase, 'individual')

    def test_dos_juntas_todavia_no_alcanzan(self):
        """Dos cayendo a la vez pasa por casualidad. El mínimo es 3 por eso."""
        eventos = [self._caida(f'ML97{i}', 'TELCONET', ip_sufijo=10 + i) for i in (1, 2)]
        clases = {self._clasificar(eventos)[e.farmacia_id][0] for e in eventos}
        self.assertEqual(clases, {'individual'})

    def test_tres_del_mismo_proveedor_son_un_probable_corte_del_proveedor(self):
        eventos = [self._caida(f'ML98{i}', 'TELCONET', minutos_atras=i, ip_sufijo=20 + i)
                   for i in range(3)]
        for e in eventos:
            clase, detalle = self._clasificar(eventos)[e.farmacia_id]
            self.assertEqual(clase, 'proveedor')
            self.assertIn('TELCONET', detalle)

    def test_varios_proveedores_en_cantidad_apuntan_al_propio_monitoreo(self):
        """Cuando el que deja de ver es el servidor, caen varios ISP EN CANTIDAD: el 17-oct
        fueron Telconet 80 y Punto Net 31. Pasó también con 194 caídas en 8 minutos de 6
        proveedores, que no eran 194 farmacias caídas."""
        eventos = [self._caida(f'ML99{i}', 'TELCONET', minutos_atras=i, ip_sufijo=30 + i) for i in range(3)]
        eventos += [self._caida(f'ML98{i}', 'PUNTO NET', minutos_atras=i, ip_sufijo=40 + i) for i in range(3)]
        for e in eventos:
            clase, detalle = self._clasificar(eventos)[e.farmacia_id]
            self.assertEqual(clase, 'ceguera')
            self.assertIn('servidor', detalle)

    def test_tres_sueltas_de_tres_proveedores_no_son_el_monitoreo(self):
        """La regla vieja: tocar tres ISP alcanzaba para "el monitoreo no vio", y tres caídas
        sueltas que coincidían salían del SLA (§10-CL)."""
        eventos = [
            self._caida('ML990', 'TELCONET', ip_sufijo=30),
            self._caida('ML991', 'PUNTO NET', minutos_atras=1, ip_sufijo=31),
            self._caida('ML992', 'FIBROMARK', minutos_atras=2, ip_sufijo=32),
        ]
        self.assertEqual({self._clasificar(eventos)[e.farmacia_id][0] for e in eventos}, {'individual'})

    def test_un_corte_de_telconet_con_una_coincidencia_ajena_sigue_siendo_de_telconet(self):
        """Telconet 31 + una de Punto Net + una de Fibromark se clasificaba "el monitoreo no
        vio" y las 31 de Telconet salían del reclamo."""
        telconet = [self._caida(f'MT{i:03d}', 'TELCONET', minutos_atras=i % 5, ip_sufijo=100 + i) for i in range(5)]
        otra = self._caida('MP001', 'PUNTO NET', minutos_atras=1, ip_sufijo=90)
        clases = self._clasificar(telconet + [otra])
        self.assertEqual({clases[e.farmacia_id][0] for e in telconet}, {'proveedor'})
        self.assertEqual(clases[otra.farmacia_id][0], 'individual')

    def test_un_racimo_no_encadena_mas_de_treinta_minutos(self):
        """Sin tope, un día con muchas caídas encadenaba eventos sin relación de diez en diez."""
        eventos = [self._caida(f'MC{i:03d}', 'TELCONET', minutos_atras=i * 8, ip_sufijo=150 + i) for i in range(8)]
        clases = {self._clasificar(eventos)[e.farmacia_id][0] for e in eventos}
        self.assertIn('proveedor', clases)
        from apps.monitoreo.enlaces import armar_racimos
        self.assertGreater(len(armar_racimos(eventos)), 1)

    def test_caidas_separadas_en_el_tiempo_no_se_juntan(self):
        """Tres del mismo proveedor pero con horas de diferencia son tres incidentes."""
        eventos = [self._caida(f'MM00{i}', 'TELCONET', minutos_atras=i * 60, ip_sufijo=40 + i)
                   for i in range(3)]
        clases = {self._clasificar(eventos)[e.farmacia_id][0] for e in eventos}
        self.assertEqual(clases, {'individual'})

    def test_la_etiqueta_nunca_afirma_una_causa(self):
        """Es una inferencia y el texto lo tiene que decir: "probable", no "hubo"."""
        eventos = [self._caida(f'MM01{i}', 'TELCONET', minutos_atras=i, ip_sufijo=50 + i)
                   for i in range(3)]
        _, detalle = self._clasificar(eventos)[eventos[0].farmacia_id]
        self.assertNotIn('corte de luz', detalle.lower())
        self.assertNotIn('energía', detalle.lower())


class ColumnaProveedorEnLaTablaTests(TestCase):
    """La tabla de /monitoreo/enlaces/ muestra a quién reclamarle."""

    def setUp(self):
        from apps.catalogo.models import Farmacia
        from apps.cuentas.models import PerfilUsuario
        from apps.monitoreo.models import EstadoEnlaceFarmacia

        self.sg = UnidadNegocio.objects.get(codigo='SG')
        grupo = Grupo.objects.create(codigo='TRX980')
        self.farmacia = Farmacia.objects.create(
            codigo='ML985', grupo=grupo, unidad_negocio=self.sg, tipo_enlace='PUNTO NET',
            circuito_proveedor='sangregorio-7deagosto-buenafe', ip_router='10.7.7.1',
        )
        EstadoEnlaceFarmacia.objects.create(
            farmacia=self.farmacia, alcanzable=True, ultima_verificacion=timezone.now(),
        )
        # Sin proveedor cargado: el borde que el prompt pidió cubrir.
        self.sin_proveedor = Farmacia.objects.create(
            codigo='ML986', grupo=grupo, unidad_negocio=self.sg, tipo_enlace='',
            ip_router='10.7.7.2',
        )

        self.usuario = User.objects.create_user(username='ver_enlaces', password='x')
        self.usuario.user_permissions.add(
            Permission.objects.get(
                content_type__app_label='monitoreo', codename='view_estadoenlacefarmacia',
            ),
        )
        PerfilUsuario.objects.create(usuario=self.usuario, acceso_todas_unidades=True)

    def test_la_tabla_muestra_el_proveedor(self):
        self.client.force_login(self.usuario)
        resp = self.client.get(reverse('panel:enlaces_farmacias_lista'))
        self.assertContains(resp, 'Proveedor')
        self.assertContains(resp, 'PUNTO NET')

    def test_el_circuito_sigue_estando_porque_es_lo_que_pide_el_proveedor(self):
        """Proveedor y circuito son cosas distintas: a uno se le reclama, el otro es el
        identificador que te pide al abrir el ticket. La columna nueva no reemplaza."""
        self.client.force_login(self.usuario)
        resp = self.client.get(reverse('panel:enlaces_farmacias_lista'))
        self.assertContains(resp, 'sangregorio-7deagosto-buenafe')

    def test_una_farmacia_sin_proveedor_no_rompe_la_tabla(self):
        self.client.force_login(self.usuario)
        resp = self.client.get(reverse('panel:enlaces_farmacias_lista'))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'ML986')

    def test_sin_caidas_simultaneas_no_aparece_ninguna_etiqueta(self):
        """Una etiqueta que sale siempre deja de significar algo."""
        self.client.force_login(self.usuario)
        resp = self.client.get(reverse('panel:enlaces_farmacias_lista'))
        self.assertNotContains(resp, 'probable corte del proveedor')

    def test_tres_caidas_del_mismo_proveedor_se_avisan_una_vez_como_corte(self):
        """Hasta el 26-oct-2026 cada fila decía "probable corte del proveedor"; se quitó
        de la tabla porque repetía lo que "Qué atender primero" muestra como un solo
        incidente. El corte se sigue avisando, pero una vez y no en cada fila."""
        from apps.catalogo.models import Farmacia
        from apps.monitoreo.models import EstadoEnlaceFarmacia, EventoEnlaceFarmacia

        grupo = Grupo.objects.get(codigo='TRX980')
        ahora = timezone.now()
        for i in range(3):
            f = Farmacia.objects.create(
                codigo=f'ML99{i}', grupo=grupo, unidad_negocio=self.sg,
                tipo_enlace='TELCONET', ip_router=f'10.7.8.{i + 1}',
            )
            EstadoEnlaceFarmacia.objects.create(
                farmacia=f, alcanzable=False, respondio_alguna_vez=True,
                ultima_verificacion=ahora,
            )
            EventoEnlaceFarmacia.objects.create(farmacia=f, inicio=ahora - timedelta(minutes=i))

        self.client.force_login(self.usuario)
        resp = self.client.get(reverse('panel:enlaces_farmacias_lista'))

        self.assertContains(resp, 'Corte masivo')
        self.assertNotContains(resp, 'probable corte del proveedor')


class EstacionPausarDesdeElPanelTests(TestCase):
    """El freno de emergencia, desde la ficha de la estación.

    Permiso propio (`catalogo.pausar_estacion`) y no heredado de reiniciar/actualizar:
    pausar es la única acción que se ejerce sobre toda la flota a la vez, y quien puede
    reiniciar una caja no necesariamente debería poder frenar las 1.800.
    """

    def setUp(self):
        self.sg = UnidadNegocio.objects.get(codigo='SG')
        grupo = Grupo.objects.create(codigo='TRX061')
        farmacia = Farmacia.objects.create(codigo='ML061', grupo=grupo, unidad_negocio=self.sg)
        self.estacion = Estacion.objects.create(
            codigo='ML061-A', farmacia=farmacia,
            estado_aprobacion=Estacion.EstadoAprobacion.APROBADA,
            estado_conexion=Estacion.EstadoConexion.ONLINE,
        )
        self.sin_permiso = User.objects.create_user(username='sin_permiso_pausa', password='x')
        PerfilUsuario.objects.create(usuario=self.sin_permiso, acceso_todas_unidades=True)

        self.con_permiso = User.objects.create_user(username='con_permiso_pausa', password='x')
        PerfilUsuario.objects.create(usuario=self.con_permiso, acceso_todas_unidades=True)
        self.con_permiso.user_permissions.add(
            Permission.objects.get(content_type__app_label='catalogo', codename='pausar_estacion'),
            Permission.objects.get(content_type__app_label='catalogo', codename='view_estacion'),
        )

    def _url(self):
        return reverse('panel:estacion_pausar', args=[self.estacion.pk])

    def test_sin_permiso_no_puede(self):
        self.client.force_login(self.sin_permiso)
        with patch('apps.catalogo.services._publicar_mqtt') as publicar:
            resp = self.client.post(self._url(), {'pausado': 'true'})
        self.assertEqual(resp.status_code, 403)
        publicar.assert_not_called()

    def test_no_acepta_GET(self):
        """Frenar la flota no puede dispararse con una visita a una URL."""
        self.client.force_login(self.con_permiso)
        self.assertEqual(self.client.get(self._url()).status_code, 405)

    def test_con_permiso_publica_y_anota_la_solicitud(self):
        self.client.force_login(self.con_permiso)
        with patch('apps.catalogo.services._publicar_mqtt', return_value=True) as publicar:
            resp = self.client.post(self._url(), {'pausado': 'true'})
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(publicar.called)
        self.estacion.refresh_from_db()
        self.assertIsNotNone(self.estacion.pausa_solicitada_en)
        # Publicar no confirma: eso lo hace la estación en su latido.
        self.assertFalse(self.estacion.pausado)

    def test_funciona_aunque_la_estacion_este_FUERA_DE_LINEA(self):
        """A diferencia de reiniciar o actualizar. La orden va retenida y la estación
        apagada la recibe al encender — si solo pudiera frenarse lo conectado, las que
        arrancan después lo harían sin freno, que es el caso que importa."""
        Estacion.objects.filter(pk=self.estacion.pk).update(
            estado_conexion=Estacion.EstadoConexion.OFFLINE,
        )
        self.client.force_login(self.con_permiso)
        with patch('apps.catalogo.services._publicar_mqtt', return_value=True) as publicar:
            resp = self.client.post(self._url(), {'pausado': 'true'})
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(publicar.called, 'una estación apagada también tiene que poder frenarse')

    def test_pide_el_estado_deseado_y_no_alterna(self):
        """Dos personas mirando el panel durante una emergencia pueden apretar casi a la
        vez. Con un toggle, la segunda reanudaría lo que la primera acababa de frenar."""
        import json

        Estacion.objects.filter(pk=self.estacion.pk).update(pausado=True)
        self.client.force_login(self.con_permiso)
        with patch('apps.catalogo.services._publicar_mqtt', return_value=True) as publicar:
            self.client.post(self._url(), {'pausado': 'true'})
        payload = json.loads(publicar.call_args[0][1])
        self.assertTrue(payload['pausado'], 'pedir pausar sobre una ya pausada no la reanuda')

    def test_queda_registrado_quien_freno(self):
        from apps.auditoria.models import EventoAuditoria

        self.client.force_login(self.con_permiso)
        with patch('apps.catalogo.services._publicar_mqtt', return_value=True):
            self.client.post(self._url(), {'pausado': 'true'})
        self.assertTrue(
            EventoAuditoria.objects.filter(
                usuario=self.con_permiso, accion='estacion.pausar',
            ).exists(),
        )

    def test_si_el_broker_falla_lo_dice_en_vez_de_callar(self):
        """El peor final posible sería un panel que informa "pausada" cuando la orden no
        salió: creerías que frenaste la flota."""
        self.client.force_login(self.con_permiso)
        with patch('apps.catalogo.services._publicar_mqtt', return_value=False):
            resp = self.client.post(self._url(), {'pausado': 'true'})
        self.assertContains(resp, 'No se pudo publicar')
        self.estacion.refresh_from_db()
        self.assertIsNone(self.estacion.pausa_solicitada_en)


class EjecucionesQueNadieCierraTests(TestCase):
    """Los resultados que la estación nunca contesta se cierran solos, con el motivo.

    Hasta el 23-sep-2026 no los cerraba **nadie**. El `timeout_segundos` viaja al agente,
    que lo aplica al proceso que lanza; si el comando nunca llega —o llega y se descarta
    por reloj corrido— no había nada del lado servidor que lo diera por vencido. El
    resultado quedaba en `enviado` y la ejecución en "En progreso" indefinidamente.

    Medido en producción el 22-sep-2026: **12 resultados colgados, el más viejo de 948
    horas (39 días)** con un timeout de 300 s.
    """

    def setUp(self):
        from apps.scripts.models import EjecucionScript, ResultadoEjecucionScript, Script, TipoScript

        self.sg = UnidadNegocio.objects.get(codigo='SG')
        grupo = Grupo.objects.create(codigo='TRX880')
        farmacia = Farmacia.objects.create(codigo='ML880', grupo=grupo, unidad_negocio=self.sg)
        self.estacion = Estacion.objects.create(
            codigo='ML880-A', farmacia=farmacia,
            estado_aprobacion=Estacion.EstadoAprobacion.APROBADA,
            estado_conexion=Estacion.EstadoConexion.ONLINE,
        )
        self.autor = User.objects.create_user(username='autor_caducar', password='x')
        script = Script.objects.create(
            nombre='Prueba', tipo=TipoScript.POWERSHELL, contenido='echo 1',
            unidad_negocio=self.sg, creado_por=self.autor,
        )
        self.ejecucion = EjecucionScript.objects.create(
            script=script, contenido_snapshot='echo 1', destino_tipo=EjecucionScript.DestinoTipo.ESTACIONES,
            unidad_negocio=self.sg, creado_por=self.autor, timeout_segundos=300,
            estado=EjecucionScript.Estado.EN_PROGRESO,
        )
        self.resultado = ResultadoEjecucionScript.objects.create(
            ejecucion=self.ejecucion, estacion=self.estacion,
            estado=ResultadoEjecucionScript.Estado.ENVIADO,
            fecha_envio=timezone.now() - timedelta(days=39),
        )

    def _caducar(self):
        from apps.scripts.services import caducar_resultados_vencidos

        return caducar_resultados_vencidos()

    def _envio_vencido_pero_reciente(self):
        """Pasado el plazo (300 s + 300 s de margen) pero dentro de la hora en que el
        estado actual de la estación todavía explica lo que pasó.

        Los casos de motivo TIENEN que usar esto: con una fecha de días atrás el
        diagnóstico se niega a opinar, y con razón."""
        self.resultado.fecha_envio = timezone.now() - timedelta(seconds=700)
        self.resultado.save(update_fields=['fecha_envio'])

    def test_cierra_lo_que_lleva_39_dias_abierto(self):
        from apps.scripts.models import EjecucionScript, ResultadoEjecucionScript

        self.assertEqual(self._caducar(), 1)
        self.resultado.refresh_from_db()
        self.ejecucion.refresh_from_db()
        self.assertEqual(self.resultado.estado, ResultadoEjecucionScript.Estado.TIMEOUT)
        self.assertIsNotNone(self.resultado.fecha_fin)
        self.assertEqual(self.ejecucion.estado, EjecucionScript.Estado.CON_ERRORES)

    def test_no_toca_lo_que_todavia_esta_a_tiempo(self):
        """El timeout es para el proceso en la estación, no para el viaje: cerrar apenas
        vence el plazo convertiría en "vencido" algo que estaba por contestar."""
        from apps.scripts.models import ResultadoEjecucionScript

        self.resultado.fecha_envio = timezone.now() - timedelta(seconds=60)
        self.resultado.save(update_fields=['fecha_envio'])
        self.assertEqual(self._caducar(), 0)
        self.resultado.refresh_from_db()
        self.assertEqual(self.resultado.estado, ResultadoEjecucionScript.Estado.ENVIADO)

    def test_respeta_el_margen_sobre_el_plazo(self):
        """Justo pasado el timeout pero dentro del margen todavía no se cierra."""
        from apps.scripts.services import MARGEN_VENCIMIENTO_SEGUNDOS

        self.resultado.fecha_envio = timezone.now() - timedelta(
            seconds=self.ejecucion.timeout_segundos + MARGEN_VENCIMIENTO_SEGUNDOS - 30,
        )
        self.resultado.save(update_fields=['fecha_envio'])
        self.assertEqual(self._caducar(), 0)

    def test_se_marca_TIMEOUT_y_no_ERROR(self):
        """ERROR es lo que reporta el agente cuando el script corrió y salió mal. Esto es
        otra cosa —nunca supimos nada— y mezclarlas borraría la distinción justo en el
        estado que hay que investigar distinto."""
        from apps.scripts.models import ResultadoEjecucionScript

        self._caducar()
        self.resultado.refresh_from_db()
        self.assertNotEqual(self.resultado.estado, ResultadoEjecucionScript.Estado.ERROR)

    # --- el motivo, que es lo que faltaba en la pantalla ---

    def test_el_reloj_corrido_se_nombra_como_causa(self):
        """La causa que más desconcierta: la estación está en línea, parece sana, y
        descarta todo comando en silencio."""
        Estacion.objects.filter(pk=self.estacion.pk).update(desfase_reloj_segundos=-270)
        self._envio_vencido_pero_reciente()
        self._caducar()
        self.resultado.refresh_from_db()
        self.assertIn('reloj', self.resultado.motivo_sin_respuesta.lower())
        self.assertIn('-270', self.resultado.motivo_sin_respuesta)

    def test_una_estacion_pausada_lo_dice(self):
        Estacion.objects.filter(pk=self.estacion.pk).update(pausado=True)
        self._envio_vencido_pero_reciente()
        self._caducar()
        self.resultado.refresh_from_db()
        self.assertIn('pausada', self.resultado.motivo_sin_respuesta.lower())

    def test_una_estacion_apagada_avisa_que_el_comando_se_perdio(self):
        """`/comando/` no es retenido: lo publicado mientras estaba apagada NO le llega al
        encender. Hay que volver a lanzarlo, y la pantalla tiene que decirlo."""
        Estacion.objects.filter(pk=self.estacion.pk).update(
            estado_conexion=Estacion.EstadoConexion.OFFLINE,
        )
        self._envio_vencido_pero_reciente()
        self._caducar()
        self.resultado.refresh_from_db()
        self.assertIn('volver a lanzarlo', self.resultado.motivo_sin_respuesta)

    def test_si_empezo_y_no_termino_lo_distingue(self):
        """No es lo mismo que nunca lo recibiera: acá el script se colgó o el agente murió
        a mitad de camino, y eso se investiga distinto."""
        from apps.scripts.models import ResultadoEjecucionScript

        self.resultado.estado = ResultadoEjecucionScript.Estado.EJECUTANDO
        self.resultado.save(update_fields=['estado'])
        self._envio_vencido_pero_reciente()
        self._caducar()
        self.resultado.refresh_from_db()
        self.assertIn('nunca reportó el final', self.resultado.motivo_sin_respuesta)


    def test_ante_algo_viejo_NO_inventa_un_diagnostico(self):
        """El diagnóstico mira el estado ACTUAL de la estación. Para algo que lleva días
        colgado eso no dice nada sobre lo que pasaba entonces, y afirmarlo es peor que
        callarse.

        Lo enseñó el despliegue del 25-sep-2026: los 21 resultados viejos de la cola se
        cerraron con "la estación está en línea y no contestó", cuando la causa real
        había sido el reloj corrido de días antes — ya corregido para ese momento. El
        mensaje era cierto sobre el presente y falso sobre el incidente, que es la peor
        combinación: manda a revisar el log de una estación sana.
        """
        # setUp ya lo deja con 39 días: es exactamente el caso.
        self._caducar()
        self.resultado.refresh_from_db()
        self.assertIn('no dice qué pasaba entonces', self.resultado.motivo_sin_respuesta)
        self.assertNotIn('Revisar su log', self.resultado.motivo_sin_respuesta)

    def test_la_tarea_esta_agendada(self):
        """Un barrido que nadie corre deja el problema igual que antes."""
        from django.conf import settings

        self.assertIn('caducar-resultados-de-script', settings.CELERY_BEAT_SCHEDULE)


class LosEventosDeWindowsSeVenEnElPanelTests(TestCase):
    """La recolección de eventos funcionaba desde el 21-sep y no se podía ver.

    Estaban el agente, el catálogo administrable, la ingesta y la regla de alerta — y la
    única puerta era el admin de Django, detrás de permisos de staff. Se acumulaban
    apagones inesperados y errores de hardware que nadie del turno podía mirar.
    """

    def setUp(self):
        from apps.monitoreo.models import EventoSistemaDetectado, EventoSistemaVigilado

        self.sg = UnidadNegocio.objects.get(codigo='SG')
        grupo = Grupo.objects.create(codigo='TRX881')
        farmacia = Farmacia.objects.create(codigo='ML881', grupo=grupo, unidad_negocio=self.sg)
        self.estacion = Estacion.objects.create(
            codigo='ML881-A', farmacia=farmacia,
            estado_aprobacion=Estacion.EstadoAprobacion.APROBADA,
        )
        EventoSistemaVigilado.objects.get_or_create(
            log='System', proveedor='Microsoft-Windows-Kernel-Power', identificador=41,
            defaults={'nombre': 'Apagón inesperado',
                      'severidad': EventoSistemaVigilado.Severidad.CRITICAL},
        )
        self.detectado = EventoSistemaDetectado.objects.create(
            estacion=self.estacion, log='System',
            origen='Microsoft-Windows-Kernel-Power', identificador=41,
            cantidad_total=3, ultimo_mensaje='El sistema se reinició sin apagarse limpiamente.',
        )

    def test_resuelve_el_nombre_legible_contra_el_catalogo(self):
        """"System/41" no le dice nada a quien está de guardia; "Apagón inesperado" sí."""
        from apps.monitoreo.models import catalogo_eventos_por_clave
        from apps.monitoreo.services import eventos_sistema_recientes

        catalogo_eventos_por_clave.__globals__['_CACHE_CATALOGO_EVENTOS']['expira'] = None
        filas = eventos_sistema_recientes([self.sg])
        self.assertEqual(len(filas), 1)
        self.assertEqual(filas[0]['nombre'], 'Apagón inesperado')
        self.assertEqual(filas[0]['severidad'], 'critical')
        self.assertFalse(filas[0]['fuera_de_catalogo'])

    def test_un_evento_fuera_del_catalogo_igual_se_muestra(self):
        """Se dio de baja el seguimiento, no la historia. Y un evento que no está en el
        catálogo puede significar algo más interesante: una estación reportando con un
        catálogo viejo."""
        from apps.monitoreo.models import EventoSistemaDetectado, catalogo_eventos_por_clave
        from apps.monitoreo.services import eventos_sistema_recientes

        EventoSistemaDetectado.objects.create(
            estacion=self.estacion, log='System', origen='Inventado', identificador=9999,
            cantidad_total=1, ultimo_mensaje='x',
        )
        catalogo_eventos_por_clave.__globals__['_CACHE_CATALOGO_EVENTOS']['expira'] = None
        filas = eventos_sistema_recientes([self.sg])
        raros = [f for f in filas if f['fuera_de_catalogo']]
        self.assertEqual(len(raros), 1)
        self.assertEqual(raros[0]['nombre'], 'System/9999')

    def test_se_puede_pedir_solo_los_de_una_estacion(self):
        """Es la mitad que sirve para investigar un equipo puntual."""
        from apps.monitoreo.services import eventos_sistema_recientes

        otra = Estacion.objects.create(
            codigo='ML881-B', farmacia=self.estacion.farmacia,
            estado_aprobacion=Estacion.EstadoAprobacion.APROBADA,
        )
        self.assertEqual(len(eventos_sistema_recientes(estacion=self.estacion)), 1)
        self.assertEqual(len(eventos_sistema_recientes(estacion=otra)), 0)

    def test_respeta_la_unidad_de_negocio(self):
        """Una pantalla de triage que muestra estaciones de otro tenant es una fuga."""
        from apps.monitoreo.services import eventos_sistema_recientes

        mia = UnidadNegocio.objects.get(codigo='MIA')
        self.assertEqual(len(eventos_sistema_recientes([mia])), 0)

    def test_el_centro_de_monitoreo_los_muestra(self):
        from django.contrib.auth.models import Permission

        from apps.cuentas.models import PerfilUsuario

        usuario = User.objects.create_user(username='ve_eventos', password='x')
        PerfilUsuario.objects.create(usuario=usuario, acceso_todas_unidades=True)
        usuario.user_permissions.add(
            Permission.objects.get(content_type__app_label='monitoreo', codename='view_alerta'),
        )
        self.client.force_login(usuario)
        resp = self.client.get(reverse('panel:centro_monitoreo_partial'))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'Eventos de Windows')
        self.assertContains(resp, 'Apagón inesperado')


class EjecucionesVencidasBordesTests(TestCase):
    """Los bordes del barrido que cierra ejecuciones sin respuesta.

    Clase aparte de `EjecucionesQueNadieCierraTests` y no métodos sumados ahí: aquella
    arma en su `setUp` un resultado ya vencido, y estos casos necesitan contar sobre una
    base limpia. Plegarlos en la misma clase hacía que cada uno arrastrara esa fila y
    contara de más — pasó al escribirlos.

    Lo que se defiende acá es que un barrido que corre **cada 10 minutos** no puede
    romperse ni duplicar trabajo.
    """

    def setUp(self):
        from apps.scripts.models import Script, TipoScript

        self.sg = UnidadNegocio.objects.get(codigo='SG')
        grupo = Grupo.objects.create(codigo='TRX882')
        self.farmacia = Farmacia.objects.create(codigo='ML882', grupo=grupo, unidad_negocio=self.sg)
        self.autor = User.objects.create_user(username='a_bordes', password='x')
        self.script = Script.objects.create(
            nombre='S', tipo=TipoScript.POWERSHELL, contenido='x',
            unidad_negocio=self.sg, creado_por=self.autor,
        )

    def _ejecucion(self):
        from apps.scripts.models import EjecucionScript

        return EjecucionScript.objects.create(
            script=self.script, contenido_snapshot='x',
            destino_tipo=EjecucionScript.DestinoTipo.ESTACIONES,
            unidad_negocio=self.sg, creado_por=self.autor, timeout_segundos=300,
            estado=EjecucionScript.Estado.EN_PROGRESO,
        )

    def test_sin_fecha_envio_no_explota(self):
        """Un resultado que nunca se envio (el publish fallo) tiene fecha_envio en None.
        Restarle un timedelta reventaria el barrido entero."""
        from apps.scripts.models import EjecucionScript, ResultadoEjecucionScript
        from apps.scripts.services import caducar_resultados_vencidos

        e = Estacion.objects.create(codigo='ML882-A', farmacia=self.farmacia,
                                    estado_aprobacion=Estacion.EstadoAprobacion.APROBADA)
        ResultadoEjecucionScript.objects.create(
            ejecucion=self._ejecucion(), estacion=e,
            estado=ResultadoEjecucionScript.Estado.ERROR, fecha_envio=None,
        )
        self.assertEqual(caducar_resultados_vencidos(), 0)

    def test_es_idempotente(self):
        """Corre cada 10 min: la segunda pasada no puede volver a tocar lo ya cerrado."""
        from apps.scripts.models import EjecucionScript, ResultadoEjecucionScript
        from apps.scripts.services import caducar_resultados_vencidos

        e = Estacion.objects.create(codigo='ML882-B', farmacia=self.farmacia,
                                    estado_aprobacion=Estacion.EstadoAprobacion.APROBADA)
        ResultadoEjecucionScript.objects.create(
            ejecucion=self._ejecucion(), estacion=e,
            estado=ResultadoEjecucionScript.Estado.ENVIADO,
            fecha_envio=timezone.now() - timedelta(days=5),
        )
        self.assertEqual(caducar_resultados_vencidos(), 1)
        self.assertEqual(caducar_resultados_vencidos(), 0)

    def test_una_ejecucion_con_todos_cerrados_no_queda_en_progreso(self):
        from apps.scripts.models import EjecucionScript, ResultadoEjecucionScript
        from apps.scripts.services import caducar_resultados_vencidos

        ej = self._ejecucion()
        for i in range(3):
            e = Estacion.objects.create(codigo=f'ML882-C{i}', farmacia=self.farmacia,
                                        estado_aprobacion=Estacion.EstadoAprobacion.APROBADA)
            ResultadoEjecucionScript.objects.create(
                ejecucion=ej, estacion=e, estado=ResultadoEjecucionScript.Estado.ENVIADO,
                fecha_envio=timezone.now() - timedelta(days=5),
            )
        self.assertEqual(caducar_resultados_vencidos(), 3)
        ej.refresh_from_db()
        self.assertEqual(ej.estado, EjecucionScript.Estado.CON_ERRORES)

    def test_una_ejecucion_a_medias_sigue_en_progreso(self):
        """Si una estacion cerro por vencimiento pero otra todavia esta a tiempo, la
        ejecucion NO puede darse por terminada."""
        from apps.scripts.models import EjecucionScript, ResultadoEjecucionScript
        from apps.scripts.services import caducar_resultados_vencidos

        ej = self._ejecucion()
        vieja = Estacion.objects.create(codigo='ML882-D0', farmacia=self.farmacia,
                                        estado_aprobacion=Estacion.EstadoAprobacion.APROBADA)
        nueva = Estacion.objects.create(codigo='ML882-D1', farmacia=self.farmacia,
                                        estado_aprobacion=Estacion.EstadoAprobacion.APROBADA)
        ResultadoEjecucionScript.objects.create(
            ejecucion=ej, estacion=vieja, estado=ResultadoEjecucionScript.Estado.ENVIADO,
            fecha_envio=timezone.now() - timedelta(days=5))
        ResultadoEjecucionScript.objects.create(
            ejecucion=ej, estacion=nueva, estado=ResultadoEjecucionScript.Estado.ENVIADO,
            fecha_envio=timezone.now())
        self.assertEqual(caducar_resultados_vencidos(), 1)
        ej.refresh_from_db()
        self.assertEqual(ej.estado, EjecucionScript.Estado.EN_PROGRESO)

    def test_no_pisa_un_resultado_que_ya_contesto(self):
        from apps.scripts.models import EjecucionScript, ResultadoEjecucionScript
        from apps.scripts.services import caducar_resultados_vencidos

        e = Estacion.objects.create(codigo='ML882-E', farmacia=self.farmacia,
                                    estado_aprobacion=Estacion.EstadoAprobacion.APROBADA)
        r = ResultadoEjecucionScript.objects.create(
            ejecucion=self._ejecucion(), estacion=e,
            estado=ResultadoEjecucionScript.Estado.COMPLETADO, exit_code=0, stdout='ok',
            fecha_envio=timezone.now() - timedelta(days=30))
        self.assertEqual(caducar_resultados_vencidos(), 0)
        r.refresh_from_db()
        self.assertEqual(r.estado, ResultadoEjecucionScript.Estado.COMPLETADO)


class ReenviarViaticoObservadoTests(TestCase):
    """Un reporte observado se puede corregir y reenviar.

    Cerraba el único callejón sin salida literal del sistema: `observar_reporte` dice en
    su docstring que "devuelve el reporte al técnico para que lo corrija", y hasta el
    26-sep-2026 el técnico **veía el comentario en su lista y no tenía con qué
    responderle**. El reporte se quedaba en OBSERVADO para siempre.
    """

    def setUp(self):
        from apps.activos.models import Colaborador
        from apps.viaticos.models import EstadoReporteViatico, ReporteViatico, RubroViatico

        self.sg = UnidadNegocio.objects.get(codigo='SG')
        grupo = Grupo.objects.create(codigo='TRX840')
        self.farmacia = Farmacia.objects.create(codigo='ML840', grupo=grupo, unidad_negocio=self.sg)

        self.tecnico_user = User.objects.create_user(username='tec_viat', password='x')
        self.tecnico = Colaborador.objects.create(
            nombre='Tec Campo', cedula='0900000840',
            unidad_negocio=self.sg, usuario=self.tecnico_user,
        )
        self.coordinador = User.objects.create_user(username='coord_viat', password='x')

        self.reporte = ReporteViatico.objects.create(
            colaborador=self.tecnico, farmacia_visitada=self.farmacia,
            fecha=timezone.localdate(), rubro=RubroViatico.MOVILIZACION, monto=Decimal('12.00'),
            estado=EstadoReporteViatico.PENDIENTE,
        )

    def _observar(self):
        from apps.viaticos import services

        return services.observar_reporte(
            reporte=self.reporte, coordinador=self.coordinador,
            comentario='Falta el origen y el destino.',
        )

    # --- el servicio ---

    def test_reenviar_lo_deja_PENDIENTE_y_no_inventa_un_estado(self):
        """PENDIENTE ya significa "esperando revisión", que es donde queda. Un estado
        nuevo duplicaría ese significado y obligaría a tocar la bandeja del coordinador,
        que filtra por PENDIENTE."""
        from apps.viaticos import services
        from apps.viaticos.models import EstadoReporteViatico

        self._observar()
        services.reenviar_reporte(reporte=self.reporte, usuario=self.tecnico_user)
        self.reporte.refresh_from_db()
        self.assertEqual(self.reporte.estado, EstadoReporteViatico.PENDIENTE)

    def test_conserva_el_comentario_del_coordinador(self):
        """Es el pedido de corrección y explica por qué el reporte cambió. Borrarlo
        dejaría al coordinador revisando de nuevo sin saber qué había pedido."""
        from apps.viaticos import services

        self._observar()
        services.reenviar_reporte(reporte=self.reporte, usuario=self.tecnico_user)
        self.reporte.refresh_from_db()
        self.assertIn('origen', self.reporte.comentario_coordinador)

    def test_limpia_quien_reviso_porque_la_revision_que_viene_es_otra(self):
        from apps.viaticos import services

        self._observar()
        services.reenviar_reporte(reporte=self.reporte, usuario=self.tecnico_user)
        self.reporte.refresh_from_db()
        self.assertIsNone(self.reporte.revisado_por)
        self.assertIsNone(self.reporte.revisado_en)

    def test_no_se_reenvia_algo_que_no_esta_observado(self):
        from apps.viaticos import services
        from apps.viaticos.services import TransicionInvalida

        with self.assertRaises(TransicionInvalida):
            services.reenviar_reporte(reporte=self.reporte, usuario=self.tecnico_user)

    # --- las notificaciones, que era la mitad silenciosa ---

    def test_observar_le_avisa_al_tecnico(self):
        """Observar es pedirle algo, no archivarlo. Sin aviso, el pedido esperaba a que
        se le ocurriera mirar."""
        from apps.mantenimiento.models import Notificacion

        self._observar()
        avisos = Notificacion.objects.filter(usuario=self.tecnico_user)
        self.assertEqual(avisos.count(), 1)
        self.assertIn('observaron', avisos.first().mensaje)

    def test_reenviar_le_avisa_al_coordinador_que_lo_habia_observado(self):
        from apps.mantenimiento.models import Notificacion
        from apps.viaticos import services

        self._observar()
        services.reenviar_reporte(reporte=self.reporte, usuario=self.tecnico_user)
        avisos = Notificacion.objects.filter(usuario=self.coordinador)
        self.assertEqual(avisos.count(), 1)
        self.assertIn('reenvió', avisos.first().mensaje)

    def test_usa_la_bandeja_que_ya_existia_y_no_una_nueva(self):
        """`Notificacion` vive en `mantenimiento` por dónde nació, pero sus tres FK son
        opcionales y su tabla se llama `notificacion`. Duplicar el modelo para viáticos
        habría dado dos bandejas que el usuario tendría que mirar por separado."""
        from apps.mantenimiento.models import Notificacion

        self._observar()
        aviso = Notificacion.objects.get(usuario=self.tecnico_user)
        self.assertIsNone(aviso.mantenimiento)
        self.assertTrue(aviso.url)

    # --- la pantalla ---

    def test_el_tecnico_puede_corregirlo_desde_el_panel(self):
        from django.contrib.auth.models import Permission

        from apps.viaticos.models import EstadoReporteViatico

        self._observar()
        self.tecnico_user.user_permissions.add(
            Permission.objects.get(content_type__app_label='viaticos', codename='add_reporteviatico'),
        )
        self.client.force_login(self.tecnico_user)
        resp = self.client.get(reverse('panel:viatico_editar', args=[self.reporte.pk]))
        self.assertEqual(resp.status_code, 200)
        # El pedido de corrección se ve mientras edita, no en otra pantalla.
        self.assertContains(resp, 'Falta el origen')

    def test_nadie_corrige_el_reporte_de_otro(self):
        from django.contrib.auth.models import Permission

        from apps.activos.models import Colaborador

        self._observar()
        otro_user = User.objects.create_user(username='otro_tec', password='x')
        Colaborador.objects.create(
            nombre='Otro Tec', cedula='0900000841',
            unidad_negocio=self.sg, usuario=otro_user,
        )
        otro_user.user_permissions.add(
            Permission.objects.get(content_type__app_label='viaticos', codename='add_reporteviatico'),
        )
        self.client.force_login(otro_user)
        resp = self.client.get(reverse('panel:viatico_editar', args=[self.reporte.pk]))
        self.assertEqual(resp.status_code, 403)


class MarcarNotificacionLeidaTests(TestCase):
    """F-13: la vista escribia en la base con cualquier metodo.

    El filtro por dueno (`get_object_or_404(..., usuario=request.user)`) aisla bien: cada uno
    marca solo lo suyo. Lo que faltaba es que la URL no aceptara GET — un prefetch del
    navegador o una imagen apuntada a esa ruta marcaba la notificacion como leida. La
    plantilla ya mandaba POST con token, asi que el arreglo no cambia ninguna interaccion.
    """

    def setUp(self):
        from apps.mantenimiento.models import Notificacion

        self.usuario = User.objects.create_user(username='u_notif_405', password='x')
        PerfilUsuario.objects.create(usuario=self.usuario, acceso_todas_unidades=True)
        self.aviso = Notificacion.objects.create(usuario=self.usuario, mensaje='Revisa esto')
        self.client.force_login(self.usuario)

    def _url(self):
        return reverse('panel:notificacion_marcar_leida', args=[self.aviso.pk])

    def test_un_get_no_la_marca(self):
        resp = self.client.get(self._url())

        self.assertEqual(resp.status_code, 405)
        self.aviso.refresh_from_db()
        self.assertFalse(self.aviso.leida)

    def test_el_post_sigue_funcionando(self):
        """El arreglo no puede romper el unico camino que la pantalla usa."""
        resp = self.client.post(self._url())

        self.assertEqual(resp.status_code, 302)
        self.aviso.refresh_from_db()
        self.assertTrue(self.aviso.leida)

    def test_no_se_puede_marcar_la_de_otro(self):
        """Ya funcionaba; se afirma para que el filtro por dueno no se caiga sin que nada
        falle al reordenar los decoradores."""
        from apps.mantenimiento.models import Notificacion

        otro = User.objects.create_user(username='u_notif_ajeno', password='x')
        PerfilUsuario.objects.create(usuario=otro, acceso_todas_unidades=True)
        ajena = Notificacion.objects.create(usuario=otro, mensaje='No es tuya')

        resp = self.client.post(reverse('panel:notificacion_marcar_leida', args=[ajena.pk]))

        self.assertEqual(resp.status_code, 404)
        ajena.refresh_from_db()
        self.assertFalse(ajena.leida)


class IniciarMantenimientoExigePostTests(TestCase):
    """F-03: la otra vista mutante del panel que escribia con cualquier metodo.

    Igual que `notificacion_marcar_leida`: la plantilla ya mandaba POST con token
    (mantenimiento_detalle.html:103), asi que el `require_POST` no cambia ninguna
    interaccion — solo cierra la via que nadie usaba y cualquiera podia disparar.
    """

    def setUp(self):
        from apps.mantenimiento.models import Mantenimiento

        sg = UnidadNegocio.objects.get(codigo='SG')
        colaborador = Colaborador.objects.create(nombre='Ana F03', cedula='9F03', unidad_negocio=sg)
        self.mantenimiento = Mantenimiento.objects.create(
            cliente=colaborador, descripcion='Pantalla', fecha_programada=timezone.now(),
        )
        self.usuario = User.objects.create_user(username='u_mant_405', password='x')
        PerfilUsuario.objects.create(usuario=self.usuario, acceso_todas_unidades=True)
        self.usuario.user_permissions.add(
            Permission.objects.get(
                content_type__app_label='mantenimiento', codename='change_mantenimiento',
            ),
        )
        self.client.force_login(self.usuario)

    def test_un_get_no_lo_inicia(self):
        from apps.mantenimiento.models import Mantenimiento

        resp = self.client.get(
            reverse('panel:mantenimiento_iniciar', args=[self.mantenimiento.pk]),
        )

        self.assertEqual(resp.status_code, 405)
        self.mantenimiento.refresh_from_db()
        self.assertEqual(
            self.mantenimiento.estado_interno, Mantenimiento.EstadoInterno.PENDIENTE,
        )

    def test_el_post_sigue_iniciandolo(self):
        from apps.mantenimiento.models import Mantenimiento

        resp = self.client.post(
            reverse('panel:mantenimiento_iniciar', args=[self.mantenimiento.pk]),
        )

        self.assertEqual(resp.status_code, 302)
        self.mantenimiento.refresh_from_db()
        self.assertEqual(
            self.mantenimiento.estado_interno, Mantenimiento.EstadoInterno.EN_PROCESO,
        )


class NotificarCierreDeMantenimientoTests(TestCase):
    """Quien abre un mantenimiento se entera de que se cerró.

    El módulo avisaba al asignar, al vencer y al atrasarse — no al terminar. El que pide
    el trabajo es justamente el que está esperando, y tenía que volver a mirar.
    """

    def setUp(self):
        from apps.activos.models import Activo, CategoriaEquipo, Colaborador, Marca
        from apps.mantenimiento.models import TipoMantenimiento

        self.sg = UnidadNegocio.objects.get(codigo='SG')
        self.quien_abre = User.objects.create_user(username='abre_mant', password='x')
        self.quien_cierra = User.objects.create_user(username='cierra_mant', password='x')
        self.tipo, _ = TipoMantenimiento.objects.get_or_create(
            codigo='correctivo', defaults={'nombre': 'Correctivo'},
        )
        self.equipo = Activo.objects.create(
            codigo='EQ-840', categoria=CategoriaEquipo.objects.create(nombre='PC-840'),
            marca=Marca.objects.create(nombre='M-840'), unidad_negocio=self.sg,
        )

    def _abrir(self, usuario):
        from apps.mantenimiento.services import crear_mantenimiento_manual

        return crear_mantenimiento_manual(
            equipos=[self.equipo], tecnico=None, descripcion='Falla',
            fecha_programada=timezone.now(), usuario=usuario,
            tipo_mantenimiento=self.tipo,
        )

    def test_cerrar_le_avisa_a_quien_lo_abrio(self):
        from apps.mantenimiento.models import Notificacion
        from apps.mantenimiento.services import cerrar_mantenimiento

        m = self._abrir(self.quien_abre)
        Notificacion.objects.all().delete()  # el aviso de asignación no interesa acá
        cerrar_mantenimiento(
            mantenimiento=m, resultado_tecnico='Reemplazado', usuario=self.quien_cierra,
        )
        avisos = Notificacion.objects.filter(usuario=self.quien_abre)
        self.assertEqual(avisos.count(), 1)
        self.assertIn('cerró', avisos.first().mensaje)

    def test_cancelar_tambien_avisa(self):
        """Posiblemente importe más: el pedido NO se resolvió."""
        from apps.mantenimiento.models import Notificacion
        from apps.mantenimiento.services import cancelar_mantenimiento

        m = self._abrir(self.quien_abre)
        Notificacion.objects.all().delete()
        cancelar_mantenimiento(mantenimiento=m, motivo='Duplicado', usuario=self.quien_cierra)
        self.assertIn('canceló', Notificacion.objects.get(usuario=self.quien_abre).mensaje)

    def test_no_se_avisa_a_si_mismo(self):
        """Quien cierra ya sabe lo que hizo."""
        from apps.mantenimiento.models import Notificacion
        from apps.mantenimiento.services import cerrar_mantenimiento

        m = self._abrir(self.quien_abre)
        Notificacion.objects.all().delete()
        cerrar_mantenimiento(mantenimiento=m, resultado_tecnico='ok', usuario=self.quien_abre)
        self.assertEqual(Notificacion.objects.filter(usuario=self.quien_abre).count(), 0)

    def test_un_mantenimiento_abierto_por_el_sistema_no_avisa_a_nadie(self):
        """Los que abre una alerta o la tarea de programados tienen `usuario=None`: no
        hay a quién avisarle, y no puede explotar por eso."""
        from apps.mantenimiento.models import Notificacion
        from apps.mantenimiento.services import cerrar_mantenimiento

        m = self._abrir(None)
        Notificacion.objects.all().delete()
        cerrar_mantenimiento(mantenimiento=m, resultado_tecnico='ok', usuario=self.quien_cierra)
        self.assertEqual(Notificacion.objects.count(), 0)


class AbrirMantenimientoDesdeLaAlertaTests(TestCase):
    """El botón manual en la pantalla de alertas.

    `abrir_mantenimiento_desde_alerta` ya existía y solo se disparaba sola cuando la
    regla tenía `abre_mantenimiento`. Para el resto, alguien tenía que ir a
    Mantenimientos, crear una orden y volver a escribir de qué alerta venía.
    """

    def setUp(self):
        from django.contrib.auth.models import Permission

        from apps.activos.models import Activo, CategoriaEquipo, Marca
        from apps.mantenimiento.models import TipoMantenimiento
        from apps.monitoreo.models import Alerta, Metrica, ReglaAlerta

        self.sg = UnidadNegocio.objects.get(codigo='SG')
        grupo = Grupo.objects.create(codigo='TRX841')
        farmacia = Farmacia.objects.create(codigo='ML841', grupo=grupo, unidad_negocio=self.sg)
        self.estacion = Estacion.objects.create(
            codigo='ML841-A', farmacia=farmacia,
            estado_aprobacion=Estacion.EstadoAprobacion.APROBADA,
        )
        TipoMantenimiento.objects.get_or_create(codigo='correctivo', defaults={'nombre': 'Correctivo'})
        autor = User.objects.create_user(username='autor_ra', password='x')
        regla = ReglaAlerta.objects.create(
            nombre='Disco lleno', metrica=Metrica.DISCO_USADO_PCT, umbral=95,
            duracion_minutos=0, severidad=ReglaAlerta.Severidad.CRITICAL, creado_por=autor,
            abre_mantenimiento=False,
        )
        self.alerta = Alerta.objects.create(
            regla=regla, estacion=self.estacion, valor_disparador=99,
        )
        self.operador = User.objects.create_user(username='op_ra', password='x')
        PerfilUsuario.objects.create(usuario=self.operador, acceso_todas_unidades=True)
        for app, code in (('mantenimiento', 'add_mantenimiento'), ('monitoreo', 'view_alerta')):
            self.operador.user_permissions.add(
                Permission.objects.get(content_type__app_label=app, codename=code),
            )
        self.categoria = CategoriaEquipo.objects.create(nombre='PC-841')
        self.marca = Marca.objects.create(nombre='M-841')

    def _vincular_equipo(self):
        from apps.activos.models import Activo

        return Activo.objects.create(
            codigo='EQ-841', categoria=self.categoria, marca=self.marca,
            unidad_negocio=self.sg, estacion=self.estacion,
        )

    def _url(self):
        return reverse('panel:alerta_abrir_mantenimiento', args=[self.alerta.pk])

    def test_crea_el_mantenimiento_reusando_la_funcion_existente(self):
        from apps.mantenimiento.models import Mantenimiento

        self._vincular_equipo()
        self.client.force_login(self.operador)
        resp = self.client.post(self._url())
        self.assertEqual(resp.status_code, 302)
        m = Mantenimiento.objects.filter(equipos__equipo__codigo='EQ-841').first()
        self.assertIsNotNone(m)
        # La descripción que arma la función original menciona la alerta.
        self.assertIn(f'#{self.alerta.pk}', m.descripcion)

    def test_quien_abre_el_mantenimiento_no_termina_en_un_403(self):
        """F-14: abrir pide `add_mantenimiento` y la ficha pide `view_mantenimiento`, dos
        codenames que nada en el codigo garantiza juntos — `self.operador` tiene el primero y
        no el segundo, como el rol real al que esta pantalla apunta.

        Redirigia a la ficha, asi que la accion se ejecutaba y el usuario recibia un 403 en
        vez de la confirmacion: el mantenimiento quedaba creado y el mensaje de exito se
        perdia con el error. Se verifica el final del redirect, no el 302.
        """
        from apps.mantenimiento.models import Mantenimiento

        self._vincular_equipo()
        self.assertFalse(self.operador.has_perm('mantenimiento.view_mantenimiento'))
        self.client.force_login(self.operador)

        resp = self.client.post(self._url(), follow=True)

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(Mantenimiento.objects.count(), 1)
        self.assertContains(resp, 'abierto desde la alerta')

    def test_queda_a_nombre_de_quien_lo_apreto(self):
        """Es lo único que se le agregó a la función: antes siempre era `usuario=None`."""
        from apps.mantenimiento.models import EventoMantenimiento, Mantenimiento

        self._vincular_equipo()
        self.client.force_login(self.operador)
        self.client.post(self._url())
        m = Mantenimiento.objects.filter(equipos__equipo__codigo='EQ-841').first()
        evento = EventoMantenimiento.objects.filter(
            mantenimiento=m, tipo_evento=EventoMantenimiento.TipoEvento.PROGRAMADO,
        ).first()
        self.assertEqual(evento.usuario, self.operador)

    def test_sin_equipo_vinculado_explica_por_que_y_no_falla_en_silencio(self):
        """La función devuelve None y loguea el motivo, que es correcto para la ruta
        automática. Acá hay alguien esperando: un botón que no hace nada y no dice por
        qué es peor que no tenerlo."""
        from apps.mantenimiento.models import Mantenimiento

        self.client.force_login(self.operador)
        resp = self.client.post(self._url(), follow=True)
        self.assertEqual(Mantenimiento.objects.count(), 0)
        self.assertContains(resp, 'no tiene un equipo del inventario vinculado')

    def test_no_duplica_si_el_equipo_ya_tiene_uno_abierto(self):
        from apps.mantenimiento.models import Mantenimiento

        self._vincular_equipo()
        self.client.force_login(self.operador)
        self.client.post(self._url())
        self.assertEqual(Mantenimiento.objects.count(), 1)
        resp = self.client.post(self._url(), follow=True)
        self.assertEqual(Mantenimiento.objects.count(), 1)
        self.assertContains(resp, 'ya tiene uno abierto')

    def test_sin_permiso_no_puede(self):
        sin = User.objects.create_user(username='sin_ra', password='x')
        PerfilUsuario.objects.create(usuario=sin, acceso_todas_unidades=True)
        self.client.force_login(sin)
        self.assertEqual(self.client.post(self._url()).status_code, 403)

    def test_no_acepta_GET(self):
        self.client.force_login(self.operador)
        self.assertEqual(self.client.get(self._url()).status_code, 405)


class TraficoDeEnlaceSoloSiEsFrescoTests(TestCase):
    """Una lectura vieja no se muestra como el consumo de ahora.

    El 2-oct-2026 ocho farmacias caídas desde las 16:46 mostraban ancho de banda de
    hasta 6 h antes, y la pantalla se leía como "está caída pero pasa tráfico". El
    `Subquery` del listado tomaba la última muestra de la HISTORIA, sin ningún límite de
    tiempo, así que el último dato antes del corte se pintaba igual que uno de recién.

    El corte es por FRESCURA y no por estado del enlace, a propósito: un enlace caído
    deja de tener muestras recientes solo porque dejó de reportar, así que el efecto
    pedido sale igual — y además cubre el caso que nadie habría notado, un enlace
    marcado ACTIVO cuyo sondeo se detuvo hace horas.
    """

    def setUp(self):
        from apps.monitoreo.enlaces import registrar_sondeo
        from apps.monitoreo.models import EstadoEnlaceFarmacia, MuestraRedFarmacia
        from apps.monitoreo.services import TOLERANCIA_FRESCURA_MINUTOS

        self.tolerancia = TOLERANCIA_FRESCURA_MINUTOS['red_farmacias']
        self.sg = UnidadNegocio.objects.get(codigo='SG')
        grupo = Grupo.objects.create(codigo='TRXFR1')

        self.fresca = Farmacia.objects.create(
            codigo='FR001', grupo=grupo, unidad_negocio=self.sg, ip_router='10.40.0.1',
            ancho_contratado_mbps=10,
        )
        self.caida = Farmacia.objects.create(
            codigo='FR002', grupo=grupo, unidad_negocio=self.sg, ip_router='10.40.0.2',
            ancho_contratado_mbps=10,
        )

        registrar_sondeo(self.fresca, True, 12.0)
        # Caída REAL: respondió antes y después dejó de responder. Sin el primer sondeo
        # exitoso sería "nunca respondió", que el panel cuenta aparte.
        registrar_sondeo(self.caida, True, 14.0)
        for _ in range(EstadoEnlaceFarmacia.UMBRAL_FALLAS_CONSECUTIVAS):
            registrar_sondeo(self.caida, False, None)

        # La fresca reporta recién. La caída reportó su última lectura ANTES del corte.
        MuestraRedFarmacia.objects.create(
            farmacia=self.fresca, bytes_recibidos=1, bytes_enviados=1,
            red_recibido_kbps=400.0, red_enviado_kbps=100.0,
        )
        vieja = MuestraRedFarmacia.objects.create(
            farmacia=self.caida, bytes_recibidos=1, bytes_enviados=1,
            red_recibido_kbps=800.0, red_enviado_kbps=200.0,
        )
        # `timestamp` es auto_now_add: se corre con un UPDATE, que no lo vuelve a tocar.
        MuestraRedFarmacia.objects.filter(pk=vieja.pk).update(
            timestamp=timezone.now() - timedelta(minutes=self.tolerancia + 5),
        )

        self.usuario = User.objects.create_user(username='u_frescura', password='x')
        PerfilUsuario.objects.create(usuario=self.usuario, acceso_todas_unidades=True)
        self.usuario.user_permissions.add(
            Permission.objects.get(
                content_type__app_label='monitoreo', codename='view_estadoenlacefarmacia',
            ),
        )
        self.client.force_login(self.usuario)
        self.url = reverse('panel:enlaces_farmacias_lista')

    def _fila(self, codigo):
        resp = self.client.get(self.url)
        for fila in resp.context['filas']:
            if fila['farmacia'].codigo == codigo:
                return fila
        self.fail(f'{codigo} no aparece en el listado')

    def test_una_lectura_vieja_no_se_muestra_como_trafico(self):
        """El caso que motivó el cambio: caída, con tráfico pintado de antes del corte."""
        self.assertIsNone(self._fila('FR002')['total_kbps'])

    def test_una_lectura_reciente_si_se_muestra(self):
        """El corte no puede llevarse puesto el dato bueno."""
        self.assertEqual(self._fila('FR001')['total_kbps'], 500.0)

    def test_el_kpi_de_con_trafico_no_cuenta_las_viejas(self):
        """Si contara la histórica, el número diría que hay lecturas donde no las hay."""
        self.assertEqual(self.client.get(self.url).context['con_ancho_banda'], 1)

    def test_el_filtro_por_trafico_tambien_usa_la_frescura(self):
        """Filtrar "sin lectura SNMP" tiene que traer la caída: hoy no tiene lectura."""
        con = self.client.get(self.url, {'trafico': 'con'}).context['pagina']
        sin = self.client.get(self.url, {'trafico': 'sin'}).context['pagina']
        self.assertEqual([f.codigo for f in con.object_list], ['FR001'])
        self.assertIn('FR002', [f.codigo for f in sin.object_list])

    def test_justo_dentro_de_la_tolerancia_sigue_contando(self):
        """El límite es la tolerancia declarada, no un número inventado en la vista."""
        from apps.monitoreo.models import MuestraRedFarmacia

        MuestraRedFarmacia.objects.filter(farmacia=self.caida).update(
            timestamp=timezone.now() - timedelta(minutes=self.tolerancia - 1),
        )
        self.assertEqual(self._fila('FR002')['total_kbps'], 1000.0)

    # --- el modal de detalle, mismo criterio ---

    def test_el_modal_marca_la_lectura_vieja_y_no_la_colorea(self):
        """La lectura se sigue mostrando —sirve para diagnosticar el corte— pero dicha
        como lo que es. Un verde de hace seis horas es peor que un "sin dato"."""
        resp = self.client.get(reverse('panel:enlace_farmacia_modal', args=[self.caida.pk]))

        self.assertTrue(resp.context['lectura_vieja'])
        self.assertEqual(resp.context['estado_bw'], 'sin_dato')
        self.assertIsNone(resp.context['consumo'].valor)
        # Y se dice en pantalla, no solo en el contexto.
        self.assertContains(resp, 'ya no es actual')

    def test_el_modal_conserva_la_serie_historica(self):
        """El gráfico está para ver la FORMA: las horas previas a un corte son justo lo
        que se mira al diagnosticarlo. Se corta el VALOR, no la historia."""
        resp = self.client.get(reverse('panel:enlace_farmacia_modal', args=[self.caida.pk]))

        self.assertEqual(resp.context['total_muestras'], 1)
        self.assertIsNotNone(resp.context['ultima'])
        self.assertEqual(resp.context['pico_kbps'], 1000.0)

    def test_el_modal_de_una_lectura_fresca_no_la_marca(self):
        resp = self.client.get(reverse('panel:enlace_farmacia_modal', args=[self.fresca.pk]))

        self.assertFalse(resp.context['lectura_vieja'])
        self.assertEqual(resp.context['consumo'].valor, 500.0)
        self.assertNotContains(resp, 'ya no es actual')


class DashboardEnlacesCaidosTests(TestCase):
    """Los enlaces caídos en el tablero: ahora dentro de la cola de incidencias.

    Hasta el 3-oct-2026 el dashboard no mostraba NINGÚN enlace, pese a que una farmacia
    sin enlace no vende: el técnico abría el tablero, lo veía tranquilo, y la caída estaba
    a dos pantallas de distancia. Desde el 10-oct-2026 las caídas ya no tienen bloque
    propio: entran en la cola unificada con prioridad 1, porque una farmacia con el enlace
    caído aparecía además en la lista de alertas y en "estaciones sin reportar", tres
    bloques para un único hecho.

    Lo que estas pruebas fijan es lo mismo de antes y es donde está el riesgo: el recorte
    (que no se cuele una farmacia de otro cliente, ni una dada de baja, ni una que nunca
    respondió — las mismas tres exclusiones que aplican `resumen_operacion`, el Centro y
    la cola, para que los cuatro números no discrepen) y que el circuito del proveedor
    siga a la vista, porque es el dato con el que se abre el ticket.
    """

    def setUp(self):
        self.sg = UnidadNegocio.objects.get(codigo='SG')
        self.mia = UnidadNegocio.objects.get(codigo='MIA')
        self.grupo = Grupo.objects.create(codigo='TRXDSH')
        self.farmacia = Farmacia.objects.create(
            codigo='DSH001', grupo=self.grupo, unidad_negocio=self.sg, ip_router='10.90.0.1',
            tipo_enlace='TELCONET', circuito_proveedor='sangregorio-centro-quito',
        )
        self.de_otro_cliente = Farmacia.objects.create(
            codigo='DSH002', grupo=self.grupo, unidad_negocio=self.mia, ip_router='10.90.0.2',
        )
        usuario = User.objects.create_user(username='u_dsh', password='x')
        PerfilUsuario.objects.create(usuario=usuario, acceso_todas_unidades=True)
        self.client.force_login(usuario)

    def _caer(self, farmacia, respondio_alguna_vez=True, hace_horas=2):
        from apps.monitoreo.models import EstadoEnlaceFarmacia, EventoEnlaceFarmacia

        EstadoEnlaceFarmacia.objects.update_or_create(
            farmacia=farmacia,
            defaults={'alcanzable': False, 'respondio_alguna_vez': respondio_alguna_vez},
        )
        return EventoEnlaceFarmacia.objects.create(
            farmacia=farmacia, inicio=timezone.now() - timedelta(hours=hace_horas),
        )

    def _dashboard(self):
        resp = self.client.get(reverse('panel:dashboard'))
        self.assertEqual(resp.status_code, 200)
        return resp

    def test_sin_caidas_no_muestra_el_bloque(self):
        """Un bloque vacío permanente enseña a no mirarlo: la cola solo aparece si hay
        algo."""
        resp = self._dashboard()
        self.assertEqual(resp.context['total_enlaces_caidos'], 0)
        self.assertEqual(resp.context['incidencias'], [])
        self.assertNotContains(resp, 'Lo que duele ahora')

    def test_muestra_la_farmacia_el_proveedor_y_el_circuito(self):
        self._caer(self.farmacia)
        resp = self._dashboard()

        self.assertEqual(resp.context['total_enlaces_caidos'], 1)
        self.assertContains(resp, 'DSH001')
        # El proveedor sale de `nombre_proveedor`, el mismo punto único que usan el correo
        # de caídas y la tabla de enlaces — no de cortar el circuito, que es otra cosa.
        self.assertContains(resp, 'TELCONET')
        self.assertContains(resp, 'sangregorio-centro-quito')

    def test_usa_la_copia_del_evento_cuando_la_tiene(self):
        """`EventoEnlaceFarmacia.circuito_proveedor` es la copia tomada al momento de la
        caída: si la farmacia cambió de circuito después, el ticket se abre con el que
        estaba vigente cuando se cayó, no con el nuevo."""
        evento = self._caer(self.farmacia)
        evento.circuito_proveedor = 'circuito-viejo-del-evento'
        evento.save(update_fields=['circuito_proveedor'])
        self.farmacia.circuito_proveedor = 'circuito-nuevo-de-la-farmacia'
        self.farmacia.save(update_fields=['circuito_proveedor'])

        resp = self._dashboard()
        self.assertContains(resp, 'circuito-viejo-del-evento')
        self.assertNotContains(resp, 'circuito-nuevo-de-la-farmacia')

    def test_cae_al_circuito_de_la_farmacia_si_el_evento_no_lo_copio(self):
        """El caso real del 3-oct-2026: se importaron 702 circuitos de golpe, así que las
        caídas abiertas ANTES de esa carga tienen la copia vacía mientras la farmacia ya
        tiene el dato. Sin este respaldo el técnico leería "circuito sin cargar" teniendo
        el circuito a un clic de distancia."""
        evento = self._caer(self.farmacia)
        self.assertEqual(evento.circuito_proveedor, '', 'el evento se creó sin copia, como los viejos')

        self.assertContains(self._dashboard(), 'sangregorio-centro-quito')

    def test_sin_circuito_en_ninguno_de_los_dos_lo_dice_en_vez_de_mentir(self):
        self.farmacia.circuito_proveedor = ''
        self.farmacia.save(update_fields=['circuito_proveedor'])
        self._caer(self.farmacia)

        self.assertContains(self._dashboard(), 'circuito sin cargar')

    def test_no_muestra_la_farmacia_de_otro_cliente_sin_acceso(self):
        otro = User.objects.create_user(username='u_dsh_solo_sg', password='x')
        perfil = PerfilUsuario.objects.create(usuario=otro, acceso_todas_unidades=False)
        perfil.unidades_negocio.add(self.sg)
        self.client.force_login(otro)

        self._caer(self.de_otro_cliente)
        resp = self._dashboard()

        self.assertEqual(resp.context['total_enlaces_caidos'], 0)
        self.assertNotContains(resp, 'DSH002')

    def test_no_muestra_una_farmacia_dada_de_baja(self):
        """Nadie la sondea más, así que su caída quedó congelada: no es algo que alguien
        vaya a ir a arreglar (mismo criterio que GP063 el 2-oct-2026)."""
        self._caer(self.farmacia)
        self.assertEqual(self._dashboard().context['total_enlaces_caidos'], 1)

        self.farmacia.activa = False
        self.farmacia.save(update_fields=['activa'])
        self.assertEqual(self._dashboard().context['total_enlaces_caidos'], 0)

    def test_no_muestra_una_que_nunca_respondio(self):
        """Nunca respondió es configuración pendiente —ruta o IP mal cargada—, no una
        caída que alguien deba atender hoy."""
        self._caer(self.farmacia, respondio_alguna_vez=False)
        self.assertEqual(self._dashboard().context['total_enlaces_caidos'], 0)

    def test_la_mas_vieja_va_primero(self):
        """A igual severidad, la que lleva más tiempo caída es la que primero hay que
        atender."""
        reciente = Farmacia.objects.create(
            codigo='DSH003', grupo=self.grupo, unidad_negocio=self.sg, ip_router='10.90.0.3',
        )
        self._caer(self.farmacia, hace_horas=9)
        self._caer(reciente, hace_horas=1)

        filas = self._dashboard().context['incidencias']
        self.assertEqual([f.farmacia.codigo for f in filas], ['DSH001', 'DSH003'])

    def test_acota_a_seis_filas_pero_informa_el_total(self):
        """Es un tablero, no un listado: mismo recorte que las otras dos listas."""
        for i in range(10):
            farmacia = Farmacia.objects.create(
                codigo='DSH1%02d' % i, grupo=self.grupo, unidad_negocio=self.sg,
                ip_router='10.91.0.%d' % (i + 1),
            )
            self._caer(farmacia)

        resp = self._dashboard()
        # El KPI cuenta TODAS: es el número que decide si hay que actuar.
        self.assertEqual(resp.context['total_enlaces_caidos'], 10)
        # La cola acota las filas y dice el total aparte: es un tablero, no un listado.
        self.assertEqual(resp.context['total_incidencias'], 10)
        self.assertEqual(len(resp.context['incidencias']), 8)
        self.assertContains(resp, 'Se muestran 8 de 10')


class AlertasListaPaginacionTests(TestCase):
    """`alertas_lista` paginar desde el 3-oct-2026.

    Antes entregaba el queryset completo a la plantilla, así que `?todas=1` renderizaba
    todas las alertas de la historia. Con el filtro por defecto —solo abiertas y
    reconocidas— no se notaba, porque esas son pocas; se nota el día que alguien pide el
    historial, y a 1.300 farmacias ese día llega.

    Reusa `apps.panel.paginacion.paginar`, el helper que ya usan activos, auditoría,
    enlaces y estaciones. Lo que estas pruebas fijan es que paginar no se haya llevado
    puesto ningún filtro: ese es el modo típico de romper una lista al paginarla.
    """

    def setUp(self):
        from apps.monitoreo.models import Metrica, ReglaAlerta

        self.sg = UnidadNegocio.objects.get(codigo='SG')
        grupo = Grupo.objects.create(codigo='TRXPAG')
        self.farmacia = Farmacia.objects.create(codigo='PAG001', grupo=grupo, unidad_negocio=self.sg)
        usuario = User.objects.create_user(username='u_pag', password='x')
        PerfilUsuario.objects.create(usuario=usuario, acceso_todas_unidades=True)
        usuario.user_permissions.add(Permission.objects.get(codename='view_alerta'))
        self.client.force_login(usuario)

        self.regla = ReglaAlerta.objects.create(
            nombre='CPU alta', metrica=Metrica.CPU_CARGA_PCT, umbral=90, creado_por=usuario,
        )
        self.otra_regla = ReglaAlerta.objects.create(
            nombre='RAM alta', metrica=Metrica.RAM_USADA_PCT, umbral=90, creado_por=usuario,
        )
        self._siguiente = 0

    def _alertas(self, cantidad, regla=None, estado=None):
        from apps.monitoreo.models import Alerta

        for _ in range(cantidad):
            self._siguiente += 1
            estacion = Estacion.objects.create(
                codigo='PAG001-%02d' % self._siguiente, farmacia=self.farmacia,
                estado_aprobacion=Estacion.EstadoAprobacion.APROBADA,
            )
            extra = {'estado': estado} if estado else {}
            Alerta.objects.create(
                regla=regla or self.regla, estacion=estacion, valor_disparador=95, **extra,
            )

    def test_una_pagina_no_muestra_mas_de_por_pagina(self):
        from apps.panel.paginacion import POR_PAGINA

        self._alertas(POR_PAGINA + 5)
        resp = self.client.get(reverse('panel:alertas_lista'))

        self.assertEqual(len(resp.context['alertas']), POR_PAGINA)
        self.assertEqual(resp.context['pagina'].paginator.count, POR_PAGINA + 5)
        self.assertTrue(resp.context['pagina'].has_next())

    def test_la_segunda_pagina_trae_el_resto(self):
        from apps.panel.paginacion import POR_PAGINA

        self._alertas(POR_PAGINA + 3)
        resp = self.client.get(reverse('panel:alertas_lista'), {'pagina': 2})

        self.assertEqual(len(resp.context['alertas']), 3)
        self.assertEqual(resp.context['pagina'].number, 2)

    def test_el_filtro_por_regla_se_conserva_al_paginar(self):
        """Si paginar perdiera el filtro, pasar de página devolvería al listado completo
        — por eso `paginar` devuelve `query_filtros` SIN el parámetro de página."""
        self._alertas(3, regla=self.regla)
        self._alertas(2, regla=self.otra_regla)

        resp = self.client.get(reverse('panel:alertas_lista'), {'regla': self.regla.pk})
        self.assertEqual(resp.context['pagina'].paginator.count, 3)
        self.assertIn('regla=%d' % self.regla.pk, resp.context['query_filtros'])
        self.assertNotIn('pagina', resp.context['query_filtros'])

    def test_todas_sigue_incluyendo_las_resueltas(self):
        from apps.monitoreo.models import Alerta

        self._alertas(2)
        self._alertas(1, estado=Alerta.Estado.RESUELTA)

        por_defecto = self.client.get(reverse('panel:alertas_lista'))
        self.assertEqual(por_defecto.context['pagina'].paginator.count, 2)

        todas = self.client.get(reverse('panel:alertas_lista'), {'todas': '1'})
        self.assertEqual(todas.context['pagina'].paginator.count, 3)
        self.assertIn('todas=1', todas.context['query_filtros'])

    def test_la_vista_agrupada_sigue_funcionando_y_no_se_paginar(self):
        """Es un rollup por regla: tantas filas como reglas activas haya, y existe
        justamente para NO tener que leer una lista larga."""
        self._alertas(3, regla=self.regla)
        self._alertas(2, regla=self.otra_regla)

        resp = self.client.get(reverse('panel:alertas_lista'), {'vista': 'agrupada'})
        self.assertTrue(resp.context['vista_agrupada'])
        self.assertEqual(len(resp.context['agrupadas']), 2)


class EstacionesBusquedaTests(TestCase):
    """El buscador del listado de estaciones, el mismo que ya tenía /monitoreo/enlaces/.

    Faltaba: la pantalla solo se podía recortar con desplegables, así que llegar a UNA
    estación por su código obligaba a filtrar por grupo y recorrer páginas. Con ~1.800
    estaciones ese es el momento en que alguien la busca por el admin de Django en vez de
    por el panel.

    La prueba que más importa de este grupo es la de la IP: `Estacion.ip_lan` es
    GenericIPAddressField, o sea `inet` en PostgreSQL, y un `__icontains` sobre ese tipo
    revienta en producción aunque en SQLite —donde es texto— pase sin ruido. Corre contra
    PostgreSQL real (ver deploy/README-local.md), así que acá sí falla si alguien quita el
    `Cast`.
    """

    def setUp(self):
        self.sg = UnidadNegocio.objects.get(codigo='SG')
        self.mia = UnidadNegocio.objects.get(codigo='MIA')
        self.grupo = Grupo.objects.create(codigo='TRXBUS')
        self.otro_grupo = Grupo.objects.create(codigo='TRXOTR')
        self.farmacia = Farmacia.objects.create(
            codigo='BUS001', nombre='Sucursal Centro', grupo=self.grupo, unidad_negocio=self.sg,
        )
        self.farmacia_otro_grupo = Farmacia.objects.create(
            codigo='BUS002', nombre='Sucursal Norte', grupo=self.otro_grupo, unidad_negocio=self.sg,
        )
        self.de_otro_cliente = Farmacia.objects.create(
            codigo='BUS003', nombre='Ajena', grupo=self.grupo, unidad_negocio=self.mia,
        )

        self.buscada = self._estacion(
            'BUS001-A', self.farmacia, hostname='CAJA-CENTRO-01',
            numero_serie='SN-ABC-99', ip_lan='10.45.7.21',
        )
        self.otra = self._estacion('BUS001-B', self.farmacia, hostname='CAJA-CENTRO-02')
        self.de_otro_grupo = self._estacion('BUS002-A', self.farmacia_otro_grupo)
        self.ajena = self._estacion('BUS003-A', self.de_otro_cliente, hostname='CAJA-CENTRO-99')

        self.usuario = User.objects.create_user(username='u_busqueda', password='x')
        self.perfil = PerfilUsuario.objects.create(usuario=self.usuario, acceso_todas_unidades=True)
        self.usuario.user_permissions.add(
            Permission.objects.get(content_type__app_label='catalogo', codename='view_estacion'),
        )
        self.client.force_login(self.usuario)

    def _estacion(self, codigo, farmacia, **extra):
        return Estacion.objects.create(
            codigo=codigo, farmacia=farmacia,
            estado_aprobacion=Estacion.EstadoAprobacion.APROBADA, **extra,
        )

    def _codigos(self, **filtros):
        resp = self.client.get(reverse('panel:estaciones_lista'), filtros)
        self.assertEqual(resp.status_code, 200)
        return sorted(e.codigo for e in resp.context['estaciones'])

    def test_sin_termino_trae_todo(self):
        self.assertEqual(
            self._codigos(), ['BUS001-A', 'BUS001-B', 'BUS002-A', 'BUS003-A'],
        )

    def test_busca_por_codigo_de_estacion(self):
        self.assertEqual(self._codigos(q='BUS001-A'), ['BUS001-A'])

    def test_busca_por_codigo_de_farmacia(self):
        """El caso más común: el técnico sabe la farmacia, no la estación."""
        self.assertEqual(self._codigos(q='BUS001'), ['BUS001-A', 'BUS001-B'])

    def test_busca_por_nombre_de_farmacia(self):
        self.assertEqual(self._codigos(q='Sucursal Norte'), ['BUS002-A'])

    def test_busca_por_hostname(self):
        self.assertEqual(self._codigos(q='CAJA-CENTRO-01'), ['BUS001-A'])

    def test_busca_por_numero_de_serie(self):
        """Es el dato que está en la etiqueta del equipo, cuando el técnico lo tiene
        delante y no sabe qué código le pusieron."""
        self.assertEqual(self._codigos(q='SN-ABC-99'), ['BUS001-A'])

    def test_busca_por_ip_lan_sobre_postgresql(self):
        """`ip_lan` es `inet`: sin el Cast explícito esto revienta en producción.

        No es una hipótesis — es la misma trampa que `ip_router` en enlaces, documentada
        en esa vista, y de la misma familia que el `ip_lan='localhost'` del §10 del plan.
        """
        self.assertEqual(self._codigos(q='10.45.7.21'), ['BUS001-A'])
        # Parcial, que es como alguien escribe una IP a medias.
        self.assertEqual(self._codigos(q='10.45.7.'), ['BUS001-A'])

    def test_busca_por_canal(self):
        self.assertEqual(self._codigos(q='TRXOTR'), ['BUS002-A'])

    def test_no_distingue_mayusculas(self):
        self.assertEqual(self._codigos(q='bus001-a'), ['BUS001-A'])

    def test_un_termino_que_no_calza_devuelve_vacio_sin_romper(self):
        self.assertEqual(self._codigos(q='no-existe-nada'), [])

    def test_los_contadores_siguen_contando_el_total(self):
        """Lo que estas tarjetas prometen es el conjunto COMPLETO: "4 estaciones" tiene
        que seguir diciendo 4 aunque estés buscando una. Si el buscador se aplicara antes
        de contar, la tarjeta diría 1 y dejaría de servir para saber si hay un problema."""
        resp = self.client.get(reverse('panel:estaciones_lista'), {'q': 'BUS001-A'})
        self.assertEqual(len(resp.context['estaciones']), 1, 'la tabla sí se recorta')
        self.assertEqual(resp.context['kpi']['total'], 4, 'los contadores NO')

    def test_se_combina_con_el_filtro_de_grupo(self):
        """Buscar y filtrar tienen que poder convivir: el formulario manda los dos."""
        self.assertEqual(self._codigos(q='BUS', grupo='TRXOTR'), ['BUS002-A'])

    def test_no_encuentra_estaciones_de_otro_cliente(self):
        """El buscador no puede ser la puerta de atrás al alcance por tenant: busca sobre
        el queryset YA escopeado."""
        self.perfil.acceso_todas_unidades = False
        self.perfil.save(update_fields=['acceso_todas_unidades'])
        self.perfil.unidades_negocio.add(self.sg)

        # 'CAJA-CENTRO' calza con la ajena por hostname y con las dos propias.
        self.assertEqual(self._codigos(q='CAJA-CENTRO'), ['BUS001-A', 'BUS001-B'])

    def test_el_termino_vuelve_al_formulario(self):
        """Si no, al buscar el campo se vacía y no se ve qué se buscó."""
        resp = self.client.get(reverse('panel:estaciones_lista'), {'q': 'BUS001-A'})
        self.assertEqual(resp.context['filtro_q'], 'BUS001-A')
        self.assertContains(resp, 'value="BUS001-A"')

    def test_el_termino_se_conserva_al_cambiar_de_pagina(self):
        self.assertIn('q=BUS001', self.client.get(
            reverse('panel:estaciones_lista'), {'q': 'BUS001'},
        ).context['query_filtros'])

    def test_los_espacios_sobrantes_no_cuentan(self):
        """Pegar un código desde un chat trae espacios; sin el strip no encontraría nada."""
        self.assertEqual(self._codigos(q='  BUS001-A  '), ['BUS001-A'])


class FarmaciaPrecargadaDesdeLaSerieTests(TestCase):
    """El alta de activo precarga tambien la farmacia desde la serie.

    Antes "Buscar equipo por serie" completaba procesador, RAM y disco, y mostraba de que
    estacion habia salido — pero dejaba la farmacia vacia, teniendo el dato a la vista.
    Elegirla a mano era trabajo duplicado, y la causa de que el activo quedara sin
    farmacia cargada.

    El campo se completa por intercambio FUERA DE BANDA de HTMX: tiene que quedar donde
    el usuario lo espera, en la seccion "Ubicacion" junto a Bodega, y no mudarse al bloque
    de especificaciones porque se apreto un boton.
    """

    def setUp(self):
        self.sg = UnidadNegocio.objects.get(codigo='SG')
        self.grupo = Grupo.objects.create(codigo='TRXPRE')
        self.farmacia = Farmacia.objects.create(
            codigo='PRE001', grupo=self.grupo, unidad_negocio=self.sg,
        )
        self.bodega = Bodega.objects.create(
            codigo='BOD-PRE', nombre='Bodega de prueba', unidad_negocio=self.sg,
        )
        self.estacion = Estacion.objects.create(
            codigo='PRE001-A', farmacia=self.farmacia, numero_serie='SN-PRE-1',
            procesador='Intel i3', ram_total_mb=7839, almacenamiento_total_gb=446,
        )
        self.usuario = User.objects.create_user(username='u_precarga', password='x')
        PerfilUsuario.objects.create(usuario=self.usuario, acceso_todas_unidades=True)
        self.usuario.user_permissions.add(
            Permission.objects.get(content_type__app_label='activos', codename='add_activo'),
        )
        self.client.force_login(self.usuario)
        self.url = reverse('panel:especificaciones_por_serie_partial')

    def test_la_precarga_y_lo_dice(self):
        resp = self.client.get(self.url, {'numero_serie': 'SN-PRE-1'})

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.context['farmacia_sugerida'], self.farmacia)
        # El aviso tiene que nombrarla: completar un campo en silencio es peor que no
        # completarlo, porque el usuario guarda sin mirar.
        self.assertContains(resp, 'PRE001')
        # Y va fuera de banda, para no mudar el campo de seccion.
        self.assertContains(resp, 'hx-swap-oob')
        self.assertContains(resp, 'id="campo-farmacia"')

    def test_no_pisa_una_farmacia_ya_elegida(self):
        """Mismo contrato que las especificaciones: la estacion COMPLETA lo que falta,
        nunca reemplaza lo que el usuario cargo a mano."""
        otra = Farmacia.objects.create(
            codigo='PRE002', grupo=self.grupo, unidad_negocio=self.sg,
        )
        resp = self.client.get(self.url, {'numero_serie': 'SN-PRE-1', 'farmacia': str(otra.pk)})

        self.assertIsNone(resp.context['farmacia_sugerida'])
        self.assertNotContains(resp, 'hx-swap-oob')

    def test_no_pisa_una_bodega_ya_elegida(self):
        """"Ingresa a bodega" y "ya esta instalado en una farmacia" son las dos mitades de
        la misma pregunta (ver ActivoIngresoForm.clean). Rellenar la farmacia sobre una
        bodega elegida dejaria el formulario diciendo dos cosas a la vez, y la que pierde
        seria la que el usuario eligio."""
        resp = self.client.get(
            self.url, {'numero_serie': 'SN-PRE-1', 'bodega': str(self.bodega.pk)},
        )

        self.assertIsNone(resp.context['farmacia_sugerida'])
        self.assertNotContains(resp, 'hx-swap-oob')

    def test_serie_desconocida_no_sugiere_nada(self):
        resp = self.client.get(self.url, {'numero_serie': 'NO-EXISTE'})

        self.assertIsNone(resp.context['farmacia_sugerida'])
        self.assertNotContains(resp, 'hx-swap-oob')
        # El aviso de "busque y no hay" sigue saliendo: es lo que distingue eso de la
        # carga inicial del formulario.
        self.assertContains(resp, 'NO-EXISTE')

    def test_las_especificaciones_siguen_precargandose(self):
        """Lo que ya funcionaba tiene que seguir funcionando."""
        resp = self.client.get(self.url, {'numero_serie': 'SN-PRE-1'})

        self.assertEqual(resp.context['form']['procesador'].value(), 'Intel i3')
        self.assertEqual(resp.context['form']['ram_gb'].value(), 8)
        self.assertEqual(resp.context['form']['almacenamiento_gb'].value(), 446)

    def test_no_sugiere_la_farmacia_de_otro_cliente(self):
        """El alcance lo aplica el servicio, pero la vista es la unica puerta expuesta:
        si alguien lo cambia alla, esto falla aca."""
        perfil = PerfilUsuario.objects.get(usuario=self.usuario)
        perfil.acceso_todas_unidades = False
        perfil.save(update_fields=['acceso_todas_unidades'])
        perfil.unidades_negocio.add(UnidadNegocio.objects.get(codigo='MIA'))

        resp = self.client.get(self.url, {'numero_serie': 'SN-PRE-1'})

        self.assertIsNone(resp.context['farmacia_sugerida'])
        self.assertIsNone(resp.context['estacion'])
        self.assertContains(resp, 'No se encontró')


class BusquedaCompartidaTests(TestCase):
    """`apps.panel.busqueda.buscar`: el filtro libre que comparten los listados.

    Existe para no escribir el mismo `Q(...)` en 28 vistas. Lo que estas pruebas fijan es
    lo que cada una de esas 28 habria tenido que acordarse sola: convertir las columnas
    `inet`, limpiar el termino, y que un termino vacio signifique "todo" y no "nada".
    """

    def setUp(self):
        self.sg = UnidadNegocio.objects.get(codigo='SG')
        grupo = Grupo.objects.create(codigo='TRXHLP')
        self.farmacia = Farmacia.objects.create(
            codigo='HLP001', nombre='Sucursal Helper', grupo=grupo, unidad_negocio=self.sg,
        )
        self.con_ip = Estacion.objects.create(
            codigo='HLP001-A', farmacia=self.farmacia, hostname='CAJA-UNO',
            ip_lan='10.60.3.44',
        )
        self.sin_ip = Estacion.objects.create(
            codigo='HLP001-B', farmacia=self.farmacia, hostname='CAJA-DOS',
        )
        self.fabrica = RequestFactory()

    def _buscar(self, termino, campos=('codigo', 'hostname', 'ip_lan')):
        from apps.panel.busqueda import buscar

        peticion = self.fabrica.get('/', {'q': termino} if termino is not None else {})
        qs, devuelto = buscar(Estacion.objects.all(), peticion, campos)
        return sorted(e.codigo for e in qs), devuelto

    def test_busca_en_un_campo_de_texto(self):
        self.assertEqual(self._buscar('CAJA-UNO')[0], ['HLP001-A'])

    def test_busca_con_or_entre_los_campos(self):
        """Un termino que calza por un campo en una fila y por otro campo en otra tiene que
        traer las dos: es OR, no AND."""
        codigos, _ = self._buscar('HLP001')
        self.assertEqual(codigos, ['HLP001-A', 'HLP001-B'])

    def test_sin_termino_devuelve_el_queryset_intacto(self):
        """`?q=` es lo que manda un formulario con el campo en blanco, y significa "todo".
        Si significara "nada", abrir el listado desde el formulario lo mostraria vacio."""
        self.assertEqual(self._buscar('')[0], ['HLP001-A', 'HLP001-B'])
        self.assertEqual(self._buscar(None)[0], ['HLP001-A', 'HLP001-B'])

    def test_devuelve_el_termino_limpio(self):
        """Se devuelve para que el formulario lo muestre de vuelta: sin eso, al buscar el
        campo queda vacio y no se ve que se busco."""
        codigos, termino = self._buscar('   CAJA-UNO   ')
        self.assertEqual(termino, 'CAJA-UNO')
        self.assertEqual(codigos, ['HLP001-A'], 'los espacios de un codigo pegado no cuentan')

    def test_no_distingue_mayusculas(self):
        self.assertEqual(self._buscar('caja-uno')[0], ['HLP001-A'])

    def test_convierte_las_columnas_inet_antes_de_comparar(self):
        """La razon principal de que este helper exista.

        `ip_lan` es GenericIPAddressField, o sea `inet` en PostgreSQL: un `__icontains`
        sobre ese tipo revienta la consulta. En SQLite pasa sin ruido, asi que una prueba
        contra el motor equivocado no lo detecta — esta corre contra PostgreSQL real (ver
        deploy/README-local.md).
        """
        self.assertEqual(self._buscar('10.60.3.44')[0], ['HLP001-A'])
        # Parcial, que es como alguien escribe una IP a medias.
        self.assertEqual(self._buscar('10.60.3.')[0], ['HLP001-A'])

    def test_detecta_el_inet_cruzando_tablas(self):
        """La deteccion camina la ruta por los modelos relacionados: si solo mirara el
        modelo de arranque, `farmacia__ip_router` se le escaparia y volveria el mismo
        error, pero en el listado que cruza tablas."""
        self.farmacia.ip_router = '192.170.57.254'
        self.farmacia.save(update_fields=['ip_router'])

        codigos, _ = self._buscar('192.170.57.254', campos=('codigo', 'farmacia__ip_router'))
        self.assertEqual(codigos, ['HLP001-A', 'HLP001-B'])

    def test_una_ruta_que_no_existe_no_la_tapa_el_helper(self):
        """Si la vista escribe mal un campo, tiene que fallar con el error de Django que
        explica que paso — no quedar en silencio devolviendo cualquier cosa."""
        from django.core.exceptions import FieldError

        with self.assertRaises(FieldError):
            self._buscar('algo', campos=('campo_que_no_existe',))

    def test_el_parametro_se_puede_cambiar(self):
        """Una pantalla con dos buscadores necesita nombres distintos."""
        from apps.panel.busqueda import buscar

        peticion = self.fabrica.get('/', {'buscar_equipo': 'CAJA-DOS'})
        qs, termino = buscar(
            Estacion.objects.all(), peticion, ('codigo', 'hostname'), parametro='buscar_equipo',
        )
        self.assertEqual(termino, 'CAJA-DOS')
        self.assertEqual([e.codigo for e in qs], ['HLP001-B'])


class BuscadorEnActivosTests(TestCase):
    """Busqueda libre en el listado de activos (tanda 1).

    La pantalla ya tenia un campo de texto, pero solo para FARMACIA: con miles de activos
    no habia forma de llegar a uno por su serie o su codigo SAP desde el panel. Los dos
    filtros conviven — uno pregunta "donde esta" y el otro "cual es".
    """

    def setUp(self):
        self.sg = UnidadNegocio.objects.get(codigo='SG')
        grupo = Grupo.objects.create(codigo='TRXACT')
        self.farmacia = Farmacia.objects.create(
            codigo='ACT001', nombre='Sucursal Machala', grupo=grupo, unidad_negocio=self.sg,
        )
        self.otra = Farmacia.objects.create(
            codigo='ACT002', nombre='Sucursal Quito', grupo=grupo, unidad_negocio=self.sg,
        )
        from apps.activos.models import Marca

        self.marca = Marca.objects.create(nombre='Hewlett Packard')
        self.buscado = self._activo(
            numero_serie='SN-BUSCADO-1', codigo_sap='SAP-9001', modelo='ProDesk 400',
            ip='10.70.4.15', farmacia=self.farmacia,
        )
        self.otro = self._activo(
            numero_serie='SN-OTRO-2', codigo_sap='SAP-9002', modelo='EliteDesk 800',
            farmacia=self.otra,
        )

        self.usuario = User.objects.create_user(username='u_busca_act', password='x')
        PerfilUsuario.objects.create(usuario=self.usuario, acceso_todas_unidades=True)
        self.usuario.user_permissions.add(
            Permission.objects.get(content_type__app_label='activos', codename='view_activo'),
        )
        self.client.force_login(self.usuario)
        self.url = reverse('panel:activos_lista')

    _siguiente = 0

    def _activo(self, **extra):
        # Codigo explicito, como el resto de las pruebas de panel: generar_codigo_activo
        # consulta la secuencia y aca no se esta probando eso.
        type(self)._siguiente += 1
        return Activo.objects.create(
            codigo='CR-DSK-%04d' % type(self)._siguiente, tipo=Activo.Tipo.DESKTOP,
            marca=self.marca, unidad_negocio=self.sg, estado=Activo.Estado.ASIGNADO, **extra,
        )

    def _codigos(self, **filtros):
        resp = self.client.get(self.url, filtros)
        self.assertEqual(resp.status_code, 200)
        return sorted(a.codigo for a in resp.context['activos'])

    def test_busca_por_numero_de_serie(self):
        self.assertEqual(self._codigos(q='SN-BUSCADO-1'), [self.buscado.codigo])

    def test_busca_por_codigo_sap(self):
        self.assertEqual(self._codigos(q='SAP-9001'), [self.buscado.codigo])

    def test_busca_por_modelo_y_por_marca(self):
        self.assertEqual(self._codigos(q='ProDesk'), [self.buscado.codigo])
        # La marca es una FK: se busca por su nombre, no por su id.
        self.assertEqual(
            self._codigos(q='Hewlett'),
            sorted([self.buscado.codigo, self.otro.codigo]),
        )

    def test_busca_por_ip_sobre_postgresql(self):
        """`Activo.ip` es `inet`: sin la conversion del helper esto revienta en produccion
        y pasa en SQLite."""
        self.assertEqual(self._codigos(q='10.70.4.15'), [self.buscado.codigo])
        self.assertEqual(self._codigos(q='10.70.4.'), [self.buscado.codigo])

    def test_el_filtro_de_farmacia_sigue_funcionando(self):
        """Lo que ya existia no se reemplaza: `farmacia` es un filtro propio y documentado,
        con su propio motivo (700 farmacias no caben en un desplegable)."""
        self.assertEqual(self._codigos(farmacia='Machala'), [self.buscado.codigo])

    def test_los_dos_se_combinan(self):
        """Buscar el equipo Y acotar la farmacia tiene que dar la interseccion."""
        self.assertEqual(self._codigos(q='Hewlett', farmacia='Quito'), [self.otro.codigo])
        self.assertEqual(self._codigos(q='SN-BUSCADO-1', farmacia='Quito'), [])

    def test_el_termino_vuelve_al_formulario(self):
        resp = self.client.get(self.url, {'q': 'SAP-9001'})
        self.assertEqual(resp.context['busqueda'], 'SAP-9001')
        self.assertContains(resp, 'value="SAP-9001"')

    def test_el_termino_se_conserva_al_paginar(self):
        self.assertIn('q=SAP-9001', self.client.get(self.url, {'q': 'SAP-9001'}).context['query_filtros'])


class BuscadorEnAuditoriaTests(TestCase):
    """Busqueda libre en el registro de auditoria (tanda 1).

    Es la unica tabla que SOLO crece, asi que paginarla no alcanzaba: responder "quien dio
    de baja esta farmacia" obligaba a recorrer paginas hasta encontrarlo.
    """

    def setUp(self):
        self.sg = UnidadNegocio.objects.get(codigo='SG')
        self.actor = User.objects.create_user(username='tecnico.perez', password='x')
        self.otro_actor = User.objects.create_user(username='admin.lopez', password='x')

        EventoAuditoria.objects.create(
            usuario=self.actor, unidad_negocio=self.sg, accion='farmacia.editar',
            modelo='Farmacia', objeto_id='77', objeto_repr='GP063', ip_address='10.111.6.20',
        )
        EventoAuditoria.objects.create(
            usuario=self.otro_actor, unidad_negocio=self.sg, accion='despliegue.aprobar',
            modelo='Despliegue', objeto_id='12', objeto_repr='POS 2.5.0', ip_address='10.111.6.99',
        )

        self.usuario = User.objects.create_user(username='u_busca_aud', password='x')
        PerfilUsuario.objects.create(usuario=self.usuario, acceso_todas_unidades=True)
        self.usuario.user_permissions.add(
            Permission.objects.get(
                content_type__app_label='auditoria', codename='view_eventoauditoria',
            ),
        )
        self.client.force_login(self.usuario)
        self.url = reverse('panel:auditoria_lista')

    def _acciones(self, **filtros):
        resp = self.client.get(self.url, filtros)
        self.assertEqual(resp.status_code, 200)
        return sorted(e.accion for e in resp.context['eventos'])

    def test_sin_termino_trae_todo(self):
        self.assertEqual(self._acciones(), ['despliegue.aprobar', 'farmacia.editar'])

    def test_busca_por_accion(self):
        self.assertEqual(self._acciones(q='despliegue'), ['despliegue.aprobar'])

    def test_busca_por_objeto(self):
        """El caso real: "quien toco GP063"."""
        self.assertEqual(self._acciones(q='GP063'), ['farmacia.editar'])

    def test_busca_por_usuario(self):
        self.assertEqual(self._acciones(q='perez'), ['farmacia.editar'])

    def test_busca_por_ip_sobre_postgresql(self):
        """`ip_address` es `inet`: la misma conversion que en activos."""
        self.assertEqual(self._acciones(q='10.111.6.99'), ['despliegue.aprobar'])

    def test_el_subtitulo_ya_no_afirma_un_corte_que_no_existe(self):
        """Decia "Ultimos 200 eventos" despues de que la vista pasara a paginar: afirmaba
        justo lo contrario de lo que ese cambio vino a arreglar."""
        resp = self.client.get(self.url)
        self.assertNotContains(resp, 'Últimos 200 eventos')
        self.assertContains(resp, 'Registro inmutable')


class BuscadorYPaginacionTanda2Tests(TestCase):
    """Los tres listados que traian TODO (tanda 2): monitoreo de servidores, movimientos
    de inventario y mantenimientos.

    Dos de ellos no solo les faltaba buscador: cortaban o renderizaban la tabla completa.
    `movimientos_inventario_lista` cortaba con `[:500]`, que es el mismo defecto que tenia
    auditoria con `[:200]` — no una optimizacion sino una perdida silenciosa.
    """

    def setUp(self):
        from apps.activos.models import MovimientoInventario, TipoConsumible

        self.sg = UnidadNegocio.objects.get(codigo='SG')
        self.grupo = Grupo.objects.create(codigo='TRXT2')
        self.farmacia = Farmacia.objects.create(
            codigo='T2A001', nombre='Sucursal Tanda Dos', grupo=self.grupo, unidad_negocio=self.sg,
        )
        self.servidor = Estacion.objects.create(
            codigo='T2A001-S', farmacia=self.farmacia, hostname='SRV-TANDA',
            estado_aprobacion=Estacion.EstadoAprobacion.APROBADA, monitorear_recursos=True,
        )
        self.otro_servidor = Estacion.objects.create(
            codigo='T2A001-T', farmacia=self.farmacia, hostname='SRV-OTRO',
            estado_aprobacion=Estacion.EstadoAprobacion.APROBADA, monitorear_recursos=True,
        )

        self.bodega = Bodega.objects.create(
            codigo='BOD-T2', nombre='Bodega tanda dos', unidad_negocio=self.sg,
        )
        self.consumible = TipoConsumible.objects.create(nombre='Toner negro 85A')
        self.usuario = User.objects.create_user(username='u_tanda2', password='x')
        MovimientoInventario.objects.create(
            tipo_movimiento=MovimientoInventario.TipoMovimiento.INGRESO_CONSUMIBLE,
            tipo_consumible=self.consumible, bodega_destino=self.bodega,
            cantidad=5, motivo='Compra inicial del trimestre', realizado_por=self.usuario,
        )
        MovimientoInventario.objects.create(
            tipo_movimiento=MovimientoInventario.TipoMovimiento.INGRESO_CONSUMIBLE,
            tipo_consumible=self.consumible, bodega_destino=self.bodega,
            cantidad=2, motivo='Reposicion de emergencia', realizado_por=self.usuario,
        )

        PerfilUsuario.objects.create(usuario=self.usuario, acceso_todas_unidades=True)
        for app, codigo in (
            ('monitoreo', 'view_muestrametrica'),
            ('activos', 'view_movimientoinventario'),
        ):
            self.usuario.user_permissions.add(
                Permission.objects.get(content_type__app_label=app, codename=codigo),
            )
        self.client.force_login(self.usuario)

    def test_monitoreo_busca_y_pagina(self):
        url = reverse('panel:monitoreo_lista')
        resp = self.client.get(url)
        self.assertEqual(len(resp.context['tarjetas']), 2)
        self.assertIsNotNone(resp.context['pagina'], 'tiene que paginar, no traer los ~700')

        resp = self.client.get(url, {'q': 'SRV-TANDA'})
        self.assertEqual(
            [t['estacion'].codigo for t in resp.context['tarjetas']], ['T2A001-S'],
        )

    def test_monitoreo_acota_los_calculos_a_la_pagina(self):
        """La parte valiosa del cambio: al paginar ANTES de calcular, las consultas de
        abajo —ultimas muestras, servicios caidos, series de los graficos— se acotan a la
        pagina. Si alguien moviera el paginado despues del calculo, esto sigue en verde
        pero la pantalla vuelve a trabajar sobre toda la flota; por eso se mide el numero
        de consultas y no solo el resultado."""
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        url = reverse('panel:monitoreo_lista')
        # Buscando UNA estacion, la pantalla no puede costar MAS consultas que sin buscar.

        with CaptureQueriesContext(connection) as todas:
            self.client.get(url)
        with CaptureQueriesContext(connection) as una:
            self.client.get(url, {'q': 'SRV-TANDA'})
        self.assertLessEqual(len(una), len(todas))

    def test_movimientos_busca_por_motivo(self):
        url = reverse('panel:movimientos_inventario_lista')
        resp = self.client.get(url, {'q': 'emergencia'})
        motivos = [m.motivo for m in resp.context['movimientos']]
        self.assertEqual(motivos, ['Reposicion de emergencia'])

    def test_movimientos_busca_por_consumible_y_bodega(self):
        url = reverse('panel:movimientos_inventario_lista')
        self.assertEqual(len(self.client.get(url, {'q': 'Toner'}).context['movimientos']), 2)
        self.assertEqual(len(self.client.get(url, {'q': 'BOD-T2'}).context['movimientos']), 2)

    def test_movimientos_ya_no_corta_en_silencio(self):
        """Antes la vista hacia `movimientos[:500]`: pasadas 500 filas habia movimientos
        que existian y no se podian ver desde ninguna parte, sin aviso. Ahora el total
        esta en el paginador, asi que nada queda escondido."""
        url = reverse('panel:movimientos_inventario_lista')
        resp = self.client.get(url)
        self.assertEqual(resp.context['pagina'].paginator.count, 2)

    def test_el_termino_se_conserva_al_paginar_en_los_tres(self):
        for nombre in ('monitoreo_lista', 'movimientos_inventario_lista'):
            with self.subTest(vista=nombre):
                resp = self.client.get(reverse('panel:' + nombre), {'q': 'algo'})
                self.assertIn('q=algo', resp.context['query_filtros'])


class BuscadorTanda3Tests(TestCase):
    """Los cuatro listados que crecen con el uso (tanda 3).

    Dos reciben solo buscador y dos lo necesitaban por motivos distintos, y la diferencia
    esta razonada en cada vista: el padron de colaboradores y el catalogo de software los
    acota la realidad —las personas de la empresa, lo que se decide vigilar—, mientras que
    las ejecuciones de scripts solo crecen, como auditoria.
    """

    def setUp(self):
        from apps.activos.models import Colaborador
        from apps.scripts.models import EjecucionScript, Script
        from apps.software.models import AplicacionCatalogo

        self.sg = UnidadNegocio.objects.get(codigo='SG')
        self.usuario = User.objects.create_user(username='u_tanda3', password='x')
        PerfilUsuario.objects.create(usuario=self.usuario, acceso_todas_unidades=True)

        Colaborador.objects.create(
            nombre='Maria Lopez', cedula='0912345678', correo='maria@ejemplo.com',
            unidad_negocio=self.sg,
        )
        Colaborador.objects.create(
            nombre='Jorge Vera', cedula='0998765432', correo='jorge@ejemplo.com',
            unidad_negocio=self.sg,
        )

        AplicacionCatalogo.objects.create(
            nombre='Adobe Reader', fabricante='Adobe', categoria='ofimatica',
            version_mas_reciente_conocida='2024.1', creado_por=self.usuario,
        )
        AplicacionCatalogo.objects.create(
            nombre='7-Zip', fabricante='Igor Pavlov', categoria='utilidades',
            version_mas_reciente_conocida='23.01', creado_por=self.usuario,
        )

        script = Script.objects.create(
            nombre='Reiniciar servicio POS', contenido='echo hola', creado_por=self.usuario,
        )
        otro = Script.objects.create(
            nombre='Limpiar temporales', contenido='echo chau', creado_por=self.usuario,
        )
        EjecucionScript.objects.create(
            script=script, destino_tipo='estacion', creado_por=self.usuario, unidad_negocio=self.sg,
        )
        EjecucionScript.objects.create(
            script=otro, destino_tipo='estacion', creado_por=self.usuario, unidad_negocio=self.sg,
        )

        for app, codigo in (
            ('activos', 'view_colaborador'),
            ('software', 'view_aplicacioncatalogo'),
            ('scripts', 'view_ejecucionscript'),
        ):
            self.usuario.user_permissions.add(
                Permission.objects.get(content_type__app_label=app, codename=codigo),
            )
        self.client.force_login(self.usuario)

    def test_colaboradores_busca_por_nombre_y_cedula(self):
        url = reverse('panel:colaboradores_lista')
        self.assertEqual(
            [c.nombre for c in self.client.get(url, {'q': 'Lopez'}).context['colaboradores']],
            ['Maria Lopez'],
        )
        self.assertEqual(
            [c.nombre for c in self.client.get(url, {'q': '0998765432'}).context['colaboradores']],
            ['Jorge Vera'],
        )

    def test_aplicaciones_busca_por_nombre_y_fabricante(self):
        url = reverse('panel:aplicaciones_lista')
        self.assertEqual(
            [a.nombre for a in self.client.get(url, {'q': '7-Zip'}).context['aplicaciones']],
            ['7-Zip'],
        )
        self.assertEqual(
            [a.nombre for a in self.client.get(url, {'q': 'Adobe'}).context['aplicaciones']],
            ['Adobe Reader'],
        )

    def test_software_desactualizado_busca_por_aplicacion(self):
        url = reverse('panel:software_desactualizado_lista')
        resp = self.client.get(url, {'q': 'Adobe'})
        self.assertEqual([f['aplicacion'].nombre for f in resp.context['filas']], ['Adobe Reader'])

    def test_software_desactualizado_avisa_que_el_total_es_de_lo_buscado(self):
        """Con una busqueda puesta el KPI cuenta lo buscado, no la flota. Decirlo es lo que
        evita que alguien lea 3 y crea que son los 3 de toda la red."""
        url = reverse('panel:software_desactualizado_lista')
        self.assertNotContains(self.client.get(url), '(en lo buscado)')
        self.assertContains(self.client.get(url, {'q': 'Adobe'}), '(en lo buscado)')

    def test_ejecuciones_busca_por_script_y_pagina(self):
        url = reverse('panel:ejecuciones_lista')
        resp = self.client.get(url)
        self.assertEqual(resp.context['pagina'].paginator.count, 2)

        resp = self.client.get(url, {'q': 'temporales'})
        self.assertEqual(
            [e.script.nombre for e in resp.context['ejecuciones']], ['Limpiar temporales'],
        )

    def test_el_termino_vuelve_al_formulario_en_los_cuatro(self):
        for nombre in (
            'colaboradores_lista', 'aplicaciones_lista',
            'software_desactualizado_lista', 'ejecuciones_lista',
        ):
            with self.subTest(vista=nombre):
                resp = self.client.get(reverse('panel:' + nombre), {'q': 'xyz'})
                self.assertEqual(resp.context['busqueda'], 'xyz')
                self.assertContains(resp, 'value="xyz"')


class BuscadorTanda4Tests(TestCase):
    """Tanda 4: los listados cortos, caso por caso y no por parejo.

    De los 19 que quedaban, reciben buscador los 12 que CRECEN con el uso. Los que la
    realidad acota —seis bodegas, una decena de reglas de alerta, la cola de cierres en
    conflicto que deberia estar casi vacia— se dejan sin buscador a proposito: un campo que
    nadie usa es una funcionalidad por cantidad, justo lo que esta etapa no queria.

    Y uno que faltaba y no era corto: `alertas_lista`, la lista principal del tecnico. Se
    habia paginado sin darle busqueda, que es cambiar una pared de filas por veinte.
    """

    def setUp(self):
        from apps.mqtt_worker.models import EnrolamientoRechazado

        self.sg = UnidadNegocio.objects.get(codigo='SG')
        self.grupo = Grupo.objects.create(codigo='TRXT4')
        self.farmacia = Farmacia.objects.create(
            codigo='T4A001', nombre='Sucursal Tanda Cuatro', grupo=self.grupo, unidad_negocio=self.sg,
        )
        self.usuario = User.objects.create_user(username='u_tanda4', password='x')
        PerfilUsuario.objects.create(usuario=self.usuario, acceso_todas_unidades=True)

        EnrolamientoRechazado.objects.create(
            codigo_recibido='T4A999-A', hostname='CAJA-FANTASMA',
            motivo='farmacia no encontrada',
        )
        EnrolamientoRechazado.objects.create(
            codigo_recibido='T4A001-B', hostname='CAJA-REAL', motivo='hardware no coincide',
        )

        # Sin try/except a proposito: un permiso mal escrito tiene que reventar aca y no
        # degradarse en un 302 que luego aparece como "context is None" diez lineas mas
        # abajo. Paso al escribir estas pruebas — se concedio
        # `mqtt_worker.view_enrolamientorechazado` cuando la vista exige
        # `catalogo.view_estacion`, y el except lo tapo.
        for app, codigo in (
            ('catalogo', 'view_estacion'),
            ('monitoreo', 'view_alerta'),
        ):
            self.usuario.user_permissions.add(
                Permission.objects.get(content_type__app_label=app, codename=codigo),
            )
        self.client.force_login(self.usuario)

    def test_enrolamientos_rechazados_busca_por_codigo(self):
        """Es la bandeja a la que se llega con un codigo en la mano, dicho por telefono."""
        url = reverse('panel:enrolamientos_rechazados_lista')
        resp = self.client.get(url, {'q': 'T4A999-A', 'todos': '1'})
        self.assertEqual(
            [r.codigo_recibido for r in resp.context['rechazados']], ['T4A999-A'],
        )

    def test_enrolamientos_rechazados_busca_por_motivo(self):
        url = reverse('panel:enrolamientos_rechazados_lista')
        resp = self.client.get(url, {'q': 'hardware', 'todos': '1'})
        self.assertEqual(
            [r.codigo_recibido for r in resp.context['rechazados']], ['T4A001-B'],
        )

    def test_el_contador_de_sin_revisar_no_lo_mueve_la_busqueda(self):
        """Mismo contrato que el resto del panel: los contadores cuentan el conjunto
        completo, los filtros recortan la tabla."""
        url = reverse('panel:enrolamientos_rechazados_lista')
        self.assertEqual(self.client.get(url).context['sin_revisar'], 2)
        self.assertEqual(self.client.get(url, {'q': 'hardware'}).context['sin_revisar'], 2)

    def test_alertas_busca_por_regla_y_por_donde_pasa(self):
        from apps.monitoreo.models import Alerta, Metrica, ReglaAlerta

        estacion = Estacion.objects.create(
            codigo='T4A001-A', farmacia=self.farmacia,
            estado_aprobacion=Estacion.EstadoAprobacion.APROBADA,
        )
        regla_cpu = ReglaAlerta.objects.create(
            nombre='CPU alta', metrica=Metrica.CPU_CARGA_PCT, umbral=90, creado_por=self.usuario,
        )
        regla_ram = ReglaAlerta.objects.create(
            nombre='RAM alta', metrica=Metrica.RAM_USADA_PCT, umbral=90, creado_por=self.usuario,
        )
        Alerta.objects.create(regla=regla_cpu, estacion=estacion, valor_disparador=95)
        Alerta.objects.create(regla=regla_ram, estacion=estacion, valor_disparador=95)

        url = reverse('panel:alertas_lista')
        resp = self.client.get(url, {'q': 'CPU'})
        self.assertEqual([a.regla.nombre for a in resp.context['alertas']], ['CPU alta'])

        # Por farmacia: es como llega el reporte — "me dicen que T4A001 tiene algo".
        resp = self.client.get(url, {'q': 'T4A001'})
        self.assertEqual(resp.context['pagina'].paginator.count, 2)

    def test_los_listados_cortos_siguen_sin_buscador_a_proposito(self):
        """Guarda de la decision, no del codigo: si alguien agrega un buscador a estas,
        esta prueba falla y obliga a justificarlo. En una tabla de seis bodegas un campo de
        busqueda es lugar ocupado que nadie usa.

        No se mira la plantilla sino el CODIGO de la vista: una vista que no lee el termino
        no busca, por mas que la plantilla muestre una caja.
        """
        import ast
        from pathlib import Path

        from django.conf import settings

        sin_buscador = {
            'bodegas_lista': 'son un punado de bodegas',
            'reglas_alerta_lista': 'es configuracion, una decena de reglas',
            'cierres_en_conflicto_lista': 'es una cola que deberia estar casi vacia',
            'scripts_programados_lista': 'son pocas programaciones',
        }
        raiz = Path(settings.BASE_DIR) / 'apps' / 'panel' / 'views'
        encontradas = {}
        for archivo in raiz.glob('*.py'):
            fuente = archivo.read_text(encoding='utf-8')
            lineas = fuente.splitlines()
            for nodo in ast.parse(fuente).body:
                if isinstance(nodo, ast.FunctionDef) and nodo.name in sin_buscador:
                    encontradas[nodo.name] = '\n'.join(lineas[nodo.lineno - 1:nodo.end_lineno])

        self.assertEqual(
            sorted(encontradas), sorted(sin_buscador),
            'alguna de estas vistas se renombro o se movio: hay que revisar la decision',
        )
        for nombre, motivo in sin_buscador.items():
            self.assertNotIn(
                'buscar(', encontradas[nombre],
                '%s recibio un buscador y la decision era NO ponerlo porque %s. '
                'Si cambio, actualizar esta prueba con el motivo nuevo.' % (nombre, motivo),
            )


class TodosLosBuscadoresRespondenTests(TestCase):
    """Prueba de humo: cada listado con buscador responde 200 con `?q=` puesto.

    Existe por un error concreto. En `enrolamientos_rechazados_lista` el buscador quedo
    insertado DESPUES de que la vista materializaba el queryset con `list()` para calcular
    una columna, asi que `buscar` recibia una lista y la pantalla devolvia 500 con
    "'list' object has no attribute 'model'". Ninguna prueba de las otras 21 vistas lo
    habria detectado, porque el error es por vista y no del helper.

    La lista de vistas se DESCUBRE leyendo el codigo, no se escribe a mano: un buscador
    nuevo entra solo a esta prueba. Si se escribiera a mano, la vista numero 23 se agregaria
    sin red, que es exactamente como se cuela este error.

    Es de humo a proposito: no verifica QUE devuelve cada busqueda —eso lo hacen las
    pruebas de cada tanda— sino que ninguna reviente. Es la unica forma barata de cubrir 22
    pantallas contra esta familia de fallo.
    """

    def setUp(self):
        self.usuario = User.objects.create_superuser(
            username='u_humo', password='x', email='humo@ejemplo.com',
        )
        PerfilUsuario.objects.create(usuario=self.usuario, acceso_todas_unidades=True)
        self.client.force_login(self.usuario)

    def _vistas_con_buscador(self):
        """Los nombres de URL de los listados cuya vista llama a `buscar`."""
        import ast
        import re
        from pathlib import Path

        from django.conf import settings

        raiz = Path(settings.BASE_DIR) / 'apps' / 'panel'
        nombres = []
        for archivo in sorted((raiz / 'views').glob('*.py')):
            if archivo.name == '__init__.py':
                continue
            fuente = archivo.read_text(encoding='utf-8')
            lineas = fuente.splitlines()
            for nodo in ast.parse(fuente).body:
                if not isinstance(nodo, ast.FunctionDef):
                    continue
                cuerpo = '\n'.join(lineas[nodo.lineno - 1:nodo.end_lineno])
                if 'buscar(' in cuerpo:
                    nombres.append(nodo.name)

        urls = (raiz / 'urls.py').read_text(encoding='utf-8')
        sin_argumentos = []
        for nombre in nombres:
            # Se saca el `name=` de la ruta y NO se asume que es el de la funcion: en
            # viaticos la vista `zonas_lista` se publica como `viaticos_zonas_lista`, y
            # asumirlo hacia que esta prueba reventara con NoReverseMatch — un falso
            # negativo que se ve igual que la pantalla rota que busca detectar.
            # La vista puede ir envuelta por `diferido('<modulo>', views.x)` (módulos fuera
            # de SaidSoft RMM 1.0, ver apps/panel/modulos.py): se acepta esa forma también,
            # para que esos buscadores se sigan probando y no desaparezcan en silencio.
            m = re.search(
                r"path\(\s*'([^']*)'\s*,\s*(?:diferido\(\s*'[a-z_]+'\s*,\s*)?views\.%s\s*\)?\s*,\s*name='([^']+)'"
                % re.escape(nombre),
                urls,
            )
            # Las rutas con `<pk>` se saltan: necesitan un objeto y no son listados.
            if m and '<' not in m.group(1):
                sin_argumentos.append(m.group(2))
        return sin_argumentos

    def test_hay_buscadores_que_probar(self):
        """Si el descubrimiento se rompe, el bucle de abajo pasaria sin probar NADA —
        una prueba que no prueba nada es peor que no tenerla."""
        self.assertGreaterEqual(len(self._vistas_con_buscador()), 20)

    def test_ninguno_revienta_con_un_termino_puesto(self):
        for nombre in self._vistas_con_buscador():
            with self.subTest(vista=nombre):
                resp = self.client.get(reverse('panel:' + nombre), {'q': 'ML001'})
                self.assertEqual(
                    resp.status_code, 200,
                    '%s devolvio %s buscando. Lo mas probable: `buscar` recibe una lista '
                    'en vez de un queryset porque la vista ya hizo list() antes.'
                    % (nombre, resp.status_code),
                )

    def test_ninguno_revienta_con_el_termino_vacio(self):
        """`?q=` es lo que manda el formulario con el campo en blanco, y tiene que
        significar "todo" — no "nada" ni un error."""
        for nombre in self._vistas_con_buscador():
            with self.subTest(vista=nombre):
                resp = self.client.get(reverse('panel:' + nombre), {'q': ''})
                self.assertEqual(resp.status_code, 200, nombre)

    def test_ninguno_revienta_con_un_termino_raro(self):
        """Lo que se pega desde un chat: espacios, comillas, un porcentaje — que en LIKE es
        un comodin — y acentos."""
        for termino in ('   ', "O'Brien", '100%', 'ñandú', '_', '%%'):
            for nombre in self._vistas_con_buscador():
                with self.subTest(vista=nombre, termino=termino):
                    resp = self.client.get(reverse('panel:' + nombre), {'q': termino})
                    self.assertEqual(resp.status_code, 200, '%s con %r' % (nombre, termino))


class ListadosQueTraianLaTablaEnteraTests(TestCase):
    """A-3 de la auditoria del 5-oct-2026: dos pantallas cuyo costo lo fijaba la TABLA y
    no la pagina.

    Son dos defectos distintos y se prueban distinto a proposito:

      - `/mantenimientos/?orden=fecha` paginaba DESPUES de precargar los acuerdos de SLA,
        y `precargar_acuerdos_sla` termina en `list(...)`: el SQL salia sin `LIMIT` y
        traia la tabla entera a memoria para mostrar 25 filas. Lo que hay que fijar es que
        el `LIMIT` exista, porque el numero de consultas ya era correcto antes — por eso
        aca se mira el SQL y no se cuentan consultas.
      - `/colaboradores/` costaba 3 consultas POR FILA (cargo, departamento via
        `Cargo.__str__` y un COUNT desde la plantilla) y esa vista no pagina, asi que el
        costo lo fijaba el padron entero. Ahi lo que hay que fijar es que el costo sea
        CONSTANTE, y eso se mide contando consultas con dos padrones de tamanio distinto.
    """

    def setUp(self):
        from apps.activos.models import Cargo, Departamento

        self.usuario = User.objects.create_user(username='u_a3', password='x')
        PerfilUsuario.objects.create(usuario=self.usuario, acceso_todas_unidades=True)
        for app, codigo in (
            ('mantenimiento', 'view_mantenimiento'),
            ('activos', 'view_colaborador'),
        ):
            self.usuario.user_permissions.add(
                Permission.objects.get(content_type__app_label=app, codename=codigo),
            )
        self.client.force_login(self.usuario)

        self.departamento = Departamento.objects.create(nombre='Soporte A-3')
        self.cargo = Cargo.objects.create(nombre='Tecnico de campo', departamento=self.departamento)

    def _colaboradores(self, cuantos, prefijo):
        for i in range(cuantos):
            Colaborador.objects.create(
                nombre='%s %02d' % (prefijo, i), cedula='%s%04d' % (prefijo[:2].upper(), i),
                cargo=self.cargo, sucursal='Matriz', zona='Centro',
            )

    def _mantenimientos(self, cuantos, prefijo):
        cliente = Colaborador.objects.create(nombre=prefijo, cedula='CL' + prefijo[:6])
        for i in range(cuantos):
            Mantenimiento.objects.create(
                cliente=cliente, descripcion='%s %03d' % (prefijo, i),
                fecha_programada=timezone.now() - timedelta(hours=i),
            )

    def _consultas(self, url, parametros=None):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        with CaptureQueriesContext(connection) as ctx:
            resp = self.client.get(url, parametros or {})
        self.assertEqual(resp.status_code, 200)
        return resp, [q['sql'] for q in ctx.captured_queries]

    def test_mantenimientos_por_fecha_pide_solo_la_pagina(self):
        """La prueba que importa de la parte 1: que el SELECT de las filas lleve `LIMIT`.

        Se mira el SQL y no el numero de consultas porque el defecto no agregaba
        consultas: la unica consulta de filas traia las 60 en vez de 25. Contar consultas
        habria pasado en verde con el bug puesto.
        """
        self._mantenimientos(60, 'Fecha')
        resp, consultas = self._consultas(reverse('panel:mantenimientos_lista'), {'orden': 'fecha'})

        filas = [
            sql for sql in consultas
            if 'FROM "mantenimiento"' in sql and 'COUNT(' not in sql
        ]
        self.assertTrue(filas, 'no se encontro la consulta que trae los mantenimientos')
        for sql in filas:
            self.assertIn(
                'LIMIT', sql,
                'el listado por fecha volvio a traer la tabla entera: alguien movio el '
                'paginado despues de `precargar_acuerdos_sla`, que materializa con list()',
            )
        self.assertEqual(len(resp.context['mantenimientos']), 25)
        self.assertEqual(resp.context['pagina'].paginator.count, 60)

    def test_mantenimientos_por_fecha_sigue_pintando_el_sla(self):
        """El `LIMIT` no sirve de nada si se gano perdiendo la precarga: la columna de SLA
        se pinta en los dos ordenes, y sin acuerdos precargados vuelve a costar 2
        consultas por fila."""
        self._mantenimientos(3, 'Sla')
        resp, _ = self._consultas(reverse('panel:mantenimientos_lista'), {'orden': 'fecha'})
        for mantenimiento in resp.context['mantenimientos']:
            self.assertTrue(
                hasattr(mantenimiento, '_acuerdos_precargados'),
                'la pagina llego sin los acuerdos precargados',
            )

    def test_los_tres_modos_de_orden_siguen_respondiendo(self):
        """`?orden=fecha`, `?orden=urgencia` y sin parametro. El cambio reordeno las dos
        ramas del `if`, que es justo donde se rompe una sin que se note la otra."""
        self._mantenimientos(30, 'Modo')
        url = reverse('panel:mantenimientos_lista')
        for parametros in ({'orden': 'fecha'}, {'orden': 'urgencia'}, {}):
            with self.subTest(parametros=parametros):
                resp, _ = self._consultas(url, parametros)
                self.assertEqual(len(resp.context['mantenimientos']), 25)
                self.assertEqual(resp.context['pagina'].paginator.count, 30)

    def test_colaboradores_no_crece_con_el_padron(self):
        """Medido antes del arreglo: 22 consultas con 0 filas, 52 con 10 y 142 con 40 —
        3,00 por fila, lineal. Ahora tiene que ser el MISMO numero con 2 y con 20."""
        url = reverse('panel:colaboradores_lista')
        self._colaboradores(2, 'Pocos')
        _, con_dos = self._consultas(url)

        self._colaboradores(18, 'Muchos')
        resp, con_veinte = self._consultas(url)

        self.assertEqual(len(resp.context['colaboradores']), 20)
        self.assertEqual(
            len(con_veinte), len(con_dos),
            'el listado de colaboradores volvio a costar por fila: faltan el '
            "select_related('cargo__departamento') o el annotate de n_activos, o la "
            'plantilla volvio a usar `activos_asignados.count`',
        )

    def test_colaboradores_cuenta_bien_los_activos_asignados(self):
        """El `annotate` reemplaza a un COUNT por fila, asi que tiene que dar lo mismo que
        daba ese COUNT: 2, 0 y 0 — no el total de la tabla ni un numero inflado."""
        self._colaboradores(3, 'Cuenta')
        con_activos = Colaborador.objects.get(nombre='Cuenta 00')
        Activo.objects.create(
            codigo='CR-DSK-A301', tipo=Activo.Tipo.DESKTOP, colaborador_actual=con_activos,
        )
        Activo.objects.create(
            codigo='CR-LAP-A302', tipo=Activo.Tipo.LAPTOP, colaborador_actual=con_activos,
        )
        Activo.objects.create(codigo='CR-DSK-A303', tipo=Activo.Tipo.DESKTOP)

        resp, _ = self._consultas(reverse('panel:colaboradores_lista'))
        cuentas = {c.nombre: c.n_activos for c in resp.context['colaboradores']}
        self.assertEqual(cuentas, {'Cuenta 00': 2, 'Cuenta 01': 0, 'Cuenta 02': 0})

    def test_el_buscador_no_infla_la_cuenta_de_activos(self):
        """El riesgo concreto del `annotate`: si el buscador recorriera una relacion
        inversa, el JOIN multiplicaria filas y el COUNT contaria de mas. Los 7 campos del
        buscador son locales o FK hacia adelante, y esta prueba es lo que lo mantiene asi
        — si alguien agrega `activos_asignados__codigo` al buscador, falla."""
        self._colaboradores(2, 'Busca')
        persona = Colaborador.objects.get(nombre='Busca 00')
        for i in range(3):
            Activo.objects.create(
                codigo='CR-DSK-B30%d' % i, tipo=Activo.Tipo.DESKTOP, colaborador_actual=persona,
            )

        resp, _ = self._consultas(reverse('panel:colaboradores_lista'), {'q': 'Tecnico de campo'})
        cuentas = {c.nombre: c.n_activos for c in resp.context['colaboradores']}
        self.assertEqual(cuentas, {'Busca 00': 3, 'Busca 01': 0})


class AltaDeActivoEnUbicacionTests(TestCase):
    """La vista de alta del panel con un activo que NO está en bodega ni en farmacia.

    El mensaje de éxito resolvía el destino con dos ramas —bodega, si no farmacia— y
    hacía `activo.farmacia.codigo`. Un activo registrado en una `ubicacion` no tiene
    ninguna de las dos, así que eso era un `AttributeError` sobre None justo después de
    guardar: el activo quedaba creado y la respuesta era un 500.
    """

    def setUp(self):
        from apps.activos.models import Ubicacion

        self.sg = UnidadNegocio.objects.get(codigo='SG')
        self.ubicacion = Ubicacion.objects.create(nombre='Matriz Guayaquil')
        self.bodega = Bodega.objects.create(codigo='BOD-PV', nombre='Bodega', unidad_negocio=self.sg)
        self.usuario = User.objects.create_user(username='u_alta_ubic', password='x')
        PerfilUsuario.objects.create(usuario=self.usuario, acceso_todas_unidades=True)
        self.usuario.user_permissions.add(
            Permission.objects.get(content_type__app_label='activos', codename='add_activo'),
            Permission.objects.get(content_type__app_label='activos', codename='view_activo'),
        )
        self.client.force_login(self.usuario)

    def _alta(self, **extra):
        datos = {'tipo': Activo.Tipo.IMPRESORA, 'modelo': 'MP C3004'}
        datos.update(extra)
        return self.client.post(reverse('panel:activo_crear'), datos, follow=True)

    def test_dar_de_alta_en_una_ubicacion_no_revienta_y_nombra_el_destino(self):
        resp = self._alta(ubicacion=self.ubicacion.pk)
        self.assertEqual(resp.status_code, 200)
        activo = Activo.objects.get(modelo='MP C3004')
        self.assertEqual(activo.ubicacion, self.ubicacion)
        self.assertEqual(activo.estado, Activo.Estado.ASIGNADO)
        self.assertContains(resp, 'Matriz Guayaquil')

    def test_la_ficha_muestra_la_ubicacion_en_vez_de_decir_que_no_la_tiene(self):
        """Antes la ficha afirmaba "Administrativo / oficina (sin farmacia asignada)"
        incluso con una ubicación cargada: decía que no se sabía dónde está el equipo
        cuando sí se sabía."""
        self._alta(ubicacion=self.ubicacion.pk, observaciones='La trajo contabilidad.')
        activo = Activo.objects.get(modelo='MP C3004')
        resp = self.client.get(reverse('panel:activo_detalle', args=[activo.pk]))
        self.assertContains(resp, 'Matriz Guayaquil')
        self.assertNotContains(resp, 'sin ubicación ni farmacia asignada')
        self.assertContains(resp, 'La trajo contabilidad.')

    def test_el_alta_en_bodega_sigue_nombrando_la_bodega(self):
        resp = self._alta(bodega=self.bodega.pk)
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'BOD-PV')
        self.assertEqual(Activo.objects.get(modelo='MP C3004').estado, Activo.Estado.EN_BODEGA)


class AntiguedadDeAlertasTests(TestCase):
    """El histograma de antigüedad de la Zona 2b del tablero.

    Lo que fija: que las cuatro cubetas no se solapen ni dejen hueco, que una reconocida
    no se cuente como sin reconocer —son dos trabajos distintos y el tablero ya se
    equivocó una vez sumándolas (hallazgo D-03)—, que una resuelta no aparezca, que el
    alcance por unidad de negocio se respete, y que el bloque cueste UNA consulta.
    """

    def setUp(self):
        self.sg = UnidadNegocio.objects.get(codigo='SG')
        self.mia = UnidadNegocio.objects.get(codigo='MIA')
        grupo = Grupo.objects.create(codigo='TRXANT')
        self.farmacia_sg = Farmacia.objects.create(codigo='ML950', grupo=grupo, unidad_negocio=self.sg)
        self.farmacia_mia = Farmacia.objects.create(codigo='MM950', grupo=grupo, unidad_negocio=self.mia)
        self.estacion_sg = Estacion.objects.create(
            codigo='ML950-A', farmacia=self.farmacia_sg,
            estado_aprobacion=Estacion.EstadoAprobacion.APROBADA,
        )
        self.estacion_mia = Estacion.objects.create(
            codigo='MM950-A', farmacia=self.farmacia_mia,
            estado_aprobacion=Estacion.EstadoAprobacion.APROBADA,
        )

        self.usuario = User.objects.create_user(username='u_antiguedad', password='x')
        PerfilUsuario.objects.create(usuario=self.usuario).unidades_negocio.add(self.sg)
        self.regla = ReglaAlerta.objects.create(
            nombre='CPU alta antiguedad', metrica=Metrica.CPU_CARGA_PCT, umbral=90,
            creado_por=self.usuario,
        )
        self.client.force_login(self.usuario)

    def _alerta(self, *, hace, estado=Alerta.Estado.ABIERTA, estacion=None):
        """`abierta_en` es auto_now_add, así que la fecha se fija con un UPDATE posterior
        — el mismo camino que ya usan las otras pruebas de alertas del archivo."""
        # Una regla por alerta: la base ya no admite dos alertas activas de la misma regla
        # en la misma estación (CR-2), y el histograma no distingue reglas.
        self._reglas_creadas = getattr(self, '_reglas_creadas', 0) + 1
        regla = ReglaAlerta.objects.create(
            nombre=f'{self.regla.nombre} {self._reglas_creadas}', metrica=self.regla.metrica,
            umbral=self.regla.umbral, creado_por=self.usuario,
        )
        alerta = Alerta.objects.create(
            regla=regla, estacion=estacion or self.estacion_sg,
            valor_disparador=95, estado=estado,
        )
        Alerta.objects.filter(pk=alerta.pk).update(abierta_en=timezone.now() - hace)
        return alerta

    def _cubetas(self, unidades=None):
        from apps.monitoreo.services import antiguedad_alertas_sin_resolver

        if unidades is None:
            unidades = [self.sg]
        return antiguedad_alertas_sin_resolver(unidades)

    def test_una_alerta_en_cada_cubeta_da_uno_en_cada_cubeta(self):
        """Los cuatro cortes son intervalos semiabiertos: ninguna alerta cae en dos
        cubetas ni se queda fuera de las cuatro."""
        self._alerta(hace=timedelta(minutes=10))
        self._alerta(hace=timedelta(hours=5))
        self._alerta(hace=timedelta(days=3))
        self._alerta(hace=timedelta(days=8))

        datos = self._cubetas()
        self.assertEqual([c['total'] for c in datos['cubetas']], [1, 1, 1, 1])
        self.assertEqual(datos['total'], 4)
        self.assertEqual(
            [c['etiqueta'] for c in datos['cubetas']],
            ['menos de 1 h', '1 – 24 h', '1 – 7 días', 'más de 7 días'],
        )

    def test_los_bordes_exactos_caen_en_la_cubeta_mas_vieja(self):
        """Una alerta de poco más de 1 h no es "menos de 1 h". El borde se prueba porque
        es donde un `__gt` por un `__gte` cambia el número sin que nada falle."""
        self._alerta(hace=timedelta(hours=1, seconds=5))
        datos = self._cubetas()
        self.assertEqual([c['total'] for c in datos['cubetas']], [0, 1, 0, 0])

    def test_una_reconocida_no_cuenta_como_sin_reconocer(self):
        """Sigue sin resolver y sigue envejeciendo, pero tiene a alguien encima: entra en
        la cubeta y en el total, en su propia columna."""
        self._alerta(hace=timedelta(hours=5), estado=Alerta.Estado.RECONOCIDA)
        datos = self._cubetas()
        cubeta = datos['cubetas'][1]
        self.assertEqual(cubeta['total'], 1)
        self.assertEqual(cubeta['en_atencion'], 1)
        self.assertEqual(cubeta['sin_reconocer'], 0)
        self.assertEqual(datos['sin_reconocer'], 0)
        self.assertEqual(datos['en_atencion'], 1)

    def test_una_resuelta_no_aparece_en_ninguna_cubeta(self):
        self._alerta(hace=timedelta(hours=5), estado=Alerta.Estado.RESUELTA)
        datos = self._cubetas()
        self.assertEqual([c['total'] for c in datos['cubetas']], [0, 0, 0, 0])
        self.assertEqual(datos['total'], 0)
        self.assertIsNone(datos['mas_vieja'])

    def test_no_cuenta_las_alertas_de_otra_unidad_de_negocio(self):
        self._alerta(hace=timedelta(hours=5), estacion=self.estacion_mia)
        self.assertEqual(self._cubetas()['total'], 0)
        # Sin acotar, la misma alerta sí se ve: lo que filtra es el alcance, no un
        # recorte escondido en la consulta. Se llama a la función directamente porque
        # `_cubetas()` sin argumentos ya significa "acotado a SG".
        from apps.monitoreo.services import antiguedad_alertas_sin_resolver

        self.assertEqual(antiguedad_alertas_sin_resolver(None)['total'], 1)

    def test_el_tablero_no_filtra_alertas_de_otro_cliente(self):
        """La misma comprobación, pero por HTTP y con el alcance real del usuario."""
        self._alerta(hace=timedelta(hours=5), estacion=self.estacion_mia)
        self._alerta(hace=timedelta(days=8))
        resp = self.client.get(reverse('panel:dashboard'))
        self.assertEqual(resp.status_code, 200)
        datos = resp.context['antiguedad_alertas']
        self.assertEqual(datos['total'], 1)
        self.assertEqual([c['total'] for c in datos['cubetas']], [0, 0, 0, 1])

    def test_sin_alertas_el_bloque_dice_que_no_hay_ninguna(self):
        """Estado vacío explícito y no cuatro barras en cero: una barra vacía se lee como
        "hay datos y están bajos"."""
        resp = self.client.get(reverse('panel:dashboard'))
        self.assertEqual(resp.context['antiguedad_alertas']['total'], 0)
        self.assertContains(resp, 'Ninguna alerta sin resolver')

    def test_las_barras_se_escalan_contra_la_cubeta_mas_poblada(self):
        """Lo que la barra compara son las cubetas entre sí. Y el porcentaje se calcula en
        el dominio porque las plantillas de Django no dividen."""
        self._alerta(hace=timedelta(days=8))
        self._alerta(hace=timedelta(days=9))
        self._alerta(hace=timedelta(hours=5))
        datos = self._cubetas()
        self.assertEqual(datos['cubetas'][3]['pct'], 100)
        self.assertEqual(datos['cubetas'][1]['pct'], 50)
        self.assertEqual(datos['cubetas'][0]['pct'], 0)

    def test_sin_alertas_ninguna_barra_se_dibuja_llena(self):
        """Con el máximo en cero, dividir habría explotado o —peor— dejado el 100%."""
        self.assertEqual([c['pct'] for c in self._cubetas()['cubetas']], [0, 0, 0, 0])

    def test_informa_cuanto_lleva_abierta_la_mas_vieja(self):
        vieja = self._alerta(hace=timedelta(days=14))
        self._alerta(hace=timedelta(minutes=10))
        self.assertEqual(
            self._cubetas()['mas_vieja'].replace(microsecond=0),
            Alerta.objects.get(pk=vieja.pk).abierta_en.replace(microsecond=0),
        )

    def test_el_calculo_cuesta_una_sola_consulta(self):
        """Ocho conteos condicionales y el mínimo, en un `aggregate`. El presupuesto que
        la auditoría fijó para este bloque era exactamente una consulta."""
        self._alerta(hace=timedelta(hours=5))
        self._alerta(hace=timedelta(days=8))
        with self.assertNumQueries(1):
            self._cubetas()


class LineaTiempoEnlacesCaidosTests(TestCase):
    """La línea de tiempo de la Zona 2c del tablero.

    Lo que fija: las tres exclusiones canónicas —las mismas que `resumen_operacion`, el
    Centro y `cola_incidencias`, para que los cuatro no discrepen—, que una caída ya
    recuperada no aparezca, que la ventana de seis horas sea la ESCALA y no un filtro
    (una crónica se dibuja recortada, no se esconde), que una farmacia no ocupe dos
    barras, y que el total del bloque sea literalmente el del KPI.
    """

    def setUp(self):
        self.sg = UnidadNegocio.objects.get(codigo='SG')
        self.mia = UnidadNegocio.objects.get(codigo='MIA')
        grupo = Grupo.objects.create(codigo='TRXLT')
        self.farmacia = Farmacia.objects.create(
            codigo='LT001', grupo=grupo, unidad_negocio=self.sg, ip_router='10.95.0.1',
            tipo_enlace='TELCONET',
        )
        self.otra = Farmacia.objects.create(
            codigo='LT002', grupo=grupo, unidad_negocio=self.sg, ip_router='10.95.0.2',
            tipo_enlace='PUNTO NET',
        )
        self.de_otro_cliente = Farmacia.objects.create(
            codigo='LT003', grupo=grupo, unidad_negocio=self.mia, ip_router='10.95.0.3',
        )
        self.inactiva = Farmacia.objects.create(
            codigo='LT004', grupo=grupo, unidad_negocio=self.sg, ip_router='10.95.0.4',
            activa=False,
        )
        self.usuario = User.objects.create_user(username='u_lt', password='x')
        PerfilUsuario.objects.create(usuario=self.usuario).unidades_negocio.add(self.sg)
        self.client.force_login(self.usuario)

    def _caer(self, farmacia, *, hace, respondio_alguna_vez=True, fin=None):
        from apps.monitoreo.models import EstadoEnlaceFarmacia, EventoEnlaceFarmacia

        EstadoEnlaceFarmacia.objects.update_or_create(
            farmacia=farmacia,
            defaults={'alcanzable': False, 'respondio_alguna_vez': respondio_alguna_vez},
        )
        return EventoEnlaceFarmacia.objects.create(
            farmacia=farmacia, inicio=timezone.now() - hace, fin=fin,
        )

    def _barras(self, unidades=None):
        from apps.monitoreo.enlaces import linea_tiempo_caidas_en_curso

        if unidades is None:
            unidades = [self.sg]
        return linea_tiempo_caidas_en_curso(unidades)

    def _codigos(self, **kwargs):
        return [b['farmacia'].codigo for b in self._barras(**kwargs)]

    def test_una_caida_en_curso_dentro_de_la_ventana_aparece(self):
        self._caer(self.farmacia, hace=timedelta(hours=2, minutes=17))
        barras = self._barras()
        self.assertEqual(len(barras), 1)
        self.assertEqual(barras[0]['farmacia'].codigo, 'LT001')
        self.assertEqual(barras[0]['minutos'], 137)
        self.assertFalse(barras[0]['recortada'])
        # 137 de 360 minutos de escala.
        self.assertEqual(barras[0]['pct'], 38.1)
        self.assertEqual(barras[0]['proveedor'], 'TELCONET')

    def test_una_caida_mas_vieja_que_la_ventana_se_dibuja_recortada_y_no_se_esconde(self):
        """La decisión de fondo del bloque: las seis horas son la escala, no un filtro.
        Filtrar por `inicio >= ahora - 6 h` habría escondido las crónicas, que son las
        peores, y habría hecho que el bloque y el KPI contaran poblaciones distintas."""
        self._caer(self.farmacia, hace=timedelta(days=14))
        barras = self._barras()
        self.assertEqual(len(barras), 1)
        self.assertTrue(barras[0]['recortada'])
        self.assertEqual(barras[0]['pct'], 100)
        # La duración real se conserva: la barra se agota, el dato no se recorta.
        self.assertGreater(barras[0]['minutos'], 6 * 60)

    def test_una_caida_ya_recuperada_no_aparece(self):
        """`fin` no nulo = el enlace volvió. Su historia está en /enlaces/, no acá."""
        self._caer(self.farmacia, hace=timedelta(hours=3), fin=timezone.now() - timedelta(hours=1))
        self.assertEqual(self._barras(), [])

    def test_una_farmacia_dada_de_baja_queda_excluida(self):
        """Deja de sondearse y su estado queda congelado: no es una caída que alguien vaya
        a ir a arreglar (reportado con GP063 el 2-oct-2026)."""
        self._caer(self.inactiva, hace=timedelta(hours=1))
        self.assertEqual(self._barras(), [])

    def test_una_farmacia_que_nunca_respondio_queda_excluida(self):
        """Nunca respondió es configuración pendiente, no una caída."""
        self._caer(self.farmacia, hace=timedelta(hours=1), respondio_alguna_vez=False)
        self.assertEqual(self._barras(), [])

    def test_no_muestra_las_caidas_de_otra_unidad_de_negocio(self):
        self._caer(self.de_otro_cliente, hace=timedelta(hours=1))
        self.assertEqual(self._barras(), [])
        # Sin acotar sí se ve: lo que filtra es el alcance, no un recorte escondido.
        from apps.monitoreo.enlaces import linea_tiempo_caidas_en_curso

        self.assertEqual(len(linea_tiempo_caidas_en_curso(None)), 1)

    def test_el_tablero_respeta_el_alcance_del_usuario(self):
        """La misma comprobación por HTTP, con el alcance real de la sesión."""
        self._caer(self.de_otro_cliente, hace=timedelta(hours=1))
        self._caer(self.farmacia, hace=timedelta(hours=2))
        resp = self.client.get(reverse('panel:dashboard'))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual([b['farmacia'].codigo for b in resp.context['caidas_en_curso']], ['LT001'])

    def test_el_total_del_bloque_es_el_mismo_numero_que_el_kpi(self):
        """No son dos conteos: la plantilla muestra `total_enlaces_caidos`, el del KPI."""
        self._caer(self.farmacia, hace=timedelta(hours=1))
        self._caer(self.otra, hace=timedelta(hours=4))
        resp = self.client.get(reverse('panel:dashboard'))
        self.assertEqual(resp.context['total_enlaces_caidos'], 2)
        self.assertEqual(len(resp.context['caidas_en_curso']), 2)

    def test_dos_eventos_abiertos_del_mismo_sitio_son_una_sola_barra(self):
        """Nada lo impide hoy —no hay constraint, hallazgo D-04— y el KPI cuenta farmacias
        distintas, no eventos. Se queda el inicio MÁS ANTIGUO: con el más reciente la barra
        diría que lleva menos tiempo caída de lo que lleva."""
        self._caer(self.farmacia, hace=timedelta(hours=5))
        self._caer(self.farmacia, hace=timedelta(hours=1))
        barras = self._barras()
        self.assertEqual(len(barras), 1)
        self.assertEqual(barras[0]['minutos'], 300)

    def test_lo_mas_reciente_va_arriba(self):
        """En una ventana de seis horas la pregunta es qué se acaba de caer; lo crónico ya
        está contado en el total y priorizado en la Zona 1."""
        self._caer(self.farmacia, hace=timedelta(hours=5))
        self._caer(self.otra, hace=timedelta(minutes=20))
        self.assertEqual(self._codigos(), ['LT002', 'LT001'])

    def test_una_caida_de_un_minuto_se_sigue_viendo(self):
        """1 de 360 minutos es 0,3% de ancho: sin un mínimo, la barra no se dibujaría y el
        sitio parecería no tener problema."""
        self._caer(self.farmacia, hace=timedelta(minutes=1))
        self.assertEqual(self._barras()[0]['pct'], 1.5)

    def test_sin_caidas_el_bloque_dice_que_no_hay_ninguna(self):
        resp = self.client.get(reverse('panel:dashboard'))
        self.assertEqual(resp.context['caidas_en_curso'], [])
        self.assertContains(resp, 'Ninguna farmacia sin enlace ahora mismo')

    def test_el_bloque_cuesta_una_sola_consulta(self):
        self._caer(self.farmacia, hace=timedelta(hours=1))
        self._caer(self.otra, hace=timedelta(hours=2))
        with self.assertNumQueries(1):
            self._barras()

    def test_no_crece_con_el_numero_de_caidas(self):
        """Una consulta acotada, no una por farmacia: el día que se caiga un proveedor
        entero son cientos de eventos abiertos y esta pantalla se refresca cada minuto."""
        from apps.monitoreo.enlaces import MAX_BARRAS_LINEA_TIEMPO

        grupo = Grupo.objects.get(codigo='TRXLT')
        for i in range(MAX_BARRAS_LINEA_TIEMPO + 4):
            farmacia = Farmacia.objects.create(
                codigo='LTX%03d' % i, grupo=grupo, unidad_negocio=self.sg,
                ip_router='10.96.0.%d' % (i + 1), tipo_enlace='TELCONET',
            )
            self._caer(farmacia, hace=timedelta(minutes=10 + i))
        with self.assertNumQueries(1):
            barras = self._barras()
        self.assertEqual(len(barras), MAX_BARRAS_LINEA_TIEMPO)


class HorarioDeFarmaciaTests(TestCase):
    """Leer `Farmacia.horario`, que es texto libre, para saber si un local debería estar
    abierto.

    Reemplaza a `HorarioDeOperacionEnElTableroTests`, que probaba una franja horaria
    GLOBAL de 07:00–22:00. Esa franja miente para unas 50 farmacias y por eso se retiró:
    8 abren 24 horas —y la franja las declaraba cerradas toda la noche— y 42 cierran
    después de las 22:00, así que a las 22:30 las declaraba cerradas mientras vendían.

    **Todos los textos de estas pruebas son valores reales**, tomados de los 364 distintos
    que hay cargados en producción. Dos de ellos son las trampas que el primer parseo que
    escribí interpretó mal, y están acá para que no vuelvan.
    """

    # (texto real, apertura esperada, cierre esperado)
    CASOS_CON_RANGO = [
        ('07H00 a 22H00', 7.0, 22.0),
        ('07H00 a 21H00', 7.0, 21.0),
        ('07H00 a 23H30', 7.0, 23.5),
        ('06H30 a 23H00', 6.5, 23.0),
        # Trampa 1: un patrón ingenuo de hora+am/pm toma los MINUTOS ("00 AM") como hora y
        # devuelve las 00:00 de apertura. Son ~11 farmacias con este formato.
        ('07:00 AM - 20:00 PM', 7.0, 20.0),
        ('07:00 am A 23:00 pm', 7.0, 23.0),
        ('07:00 am A 22:00 pm', 7.0, 22.0),
        # Trampa 2: el "1:" de "DOMINGO  1:" parece la hora 01:08.
        ('LUNES A VIERNES: 07:00 - 21:00 / SABADO: 08:00:00 - 21:00 / DOMINGO  1: 08:00 - 13:00 '
         '/ DOMINGO  2: - - -', 7.0, 21.0),
        ('LUNES A VIERNES: 07:30 - 22:00 / SABADO: 08:00:00 - 21:00 / DOMINGO  1: 08:00 - 21:00 '
         '/ DOMINGO  2: - - -', 7.5, 22.0),
        # Sin espacio entre el primer rango y el siguiente.
        ('07H30 a 21H30Sab y Dom: 07H30 a 21H00', 7.5, 21.5),
        ('LUNES A VIERNES 06H30 a 22H30 SAB Y DOM 06H30 a 22H00', 6.5, 22.5),
        # Prosa con am/pm sueltos.
        ('Lunes a viernes de 7 am a 8pm / Sábado de 7am a 8pm / Domingo de 8 am a 5pm', 7.0, 20.0),
        ('Lunes a Sabados de 7am a 9pm / Domingo de 8 am a 8pm', 7.0, 21.0),
        # "a 24H00" es cerrar a medianoche, NO abrir 24 horas. Son cinco farmacias.
        ('06H00 a 24H00', 6.0, 24.0),
        ('07H00 a 24H00', 7.0, 24.0),
        ('07H00 a 23H00(LUN-DOM)', 7.0, 23.0),
        ('TODOS LOS DIAS: 07H00 a 21H00', 7.0, 21.0),
        # La hora pegada al nombre del día. Tres farmacias lo tienen así, y con la limpieza
        # de 'DOMINGO  1:' sin afinar el '7:' se perdía y el horario quedaba sin determinar.
        ('LUNES A DOMINGO 7:00 A 21:00', 7.0, 21.0),
        ('LUNES A SABADO 7:00 A 21:00 Y DOMINGO DE 08:00 A 20:00', 7.0, 21.0),
    ]

    SIEMPRE_ABIERTAS = [
        '24 HORAS',
        'LUNES A VIERNES: 24 HORAS - 24 HORAS / SABADO: 24 HORAS - 24 HORAS / DOMINGO  1: 24 HORAS',
    ]

    NO_DETERMINABLES = [
        '',
        '   ',
        # Una sola hora reconocible: el "7" no lleva marca, así que solo se lee "9 pm". Con
        # eso la apertura saldría a las 21:00, peor que no saber.
        'De lunes a domingo horario 7 a 9 pm',
        'horario corrido',
    ]

    def test_extrae_la_envolvente_de_cada_formato_real(self):
        from apps.catalogo.horarios import leer_horario

        for texto, apertura, cierre in self.CASOS_CON_RANGO:
            h = leer_horario(texto)
            self.assertTrue(h.determinado, 'no se pudo leer: %r' % texto)
            self.assertFalse(h.es_24h, '%r no es de 24 horas' % texto)
            self.assertEqual(
                (h.apertura, h.cierre), (apertura, cierre),
                'mal leído %r -> %s' % (texto, (h.apertura, h.cierre)),
            )

    def test_reconoce_las_que_abren_siempre(self):
        from apps.catalogo.horarios import leer_horario

        for texto in self.SIEMPRE_ABIERTAS:
            h = leer_horario(texto)
            self.assertTrue(h.es_24h, 'debería ser de 24 horas: %r' % texto)
            self.assertTrue(h.abierta_a(3.0), 'una de 24 horas está abierta a las 03:00')
            self.assertTrue(h.abierta_a(23.5))

    def test_un_horario_ilegible_no_sirve_de_excusa(self):
        """La regla de seguridad del módulo: ante la duda, el local cuenta como ABIERTO, así
        que la estación callada queda a la vista en vez de quedar explicada por un dato
        malo."""
        from apps.catalogo.horarios import leer_horario

        for texto in self.NO_DETERMINABLES:
            h = leer_horario(texto)
            self.assertFalse(h.determinado, 'no debería poder leerse: %r' % texto)
            self.assertTrue(h.abierta_a(3.0), 'sin dato, no se excusa a nadie: %r' % texto)

    def test_los_bordes_del_rango(self):
        """Intervalo semiabierto, igual que las cubetas de antigüedad: a la hora de cierre
        ya está cerrado."""
        from apps.catalogo.horarios import leer_horario

        h = leer_horario('07H00 a 22H00')
        self.assertFalse(h.abierta_a(6.99))
        self.assertTrue(h.abierta_a(7.0))
        self.assertTrue(h.abierta_a(21.99))
        self.assertFalse(h.abierta_a(22.0))

    def test_una_que_cierra_a_las_2330_sigue_abierta_a_las_2230(self):
        """El caso que la franja global de 07:00–22:00 declaraba cerrado mientras vendía.
        Son 42 farmacias las que cierran después de las 22:00."""
        from apps.catalogo.horarios import leer_horario

        self.assertTrue(leer_horario('07H00 a 23H30').abierta_a(22.5))
        self.assertTrue(leer_horario('06H30 a 23H00').abierta_a(22.5))


class CalladasSegunElHorarioDeSuFarmaciaTests(TestCase):
    """El reparto de las estaciones calladas según si su farmacia debería estar operando.

    Es la diferencia entre "hay 37 estaciones calladas" y "hay 7 que deberían estar
    vendiendo y no responden". Medido el 12-oct-2026 a las 23:29: 37 calladas de 66 y
    ninguna era una avería.
    """

    def setUp(self):
        from datetime import datetime, timezone as dt_timezone

        self.sg = UnidadNegocio.objects.get(codigo='SG')
        grupo = Grupo.objects.create(codigo='TRXHOR')
        viejo = timezone.now() - timedelta(hours=4)

        # Tres farmacias con horarios distintos y una estación callada en cada una.
        self.corta = Farmacia.objects.create(
            codigo='HR001', grupo=grupo, unidad_negocio=self.sg, horario='07H00 a 21H00')
        self.tardia = Farmacia.objects.create(
            codigo='HR002', grupo=grupo, unidad_negocio=self.sg, horario='07H00 a 23H30')
        self.siempre = Farmacia.objects.create(
            codigo='HR003', grupo=grupo, unidad_negocio=self.sg, horario='24 HORAS')
        self.sin_horario = Farmacia.objects.create(
            codigo='HR004', grupo=grupo, unidad_negocio=self.sg, horario='')

        for codigo, farmacia in (('HR001-A', self.corta), ('HR002-A', self.tardia),
                                 ('HR003-A', self.siempre), ('HR004-A', self.sin_horario)):
            Estacion.objects.create(
                codigo=codigo, farmacia=farmacia,
                estado_aprobacion=Estacion.EstadoAprobacion.APROBADA,
                ultimo_heartbeat=viejo,
            )

        # Un instante fijo: 22:00 en Ecuador (03:00 UTC del día siguiente).
        self.a_las_22 = datetime(2026, 10, 14, 3, 0, tzinfo=dt_timezone.utc)
        # Y otro a las 10:00 de la mañana.
        self.a_las_10 = datetime(2026, 10, 13, 15, 0, tzinfo=dt_timezone.utc)

        self.usuario = User.objects.create_user(username='u_horario', password='x')
        PerfilUsuario.objects.create(usuario=self.usuario, acceso_todas_unidades=True)
        self.client.force_login(self.usuario)

    def _reparto(self, instante):
        from apps.catalogo.horarios import separar_calladas_por_horario

        calladas = list(
            Estacion.objects.filter(codigo__startswith='HR').select_related('farmacia')
        )
        return separar_calladas_por_horario(calladas, instante)

    def test_a_las_22_solo_quedan_la_tardia_la_de_24h_y_la_sin_horario(self):
        """La que cierra a las 21:00 está cerrada: su estación callada es esperable. Las
        otras tres deberían estar operando — la de 24 horas siempre, y la que no tiene
        horario cargado porque un dato ausente no excusa a nadie."""
        reparto = self._reparto(self.a_las_22)
        self.assertEqual(
            sorted(e.codigo for e in reparto['dentro']), ['HR002-A', 'HR003-A', 'HR004-A'])
        self.assertEqual(reparto['total_dentro'], 3)
        self.assertEqual(reparto['total_fuera'], 1)

    def test_a_las_10_de_la_manana_todas_deberian_estar_operando(self):
        reparto = self._reparto(self.a_las_10)
        self.assertEqual(reparto['total_dentro'], 4)
        self.assertEqual(reparto['total_fuera'], 0)

    def test_la_de_24_horas_nunca_se_excusa(self):
        """El caso que la franja global trataba peor: las declaraba cerradas toda la noche.
        Son 8 farmacias."""
        from datetime import datetime, timezone as dt_timezone

        a_las_3 = datetime(2026, 10, 13, 8, 0, tzinfo=dt_timezone.utc)  # 03:00 en Ecuador
        dentro = [e.codigo for e in self._reparto(a_las_3)['dentro']]
        self.assertIn('HR003-A', dentro)

    def test_el_reparto_no_consulta_nada(self):
        """Recibe la lista ya materializada y el horario viaja en `farmacia`, que vino con
        select_related: el costo es del llamador y se ve en su presupuesto."""
        from apps.catalogo.horarios import separar_calladas_por_horario

        calladas = list(
            Estacion.objects.filter(codigo__startswith='HR').select_related('farmacia')
        )
        with self.assertNumQueries(0):
            separar_calladas_por_horario(calladas, self.a_las_22)

    def test_el_tablero_desglosa_sin_cambiar_el_total(self):
        """Lo que importa: el número grande sigue siendo el mismo conteo de siempre, y lo
        que se agrega es el desglose."""
        resp = self.client.get(reverse('panel:dashboard'))
        self.assertEqual(resp.status_code, 200)
        total = resp.context['total_sin_reportar']
        self.assertEqual(
            total,
            resp.context['calladas_dentro_de_horario'] + resp.context['calladas_fuera_de_horario'],
            'el desglose tiene que sumar exactamente el total del KPI',
        )
        self.assertEqual(total, 4)

    def test_el_tablero_muestra_primero_las_accionables(self):
        """La lista corta de la Zona 5 prioriza las estaciones cuya farmacia debería estar
        abierta: son las únicas sobre las que alguien actúa ahora."""
        resp = self.client.get(reverse('panel:dashboard'))
        mostradas = [e.codigo for e in resp.context['sin_reportar']]
        dentro = resp.context['calladas_dentro_de_horario']
        if dentro:
            self.assertEqual(len(mostradas), min(dentro, 6))

    def test_estado_flota_ya_no_informa_una_franja_global(self):
        """La franja de 07:00–22:00 se retiró porque miente para unas 50 farmacias. Esta
        prueba existe para que no vuelva por costumbre."""
        from apps.catalogo.services import estado_flota

        flota = estado_flota(None)
        self.assertNotIn('fuera_de_horario', flota)
        self.assertNotIn('horario_operacion', flota)

    def test_estado_flota_sigue_costando_una_consulta(self):
        from apps.catalogo.services import estado_flota

        with self.assertNumQueries(1):
            estado_flota(None)


class SerieDisponibilidad24hTests(TestCase):
    """La serie de 24 h de la Zona 2d, reconstruida desde las transiciones del latido.

    Lo que fija: que el último punto coincida con el KPI —que era el riesgo señalado como
    mayor de esta fase y resultó teórico, pero una prueba es más barata que volver a
    medirlo—, que el estado anterior a la primera transición se infiera bien, que solo
    cuente la fuente MQTT, que use el mismo recorte que `estado_flota`, y que el costo no
    crezca con la cantidad de puntos.
    """

    def setUp(self):
        from datetime import datetime, timezone as dt_timezone

        self.sg = UnidadNegocio.objects.get(codigo='SG')
        self.mia = UnidadNegocio.objects.get(codigo='MIA')
        grupo = Grupo.objects.create(codigo='TRXSER')
        self.farmacia = Farmacia.objects.create(
            codigo='SR001', grupo=grupo, unidad_negocio=self.sg, horario='07H00 a 21H00')
        self.otra_unidad = Farmacia.objects.create(
            codigo='SR002', grupo=grupo, unidad_negocio=self.mia, horario='07H00 a 21H00')

        # Un instante fijo y en punto, para que las cuentas por hora sean exactas.
        self.ahora = datetime(2026, 10, 14, 18, 0, tzinfo=dt_timezone.utc)

        self.a = Estacion.objects.create(
            codigo='SR001-A', farmacia=self.farmacia,
            estado_aprobacion=Estacion.EstadoAprobacion.APROBADA)
        self.b = Estacion.objects.create(
            codigo='SR001-B', farmacia=self.farmacia,
            estado_aprobacion=Estacion.EstadoAprobacion.APROBADA)

        self.usuario = User.objects.create_user(username='u_serie', password='x')
        PerfilUsuario.objects.create(usuario=self.usuario, acceso_todas_unidades=True)
        self.client.force_login(self.usuario)

    def _estado(self, estacion, en_linea, fuente=None):
        from apps.monitoreo.models import EstadoDispositivo

        EstadoDispositivo.objects.update_or_create(
            estacion=estacion, fuente=fuente or EstadoDispositivo.Fuente.MQTT,
            defaults={'en_linea': en_linea},
        )

    def _transicion(self, estacion, en_linea, hace_horas, fuente=None):
        """El `timestamp` se pasa al CREAR, no se corrige después con un `update`.

        `evento_monitoreo` es un hypertable y mover una fila a otro chunk viola el check
        constraint del chunk que la contiene (`_hyper_N_M_chunk`) — un `create` + `update`
        revienta en cuanto las dos fechas caen en semanas distintas, que es como esta
        prueba empezó a fallar sola al cambiar el día.

        El modelo ya lo anticipa: `timestamp` usa `default=` y no `auto_now_add`
        justamente para poder fijarlo al crear (ver el comentario en `MuestraMetrica`).
        """
        from apps.monitoreo.models import EstadoDispositivo, EventoMonitoreo

        return EventoMonitoreo.objects.create(
            estacion=estacion, fuente=fuente or EstadoDispositivo.Fuente.MQTT,
            en_linea=en_linea, timestamp=self.ahora - timedelta(hours=hace_horas))

    def _serie(self, unidades=None):
        from apps.monitoreo.series import serie_disponibilidad

        return serie_disponibilidad(unidades, instante=self.ahora)

    def test_sin_transiciones_la_serie_es_plana_con_el_estado_actual(self):
        """Una estación sin eventos en la ventana estuvo todo el rato como está ahora."""
        self._estado(self.a, True)
        self._estado(self.b, False)
        serie = self._serie()
        self.assertEqual(len(serie), 25)  # 24 horas + el punto de ahora
        self.assertEqual({p.en_linea for p in serie}, {1})
        self.assertEqual({p.total for p in serie}, {2})
        self.assertEqual(serie[-1].pct, 50.0)

    def test_una_caida_a_media_ventana_parte_la_curva(self):
        """Antes de la transición estaba en línea; después, no. El estado previo se
        INFIERE del propio evento: son transiciones, no muestras."""
        self._estado(self.a, False)
        self._estado(self.b, True)
        self._transicion(self.a, en_linea=False, hace_horas=12)

        serie = self._serie()
        # A las 18 h atrás (antes de la caída) las dos estaban arriba.
        self.assertEqual(serie[6].en_linea, 2)
        # Justo después de la caída, solo una.
        self.assertEqual(serie[13].en_linea, 1)
        self.assertEqual(serie[-1].en_linea, 1)

    def test_el_ultimo_punto_coincide_con_el_kpi(self):
        """El riesgo que la auditoría señaló como mayor: que la curva y el número de al
        lado contaran cosas distintas (hallazgo D-01 otra vez). Resultó teórico —los
        cuatro criterios dan el mismo número— y esta prueba lo deja fijado."""
        from apps.catalogo.services import estado_flota

        self._estado(self.a, True)
        self._estado(self.b, False)
        self.a.ultimo_heartbeat = timezone.now()
        self.a.save(update_fields=['ultimo_heartbeat'])

        serie = self._serie()
        self.assertEqual(serie[-1].en_linea, estado_flota(None)['en_linea'])
        self.assertEqual(serie[-1].total, estado_flota(None)['total'])

    def test_ignora_las_transiciones_de_meshcentral(self):
        """Mezclar las fuentes contaría en línea a estaciones que el KPI cuenta caídas —
        que es justo el cruce con el que se detecta "el agente cayó pero la red vive"."""
        from apps.monitoreo.models import EstadoDispositivo

        self._estado(self.a, False)
        self._estado(self.b, False)
        self._transicion(self.a, en_linea=True, hace_horas=5,
                         fuente=EstadoDispositivo.Fuente.MESHCENTRAL)
        self.assertEqual(self._serie()[-1].en_linea, 0)

    def test_respeta_el_alcance_por_unidad_de_negocio(self):
        otra = Estacion.objects.create(
            codigo='SR002-A', farmacia=self.otra_unidad,
            estado_aprobacion=Estacion.EstadoAprobacion.APROBADA)
        self._estado(self.a, True)
        self._estado(self.b, True)
        self._estado(otra, True)

        self.assertEqual(self._serie()[-1].total, 3)
        self.assertEqual(self._serie(unidades=[self.sg])[-1].total, 2)

    def test_usa_el_mismo_recorte_que_el_kpi(self):
        """Solo aprobadas: una pendiente no reporta porque todavía no le toca."""
        Estacion.objects.create(
            codigo='SR001-C', farmacia=self.farmacia,
            estado_aprobacion=Estacion.EstadoAprobacion.PENDIENTE)
        self._estado(self.a, True)
        self._estado(self.b, True)
        self.assertEqual(self._serie()[-1].total, 2)

    def test_marca_las_horas_en_que_casi_todo_esta_cerrado(self):
        """La banda bajo la curva. No cambia el numerador ni el denominador: solo dice
        qué se espera a cada hora, con el horario real de cada farmacia."""
        self._estado(self.a, True)
        self._estado(self.b, True)
        serie = self._serie()
        # La farmacia abre 07:00-21:00 (hora de Ecuador). En la madrugada local, cerrada.
        from django.utils import timezone as tz

        for p in serie:
            local = tz.localtime(p.instante).hour
            if 2 <= local <= 5:
                self.assertTrue(p.fuera_de_horario, 'las %02d:00 deberían estar fuera' % local)
            if 10 <= local <= 16:
                self.assertFalse(p.fuera_de_horario, 'las %02d:00 deberían estar dentro' % local)

    def test_el_porcentaje_no_explota_sin_estaciones(self):
        Estacion.objects.all().delete()
        self.assertEqual(self._serie(), [])

    def test_cuesta_dos_consultas_y_no_crece_con_los_puntos(self):
        """Una para las estaciones con su estado y otra para los eventos. La
        reconstrucción es en memoria, así que dibujar más puntos no cuesta más consultas."""
        from apps.monitoreo.series import serie_disponibilidad

        self._estado(self.a, True)
        self._estado(self.b, False)
        self._transicion(self.a, en_linea=True, hace_horas=3)
        with self.assertNumQueries(3):   # estaciones + estados + eventos
            serie_disponibilidad(None, instante=self.ahora)
        with self.assertNumQueries(3):
            serie_disponibilidad(None, horas=72, instante=self.ahora)

    def test_el_tablero_dibuja_la_serie_sin_inventar_un_sla(self):
        """La marca es el promedio del período. Nadie definió un objetivo de
        disponibilidad en este proyecto; dibujar una línea de 90% sería inventarlo."""
        self._estado(self.a, True)
        self._estado(self.b, True)
        resp = self.client.get(reverse('panel:dashboard'))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'Estaciones en línea · últimas 24 h')
        self.assertEqual(len(resp.context['serie_24h']), 25)
        etiquetas = [m.etiqueta for m in resp.context['grafico_24h'].marcas]
        self.assertTrue(any('promedio' in e for e in etiquetas), etiquetas)
        self.assertFalse(any('SLA' in e for e in etiquetas), etiquetas)


class MosaicoDeFarmaciasTests(TestCase):
    """El mosaico de la Zona 2e: una celda por farmacia con agente, por cliente.

    Lo que fija: que el universo sean las farmacias INSTRUMENTADAS y no las ~697 que solo
    se sondean por enlace, que el estado salga de `cola_incidencias` y no de un criterio
    propio —dos definiciones de "farmacia con problemas" es como dos zonas de la misma
    pantalla acaban contradiciéndose—, el orden, y que reusar la cola ya calculada no
    cueste consultas de más.
    """

    def setUp(self):
        self.sg = UnidadNegocio.objects.get(codigo='SG')
        self.mia = UnidadNegocio.objects.get(codigo='MIA')
        grupo = Grupo.objects.create(codigo='TRXMOS')

        # Con agente: entran al mosaico.
        self.sana = Farmacia.objects.create(
            codigo='MS001', grupo=grupo, unidad_negocio=self.sg, ip_router='10.97.0.1')
        self.caida = Farmacia.objects.create(
            codigo='MS002', grupo=grupo, unidad_negocio=self.sg, ip_router='10.97.0.2')
        self.de_mia = Farmacia.objects.create(
            codigo='MM001', grupo=grupo, unidad_negocio=self.mia, ip_router='10.97.0.3')
        for codigo, farmacia in (('MS001-A', self.sana), ('MS002-A', self.caida),
                                 ('MM001-A', self.de_mia)):
            Estacion.objects.create(
                codigo=codigo, farmacia=farmacia,
                estado_aprobacion=Estacion.EstadoAprobacion.APROBADA,
                ultimo_heartbeat=timezone.now())

        # Sin agente, solo enlace: NO entra al mosaico aunque esté caída.
        self.solo_enlace = Farmacia.objects.create(
            codigo='MS003', grupo=grupo, unidad_negocio=self.sg, ip_router='10.97.0.4')

        self.usuario = User.objects.create_user(username='u_mosaico', password='x')
        PerfilUsuario.objects.create(usuario=self.usuario, acceso_todas_unidades=True)
        self.client.force_login(self.usuario)

    def _caer_enlace(self, farmacia):
        from apps.monitoreo.models import EstadoEnlaceFarmacia, EventoEnlaceFarmacia

        EstadoEnlaceFarmacia.objects.update_or_create(
            farmacia=farmacia, defaults={'alcanzable': False, 'respondio_alguna_vez': True})
        return EventoEnlaceFarmacia.objects.create(
            farmacia=farmacia, inicio=timezone.now() - timedelta(hours=1))

    def _mosaico(self, unidades=None):
        from apps.monitoreo.incidencias import mosaico_farmacias

        return mosaico_farmacias(unidades)

    def _celdas(self, mosaico):
        return {c['farmacia'].codigo: c['estado'] for g in mosaico for c in g['celdas']}

    def test_solo_entran_las_farmacias_con_agente(self):
        """697 celdas no se leen. El universo son las instrumentadas; la que solo tiene
        enlace sigue apareciendo en la cola de la Zona 1, que es donde se actúa."""
        self._caer_enlace(self.solo_enlace)
        celdas = self._celdas(self._mosaico())
        self.assertEqual(set(celdas), {'MS001', 'MS002', 'MM001'})
        self.assertNotIn('MS003', celdas)

    def test_sin_incidencia_la_farmacia_esta_operativa(self):
        self.assertEqual(self._celdas(self._mosaico())['MS001'], 'ok')

    def test_el_estado_sale_de_la_cola_y_no_de_un_criterio_propio(self):
        """Un enlace caído es prioridad 1 en la cola, o sea `critical`. El mosaico no
        vuelve a decidir qué es grave: usa el estado que la cola ya dio."""
        from apps.monitoreo.incidencias import cola_incidencias

        self._caer_enlace(self.caida)
        celdas = self._celdas(self._mosaico())
        self.assertEqual(celdas['MS002'], 'critical')
        self.assertEqual(celdas['MS001'], 'ok')

        en_cola = {i.farmacia.codigo: i.estado for i in cola_incidencias(None, limite=None)}
        self.assertEqual(celdas['MS002'], en_cola['MS002'])

    def test_agrupa_por_cliente_y_cuenta_las_criticas(self):
        self._caer_enlace(self.caida)
        mosaico = self._mosaico()
        por_cliente = {g['cliente']: g for g in mosaico}
        self.assertEqual(len(por_cliente), 2)
        sg = por_cliente[self.sg.nombre]
        self.assertEqual(sg['total'], 2)
        self.assertEqual(sg['criticas'], 1)
        self.assertEqual(por_cliente[self.mia.nombre]['criticas'], 0)

    def test_el_cliente_con_mas_criticas_va_primero(self):
        self._caer_enlace(self.de_mia)
        self.assertEqual(self._mosaico()[0]['cliente'], self.mia.nombre)

    def test_lo_peor_primero_dentro_de_cada_cliente(self):
        self._caer_enlace(self.caida)
        grupo = next(g for g in self._mosaico() if g['cliente'] == self.sg.nombre)
        self.assertEqual([c['farmacia'].codigo for c in grupo['celdas']], ['MS002', 'MS001'])

    def test_respeta_el_alcance_por_unidad_de_negocio(self):
        self.assertEqual(set(self._celdas(self._mosaico())), {'MS001', 'MS002', 'MM001'})
        self.assertEqual(set(self._celdas(self._mosaico(unidades=[self.sg]))), {'MS001', 'MS002'})

    def test_reusar_la_cola_no_cuesta_consultas_de_mas(self):
        """El tablero ya calcula la cola para la Zona 1: pasándosela, el mosaico solo paga
        la consulta de las farmacias."""
        from apps.monitoreo.incidencias import cola_incidencias, mosaico_farmacias

        self._caer_enlace(self.caida)
        cola = cola_incidencias(None, limite=None)
        with self.assertNumQueries(1):
            mosaico_farmacias(None, incidencias=cola)

    def test_el_tablero_lo_dibuja_con_el_mismo_estado_que_la_cola(self):
        self._caer_enlace(self.caida)
        resp = self.client.get(reverse('panel:dashboard'))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'Estado por farmacia')
        celdas = self._celdas(resp.context['mosaico'])
        self.assertEqual(celdas['MS002'], 'critical')
        # La misma farmacia está en la cola de la Zona 1, con el mismo estado.
        en_cola = {i.farmacia.codigo: i.estado for i in resp.context['incidencias']}
        self.assertEqual(en_cola['MS002'], celdas['MS002'])

    def test_sin_farmacias_con_agente_no_se_dibuja_nada(self):
        """Un bloque vacío permanente enseña a no mirarlo."""
        Estacion.objects.all().delete()
        self.assertEqual(self._mosaico(), [])
        resp = self.client.get(reverse('panel:dashboard'))
        self.assertNotContains(resp, 'Estado por farmacia')


class EstacionesSinSuscripcionesTests(TestCase):
    """Estaciones conectadas al broker que no escuchan nada.

    El caso que esto detecta apareció el 13-oct-2026: cuatro estaciones llevaban semanas
    sin aplicar el rollout del agente. Tenían credencial propia, ACL correcta con `allow
    all` sobre sus tópicos, latido fresco —el panel las pintaba verdes— y **cero
    suscripciones**. Publicaban y no recibían: ni la actualización, ni un script, ni un
    despliegue, ni la pausa de emergencia.

    Lo que estas pruebas fijan, sobre todo: que "no se pudo preguntar" NO se confunda con
    "ninguna", que es la regla que gobierna toda la Zona 0.
    """

    CONFIG = {'URL': 'http://emqx:18083/api/v5', 'API_KEY': 'k', 'API_SECRET': 's'}

    def setUp(self):
        self.sg = UnidadNegocio.objects.get(codigo='SG')
        grupo = Grupo.objects.create(codigo='TRXSOR')
        farmacia = Farmacia.objects.create(codigo='SO001', grupo=grupo, unidad_negocio=self.sg)
        for codigo in ('SO001-A', 'SO001-B'):
            Estacion.objects.create(
                codigo=codigo, farmacia=farmacia,
                estado_aprobacion=Estacion.EstadoAprobacion.APROBADA,
                ultimo_heartbeat=timezone.now())
        self.codigos = ['SO001-A', 'SO001-B']

        self.usuario = User.objects.create_user(username='u_sordas', password='x')
        PerfilUsuario.objects.create(usuario=self.usuario, acceso_todas_unidades=True)
        self.client.force_login(self.usuario)

    def _respuesta(self, clientes, hasnext=False):
        """Un stub de lo que devuelve la API de EMQX, con su context manager."""
        import json
        from contextlib import contextmanager

        cuerpo = json.dumps({'data': clientes, 'meta': {'hasnext': hasnext}}).encode()

        @contextmanager
        def falso_urlopen(req, timeout=None):
            class R:
                def read(self):
                    return cuerpo
            yield R()

        return falso_urlopen

    def _consultar(self, clientes, **kwargs):
        from unittest.mock import patch

        from apps.mqtt_worker import emqx_admin

        with override_settings(EMQX_ADMIN_CONFIG=self.CONFIG):
            with patch.object(emqx_admin.urllib.request, 'urlopen',
                              self._respuesta(clientes, **kwargs)):
                return emqx_admin.estaciones_sin_suscripciones(self.codigos)

    def test_detecta_la_conectada_sin_suscripciones(self):
        sordas = self._consultar([
            {'clientid': 'agente-prueba-SO001-A', 'connected': True, 'subscriptions_cnt': 0},
            {'clientid': 'agente-prueba-SO001-B', 'connected': True, 'subscriptions_cnt': 8},
        ])
        self.assertEqual(sordas, ['SO001-A'])

    def test_una_desconectada_no_cuenta(self):
        """Sin conexión no hay suscripciones, y eso no es estar sordo: es estar apagado,
        que el tablero ya cuenta en 'sin reportar'."""
        sordas = self._consultar([
            {'clientid': 'agente-prueba-SO001-A', 'connected': False, 'subscriptions_cnt': 0},
        ])
        self.assertEqual(sordas, [])

    def test_ignora_clientes_que_no_son_estaciones(self):
        """El worker y el panel también se conectan al broker."""
        sordas = self._consultar([
            {'clientid': 'saidsoft-worker', 'connected': True, 'subscriptions_cnt': 0},
            {'clientid': 'panel-publicador', 'connected': True, 'subscriptions_cnt': 0},
        ])
        self.assertEqual(sordas, [])

    def test_resuelve_el_codigo_por_sufijo_y_no_partiendo_el_clientid(self):
        """Los códigos llevan guion adentro (ML001-A) y el prefijo depende de cómo se
        llame el agente, así que partir la cadena sería frágil."""
        sordas = self._consultar([
            {'clientid': 'otro-nombre-de-agente-SO001-B', 'connected': True, 'subscriptions_cnt': 0},
        ])
        self.assertEqual(sordas, ['SO001-B'])

    def test_si_no_se_puede_preguntar_devuelve_None_y_no_lista_vacia(self):
        """La distinción que importa: "no lo sé" no es "no hay ninguna". Pintar de verde
        algo que no se comprobó es justo lo que la Zona 0 existe para evitar."""
        from unittest.mock import patch

        from apps.mqtt_worker import emqx_admin

        def explota(req, timeout=None):
            raise OSError('timeout')

        with override_settings(EMQX_ADMIN_CONFIG=self.CONFIG):
            with patch.object(emqx_admin.urllib.request, 'urlopen', explota):
                self.assertIsNone(emqx_admin.estaciones_sin_suscripciones(self.codigos))

    def test_sin_configuracion_de_emqx_devuelve_None(self):
        from apps.mqtt_worker import emqx_admin

        with override_settings(EMQX_ADMIN_CONFIG={}):
            self.assertIsNone(emqx_admin.estaciones_sin_suscripciones(self.codigos))

    def _tablero(self, valor):
        from unittest.mock import patch

        with patch('apps.panel.views.dashboard.estaciones_sin_suscripciones', return_value=valor):
            return self.client.get(reverse('panel:dashboard'))

    def test_el_tablero_avisa_cuando_hay_sordas(self):
        resp = self._tablero(['SO001-A'])
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.context['total_sordas'], 1)
        self.assertContains(resp, 'Estaciones sin escuchar')
        self.assertContains(resp, '1 no recibe órdenes')
        # Y el código de la estación va en el title, que es lo accionable: con eso se
        # sabe a cuál reiniciarle el agente.
        self.assertContains(resp, 'SO001-A')

    def test_el_aviso_concuerda_en_numero(self):
        self.assertContains(self._tablero(['SO001-A', 'SO001-B']), '2 no reciben órdenes')

    def test_el_tablero_dice_que_todas_escuchan_cuando_no_hay_ninguna(self):
        resp = self._tablero([])
        self.assertEqual(resp.context['total_sordas'], 0)
        self.assertContains(resp, 'todas escuchan')

    def test_no_se_pudo_comprobar_no_se_pinta_de_verde(self):
        """La regla de la Zona 0, aplicada a esta señal."""
        resp = self._tablero(None)
        self.assertIsNone(resp.context['total_sordas'])
        self.assertContains(resp, 'no se pudo comprobar')
        self.assertNotContains(resp, 'todas escuchan')


@override_settings(MQTT_PASSWORD_AGENTE='clave-de-prueba',
                   CSRF_TRUSTED_ORIGINS=['http://otro', 'https://10.9.9.9:8084'])
class EnlaceInstaladorTests(TestCase):
    """El enlace de un solo uso para bajar Instalar-Saidsoft.ps1 desde la estación (§10-CG).

    El script lleva la clave MQTT compartida. Lo que estas pruebas fijan es que el enlace
    sea lo único que lo entrega, y solo una vez: un enlace reusable o que no vence sería lo
    mismo que publicarlo en /media/, que es justo lo que esto vino a evitar.
    """

    def setUp(self):
        import tempfile as _tempfile
        from pathlib import Path as _Path

        # Un MEDIA_ROOT propio con el paquete publicado: el servidor calcula su SHA-256 al
        # armar el script y sin paquete no entrega nada (§10-CL).
        self._media = _tempfile.mkdtemp()
        ajuste = override_settings(MEDIA_ROOT=self._media)
        ajuste.enable()
        self.addCleanup(ajuste.disable)
        carpeta = _Path(self._media) / 'agente-instalador'
        carpeta.mkdir(parents=True)
        self.paquete = carpeta / 'agente-instalador.zip'
        self.paquete.write_bytes(b'PK-paquete-de-prueba')

        creador = User.objects.create_user(username='creador_inst', password='x')
        VersionAgente.objects.create(
            version='agente-prueba-0.34', ejecutable=SimpleUploadedFile('agente.exe', b'MZ'),
            creado_por=creador,
        )
        self.operador = User.objects.create_user(username='operador_inst', password='x')
        self.operador.user_permissions.add(
            Permission.objects.get(codename='aprobar_estacion'),
            Permission.objects.get(codename='view_estacion'),
        )
        PerfilUsuario.objects.create(usuario=self.operador, acceso_todas_unidades=True)
        self.client.force_login(self.operador)

    def _emitir(self):
        resp = self.client.post(reverse('panel:instalador_enlace_emitir'))
        self.assertEqual(resp.status_code, 200)
        url = re.search(r'curl\.exe .*?"(https://[^"]+)"', resp.content.decode()).group(1)
        return url

    def _bajar(self, url):
        from django.test import Client

        # Otro cliente, sin sesión: así lo baja curl.exe desde la estación.
        return Client().get(url.split(':8084', 1)[1])

    def test_el_enlace_apunta_al_panel_https_con_su_puerto(self):
        """nginx reenvía Host sin el puerto: armarlo desde el request daría el 443."""
        self.assertTrue(self._emitir().startswith('https://10.9.9.9:8084/instalador/'))

    def test_entrega_el_script_con_la_clave_sin_pedir_login(self):
        resp = self._bajar(self._emitir())
        self.assertEqual(resp.status_code, 200)
        cuerpo = resp.content.decode('utf-8-sig')
        self.assertIn("-MqttPassword 'clave-de-prueba'", cuerpo)
        self.assertIn('Instalar-Saidsoft.ps1', resp['Content-Disposition'])
        self.assertIn('no-store', resp['Cache-Control'])

    def test_lleva_bom_para_powershell_5(self):
        """Sin BOM, PowerShell 5.1 lee el .ps1 como ANSI y rompe los acentos."""
        self.assertTrue(self._bajar(self._emitir()).content.startswith(b'\xef\xbb\xbf'))

    def test_sirve_una_sola_vez(self):
        url = self._emitir()
        self.assertEqual(self._bajar(url).status_code, 200)
        segunda = self._bajar(url)
        self.assertEqual(segunda.status_code, 410)
        self.assertNotIn(b'clave-de-prueba', segunda.content)

    def test_vence(self):
        from apps.catalogo.models import EnlaceInstalador

        url = self._emitir()
        EnlaceInstalador.objects.update(expira_en=timezone.now() - timedelta(seconds=1))
        self.assertEqual(self._bajar(url).status_code, 410)

    def test_un_token_inventado_no_entrega_nada(self):
        resp = self.client.get(reverse('panel:instalador_descargar', args=['inventado']))
        self.assertEqual(resp.status_code, 410)
        self.assertNotIn(b'clave-de-prueba', resp.content)

    def test_no_guarda_el_token_en_claro(self):
        from apps.catalogo.models import EnlaceInstalador

        token = self._emitir().rstrip('/').rsplit('/', 1)[1]
        enlace = EnlaceInstalador.objects.get()
        self.assertNotEqual(enlace.token_hash, token)
        self.assertEqual(enlace.prefijo, token[:8])

    def test_registra_quien_lo_pidio_y_desde_donde_se_bajo(self):
        from apps.catalogo.models import EnlaceInstalador

        self._bajar(self._emitir())
        enlace = EnlaceInstalador.objects.get()
        self.assertEqual(enlace.creado_por, self.operador)
        self.assertIsNotNone(enlace.usado_en)
        self.assertEqual(enlace.ip_uso, '127.0.0.1')
        acciones = set(EventoAuditoria.objects.values_list('accion', flat=True))
        self.assertTrue({'instalador.emitir', 'instalador.descargar'} <= acciones)

    def test_sin_permiso_no_se_emite(self):
        otro = User.objects.create_user(username='sin_perm_inst', password='x')
        PerfilUsuario.objects.create(usuario=otro, acceso_todas_unidades=True)
        self.client.force_login(otro)
        self.assertEqual(self.client.post(reverse('panel:instalador_enlace_emitir')).status_code, 403)

    def test_sin_clave_en_el_servidor_no_emite_ni_gasta_enlaces(self):
        from apps.catalogo.models import EnlaceInstalador

        url = self._emitir()
        with override_settings(MQTT_PASSWORD_AGENTE=''):
            resp = self.client.post(reverse('panel:instalador_enlace_emitir'))
            self.assertContains(resp, 'MQTT_PASSWORD_AGENTE')
            self.assertEqual(self._bajar(url).status_code, 503)
        # El enlace que ya estaba emitido sigue sirviendo cuando vuelve la clave.
        self.assertEqual(EnlaceInstalador.objects.count(), 1)
        self.assertEqual(self._bajar(url).status_code, 200)

    def test_la_pagina_de_estaciones_ofrece_el_boton_solo_con_permiso(self):
        self.assertContains(self.client.get(reverse('panel:estaciones_lista')), 'Instalar estación nueva')
        otro = User.objects.create_user(username='solo_ver_inst', password='x')
        otro.user_permissions.add(Permission.objects.get(codename='view_estacion'))
        PerfilUsuario.objects.create(usuario=otro, acceso_todas_unidades=True)
        self.client.force_login(otro)
        self.assertNotContains(self.client.get(reverse('panel:estaciones_lista')), 'Instalar estación nueva')

    @override_settings(CONFIAR_X_REAL_IP=True)
    def test_guarda_la_ip_de_la_estacion_y_no_la_de_nginx(self):
        from django.test import Client

        from apps.catalogo.models import EnlaceInstalador

        url = self._emitir()
        Client().get(url.split(':8084', 1)[1], REMOTE_ADDR='172.18.0.4', HTTP_X_REAL_IP='10.111.50.7')
        self.assertEqual(EnlaceInstalador.objects.get().ip_uso, '10.111.50.7')

    def test_verifica_el_sha256_del_paquete(self):
        import hashlib

        cuerpo = self._bajar(self._emitir()).content.decode('utf-8-sig')
        self.assertIn(hashlib.sha256(b'PK-paquete-de-prueba').hexdigest(), cuerpo)
        self.assertIn("Get-FileHash 'agente-instalador.zip' -Algorithm SHA256", cuerpo)

    def test_se_borra_aunque_la_instalacion_falle(self):
        """El borrado era la última línea: un hostname fuera de FARMACIA-SUFIJO dejaba el
        script con la clave en %TEMP%. Ahora todo va dentro de try/finally."""
        cuerpo = self._bajar(self._emitir()).content.decode('utf-8-sig')
        inicio_try = cuerpo.index('try {\nif ($Codigo -notmatch')
        finally_ = cuerpo.index('} finally {')
        self.assertLess(inicio_try, cuerpo.index("throw \"El codigo '$Codigo'"))
        self.assertIn('Remove-Item -LiteralPath $PSCommandPath', cuerpo[finally_:])

    def test_sin_paquete_publicado_el_enlace_no_se_gasta(self):
        from apps.catalogo.models import EnlaceInstalador

        url = self._emitir()
        self.paquete.unlink()
        self.assertEqual(self._bajar(url).status_code, 503)
        self.assertIsNone(EnlaceInstalador.objects.get().usado_en)
        self.paquete.write_bytes(b'PK-paquete-de-prueba')
        self.assertEqual(self._bajar(url).status_code, 200)

    @override_settings(MQTT_PASSWORD_AGENTE="cla've’; Remove-Item C:/x -Recurse #")
    def test_escapa_tambien_las_comillas_tipograficas(self):
        """PowerShell también cierra una cadena simple con ’: escapar solo la recta dejaba
        inyectar código desde la clave."""
        cuerpo = self._bajar(self._emitir()).content.decode('utf-8-sig')
        self.assertIn("-MqttPassword 'cla''ve’’; Remove-Item", cuerpo)

    def test_el_script_se_borra_solo_al_terminar(self):
        self.assertIn('Remove-Item -LiteralPath $PSCommandPath', self._bajar(self._emitir()).content.decode('utf-8-sig'))


class IncidentesDeEnlaceTests(TestCase):
    """"Qué atender primero" en /enlaces/ (`incidentes_de_enlace`).

    El caso que lo motivó, 17-oct-2026: a las 06:05 cayeron 36 farmacias de Telconet, 20
    volvieron solas y 16 no; y mientras tanto GBV07 se cayó SOLA y sin respaldo, perdida
    entre las otras. Esto fija que el corte se vea como UN incidente con su cuenta de
    cayeron/volvieron/siguen, y que la aislada sin respaldo vaya primero.
    """

    def setUp(self):
        self.sg = UnidadNegocio.objects.get(codigo='SG')
        self.grupo = Grupo.objects.create(codigo='TRX960')
        self.ahora = timezone.now()
        self.n = 0
        self.usuario = User.objects.create_user(username='u_incidentes', password='x')
        self.usuario.user_permissions.add(Permission.objects.get(codename='view_estadoenlacefarmacia'))
        PerfilUsuario.objects.create(usuario=self.usuario, acceso_todas_unidades=True)
        self.client.force_login(self.usuario)

    def _evento(self, isp='TELCONET', *, hace_min=0, volvio=False, backup=True, respondio=True, codigo=None):
        from apps.monitoreo.models import EstadoEnlaceFarmacia, EventoEnlaceFarmacia

        self.n += 1
        f = Farmacia.objects.create(
            codigo=codigo or f'GI{self.n:03d}', grupo=self.grupo, unidad_negocio=self.sg,
            tipo_enlace=isp, tiene_backup=backup, ip_router=f'10.7.7.{self.n}',
        )
        EstadoEnlaceFarmacia.objects.create(farmacia=f, alcanzable=volvio, respondio_alguna_vez=respondio)
        inicio = self.ahora - timedelta(minutes=hace_min)
        return EventoEnlaceFarmacia.objects.create(
            farmacia=f, inicio=inicio, fin=(inicio + timedelta(minutes=30)) if volvio else None,
        )

    def _incidentes(self):
        from apps.monitoreo.enlaces import incidentes_de_enlace

        return incidentes_de_enlace()

    def test_el_corte_cuenta_tambien_los_que_ya_volvieron(self):
        for i in range(4):
            self._evento(hace_min=180, volvio=True)
        siguen = [self._evento(hace_min=180) for _ in range(2)]
        masivos = self._incidentes()['masivos']
        self.assertEqual(len(masivos), 1)
        m = masivos[0]
        self.assertEqual((m['cayeron'], m['volvieron'], len(m['siguen'])), (6, 4, 2))
        self.assertEqual({s['farmacia'] for s in m['siguen']}, {e.farmacia for e in siguen})
        self.assertEqual(m['clase'], 'proveedor')

    def test_un_corte_que_ya_volvio_entero_no_aparece(self):
        for _ in range(5):
            self._evento(hace_min=120, volvio=True)
        self.assertEqual(self._incidentes()['masivos'], [])

    def test_la_aislada_sin_respaldo_va_primero(self):
        con = self._evento(hace_min=10, codigo='GI901')                 # más reciente
        sin = self._evento(hace_min=200, backup=False, codigo='GI902')  # más vieja, sin backup
        aisladas = self._incidentes()['aisladas']
        self.assertEqual([s['farmacia'] for s in aisladas], [sin.farmacia, con.farmacia])

    def test_la_que_lleva_dias_va_al_final(self):
        vieja = self._evento(hace_min=60 * 24 * 23, backup=False, codigo='GI903')
        nueva = self._evento(hace_min=30, codigo='GI904')
        aisladas = self._incidentes()['aisladas']
        self.assertEqual([s['farmacia'] for s in aisladas], [nueva.farmacia, vieja.farmacia])
        self.assertTrue(aisladas[1]['cronica'])

    def test_las_aisladas_no_repiten_las_del_corte(self):
        for _ in range(3):
            self._evento(hace_min=60)
        sola = self._evento(hace_min=300, codigo='GI905')
        r = self._incidentes()
        self.assertEqual([s['farmacia'] for s in r['aisladas']], [sola.farmacia])

    def test_varios_proveedores_a_la_vez_apuntan_al_monitoreo(self):
        for isp in ('TELCONET', 'PUNTO NET'):
            for _ in range(3):
                self._evento(isp, hace_min=15)
        self.assertEqual(self._incidentes()['masivos'][0]['clase'], 'ceguera')

    def test_el_corte_se_reconoce_aunque_el_usuario_vea_una_sola_farmacia(self):
        """Los racimos se arman con la flota entera: con el alcance de un cliente, un corte
        ajeno se volvía "caída del sitio" o desaparecía (§10-CL)."""
        from apps.monitoreo.enlaces import incidentes_de_enlace

        eventos = [self._evento(hace_min=30) for _ in range(4)]
        r = incidentes_de_enlace([eventos[0].farmacia_id])
        self.assertEqual(len(r['masivos']), 1)
        self.assertEqual(r['masivos'][0]['cayeron'], 1)  # solo lo suyo, no el total ajeno
        self.assertEqual(r['aisladas'], [])
        self.assertEqual(r['clase_por_farmacia'][eventos[0].farmacia_id][0], 'proveedor')

    def test_los_que_nunca_respondieron_no_son_caidas(self):
        self._evento(hace_min=5, respondio=False)
        r = self._incidentes()
        self.assertEqual((r['masivos'], r['aisladas']), ([], []))

    def test_la_pagina_de_enlaces_lo_muestra(self):
        for _ in range(3):
            self._evento(hace_min=60, volvio=True)
        self._evento(hace_min=60, codigo='GI910')
        self._evento(hace_min=5, backup=False, codigo='GI911')
        resp = self.client.get(reverse('panel:enlaces_farmacias_lista'))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'Qué atender primero')
        self.assertContains(resp, 'Corte masivo')
        self.assertContains(resp, 'siguen caídas 1')
        self.assertContains(resp, 'sin respaldo: probablemente sin sistema')

    def test_sin_caidas_no_se_dibuja(self):
        resp = self.client.get(reverse('panel:enlaces_farmacias_lista'))
        self.assertNotContains(resp, 'Qué atender primero')


@override_settings(ENLACES_NOTIFICAR_A=['noc@x.test'], ENLACES_TELEGRAM_CHAT_ID='')
class ReanudacionMonitoreoTests(TestCase):
    """"No volvieron tras el apagón" (§10-CJ).

    El 17-oct-2026 el monitoreo estuvo apagado de ~03:15 a 06:00 por un corte de luz en el
    data center. Al volver, ~35 farmacias no respondían y se reiniciaron a mano recién al
    abrir. Esto fija que el hueco se detecte, que se mida contra quién respondía ANTES, y
    que se avise una sola vez, después de dar tiempo a las que vuelven solas.
    """

    def setUp(self):
        from apps.monitoreo.models import EstadoEnlaceFarmacia

        self.sg = UnidadNegocio.objects.get(codigo='SG')
        grupo = Grupo.objects.create(codigo='TRX950')
        self.ahora = timezone.now()
        hace_3h = self.ahora - timedelta(hours=3)
        self.farmacias = []
        for i in range(10):
            f = Farmacia.objects.create(codigo=f'GR{i:03d}', grupo=grupo, unidad_negocio=self.sg,
                                        tipo_enlace='TELCONET', ip_router=f'10.6.6.{i + 1}')
            EstadoEnlaceFarmacia.objects.create(farmacia=f, alcanzable=True, respondio_alguna_vez=True,
                                                ultima_verificacion=hace_3h)
            self.farmacias.append(f)

    def _barrer(self, caidas=(), farmacias=None):
        from apps.monitoreo import enlaces

        caidas = {f.ip_router for f in caidas}
        with patch.object(enlaces, 'verificar_ping_disponible'), \
                patch.object(enlaces, 'sondear_enlace', side_effect=lambda ip, *a, **k: (ip not in caidas, 1.0 if ip not in caidas else None)):
            return enlaces.sondear_enlaces_farmacias(farmacias)

    def test_un_hueco_registra_la_reanudacion_con_quien_respondia(self):
        from apps.monitoreo.models import ReanudacionMonitoreo

        self._barrer(caidas=self.farmacias[:2])
        r = ReanudacionMonitoreo.objects.get()
        self.assertEqual(set(r.farmacias_previas), {f.pk for f in self.farmacias})
        self.assertGreaterEqual(r.duracion_minutos, 179)

    def test_sin_hueco_no_registra_nada(self):
        from apps.monitoreo.models import EstadoEnlaceFarmacia, ReanudacionMonitoreo

        EstadoEnlaceFarmacia.objects.update(ultima_verificacion=self.ahora - timedelta(minutes=2))
        self._barrer()
        self.assertFalse(ReanudacionMonitoreo.objects.exists())

    def test_un_barrido_manual_de_unas_pocas_no_cuenta(self):
        from apps.monitoreo.models import ReanudacionMonitoreo

        self._barrer(farmacias=self.farmacias[:2])
        self.assertFalse(ReanudacionMonitoreo.objects.exists())

    def test_las_que_cayeron_justo_antes_del_hueco_cuentan_como_previas(self):
        """La red del data center se degrada antes de apagarse: esas caídas son del corte."""
        from apps.monitoreo.models import EstadoEnlaceFarmacia, EventoEnlaceFarmacia, ReanudacionMonitoreo

        f = self.farmacias[0]
        EstadoEnlaceFarmacia.objects.filter(farmacia=f).update(alcanzable=False)
        EventoEnlaceFarmacia.objects.create(farmacia=f, inicio=self.ahora - timedelta(hours=3, minutes=10))
        self._barrer()
        self.assertIn(f.pk, ReanudacionMonitoreo.objects.get().farmacias_previas)

    def _con_reanudacion(self, caidas, hace_min=20):
        from apps.monitoreo.models import EstadoEnlaceFarmacia, ReanudacionMonitoreo

        r = ReanudacionMonitoreo.objects.create(
            apagado_desde=self.ahora - timedelta(hours=3), reanudado_en=self.ahora - timedelta(minutes=hace_min),
            farmacias_previas=[f.pk for f in self.farmacias])
        EstadoEnlaceFarmacia.objects.filter(farmacia__in=caidas).update(alcanzable=False, fallas_consecutivas=5)
        return r

    def test_avisa_una_vez_con_las_que_no_volvieron(self):
        from django.core import mail

        from apps.monitoreo.enlaces import avisar_reanudacion_monitoreo

        self._con_reanudacion(self.farmacias[:3])
        resumen = avisar_reanudacion_monitoreo()
        self.assertTrue(resumen['enviado'])
        self.assertEqual(resumen['pendientes'], 3)
        self.assertEqual(len(mail.outbox), 1)
        cuerpo = mail.outbox[0].body
        for f in self.farmacias[:3]:
            self.assertIn(f.codigo, cuerpo)
        self.assertNotIn(self.farmacias[5].codigo, cuerpo)
        self.assertIn('7 volvieron solas', cuerpo)
        self.assertFalse(avisar_reanudacion_monitoreo()['enviado'])
        self.assertEqual(len(mail.outbox), 1)

    def test_espera_antes_de_avisar(self):
        """Las que vuelven solas lo hacen en los primeros minutos: avisar antes mandaría a
        reiniciar routers que iban a volver."""
        from django.core import mail

        from apps.monitoreo.enlaces import avisar_reanudacion_monitoreo

        self._con_reanudacion(self.farmacias[:3], hace_min=5)
        self.assertFalse(avisar_reanudacion_monitoreo()['enviado'])
        self.assertEqual(len(mail.outbox), 0)

    def test_si_volvieron_todas_no_avisa(self):
        from django.core import mail

        from apps.monitoreo.enlaces import avisar_reanudacion_monitoreo
        from apps.monitoreo.models import ReanudacionMonitoreo

        self._con_reanudacion([])
        self.assertFalse(avisar_reanudacion_monitoreo()['enviado'])
        self.assertEqual(len(mail.outbox), 0)
        self.assertIsNotNone(ReanudacionMonitoreo.objects.get().avisado_en)

    def test_dos_barridos_juntos_no_duplican_el_apagon(self):
        from apps.monitoreo.enlaces import _registrar_reanudacion_si_hubo_hueco
        from apps.monitoreo.models import ReanudacionMonitoreo

        primera = _registrar_reanudacion_si_hubo_hueco()
        segunda = _registrar_reanudacion_si_hubo_hueco()
        self.assertEqual(ReanudacionMonitoreo.objects.count(), 1)
        self.assertEqual(primera.pk, segunda.pk)

    def test_una_lectura_suelta_durante_el_apagon_no_tapa_el_hueco(self):
        """Probar una farmacia a mano o un reporte de la sonda externa movían el Max de
        `ultima_verificacion` y el apagón no se avisaba. Ahora cuenta el último barrido completo."""
        from apps.monitoreo.models import BarridoEnlaces, EstadoEnlaceFarmacia, ReanudacionMonitoreo

        BarridoEnlaces.objects.create(pk=1, ultimo_completo=self.ahora - timedelta(hours=3))
        EstadoEnlaceFarmacia.objects.filter(farmacia=self.farmacias[0]).update(
            ultima_verificacion=self.ahora - timedelta(minutes=1))
        self._barrer()
        self.assertTrue(ReanudacionMonitoreo.objects.exists())
        self.assertGreater(BarridoEnlaces.objects.get().ultimo_completo, self.ahora - timedelta(minutes=1))

    def test_el_barrido_manual_de_la_flota_tambien_cuenta(self):
        from apps.monitoreo import enlaces
        from apps.monitoreo.models import ReanudacionMonitoreo

        # `manage.py sondear_enlaces` sin --farmacia pasa la flota como lista: antes eso no
        # contaba como barrido completo y nunca detectaba un apagón.
        with patch.object(enlaces, 'verificar_ping_disponible'), \
                patch.object(enlaces, 'sondear_enlace', return_value=(True, 1.0)):
            enlaces.sondear_enlaces_farmacias(self.farmacias, flota_entera=True)
        self.assertTrue(ReanudacionMonitoreo.objects.exists())

    def test_previas_cuenta_solo_las_del_cliente_y_activas(self):
        from apps.monitoreo.enlaces import reanudacion_vigente

        self._con_reanudacion(self.farmacias[:2])
        Farmacia.objects.filter(pk=self.farmacias[9].pk).update(activa=False)
        alcance = [f.pk for f in self.farmacias[:5]]
        self.assertEqual(reanudacion_vigente(alcance)['previas'], 5)
        self.assertEqual(reanudacion_vigente()['previas'], 9)

    def test_un_aviso_viejo_sin_mandar_no_sale_despues_del_nuevo(self):
        from django.core import mail

        from apps.monitoreo.enlaces import avisar_reanudacion_monitoreo
        from apps.monitoreo.models import ReanudacionMonitoreo

        ReanudacionMonitoreo.objects.create(
            apagado_desde=self.ahora - timedelta(hours=10), reanudado_en=self.ahora - timedelta(hours=9),
            farmacias_previas=[f.pk for f in self.farmacias])
        self._con_reanudacion(self.farmacias[:2])
        avisar_reanudacion_monitoreo()
        self.assertFalse(avisar_reanudacion_monitoreo()['enviado'])
        self.assertEqual(len(mail.outbox), 1)
        self.assertFalse(ReanudacionMonitoreo.objects.filter(avisado_en__isnull=True).exists())

    def test_la_pagina_de_enlaces_lo_muestra(self):
        self._con_reanudacion(self.farmacias[:2])
        usuario = User.objects.create_user(username='u_reanudacion', password='x')
        usuario.user_permissions.add(Permission.objects.get(codename='view_estadoenlacefarmacia'))
        PerfilUsuario.objects.create(usuario=usuario, acceso_todas_unidades=True)
        self.client.force_login(usuario)
        resp = self.client.get(reverse('panel:enlaces_farmacias_lista'))
        self.assertContains(resp, 'No volvieron tras el apagón')
        self.assertContains(resp, '2 no volvieron')
        self.assertContains(resp, self.farmacias[0].codigo)


class HistorialEnlacesTests(TestCase):
    """Historial y disponibilidad de enlaces (§10-CK).

    Lo que fija: que la duración se recorte al período, que lo que el monitoreo no vio
    (varios proveedores a la vez) se muestre pero no cuente para la disponibilidad, que un
    corte masivo se reconozca aunque se filtre una sola farmacia, y que el CSV salga.
    """

    def setUp(self):
        from apps.monitoreo.models import EstadoEnlaceFarmacia

        self.sg = UnidadNegocio.objects.get(codigo='SG')
        self.grupo = Grupo.objects.create(codigo='TRX940')
        self.hoy = timezone.localdate()
        self.n = 0
        self.usuario = User.objects.create_user(username='u_hist', password='x')
        self.usuario.user_permissions.add(Permission.objects.get(codename='view_estadoenlacefarmacia'))
        PerfilUsuario.objects.create(usuario=self.usuario, acceso_todas_unidades=True)
        self.client.force_login(self.usuario)
        self._estado = EstadoEnlaceFarmacia

    def _farmacia(self, isp='TELCONET'):
        self.n += 1
        f = Farmacia.objects.create(codigo=f'GH{self.n:03d}', grupo=self.grupo, unidad_negocio=self.sg,
                                    tipo_enlace=isp, ip_router=f'10.5.5.{self.n}')
        self._estado.objects.create(farmacia=f, alcanzable=True, respondio_alguna_vez=True)
        return f

    def _caida(self, farmacia, inicio, minutos):
        from apps.monitoreo.models import EventoEnlaceFarmacia

        return EventoEnlaceFarmacia.objects.create(
            farmacia=farmacia, inicio=inicio, fin=inicio + timedelta(minutes=minutos) if minutos is not None else None)

    def _historial(self, dias=7, **kw):
        from apps.monitoreo.historial_enlaces import historial_enlaces

        hasta = timezone.now()
        return historial_enlaces(Farmacia.objects.all(), hasta - timedelta(days=dias), hasta, **kw)

    def test_disponibilidad_de_una_caida_del_sitio(self):
        f = self._farmacia()
        self._caida(f, timezone.now() - timedelta(days=2), 60)
        r = self._historial(dias=1 + 6)
        fila = r.por_farmacia[0]
        self.assertEqual((fila.caidas_sla, round(fila.minutos_sla)), (1, 60))
        self.assertAlmostEqual(fila.disponibilidad_sla, round(100 * (1 - 60 / (7 * 24 * 60)), 2), places=1)

    def test_la_duracion_se_recorta_al_periodo(self):
        """Una caída de 23 días no suma 23 días en un informe de una semana."""
        f = self._farmacia()
        self._caida(f, timezone.now() - timedelta(days=23), None)
        fila = self._historial(dias=7).por_farmacia[0]
        self.assertAlmostEqual(fila.minutos_sla, 7 * 24 * 60, delta=2)
        self.assertEqual(fila.disponibilidad_sla, 0.0)

    def test_lo_que_el_monitoreo_no_vio_no_cuenta_para_la_disponibilidad(self):
        inicio = timezone.now() - timedelta(days=1)
        afectadas = [self._farmacia(isp) for isp in ('TELCONET', 'PUNTO NET') for _ in range(3)]
        for f in afectadas:
            self._caida(f, inicio, 120)
        r = self._historial()
        self.assertEqual(r.excluidas_por_ceguera, 6)
        self.assertTrue(all(fila.minutos_sla == 0 and fila.disponibilidad_sla == 100 for fila in r.por_farmacia))
        self.assertEqual({c.clase for c in r.caidas}, {'ceguera'})

    def test_un_corte_del_proveedor_si_cuenta_y_se_cuenta_una_vez(self):
        inicio = timezone.now() - timedelta(days=1)
        for _ in range(4):
            self._caida(self._farmacia(), inicio, 30)
        r = self._historial()
        self.assertEqual({c.clase for c in r.caidas}, {'proveedor'})
        self.assertEqual(r.por_proveedor[0].cortes_masivos, 1)
        self.assertEqual(r.por_proveedor[0].caidas, 4)

    def test_filtrar_una_farmacia_no_convierte_el_corte_en_caida_del_sitio(self):
        from apps.monitoreo.historial_enlaces import historial_enlaces

        inicio = timezone.now() - timedelta(days=1)
        fs = [self._farmacia() for _ in range(3)]
        for f in fs:
            self._caida(f, inicio, 30)
        hasta = timezone.now()
        r = historial_enlaces(Farmacia.objects.filter(pk=fs[0].pk), hasta - timedelta(days=7), hasta)
        self.assertEqual([c.clase for c in r.caidas], ['proveedor'])

    def test_la_duracion_minima_no_cambia_la_disponibilidad(self):
        """40 caídas de 45 min con "1 hora o más" salían al 100 %: el filtro achicaba la suma."""
        f = self._farmacia()
        for i in range(4):
            self._caida(f, timezone.now() - timedelta(days=1, hours=i * 3), 45)
        self._caida(f, timezone.now() - timedelta(days=3), 90)
        todas = self._historial()
        filtrada = self._historial(minimo_minutos=60)
        self.assertEqual(len(filtrada.caidas), 1)
        self.assertEqual(filtrada.por_farmacia[0].disponibilidad_sla, todas.por_farmacia[0].disponibilidad_sla)
        self.assertEqual(round(filtrada.por_farmacia[0].minutos_sla), 4 * 45 + 90)

    def test_la_clase_no_depende_del_desde_pedido(self):
        """Una caída abierta de un corte que ya volvió no queda sola al mover el "desde"
        después del corte: se clasifica con lo que pasó alrededor (§10-CL)."""
        from apps.monitoreo.historial_enlaces import historial_enlaces

        inicio = timezone.now() - timedelta(days=3)
        sigue = self._farmacia()
        self._caida(sigue, inicio, None)
        for _ in range(3):
            self._caida(self._farmacia(), inicio, 20)
        hasta = timezone.now()
        amplio = historial_enlaces(Farmacia.objects.all(), hasta - timedelta(days=7), hasta)
        despues = historial_enlaces(Farmacia.objects.all(), hasta - timedelta(days=1), hasta)

        def clase(h):
            return next(c.clase for c in h.caidas if c.farmacia == sigue)

        self.assertEqual(clase(amplio), 'proveedor')
        self.assertEqual(clase(despues), 'proveedor')

    def test_filtro_por_duracion_minima(self):
        f = self._farmacia()
        self._caida(f, timezone.now() - timedelta(days=1), 5)
        self._caida(f, timezone.now() - timedelta(days=2), 90)
        self.assertEqual(len(self._historial(minimo_minutos=60).caidas), 1)

    def test_los_que_nunca_respondieron_no_entran(self):
        f = self._farmacia()
        self._estado.objects.filter(farmacia=f).update(respondio_alguna_vez=False)
        self._caida(f, timezone.now() - timedelta(days=1), None)
        self.assertEqual(self._historial().caidas, [])

    def test_la_pagina_y_sus_dos_vistas(self):
        f = self._farmacia()
        self._caida(f, timezone.now() - timedelta(days=1), 45)
        url = reverse('panel:enlaces_historial')
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'Historial de enlaces')
        self.assertContains(resp, f.codigo)
        self.assertContains(resp, 'TELCONET')
        resp = self.client.get(url, {'vista': 'eventos'})
        self.assertContains(resp, 'Del sitio')

    def test_exporta_csv_por_farmacia_y_por_evento(self):
        f = self._farmacia()
        self._caida(f, timezone.now() - timedelta(days=1), 45)
        url = reverse('panel:enlaces_historial')
        resp = self.client.get(url, {'formato': 'csv'})
        self.assertEqual(resp['Content-Type'], 'text/csv; charset=utf-8')
        cuerpo = resp.content.decode('utf-8-sig')
        self.assertIn('disponibilidad_sla_pct', cuerpo)
        self.assertIn(f.codigo, cuerpo)
        cuerpo = self.client.get(url, {'formato': 'csv', 'vista': 'eventos'}).content.decode('utf-8-sig')
        self.assertIn('cuenta_para_sla', cuerpo)
        self.assertIn('45', cuerpo)

    def test_sin_permiso_no_entra(self):
        otro = User.objects.create_user(username='u_hist_no', password='x')
        PerfilUsuario.objects.create(usuario=otro, acceso_todas_unidades=True)
        self.client.force_login(otro)
        self.assertEqual(self.client.get(reverse('panel:enlaces_historial')).status_code, 403)

    def test_reportes_lo_ofrece_solo_a_quien_puede_verlo(self):
        self.assertContains(self.client.get(reverse('panel:reportes_index')), 'Disponibilidad de enlaces')
        otro = User.objects.create_user(username='u_hist_rep', password='x')
        PerfilUsuario.objects.create(usuario=otro, acceso_todas_unidades=True)
        self.client.force_login(otro)
        self.assertNotContains(self.client.get(reverse('panel:reportes_index')), 'Disponibilidad de enlaces')
