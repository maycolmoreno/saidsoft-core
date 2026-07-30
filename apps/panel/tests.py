import csv
import io
from datetime import date

from cryptography.fernet import Fernet
from django.contrib.auth.models import Permission, User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.activos.models import Activo, Colaborador
from apps.auditoria.models import EventoAuditoria, registrar_evento
from apps.catalogo import crypto
from apps.catalogo.models import ClaveRecuperacionBitLocker, Estacion, Farmacia, Grupo, UnidadNegocio
from apps.cuentas.models import PerfilUsuario
from apps.cumplimiento.models import (
    ActividadCumplimiento, ResultadoCumplimientoEstacion, TipoObjetivoCumplimiento,
)
from apps.despliegues.models import Despliegue
from apps.mantenimiento.models import EstadoGeneralEquipo, Mantenimiento
from apps.monitoreo.models import Alerta, Metrica, ReglaAlerta
from apps.scripts.models import EjecucionScript, Script, ScriptProgramado, TipoScript


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

        self.usuario_sin_permiso = User.objects.create_user(username='sin_permiso', password='x')
        PerfilUsuario.objects.create(usuario=self.usuario_sin_permiso, acceso_todas_unidades=True)

        self.usuario_con_permiso = User.objects.create_user(username='con_permiso', password='x')
        PerfilUsuario.objects.create(usuario=self.usuario_con_permiso, acceso_todas_unidades=True)
        permiso = Permission.objects.get(content_type__app_label='catalogo', codename='acceso_remoto_estacion')
        self.usuario_con_permiso.user_permissions.add(permiso)

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


class CumplimientoViewsTests(TestCase):
    def setUp(self):
        self.usuario = User.objects.create_user(username='u', password='x')
        PerfilUsuario.objects.create(usuario=self.usuario, acceso_todas_unidades=True)
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
        self.client.force_login(self.usuario)
        self.equipo = Activo.objects.create(codigo='CR-DSK-0099', tipo=Activo.Tipo.DESKTOP)
        self.cliente = Colaborador.objects.create(nombre='Ana', cedula='9999')

    def _post_valido(self):
        return self.client.post(reverse('panel:mantenimiento_crear'), {
            'equipos': [self.equipo.pk],
            'cliente': self.cliente.pk,
            'tipo_mantenimiento': 'preventivo',
            'estado_general': EstadoGeneralEquipo.OPERATIVO,
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


class MantenimientoApiCrearTests(TestCase):
    def setUp(self):
        from rest_framework.authtoken.models import Token
        self.tecnico = User.objects.create_user(username='tec', password='x')
        self.token = Token.objects.create(user=self.tecnico)
        self.equipo = Activo.objects.create(codigo='CR-DSK-0100', tipo=Activo.Tipo.DESKTOP)
        self.cliente = Colaborador.objects.create(nombre='Beto', cedula='8888')

    def _payload(self):
        return {
            'equipos': [self.equipo.pk],
            'cliente': self.cliente.pk,
            'tipo_mantenimiento': 'correctivo',
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
        self.client.force_login(self.usuario_mia)

    def test_usuario_de_otro_tenant_no_ve_la_alerta_en_el_listado(self):
        resp = self.client.get(reverse('panel:alertas_lista'))
        self.assertNotContains(resp, 'Privada SG')

    def test_usuario_de_otro_tenant_no_puede_resolverla(self):
        resp = self.client.post(reverse('panel:alerta_resolver', args=[self.alerta.pk]))
        self.assertEqual(resp.status_code, 403)
        self.alerta.refresh_from_db()
        self.assertEqual(self.alerta.estado, Alerta.Estado.ABIERTA)


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
        self.client.force_login(self.usuario_mia)

    def test_usuario_de_otro_tenant_no_ve_la_programacion(self):
        resp = self.client.get(reverse('panel:scripts_programados_lista'))
        self.assertEqual(resp.status_code, 200)
        self.assertNotContains(resp, self.script.nombre)

    def test_form_no_ofrece_unidad_de_negocio_ajena(self):
        resp = self.client.get(reverse('panel:script_programado_crear'))
        self.assertNotContains(resp, '>SG</option>')
        self.assertContains(resp, '>MIA</option>')


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
