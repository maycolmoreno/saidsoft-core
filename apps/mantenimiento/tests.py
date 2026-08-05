from datetime import date, timedelta

from django.contrib.auth.models import User
from django.test import TestCase
from django.utils import timezone

from apps.activos.models import Activo, Bodega, Colaborador, StockBodega, TipoConsumible

from .models import (
    EstadoGeneralEquipo, EventoMantenimiento, Mantenimiento, MantenimientoProgramado, RepuestoUtilizado,
    ResultadoTecnico,
)
from .services import (
    cerrar_mantenimiento, crear_mantenimiento_manual, generar_informe_pdf, registrar_repuesto_utilizado,
)


class CrearMantenimientoManualTests(TestCase):
    def setUp(self):
        self.usuario = User.objects.create_user(username='u', password='x')
        self.tecnico = User.objects.create_user(username='tec', password='x')
        self.equipo = Activo.objects.create(codigo='CR-DSK-0001', tipo=Activo.Tipo.DESKTOP)
        self.cliente = Colaborador.objects.create(nombre='Ana', cedula='0001')

    def _crear(self, **overrides):
        kwargs = dict(
            equipos=[self.equipo], tecnico=self.tecnico, cliente=self.cliente,
            tipo_mantenimiento='preventivo', estado_general=EstadoGeneralEquipo.OPERATIVO,
            descripcion='Revisión', fecha_programada=timezone.now(), usuario=self.usuario,
        )
        kwargs.update(overrides)
        return crear_mantenimiento_manual(**kwargs)

    def test_crea_correctamente(self):
        mantenimiento = self._crear()
        self.assertEqual(mantenimiento.estado_general, EstadoGeneralEquipo.OPERATIVO)
        self.assertEqual(mantenimiento.estado_interno, Mantenimiento.EstadoInterno.PENDIENTE)

    def test_rechaza_si_el_equipo_ya_tiene_uno_pendiente(self):
        self._crear()
        with self.assertRaises(ValueError):
            self._crear()

    def test_rechaza_si_el_equipo_ya_tiene_uno_en_proceso(self):
        mantenimiento = self._crear()
        mantenimiento.estado_interno = Mantenimiento.EstadoInterno.EN_PROCESO
        mantenimiento.save(update_fields=['estado_interno'])
        with self.assertRaises(ValueError):
            self._crear()

    def test_permite_nuevo_si_el_anterior_esta_cerrado(self):
        mantenimiento = self._crear()
        mantenimiento.estado_interno = Mantenimiento.EstadoInterno.CERRADO
        mantenimiento.save(update_fields=['estado_interno'])
        # No debe lanzar.
        self._crear()

    def test_permite_nuevo_si_el_anterior_esta_cancelado(self):
        mantenimiento = self._crear()
        mantenimiento.estado_interno = Mantenimiento.EstadoInterno.CANCELADO
        mantenimiento.save(update_fields=['estado_interno'])
        self._crear()


class CerrarMantenimientoTests(TestCase):
    def setUp(self):
        self.usuario = User.objects.create_user(username='u', password='x')
        self.equipo = Activo.objects.create(codigo='CR-DSK-0002', tipo=Activo.Tipo.DESKTOP)

    def _crear(self, **overrides):
        kwargs = dict(
            equipos=[self.equipo], tecnico=None, tipo_mantenimiento='correctivo',
            estado_general=EstadoGeneralEquipo.NO_OPERATIVO, descripcion='Falla',
            fecha_programada=timezone.now(), usuario=self.usuario,
        )
        kwargs.update(overrides)
        return crear_mantenimiento_manual(**kwargs)

    def test_irreparable_marca_baja_recomendada(self):
        mantenimiento = self._crear()
        cerrar_mantenimiento(
            mantenimiento=mantenimiento, resultado_tecnico=ResultadoTecnico.IRREPARABLE, usuario=self.usuario,
        )
        self.equipo.refresh_from_db()
        self.assertTrue(self.equipo.baja_recomendada)

    def test_requiere_baja_marca_baja_recomendada(self):
        mantenimiento = self._crear()
        cerrar_mantenimiento(
            mantenimiento=mantenimiento, resultado_tecnico=ResultadoTecnico.REQUIERE_BAJA, usuario=self.usuario,
        )
        self.equipo.refresh_from_db()
        self.assertTrue(self.equipo.baja_recomendada)

    def test_reparado_no_marca_baja_recomendada(self):
        mantenimiento = self._crear()
        cerrar_mantenimiento(
            mantenimiento=mantenimiento, resultado_tecnico=ResultadoTecnico.REPARADO, usuario=self.usuario,
        )
        self.equipo.refresh_from_db()
        self.assertFalse(self.equipo.baja_recomendada)

    def test_guarda_tiempo_real_minutos(self):
        mantenimiento = self._crear()
        cerrar_mantenimiento(
            mantenimiento=mantenimiento, resultado_tecnico=ResultadoTecnico.REPARADO, usuario=self.usuario,
            tiempo_real_minutos=45,
        )
        mantenimiento.refresh_from_db()
        self.assertEqual(mantenimiento.tiempo_real_minutos, 45)

    def test_cerrar_con_plan_preventivo_recalcula_proxima_fecha(self):
        tecnico = User.objects.create_user(username='tec2', password='x')
        programado = MantenimientoProgramado.objects.create(
            equipo=self.equipo, tecnico=tecnico, frecuencia_dias=30, fecha_proximo=date(2026, 1, 1),
        )
        mantenimiento = self._crear(mantenimiento_programado=programado)

        cerrar_mantenimiento(
            mantenimiento=mantenimiento, resultado_tecnico=ResultadoTecnico.REPARADO, usuario=self.usuario,
        )

        programado.refresh_from_db()
        hoy = timezone.now().date()
        self.assertEqual(programado.fecha_ultimo, hoy)
        self.assertEqual(programado.fecha_proximo, hoy + timedelta(days=30))


class GenerarMantenimientosProgramadosTaskTests(TestCase):
    """CELERY_TASK_ALWAYS_EAGER=True hace que .delay() corra sincrónico en el test."""

    def test_delay_genera_el_mantenimiento_vencido(self):
        from apps.mantenimiento.tasks import generar_mantenimientos_programados_task

        usuario = User.objects.create_user(username='u_task_mant', password='x')
        equipo = Activo.objects.create(codigo='CR-DSK-0099', tipo=Activo.Tipo.DESKTOP)
        programado = MantenimientoProgramado.objects.create(
            equipo=equipo, tecnico=usuario, frecuencia_dias=30, fecha_proximo=timezone.now().date(),
        )

        resultado = generar_mantenimientos_programados_task.delay()

        self.assertTrue(Mantenimiento.objects.filter(mantenimiento_programado=programado).exists())
        self.assertIn('1 mantenimiento', resultado.get())


class GenerarInformePdfTests(TestCase):
    """Fase 2 IT Operations Platform: primer caso real de PDF/reporte pesado movido a
    Celery (ver apps/mantenimiento/tasks.py::generar_informe_pdf_task)."""

    def setUp(self):
        self.usuario = User.objects.create_user(username='u_pdf', password='x')
        self.equipo = Activo.objects.create(codigo='CR-DSK-0100', tipo=Activo.Tipo.DESKTOP)
        self.mantenimiento = crear_mantenimiento_manual(
            equipos=[self.equipo], tecnico=self.usuario, cliente=None, tipo_mantenimiento='preventivo',
            estado_general=EstadoGeneralEquipo.OPERATIVO, descripcion='Revisión',
            fecha_programada=timezone.now(), usuario=self.usuario,
        )

    def test_genera_pdf_valido_y_registra_evento(self):
        generar_informe_pdf(mantenimiento=self.mantenimiento)

        self.mantenimiento.refresh_from_db()
        self.assertTrue(self.mantenimiento.informe_pdf.name)
        self.assertIsNotNone(self.mantenimiento.informe_pdf_generado_en)
        contenido = self.mantenimiento.informe_pdf.read()
        self.assertTrue(contenido.startswith(b'%PDF'))
        self.assertTrue(
            EventoMantenimiento.objects.filter(
                mantenimiento=self.mantenimiento, tipo_evento=EventoMantenimiento.TipoEvento.INFORME_GENERADO,
            ).exists(),
        )

    def tearDown(self):
        self.mantenimiento.refresh_from_db()
        if self.mantenimiento.informe_pdf:
            self.mantenimiento.informe_pdf.delete(save=False)


class GenerarInformePdfTaskTests(TestCase):
    """CELERY_TASK_ALWAYS_EAGER=True hace que .delay() corra sincrónico en el test."""

    def setUp(self):
        self.usuario = User.objects.create_user(username='u_pdf_task', password='x')
        self.equipo = Activo.objects.create(codigo='CR-DSK-0101', tipo=Activo.Tipo.DESKTOP)
        self.mantenimiento = crear_mantenimiento_manual(
            equipos=[self.equipo], tecnico=self.usuario, cliente=None, tipo_mantenimiento='preventivo',
            estado_general=EstadoGeneralEquipo.OPERATIVO, descripcion='Revisión',
            fecha_programada=timezone.now(), usuario=self.usuario,
        )

    def test_delay_genera_el_pdf(self):
        from apps.mantenimiento.tasks import generar_informe_pdf_task

        resultado = generar_informe_pdf_task.delay(self.mantenimiento.pk)

        self.mantenimiento.refresh_from_db()
        self.assertTrue(self.mantenimiento.informe_pdf.name)
        self.assertIn(str(self.mantenimiento.pk), resultado.get())

    def tearDown(self):
        self.mantenimiento.refresh_from_db()
        if self.mantenimiento.informe_pdf:
            self.mantenimiento.informe_pdf.delete(save=False)


class RegistrarRepuestoUtilizadoTests(TestCase):
    """Fase 5 IT Operations Platform: repuestos/costo en Mantenimiento."""

    def setUp(self):
        self.usuario = User.objects.create_user(username='u_repuesto', password='x')
        self.equipo = Activo.objects.create(codigo='CR-DSK-0200', tipo=Activo.Tipo.DESKTOP)
        self.mantenimiento = crear_mantenimiento_manual(
            equipos=[self.equipo], tecnico=self.usuario, cliente=None, tipo_mantenimiento='correctivo',
            estado_general=EstadoGeneralEquipo.NO_OPERATIVO, descripcion='Falla',
            fecha_programada=timezone.now(), usuario=self.usuario,
        )
        self.bodega = Bodega.objects.create(codigo='BOD01')
        self.tipo_consumible = TipoConsumible.objects.create(codigo='FUENTE', nombre='Fuente de poder')
        StockBodega.objects.create(bodega=self.bodega, tipo_consumible=self.tipo_consumible, cantidad=3)

    def test_registrar_con_bodega_descuenta_stock_y_registra_evento(self):
        repuesto = registrar_repuesto_utilizado(
            mantenimiento=self.mantenimiento, tipo_consumible=self.tipo_consumible, cantidad=2,
            bodega=self.bodega, costo_unitario=15, usuario=self.usuario,
        )
        self.assertEqual(repuesto.costo_total, 30)

        stock = StockBodega.objects.get(bodega=self.bodega, tipo_consumible=self.tipo_consumible)
        self.assertEqual(stock.cantidad, 1)

        self.assertTrue(
            EventoMantenimiento.objects.filter(
                mantenimiento=self.mantenimiento, tipo_evento=EventoMantenimiento.TipoEvento.REPUESTO_REGISTRADO,
            ).exists(),
        )

    def test_registrar_sin_bodega_no_toca_stock(self):
        registrar_repuesto_utilizado(
            mantenimiento=self.mantenimiento, tipo_consumible=self.tipo_consumible, cantidad=1,
            costo_unitario=5, usuario=self.usuario,
        )
        stock = StockBodega.objects.get(bodega=self.bodega, tipo_consumible=self.tipo_consumible)
        self.assertEqual(stock.cantidad, 3)

    def test_stock_insuficiente_no_crea_repuesto(self):
        with self.assertRaises(ValueError):
            registrar_repuesto_utilizado(
                mantenimiento=self.mantenimiento, tipo_consumible=self.tipo_consumible, cantidad=10,
                bodega=self.bodega, usuario=self.usuario,
            )
        self.assertFalse(RepuestoUtilizado.objects.filter(mantenimiento=self.mantenimiento).exists())

    def test_costo_total_repuestos_suma_todos_los_registrados(self):
        registrar_repuesto_utilizado(
            mantenimiento=self.mantenimiento, tipo_consumible=self.tipo_consumible, cantidad=1,
            costo_unitario=10, usuario=self.usuario,
        )
        otro_tipo = TipoConsumible.objects.create(codigo='CABLE', nombre='Cable SATA')
        registrar_repuesto_utilizado(
            mantenimiento=self.mantenimiento, tipo_consumible=otro_tipo, cantidad=2,
            costo_unitario=5, usuario=self.usuario,
        )
        self.assertEqual(self.mantenimiento.costo_total_repuestos, 20)

    def test_informe_pdf_incluye_repuestos(self):
        registrar_repuesto_utilizado(
            mantenimiento=self.mantenimiento, tipo_consumible=self.tipo_consumible, cantidad=1,
            costo_unitario=15, usuario=self.usuario,
        )
        generar_informe_pdf(mantenimiento=self.mantenimiento)

        self.mantenimiento.refresh_from_db()
        self.assertTrue(self.mantenimiento.informe_pdf.read().startswith(b'%PDF'))

    def tearDown(self):
        self.mantenimiento.refresh_from_db()
        if self.mantenimiento.informe_pdf:
            self.mantenimiento.informe_pdf.delete(save=False)
