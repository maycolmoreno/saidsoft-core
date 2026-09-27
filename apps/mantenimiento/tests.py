from datetime import date, timedelta

from django.contrib.auth.models import Permission, User
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.activos.models import Activo, Bodega, Colaborador, StockBodega, TipoConsumible
from apps.catalogo.models import Farmacia, Grupo, UnidadNegocio
from apps.cuentas.models import PerfilUsuario

from .forms import (
    ActividadPlanificadaForm, MantenimientoManualForm, MantenimientoProgramadoForm, VisitaTecnicaForm,
)
from .models import (
    AcuerdoNivelServicio, CierreEnConflicto, EstadoGeneralEquipo, EventoMantenimiento, Mantenimiento,
    MantenimientoProgramado,
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
from .services import (
    DIAS_RETENCION_UBICACIONES, HORAS_ESCALAMIENTO_CONFLICTO, aplicar_cierre_en_conflicto,
    descartar_cierre_en_conflicto, escalar_cierres_en_conflicto, purgar_ubicaciones_antiguas,
)


def otorgar(usuario, *etiquetas):
    """Da permisos por codename completo ('app.codename') a un usuario de prueba.

    Desde el 26-sep-2026 la API móvil exige los MISMOS codenames que el panel (ver
    apps/mantenimiento/api_permissions.py), así que un usuario de prueba pelado
    —que es como se creaban hasta ahora— recibe 403 en todos lados. Esto arma el
    usuario con exactamente lo que su rol real tendría, ni más ni menos: pedir un
    permiso de más acá esconde justamente el bug que el chequeo viene a evitar.
    """
    for etiqueta in etiquetas:
        app_label, codename = etiqueta.split('.')
        usuario.user_permissions.add(
            Permission.objects.get(content_type__app_label=app_label, codename=codename),
        )


# Lo que la app de campo necesita, alineado con el grupo 'Soporte Técnico' de
# seed_permisos.py (donde están los 9 técnicos reales, ver crear_tecnicos_soporte.py).
PERMISOS_APP_CAMPO = (
    'mantenimiento.view_mantenimiento', 'mantenimiento.add_mantenimiento',
    'mantenimiento.change_mantenimiento',
    'mantenimiento.view_visitatecnica', 'mantenimiento.change_visitatecnica',
    'mantenimiento.view_ubicaciontecnico', 'mantenimiento.add_ubicaciontecnico',
    'activos.view_activo', 'activos.add_activo',
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


# `CELERY_TASK_ALWAYS_EAGER` solo está en config/settings/desarrollo.py y no se lee del
# entorno: sin este override la prueba pasa en local y falla siempre en el contenedor,
# que es donde CLAUDE.md pide correr la suite contra PostgreSQL.
@override_settings(CELERY_TASK_ALWAYS_EAGER=True)
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


# `CELERY_TASK_ALWAYS_EAGER` solo está en config/settings/desarrollo.py y no se lee del
# entorno: sin este override la prueba pasa en local y falla siempre en el contenedor,
# que es donde CLAUDE.md pide correr la suite contra PostgreSQL.
@override_settings(CELERY_TASK_ALWAYS_EAGER=True)
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
    """`equipos` se puebla BUSCANDO, no eligiendo primero un custodio.

    El diseño anterior filtraba los equipos por el colaborador elegido. Servía para un
    equipo de oficina, pero dejaba fuera a los POS de farmacia -- que no tienen
    custodio -- y obligaba a saber de quién es el equipo cuando lo que se tiene a mano
    es la farmacia o el número de serie.

    Lo que SÍ se sigue impidiendo es cruzar de unidad de negocio.
    """

    def setUp(self):
        from apps.mantenimiento.forms import MantenimientoManualForm

        self.Form = MantenimientoManualForm
        self.sg = UnidadNegocio.objects.get(codigo='SG')
        self.mia = UnidadNegocio.objects.get(codigo='MIA')
        self.cliente = Colaborador.objects.create(nombre='Ana', cedula='9201', unidad_negocio=self.sg)
        self.otro_cliente = Colaborador.objects.create(nombre='Luis', cedula='9202', unidad_negocio=self.sg)
        self.equipo_de_ana = Activo.objects.create(
            codigo='CR-DSK-0400', tipo=Activo.Tipo.DESKTOP, colaborador_actual=self.cliente,
        )
        self.equipo_de_luis = Activo.objects.create(
            codigo='CR-DSK-0401', tipo=Activo.Tipo.DESKTOP, colaborador_actual=self.otro_cliente,
        )
        self.equipo_de_otro_tenant = Activo.objects.create(
            codigo='CR-DSK-0402', tipo=Activo.Tipo.DESKTOP, unidad_negocio=self.mia,
        )

    def test_formulario_sin_enviar_no_ofrece_ningun_equipo(self):
        # La lista arranca vacia y la puebla HTMX al buscar: volcar cientos de equipos
        # en un <select> no ayuda a nadie.
        form = self.Form()
        self.assertEqual(form.fields['equipos'].queryset.count(), 0)

    def test_al_enviar_se_aceptan_equipos_de_cualquier_custodio(self):
        # La lista la poblo una busqueda, no el custodio: acotar por cliente acá
        # rechazaria selecciones legitimas.
        form = self.Form(data={'cliente': self.cliente.pk})
        queryset = form.fields['equipos'].queryset
        self.assertIn(self.equipo_de_ana, queryset)
        self.assertIn(self.equipo_de_luis, queryset)

    def test_el_cliente_es_opcional(self):
        # Un POS de farmacia no tiene custodio.
        form = self.Form(data={
            'equipos': [self.equipo_de_ana.pk],
            'estado_general': EstadoGeneralEquipo.OPERATIVO, 'descripcion': 'x',
            'prioridad': PrioridadMantenimiento.NORMAL,
            'fecha_programada': timezone.now(),
        })
        self.assertTrue(form.is_valid(), form.errors)
        self.assertIsNone(form.cleaned_data['cliente'])

    def test_no_se_puede_enviar_un_equipo_de_otra_unidad_de_negocio(self):
        # Sin esto, alguien podria forzar el id de un activo de otro tenant en el POST.
        usuario = User.objects.create_user(username='u_form_sg', password='x')
        perfil = PerfilUsuario.objects.create(usuario=usuario)
        perfil.unidades_negocio.add(self.sg)

        form = self.Form(user=usuario, data={
            'equipos': [self.equipo_de_otro_tenant.pk],
            'estado_general': EstadoGeneralEquipo.OPERATIVO, 'descripcion': 'x',
            'fecha_programada': timezone.now(),
        })
        self.assertFalse(form.is_valid())
        self.assertIn('equipos', form.errors)

    def test_los_equipos_sin_unidad_de_negocio_siguen_siendo_elegibles(self):
        # Un activo sin unidad asignada se trata como compartido, mismo criterio que
        # el resto del panel.
        usuario = User.objects.create_user(username='u_form_sg2', password='x')
        perfil = PerfilUsuario.objects.create(usuario=usuario)
        perfil.unidades_negocio.add(self.sg)

        form = self.Form(user=usuario, data={'cliente': self.cliente.pk})
        self.assertIn(self.equipo_de_ana, form.fields['equipos'].queryset)


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
        otorgar(self.tecnico, *PERMISOS_APP_CAMPO)
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
        otorgar(self.usuario, *PERMISOS_APP_CAMPO)
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
        otorgar(self.tecnico, *PERMISOS_APP_CAMPO)
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
        """409 y no 400 desde el 26-sep-2026: que la visita ya este en curso no se
        arregla mandando otros datos, asi que la app tiene que poder distinguirlo de un
        error de validacion y dejar de reintentar (ver ConflictoDeEstado)."""
        self.client.post(f'/api/v1/visitas/{self.visita.pk}/iniciar/', **self._auth())
        resp = self.client.post(f'/api/v1/visitas/{self.visita.pk}/iniciar/', **self._auth())
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.json()['codigo'], 'conflicto_de_estado')
        self.assertEqual(resp.json()['estado_actual'], 'en_curso')


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
        otorgar(self.tecnico, *PERMISOS_APP_CAMPO)
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
        otorgar(self.tecnico, *PERMISOS_APP_CAMPO)
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


class EquipoListApiScopeTests(TestCase):
    """Un técnico con tenant acotado veía CERO equipos en la app, y sin equipo no se
    puede abrir un mantenimiento.

    La app usaba el scope estricto (excluye `unidad_negocio` nulo) mientras el panel
    usa el opcional (nulo = compartido, visible para todos). Como `registrar_ingreso`
    nunca setea `unidad_negocio`, "los nulos" son en la práctica TODOS los equipos.
    """

    def setUp(self):
        from rest_framework.authtoken.models import Token
        self.sg = UnidadNegocio.objects.get(codigo='SG')
        self.mia = UnidadNegocio.objects.get(codigo='MIA')
        self.compartido = Activo.objects.create(codigo='CR-DSK-9001', tipo=Activo.Tipo.DESKTOP)
        self.de_sg = Activo.objects.create(
            codigo='CR-DSK-9002', tipo=Activo.Tipo.DESKTOP, unidad_negocio=self.sg,
        )
        self.de_mia = Activo.objects.create(
            codigo='CR-DSK-9003', tipo=Activo.Tipo.DESKTOP, unidad_negocio=self.mia,
        )
        self.tecnico = User.objects.create_user(username='tec_scope_equipos', password='x')
        otorgar(self.tecnico, *PERMISOS_APP_CAMPO)
        perfil = PerfilUsuario.objects.create(usuario=self.tecnico, acceso_todas_unidades=False)
        perfil.unidades_negocio.add(self.sg)
        self.token = Token.objects.create(user=self.tecnico)

    def _codigos(self):
        resp = self.client.get('/api/v1/equipos/', HTTP_AUTHORIZATION=f'Token {self.token.key}')
        self.assertEqual(resp.status_code, 200)
        return {a['codigo'] for a in resp.json()}

    def test_ve_los_compartidos_ademas_de_los_de_su_unidad(self):
        codigos = self._codigos()
        self.assertIn('CR-DSK-9001', codigos)  # sin unidad = compartido
        self.assertIn('CR-DSK-9002', codigos)  # su propia unidad

    def test_sigue_sin_ver_los_de_otra_unidad(self):
        """El arreglo suma los compartidos; no afloja el aislamiento entre clientes."""
        self.assertNotIn('CR-DSK-9003', self._codigos())

    def test_los_dados_de_baja_no_son_accionables_en_campo(self):
        self.compartido.estado = Activo.Estado.DADO_DE_BAJA
        self.compartido.save(update_fields=['estado'])
        self.assertNotIn('CR-DSK-9001', self._codigos())


class MantenimientoApiCancelarYRepuestosTests(TestCase):
    """Dos cosas que el panel podía hacer y la app no, y que impedían TERMINAR el
    trabajo desde el campo (auditoría de pantallas, 4-sep-2026)."""

    def setUp(self):
        from rest_framework.authtoken.models import Token
        self.tecnico = User.objects.create_user(username='tec_huecos', password='x')
        otorgar(self.tecnico, *PERMISOS_APP_CAMPO)
        self.token = Token.objects.create(user=self.tecnico)
        PerfilUsuario.objects.create(usuario=self.tecnico, acceso_todas_unidades=True)
        sg = UnidadNegocio.objects.get(codigo='SG')
        grupo = Grupo.objects.create(codigo='TRX001')
        farmacia = Farmacia.objects.create(codigo='ML001', grupo=grupo, unidad_negocio=sg)
        self.activo = Activo.objects.create(
            codigo='CR-DSK-7001', tipo=Activo.Tipo.DESKTOP, farmacia=farmacia, unidad_negocio=sg,
        )
        self.mantenimiento = crear_mantenimiento_manual(
            equipos=[self.activo], tecnico=self.tecnico, cliente=None, tipo_mantenimiento=None,
            descripcion='revision', fecha_programada=timezone.now(),
            estado_general=EstadoGeneralEquipo.OPERATIVO, usuario=self.tecnico,
        )

    def _auth(self):
        return {'HTTP_AUTHORIZATION': f'Token {self.token.key}'}

    # --- Cancelar ---

    def test_cancelar_libera_el_equipo_para_un_mantenimiento_nuevo(self):
        """El caso real: un mantenimiento abierto por error bloquea el equipo, y sin
        cancelar desde la app había que entrar al panel para destrabarlo."""
        resp = self.client.post(
            f'/api/v1/mantenimientos/{self.mantenimiento.pk}/cancelar/',
            {'motivo': 'Cargado por error'}, content_type='application/json', **self._auth(),
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        self.mantenimiento.refresh_from_db()
        self.assertEqual(self.mantenimiento.estado_interno, Mantenimiento.EstadoInterno.CANCELADO)

        # Y ahora el equipo acepta uno nuevo.
        nuevo = self.client.post(
            '/api/v1/mantenimientos/',
            {'equipos': [self.activo.pk], 'estado_general': 'operativo', 'descripcion': 'de verdad'},
            content_type='application/json', **self._auth(),
        )
        self.assertEqual(nuevo.status_code, 201, nuevo.content)

    def test_cancelar_exige_motivo(self):
        resp = self.client.post(
            f'/api/v1/mantenimientos/{self.mantenimiento.pk}/cancelar/',
            {'motivo': ''}, content_type='application/json', **self._auth(),
        )
        self.assertEqual(resp.status_code, 400)
        self.mantenimiento.refresh_from_db()
        self.assertEqual(self.mantenimiento.estado_interno, Mantenimiento.EstadoInterno.PENDIENTE)

    def test_no_se_cancela_el_de_otro_tecnico(self):
        otro = User.objects.create_user(username='otro_tec_huecos', password='x')
        # CON permisos a proposito: lo que se prueba es el aislamiento por tecnico, no
        # el permiso. Sin otorgarlos, el 403 de PermisoDeclarado saltaria primero y el
        # test pasaria sin haber ejercitado nunca el filtro `tecnico=request.user`.
        otorgar(otro, *PERMISOS_APP_CAMPO)
        PerfilUsuario.objects.create(usuario=otro, acceso_todas_unidades=True)
        from rest_framework.authtoken.models import Token
        token = Token.objects.create(user=otro)
        resp = self.client.post(
            f'/api/v1/mantenimientos/{self.mantenimiento.pk}/cancelar/',
            {'motivo': 'ajeno'}, content_type='application/json',
            HTTP_AUTHORIZATION=f'Token {token.key}',
        )
        self.assertEqual(resp.status_code, 404)

    # --- Repuestos ---

    def test_repuesto_con_bodega_descuenta_stock(self):
        """Lo gasta el técnico en campo: si solo se podía cargar desde el panel, el
        stock mentía hasta que alguien de oficina lo transcribiera."""
        bodega = Bodega.objects.create(codigo='BOD-1', nombre='Central')
        tipo = TipoConsumible.objects.create(nombre='Toner negro')
        StockBodega.objects.create(bodega=bodega, tipo_consumible=tipo, cantidad=10)

        resp = self.client.post(
            f'/api/v1/mantenimientos/{self.mantenimiento.pk}/repuestos/',
            {'tipo_consumible': tipo.pk, 'cantidad': 3, 'bodega': bodega.pk},
            content_type='application/json', **self._auth(),
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(StockBodega.objects.get(bodega=bodega, tipo_consumible=tipo).cantidad, 7)

    def test_repuesto_sin_bodega_no_toca_stock(self):
        """Un repuesto que no salió de bodega igual se registra: sirve para el costo."""
        tipo = TipoConsumible.objects.create(nombre='Cable HDMI')
        resp = self.client.post(
            f'/api/v1/mantenimientos/{self.mantenimiento.pk}/repuestos/',
            {'tipo_consumible': tipo.pk, 'cantidad': 1, 'costo_unitario': '12.50'},
            content_type='application/json', **self._auth(),
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(self.mantenimiento.repuestos_utilizados.count(), 1)

    def test_stock_insuficiente_se_explica_no_revienta(self):
        bodega = Bodega.objects.create(codigo='BOD-2', nombre='Chica')
        tipo = TipoConsumible.objects.create(nombre='Mouse')
        StockBodega.objects.create(bodega=bodega, tipo_consumible=tipo, cantidad=1)

        resp = self.client.post(
            f'/api/v1/mantenimientos/{self.mantenimiento.pk}/repuestos/',
            {'tipo_consumible': tipo.pk, 'cantidad': 5, 'bodega': bodega.pk},
            content_type='application/json', **self._auth(),
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn('detail', resp.json())
        self.assertEqual(StockBodega.objects.get(bodega=bodega, tipo_consumible=tipo).cantidad, 1)

    # --- Catálogo ---

    def test_los_catalogos_traen_los_consumibles(self):
        """Sin esto el selector de repuestos de la app no tendría qué ofrecer."""
        TipoConsumible.objects.create(nombre='Toner')
        datos = self.client.get('/api/v1/catalogos/', **self._auth()).json()
        self.assertIn('tipos_consumible', datos)
        self.assertEqual([t['nombre'] for t in datos['tipos_consumible']], ['Toner'])


class TecnicoAutoAsignadoTests(TestCase):
    """El campo `tecnico` se preselecciona en quien está en sesión.

    Un técnico que registra su propio trabajo no tenía por qué buscarse en un
    desplegable con todos los usuarios activos, ni podía evitar que ese mismo
    desplegable le dejara cargarle trabajo a un compañero.
    """

    def setUp(self):
        self.tecnico = User.objects.create_user(username='tecnico_campo', password='x')
        self.otro = User.objects.create_user(username='otro_tecnico', password='x')
        self.coordinador = User.objects.create_user(username='coordinador', password='x')
        self.coordinador.user_permissions.add(
            Permission.objects.get(content_type__app_label='mantenimiento', codename='asignar_tecnico'),
        )
        # has_perm cachea los permisos en la instancia; sin recargarla, el usuario
        # recién permisado sigue viéndose sin permiso.
        self.coordinador = User.objects.get(pk=self.coordinador.pk)

    def _formularios(self, user):
        return [
            MantenimientoManualForm(user=user),
            MantenimientoProgramadoForm(user=user),
            ActividadPlanificadaForm(user=user),
            VisitaTecnicaForm(user=user),
        ]

    def test_sin_permiso_el_campo_queda_fijo_en_el_propio_usuario(self):
        for form in self._formularios(self.tecnico):
            with self.subTest(form=type(form).__name__):
                campo = form.fields['tecnico']
                self.assertEqual(list(campo.queryset), [self.tecnico])
                self.assertTrue(campo.disabled)
                self.assertEqual(form['tecnico'].value(), self.tecnico.pk)

    def test_con_permiso_conserva_el_desplegable_completo_preseleccionado(self):
        for form in self._formularios(self.coordinador):
            with self.subTest(form=type(form).__name__):
                campo = form.fields['tecnico']
                self.assertIn(self.otro, campo.queryset)
                self.assertFalse(campo.disabled)
                # Preseleccionado en sí mismo: es el caso más frecuente incluso
                # para quien puede repartir trabajo.
                self.assertEqual(form['tecnico'].value(), self.coordinador.pk)

    def test_sin_permiso_un_post_a_nombre_de_otro_se_guarda_a_nombre_propio(self):
        """La defensa real: `disabled` hace que Django ignore lo que venga en el POST.

        Acotar solo el widget dejaría pasar un POST armado a mano con el id de un
        compañero.
        """
        farmacia = Farmacia.objects.create(
            codigo='ML900', grupo=Grupo.objects.create(codigo='TRX900'),
            unidad_negocio=UnidadNegocio.objects.get(codigo='SG'),
        )
        PerfilUsuario.objects.create(usuario=self.tecnico, acceso_todas_unidades=True)
        form = VisitaTecnicaForm(
            {
                'farmacia': farmacia.pk,
                'tecnico': self.otro.pk,          # intento de asignárselo a un compañero
                'fecha_planificada': date.today().isoformat(),
            },
            user=self.tecnico,
        )
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data['tecnico'], self.tecnico)

    def test_con_permiso_si_puede_asignar_a_otro(self):
        farmacia = Farmacia.objects.create(
            codigo='ML901', grupo=Grupo.objects.create(codigo='TRX901'),
            unidad_negocio=UnidadNegocio.objects.get(codigo='SG'),
        )
        PerfilUsuario.objects.create(usuario=self.coordinador, acceso_todas_unidades=True)
        form = VisitaTecnicaForm(
            {
                'farmacia': farmacia.pk,
                'tecnico': self.otro.pk,
                'fecha_planificada': date.today().isoformat(),
            },
            user=self.coordinador,
        )
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data['tecnico'], self.otro)

    def test_sin_usuario_el_formulario_sigue_armandose(self):
        """Los comandos de management y el shell instancian formularios sin sesión."""
        form = VisitaTecnicaForm()
        self.assertFalse(form.fields['tecnico'].disabled)

    def test_editar_un_registro_ajeno_no_lo_reasigna(self):
        """Preseleccionar es para un alta, no para pisar a quien ya tenía el trabajo.

        Hoy no hay vista de edición para estos modelos; esta prueba fija el
        comportamiento para la primera que se agregue.
        """
        equipo = Activo.objects.create(
            codigo='ACT-900', numero_serie='SN900', estado=Activo.Estado.ASIGNADO,
        )
        programado = MantenimientoProgramado.objects.create(
            equipo=equipo, tecnico=self.otro, frecuencia_dias=30,
            fecha_proximo=date.today() + timedelta(days=30),
        )
        form = MantenimientoProgramadoForm(instance=programado, user=self.tecnico)
        campo = form.fields['tecnico']
        # El dueño original sigue en el queryset aunque el editor no tenga permiso:
        # si no, la validación fallaría con "elección no válida" sobre un campo que
        # el usuario ni puede tocar.
        self.assertIn(self.otro, campo.queryset)
        self.assertTrue(campo.disabled)
        self.assertEqual(form['tecnico'].value(), self.otro.pk)


class ApiExigeLosMismosPermisosQueElPanelTests(TestCase):
    """Regresión de BUG-1 (docs/modulos.md): hasta el 26-sep-2026 la API móvil era
    `IsAuthenticated` a secas, así que un usuario SIN permisos podía cerrar, firmar y
    cancelar por API lo que el panel le negaba con 403. El gating de la app era
    cosmético.

    Un usuario autenticado y pelado tiene que rebotar en TODO lo que el panel protege.
    """

    def setUp(self):
        from rest_framework.authtoken.models import Token
        self.pelado = User.objects.create_user(username='sin_permisos', password='x')
        PerfilUsuario.objects.create(usuario=self.pelado, acceso_todas_unidades=True)
        self.token = Token.objects.create(user=self.pelado)
        sg = UnidadNegocio.objects.get(codigo='SG')
        self.farmacia = Farmacia.objects.create(
            codigo='ML900', grupo=Grupo.objects.create(codigo='TRX900'), unidad_negocio=sg,
            nombre='Farmacia permisos', latitud=-2.17, longitud=-79.92,
        )
        equipo = Activo.objects.create(codigo='CR-DSK-9900', tipo=Activo.Tipo.DESKTOP, farmacia=self.farmacia)
        # Asignados al usuario PELADO: así el 403 solo puede venir del permiso, nunca
        # del filtro `tecnico=request.user` (que devolvería 404 y taparía el punto).
        self.mantenimiento = crear_mantenimiento_manual(
            equipos=[equipo], tecnico=self.pelado, descripcion='POS caido',
            fecha_programada=timezone.now(), usuario=self.pelado,
        )
        self.visita = crear_visita_tecnica(
            farmacia=self.farmacia, tecnico=self.pelado, fecha_planificada=timezone.localdate(),
            motivo='relevamiento', usuario=self.pelado,
        )

    def _auth(self):
        return {'HTTP_AUTHORIZATION': f'Token {self.token.key}'}

    def test_lecturas_sin_permiso_dan_403(self):
        for ruta in (
            '/api/v1/mantenimientos/',
            f'/api/v1/mantenimientos/{self.mantenimiento.pk}/',
            f'/api/v1/mantenimientos/{self.mantenimiento.pk}/checklist/',
            '/api/v1/visitas/',
            '/api/v1/equipos/',
            '/api/v1/catalogos/',
            '/api/v1/actividades-checklist/',
            '/api/v1/ubicaciones-tecnico/',
        ):
            with self.subTest(ruta=ruta):
                self.assertEqual(self.client.get(ruta, **self._auth()).status_code, 403)

    def test_mutaciones_sin_permiso_dan_403(self):
        casos = [
            (f'/api/v1/mantenimientos/{self.mantenimiento.pk}/iniciar/', {}),
            (f'/api/v1/mantenimientos/{self.mantenimiento.pk}/cerrar/',
             {'resultado_tecnico': ResultadoTecnico.REPARADO}),
            (f'/api/v1/mantenimientos/{self.mantenimiento.pk}/cancelar/', {'motivo': 'duplicado'}),
            (f'/api/v1/mantenimientos/{self.mantenimiento.pk}/firmar/',
             {'tipo_firma': 'tecnico', 'firma_base64': 'x'}),
            (f'/api/v1/mantenimientos/{self.mantenimiento.pk}/repuestos/',
             {'tipo_consumible': 1, 'cantidad': 1}),
            (f'/api/v1/visitas/{self.visita.pk}/iniciar/', {}),
            (f'/api/v1/visitas/{self.visita.pk}/cerrar/', {'observaciones': 'ok'}),
            ('/api/v1/ubicaciones-tecnico/',
             {'latitud': -2.17, 'longitud': -79.92, 'timestamp_captura': timezone.now().isoformat()}),
        ]
        for ruta, cuerpo in casos:
            with self.subTest(ruta=ruta):
                resp = self.client.post(ruta, cuerpo, content_type='application/json', **self._auth())
                self.assertEqual(resp.status_code, 403, f'{ruta} devolvio {resp.status_code}')

    def test_nada_se_movio_en_la_base(self):
        """El 403 tiene que cortar ANTES del servicio, no después de escribir."""
        self.client.post(
            f'/api/v1/mantenimientos/{self.mantenimiento.pk}/cerrar/',
            {'resultado_tecnico': ResultadoTecnico.REPARADO},
            content_type='application/json', **self._auth(),
        )
        self.mantenimiento.refresh_from_db()
        self.assertEqual(self.mantenimiento.estado_interno, Mantenimiento.EstadoInterno.PENDIENTE)
        self.assertIsNone(self.mantenimiento.fecha_cierre)

    def test_una_accion_nueva_sin_permiso_declarado_se_rechaza(self):
        """Fail-closed: el mapa es la lista blanca. Si alguien agrega un @action y se
        olvida de declararlo, tiene que fallar acá y no quedar abierto en producción."""
        from apps.mantenimiento.api_permissions import PermisoDeclarado

        class VistaFalsa:
            action = 'accion_inventada'
            permisos_por_accion = {'list': ['mantenimiento.view_mantenimiento']}

        self.assertIsNone(PermisoDeclarado._requeridos(VistaFalsa(), None))
        self.assertFalse(PermisoDeclarado().has_permission(_PeticionFalsa(self.pelado), VistaFalsa()))


class _PeticionFalsa:
    """Lo mínimo que mira PermisoDeclarado.has_permission."""

    def __init__(self, usuario, metodo='GET'):
        self.user = usuario
        self.method = metodo


class FlujoRolRealEnLaAppTests(TestCase):
    """Paso 5 del fix de BUG-1/BUG-2: el técnico REAL (grupo 'Soporte Técnico', que es
    donde están los 9 de campo — ver crear_tecnicos_soporte.py) tiene que poder hacer
    su jornada completa por API, sin ser superusuario.

    Se arma con `seed_permisos` de verdad y no con una lista a mano: si el comando y la
    API se desincronizan otra vez, esto falla, que es exactamente lo que no pasó antes.
    """

    @classmethod
    def setUpTestData(cls):
        from django.core.management import call_command
        call_command('seed_permisos', verbosity=0)

    def setUp(self):
        from django.contrib.auth.models import Group
        from rest_framework.authtoken.models import Token
        self.tecnico = User.objects.create_user(username='tec_real', password='x')
        self.tecnico.groups.add(Group.objects.get(name='Soporte Técnico'))
        PerfilUsuario.objects.create(usuario=self.tecnico, acceso_todas_unidades=True)
        self.token = Token.objects.create(user=self.tecnico)
        sg = UnidadNegocio.objects.get(codigo='SG')
        self.farmacia = Farmacia.objects.create(
            codigo='ML901', grupo=Grupo.objects.create(codigo='TRX901'), unidad_negocio=sg,
            nombre='Farmacia rol real', latitud=-2.170998, longitud=-79.922359,
        )
        self.equipo = Activo.objects.create(
            codigo='CR-DSK-9901', tipo=Activo.Tipo.DESKTOP, farmacia=self.farmacia,
        )
        self.mantenimiento = crear_mantenimiento_manual(
            equipos=[self.equipo], tecnico=self.tecnico, descripcion='POS no enciende',
            fecha_programada=timezone.now(), usuario=self.tecnico,
        )
        self.visita = crear_visita_tecnica(
            farmacia=self.farmacia, tecnico=self.tecnico, fecha_planificada=timezone.localdate(),
            motivo='preventivo de ruta', usuario=self.tecnico,
        )

    def _auth(self):
        return {'HTTP_AUTHORIZATION': f'Token {self.token.key}'}

    def _post(self, ruta, cuerpo=None):
        return self.client.post(
            ruta, cuerpo or {}, content_type='application/json', **self._auth(),
        )

    def test_la_app_le_habilita_las_cuatro_pantallas(self):
        """Los codenames que gatea sesion.dart. Sin estos, la app esconde 'Visitas',
        'Ubicacion' y 'Registrar equipo' — que es como estaba hasta hoy (BUG-2)."""
        permisos = self.client.get('/api/v1/auth/yo/', **self._auth()).json()['permisos']
        for codename in (
            'mantenimiento.view_mantenimiento', 'mantenimiento.add_mantenimiento',
            'mantenimiento.change_mantenimiento',
            'mantenimiento.view_visitatecnica', 'mantenimiento.change_visitatecnica',
            'mantenimiento.add_ubicaciontecnico', 'activos.add_activo',
        ):
            with self.subTest(codename=codename):
                self.assertIn(codename, permisos)

    def test_jornada_completa_de_mantenimiento(self):
        self.assertEqual(self.client.get('/api/v1/mantenimientos/', **self._auth()).status_code, 200)
        self.assertEqual(
            self.client.get(f'/api/v1/mantenimientos/{self.mantenimiento.pk}/checklist/', **self._auth()).status_code,
            200,
        )
        self.assertEqual(self._post(f'/api/v1/mantenimientos/{self.mantenimiento.pk}/iniciar/').status_code, 200)
        self.assertEqual(
            self._post(
                f'/api/v1/mantenimientos/{self.mantenimiento.pk}/firmar/',
                {'tipo_firma': 'tecnico', 'firma_base64': 'data:image/png;base64,xx'},
            ).status_code,
            201,
        )
        self.assertEqual(
            self._post(
                f'/api/v1/mantenimientos/{self.mantenimiento.pk}/cerrar/',
                {'resultado_tecnico': ResultadoTecnico.REPARADO, 'tiempo_real_minutos': 30},
            ).status_code,
            200,
        )
        self.mantenimiento.refresh_from_db()
        self.assertEqual(self.mantenimiento.estado_interno, Mantenimiento.EstadoInterno.CERRADO)

    def test_jornada_completa_de_visita(self):
        self.assertEqual(self.client.get('/api/v1/visitas/', **self._auth()).status_code, 200)
        self.assertEqual(self._post(f'/api/v1/visitas/{self.visita.pk}/iniciar/').status_code, 200)
        self.assertEqual(
            self._post(f'/api/v1/visitas/{self.visita.pk}/cerrar/', {'observaciones': 'todo ok'}).status_code,
            200,
        )
        self.visita.refresh_from_db()
        self.assertEqual(self.visita.estado, VisitaTecnica.Estado.REALIZADA)

    def test_consentimiento_y_envio_de_ubicacion(self):
        self.assertEqual(
            self._post('/api/v1/consentimiento-monitoreo/', {'aceptado': True, 'version_terminos': '1.0'}).status_code,
            201,
        )
        resp = self._post('/api/v1/ubicaciones-tecnico/', {
            'latitud': -2.170998, 'longitud': -79.922359, 'precision_metros': 8.0,
            'timestamp_captura': timezone.now().isoformat(),
        })
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(self.client.get('/api/v1/ubicaciones-tecnico/', **self._auth()).status_code, 200)

    def test_catalogos_equipos_y_alta_de_equipo(self):
        for ruta in ('/api/v1/catalogos/', '/api/v1/equipos/', '/api/v1/actividades-checklist/'):
            with self.subTest(ruta=ruta):
                self.assertEqual(self.client.get(ruta, **self._auth()).status_code, 200)
        resp = self._post('/api/v1/equipos/nuevo/', {
            'tipo': Activo.Tipo.DESKTOP, 'modelo': 'OptiPlex 3080', 'farmacia': self.farmacia.pk,
        })
        self.assertEqual(resp.status_code, 201, resp.content)

    def test_puede_abrir_un_mantenimiento_desde_el_campo(self):
        otro = Activo.objects.create(
            codigo='CR-DSK-9902', tipo=Activo.Tipo.DESKTOP, farmacia=self.farmacia,
        )
        resp = self._post('/api/v1/mantenimientos/', {
            'equipos': [otro.pk], 'descripcion': 'No imprime',
            'estado_general': EstadoGeneralEquipo.NO_OPERATIVO,
        })
        self.assertEqual(resp.status_code, 201, resp.content)


class HoraRealDeAccionesOfflineTests(TestCase):
    """BUG-3 (docs/modulos.md): la cola offline no mandaba CUÁNDO ocurrió la acción, y
    el backend la fechaba al recibirla. Un cierre hecho a las 10:00 sin señal y subido
    a las 18:00 quedaba a las 18:00 — corriendo el SLA y la ventana de verificación GPS.
    """

    def setUp(self):
        from rest_framework.authtoken.models import Token
        self.tecnico = User.objects.create_user(username='tec_hora', password='x')
        otorgar(self.tecnico, *PERMISOS_APP_CAMPO)
        PerfilUsuario.objects.create(usuario=self.tecnico, acceso_todas_unidades=True)
        self.token = Token.objects.create(user=self.tecnico)
        sg = UnidadNegocio.objects.get(codigo='SG')
        self.farmacia = Farmacia.objects.create(
            codigo='ML902', grupo=Grupo.objects.create(codigo='TRX902'), unidad_negocio=sg,
            nombre='Farmacia hora real', latitud=-2.170998, longitud=-79.922359,
        )
        self.equipo = Activo.objects.create(
            codigo='CR-DSK-9903', tipo=Activo.Tipo.DESKTOP, farmacia=self.farmacia,
        )
        self.mantenimiento = crear_mantenimiento_manual(
            equipos=[self.equipo], tecnico=self.tecnico, descripcion='POS no enciende',
            fecha_programada=timezone.now() - timedelta(hours=9), usuario=self.tecnico,
        )

    def _auth(self):
        return {'HTTP_AUTHORIZATION': f'Token {self.token.key}'}

    def _post(self, ruta, cuerpo=None):
        return self.client.post(ruta, cuerpo or {}, content_type='application/json', **self._auth())

    # --- Se respeta la hora declarada ------------------------------------------

    def test_el_cierre_conserva_la_hora_en_que_ocurrio(self):
        hace_ocho_horas = timezone.now() - timedelta(hours=8)
        resp = self._post(
            f'/api/v1/mantenimientos/{self.mantenimiento.pk}/cerrar/',
            {'resultado_tecnico': ResultadoTecnico.REPARADO, 'ocurrido_en': hace_ocho_horas.isoformat()},
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        self.mantenimiento.refresh_from_db()
        self.assertAlmostEqual(
            self.mantenimiento.fecha_cierre, hace_ocho_horas, delta=timedelta(seconds=2),
        )

    def test_la_llegada_conserva_su_hora_y_arregla_el_sla_de_respuesta(self):
        """El caso que motivó todo: llegó en plazo pero sincronizó tarde."""
        AcuerdoNivelServicio.objects.update_or_create(
            prioridad=PrioridadMantenimiento.NORMAL,
            defaults={'horas_respuesta': 4, 'horas_resolucion': 24, 'activo': True},
        )
        llegada_en_plazo = self.mantenimiento.fecha_programada + timedelta(hours=1)
        resp = self._post(
            f'/api/v1/mantenimientos/{self.mantenimiento.pk}/iniciar/',
            {'ocurrido_en': llegada_en_plazo.isoformat()},
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        self.mantenimiento.refresh_from_db()
        self.assertAlmostEqual(self.mantenimiento.inicio_real, llegada_en_plazo, delta=timedelta(seconds=2))
        # Sin el fix, `inicio_real` seria AHORA (9 h despues de la fecha programada,
        # contra un SLA de respuesta de 4 h) y esto daria True.
        self.assertFalse(self.mantenimiento.sla_respuesta_incumplido)

    def test_la_firma_conserva_la_hora_en_que_firmo_el_custodio(self):
        iniciar_mantenimiento(mantenimiento=self.mantenimiento, usuario=self.tecnico)
        firmado = timezone.now() - timedelta(hours=6)
        resp = self._post(
            f'/api/v1/mantenimientos/{self.mantenimiento.pk}/firmar/',
            {'tipo_firma': 'tecnico', 'firma_base64': 'xx', 'ocurrido_en': firmado.isoformat()},
        )
        self.assertEqual(resp.status_code, 201, resp.content)
        self.assertAlmostEqual(
            self.mantenimiento.firmas.get().firmado_en, firmado, delta=timedelta(seconds=2),
        )

    def test_la_visita_conserva_llegada_y_cierre(self):
        visita = crear_visita_tecnica(
            farmacia=self.farmacia, tecnico=self.tecnico, fecha_planificada=timezone.localdate(),
            motivo='ruta', usuario=self.tecnico,
        )
        llegada = timezone.now() - timedelta(hours=7)
        cierre = timezone.now() - timedelta(hours=5)
        self.assertEqual(
            self._post(f'/api/v1/visitas/{visita.pk}/iniciar/', {'ocurrido_en': llegada.isoformat()}).status_code,
            200,
        )
        self.assertEqual(
            self._post(
                f'/api/v1/visitas/{visita.pk}/cerrar/',
                {'observaciones': 'ok', 'ocurrido_en': cierre.isoformat()},
            ).status_code,
            200,
        )
        visita.refresh_from_db()
        self.assertAlmostEqual(visita.fecha_inicio, llegada, delta=timedelta(seconds=2))
        self.assertAlmostEqual(visita.fecha_cierre, cierre, delta=timedelta(seconds=2))

    def test_la_ventana_gps_de_la_visita_usa_las_horas_reales(self):
        """Lo que hacía fallar la verificación: la posición se registró EN la farmacia
        a media mañana, pero la ventana se corría al momento de sincronizar."""
        visita = crear_visita_tecnica(
            farmacia=self.farmacia, tecnico=self.tecnico, fecha_planificada=timezone.localdate(),
            motivo='ruta', usuario=self.tecnico,
        )
        llegada = timezone.now() - timedelta(hours=7)
        UbicacionTecnico.objects.create(
            usuario=self.tecnico, latitud=self.farmacia.latitud, longitud=self.farmacia.longitud,
            timestamp_captura=llegada + timedelta(minutes=10),
        )
        self._post(f'/api/v1/visitas/{visita.pk}/iniciar/', {'ocurrido_en': llegada.isoformat()})
        self._post(
            f'/api/v1/visitas/{visita.pk}/cerrar/',
            {'ocurrido_en': (llegada + timedelta(hours=1)).isoformat()},
        )
        visita.refresh_from_db()
        self.assertEqual(visita.presencia_en_sitio, 'verificada')

    # --- Compatibilidad con telefonos sin actualizar ---------------------------

    def test_sin_el_campo_sigue_usando_la_hora_del_servidor(self):
        """CRÍTICO: la app se distribuye a mano, así que conviven APKs con y sin este
        campo durante semanas. El viejo NO puede romperse."""
        antes = timezone.now()
        resp = self._post(
            f'/api/v1/mantenimientos/{self.mantenimiento.pk}/cerrar/',
            {'resultado_tecnico': ResultadoTecnico.REPARADO},
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        self.mantenimiento.refresh_from_db()
        self.assertGreaterEqual(self.mantenimiento.fecha_cierre, antes)

    def test_iniciar_sin_cuerpo_sigue_funcionando(self):
        """El APK viejo hace POST sin cuerpo: no puede dar 400."""
        resp = self.client.post(
            f'/api/v1/mantenimientos/{self.mantenimiento.pk}/iniciar/', **self._auth(),
        )
        self.assertEqual(resp.status_code, 200, resp.content)

    def test_el_campo_en_null_equivale_a_no_mandarlo(self):
        resp = self._post(
            f'/api/v1/mantenimientos/{self.mantenimiento.pk}/cerrar/',
            {'resultado_tecnico': ResultadoTecnico.REPARADO, 'ocurrido_en': None},
        )
        self.assertEqual(resp.status_code, 200, resp.content)

    # --- Validacion ------------------------------------------------------------

    def test_una_hora_futura_se_rechaza(self):
        resp = self._post(
            f'/api/v1/mantenimientos/{self.mantenimiento.pk}/cerrar/',
            {'resultado_tecnico': ResultadoTecnico.REPARADO,
             'ocurrido_en': (timezone.now() + timedelta(hours=3)).isoformat()},
        )
        self.assertEqual(resp.status_code, 400)
        self.mantenimiento.refresh_from_db()
        self.assertEqual(self.mantenimiento.estado_interno, Mantenimiento.EstadoInterno.PENDIENTE)

    def test_una_hora_absurdamente_vieja_se_rechaza(self):
        resp = self._post(
            f'/api/v1/mantenimientos/{self.mantenimiento.pk}/cerrar/',
            {'resultado_tecnico': ResultadoTecnico.REPARADO,
             'ocurrido_en': (timezone.now() - timedelta(days=400)).isoformat()},
        )
        self.assertEqual(resp.status_code, 400)

    def test_un_desfase_chico_de_reloj_se_tolera(self):
        """Ningún teléfono tiene el reloj perfecto; un minuto adelantado no es un error."""
        resp = self._post(
            f'/api/v1/mantenimientos/{self.mantenimiento.pk}/cerrar/',
            {'resultado_tecnico': ResultadoTecnico.REPARADO,
             'ocurrido_en': (timezone.now() + timedelta(minutes=1)).isoformat()},
        )
        self.assertEqual(resp.status_code, 200, resp.content)

    def test_varios_dias_sin_senal_siguen_siendo_validos(self):
        """Un técnico puede estar días sin conexión: su trabajo no se rechaza."""
        resp = self._post(
            f'/api/v1/mantenimientos/{self.mantenimiento.pk}/cerrar/',
            {'resultado_tecnico': ResultadoTecnico.REPARADO,
             'ocurrido_en': (timezone.now() - timedelta(days=6)).isoformat()},
        )
        self.assertEqual(resp.status_code, 200, resp.content)


class FirmaIdempotenteTests(TestCase):
    """Mitad barata de BUG-4: el reintento de la cola offline duplicaba firmas.

    Si el servidor procesa el POST y la respuesta se pierde (timeout de 20 s en
    api.dart, que la app traduce a SinConexion), la accion se reencola y se reintenta.
    `create()` a secas dejaba DOS firmas del mismo custodio.
    """

    def setUp(self):
        self.tecnico = User.objects.create_user(username='tec_firma', password='x')
        equipo = Activo.objects.create(codigo='CR-DSK-9904', tipo=Activo.Tipo.DESKTOP)
        self.mantenimiento = crear_mantenimiento_manual(
            equipos=[equipo], tecnico=self.tecnico, descripcion='firma',
            fecha_programada=timezone.now(), usuario=self.tecnico,
        )

    def _firmar(self, firma_base64='AAA', tipo='custodio'):
        from apps.mantenimiento.services import firmar_mantenimiento
        return firmar_mantenimiento(
            mantenimiento=self.mantenimiento, tipo_firma=tipo,
            firma_base64=firma_base64, usuario=self.tecnico,
        )

    def test_reintentar_la_misma_firma_no_la_duplica(self):
        primera = self._firmar()
        segunda = self._firmar()
        self.assertEqual(primera.pk, segunda.pk)
        self.assertEqual(self.mantenimiento.firmas.count(), 1)

    def test_custodio_y_tecnico_conviven(self):
        self._firmar(tipo='custodio')
        self._firmar(tipo='tecnico')
        self.assertEqual(self.mantenimiento.firmas.count(), 2)

    def test_volver_a_firmar_reemplaza_en_vez_de_acumular(self):
        self._firmar(firma_base64='vieja')
        self._firmar(firma_base64='corregida')
        self.assertEqual(self.mantenimiento.firmas.count(), 1)
        self.assertEqual(self.mantenimiento.firmas.get().firma_base64, 'corregida')

    def test_la_base_lo_impide_aunque_alguien_esquive_el_servicio(self):
        from django.db import IntegrityError, transaction

        from apps.mantenimiento.models import FirmaMantenimiento
        self._firmar()
        with self.assertRaises(IntegrityError), transaction.atomic():
            FirmaMantenimiento.objects.create(
                mantenimiento=self.mantenimiento, tipo_firma='custodio', firma_base64='otra',
            )


class _BaseConflictoTests(TestCase):
    """Armado común de los tres escenarios de BUG-4."""

    def setUp(self):
        from rest_framework.authtoken.models import Token
        self.tecnico = User.objects.create_user(username='tec_conf', password='x')
        otorgar(self.tecnico, *PERMISOS_APP_CAMPO)
        PerfilUsuario.objects.create(usuario=self.tecnico, acceso_todas_unidades=True)
        self.token = Token.objects.create(user=self.tecnico)

        self.mesa = User.objects.create_user(username='mesa_conf', password='x')
        otorgar(
            self.mesa, 'mantenimiento.view_mantenimiento', 'mantenimiento.change_mantenimiento',
            'mantenimiento.view_cierreenconflicto', 'mantenimiento.change_cierreenconflicto',
        )
        PerfilUsuario.objects.create(usuario=self.mesa, acceso_todas_unidades=True)

        sg = UnidadNegocio.objects.get(codigo='SG')
        self.farmacia = Farmacia.objects.create(
            codigo='ML910', grupo=Grupo.objects.create(codigo='TRX910'), unidad_negocio=sg,
            nombre='Farmacia conflicto', latitud=-2.170998, longitud=-79.922359,
        )
        self.equipo = Activo.objects.create(
            codigo='CR-DSK-9910', tipo=Activo.Tipo.DESKTOP, farmacia=self.farmacia,
            estado=Activo.Estado.EN_REPARACION,
        )
        self.mantenimiento = crear_mantenimiento_manual(
            equipos=[self.equipo], tecnico=self.tecnico, descripcion='POS no enciende',
            fecha_programada=timezone.now() - timedelta(hours=2), usuario=self.tecnico,
        )
        iniciar_mantenimiento(mantenimiento=self.mantenimiento, usuario=self.tecnico)

    def _auth(self):
        return {'HTTP_AUTHORIZATION': f'Token {self.token.key}'}

    def _cerrar(self, **extra):
        cuerpo = {'resultado_tecnico': ResultadoTecnico.REPARADO, 'tiempo_real_minutos': 40}
        cuerpo.update(extra)
        return self.client.post(
            f'/api/v1/mantenimientos/{self.mantenimiento.pk}/cerrar/',
            cuerpo, content_type='application/json', **self._auth(),
        )


class EscenarioReintentoSimpleTests(_BaseConflictoTests):
    """(a) Se corta la red DESPUÉS de que el servidor proceso: la app reencola y
    reintenta la misma acción. No puede duplicar ni re-fallar."""

    def test_reintentar_un_cierre_devuelve_lo_mismo_sin_re_fallar(self):
        primera = self._cerrar(origen_id=7)
        self.assertEqual(primera.status_code, 200)

        segunda = self._cerrar(origen_id=7)
        # Sin idempotencia esto seria un 409 ("ya esta cerrado") y la accion quedaria
        # trabada en el telefono para siempre.
        self.assertEqual(segunda.status_code, 200)
        self.assertEqual(segunda.json()['id'], primera.json()['id'])
        self.assertEqual(segunda.json()['estado_interno'], 'cerrado')

    def test_el_reintento_no_crea_un_conflicto_fantasma(self):
        self._cerrar(origen_id=7)
        self._cerrar(origen_id=7)
        self.assertEqual(CierreEnConflicto.objects.count(), 0)

    def test_el_reintento_no_duplica_eventos_ni_mueve_el_activo(self):
        self._cerrar(origen_id=7)
        eventos = self.mantenimiento.eventos.filter(
            tipo_evento=EventoMantenimiento.TipoEvento.CERRADO,
        ).count()
        self._cerrar(origen_id=7)
        self.assertEqual(
            self.mantenimiento.eventos.filter(
                tipo_evento=EventoMantenimiento.TipoEvento.CERRADO,
            ).count(),
            eventos,
        )

    def test_reintentar_una_firma_no_la_duplica(self):
        cuerpo = {'tipo_firma': 'custodio', 'firma_base64': 'AAA', 'origen_id': 11}
        for _ in range(2):
            resp = self.client.post(
                f'/api/v1/mantenimientos/{self.mantenimiento.pk}/firmar/',
                cuerpo, content_type='application/json', **self._auth(),
            )
            self.assertEqual(resp.status_code, 201)
        self.assertEqual(self.mantenimiento.firmas.count(), 1)

    def test_una_accion_distinta_del_mismo_telefono_si_se_aplica(self):
        """La clave es por acción, no por dispositivo: otro origen_id es otro hecho."""
        self.assertEqual(self._cerrar(origen_id=7).status_code, 200)
        otro = Activo.objects.create(codigo='CR-DSK-9911', tipo=Activo.Tipo.DESKTOP)
        resp = self.client.post(
            '/api/v1/mantenimientos/',
            {'equipos': [otro.pk], 'descripcion': 'otro', 'estado_general': 'no_operativo'},
            content_type='application/json', **self._auth(),
        )
        self.assertEqual(resp.status_code, 201)

    def test_sin_origen_id_se_comporta_como_siempre(self):
        """APK viejo: no manda la clave, no tiene proteccion -- pero no se rompe."""
        self.assertEqual(self._cerrar().status_code, 200)
        self.assertEqual(self._cerrar().status_code, 409)


class EscenarioConflictoRealTests(_BaseConflictoTests):
    """(b) El técnico cierra sin señal mientras mesa de ayuda cancela desde el panel."""

    def _cancelar_desde_el_panel(self):
        cancelar_mantenimiento(
            mantenimiento=self.mantenimiento, motivo='Falsa alarma', usuario=self.mesa,
        )

    def test_devuelve_409_con_codigo_distinguible(self):
        self._cancelar_desde_el_panel()
        resp = self._cerrar(origen_id=3)
        self.assertEqual(resp.status_code, 409)
        cuerpo = resp.json()
        self.assertEqual(cuerpo['codigo'], 'conflicto_de_estado')
        self.assertEqual(cuerpo['estado_actual'], 'cancelado')
        self.assertEqual(cuerpo['modificado_por'], 'mesa_conf')
        self.assertIsNotNone(cuerpo['modificado_en'])

    def test_no_pisa_la_decision_del_panel(self):
        self._cancelar_desde_el_panel()
        self._cerrar(origen_id=3)
        self.mantenimiento.refresh_from_db()
        self.assertEqual(self.mantenimiento.estado_interno, Mantenimiento.EstadoInterno.CANCELADO)

    def test_el_trabajo_del_tecnico_queda_guardado(self):
        self._cancelar_desde_el_panel()
        hace_tres_horas = timezone.now() - timedelta(hours=3)
        self._cerrar(origen_id=3, ocurrido_en=hace_tres_horas.isoformat())

        conflicto = CierreEnConflicto.objects.get()
        self.assertEqual(conflicto.mantenimiento, self.mantenimiento)
        self.assertEqual(conflicto.tecnico, self.tecnico)
        self.assertEqual(conflicto.payload_rechazado['resultado_tecnico'], ResultadoTecnico.REPARADO)
        self.assertEqual(conflicto.payload_rechazado['tiempo_real_minutos'], 40)
        self.assertEqual(conflicto.estado_al_llegar, 'cancelado')
        self.assertFalse(conflicto.revisado)
        self.assertAlmostEqual(conflicto.ocurrido_en, hace_tres_horas, delta=timedelta(seconds=2))

    def test_sale_la_notificacion_apenas_se_crea(self):
        self._cancelar_desde_el_panel()
        self._cerrar(origen_id=3)
        avisos = Notificacion.objects.filter(usuario=self.mesa, mantenimiento=self.mantenimiento)
        self.assertTrue(avisos.filter(mensaje__contains='Necesita revisión').exists())

    def test_no_se_le_avisa_al_tecnico_que_lo_cerro(self):
        """Su app ya se lo dice; el aviso in-app es para que alguien del panel actúe."""
        self._cancelar_desde_el_panel()
        self._cerrar(origen_id=3)
        self.assertFalse(
            Notificacion.objects.filter(
                usuario=self.tecnico, mensaje__contains='Necesita revisión',
            ).exists(),
        )

    def test_reintentar_el_mismo_conflicto_no_lo_duplica(self):
        self._cancelar_desde_el_panel()
        self._cerrar(origen_id=3)
        self._cerrar(origen_id=3)
        self.assertEqual(CierreEnConflicto.objects.count(), 1)

    # --- Las dos acciones del panel -------------------------------------------

    def test_aplicar_el_cierre_del_tecnico(self):
        self._cancelar_desde_el_panel()
        ocurrido = timezone.now() - timedelta(hours=3)
        self._cerrar(origen_id=3, ocurrido_en=ocurrido.isoformat())
        conflicto = CierreEnConflicto.objects.get()

        self.client.force_login(self.mesa)
        resp = self.client.post(reverse('panel:cierre_en_conflicto_aplicar', args=[conflicto.pk]))
        self.assertEqual(resp.status_code, 302)

        self.mantenimiento.refresh_from_db()
        self.assertEqual(self.mantenimiento.estado_interno, Mantenimiento.EstadoInterno.CERRADO)
        self.assertEqual(self.mantenimiento.resultado_tecnico, ResultadoTecnico.REPARADO)
        self.assertEqual(self.mantenimiento.tiempo_real_minutos, 40)
        # Con la hora REAL del cierre en campo, no la de la revisión.
        self.assertAlmostEqual(self.mantenimiento.fecha_cierre, ocurrido, delta=timedelta(seconds=2))

        conflicto.refresh_from_db()
        self.assertTrue(conflicto.revisado)
        self.assertEqual(conflicto.revisado_por, self.mesa)
        self.assertIsNotNone(conflicto.revisado_en)

    def test_aplicar_pasa_por_el_servicio_completo_y_devuelve_el_activo(self):
        """Reabre y cierra de verdad, no escribe los campos a mano: si no, el activo se
        quedaría 'en reparación' para siempre."""
        self._cancelar_desde_el_panel()
        self._cerrar(origen_id=3, estado_general=EstadoGeneralEquipo.OPERATIVO)
        conflicto = CierreEnConflicto.objects.get()

        self.client.force_login(self.mesa)
        self.client.post(reverse('panel:cierre_en_conflicto_aplicar', args=[conflicto.pk]))

        self.equipo.refresh_from_db()
        self.assertNotEqual(self.equipo.estado, Activo.Estado.EN_REPARACION)

    def test_descartar_con_motivo(self):
        self._cancelar_desde_el_panel()
        self._cerrar(origen_id=3)
        conflicto = CierreEnConflicto.objects.get()

        self.client.force_login(self.mesa)
        resp = self.client.post(
            reverse('panel:cierre_en_conflicto_descartar', args=[conflicto.pk]),
            {'motivo': 'El equipo se reemplazo entero, el cierre no aplica.'},
        )
        self.assertEqual(resp.status_code, 302)

        conflicto.refresh_from_db()
        self.assertTrue(conflicto.revisado)
        self.assertEqual(conflicto.revisado_por, self.mesa)
        self.assertIn('se reemplazo entero', conflicto.resolucion)
        # El payload NO se borra: es la constancia de un trabajo que no se contabilizo.
        self.assertEqual(conflicto.payload_rechazado['resultado_tecnico'], ResultadoTecnico.REPARADO)
        self.mantenimiento.refresh_from_db()
        self.assertEqual(self.mantenimiento.estado_interno, Mantenimiento.EstadoInterno.CANCELADO)

    def test_no_se_puede_resolver_dos_veces(self):
        self._cancelar_desde_el_panel()
        self._cerrar(origen_id=3)
        conflicto = CierreEnConflicto.objects.get()
        descartar_cierre_en_conflicto(conflicto=conflicto, usuario=self.mesa, motivo='ya')
        with self.assertRaises(ValueError):
            aplicar_cierre_en_conflicto(conflicto=conflicto, usuario=self.mesa)

    def test_la_lista_del_panel_lo_muestra(self):
        self._cancelar_desde_el_panel()
        self._cerrar(origen_id=3)
        self.client.force_login(self.mesa)
        resp = self.client.get(reverse('panel:cierres_en_conflicto_lista'))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, f'#{self.mantenimiento.pk}')

    def test_sin_permiso_de_resolver_no_se_puede_aplicar(self):
        """Mesa de Ayuda VE la bandeja pero no interviene."""
        self._cancelar_desde_el_panel()
        self._cerrar(origen_id=3)
        conflicto = CierreEnConflicto.objects.get()

        mirona = User.objects.create_user(username='solo_mira', password='x')
        otorgar(mirona, 'mantenimiento.view_cierreenconflicto')
        PerfilUsuario.objects.create(usuario=mirona, acceso_todas_unidades=True)
        self.client.force_login(mirona)

        self.assertEqual(self.client.get(reverse('panel:cierres_en_conflicto_lista')).status_code, 200)
        resp = self.client.post(reverse('panel:cierre_en_conflicto_aplicar', args=[conflicto.pk]))
        self.assertEqual(resp.status_code, 403)

    def test_una_visita_ya_cerrada_tambien_da_409(self):
        visita = crear_visita_tecnica(
            farmacia=self.farmacia, tecnico=self.tecnico, fecha_planificada=timezone.localdate(),
            motivo='ruta', usuario=self.tecnico,
        )
        cerrar_visita_tecnica(visita=visita, usuario=self.mesa)
        resp = self.client.post(
            f'/api/v1/visitas/{visita.pk}/cerrar/', {'observaciones': 'tarde'},
            content_type='application/json', **self._auth(),
        )
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.json()['codigo'], 'conflicto_de_estado')


class EscenarioEscalamientoTests(_BaseConflictoTests):
    """(c) Un conflicto que nadie revisó tiene que insistir solo."""

    def _crear_conflicto(self):
        cancelar_mantenimiento(
            mantenimiento=self.mantenimiento, motivo='Falsa alarma', usuario=self.mesa,
        )
        self._cerrar(origen_id=3)
        return CierreEnConflicto.objects.get()

    def _envejecer(self, conflicto, horas):
        CierreEnConflicto.objects.filter(pk=conflicto.pk).update(
            creado_en=timezone.now() - timedelta(hours=horas),
        )

    def test_uno_viejo_sin_revisar_escala(self):
        conflicto = self._crear_conflicto()
        self._envejecer(conflicto, HORAS_ESCALAMIENTO_CONFLICTO + 1)
        avisos_antes = Notificacion.objects.filter(usuario=self.mesa).count()

        self.assertEqual(escalar_cierres_en_conflicto(), 1)

        conflicto.refresh_from_db()
        self.assertIsNotNone(conflicto.escalado_en)
        self.assertGreater(Notificacion.objects.filter(usuario=self.mesa).count(), avisos_antes)
        self.assertTrue(
            Notificacion.objects.filter(
                usuario=self.mesa, mensaje__startswith='SIGUE SIN REVISARSE',
            ).exists(),
        )

    def test_uno_reciente_no_escala(self):
        conflicto = self._crear_conflicto()
        self._envejecer(conflicto, HORAS_ESCALAMIENTO_CONFLICTO - 1)
        self.assertEqual(escalar_cierres_en_conflicto(), 0)

    def test_uno_ya_revisado_no_escala(self):
        conflicto = self._crear_conflicto()
        self._envejecer(conflicto, HORAS_ESCALAMIENTO_CONFLICTO + 5)
        descartar_cierre_en_conflicto(conflicto=conflicto, usuario=self.mesa, motivo='visto')
        self.assertEqual(escalar_cierres_en_conflicto(), 0)

    def test_no_escala_dos_veces(self):
        """`escalado_en` corta: si no, insistiría en cada corrida de beat (cada 15 min)."""
        conflicto = self._crear_conflicto()
        self._envejecer(conflicto, HORAS_ESCALAMIENTO_CONFLICTO + 1)
        self.assertEqual(escalar_cierres_en_conflicto(), 1)
        self.assertEqual(escalar_cierres_en_conflicto(), 0)

    def test_la_tarea_de_celery_lo_corre(self):
        from apps.mantenimiento.tasks import escalar_cierres_en_conflicto_task

        conflicto = self._crear_conflicto()
        self._envejecer(conflicto, HORAS_ESCALAMIENTO_CONFLICTO + 1)
        self.assertIn('1 cierre(s)', escalar_cierres_en_conflicto_task())

    def test_el_centro_de_monitoreo_muestra_el_conteo(self):
        from apps.monitoreo.services import resumen_operacion

        self._crear_conflicto()
        self.assertEqual(resumen_operacion()['cierres_en_conflicto'], 1)

    def test_el_conteo_baja_al_revisarlo(self):
        from apps.monitoreo.services import resumen_operacion

        conflicto = self._crear_conflicto()
        descartar_cierre_en_conflicto(conflicto=conflicto, usuario=self.mesa, motivo='visto')
        self.assertEqual(resumen_operacion()['cierres_en_conflicto'], 0)


class ConflictoNoRompeElPanelTests(TestCase):
    """`ConflictoDeEstado` hereda de `ValueError` a propósito.

    Las tres vistas del panel que llaman a estos servicios ya capturaban `ValueError` y
    lo mostraban como error del formulario. Si el conflicto fuera un `Exception` suelto,
    cancelar dos veces desde la web —que hoy funciona— pasaría a ser un 500.
    """

    def setUp(self):
        self.usuario = User.objects.create_user(username='panel_conf', password='x')
        otorgar(
            self.usuario, 'mantenimiento.view_mantenimiento', 'mantenimiento.change_mantenimiento',
            'mantenimiento.view_visitatecnica', 'mantenimiento.change_visitatecnica',
        )
        PerfilUsuario.objects.create(usuario=self.usuario, acceso_todas_unidades=True)
        equipo = Activo.objects.create(codigo='CR-DSK-9920', tipo=Activo.Tipo.DESKTOP)
        self.mantenimiento = crear_mantenimiento_manual(
            equipos=[equipo], tecnico=self.usuario, descripcion='x',
            fecha_programada=timezone.now(), usuario=self.usuario,
        )

    def test_es_un_valueerror(self):
        from apps.mantenimiento.services import ConflictoDeEstado

        self.assertTrue(issubclass(ConflictoDeEstado, ValueError))

    def test_cancelar_dos_veces_desde_el_panel_no_es_un_500(self):
        cancelar_mantenimiento(
            mantenimiento=self.mantenimiento, motivo='primera', usuario=self.usuario,
        )
        self.client.force_login(self.usuario)
        resp = self.client.post(
            reverse('panel:mantenimiento_cancelar', args=[self.mantenimiento.pk]),
            {'motivo': 'segunda'},
        )
        # 200 = vuelve a mostrar el formulario con el error, que es lo que hacía antes.
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'no puede cancelarse de nuevo')

    def test_cerrar_uno_ya_cerrado_desde_el_panel_tampoco(self):
        iniciar_mantenimiento(mantenimiento=self.mantenimiento, usuario=self.usuario)
        cerrar_mantenimiento(
            mantenimiento=self.mantenimiento, resultado_tecnico=ResultadoTecnico.REPARADO,
            usuario=self.usuario,
        )
        self.client.force_login(self.usuario)
        resp = self.client.post(
            reverse('panel:mantenimiento_cerrar', args=[self.mantenimiento.pk]),
            {'resultado_tecnico': ResultadoTecnico.REPARADO},
        )
        self.assertEqual(resp.status_code, 200)


class ConsentimientoVigenteTests(TestCase):
    """BUG-6: el consentimiento se validaba con `.exists()` sobre el HISTÓRICO, así que
    una revocación no revocaba nada — el `True` viejo seguía ahí y el servidor seguía
    guardando posiciones de alguien que había dicho que no.
    """

    def setUp(self):
        from rest_framework.authtoken.models import Token
        self.tecnico = User.objects.create_user(username='tec_gps', password='x')
        otorgar(self.tecnico, *PERMISOS_APP_CAMPO)
        PerfilUsuario.objects.create(usuario=self.tecnico, acceso_todas_unidades=True)
        self.token = Token.objects.create(user=self.tecnico)

    def _auth(self):
        return {'HTTP_AUTHORIZATION': f'Token {self.token.key}'}

    def _consentir(self, aceptado=True):
        return self.client.post(
            '/api/v1/consentimiento-monitoreo/',
            {'aceptado': aceptado, 'version_terminos': '1.0'},
            content_type='application/json', **self._auth(),
        )

    def _enviar_posicion(self, **extra):
        cuerpo = {
            'latitud': -2.170998, 'longitud': -79.922359,
            'timestamp_captura': timezone.now().isoformat(),
        }
        cuerpo.update(extra)
        return self.client.post(
            '/api/v1/ubicaciones-tecnico/', cuerpo,
            content_type='application/json', **self._auth(),
        )

    # --- Lo que ya funcionaba ---------------------------------------------------

    def test_sin_consentimiento_no_se_acepta_la_posicion(self):
        resp = self._enviar_posicion()
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(UbicacionTecnico.objects.count(), 0)

    def test_con_consentimiento_se_acepta(self):
        self._consentir()
        self.assertEqual(self._enviar_posicion().status_code, 201)
        self.assertEqual(UbicacionTecnico.objects.count(), 1)

    # --- El bug ------------------------------------------------------------------

    def test_revocar_corta_el_registro_de_posiciones(self):
        self._consentir(aceptado=True)
        self.assertEqual(self._enviar_posicion().status_code, 201)

        self._consentir(aceptado=False)

        resp = self._enviar_posicion(origen_id=2)
        # Antes esto daba 201: el `.exists()` seguía encontrando el True viejo.
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(resp.json()['codigo'], 'sin_consentimiento')
        self.assertEqual(UbicacionTecnico.objects.count(), 1)

    def test_volver_a_aceptar_lo_reactiva(self):
        """Revocar no es irreversible: el vigente es siempre el último."""
        self._consentir(aceptado=True)
        self._consentir(aceptado=False)
        self.assertEqual(self._enviar_posicion(origen_id=1).status_code, 403)

        self._consentir(aceptado=True)
        self.assertEqual(self._enviar_posicion(origen_id=2).status_code, 201)

    def test_la_revocacion_queda_registrada_y_no_borra_el_historial(self):
        """Append-only: hay que poder demostrar qué se aceptó y cuándo, y retirarlo es
        un hecho tan registrable como darlo."""
        from apps.mantenimiento.models import ConsentimientoMonitoreo

        self._consentir(aceptado=True)
        self._consentir(aceptado=False)
        filas = ConsentimientoMonitoreo.objects.filter(usuario=self.tecnico).order_by('timestamp', 'pk')
        self.assertEqual([f.aceptado for f in filas], [True, False])

    def test_el_get_refleja_el_estado_vigente(self):
        """Las dos mitades del mismo flujo tienen que coincidir: antes el GET miraba el
        último y el POST de ubicación miraba el histórico."""
        self._consentir(aceptado=True)
        self.assertTrue(self.client.get('/api/v1/consentimiento-monitoreo/', **self._auth()).json()['aceptado'])

        self._consentir(aceptado=False)
        self.assertFalse(self.client.get('/api/v1/consentimiento-monitoreo/', **self._auth()).json()['aceptado'])

    def test_el_consentimiento_de_otro_no_habilita_el_propio(self):
        otro = User.objects.create_user(username='otro_gps', password='x')
        from apps.mantenimiento.models import ConsentimientoMonitoreo
        ConsentimientoMonitoreo.objects.create(usuario=otro, aceptado=True, version_terminos='1.0')
        self.assertEqual(self._enviar_posicion().status_code, 403)


class RetencionDeUbicacionesTests(TestCase):
    """PROCESO-5: las posiciones no se purgaban NUNCA, pese a que el comentario de
    `cerrar_mantenimiento` lo afirmaba para justificar persistir la distancia."""

    def setUp(self):
        self.tecnico = User.objects.create_user(username='tec_purga', password='x')

    def _posicion(self, dias_atras):
        return UbicacionTecnico.objects.create(
            usuario=self.tecnico, latitud=-2.17, longitud=-79.92,
            timestamp_captura=timezone.now() - timedelta(days=dias_atras),
        )

    def test_borra_las_viejas_y_conserva_las_recientes(self):
        vieja = self._posicion(DIAS_RETENCION_UBICACIONES + 5)
        reciente = self._posicion(1)

        self.assertEqual(purgar_ubicaciones_antiguas(), 1)

        self.assertFalse(UbicacionTecnico.objects.filter(pk=vieja.pk).exists())
        self.assertTrue(UbicacionTecnico.objects.filter(pk=reciente.pk).exists())

    def test_la_ventana_cubre_la_cola_offline_mas_larga_posible(self):
        """La retención está ATADA a ANTIGUEDAD_MAXIMA: un cierre puede tardar hasta 30
        días en llegar, y purgar antes lo dejaría verificando contra posiciones ya
        borradas. Si alguien mueve una, tiene que mover la otra."""
        from apps.mantenimiento.serializers import ANTIGUEDAD_MAXIMA

        self.assertGreater(DIAS_RETENCION_UBICACIONES, ANTIGUEDAD_MAXIMA.days)

    def test_un_cierre_que_llega_tarde_todavia_verifica_la_presencia(self):
        sg = UnidadNegocio.objects.get(codigo='SG')
        farmacia = Farmacia.objects.create(
            codigo='ML930', grupo=Grupo.objects.create(codigo='TRX930'), unidad_negocio=sg,
            nombre='Farmacia purga', latitud=-2.170998, longitud=-79.922359,
        )
        equipo = Activo.objects.create(
            codigo='CR-DSK-9930', tipo=Activo.Tipo.DESKTOP, farmacia=farmacia,
        )
        hace_25_dias = timezone.now() - timedelta(days=25)
        mantenimiento = crear_mantenimiento_manual(
            equipos=[equipo], tecnico=self.tecnico, descripcion='POS',
            fecha_programada=hace_25_dias, usuario=self.tecnico,
        )
        iniciar_mantenimiento(
            mantenimiento=mantenimiento, usuario=self.tecnico, ocurrido_en=hace_25_dias,
        )
        UbicacionTecnico.objects.create(
            usuario=self.tecnico, latitud=farmacia.latitud, longitud=farmacia.longitud,
            timestamp_captura=hace_25_dias + timedelta(minutes=5),
        )

        purgar_ubicaciones_antiguas()

        cerrar_mantenimiento(
            mantenimiento=mantenimiento, resultado_tecnico=ResultadoTecnico.REPARADO,
            usuario=self.tecnico, ocurrido_en=hace_25_dias + timedelta(hours=1),
        )
        mantenimiento.refresh_from_db()
        self.assertEqual(mantenimiento.presencia_en_sitio, 'verificada')

    def test_la_tarea_de_celery_lo_corre(self):
        from apps.mantenimiento.tasks import purgar_ubicaciones_task

        self._posicion(DIAS_RETENCION_UBICACIONES + 1)
        self.assertIn('1 posicion', purgar_ubicaciones_task())

    def test_purgar_no_borra_la_distancia_ya_verificada(self):
        """Lo que se pierde es la posición cruda, no el hecho: la distancia se persiste
        al cerrar justo para sobrevivir a esta purga."""
        equipo = Activo.objects.create(codigo='CR-DSK-9931', tipo=Activo.Tipo.DESKTOP)
        mantenimiento = crear_mantenimiento_manual(
            equipos=[equipo], tecnico=self.tecnico, descripcion='x',
            fecha_programada=timezone.now(), usuario=self.tecnico,
        )
        Mantenimiento.objects.filter(pk=mantenimiento.pk).update(distancia_verificacion_metros=12.5)
        self._posicion(DIAS_RETENCION_UBICACIONES + 1)

        purgar_ubicaciones_antiguas()

        mantenimiento.refresh_from_db()
        self.assertEqual(mantenimiento.distancia_verificacion_metros, 12.5)
        self.assertEqual(mantenimiento.presencia_en_sitio, 'verificada')


class AltaAcotadaPorUnidadNegocioTests(TestCase):
    """BUG-9: los serializers de alta validaban contra TODA la base.

    Un `PrimaryKeyRelatedField` define su queryset en tiempo de import, así que aceptaba
    cualquier id existente aunque fuera de otra unidad de negocio. El panel no tiene ese
    agujero porque sus formularios arman los desplegables ya acotados.
    """

    def setUp(self):
        from rest_framework.authtoken.models import Token
        self.tecnico = User.objects.create_user(username='tec_mia', password='x')
        otorgar(self.tecnico, *PERMISOS_APP_CAMPO)
        self.token = Token.objects.create(user=self.tecnico)

        sg = UnidadNegocio.objects.get(codigo='SG')
        mia = UnidadNegocio.objects.get(codigo='MIA')
        # El técnico ve SOLO MIA.
        PerfilUsuario.objects.create(usuario=self.tecnico).unidades_negocio.add(mia)

        self.farmacia_mia = Farmacia.objects.create(
            codigo='MM940', grupo=Grupo.objects.create(codigo='TRX940'), unidad_negocio=mia,
            nombre='Farmacia MIA',
        )
        self.farmacia_sg = Farmacia.objects.create(
            codigo='ML940', grupo=Grupo.objects.create(codigo='TRX941'), unidad_negocio=sg,
            nombre='Farmacia SG',
        )
        # `unidad_negocio` explícita: es nullable y el vacío significa "compartido".
        self.equipo_ajeno = Activo.objects.create(
            codigo='CR-DSK-9940', tipo=Activo.Tipo.DESKTOP,
            farmacia=self.farmacia_sg, unidad_negocio=sg,
        )
        self.equipo_propio = Activo.objects.create(
            codigo='CR-DSK-9941', tipo=Activo.Tipo.DESKTOP,
            farmacia=self.farmacia_mia, unidad_negocio=mia,
        )
        self.equipo_compartido = Activo.objects.create(
            codigo='CR-DSK-9942', tipo=Activo.Tipo.DESKTOP,
        )

    def _auth(self):
        return {'HTTP_AUTHORIZATION': f'Token {self.token.key}'}

    def _crear_mantenimiento(self, equipo_pk):
        return self.client.post(
            '/api/v1/mantenimientos/',
            {'equipos': [equipo_pk], 'descripcion': 'x',
             'estado_general': EstadoGeneralEquipo.NO_OPERATIVO},
            content_type='application/json', **self._auth(),
        )

    def test_no_puede_abrir_un_mantenimiento_sobre_un_equipo_de_otra_unidad(self):
        resp = self._crear_mantenimiento(self.equipo_ajeno.pk)
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(Mantenimiento.objects.count(), 0)

    def test_sobre_el_propio_si(self):
        self.assertEqual(self._crear_mantenimiento(self.equipo_propio.pk).status_code, 201)

    def test_un_equipo_sin_unidad_sigue_siendo_de_todos(self):
        """`scope_opcional_*` y no la estricta: unidad_negocio es nullable y el panel
        trata el vacío como compartido. Con la estricta, un técnico acotado no habría
        podido abrir NINGÚN mantenimiento (registrar_ingreso no setea unidad_negocio)."""
        self.assertEqual(self._crear_mantenimiento(self.equipo_compartido.pk).status_code, 201)

    def test_no_puede_dar_de_alta_un_equipo_en_una_farmacia_ajena(self):
        resp = self.client.post(
            '/api/v1/equipos/nuevo/',
            {'tipo': Activo.Tipo.DESKTOP, 'farmacia': self.farmacia_sg.pk},
            content_type='application/json', **self._auth(),
        )
        self.assertEqual(resp.status_code, 400)

    def test_en_la_propia_si(self):
        resp = self.client.post(
            '/api/v1/equipos/nuevo/',
            {'tipo': Activo.Tipo.DESKTOP, 'farmacia': self.farmacia_mia.pk},
            content_type='application/json', **self._auth(),
        )
        self.assertEqual(resp.status_code, 201, resp.content)

    def test_quien_ve_todo_sigue_viendo_todo(self):
        sin_limite = User.objects.create_user(username='tec_total', password='x')
        otorgar(sin_limite, *PERMISOS_APP_CAMPO)
        PerfilUsuario.objects.create(usuario=sin_limite, acceso_todas_unidades=True)
        from rest_framework.authtoken.models import Token
        token = Token.objects.create(user=sin_limite)
        resp = self.client.post(
            '/api/v1/mantenimientos/',
            {'equipos': [self.equipo_ajeno.pk], 'descripcion': 'x',
             'estado_general': EstadoGeneralEquipo.NO_OPERATIVO},
            content_type='application/json', HTTP_AUTHORIZATION=f'Token {token.key}',
        )
        self.assertEqual(resp.status_code, 201, resp.content)


class AuditoriaDeLaAppTests(TestCase):
    """BUG-7: la API no auditaba nada y el panel auditaba 15 acciones.

    Para mantenimientos el hueco era parcial (EventoMantenimiento queda igual); para
    VISITAS no quedaba rastro en ningún lado, porque VisitaTecnica no tiene modelo de
    eventos propio.
    """

    def setUp(self):
        from rest_framework.authtoken.models import Token
        self.tecnico = User.objects.create_user(username='tec_audit', password='x')
        otorgar(self.tecnico, *PERMISOS_APP_CAMPO)
        PerfilUsuario.objects.create(usuario=self.tecnico, acceso_todas_unidades=True)
        self.token = Token.objects.create(user=self.tecnico)

        sg = UnidadNegocio.objects.get(codigo='SG')
        self.farmacia = Farmacia.objects.create(
            codigo='ML950', grupo=Grupo.objects.create(codigo='TRX950'), unidad_negocio=sg,
            nombre='Farmacia auditoria', latitud=-2.17, longitud=-79.92,
        )
        equipo = Activo.objects.create(
            codigo='CR-DSK-9950', tipo=Activo.Tipo.DESKTOP, farmacia=self.farmacia,
        )
        self.mantenimiento = crear_mantenimiento_manual(
            equipos=[equipo], tecnico=self.tecnico, descripcion='POS',
            fecha_programada=timezone.now(), usuario=self.tecnico,
        )

    def _auth(self):
        return {'HTTP_AUTHORIZATION': f'Token {self.token.key}'}

    def _post(self, ruta, cuerpo=None):
        return self.client.post(ruta, cuerpo or {}, content_type='application/json', **self._auth())

    def _acciones(self):
        from apps.auditoria.models import EventoAuditoria

        return list(EventoAuditoria.objects.values_list('accion', flat=True))

    def test_la_jornada_de_un_mantenimiento_queda_auditada(self):
        self._post(f'/api/v1/mantenimientos/{self.mantenimiento.pk}/iniciar/')
        self._post(
            f'/api/v1/mantenimientos/{self.mantenimiento.pk}/firmar/',
            {'tipo_firma': 'tecnico', 'firma_base64': 'x'},
        )
        self._post(
            f'/api/v1/mantenimientos/{self.mantenimiento.pk}/cerrar/',
            {'resultado_tecnico': ResultadoTecnico.REPARADO},
        )
        acciones = self._acciones()
        self.assertIn('mantenimiento.iniciar', acciones)
        self.assertIn('mantenimiento.firmar', acciones)
        self.assertIn('mantenimiento.cerrar', acciones)

    def test_la_visita_tambien_y_es_su_UNICO_rastro(self):
        visita = crear_visita_tecnica(
            farmacia=self.farmacia, tecnico=self.tecnico,
            fecha_planificada=timezone.localdate(), motivo='ruta', usuario=self.tecnico,
        )
        self._post(f'/api/v1/visitas/{visita.pk}/iniciar/')
        self._post(f'/api/v1/visitas/{visita.pk}/cerrar/', {'observaciones': 'ok'})
        acciones = self._acciones()
        self.assertIn('visita.iniciar', acciones)
        self.assertIn('visita.cerrar', acciones)

    def test_usa_los_mismos_nombres_que_el_panel(self):
        """Para que una consulta de auditoría no tenga que saber por qué superficie
        entró cada cosa."""
        self._post(f'/api/v1/mantenimientos/{self.mantenimiento.pk}/iniciar/')
        from apps.auditoria.models import EventoAuditoria

        evento = EventoAuditoria.objects.get(accion='mantenimiento.iniciar')
        self.assertEqual(evento.usuario, self.tecnico)
        self.assertEqual(evento.objeto_id, str(self.mantenimiento.pk))

    def test_se_distingue_que_vino_del_celular(self):
        self._post(f'/api/v1/mantenimientos/{self.mantenimiento.pk}/iniciar/')
        from apps.auditoria.models import EventoAuditoria

        evento = EventoAuditoria.objects.get(accion='mantenimiento.iniciar')
        self.assertEqual(evento.detalle.get('origen'), 'app_movil')

    def test_una_accion_rechazada_no_deja_fila(self):
        """Solo se audita lo que pasó: el 400 corta antes."""
        resp = self._post(
            f'/api/v1/mantenimientos/{self.mantenimiento.pk}/cerrar/',
            {'resultado_tecnico': 'invento'},
        )
        self.assertEqual(resp.status_code, 400)
        self.assertNotIn('mantenimiento.cerrar', self._acciones())

    def test_un_reintento_idempotente_no_duplica_la_fila(self):
        """La respuesta cacheada no vuelve a ejecutar la acción, así que tampoco la
        vuelve a auditar."""
        self._post(f'/api/v1/mantenimientos/{self.mantenimiento.pk}/iniciar/', {'origen_id': 5})
        self._post(f'/api/v1/mantenimientos/{self.mantenimiento.pk}/iniciar/', {'origen_id': 5})
        self.assertEqual(self._acciones().count('mantenimiento.iniciar'), 1)

    def test_el_alta_de_equipo_usa_el_nombre_del_panel(self):
        resp = self.client.post(
            '/api/v1/equipos/nuevo/',
            {'tipo': Activo.Tipo.DESKTOP, 'farmacia': self.farmacia.pk},
            content_type='application/json', **self._auth(),
        )
        self.assertEqual(resp.status_code, 201, resp.content)
        self.assertIn('activo.ingreso', self._acciones())
