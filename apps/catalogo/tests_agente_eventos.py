"""Regresión de la consulta al visor de eventos del agente (P0-1, fase D2).

Hasta 0.36 `_leer_eventos_sistema` armaba el script de PowerShell insertando valores del
catálogo dentro del código. Desde D2 el script es CONSTANTE (`SCRIPT_CONSULTA_EVENTOS`,
entregado con `-EncodedCommand`) y los filtros viajan como JSON por la entrada estándar.
Estas pruebas fijan ese contrato: si alguien vuelve a meter datos en el texto del script,
fallan.

Vive en un archivo propio, como `tests_equivalencia.py`, porque prueba un contrato de
seguridad del agente y no lógica de `catalogo`. Carga el `agente_prueba.py` real por ruta
(mismo motivo que `CargaAgenteRealMixin` en `tests.py`: el guion del nombre impide
importarlo) y **nunca** inicia PowerShell: `subprocess.run` está simulado en todas.
`SimpleTestCase`: no tocan la base.

P0-1 sigue abierto: esto no cubre la verificación obligatoria de firmas ni la validación
de los catálogos persistidos.
"""
import base64
import importlib.util
import json
import subprocess
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from django.conf import settings
from django.test import SimpleTestCase

INICIO = datetime(2026, 10, 1, 8, 30, 0)
EVENTO_VALIDO = {'log': 'System', 'proveedor': 'Ntfs', 'id': 55}


class _AgenteReal(SimpleTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        ruta = Path(settings.BASE_DIR) / 'agente-prueba' / 'agente_prueba.py'
        spec = importlib.util.spec_from_file_location('agente_eventos_bajo_prueba', ruta)
        cls.ag = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.ag)

    def _agente(self, catalogo, marca=None):
        agente = self.ag.AgentePrueba.__new__(self.ag.AgentePrueba)
        agente.identidad = {'catalogo_eventos_sistema': catalogo}
        if marca:
            agente.identidad['eventos_sistema_marca'] = marca
        agente.args = SimpleNamespace(codigo='LAB-D3')
        return agente

    def _proceso(self, stdout='[]', returncode=0, stderr=''):
        return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)

    def _leer(self, catalogo, **proceso):
        """Corre `_leer_eventos_sistema` con `subprocess.run` simulado. (resultado, mock)."""
        efecto = proceso.pop('side_effect', None)
        with patch.object(self.ag.subprocess, 'run', return_value=self._proceso(**proceso),
                          side_effect=efecto) as run:
            resultado = self._agente(catalogo, marca='2026-10-01T08:30:00')._leer_eventos_sistema()
        return resultado, run


class ScriptConstanteTests(_AgenteReal):
    """A y G: el código que se ejecuta es siempre el mismo, sin importar los datos."""

    def test_el_argumento_es_el_script_constante_codificado_en_utf16le(self):
        decodificado = base64.b64decode(self.ag.SCRIPT_CONSULTA_EVENTOS_CODIFICADO).decode('utf-16-le')
        self.assertEqual(decodificado, self.ag.SCRIPT_CONSULTA_EVENTOS)

    def test_la_linea_de_comandos_entra_holgada_en_el_limite_de_windows(self):
        # CreateProcess acepta hasta 32.767 caracteres en la línea de comandos.
        self.assertLess(len(self.ag.SCRIPT_CONSULTA_EVENTOS_CODIFICADO), 8000)

    def test_el_script_lee_los_filtros_de_stdin(self):
        self.assertIn('[Console]::In.ReadToEnd()', self.ag.SCRIPT_CONSULTA_EVENTOS)

    def test_filtros_distintos_producen_exactamente_la_misma_invocacion(self):
        catalogos = [
            [EVENTO_VALIDO],
            [{'log': 'Application', 'proveedor': 'Application Error', 'id': 1000}],
            [{'log': 'System', 'proveedor': 'Proveedor (x86)/Sub_1.0', 'id': 7}, EVENTO_VALIDO],
            # Con valores que la validación descarta, junto a uno válido para que haya consulta.
            [EVENTO_VALIDO, {'log': 'System', 'proveedor': "Prov'x", 'id': 1}],
            [EVENTO_VALIDO, {'log': 'System', 'proveedor': 'Prov$x', 'id': 2}],
        ]
        invocaciones = []
        for catalogo in catalogos:
            _, run = self._leer(catalogo)
            invocaciones.append(run.call_args.args[0])
        esperado = ['powershell', '-NoProfile', '-NonInteractive', '-EncodedCommand',
                    self.ag.SCRIPT_CONSULTA_EVENTOS_CODIFICADO]
        for argv in invocaciones:
            self.assertEqual(argv, esperado)

    def test_el_codigo_del_agente_no_usa_shell_true_en_la_consulta(self):
        import inspect

        fuente = inspect.getsource(self.ag.AgentePrueba._leer_eventos_sistema)
        self.assertNotIn('shell=True', fuente)
        self.assertNotIn('check_output', fuente)


class TransportePorStdinTests(_AgenteReal):
    """B: los filtros viajan por stdin, como JSON ASCII, y nunca en la línea de comandos."""

    def test_los_valores_aceptados_viajan_por_stdin_y_no_en_argv(self):
        proveedor = 'Proveedor (x86)/Sub_1.0'
        _, run = self._leer([{'log': 'System', 'proveedor': proveedor, 'id': 7}])
        argv, kwargs = run.call_args.args[0], run.call_args.kwargs
        datos = json.loads(kwargs['input'])
        self.assertEqual(datos['fuentes'], [{'log': 'System', 'proveedor': proveedor, 'ids': [7]}])
        self.assertEqual(datos['inicio'], '2026-10-01T08:30:00')
        texto_argv = ' '.join(argv) + self.ag.SCRIPT_CONSULTA_EVENTOS
        self.assertNotIn(proveedor, texto_argv)

    def test_el_json_de_stdin_es_ascii(self):
        _, run = self._leer([EVENTO_VALIDO])
        self.assertTrue(run.call_args.kwargs['input'].isascii())

    def test_opciones_de_la_invocacion(self):
        _, run = self._leer([EVENTO_VALIDO])
        kwargs = run.call_args.kwargs
        self.assertFalse(kwargs.get('shell', False))
        self.assertTrue(kwargs['capture_output'])
        self.assertTrue(kwargs['text'])
        self.assertEqual(kwargs['encoding'], 'utf-8')
        self.assertEqual(kwargs['timeout'], self.ag.TIMEOUT_CONSULTA_EVENTOS_SEGUNDOS)


class FiltrosPreviosTests(_AgenteReal):
    """C: qué deja pasar `_filtros_consulta_eventos` y qué descarta."""

    def _fuentes(self, catalogo):
        return self.ag.AgentePrueba._filtros_consulta_eventos(catalogo, INICIO)['fuentes']

    def test_entrada_valida_y_agrupacion_por_log_y_proveedor(self):
        fuentes = self._fuentes([
            EVENTO_VALIDO,
            {'log': 'System', 'proveedor': 'Ntfs', 'id': 98},
            {'log': 'Application', 'proveedor': 'Application Error', 'id': 1000},
        ])
        self.assertEqual(fuentes, [
            {'log': 'System', 'proveedor': 'Ntfs', 'ids': [55, 98]},
            {'log': 'Application', 'proveedor': 'Application Error', 'ids': [1000]},
        ])

    def test_politica_de_valores_por_defecto(self):
        """Lo que falta NO se rechaza: log ausente es System y proveedor ausente es sin filtro."""
        self.assertEqual(self._fuentes([{'id': 41}]), [{'log': 'System', 'proveedor': '', 'ids': [41]}])
        self.assertEqual(self._fuentes([{'log': '', 'proveedor': None, 'id': 41}]),
                         [{'log': 'System', 'proveedor': '', 'ids': [41]}])

    def test_inicio_en_formato_s_de_powershell(self):
        datos = self.ag.AgentePrueba._filtros_consulta_eventos([EVENTO_VALIDO], INICIO)
        self.assertEqual(datos['inicio'], '2026-10-01T08:30:00')

    def test_se_descartan_las_entradas_con_forma_invalida(self):
        invalidas = {
            'log fuera de la lista': {'log': 'Security', 'proveedor': 'Ntfs', 'id': 55},
            'log no texto': {'log': 5, 'proveedor': 'Ntfs', 'id': 55},
            'id texto': {'log': 'System', 'proveedor': 'Ntfs', 'id': '55'},
            'id booleano': {'log': 'System', 'proveedor': 'Ntfs', 'id': True},
            'id fraccionario': {'log': 'System', 'proveedor': 'Ntfs', 'id': 55.0},
            'id negativo': {'log': 'System', 'proveedor': 'Ntfs', 'id': -1},
            'id mayor a 65535': {'log': 'System', 'proveedor': 'Ntfs', 'id': 65536},
            'id ausente': {'log': 'System', 'proveedor': 'Ntfs'},
            'proveedor no texto': {'log': 'System', 'proveedor': 123, 'id': 55},
            'proveedor con espacio inicial': {'log': 'System', 'proveedor': ' Ntfs', 'id': 55},
            'proveedor con salto final': {'log': 'System', 'proveedor': 'Ntfs\n', 'id': 55},
            'proveedor no ASCII': {'log': 'System', 'proveedor': 'Ntfś', 'id': 55},
        }
        for caso, entrada in invalidas.items():
            with self.subTest(caso=caso):
                self.assertEqual(self._fuentes([entrada]), [])

    def test_se_descartan_proveedores_con_metacaracteres(self):
        for caracter in ["'", '"', ';', '$', '`', '|', '&', '{', '}', '@', '\t']:
            with self.subTest(caracter=repr(caracter)):
                self.assertEqual(self._fuentes([{'log': 'System', 'proveedor': f'Prov{caracter}x', 'id': 1}]), [])

    def test_estructuras_inesperadas(self):
        for caso, catalogo in {
            'catalogo diccionario': {'eventos': [EVENTO_VALIDO]},
            'catalogo texto': 'System',
            'catalogo nulo': None,
            'elementos que no son objetos': ['System', 5, None, [EVENTO_VALIDO]],
        }.items():
            with self.subTest(caso=caso):
                self.assertEqual(self._fuentes(catalogo), [])

    def test_lo_invalido_se_descarta_y_lo_valido_sigue(self):
        fuentes = self._fuentes([{'log': 'System', 'proveedor': 'Prov;x', 'id': 1}, EVENTO_VALIDO])
        self.assertEqual(fuentes, [{'log': 'System', 'proveedor': 'Ntfs', 'ids': [55]}])


class SinProcesoAnteEntradasRechazadasTests(_AgenteReal):
    """D: si no queda ninguna entrada válida, PowerShell no se inicia y la marca no avanza."""

    def test_catalogo_con_todo_invalido_no_inicia_el_proceso(self):
        resultado, run = self._leer([{'log': 'Security', 'proveedor': 'Prov;x', 'id': '1'}])
        run.assert_not_called()
        self.assertEqual(resultado, ([], None))

    def test_catalogo_vacio_no_inicia_el_proceso(self):
        resultado, run = self._leer([])
        run.assert_not_called()
        self.assertEqual(resultado, ([], None))

    def test_entrada_con_valor_por_defecto_si_inicia_el_proceso(self):
        """Contraste con lo anterior: la política de valores por defecto NO es un rechazo."""
        _, run = self._leer([{'id': 41}])
        run.assert_called_once()


class ManejoDeErroresTests(_AgenteReal):
    """E: un fallo nunca se devuelve como una lectura correcta, y la marca no avanza."""

    def test_timeout(self):
        resultado, _ = self._leer([EVENTO_VALIDO], side_effect=subprocess.TimeoutExpired('powershell', 60))
        self.assertEqual(resultado, ([], None))

    def test_codigo_de_salida_distinto_de_cero(self):
        resultado, _ = self._leer([EVENTO_VALIDO], returncode=2, stderr='entrada invalida', stdout='[]')
        self.assertEqual(resultado, ([], None))

    def test_fallo_al_iniciar_el_proceso(self):
        resultado, _ = self._leer([EVENTO_VALIDO], side_effect=FileNotFoundError('powershell'))
        self.assertEqual(resultado, ([], None))

    def test_salida_que_no_es_json(self):
        resultado, _ = self._leer([EVENTO_VALIDO], stdout='esto no es json')
        self.assertEqual(resultado, ([], None))

    def _reporte_no_publica_ni_avanza(self, salida):
        """Corre el ciclo completo (`_reportar_eventos_sistema`) y exige que no publique ni
        guarde la marca."""
        agente = self._agente([EVENTO_VALIDO], marca='2026-10-01T08:30:00')
        agente._publicar = lambda *a, **k: self.fail('no debe publicar')
        agente._guardar_identidad = lambda: self.fail('no debe guardar la marca')
        agente._token = lambda: 'token-lab'
        with patch.object(self.ag.subprocess, 'run', return_value=self._proceso(stdout=salida)):
            agente._reportar_eventos_sistema()
        self.assertEqual(agente.identidad['eventos_sistema_marca'], '2026-10-01T08:30:00')

    def test_json_que_no_es_una_lista_de_objetos_no_pasa_como_valido(self):
        for salida in ['{"log": "System"}', '[1, 2]', '"texto"', 'null', '42']:
            with self.subTest(salida=salida):
                resultado, _ = self._leer([EVENTO_VALIDO], stdout=salida)
                self.assertEqual(resultado, ([], None))
                self._reporte_no_publica_ni_avanza(salida)

    def test_salida_vacia_con_codigo_cero_es_un_error(self):
        """El script siempre imprime al menos `[]`: vacío con código 0 es un fallo."""
        for salida in ['', '   ', '\r\n', ' \n\t ']:
            with self.subTest(salida=repr(salida)):
                resultado, _ = self._leer([EVENTO_VALIDO], stdout=salida)
                self.assertEqual(resultado, ([], None))
                self._reporte_no_publica_ni_avanza(salida)

    def test_objetos_sin_las_claves_del_contrato_se_rechazan(self):
        resultado, _ = self._leer([EVENTO_VALIDO], stdout='[{"x": 1}]')
        self.assertEqual(resultado, ([], None))
        self._reporte_no_publica_ni_avanza('[{"x": 1}]')


class ContratoDeSalidaTests(_AgenteReal):
    """F: con una respuesta válida, el formato que recibe el resto del agente no cambia."""

    RESPUESTA = [{'log': 'System', 'id': 41, 'cantidad': 3,
                  'origen': 'Microsoft-Windows-Kernel-Power', 'mensaje': 'm' * 600}]

    def test_eventos_con_las_claves_y_tipos_esperados(self):
        (eventos, marca), _ = self._leer([EVENTO_VALIDO], stdout=json.dumps(self.RESPUESTA))
        self.assertEqual(len(eventos), 1)
        evento = eventos[0]
        self.assertEqual(set(evento), {'log', 'id', 'cantidad', 'origen', 'mensaje'})
        self.assertEqual((evento['log'], evento['id'], evento['cantidad']), ('System', 41, 3))
        self.assertEqual(len(evento['mensaje']), 500, 'el mensaje se recorta a 500')
        datetime.fromisoformat(marca)

    def test_acentos_de_la_salida_se_conservan(self):
        respuesta = [dict(self.RESPUESTA[0], mensaje='operación de paginación')]
        (eventos, _), _ = self._leer([EVENTO_VALIDO], stdout=json.dumps(respuesta, ensure_ascii=False))
        self.assertEqual(eventos[0]['mensaje'], 'operación de paginación')

    def test_sin_eventos_la_marca_avanza_y_no_se_publica(self):
        agente = self._agente([EVENTO_VALIDO], marca='2026-10-01T08:30:00')
        agente._publicar = lambda *a, **k: self.fail('sin eventos no se publica')
        guardados = []
        agente._guardar_identidad = lambda: guardados.append(True)
        with patch.object(self.ag.subprocess, 'run', return_value=self._proceso(stdout='[]')):
            agente._reportar_eventos_sistema()
        self.assertNotEqual(agente.identidad['eventos_sistema_marca'], '2026-10-01T08:30:00')
        self.assertEqual(guardados, [True])

    def test_la_marca_avanza_solo_si_la_publicacion_salio(self):
        for salio in (True, False):
            with self.subTest(publicacion=salio):
                agente = self._agente([EVENTO_VALIDO], marca='2026-10-01T08:30:00')
                agente._token = lambda: 'token-lab'
                agente._publicar = lambda *a, **k: salio
                agente._guardar_identidad = lambda: None
                with patch.object(self.ag.subprocess, 'run', return_value=self._proceso(stdout=json.dumps(self.RESPUESTA))):
                    agente._reportar_eventos_sistema()
                avanzo = agente.identidad['eventos_sistema_marca'] != '2026-10-01T08:30:00'
                self.assertEqual(avanzo, salio)


class ContratoDeResultadoTests(_AgenteReal):
    """D4: la salida del proceso se valida contra el contrato antes de aceptarse.

    Contrato (del script y de sus consumidores): lista de objetos con exactamente `log`
    (System/Application), `id` (int 0..65535), `cantidad` (int >= 1), `origen` (str o
    null) y `mensaje` (str o null). Un elemento fuera de contrato invalida TODA la lectura.
    """

    BASE = {'log': 'System', 'id': 41, 'cantidad': 2, 'origen': 'Microsoft-Windows-Kernel-Power', 'mensaje': 'm'}

    def _leer_respuesta(self, elementos):
        resultado, _ = self._leer([EVENTO_VALIDO], stdout=json.dumps(elementos))
        return resultado

    def test_lista_vacia_es_una_lectura_valida_sin_eventos(self):
        """Contraste con la salida vacía: `[]` SÍ es válido y la marca avanza."""
        eventos, marca = self._leer_respuesta([])
        self.assertEqual(eventos, [])
        self.assertIsNotNone(marca)

    def test_elemento_que_cumple_el_contrato_se_acepta(self):
        eventos, marca = self._leer_respuesta([self.BASE])
        self.assertEqual(eventos, [self.BASE])
        self.assertIsNotNone(marca)

    def test_origen_y_mensaje_nulos_se_conservan_como_hasta_ahora(self):
        eventos, marca = self._leer_respuesta([dict(self.BASE, origen=None, mensaje=None)])
        self.assertIsNotNone(marca)
        self.assertIsNone(eventos[0]['origen'])
        self.assertEqual(eventos[0]['mensaje'], '', 'mensaje nulo se normaliza a texto vacío')

    def test_cada_clave_obligatoria_ausente_invalida_la_lectura(self):
        for clave in self.BASE:
            with self.subTest(falta=clave):
                elemento = {k: v for k, v in self.BASE.items() if k != clave}
                self.assertEqual(self._leer_respuesta([elemento]), ([], None))

    def test_una_clave_sobrante_invalida_la_lectura(self):
        self.assertEqual(self._leer_respuesta([dict(self.BASE, extra=1)]), ([], None))

    def test_tipos_y_rangos_incompatibles(self):
        invalidos = {
            'log no permitido': {'log': 'Security'},
            'log no texto': {'log': 1},
            'id texto': {'id': '41'},
            'id booleano': {'id': True},
            'id fraccionario': {'id': 41.0},
            'id negativo': {'id': -1},
            'id mayor a 65535': {'id': 65536},
            'id nulo': {'id': None},
            'cantidad cero': {'cantidad': 0},
            'cantidad texto': {'cantidad': '2'},
            'cantidad booleana': {'cantidad': True},
            'cantidad fraccionaria': {'cantidad': 2.5},
            'origen numero': {'origen': 5},
            'origen lista': {'origen': ['x']},
            'mensaje numero': {'mensaje': 5},
            'mensaje objeto': {'mensaje': {'a': 1}},
        }
        for caso, cambio in invalidos.items():
            with self.subTest(caso=caso):
                self.assertEqual(self._leer_respuesta([dict(self.BASE, **cambio)]), ([], None))

    def test_un_elemento_invalido_invalida_toda_la_lectura(self):
        """No se publica la parte "buena": la ventana entera se vuelve a leer."""
        mezcla = [self.BASE, dict(self.BASE, id='x')]
        self.assertEqual(self._leer_respuesta(mezcla), ([], None))
        agente = self._agente([EVENTO_VALIDO], marca='2026-10-01T08:30:00')
        agente._publicar = lambda *a, **k: self.fail('no debe publicar resultados parciales')
        agente._guardar_identidad = lambda: self.fail('no debe guardar la marca')
        agente._token = lambda: 'token-lab'
        with patch.object(self.ag.subprocess, 'run', return_value=self._proceso(stdout=json.dumps(mezcla))):
            agente._reportar_eventos_sistema()
        self.assertEqual(agente.identidad['eventos_sistema_marca'], '2026-10-01T08:30:00')
