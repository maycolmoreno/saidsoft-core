from datetime import timedelta

from django.contrib.auth.models import User
from django.core.management import call_command
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
