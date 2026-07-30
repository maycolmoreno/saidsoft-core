from datetime import date

from django.contrib.auth.models import Permission, User
from django.test import TestCase
from django.urls import reverse

from apps.activos.models import Activo, Colaborador
from apps.auditoria.models import EventoAuditoria
from apps.catalogo.models import Estacion, Farmacia, Grupo, UnidadNegocio
from apps.cumplimiento.models import (
    ActividadCumplimiento, ResultadoCumplimientoEstacion, TipoObjetivoCumplimiento,
)
from apps.mantenimiento.models import EstadoGeneralEquipo, Mantenimiento


class EstacionMeshCentralTests(TestCase):
    def setUp(self):
        grupo = Grupo.objects.create(codigo='TRX001')
        farmacia = Farmacia.objects.create(codigo='ML001', grupo=grupo)
        self.estacion = Estacion.objects.create(
            codigo='ML001-A', farmacia=farmacia,
            estado_aprobacion=Estacion.EstadoAprobacion.APROBADA,
            estado_conexion=Estacion.EstadoConexion.ONLINE,
        )

        self.usuario_sin_permiso = User.objects.create_user(username='sin_permiso', password='x')

        self.usuario_con_permiso = User.objects.create_user(username='con_permiso', password='x')
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


class CumplimientoViewsTests(TestCase):
    def setUp(self):
        self.usuario = User.objects.create_user(username='u', password='x')
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
