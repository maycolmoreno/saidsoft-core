from datetime import date, timedelta

from django.contrib.auth.models import User
from django.test import TestCase
from django.utils import timezone

from apps.activos.models import Activo, Bodega, Colaborador, StockBodega, TipoConsumible
from apps.catalogo.models import Farmacia, Grupo, UnidadNegocio
from apps.cuentas.models import PerfilUsuario

from .models import (
    AcuerdoNivelServicio, EstadoGeneralEquipo, EventoMantenimiento, Mantenimiento, MantenimientoProgramado,
    Notificacion, PrioridadMantenimiento, RADIO_VERIFICACION_METROS, RepuestoUtilizado, ResultadoTecnico,
    TipoMantenimiento, TipoOrigenMantenimiento, UbicacionTecnico, VisitaTecnica,
)
from .services import (
    _metros_entre, cancelar_mantenimiento, cancelar_visita_tecnica, cerrar_visita_tecnica,
    crear_visita_tecnica, iniciar_visita_tecnica, cerrar_mantenimiento, crear_mantenimiento_manual, generar_informe_pdf,
    iniciar_mantenimiento, iniciar_reparacion_desde_activo, mantenimientos_atrasados,
    mantenimientos_programados_por_vencer, notificar_mantenimientos_proximos_y_atrasados,
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


class SlaTests(TestCase):
    """El SLA reemplaza al umbral único de días: una falla crítica y un preventivo de
    rutina ya no vencen al mismo tiempo. El reloj corre desde `fecha_programada`."""

    def setUp(self):
        self.tecnico = User.objects.create_user(username='u_sla', password='x')
        self.equipo = Activo.objects.create(codigo='CR-DSK-9001', tipo=Activo.Tipo.DESKTOP)

    def _crear(self, prioridad, horas_atras):
        return crear_mantenimiento_manual(
            equipos=[self.equipo], tecnico=self.tecnico, descripcion='x',
            fecha_programada=timezone.now() - timedelta(hours=horas_atras),
            usuario=self.tecnico, prioridad=prioridad,
        )

    def test_los_acuerdos_vienen_sembrados_por_migracion(self):
        self.assertEqual(AcuerdoNivelServicio.objects.count(), 4)
        critica = AcuerdoNivelServicio.objects.get(prioridad=PrioridadMantenimiento.CRITICA)
        self.assertEqual(critica.horas_resolucion, 4)

    def test_critica_vence_mucho_antes_que_normal(self):
        # 6 horas abierto: la crítica (4h) ya incumplió, la normal (72h) no.
        critica = self._crear(PrioridadMantenimiento.CRITICA, horas_atras=6)
        self.assertTrue(critica.sla_resolucion_incumplido)
        self.assertEqual(critica.estado_sla, 'incumplido')

        self.equipo2 = Activo.objects.create(codigo='CR-DSK-9002', tipo=Activo.Tipo.DESKTOP)
        normal = crear_mantenimiento_manual(
            equipos=[self.equipo2], tecnico=self.tecnico, descripcion='x',
            fecha_programada=timezone.now() - timedelta(hours=6),
            usuario=self.tecnico, prioridad=PrioridadMantenimiento.NORMAL,
        )
        self.assertFalse(normal.sla_resolucion_incumplido)

    def test_atrasados_usa_el_sla_de_cada_prioridad(self):
        critica = self._crear(PrioridadMantenimiento.CRITICA, horas_atras=6)
        equipo2 = Activo.objects.create(codigo='CR-DSK-9003', tipo=Activo.Tipo.DESKTOP)
        crear_mantenimiento_manual(
            equipos=[equipo2], tecnico=self.tecnico, descripcion='x',
            fecha_programada=timezone.now() - timedelta(hours=6),
            usuario=self.tecnico, prioridad=PrioridadMantenimiento.NORMAL,
        )
        atrasados = list(mantenimientos_atrasados())
        self.assertEqual([m.pk for m in atrasados], [critica.pk])

    def test_cerrado_a_tiempo_queda_cumplido(self):
        m = self._crear(PrioridadMantenimiento.CRITICA, horas_atras=1)
        cerrar_mantenimiento(mantenimiento=m, resultado_tecnico=ResultadoTecnico.REPARADO, usuario=self.tecnico)
        m.refresh_from_db()
        self.assertEqual(m.estado_sla, 'cumplido')
        self.assertFalse(m.sla_resolucion_incumplido)

    def test_cerrado_tarde_queda_incumplido_aunque_ya_este_cerrado(self):
        m = self._crear(PrioridadMantenimiento.CRITICA, horas_atras=10)
        cerrar_mantenimiento(mantenimiento=m, resultado_tecnico=ResultadoTecnico.REPARADO, usuario=self.tecnico)
        m.refresh_from_db()
        self.assertEqual(m.estado_sla, 'incumplido')

    def test_respuesta_se_juzga_contra_el_inicio_real_no_contra_ahora(self):
        # Iniciado dentro de la hora de respuesta: aunque siga abierto mucho después,
        # la respuesta NO se incumplió.
        m = self._crear(PrioridadMantenimiento.CRITICA, horas_atras=0)
        iniciar_mantenimiento(mantenimiento=m, usuario=self.tecnico)
        m.refresh_from_db()
        self.assertFalse(m.sla_respuesta_incumplido)

    def test_sin_acuerdo_no_afirma_incumplimiento(self):
        AcuerdoNivelServicio.objects.all().delete()
        m = self._crear(PrioridadMantenimiento.CRITICA, horas_atras=999)
        self.assertIsNone(m.limite_resolucion)
        self.assertFalse(m.sla_resolucion_incumplido)
        self.assertEqual(m.estado_sla, 'sin_sla')

    def test_cancelado_no_cuenta_como_incumplido(self):
        m = self._crear(PrioridadMantenimiento.CRITICA, horas_atras=999)
        cancelar_mantenimiento(mantenimiento=m, motivo='ya no aplica', usuario=self.tecnico)
        m.refresh_from_db()
        self.assertFalse(m.sla_resolucion_incumplido)
        self.assertEqual(m.estado_sla, 'sin_sla')

    def test_reparacion_desde_activo_arranca_en_alta(self):
        activo = Activo.objects.create(codigo='CR-DSK-9004', tipo=Activo.Tipo.DESKTOP)
        m = iniciar_reparacion_desde_activo(
            activo=activo, motivo='falla', detalle_motivo='no enciende', usuario=self.tecnico,
        )
        self.assertEqual(m.prioridad, PrioridadMantenimiento.ALTA)


class PresenciaEnSitioTests(TestCase):
    """Verificación GPS al cerrar: confirma si el técnico estuvo físicamente en la
    farmacia. 'sin_datos' NO equivale a "no fue" -- hay motivos legítimos."""

    def setUp(self):
        self.tecnico = User.objects.create_user(username='u_gps', password='x')
        sg = UnidadNegocio.objects.get(codigo='SG')
        grupo = Grupo.objects.create(codigo='TRX001')
        # Coordenadas reales de Guayaquil como referencia.
        self.farmacia = Farmacia.objects.create(
            codigo='ML001', grupo=grupo, unidad_negocio=sg, latitud=-2.170998, longitud=-79.922359,
        )
        self.equipo = Activo.objects.create(
            codigo='CR-DSK-8001', tipo=Activo.Tipo.DESKTOP, farmacia=self.farmacia,
        )

    def _mantenimiento(self):
        return crear_mantenimiento_manual(
            equipos=[self.equipo], tecnico=self.tecnico, descripcion='x',
            fecha_programada=timezone.now() - timedelta(hours=1), usuario=self.tecnico,
        )

    def _posicion(self, lat, lon):
        UbicacionTecnico.objects.create(
            usuario=self.tecnico, latitud=lat, longitud=lon, timestamp_captura=timezone.now(),
        )

    def test_haversine_da_una_distancia_creible(self):
        # ~1 grado de latitud son ~111 km.
        d = _metros_entre(-2.170998, -79.922359, -3.170998, -79.922359)
        self.assertAlmostEqual(d, 111_195, delta=500)

    def test_tecnico_en_la_farmacia_queda_verificada(self):
        m = self._mantenimiento()
        self._posicion(-2.171050, -79.922400)  # a unos pocos metros
        cerrar_mantenimiento(mantenimiento=m, resultado_tecnico=ResultadoTecnico.REPARADO, usuario=self.tecnico)
        m.refresh_from_db()
        self.assertLess(m.distancia_verificacion_metros, RADIO_VERIFICACION_METROS)
        self.assertEqual(m.presencia_en_sitio, 'verificada')

    def test_tecnico_lejos_queda_fuera_de_rango(self):
        m = self._mantenimiento()
        self._posicion(-2.200000, -79.950000)  # varios kilómetros
        cerrar_mantenimiento(mantenimiento=m, resultado_tecnico=ResultadoTecnico.REPARADO, usuario=self.tecnico)
        m.refresh_from_db()
        self.assertGreater(m.distancia_verificacion_metros, RADIO_VERIFICACION_METROS)
        self.assertEqual(m.presencia_en_sitio, 'fuera_de_rango')

    def test_toma_la_distancia_minima_no_la_ultima(self):
        # Estuvo en la farmacia y después se alejó: sigue contando como que estuvo.
        m = self._mantenimiento()
        self._posicion(-2.171050, -79.922400)   # en el local
        self._posicion(-2.250000, -79.980000)   # ya se fue
        cerrar_mantenimiento(mantenimiento=m, resultado_tecnico=ResultadoTecnico.REPARADO, usuario=self.tecnico)
        m.refresh_from_db()
        self.assertEqual(m.presencia_en_sitio, 'verificada')

    def test_sin_posiciones_queda_sin_datos_y_el_cierre_no_falla(self):
        m = self._mantenimiento()
        cerrar_mantenimiento(mantenimiento=m, resultado_tecnico=ResultadoTecnico.REPARADO, usuario=self.tecnico)
        m.refresh_from_db()
        self.assertIsNone(m.distancia_verificacion_metros)
        self.assertEqual(m.presencia_en_sitio, 'sin_datos')
        self.assertEqual(m.estado_interno, Mantenimiento.EstadoInterno.CERRADO)

    def test_farmacia_sin_coordenadas_no_rompe_el_cierre(self):
        self.farmacia.latitud = None
        self.farmacia.save(update_fields=['latitud'])
        m = self._mantenimiento()
        self._posicion(-2.171050, -79.922400)
        cerrar_mantenimiento(mantenimiento=m, resultado_tecnico=ResultadoTecnico.REPARADO, usuario=self.tecnico)
        m.refresh_from_db()
        self.assertEqual(m.presencia_en_sitio, 'sin_datos')

    def test_ignora_posiciones_fuera_de_la_ventana_de_intervencion(self):
        # Una posición de ayer en la farmacia no sirve para dar por verificado el
        # mantenimiento de hoy.
        m = self._mantenimiento()
        UbicacionTecnico.objects.create(
            usuario=self.tecnico, latitud=-2.171050, longitud=-79.922400,
            timestamp_captura=timezone.now() - timedelta(days=1),
        )
        cerrar_mantenimiento(mantenimiento=m, resultado_tecnico=ResultadoTecnico.REPARADO, usuario=self.tecnico)
        m.refresh_from_db()
        self.assertEqual(m.presencia_en_sitio, 'sin_datos')


class VisitaTecnicaTests(TestCase):
    """La visita pasó de ser un reporte de solo lectura (sobre activos.Ubicacion, que
    en producción está vacía) a un proceso con ciclo de vida y verificación GPS."""

    def setUp(self):
        self.tecnico = User.objects.create_user(username='u_visita', password='x')
        sg = UnidadNegocio.objects.get(codigo='SG')
        grupo = Grupo.objects.create(codigo='TRX001')
        self.farmacia = Farmacia.objects.create(
            codigo='ML001', grupo=grupo, unidad_negocio=sg, latitud=-2.170998, longitud=-79.922359,
        )

    def _visita(self, fecha=None):
        return crear_visita_tecnica(
            farmacia=self.farmacia, tecnico=self.tecnico,
            fecha_planificada=fecha or timezone.localdate(), motivo='relevamiento', usuario=self.tecnico,
        )

    def test_nace_planificada(self):
        v = self._visita()
        self.assertEqual(v.estado, VisitaTecnica.Estado.PLANIFICADA)
        self.assertIsNone(v.fecha_inicio)

    def test_ciclo_completo_con_gps_verifica_presencia(self):
        v = self._visita()
        iniciar_visita_tecnica(visita=v, usuario=self.tecnico)
        UbicacionTecnico.objects.create(
            usuario=self.tecnico, latitud=-2.171050, longitud=-79.922400,
            timestamp_captura=timezone.now(),
        )
        cerrar_visita_tecnica(visita=v, usuario=self.tecnico, observaciones='todo ok')
        v.refresh_from_db()
        self.assertEqual(v.estado, VisitaTecnica.Estado.REALIZADA)
        self.assertEqual(v.presencia_en_sitio, 'verificada')
        self.assertEqual(v.observaciones, 'todo ok')

    def test_cerrar_sin_gps_no_falla_y_queda_sin_datos(self):
        v = self._visita()
        iniciar_visita_tecnica(visita=v, usuario=self.tecnico)
        cerrar_visita_tecnica(visita=v, usuario=self.tecnico)
        v.refresh_from_db()
        self.assertEqual(v.estado, VisitaTecnica.Estado.REALIZADA)
        self.assertEqual(v.presencia_en_sitio, 'sin_datos')

    def test_no_se_puede_iniciar_dos_veces(self):
        v = self._visita()
        iniciar_visita_tecnica(visita=v, usuario=self.tecnico)
        with self.assertRaises(ValueError):
            iniciar_visita_tecnica(visita=v, usuario=self.tecnico)

    def test_no_se_puede_cerrar_una_cancelada(self):
        v = self._visita()
        cancelar_visita_tecnica(visita=v, motivo='se reprograma', usuario=self.tecnico)
        with self.assertRaises(ValueError):
            cerrar_visita_tecnica(visita=v, usuario=self.tecnico)

    def test_planificada_para_ayer_y_sin_hacer_esta_atrasada(self):
        v = self._visita(fecha=timezone.localdate() - timedelta(days=1))
        self.assertTrue(v.atrasada)
        cerrar_visita_tecnica(visita=v, usuario=self.tecnico)
        v.refresh_from_db()
        self.assertFalse(v.atrasada)

    def test_el_mantenimiento_que_sale_de_la_visita_queda_enlazado(self):
        v = self._visita()
        equipo = Activo.objects.create(codigo='CR-DSK-6001', tipo=Activo.Tipo.DESKTOP, farmacia=self.farmacia)
        m = crear_mantenimiento_manual(
            equipos=[equipo], tecnico=self.tecnico, descripcion='falla detectada en la visita',
            fecha_programada=timezone.now(), usuario=self.tecnico,
        )
        m.visita = v
        m.save(update_fields=['visita'])
        self.assertEqual(list(v.mantenimientos_generados.all()), [m])


class UsuarioActualApiTests(TestCase):
    """La app móvil necesita saber quién es y qué puede hacer: obtain_auth_token de DRF
    solo devuelve el token."""

    def setUp(self):
        from rest_framework.authtoken.models import Token
        self.usuario = User.objects.create_user(
            username='tecnico1', password='x', first_name='Ana', last_name='Pérez',
            email='ana@ejemplo.com',
        )
        self.token = Token.objects.create(user=self.usuario)

    def _get(self):
        return self.client.get('/api/v1/auth/yo/', HTTP_AUTHORIZATION=f'Token {self.token.key}')

    def test_sin_token_devuelve_401(self):
        self.assertEqual(self.client.get('/api/v1/auth/yo/').status_code, 401)

    def test_devuelve_identidad_del_usuario(self):
        datos = self._get().json()
        self.assertEqual(datos['username'], 'tecnico1')
        self.assertEqual(datos['nombre'], 'Ana Pérez')
        self.assertEqual(datos['email'], 'ana@ejemplo.com')
        self.assertFalse(datos['es_staff'])

    def test_nombre_cae_al_username_si_no_hay_nombre_completo(self):
        self.usuario.first_name = ''
        self.usuario.last_name = ''
        self.usuario.save(update_fields=['first_name', 'last_name'])
        self.assertEqual(self._get().json()['nombre'], 'tecnico1')

    def test_incluye_los_permisos_de_django(self):
        from django.contrib.auth.models import Permission
        self.usuario.user_permissions.add(
            Permission.objects.get(content_type__app_label='mantenimiento', codename='view_visitatecnica'),
        )
        permisos = self._get().json()['permisos']
        self.assertIn('mantenimiento.view_visitatecnica', permisos)

    def test_token_se_obtiene_con_usuario_y_clave(self):
        resp = self.client.post('/api/v1/auth/token/', {'username': 'tecnico1', 'password': 'x'})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()['token'], self.token.key)


class MantenimientoApiMovilTests(TestCase):
    """Contrato que consume la app Flutter: la lista tiene que alcanzar para que el
    técnico priorice (SLA) y sepa a dónde ir (farmacia)."""

    def setUp(self):
        from rest_framework.authtoken.models import Token
        self.tecnico = User.objects.create_user(username='tec_api', password='x')
        self.token = Token.objects.create(user=self.tecnico)
        sg = UnidadNegocio.objects.get(codigo='SG')
        grupo = Grupo.objects.create(codigo='TRX001')
        self.farmacia = Farmacia.objects.create(
            codigo='ML001', grupo=grupo, unidad_negocio=sg, nombre='San Gregorio Centro',
            direccion='9 de Octubre 123', latitud=-2.170998, longitud=-79.922359,
        )
        self.equipo = Activo.objects.create(
            codigo='CR-DSK-5001', tipo=Activo.Tipo.DESKTOP, farmacia=self.farmacia,
        )
        self.mantenimiento = crear_mantenimiento_manual(
            equipos=[self.equipo], tecnico=self.tecnico, descripcion='POS no enciende',
            fecha_programada=timezone.now(), usuario=self.tecnico,
            prioridad=PrioridadMantenimiento.CRITICA,
        )

    def _auth(self):
        return {'HTTP_AUTHORIZATION': f'Token {self.token.key}'}

    def test_la_lista_trae_prioridad_y_sla(self):
        datos = self.client.get('/api/v1/mantenimientos/', **self._auth()).json()[0]
        self.assertEqual(datos['prioridad'], PrioridadMantenimiento.CRITICA)
        self.assertIn(datos['estado_sla'], ('en_plazo', 'por_vencer', 'incumplido'))
        self.assertIsNotNone(datos['limite_resolucion'])

    def test_la_lista_trae_la_farmacia_con_coordenadas(self):
        farmacia = self.client.get('/api/v1/mantenimientos/', **self._auth()).json()[0]['farmacia']
        self.assertEqual(farmacia['codigo'], 'ML001')
        self.assertEqual(farmacia['direccion'], '9 de Octubre 123')
        self.assertAlmostEqual(farmacia['latitud'], -2.170998)

    def test_equipo_sin_farmacia_devuelve_null_sin_romper(self):
        self.equipo.farmacia = None
        self.equipo.save(update_fields=['farmacia'])
        datos = self.client.get('/api/v1/mantenimientos/', **self._auth()).json()[0]
        self.assertIsNone(datos['farmacia'])

    def test_no_ve_mantenimientos_de_otro_tecnico(self):
        otro = User.objects.create_user(username='otro_tec', password='x')
        equipo2 = Activo.objects.create(codigo='CR-DSK-5002', tipo=Activo.Tipo.DESKTOP)
        crear_mantenimiento_manual(
            equipos=[equipo2], tecnico=otro, descripcion='ajeno',
            fecha_programada=timezone.now(), usuario=otro,
        )
        datos = self.client.get('/api/v1/mantenimientos/', **self._auth()).json()
        self.assertEqual([m['id'] for m in datos], [self.mantenimiento.pk])

    def test_cerrar_acepta_tiempo_real_y_estado_general(self):
        from apps.mantenimiento.services import iniciar_mantenimiento
        iniciar_mantenimiento(mantenimiento=self.mantenimiento, usuario=self.tecnico)
        resp = self.client.post(
            f'/api/v1/mantenimientos/{self.mantenimiento.pk}/cerrar/',
            {'resultado_tecnico': ResultadoTecnico.REPARADO, 'tiempo_real_minutos': 45,
             'estado_general': EstadoGeneralEquipo.OPERATIVO},
            content_type='application/json', **self._auth(),
        )
        self.assertEqual(resp.status_code, 200)
        self.mantenimiento.refresh_from_db()
        self.assertEqual(self.mantenimiento.tiempo_real_minutos, 45)
        self.assertEqual(self.mantenimiento.estado_general, EstadoGeneralEquipo.OPERATIVO)

    def test_el_detalle_expone_la_verificacion_de_presencia(self):
        resp = self.client.get(f'/api/v1/mantenimientos/{self.mantenimiento.pk}/', **self._auth())
        self.assertEqual(resp.json()['presencia_en_sitio'], 'sin_datos')


class EquiposYNotificacionesApiTests(TestCase):
    """Endpoints que consume el dashboard de la app. Antes no existían y su 404
    tumbaba toda la pantalla (las tres llamadas van en un Future.wait)."""

    def setUp(self):
        from rest_framework.authtoken.models import Token
        self.usuario = User.objects.create_user(username='tec_dash', password='x')
        self.token = Token.objects.create(user=self.usuario)
        PerfilUsuario.objects.create(usuario=self.usuario, acceso_todas_unidades=True)
        sg = UnidadNegocio.objects.get(codigo='SG')
        grupo = Grupo.objects.create(codigo='TRX001')
        self.farmacia = Farmacia.objects.create(
            codigo='ML001', grupo=grupo, unidad_negocio=sg, nombre='SG Centro',
        )
        self.activo = Activo.objects.create(
            codigo='CR-DSK-4001', tipo=Activo.Tipo.DESKTOP, farmacia=self.farmacia, unidad_negocio=sg,
        )

    def _auth(self):
        return {'HTTP_AUTHORIZATION': f'Token {self.token.key}'}

    def test_equipos_devuelve_la_farmacia_donde_esta(self):
        datos = self.client.get('/api/v1/equipos/', **self._auth()).json()
        self.assertEqual(len(datos), 1)
        self.assertEqual(datos[0]['codigo'], 'CR-DSK-4001')
        self.assertEqual(datos[0]['farmacia']['codigo'], 'ML001')

    def test_equipos_excluye_los_dados_de_baja(self):
        self.activo.estado = Activo.Estado.DADO_DE_BAJA
        self.activo.save(update_fields=['estado'])
        self.assertEqual(self.client.get('/api/v1/equipos/', **self._auth()).json(), [])

    def test_conteo_de_notificaciones_no_leidas(self):
        Notificacion.objects.create(usuario=self.usuario, mensaje='una', leida=False)
        Notificacion.objects.create(usuario=self.usuario, mensaje='otra', leida=True)
        resp = self.client.get('/api/v1/notificaciones/count/', **self._auth())
        self.assertEqual(resp.json()['count'], 1)

    def test_no_se_ven_notificaciones_de_otro_usuario(self):
        otro = User.objects.create_user(username='otro_dash', password='x')
        Notificacion.objects.create(usuario=otro, mensaje='ajena', leida=False)
        self.assertEqual(self.client.get('/api/v1/notificaciones/', **self._auth()).json(), [])

    def test_marcar_leida(self):
        n = Notificacion.objects.create(usuario=self.usuario, mensaje='una', leida=False)
        resp = self.client.post(f'/api/v1/notificaciones/{n.pk}/leer/', **self._auth())
        self.assertEqual(resp.status_code, 204)
        n.refresh_from_db()
        self.assertTrue(n.leida)

    def test_no_se_puede_marcar_leida_la_de_otro(self):
        otro = User.objects.create_user(username='otro_leer', password='x')
        n = Notificacion.objects.create(usuario=otro, mensaje='ajena', leida=False)
        self.assertEqual(
            self.client.post(f'/api/v1/notificaciones/{n.pk}/leer/', **self._auth()).status_code, 404,
        )
        n.refresh_from_db()
        self.assertFalse(n.leida)

    def test_el_catalogo_de_checklist_responde(self):
        datos = self.client.get('/api/v1/actividades-checklist/', **self._auth()).json()
        self.assertEqual(len(datos), 14)


class VisitaTecnicaApiMovilTests(TestCase):
    """Visitas en la app: el técnico ve las suyas y las opera en campo."""

    def setUp(self):
        from rest_framework.authtoken.models import Token
        self.tecnico = User.objects.create_user(username='tec_vis_api', password='x')
        self.token = Token.objects.create(user=self.tecnico)
        sg = UnidadNegocio.objects.get(codigo='SG')
        grupo = Grupo.objects.create(codigo='TRX001')
        self.farmacia = Farmacia.objects.create(
            codigo='ML001', grupo=grupo, unidad_negocio=sg, nombre='SG Centro',
            direccion='Av. Principal 100', latitud=-2.170998, longitud=-79.922359,
        )
        self.visita = crear_visita_tecnica(
            farmacia=self.farmacia, tecnico=self.tecnico,
            fecha_planificada=timezone.localdate(), motivo='relevamiento', usuario=self.tecnico,
        )

    def _auth(self):
        return {'HTTP_AUTHORIZATION': f'Token {self.token.key}'}

    def test_lista_trae_la_farmacia_con_coordenadas(self):
        datos = self.client.get('/api/v1/visitas/', **self._auth()).json()
        self.assertEqual(len(datos), 1)
        self.assertEqual(datos[0]['farmacia']['codigo'], 'ML001')
        self.assertAlmostEqual(datos[0]['farmacia']['latitud'], -2.170998)
        self.assertEqual(datos[0]['estado'], 'planificada')

    def test_no_ve_visitas_de_otro_tecnico(self):
        otro = User.objects.create_user(username='otro_vis', password='x')
        crear_visita_tecnica(
            farmacia=self.farmacia, tecnico=otro,
            fecha_planificada=timezone.localdate(), usuario=otro,
        )
        datos = self.client.get('/api/v1/visitas/', **self._auth()).json()
        self.assertEqual([v['id'] for v in datos], [self.visita.pk])

    def test_iniciar_y_cerrar(self):
        resp = self.client.post(f'/api/v1/visitas/{self.visita.pk}/iniciar/', **self._auth())
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()['estado'], 'en_curso')

        resp = self.client.post(
            f'/api/v1/visitas/{self.visita.pk}/cerrar/',
            {'observaciones': 'sin novedades'},
            content_type='application/json', **self._auth(),
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()['estado'], 'realizada')
        self.assertEqual(resp.json()['observaciones'], 'sin novedades')
        # Sin posiciones GPS no se puede verificar, y eso NO acusa al técnico.
        self.assertEqual(resp.json()['presencia_en_sitio'], 'sin_datos')

    def test_no_se_puede_iniciar_dos_veces(self):
        self.client.post(f'/api/v1/visitas/{self.visita.pk}/iniciar/', **self._auth())
        resp = self.client.post(f'/api/v1/visitas/{self.visita.pk}/iniciar/', **self._auth())
        self.assertEqual(resp.status_code, 400)


class AvisoAlAsignarTests(TestCase):
    """Asignar trabajo tiene que avisarle al técnico. Antes la bandeja solo se poblaba
    desde la tarea diaria de vencimientos, así que una asignación nueva no generaba
    ningún aviso."""

    def setUp(self):
        self.coordinador = User.objects.create_user(username='coord', password='x')
        self.tecnico = User.objects.create_user(username='tec_aviso', password='x')
        self.equipo = Activo.objects.create(codigo='CR-DSK-3001', tipo=Activo.Tipo.DESKTOP)

    def _crear(self, tecnico, usuario):
        return crear_mantenimiento_manual(
            equipos=[self.equipo], tecnico=tecnico, descripcion='x',
            fecha_programada=timezone.now(), usuario=usuario,
        )

    def test_avisa_al_tecnico_asignado(self):
        m = self._crear(self.tecnico, self.coordinador)
        aviso = Notificacion.objects.get(usuario=self.tecnico)
        self.assertIn('CR-DSK-3001', aviso.mensaje)
        self.assertEqual(aviso.mantenimiento, m)
        self.assertFalse(aviso.leida)

    def test_no_se_avisa_a_si_mismo(self):
        # Autoservicio desde la app: el técnico ya sabe que lo creó.
        self._crear(self.tecnico, self.tecnico)
        self.assertFalse(Notificacion.objects.exists())

    def test_sin_tecnico_no_avisa_a_nadie(self):
        self._crear(None, self.coordinador)
        self.assertFalse(Notificacion.objects.exists())


class CrearDesdeAppTests(TestCase):
    """Alta de mantenimiento y de equipo desde el campo. El técnico está parado frente
    al equipo: el formulario tiene que pedirle lo mínimo."""

    def setUp(self):
        from rest_framework.authtoken.models import Token
        from django.contrib.auth.models import Permission
        self.tecnico = User.objects.create_user(username='tec_campo', password='x')
        self.token = Token.objects.create(user=self.tecnico)
        PerfilUsuario.objects.create(usuario=self.tecnico, acceso_todas_unidades=True)
        self.tecnico.user_permissions.add(
            Permission.objects.get(content_type__app_label='activos', codename='add_activo'),
        )
        sg = UnidadNegocio.objects.get(codigo='SG')
        grupo = Grupo.objects.create(codigo='TRX001')
        self.farmacia = Farmacia.objects.create(
            codigo='ML001', grupo=grupo, unidad_negocio=sg, nombre='SG Centro',
        )
        self.equipo = Activo.objects.create(
            codigo='CR-DSK-2001', tipo=Activo.Tipo.DESKTOP, farmacia=self.farmacia,
            numero_serie='ABC123', unidad_negocio=sg,
        )

    def _auth(self):
        return {'HTTP_AUTHORIZATION': f'Token {self.token.key}'}

    def test_crea_mantenimiento_sin_cliente_ni_fecha(self):
        # Un POS de farmacia no tiene custodio, y quien abre desde el celular esta
        # frente al equipo: la fecha es ahora.
        resp = self.client.post(
            '/api/v1/mantenimientos/',
            {'equipos': [self.equipo.pk], 'estado_general': 'no_operativo',
             'descripcion': 'No enciende', 'prioridad': 'critica'},
            content_type='application/json', **self._auth(),
        )
        self.assertEqual(resp.status_code, 201, resp.content)
        datos = resp.json()
        self.assertEqual(datos['prioridad'], 'critica')
        m = Mantenimiento.objects.get(pk=datos['id'])
        self.assertEqual(m.tecnico, self.tecnico)
        self.assertIsNone(m.cliente)
        self.assertIsNotNone(m.fecha_programada)

    def test_el_tecnico_es_siempre_quien_lo_crea(self):
        otro = User.objects.create_user(username='ajeno', password='x')
        resp = self.client.post(
            '/api/v1/mantenimientos/',
            {'equipos': [self.equipo.pk], 'estado_general': 'operativo',
             'descripcion': 'x', 'tecnico': otro.pk},
            content_type='application/json', **self._auth(),
        )
        self.assertEqual(Mantenimiento.objects.get(pk=resp.json()['id']).tecnico, self.tecnico)

    def test_busca_equipos_por_serie_o_codigo(self):
        for termino in ('ABC123', 'abc', 'DSK-2001'):
            datos = self.client.get(f'/api/v1/equipos/?buscar={termino}', **self._auth()).json()
            self.assertEqual([e['codigo'] for e in datos], ['CR-DSK-2001'], termino)
        vacio = self.client.get('/api/v1/equipos/?buscar=NADA', **self._auth()).json()
        self.assertEqual(vacio, [])

    def test_registra_un_equipo_en_la_farmacia_sin_bodega(self):
        resp = self.client.post(
            '/api/v1/equipos/nuevo/',
            {'tipo': Activo.Tipo.DESKTOP, 'modelo': 'OptiPlex', 'numero_serie': 'XYZ789',
             'farmacia': self.farmacia.pk},
            content_type='application/json', **self._auth(),
        )
        self.assertEqual(resp.status_code, 201, resp.content)
        activo = Activo.objects.get(numero_serie='XYZ789')
        self.assertEqual(activo.farmacia, self.farmacia)
        self.assertIsNone(activo.bodega_actual)

    def test_no_se_puede_registrar_sin_indicar_donde_esta(self):
        resp = self.client.post(
            '/api/v1/equipos/nuevo/',
            {'tipo': Activo.Tipo.DESKTOP, 'modelo': 'OptiPlex'},
            content_type='application/json', **self._auth(),
        )
        self.assertEqual(resp.status_code, 400)

    def test_sin_permiso_no_puede_registrar_equipos(self):
        sin = User.objects.create_user(username='sin_alta', password='x')
        PerfilUsuario.objects.create(usuario=sin, acceso_todas_unidades=True)
        from rest_framework.authtoken.models import Token
        token = Token.objects.create(user=sin)
        resp = self.client.post(
            '/api/v1/equipos/nuevo/',
            {'tipo': Activo.Tipo.DESKTOP, 'farmacia': self.farmacia.pk},
            content_type='application/json',
            HTTP_AUTHORIZATION=f'Token {token.key}',
        )
        self.assertEqual(resp.status_code, 403)

    def test_los_catalogos_vienen_en_una_sola_respuesta(self):
        datos = self.client.get('/api/v1/catalogos/', **self._auth()).json()
        for clave in ('tipos_equipo', 'marcas', 'categorias', 'tipos_mantenimiento',
                      'estados_generales', 'prioridades', 'farmacias', 'bodegas'):
            self.assertIn(clave, datos)
        self.assertEqual([f['codigo'] for f in datos['farmacias']], ['ML001'])
        self.assertTrue(any(p['valor'] == 'critica' for p in datos['prioridades']))


class BuscarEquiposTests(TestCase):
    """Un solo campo que entiende lo que el técnico tiene a mano: la etiqueta del
    equipo, el código de la farmacia donde está parado, o quién lo usa."""

    def setUp(self):
        from rest_framework.authtoken.models import Token
        self.tecnico = User.objects.create_user(username='tec_busca', password='x')
        self.token = Token.objects.create(user=self.tecnico)
        PerfilUsuario.objects.create(usuario=self.tecnico, acceso_todas_unidades=True)
        sg = UnidadNegocio.objects.get(codigo='SG')
        grupo = Grupo.objects.create(codigo='TRX001')
        self.farmacia = Farmacia.objects.create(
            codigo='MAM06', grupo=grupo, unidad_negocio=sg, nombre='FARMACIAS MIA MAM06',
        )
        otra = Farmacia.objects.create(
            codigo='ML006', grupo=grupo, unidad_negocio=sg, nombre='SG Loja',
        )
        self.custodio = Colaborador.objects.create(
            nombre='Alvarez Mendoza Wellington', cedula='1314821941', unidad_negocio=sg,
        )
        self.equipo = Activo.objects.create(
            codigo='CR-DSK-9101', tipo=Activo.Tipo.DESKTOP, farmacia=self.farmacia,
            numero_serie='BB3VJD3', colaborador_actual=self.custodio, unidad_negocio=sg,
        )
        self.ajeno = Activo.objects.create(
            codigo='CR-DSK-9102', tipo=Activo.Tipo.DESKTOP, farmacia=otra,
            numero_serie='OTRA999', unidad_negocio=sg,
        )

    def _buscar(self, termino='', **extra):
        params = f'buscar={termino}' if termino else ''
        for k, v in extra.items():
            params += f'&{k}={v}' if params else f'{k}={v}'
        resp = self.client.get(
            f'/api/v1/equipos/?{params}', HTTP_AUTHORIZATION=f'Token {self.token.key}',
        )
        return [e['codigo'] for e in resp.json()]

    def test_busca_por_codigo_de_farmacia(self):
        self.assertEqual(self._buscar('MAM06'), ['CR-DSK-9101'])

    def test_busca_por_nombre_de_farmacia(self):
        self.assertEqual(self._buscar('MIA'), ['CR-DSK-9101'])

    def test_busca_por_nombre_del_custodio(self):
        self.assertEqual(self._buscar('Wellington'), ['CR-DSK-9101'])

    def test_sigue_buscando_por_serie_y_codigo(self):
        self.assertEqual(self._buscar('BB3VJD3'), ['CR-DSK-9101'])
        self.assertEqual(self._buscar('9102'), ['CR-DSK-9102'])

    def test_filtro_explicito_por_farmacia(self):
        # Para listar TODO lo de una farmacia sin depender de que el texto coincida.
        self.assertEqual(self._buscar(farmacia=self.farmacia.pk), ['CR-DSK-9101'])

    def test_filtro_explicito_por_cliente(self):
        self.assertEqual(self._buscar(cliente=self.custodio.pk), ['CR-DSK-9101'])

    def test_el_resultado_dice_farmacia_y_custodio(self):
        resp = self.client.get(
            '/api/v1/equipos/?buscar=MAM06', HTTP_AUTHORIZATION=f'Token {self.token.key}',
        ).json()[0]
        self.assertEqual(resp['farmacia']['codigo'], 'MAM06')
        self.assertEqual(resp['custodio'], 'Alvarez Mendoza Wellington')

    def test_los_colaboradores_vienen_en_el_catalogo(self):
        datos = self.client.get(
            '/api/v1/catalogos/', HTTP_AUTHORIZATION=f'Token {self.token.key}',
        ).json()
        self.assertIn('colaboradores', datos)
        self.assertTrue(any(c['nombre'].startswith('Alvarez') for c in datos['colaboradores']))
