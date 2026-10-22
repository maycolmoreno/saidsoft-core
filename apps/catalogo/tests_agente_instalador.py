"""El agente no se traba con un instalador que deja procesos vivos, y el servicio se
reinicia si el agente se cuelga (0.37).

Caso real: LibreOffice en ML008-A y ML008-B (19-oct-2026). El programa quedó instalado,
pero el agente esperaba para siempre la tubería de salida que un proceso hijo del
instalador había heredado; el servicio seguía en RUNNING y Windows nunca lo reinició.

Carga el `agente_prueba.py` real por ruta, como `tests_agente_eventos.py`. Las pruebas de
`ejecutar_sin_tuberias` corren procesos de verdad (el intérprete de Python, nada del
sistema), porque lo que se prueba es justamente cómo se espera a un proceso y a sus
hijos; funcionan en Windows y en Linux. `servicio_windows.py` necesita pywin32, así que
de él se revisa el código fuente.
"""
import importlib.util
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from django.conf import settings
from django.test import SimpleTestCase

CARPETA_AGENTE = Path(settings.BASE_DIR) / 'agente-prueba'


def _python(codigo: str) -> str:
    """Comando de shell que corre `codigo` con este mismo intérprete."""
    return f'"{sys.executable}" -c "{codigo}"'


def _con_nieto(segundos: int, despues: str = "print('instalador termino')") -> str:
    """Un "instalador" que lanza un hijo vivo `segundos` (hereda la salida) y termina."""
    return _python(
        "import subprocess,sys; "
        f"subprocess.Popen([sys.executable,'-c','import time; time.sleep({segundos})']); {despues}"
    )


class _AgenteReal(SimpleTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        spec = importlib.util.spec_from_file_location('agente_instalador_bajo_prueba', CARPETA_AGENTE / 'agente_prueba.py')
        cls.ag = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.ag)


class EjecutarSinTuberiasTests(_AgenteReal):
    def test_un_hijo_vivo_del_instalador_no_traba_al_agente(self):
        """El caso de ML008: el instalador termina y deja un hijo con la salida heredada.
        Con la tubería de antes, el agente esperaba a ese hijo; ahora no."""
        inicio = time.monotonic()
        codigo, salida = self.ag.ejecutar_sin_tuberias(_con_nieto(15), timeout=60)
        self.assertLess(time.monotonic() - inicio, 8, 'el agente esperó al proceso hijo del instalador')
        self.assertEqual(codigo, 0)
        self.assertIn('instalador termino', salida)

    def test_el_patron_anterior_si_se_trababa(self):
        """La causa, comprobada: `run(capture_output)` espera al hijo aunque el instalador
        ya haya terminado. Si algún día deja de pasar, esta prueba avisa que la
        explicación del módulo quedó vieja."""
        inicio = time.monotonic()
        subprocess.run(_con_nieto(4), shell=True, capture_output=True, timeout=60)
        self.assertGreaterEqual(time.monotonic() - inicio, 3.5)

    def test_al_vencer_el_tiempo_se_corta_aunque_haya_hijos_vivos(self):
        inicio = time.monotonic()
        codigo, _ = self.ag.ejecutar_sin_tuberias(_con_nieto(30, despues='import time; time.sleep(30)'), timeout=2)
        self.assertIsNone(codigo)
        self.assertLess(time.monotonic() - inicio, 20, 'el tiempo límite no cortó la espera')

    def test_devuelve_el_codigo_y_la_salida(self):
        codigo, salida = self.ag.ejecutar_sin_tuberias(_python("import sys; print('falta un archivo'); sys.exit(1603)"), 60)
        self.assertEqual(codigo, 1603)
        self.assertIn('falta un archivo', salida)


class CorrerComandoSoftwareTests(_AgenteReal):
    def _correr(self, resultado):
        agente = self.ag.AgentePrueba.__new__(self.ag.AgentePrueba)
        reportes = []
        agente._reportar_instalacion = lambda solicitud, paso, **kw: reportes.append((paso, kw))
        with patch.object(self.ag, 'ejecutar_sin_tuberias', return_value=resultado) as ejecutar:
            agente._correr_comando_software(14, 'msiexec /i "x.msi" /qn', {'version': '26.8.1', 'argumentos_adicionales': ''})
        return reportes, ejecutar

    def test_usa_la_ejecucion_sin_tuberias_con_el_tiempo_limite(self):
        _, ejecutar = self._correr((0, ''))
        ejecutar.assert_called_once_with('msiexec /i "x.msi" /qn', self.ag.TIMEOUT_INSTALADOR_SEGUNDOS)

    def test_exito_y_exito_con_reinicio_pendiente(self):
        for codigo in (0, 3010, 1641):
            with self.subTest(codigo=codigo):
                reportes, _ = self._correr((codigo, ''))
                self.assertEqual(reportes, [('instalado', {'version_instalada': '26.8.1'})])

    def test_error_con_la_salida_del_instalador(self):
        reportes, _ = self._correr((1603, 'Error 1603: falta un archivo'))
        self.assertEqual(reportes[0][0], 'error')
        self.assertIn('1603', reportes[0][1]['detalle'])
        self.assertIn('falta un archivo', reportes[0][1]['detalle'])

    def test_tiempo_vencido_se_reporta_como_error(self):
        reportes, _ = self._correr((None, 'quedó esperando'))
        self.assertEqual(reportes[0][0], 'error')
        self.assertIn('Timeout', reportes[0][1]['detalle'])
        self.assertIn('procesos hijos', reportes[0][1]['detalle'])


class VigilanciaTests(_AgenteReal):
    def _agente(self, ultimo=None, intervalo=60):
        agente = self.ag.AgentePrueba.__new__(self.ag.AgentePrueba)
        agente.args = SimpleNamespace(intervalo_heartbeat=intervalo)
        if ultimo is not None:
            agente.ultimo_ciclo_latido = ultimo
        return agente

    def _hilo(self, vivo=True):
        return SimpleNamespace(is_alive=lambda: vivo)

    def test_sano(self):
        self.assertEqual(self.ag.motivo_para_reiniciar(self._agente(ultimo=1000.0), self._hilo(), ahora=1100.0), '')

    def test_antes_del_primer_ciclo_no_se_reinicia(self):
        self.assertEqual(self.ag.motivo_para_reiniciar(self._agente(), self._hilo(), ahora=99999.0), '')

    def test_ciclo_trabado_mas_alla_de_la_tolerancia(self):
        motivo = self.ag.motivo_para_reiniciar(self._agente(ultimo=1000.0), self._hilo(), ahora=1000.0 + 601)
        self.assertIn('no avanza', motivo)

    def test_la_tolerancia_crece_con_un_intervalo_largo(self):
        agente = self._agente(ultimo=1000.0, intervalo=300)  # tolerancia 5 x 300 = 1500 s
        self.assertEqual(self.ag.motivo_para_reiniciar(agente, self._hilo(), ahora=1000.0 + 1400), '')
        self.assertTrue(self.ag.motivo_para_reiniciar(agente, self._hilo(), ahora=1000.0 + 1600))

    def test_hilo_principal_terminado(self):
        motivo = self.ag.motivo_para_reiniciar(self._agente(ultimo=1000.0), self._hilo(vivo=False), ahora=1001.0)
        self.assertIn('terminó', motivo)

    def test_el_bucle_de_latidos_marca_cada_vuelta_aunque_no_este_enrolado(self):
        agente = self._agente(intervalo=0)
        agente._token = lambda: None  # sin enrolar: la vuelta no publica nada
        hilo = threading.Thread(target=agente.bucle_heartbeat, daemon=True)
        with patch.object(self.ag.time, 'sleep', side_effect=[None, None, SystemExit]):
            hilo.start()
            hilo.join(5)
        self.assertIsNotNone(getattr(agente, 'ultimo_ciclo_latido', None))

    def test_recuperacion_del_servicio(self):
        with patch.object(self.ag.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0)) as run:
            self.assertTrue(self.ag.configurar_recuperacion_servicio())
        self.assertEqual(run.call_args.args[0], [
            'sc.exe', 'failure', 'SaidsoftAgente', 'reset=', '86400',
            'actions=', 'restart/30000/restart/60000/restart/120000',
        ])
        with patch.object(self.ag.subprocess, 'run', side_effect=OSError('sin sc.exe')):
            self.assertFalse(self.ag.configurar_recuperacion_servicio())

    def test_mismos_tiempos_que_el_instalador(self):
        instalador = (CARPETA_AGENTE / 'instalar-servicio.ps1').read_text(encoding='utf-8')
        self.assertIn('actions= restart/30000/restart/60000/restart/120000', instalador)


class ServicioWindowsTests(SimpleTestCase):
    """`servicio_windows.py` importa pywin32: se revisa el código fuente."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.fuente = (CARPETA_AGENTE / 'servicio_windows.py').read_text(encoding='utf-8')

    def test_ya_no_espera_para_siempre(self):
        self.assertNotIn('win32event.INFINITE', self.fuente)
        self.assertIn('INTERVALO_VIGILANCIA_MS', self.fuente)

    def test_vigila_y_termina_con_error_para_que_windows_lo_reinicie(self):
        self.assertIn('motivo_para_reiniciar(self.agente, hilo)', self.fuente)
        self.assertIn('os._exit(1)', self.fuente)
        self.assertIn('configurar_recuperacion_servicio()', self.fuente)

    def test_una_parada_pedida_no_se_toma_por_falla(self):
        self.assertLess(self.fuente.index('self.deteniendo = True'), self.fuente.index('self.agente.detener()'))


class EjecutarCapturandoTests(_AgenteReal):
    """Scripts sin tuberías (0.38, H-14): el mismo bloqueo de ML008, en la ejecución de
    scripts, que además necesita stdout y stderr por separado."""

    def _comando(self, codigo):
        return [sys.executable, '-c', codigo]

    def test_un_hijo_vivo_del_script_no_traba_al_agente(self):
        inicio = time.monotonic()
        codigo, stdout, _ = self.ag.ejecutar_capturando(self._comando(
            "import subprocess,sys; subprocess.Popen([sys.executable,'-c','import time; time.sleep(15)']); "
            "print('script termino')"), timeout=60)
        self.assertLess(time.monotonic() - inicio, 8)
        self.assertEqual(codigo, 0)
        self.assertIn('script termino', stdout)

    def test_separa_salida_y_errores_y_devuelve_el_codigo(self):
        codigo, stdout, stderr = self.ag.ejecutar_capturando(self._comando(
            "import sys; print('a la salida'); print('al error', file=sys.stderr); sys.exit(3)"), timeout=60)
        self.assertEqual(codigo, 3)
        self.assertIn('a la salida', stdout)
        self.assertNotIn('al error', stdout)
        self.assertIn('al error', stderr)

    def test_respeta_el_tope_de_caracteres(self):
        _, stdout, _ = self.ag.ejecutar_capturando(self._comando("print('x' * 10000 + 'FIN')"), timeout=60)
        self.assertLessEqual(len(stdout), 4000)
        self.assertIn('FIN', stdout)

    def test_al_vencer_el_tiempo_devuelve_none(self):
        inicio = time.monotonic()
        codigo, _, _ = self.ag.ejecutar_capturando(self._comando('import time; time.sleep(30)'), timeout=2)
        self.assertIsNone(codigo)
        self.assertLess(time.monotonic() - inicio, 20)

    def test_el_script_reporta_completado_error_y_timeout(self):
        agente = self.ag.AgentePrueba.__new__(self.ag.AgentePrueba)
        agente._firma_valida = lambda *a, **k: True
        reportes = []
        agente._reportar_script = lambda rid, estado, **kw: reportes.append((estado, kw))
        payload = {'ejecucion_id': 1, 'resultado_id': 7, 'tipo_script': 'powershell', 'timeout_segundos': 60,
                   'contenido': 'Get-Date', 'estacion': 'X', 'timestamp': 0}
        for resultado, esperado in (((0, 'ok', ''), 'completado'), ((1, '', 'fallo'), 'error'),
                                    ((None, 'parcial', ''), 'timeout')):
            with self.subTest(esperado=esperado), patch.object(self.ag, 'ejecutar_capturando', return_value=resultado):
                reportes.clear()
                agente._verificar_y_ejecutar_script(payload)
                self.assertEqual([r[0] for r in reportes], ['ejecutando', esperado])


class ReportesQoS1Tests(_AgenteReal):
    """0.38, E-01: los reportes de operaciones van con QoS 1 y, sin conexión, quedan en la
    cola de paho en vez de perderse (comprobado contra EMQX 5.8.3 el 22-oct-2026)."""

    def _agente(self, rc):
        agente = self.ag.AgentePrueba.__new__(self.ag.AgentePrueba)
        agente.args = SimpleNamespace(codigo='LAB-QOS')
        agente._token = lambda: 'tok'
        publicados = []

        def publicar(topico, cuerpo, qos=0):
            publicados.append((topico, qos))
            return SimpleNamespace(rc=rc)

        agente.client = SimpleNamespace(publish=publicar)
        return agente, publicados

    def test_qos1_sin_conexion_queda_en_cola(self):
        agente, _ = self._agente(self.ag.mqtt.MQTT_ERR_NO_CONN)
        self.assertTrue(agente._publicar('t', {}, qos=1))

    def test_qos0_sin_conexion_sigue_contando_como_perdido(self):
        agente, _ = self._agente(self.ag.mqtt.MQTT_ERR_NO_CONN)
        self.assertFalse(agente._publicar('t', {}))

    def test_los_tres_reportes_de_operaciones_van_con_qos1(self):
        agente, publicados = self._agente(self.ag.mqtt.MQTT_ERR_SUCCESS)
        agente._reportar_instalacion(14, 'recibido')
        agente._reportar_despliegue(9, 'ok')
        agente._reportar_script(7, 'completado', exit_code=0)
        self.assertEqual([qos for _, qos in publicados], [1, 1, 1])
        self.assertEqual([t.rsplit('/', 2)[-2] for t, _ in publicados],
                         ['software_estado', 'despliegue_estado', 'script_estado'])


class VersionTests(_AgenteReal):
    def test_version(self):
        self.assertEqual(self.ag.VERSION_AGENTE_PRUEBA, 'agente-prueba-0.38')
