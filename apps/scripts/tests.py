from datetime import timedelta

from django.contrib.auth.models import User
from django.core.management import CommandError, call_command
from django.test import TestCase
from django.utils import timezone

from apps.catalogo.models import Estacion, Farmacia, Grupo, UnidadNegocio

from .models import EjecucionScript, Script, ScriptProgramado, TipoScript
from .services import generar_ejecucion_programada


class GenerarEjecucionProgramadaTests(TestCase):
    def setUp(self):
        self.sg = UnidadNegocio.objects.get(codigo='SG')
        grupo = Grupo.objects.create(codigo='TRX001')
        farmacia = Farmacia.objects.create(codigo='ML001', grupo=grupo, unidad_negocio=self.sg)
        self.estacion = Estacion.objects.create(
            codigo='ML001-A', farmacia=farmacia, estado_aprobacion=Estacion.EstadoAprobacion.APROBADA,
        )
        self.usuario = User.objects.create_user(username='u', password='x')
        self.script = Script.objects.create(
            nombre='Actualizar winget', tipo=TipoScript.POWERSHELL, contenido='winget upgrade --all',
            creado_por=self.usuario,
        )
        self.programado = ScriptProgramado.objects.create(
            script=self.script, unidad_negocio=self.sg, destino_tipo=EjecucionScript.DestinoTipo.CADENA,
            frecuencia_dias=7, fecha_proxima_ejecucion=timezone.now().date(), creado_por=self.usuario,
        )

    def test_genera_ejecucion_y_avanza_fechas(self):
        hoy = timezone.now().date()
        ejecucion = generar_ejecucion_programada(programado=self.programado)

        self.assertEqual(ejecucion.script, self.script)
        self.assertEqual(ejecucion.programado, self.programado)
        self.assertEqual(ejecucion.resultados.count(), 1)  # la única estación aprobada de la cadena

        self.programado.refresh_from_db()
        self.assertEqual(self.programado.fecha_ultima_ejecucion, hoy)
        self.assertEqual(self.programado.fecha_proxima_ejecucion, hoy + timedelta(days=7))

    def test_comando_solo_recoge_las_vencidas(self):
        futuro = ScriptProgramado.objects.create(
            script=self.script, unidad_negocio=self.sg, destino_tipo=EjecucionScript.DestinoTipo.CADENA,
            frecuencia_dias=7, fecha_proxima_ejecucion=timezone.now().date() + timedelta(days=5),
            creado_por=self.usuario,
        )
        call_command('generar_ejecuciones_programadas')

        self.assertEqual(EjecucionScript.objects.filter(programado=self.programado).count(), 1)
        self.assertEqual(EjecucionScript.objects.filter(programado=futuro).count(), 0)

    def test_comando_no_recoge_inactivas(self):
        self.programado.activo = False
        self.programado.save(update_fields=['activo'])
        call_command('generar_ejecuciones_programadas')
        self.assertFalse(EjecucionScript.objects.filter(programado=self.programado).exists())


class GenerarEjecucionesProgramadasTaskTests(TestCase):
    """CELERY_TASK_ALWAYS_EAGER=True hace que .delay() corra sincrónico en el test."""

    def test_delay_genera_la_ejecucion_vencida(self):
        from apps.scripts.tasks import generar_ejecuciones_programadas_task

        sg = UnidadNegocio.objects.get(codigo='SG')
        grupo = Grupo.objects.create(codigo='TRX001')
        farmacia = Farmacia.objects.create(codigo='ML001', grupo=grupo, unidad_negocio=sg)
        Estacion.objects.create(codigo='ML001-A', farmacia=farmacia, estado_aprobacion=Estacion.EstadoAprobacion.APROBADA)
        usuario = User.objects.create_user(username='u_task', password='x')
        script = Script.objects.create(
            nombre='Winget upgrade', tipo=TipoScript.POWERSHELL, contenido='winget upgrade --all',
            creado_por=usuario,
        )
        programado = ScriptProgramado.objects.create(
            script=script, unidad_negocio=sg, destino_tipo=EjecucionScript.DestinoTipo.CADENA,
            frecuencia_dias=7, fecha_proxima_ejecucion=timezone.now().date(), creado_por=usuario,
        )

        resultado = generar_ejecuciones_programadas_task.delay()

        self.assertTrue(EjecucionScript.objects.filter(programado=programado).exists())
        self.assertIn('1 ejecución', resultado.get())


class CambiarNodoPosTests(TestCase):
    def setUp(self):
        self.sg = UnidadNegocio.objects.get(codigo='SG')
        self.grupo = Grupo.objects.create(codigo='TRX002')
        farmacia = Farmacia.objects.create(codigo='ML002', grupo=self.grupo, unidad_negocio=self.sg)
        self.estacion = Estacion.objects.create(
            codigo='ML002-A', farmacia=farmacia, estado_aprobacion=Estacion.EstadoAprobacion.APROBADA,
        )
        self.usuario = User.objects.create_user(username='operador', password='x')

    def _correr(self, **overrides):
        opciones = {
            'unidad_negocio': 'SG', 'grupo': 'TRX002', 'nodo': 'trx002', 'usuario': 'operador',
        }
        opciones.update(overrides)
        call_command('cambiar_nodo_pos', **opciones)

    def test_nodo_invalido_rechaza_sin_tocar_la_base(self):
        with self.assertRaises(CommandError):
            self._correr(nodo='trx002"; Remove-Item C:\\ -Recurse')
        self.assertFalse(Script.objects.exists())

    def test_unidad_negocio_inexistente(self):
        with self.assertRaises(CommandError):
            self._correr(unidad_negocio='NOEXISTE')

    def test_grupo_inexistente(self):
        with self.assertRaises(CommandError):
            self._correr(grupo='NOEXISTE')

    def test_usuario_inexistente(self):
        with self.assertRaises(CommandError):
            self._correr(usuario='noexiste')

    def test_arma_y_envia_en_una_sola_pasada(self):
        self._correr()

        script = Script.objects.get(es_adhoc=True)
        self.assertIn('trx002', script.contenido)
        self.assertIn("SelectSingleNode(\"//add[@key='Bdd']\")", script.contenido)
        self.assertEqual(script.unidad_negocio, self.sg)

        ejecucion = EjecucionScript.objects.get(script=script)
        self.assertEqual(ejecucion.destino_tipo, EjecucionScript.DestinoTipo.GRUPOS)
        self.assertEqual(list(ejecucion.grupos.all()), [self.grupo])
        self.assertEqual(ejecucion.resultados.count(), 1)
        self.assertEqual(ejecucion.resultados.first().estacion, self.estacion)

    def test_no_llega_a_estacion_de_otro_grupo(self):
        otro_grupo = Grupo.objects.create(codigo='TRX003')
        otra_farmacia = Farmacia.objects.create(codigo='ML003', grupo=otro_grupo, unidad_negocio=self.sg)
        Estacion.objects.create(
            codigo='ML003-A', farmacia=otra_farmacia, estado_aprobacion=Estacion.EstadoAprobacion.APROBADA,
        )
        self._correr()
        ejecucion = EjecucionScript.objects.get()
        self.assertEqual(ejecucion.resultados.count(), 1)
        self.assertEqual(ejecucion.resultados.first().estacion.codigo, 'ML002-A')
