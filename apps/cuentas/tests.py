from django.contrib.auth.models import User
from django.core.exceptions import PermissionDenied
from django.test import RequestFactory, TestCase

from apps.catalogo.models import UnidadNegocio
from apps.scripts.models import Script, TipoScript

from .models import PerfilUsuario
from .services import (
    SESSION_KEY_UNIDAD_ACTIVA, scope_opcional_por_unidad_negocio, scope_por_unidad_negocio,
    scope_scripts_visibles, unidad_negocio_activa, unidades_negocio_visibles, usuario_puede_ver,
    usuario_tiene_acceso_total, verificar_acceso,
)


class UsuarioTieneAccesoTotalTests(TestCase):
    def test_superusuario_tiene_acceso_total(self):
        admin = User.objects.create_user(username='admin', password='x', is_superuser=True)
        self.assertTrue(usuario_tiene_acceso_total(admin))

    def test_perfil_con_acceso_todas_unidades(self):
        usuario = User.objects.create_user(username='u', password='x')
        PerfilUsuario.objects.create(usuario=usuario, acceso_todas_unidades=True)
        self.assertTrue(usuario_tiene_acceso_total(usuario))

    def test_usuario_normal_no_tiene_acceso_total(self):
        usuario = User.objects.create_user(username='u2', password='x')
        self.assertFalse(usuario_tiene_acceso_total(usuario))

    def test_usuario_sin_perfil_no_tiene_acceso_total(self):
        usuario = User.objects.create_user(username='u3', password='x')
        self.assertFalse(usuario_tiene_acceso_total(usuario))

    def test_none_no_tiene_acceso_total(self):
        self.assertFalse(usuario_tiene_acceso_total(None))


class UnidadesNegocioVisiblesTests(TestCase):
    def setUp(self):
        self.sg = UnidadNegocio.objects.get(codigo='SG')
        self.mia = UnidadNegocio.objects.get(codigo='MIA')

    def test_acceso_total_ve_todas(self):
        admin = User.objects.create_user(username='admin', password='x', is_superuser=True)
        self.assertEqual(set(unidades_negocio_visibles(admin)), {self.sg, self.mia})

    def test_usuario_restringido_ve_solo_las_suyas(self):
        usuario = User.objects.create_user(username='u', password='x')
        PerfilUsuario.objects.create(usuario=usuario).unidades_negocio.add(self.mia)
        self.assertEqual(set(unidades_negocio_visibles(usuario)), {self.mia})

    def test_usuario_sin_perfil_no_ve_ninguna(self):
        usuario = User.objects.create_user(username='u2', password='x')
        self.assertEqual(list(unidades_negocio_visibles(usuario)), [])


class UsuarioPuedeVerYVerificarAccesoTests(TestCase):
    def setUp(self):
        self.sg = UnidadNegocio.objects.get(codigo='SG')
        self.mia = UnidadNegocio.objects.get(codigo='MIA')
        self.usuario = User.objects.create_user(username='u', password='x')
        PerfilUsuario.objects.create(usuario=self.usuario).unidades_negocio.add(self.mia)

    def test_puede_ver_la_suya(self):
        self.assertTrue(usuario_puede_ver(self.usuario, self.mia))

    def test_no_puede_ver_una_ajena(self):
        self.assertFalse(usuario_puede_ver(self.usuario, self.sg))

    def test_objeto_sin_unidad_negocio_es_visible_para_cualquiera(self):
        self.assertTrue(usuario_puede_ver(self.usuario, None))

    def test_verificar_acceso_lanza_permission_denied_para_unidad_ajena(self):
        with self.assertRaises(PermissionDenied):
            verificar_acceso(self.usuario, self.sg)

    def test_verificar_acceso_no_lanza_para_la_propia(self):
        verificar_acceso(self.usuario, self.mia)  # no debe lanzar

    def test_verificar_acceso_no_lanza_para_none(self):
        verificar_acceso(self.usuario, None)  # no debe lanzar


class ScopePorUnidadNegocioTests(TestCase):
    def setUp(self):
        self.sg = UnidadNegocio.objects.get(codigo='SG')
        self.mia = UnidadNegocio.objects.get(codigo='MIA')
        self.usuario = User.objects.create_user(username='u', password='x')
        PerfilUsuario.objects.create(usuario=self.usuario).unidades_negocio.add(self.mia)
        creador = User.objects.create_user(username='creador', password='x')
        self.script_sg = Script.objects.create(
            nombre='Privado SG', tipo=TipoScript.POWERSHELL, contenido='echo 1',
            unidad_negocio=self.sg, creado_por=creador,
        )
        self.script_mia = Script.objects.create(
            nombre='Privado MIA', tipo=TipoScript.POWERSHELL, contenido='echo 1',
            unidad_negocio=self.mia, creado_por=creador,
        )
        self.script_compartido = Script.objects.create(
            nombre='Compartido', tipo=TipoScript.POWERSHELL, contenido='echo 1', creado_por=creador,
        )

    def test_scope_por_unidad_negocio_excluye_ajenos_y_compartidos(self):
        visibles = scope_por_unidad_negocio(Script.objects.all(), self.usuario, 'unidad_negocio')
        self.assertEqual(set(visibles), {self.script_mia})

    def test_scope_opcional_incluye_compartidos_ademas_de_los_propios(self):
        visibles = scope_opcional_por_unidad_negocio(Script.objects.all(), self.usuario, 'unidad_negocio')
        self.assertEqual(set(visibles), {self.script_mia, self.script_compartido})

    def test_scope_scripts_visibles_mismo_criterio_que_scope_opcional(self):
        visibles = scope_scripts_visibles(Script.objects.all(), self.usuario)
        self.assertEqual(set(visibles), {self.script_mia, self.script_compartido})

    def test_acceso_total_no_filtra(self):
        admin = User.objects.create_user(username='admin', password='x', is_superuser=True)
        visibles = scope_por_unidad_negocio(Script.objects.all(), admin, 'unidad_negocio')
        self.assertEqual(set(visibles), {self.script_sg, self.script_mia, self.script_compartido})


class UnidadNegocioActivaTests(TestCase):
    def setUp(self):
        self.sg = UnidadNegocio.objects.get(codigo='SG')
        self.mia = UnidadNegocio.objects.get(codigo='MIA')
        self.usuario = User.objects.create_user(username='u', password='x')
        perfil = PerfilUsuario.objects.create(usuario=self.usuario)
        perfil.unidades_negocio.add(self.sg, self.mia)
        self.factory = RequestFactory()

    def _request_con_sesion(self, unidad_id=None):
        request = self.factory.get('/')
        request.user = self.usuario
        from django.contrib.sessions.backends.db import SessionStore
        request.session = SessionStore()
        if unidad_id is not None:
            request.session[SESSION_KEY_UNIDAD_ACTIVA] = unidad_id
        return request

    def test_sin_nada_en_sesion_devuelve_none(self):
        request = self._request_con_sesion()
        self.assertIsNone(unidad_negocio_activa(request))

    def test_unidad_valida_en_sesion_se_devuelve(self):
        request = self._request_con_sesion(self.mia.pk)
        self.assertEqual(unidad_negocio_activa(request), self.mia)

    def test_unidad_no_visible_en_sesion_se_ignora(self):
        otro_usuario = User.objects.create_user(username='u2', password='x')
        PerfilUsuario.objects.create(usuario=otro_usuario).unidades_negocio.add(self.sg)
        request = self.factory.get('/')
        request.user = otro_usuario
        from django.contrib.sessions.backends.db import SessionStore
        request.session = SessionStore()
        request.session[SESSION_KEY_UNIDAD_ACTIVA] = self.mia.pk  # otro_usuario no tiene acceso a MIA
        self.assertIsNone(unidad_negocio_activa(request))
