from django.contrib.auth.models import Group, User
from django.core.management import call_command
from django.test import TestCase

from apps.catalogo.models import UnidadNegocio

from .models import Activo, Colaborador
from .services import registrar_asignacion


class SeedPermisosTests(TestCase):
    def test_administrador_incluye_acceso_remoto_estacion(self):
        call_command('seed_permisos')
        grupo = Group.objects.get(name='Administrador')
        self.assertTrue(
            grupo.permissions.filter(
                content_type__app_label='catalogo', codename='acceso_remoto_estacion',
            ).exists(),
        )


class RegistrarAsignacionTests(TestCase):
    def setUp(self):
        self.usuario = User.objects.create_user(username='u', password='x')
        self.mia = UnidadNegocio.objects.get(codigo='MIA')
        self.colaborador = Colaborador.objects.create(nombre='Ana', cedula='0001', unidad_negocio=self.mia)
        self.activo = Activo.objects.create(codigo='CR-DSK-0001', tipo=Activo.Tipo.DESKTOP)

    def test_activo_hereda_unidad_negocio_del_colaborador(self):
        registrar_asignacion(
            activo=self.activo, colaborador=self.colaborador,
            estado_fisico_entrega=Activo.EstadoFisico.BUENO, usuario=self.usuario,
        )
        self.activo.refresh_from_db()
        self.assertEqual(self.activo.unidad_negocio, self.mia)

    def test_devolver_no_limpia_la_unidad_negocio_heredada(self):
        from .services import registrar_devolucion
        registrar_asignacion(
            activo=self.activo, colaborador=self.colaborador,
            estado_fisico_entrega=Activo.EstadoFisico.BUENO, usuario=self.usuario,
        )
        registrar_devolucion(
            activo=self.activo, estado_fisico_devolucion=Activo.EstadoFisico.BUENO,
            requiere_reparacion=False, usuario=self.usuario,
        )
        self.activo.refresh_from_db()
        self.assertEqual(self.activo.unidad_negocio, self.mia)
