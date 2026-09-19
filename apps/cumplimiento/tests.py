import datetime
from datetime import date

from django.contrib.auth.models import User
from django.test import TestCase

from apps.activos.models import Cargo, Colaborador, Departamento
from apps.auditoria.models import EventoAuditoria
from apps.catalogo.models import Estacion, Farmacia, Grupo, UnidadNegocio

from .models import (
    ActividadCumplimiento, EstadoCumplimiento, ResultadoCumplimientoColaborador,
    ResultadoCumplimientoEstacion, TipoObjetivoCumplimiento,
)
from .services import calcular_avance, generar_resultados, marcar_completado, resolver_objetivos


class ResolverObjetivosTests(TestCase):
    def setUp(self):
        self.usuario = User.objects.create_user(username='u', password='x')
        # La migración de datos de catalogo ya siembra SG/MIA — reutilizarlas, no recrearlas.
        self.sg = UnidadNegocio.objects.get(codigo='SG')
        self.mia = UnidadNegocio.objects.get(codigo='MIA')
        grupo = Grupo.objects.create(codigo='TRX001')

        self.farmacia_sg = Farmacia.objects.create(
            codigo='ML001', grupo=grupo, unidad_negocio=self.sg, fecha_apertura=date(2026, 8, 1),
        )
        self.farmacia_sg_vieja = Farmacia.objects.create(
            codigo='ML002', grupo=grupo, unidad_negocio=self.sg, fecha_apertura=date(2026, 1, 1),
        )
        self.farmacia_mia = Farmacia.objects.create(codigo='MAM01', grupo=grupo, unidad_negocio=self.mia)

        self.estacion_sg = Estacion.objects.create(
            codigo='ML001-A', farmacia=self.farmacia_sg, estado_aprobacion=Estacion.EstadoAprobacion.APROBADA,
        )
        self.estacion_mia = Estacion.objects.create(
            codigo='MAM01-A', farmacia=self.farmacia_mia, estado_aprobacion=Estacion.EstadoAprobacion.APROBADA,
        )

        depto = Departamento.objects.create(nombre='Operaciones')
        self.cargo_lider = Cargo.objects.create(nombre='Líder', departamento=depto)
        self.cargo_vendedor = Cargo.objects.create(nombre='Vendedor', departamento=depto)
        self.colaborador_lider = Colaborador.objects.create(
            nombre='Ana', cedula='001', cargo=self.cargo_lider, unidad_negocio=self.sg,
        )
        self.colaborador_vendedor = Colaborador.objects.create(
            nombre='Beto', cedula='002', cargo=self.cargo_vendedor, unidad_negocio=self.sg,
        )

    def _crear_actividad(self, **kwargs):
        actividad = ActividadCumplimiento.objects.create(creado_por=self.usuario, **kwargs)
        return actividad

    def test_resolver_estaciones_filtra_por_unidad_negocio(self):
        actividad = self._crear_actividad(
            nombre='AD', tipo_objetivo=TipoObjetivoCumplimiento.ESTACIONES, fecha_limite=date(2026, 10, 30),
        )
        actividad.unidades_negocio.add(self.sg)

        objetivos = list(resolver_objetivos(actividad))
        self.assertEqual(objetivos, [self.estacion_sg])

    def test_resolver_farmacias_filtra_por_fecha_apertura(self):
        actividad = self._crear_actividad(
            nombre='Checklist de Apertura', tipo_objetivo=TipoObjetivoCumplimiento.FARMACIAS,
            fecha_limite=date(2026, 10, 7), farmacias_aperturadas_desde=date(2026, 7, 1),
        )
        actividad.unidades_negocio.add(self.sg, self.mia)

        objetivos = set(resolver_objetivos(actividad))
        # farmacia_sg_vieja se abrió en enero -> queda fuera; farmacia_mia no tiene fecha -> también fuera.
        self.assertEqual(objetivos, {self.farmacia_sg})

    def test_resolver_colaboradores_filtra_por_cargo(self):
        actividad = self._crear_actividad(
            nombre='2FA', tipo_objetivo=TipoObjetivoCumplimiento.COLABORADORES, fecha_limite=date(2026, 10, 7),
        )
        actividad.unidades_negocio.add(self.sg)
        actividad.cargos.add(self.cargo_lider)

        objetivos = list(resolver_objetivos(actividad))
        self.assertEqual(objetivos, [self.colaborador_lider])

    def test_generar_resultados_es_idempotente(self):
        actividad = self._crear_actividad(
            nombre='AD', tipo_objetivo=TipoObjetivoCumplimiento.ESTACIONES, fecha_limite=date(2026, 10, 30),
        )
        actividad.unidades_negocio.add(self.sg)

        generar_resultados(actividad, self.usuario)
        generar_resultados(actividad, self.usuario)

        self.assertEqual(ResultadoCumplimientoEstacion.objects.filter(actividad=actividad).count(), 1)
        self.assertTrue(EventoAuditoria.objects.filter(accion='cumplimiento.generar_resultados').exists())

    def test_marcar_completado_actualiza_estado_y_audita(self):
        actividad = self._crear_actividad(
            nombre='AD', tipo_objetivo=TipoObjetivoCumplimiento.ESTACIONES, fecha_limite=date(2026, 10, 30),
        )
        actividad.unidades_negocio.add(self.sg)
        generar_resultados(actividad, self.usuario)
        resultado = ResultadoCumplimientoEstacion.objects.get(actividad=actividad, estacion=self.estacion_sg)

        marcar_completado(resultado, self.usuario, observacion='Instalado correctamente')

        resultado.refresh_from_db()
        self.assertEqual(resultado.estado, EstadoCumplimiento.COMPLETADO)
        self.assertIsNotNone(resultado.fecha_completado)
        self.assertEqual(resultado.completado_por, self.usuario)
        self.assertTrue(EventoAuditoria.objects.filter(accion='cumplimiento.marcar_completado').exists())

    def test_calcular_avance(self):
        actividad = self._crear_actividad(
            nombre='2FA', tipo_objetivo=TipoObjetivoCumplimiento.COLABORADORES, fecha_limite=date(2026, 10, 7),
        )
        actividad.unidades_negocio.add(self.sg)
        actividad.cargos.add(self.cargo_lider, self.cargo_vendedor)
        generar_resultados(actividad, self.usuario)

        self.assertEqual(calcular_avance(actividad), 0)

        resultado = ResultadoCumplimientoColaborador.objects.get(actividad=actividad, colaborador=self.colaborador_lider)
        marcar_completado(resultado, self.usuario)

        self.assertEqual(calcular_avance(actividad), 50)

    def test_calcular_avance_sin_objetivos_es_none(self):
        actividad = self._crear_actividad(
            nombre='Sin objetivos', tipo_objetivo=TipoObjetivoCumplimiento.FARMACIAS, fecha_limite=date(2026, 10, 7),
        )
        self.assertIsNone(calcular_avance(actividad))


class FechaLocalVsUtcTests(TestCase):
    """Todo lo que un humano lee como "hoy" tiene que ser hoy EN ECUADOR.

    `timezone.now().date()` devuelve la fecha en UTC. Con TIME_ZONE='America/Guayaquil'
    (UTC-5), entre las 19:00 y medianoche hora local en UTC ya es el dia siguiente: cinco
    de cada veinticuatro horas, todo calculo de "hoy" daba un dia de mas.

    Ya se habia corregido en mantenimiento (commit 9f9ba9a, 23-ago-2026) y quedo sin
    corregir en otros siete lugares. Estos tests fijan el comportamiento en la franja
    exacta donde se rompe — de dia pasan igual con el bug presente, por eso nadie lo
    noto.
    """

    # 18-sep-2026 02:30 UTC = 17-sep-2026 21:30 en Guayaquil. Para una persona en la
    # farmacia es todavia el 17.
    NOCHE_UTC = datetime.datetime(2026, 9, 18, 2, 30, tzinfo=datetime.timezone.utc)

    def test_la_franja_elegida_es_la_que_rompe(self):
        """Si esto falla, el resto de la clase no prueba lo que dice probar."""
        from django.utils import timezone as tz

        self.assertEqual(self.NOCHE_UTC.date(), datetime.date(2026, 9, 18), 'en UTC ya es 18')
        self.assertEqual(tz.localtime(self.NOCHE_UTC).date(), datetime.date(2026, 9, 17),
                         'en Ecuador todavia es 17')

    def test_una_actividad_de_cumplimiento_no_vence_antes_de_tiempo(self):
        """Con fecha limite HOY (17) y siendo las 21:30 del 17, no puede estar vencida."""
        from unittest.mock import patch

        from apps.cumplimiento.models import ActividadCumplimiento

        actividad = ActividadCumplimiento(
            nombre='Revision', fecha_limite=datetime.date(2026, 9, 17),
        )
        with patch('django.utils.timezone.now', return_value=self.NOCHE_UTC):
            self.assertFalse(
                actividad.vencida,
                'a las 21:30 del dia limite todavia queda el dia: en UTC ya seria manana',
            )

    def test_una_actividad_si_vence_al_dia_siguiente(self):
        """El contraste: el arreglo no puede volverla eterna."""
        from unittest.mock import patch

        from apps.cumplimiento.models import ActividadCumplimiento

        actividad = ActividadCumplimiento(
            nombre='Revision', fecha_limite=datetime.date(2026, 9, 16),
        )
        with patch('django.utils.timezone.now', return_value=self.NOCHE_UTC):
            self.assertTrue(actividad.vencida)

    def test_un_script_programado_agenda_desde_la_fecha_local(self):
        """Con frecuencia 7 y corriendo la noche del 17, la proxima es el 24 — no el 25."""
        from unittest.mock import patch

        from apps.catalogo.models import Grupo, UnidadNegocio
        from apps.scripts.models import Script, ScriptProgramado
        from apps.scripts.services import generar_ejecucion_programada

        sg = UnidadNegocio.objects.get(codigo='SG')
        usuario = User.objects.create_user(username='u_huso', password='x')
        Grupo.objects.create(codigo='TRX996')
        script = Script.objects.create(
            nombre='Limpieza', tipo='powershell', contenido='echo hola',
            unidad_negocio=sg, creado_por=usuario,
        )
        programado = ScriptProgramado.objects.create(
            script=script, destino_tipo='cadena', unidad_negocio=sg, frecuencia_dias=7,
            fecha_proxima_ejecucion=datetime.date(2026, 9, 17), creado_por=usuario,
        )

        with patch('django.utils.timezone.now', return_value=self.NOCHE_UTC):
            generar_ejecucion_programada(programado=programado)

        programado.refresh_from_db()
        self.assertEqual(programado.fecha_ultima_ejecucion, datetime.date(2026, 9, 17),
                         'corrio la noche del 17, no el 18')
        self.assertEqual(programado.fecha_proxima_ejecucion, datetime.date(2026, 9, 24),
                         '17 + 7 = 24; con UTC habria quedado el 25')

    def test_los_avisos_de_garantia_usan_el_limite_local(self):
        from unittest.mock import patch

        from apps.activos.models import Activo, Marca
        from apps.activos.services import activos_por_vencer_garantia
        from apps.catalogo.models import UnidadNegocio

        sg = UnidadNegocio.objects.get(codigo='SG')
        marca = Marca.objects.create(nombre='MarcaPrueba')
        # Vence justo en el borde: 17 + 30 dias = 17 de octubre.
        Activo.objects.create(
            codigo='CR-TST-0001', tipo=Activo.Tipo.DESKTOP, marca=marca, unidad_negocio=sg,
            vencimiento_garantia=datetime.date(2026, 10, 17),
        )
        # Y uno un dia despues del limite local, que NO debe entrar.
        Activo.objects.create(
            codigo='CR-TST-0002', tipo=Activo.Tipo.DESKTOP, marca=marca, unidad_negocio=sg,
            vencimiento_garantia=datetime.date(2026, 10, 18),
        )

        with patch('django.utils.timezone.now', return_value=self.NOCHE_UTC):
            codigos = set(activos_por_vencer_garantia(dias=30).values_list('codigo', flat=True))

        self.assertIn('CR-TST-0001', codigos)
        self.assertNotIn('CR-TST-0002', codigos,
                         'con UTC el limite se corria un dia y este entraba de mas')
