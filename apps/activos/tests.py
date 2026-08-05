from django.contrib.auth.models import Group, User
from django.core.management import call_command
from django.test import TestCase

from apps.catalogo.models import UnidadNegocio

from .models import Activo, Bodega, Cargo, Colaborador, Departamento, EventoActivo, StockBodega, TipoConsumible
from .services import registrar_asignacion, registrar_salida_stock


class SeedPermisosTests(TestCase):
    def test_administrador_incluye_acceso_remoto_estacion(self):
        call_command('seed_permisos')
        grupo = Group.objects.get(name='Administrador')
        self.assertTrue(
            grupo.permissions.filter(
                content_type__app_label='catalogo', codename='acceso_remoto_estacion',
            ).exists(),
        )

    def test_auditor_incluye_supervision_auditoria_pero_no_acceso_remoto(self):
        call_command('seed_permisos')
        grupo = Group.objects.get(name='Auditor')
        self.assertTrue(
            grupo.permissions.filter(
                content_type__app_label='catalogo', codename='supervision_auditoria_estacion',
            ).exists(),
        )
        self.assertFalse(
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

    def test_colaborador_con_cargo_no_revienta_al_serializar_el_evento(self):
        """colaborador.cargo es FK a Cargo (no serializable a JSON tal cual) — antes
        se guardaba el objeto directo en el detalle y reventaba con TypeError en
        cuanto el colaborador tuviera un cargo asignado."""
        departamento = Departamento.objects.create(nombre='TI')
        cargo = Cargo.objects.create(nombre='Técnico de soporte', departamento=departamento)
        self.colaborador.cargo = cargo
        self.colaborador.save(update_fields=['cargo'])

        registrar_asignacion(
            activo=self.activo, colaborador=self.colaborador,
            estado_fisico_entrega=Activo.EstadoFisico.BUENO, usuario=self.usuario,
        )
        evento = EventoActivo.objects.get(activo=self.activo, tipo_evento=EventoActivo.TipoEvento.ASIGNACION)
        self.assertEqual(evento.detalle['cargo'], 'Técnico de soporte')

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


class RegistrarSalidaStockTests(TestCase):
    def setUp(self):
        self.bodega = Bodega.objects.create(codigo='BOD01')
        self.tipo_consumible = TipoConsumible.objects.create(codigo='MOUSE', nombre='Mouse USB')
        StockBodega.objects.create(bodega=self.bodega, tipo_consumible=self.tipo_consumible, cantidad=5)

    def test_descuenta_stock_disponible(self):
        stock = registrar_salida_stock(bodega=self.bodega, tipo_consumible=self.tipo_consumible, cantidad=3)
        self.assertEqual(stock.cantidad, 2)

    def test_rechaza_cantidad_mayor_al_disponible(self):
        with self.assertRaises(ValueError):
            registrar_salida_stock(bodega=self.bodega, tipo_consumible=self.tipo_consumible, cantidad=6)
        stock = StockBodega.objects.get(bodega=self.bodega, tipo_consumible=self.tipo_consumible)
        self.assertEqual(stock.cantidad, 5)

    def test_rechaza_cantidad_cero_o_negativa(self):
        with self.assertRaises(ValueError):
            registrar_salida_stock(bodega=self.bodega, tipo_consumible=self.tipo_consumible, cantidad=0)
