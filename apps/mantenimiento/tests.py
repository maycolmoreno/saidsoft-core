from datetime import date, timedelta

from django.contrib.auth.models import User
from django.test import TestCase
from django.utils import timezone

from apps.activos.models import Activo, Bodega, Colaborador, StockBodega, TipoConsumible

from .models import (
    EstadoGeneralEquipo, EventoMantenimiento, Mantenimiento, MantenimientoProgramado, Notificacion,
    RepuestoUtilizado, ResultadoTecnico, TipoMantenimiento, TipoOrigenMantenimiento,
)
from .services import (
    cerrar_mantenimiento, crear_mantenimiento_manual, generar_informe_pdf, iniciar_reparacion_desde_activo,
    mantenimientos_atrasados, mantenimientos_programados_por_vencer, notificar_mantenimientos_proximos_y_atrasados,
    registrar_repuesto_utilizado,
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
            tipo_mantenimiento=TipoMantenimiento.objects.get(codigo='preventivo'), estado_general=EstadoGeneralEquipo.OPERATIVO,
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
            equipos=[self.equipo], tecnico=None, tipo_mantenimiento=TipoMantenimiento.objects.get(codigo='correctivo'),
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
        hoy = timezone.localdate()
        self.assertEqual(programado.fecha_ultimo, hoy)
        self.assertEqual(programado.fecha_proximo, hoy + timedelta(days=30))

    def test_reparado_devuelve_a_bodega_un_equipo_en_reparacion(self):
        self.equipo.estado = Activo.Estado.EN_REPARACION
        self.equipo.save(update_fields=['estado'])
        mantenimiento = self._crear(estado_general=EstadoGeneralEquipo.OPERATIVO)

        cerrar_mantenimiento(
            mantenimiento=mantenimiento, resultado_tecnico=ResultadoTecnico.REPARADO, usuario=self.usuario,
        )

        self.equipo.refresh_from_db()
        self.assertEqual(self.equipo.estado, Activo.Estado.EN_BODEGA)
        self.assertEqual(self.equipo.estado_fisico_actual, Activo.EstadoFisico.BUENO)

    def test_estado_general_al_cerrar_decide_el_estado_fisico_de_vuelta(self):
        self.equipo.estado = Activo.Estado.EN_REPARACION
        self.equipo.save(update_fields=['estado'])
        mantenimiento = self._crear(estado_general=EstadoGeneralEquipo.OPERATIVO)

        cerrar_mantenimiento(
            mantenimiento=mantenimiento, resultado_tecnico=ResultadoTecnico.REPARADO, usuario=self.usuario,
            estado_general=EstadoGeneralEquipo.REQUIERE_REVISION,
        )

        self.equipo.refresh_from_db()
        self.assertEqual(self.equipo.estado, Activo.Estado.EN_BODEGA)
        self.assertEqual(self.equipo.estado_fisico_actual, Activo.EstadoFisico.REGULAR)

    def test_resultado_todavia_roto_no_devuelve_a_bodega(self):
        self.equipo.estado = Activo.Estado.EN_REPARACION
        self.equipo.save(update_fields=['estado'])
        mantenimiento = self._crear()

        cerrar_mantenimiento(
            mantenimiento=mantenimiento, resultado_tecnico=ResultadoTecnico.REQUIERE_REPUESTO, usuario=self.usuario,
        )

        self.equipo.refresh_from_db()
        self.assertEqual(self.equipo.estado, Activo.Estado.EN_REPARACION)

    def test_no_toca_el_estado_de_un_equipo_que_no_estaba_en_reparacion(self):
        # Mantenimiento preventivo sobre un equipo asignado -- cerrarlo no debe
        # "devolverlo a bodega" de la nada.
        self.equipo.estado = Activo.Estado.ASIGNADO
        self.equipo.save(update_fields=['estado'])
        mantenimiento = self._crear()

        cerrar_mantenimiento(
            mantenimiento=mantenimiento, resultado_tecnico=ResultadoTecnico.REPARADO, usuario=self.usuario,
        )

        self.equipo.refresh_from_db()
        self.assertEqual(self.equipo.estado, Activo.Estado.ASIGNADO)

    def test_requiere_baja_no_devuelve_a_bodega(self):
        self.equipo.estado = Activo.Estado.EN_REPARACION
        self.equipo.save(update_fields=['estado'])
        mantenimiento = self._crear()

        cerrar_mantenimiento(
            mantenimiento=mantenimiento, resultado_tecnico=ResultadoTecnico.REQUIERE_BAJA, usuario=self.usuario,
        )

        self.equipo.refresh_from_db()
        self.assertEqual(self.equipo.estado, Activo.Estado.EN_REPARACION)
        self.assertTrue(self.equipo.baja_recomendada)


class IniciarReparacionDesdeActivoTests(TestCase):
    def setUp(self):
        self.usuario = User.objects.create_user(username='u', password='x')
        self.colaborador = Colaborador.objects.create(nombre='Ana', cedula='0002')
        self.equipo = Activo.objects.create(
            codigo='CR-DSK-0003', tipo=Activo.Tipo.DESKTOP,
            estado=Activo.Estado.ASIGNADO, colaborador_actual=self.colaborador,
        )

    def test_envia_a_reparacion_y_abre_mantenimiento_vinculado(self):
        from apps.activos.models import MotivoReparacion

        mantenimiento = iniciar_reparacion_desde_activo(
            activo=self.equipo, motivo=MotivoReparacion.FALLA_TECNICA, detalle_motivo='No enciende',
            usuario=self.usuario,
        )

        self.equipo.refresh_from_db()
        self.assertEqual(self.equipo.estado, Activo.Estado.EN_REPARACION)
        self.assertIsNone(self.equipo.colaborador_actual)

        self.assertEqual(mantenimiento.tipo_origen, TipoOrigenMantenimiento.MANUAL)
        self.assertEqual(mantenimiento.cliente_id, self.colaborador.pk)  # capturado ANTES de limpiarlo
        self.assertEqual(mantenimiento.descripcion, 'No enciende')
        self.assertTrue(mantenimiento.equipos.filter(equipo=self.equipo, es_principal=True).exists())

    def test_rechaza_si_el_activo_ya_esta_dado_de_baja(self):
        from apps.activos.models import MotivoReparacion

        self.equipo.estado = Activo.Estado.DADO_DE_BAJA
        self.equipo.save(update_fields=['estado'])

        with self.assertRaises(ValueError):
            iniciar_reparacion_desde_activo(
                activo=self.equipo, motivo=MotivoReparacion.FALLA_TECNICA, detalle_motivo='',
                usuario=self.usuario,
            )
        # No debe quedar un Mantenimiento huérfano si registrar_envio_reparacion falla.
        self.assertEqual(Mantenimiento.objects.count(), 0)


class NotificarVencimientoTests(TestCase):
    """Notificacion existía desde antes pero nada la poblaba -- estas pruebas cubren
    los dos avisos nuevos (plan próximo a vencer, mantenimiento atrasado) y su
    idempotencia diaria (22-ago-2026)."""

    def setUp(self):
        self.tecnico = User.objects.create_user(username='tec', password='x')
        self.equipo = Activo.objects.create(codigo='CR-DSK-0010', tipo=Activo.Tipo.DESKTOP)

    def test_detecta_plan_proximo_a_vencer_dentro_de_la_ventana(self):
        hoy = timezone.localdate()
        dentro = MantenimientoProgramado.objects.create(
            equipo=self.equipo, tecnico=self.tecnico, frecuencia_dias=90, fecha_proximo=hoy + timedelta(days=5),
        )
        fuera = MantenimientoProgramado.objects.create(
            equipo=Activo.objects.create(codigo='CR-DSK-0011', tipo=Activo.Tipo.DESKTOP),
            tecnico=self.tecnico, frecuencia_dias=90, fecha_proximo=hoy + timedelta(days=20),
        )
        resultado = list(mantenimientos_programados_por_vencer(dias=7))
        self.assertIn(dentro, resultado)
        self.assertNotIn(fuera, resultado)

    def test_plan_inactivo_no_se_detecta(self):
        hoy = timezone.localdate()
        MantenimientoProgramado.objects.create(
            equipo=self.equipo, tecnico=self.tecnico, frecuencia_dias=90,
            fecha_proximo=hoy + timedelta(days=2), activo=False,
        )
        self.assertEqual(mantenimientos_programados_por_vencer().count(), 0)

    def test_detecta_mantenimiento_atrasado(self):
        viejo = crear_mantenimiento_manual(
            equipos=[self.equipo], tecnico=self.tecnico, tipo_mantenimiento=TipoMantenimiento.objects.get(codigo='correctivo'),
            descripcion='Falla', fecha_programada=timezone.now() - timedelta(days=10), usuario=self.tecnico,
        )
        reciente = crear_mantenimiento_manual(
            equipos=[Activo.objects.create(codigo='CR-DSK-0012', tipo=Activo.Tipo.DESKTOP)],
            tecnico=self.tecnico, tipo_mantenimiento=TipoMantenimiento.objects.get(codigo='correctivo'), descripcion='Falla',
            fecha_programada=timezone.now() - timedelta(days=1), usuario=self.tecnico,
        )
        resultado = list(mantenimientos_atrasados(dias_gracia=3))
        self.assertIn(viejo, resultado)
        self.assertNotIn(reciente, resultado)

    def test_mantenimiento_cerrado_no_se_considera_atrasado(self):
        mantenimiento = crear_mantenimiento_manual(
            equipos=[self.equipo], tecnico=self.tecnico, tipo_mantenimiento=TipoMantenimiento.objects.get(codigo='correctivo'),
            descripcion='Falla', fecha_programada=timezone.now() - timedelta(days=10), usuario=self.tecnico,
        )
        cerrar_mantenimiento(mantenimiento=mantenimiento, resultado_tecnico=ResultadoTecnico.REPARADO, usuario=self.tecnico)
        self.assertEqual(mantenimientos_atrasados(dias_gracia=3).count(), 0)

    def test_notificar_crea_avisos_para_ambos_casos(self):
        hoy = timezone.localdate()
        MantenimientoProgramado.objects.create(
            equipo=self.equipo, tecnico=self.tecnico, frecuencia_dias=90, fecha_proximo=hoy + timedelta(days=3),
        )
        crear_mantenimiento_manual(
            equipos=[Activo.objects.create(codigo='CR-DSK-0013', tipo=Activo.Tipo.DESKTOP)],
            tecnico=self.tecnico, tipo_mantenimiento=TipoMantenimiento.objects.get(codigo='correctivo'), descripcion='Falla',
            fecha_programada=timezone.now() - timedelta(days=10), usuario=self.tecnico,
        )
        resultado = notificar_mantenimientos_proximos_y_atrasados()
        self.assertEqual(resultado, {'proximos': 1, 'atrasados': 1})
        self.assertEqual(Notificacion.objects.filter(usuario=self.tecnico).count(), 2)

    def test_no_duplica_el_aviso_el_mismo_dia(self):
        hoy = timezone.localdate()
        MantenimientoProgramado.objects.create(
            equipo=self.equipo, tecnico=self.tecnico, frecuencia_dias=90, fecha_proximo=hoy + timedelta(days=3),
        )
        notificar_mantenimientos_proximos_y_atrasados()
        resultado = notificar_mantenimientos_proximos_y_atrasados()
        self.assertEqual(resultado['proximos'], 0)
        self.assertEqual(Notificacion.objects.count(), 1)

    def test_sin_tecnico_asignado_no_falla_ni_notifica(self):
        MantenimientoProgramado.objects.create(
            equipo=self.equipo, tecnico=self.tecnico, frecuencia_dias=90,
            fecha_proximo=timezone.localdate() + timedelta(days=3),
        )
        # Mantenimiento manual sin técnico asignado.
        crear_mantenimiento_manual(
            equipos=[Activo.objects.create(codigo='CR-DSK-0014', tipo=Activo.Tipo.DESKTOP)],
            tecnico=None, tipo_mantenimiento=TipoMantenimiento.objects.get(codigo='correctivo'), descripcion='Falla',
            fecha_programada=timezone.now() - timedelta(days=10), usuario=self.tecnico,
        )
        resultado = notificar_mantenimientos_proximos_y_atrasados()
        self.assertEqual(resultado['atrasados'], 0)  # el atrasado sin técnico se omite


class GenerarMantenimientosProgramadosTaskTests(TestCase):
    """CELERY_TASK_ALWAYS_EAGER=True hace que .delay() corra sincrónico en el test."""

    def test_delay_genera_el_mantenimiento_vencido(self):
        from apps.mantenimiento.tasks import generar_mantenimientos_programados_task

        usuario = User.objects.create_user(username='u_task_mant', password='x')
        equipo = Activo.objects.create(codigo='CR-DSK-0099', tipo=Activo.Tipo.DESKTOP)
        programado = MantenimientoProgramado.objects.create(
            equipo=equipo, tecnico=usuario, frecuencia_dias=30, fecha_proximo=timezone.localdate(),
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
            equipos=[self.equipo], tecnico=self.usuario, cliente=None, tipo_mantenimiento=TipoMantenimiento.objects.get(codigo='preventivo'),
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
            equipos=[self.equipo], tecnico=self.usuario, cliente=None, tipo_mantenimiento=TipoMantenimiento.objects.get(codigo='preventivo'),
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
            equipos=[self.equipo], tecnico=self.usuario, cliente=None, tipo_mantenimiento=TipoMantenimiento.objects.get(codigo='correctivo'),
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


class ResumenMantenimientoPeriodoTests(TestCase):
    """KPIs del resumen por cliente (docs/proceso-mantenimiento-ti.md, brecha #2:
    dashboard/reportes -- 23-ago-2026). Todo calculado al vuelo, sin tablas nuevas."""

    def setUp(self):
        from apps.catalogo.models import UnidadNegocio

        self.mia = UnidadNegocio.objects.get(codigo='MIA')
        self.usuario = User.objects.create_user(username='u_kpi', password='x')
        self.colaborador = Colaborador.objects.create(nombre='Ana', cedula='9101', unidad_negocio=self.mia)
        self.equipo = Activo.objects.create(codigo='CR-DSK-0300', tipo=Activo.Tipo.DESKTOP, unidad_negocio=self.mia)
        self.desde = timezone.now() - timedelta(days=30)
        self.hasta = timezone.now() + timedelta(days=1)

    def _kpis(self):
        from apps.mantenimiento.services import resumen_mantenimiento_periodo
        return resumen_mantenimiento_periodo(self.mia, self.desde, self.hasta)

    def test_cuenta_total_y_cerrados_del_periodo(self):
        crear_mantenimiento_manual(
            equipos=[self.equipo], tecnico=self.usuario, cliente=self.colaborador,
            tipo_mantenimiento=TipoMantenimiento.objects.get(codigo='correctivo'), descripcion='Falla', fecha_programada=timezone.now(),
            usuario=self.usuario,
        )
        m2 = crear_mantenimiento_manual(
            equipos=[Activo.objects.create(codigo='CR-DSK-0301', tipo=Activo.Tipo.DESKTOP, unidad_negocio=self.mia)],
            tecnico=self.usuario, cliente=self.colaborador, tipo_mantenimiento=TipoMantenimiento.objects.get(codigo='correctivo'),
            descripcion='Falla', fecha_programada=timezone.now(), usuario=self.usuario,
        )
        cerrar_mantenimiento(mantenimiento=m2, resultado_tecnico=ResultadoTecnico.REPARADO, usuario=self.usuario)

        kpis = self._kpis()
        self.assertEqual(kpis['total_periodo'], 2)
        self.assertEqual(kpis['cerrados_periodo'], 1)

    def test_no_cuenta_mantenimientos_de_otra_unidad_de_negocio(self):
        from apps.catalogo.models import UnidadNegocio

        sg = UnidadNegocio.objects.get(codigo='SG')
        colaborador_sg = Colaborador.objects.create(nombre='Luis', cedula='9102', unidad_negocio=sg)
        crear_mantenimiento_manual(
            equipos=[Activo.objects.create(codigo='CR-DSK-0302', tipo=Activo.Tipo.DESKTOP, unidad_negocio=sg)],
            tecnico=self.usuario, cliente=colaborador_sg, tipo_mantenimiento=TipoMantenimiento.objects.get(codigo='correctivo'),
            descripcion='Falla', fecha_programada=timezone.now(), usuario=self.usuario,
        )
        self.assertEqual(self._kpis()['total_periodo'], 0)

    def test_mttr_es_el_promedio_de_horas_entre_creacion_y_cierre(self):
        mantenimiento = Mantenimiento.objects.create(
            cliente=self.colaborador, descripcion='Falla', fecha_programada=timezone.now(),
        )
        # Simula una intervención de 4 horas.
        Mantenimiento.objects.filter(pk=mantenimiento.pk).update(
            fecha_creacion=timezone.now() - timedelta(hours=4),
        )
        mantenimiento.refresh_from_db()
        cerrar_mantenimiento(mantenimiento=mantenimiento, resultado_tecnico=ResultadoTecnico.REPARADO, usuario=self.usuario)

        self.assertAlmostEqual(self._kpis()['mttr_horas'], 4.0, delta=0.05)

    def test_sin_mantenimientos_cerrados_mttr_es_none(self):
        crear_mantenimiento_manual(
            equipos=[self.equipo], tecnico=self.usuario, cliente=self.colaborador,
            tipo_mantenimiento=TipoMantenimiento.objects.get(codigo='correctivo'), descripcion='Falla', fecha_programada=timezone.now(),
            usuario=self.usuario,
        )
        self.assertIsNone(self._kpis()['mttr_horas'])

    def test_costo_repuestos_suma_solo_los_del_periodo(self):
        mantenimiento = crear_mantenimiento_manual(
            equipos=[self.equipo], tecnico=self.usuario, cliente=self.colaborador,
            tipo_mantenimiento=TipoMantenimiento.objects.get(codigo='correctivo'), descripcion='Falla', fecha_programada=timezone.now(),
            usuario=self.usuario,
        )
        tipo_consumible = TipoConsumible.objects.create(codigo='FUENTE2', nombre='Fuente de poder')
        registrar_repuesto_utilizado(
            mantenimiento=mantenimiento, tipo_consumible=tipo_consumible, cantidad=2,
            costo_unitario=15, usuario=self.usuario,
        )
        self.assertEqual(self._kpis()['costo_repuestos_periodo'], 30)

    def test_equipos_requieren_reemplazo_cuenta_baja_recomendada_no_dados_de_baja(self):
        self.equipo.baja_recomendada = True
        self.equipo.save(update_fields=['baja_recomendada'])
        Activo.objects.create(
            codigo='CR-DSK-0303', tipo=Activo.Tipo.DESKTOP, unidad_negocio=self.mia,
            baja_recomendada=True, estado=Activo.Estado.DADO_DE_BAJA,
        )
        self.assertEqual(self._kpis()['equipos_requieren_reemplazo'], 1)

    def test_atrasados_ahora_cuenta_mantenimientos_abiertos_hace_mas_de_3_dias(self):
        crear_mantenimiento_manual(
            equipos=[self.equipo], tecnico=self.usuario, cliente=self.colaborador,
            tipo_mantenimiento=TipoMantenimiento.objects.get(codigo='correctivo'), descripcion='Falla',
            fecha_programada=timezone.now() - timedelta(days=10), usuario=self.usuario,
        )
        self.assertEqual(self._kpis()['atrasados_ahora'], 1)


class MantenimientoManualFormTests(TestCase):
    """El cliente va primero: `equipos` se filtra a lo que tiene asignado ese
    colaborador -- antes se elegían por separado, sin ninguna relación entre sí
    (23-ago-2026)."""

    def setUp(self):
        from apps.mantenimiento.forms import MantenimientoManualForm

        self.Form = MantenimientoManualForm
        self.cliente = Colaborador.objects.create(nombre='Ana', cedula='9201')
        self.otro_cliente = Colaborador.objects.create(nombre='Luis', cedula='9202')
        self.equipo_de_ana = Activo.objects.create(
            codigo='CR-DSK-0400', tipo=Activo.Tipo.DESKTOP, colaborador_actual=self.cliente,
        )
        self.equipo_de_luis = Activo.objects.create(
            codigo='CR-DSK-0401', tipo=Activo.Tipo.DESKTOP, colaborador_actual=self.otro_cliente,
        )

    def test_formulario_sin_enviar_no_ofrece_ningun_equipo(self):
        form = self.Form()
        self.assertEqual(form.fields['equipos'].queryset.count(), 0)

    def test_formulario_enviado_solo_ofrece_equipos_del_cliente_elegido(self):
        form = self.Form(data={'cliente': self.cliente.pk})
        queryset = form.fields['equipos'].queryset
        self.assertIn(self.equipo_de_ana, queryset)
        self.assertNotIn(self.equipo_de_luis, queryset)

    def test_no_se_puede_enviar_un_equipo_de_otro_cliente(self):
        form = self.Form(data={
            'cliente': self.cliente.pk, 'equipos': [self.equipo_de_luis.pk],
            'estado_general': EstadoGeneralEquipo.OPERATIVO, 'descripcion': 'x',
            'fecha_programada': timezone.now(),
        })
        self.assertFalse(form.is_valid())
        self.assertIn('equipos', form.errors)
