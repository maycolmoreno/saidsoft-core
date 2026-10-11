"""Salud de la plataforma: lo que se puede afirmar de cada componente, y lo que no.

Estas pruebas cubren la Fase D del plan de docs/auditoria/centro-monitoreo/: el hallazgo
D-05 decía que el latido del worker MQTT no prueba nada más que la existencia del proceso
—lo escribe un hilo aparte sin mirar la conexión al broker ni si llegó un solo mensaje—
así que un worker desconectado de EMQX se veía perfectamente sano en el panel.

Lo que se fija acá es la distinción entre los cuatro estados que un tablero de monitoreo
tiene que poder separar: proceso activo, comunicación funcional, procesamiento efectivo y
ausencia de datos. Y la regla que los gobierna: **nada sin mecanismo de comprobación se
pinta de verde.**

Archivo propio y no `tests.py` por tamaño: ese archivo tiene casi 10.000 líneas y esto es
un tema cerrado con su propio vocabulario.
"""
from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

from apps.monitoreo.salud import estado_latidos, resumen_salud
from apps.monitoreo.umbrales import RESPALDO_UMBRAL_HORAS, WORKER_MQTT_UMBRAL_SEGUNDOS
from apps.mqtt_worker.models import WorkerHeartbeat


class EstadoDeLaPlataformaTests(TestCase):
    def _latido(self, nombre, hace_segundos=1, conectado=None, mensajes=0):
        WorkerHeartbeat.objects.update_or_create(
            nombre=nombre,
            defaults={
                'ultimo_latido': timezone.now() - timedelta(seconds=hace_segundos),
                'conectado': conectado,
                'mensajes_procesados': mensajes,
            },
        )

    def _componente(self, clave):
        por_clave = {c.clave: c for c in estado_latidos()}
        self.assertIn(clave, por_clave)
        return por_clave[clave]

    # --- Los tres estados del worker --------------------------------------------------

    def test_proceso_muerto_es_critico(self):
        self._latido('mqtt_worker', hace_segundos=WORKER_MQTT_UMBRAL_SEGUNDOS + 60, conectado=True)
        componente = self._componente('mqtt_worker')
        self.assertEqual(componente.estado, 'critical')
        self.assertIn('último latido', componente.detalle)

    def test_proceso_vivo_y_desconectado_del_broker_es_critico(self):
        """El estado que antes se veía EN VERDE. Con EMQX caído, `loop_forever()`
        reintenta en silencio mientras el hilo de latido sigue escribiendo: el proceso
        está perfecto y no está llegando nada de la flota."""
        self._latido('mqtt_worker', conectado=False)
        componente = self._componente('mqtt_worker')
        self.assertEqual(componente.estado, 'critical')
        self.assertIn('NO está conectado', componente.detalle)

    def test_proceso_vivo_y_conectado_es_ok_y_dice_cuanto_proceso(self):
        self._latido('mqtt_worker', conectado=True, mensajes=4210)
        componente = self._componente('mqtt_worker')
        self.assertEqual(componente.estado, 'ok')
        self.assertIn('conectado', componente.detalle)
        self.assertIn('4210', componente.detalle)

    def test_un_worker_que_no_sabe_responder_queda_en_advertencia_no_en_verde(self):
        """`conectado=None` es un worker anterior a este campo. No se puede afirmar que
        esté sano ni que esté roto: se dice que no se verificó."""
        self._latido('mqtt_worker', conectado=None)
        componente = self._componente('mqtt_worker')
        self.assertEqual(componente.estado, 'warning')
        self.assertIn('sin verificar', componente.detalle)

    def test_nunca_latio_es_distinto_de_atrasado(self):
        """Puede ser que el contenedor nunca haya arrancado, no que se haya colgado."""
        componente = self._componente('mqtt_worker')
        self.assertEqual(componente.estado, 'critical')
        self.assertIn('nunca', componente.detalle)

    # --- El broker ---------------------------------------------------------------------

    def test_el_broker_sale_de_la_conexion_real_del_worker(self):
        """No de su API de administración: lo que importa no es si el panel de EMQX
        contesta, sino si el broker ACEPTA al worker — y eso ya viaja en cada latido."""
        self._latido('mqtt_worker', conectado=True)
        self.assertEqual(self._componente('broker_mqtt').estado, 'ok')

        self._latido('mqtt_worker', conectado=False)
        broker = self._componente('broker_mqtt')
        self.assertEqual(broker.estado, 'critical')
        self.assertIn('nada de la flota está llegando', broker.detalle)

    def test_sin_quien_lo_reporte_el_broker_queda_sin_verificar(self):
        """Gris y declarado, no omitido: que falte la comprobación es información útil."""
        broker = self._componente('broker_mqtt')
        self.assertEqual(broker.estado, 'sin_dato')
        self.assertFalse(broker.verificable)

    def test_lo_sin_verificar_no_cuenta_como_problema(self):
        """Una barra permanentemente en rojo informa tan poco como una permanentemente en
        verde. Lo no verificable se informa aparte, no se suma a los problemas."""
        self._latido('mqtt_worker', conectado=True)
        self._latido('meshcentral_worker', conectado=True)
        self._latido('celery_beat')
        self._latido('respaldo')

        resumen = resumen_salud(estado_latidos())
        self.assertEqual(resumen['estado'], 'ok')
        self.assertEqual(resumen['texto'], 'Plataforma verificada')

    # --- Celery Beat --------------------------------------------------------------------

    def test_beat_tiene_latido_propio_en_vez_de_inferirse(self):
        """Antes su salud se deducía de que `MuestraRedFarmacia` siguiera creciendo, y esa
        tabla la escriben DOS rutas: podía crecer con Beat a medias, y quedarse quieta con
        Beat perfecto si no había farmacias sondeables."""
        self._latido('celery_beat')
        self.assertEqual(self._componente('celery_beat').estado, 'ok')

        self._latido('celery_beat', hace_segundos=WORKER_MQTT_UMBRAL_SEGUNDOS + 60)
        self.assertEqual(self._componente('celery_beat').estado, 'critical')

    def test_la_tarea_de_latido_de_beat_escribe_la_fila(self):
        from apps.monitoreo.tasks import NOMBRE_BEAT, latido_beat_task

        latido_beat_task()
        self.assertTrue(WorkerHeartbeat.objects.filter(nombre=NOMBRE_BEAT).exists())

    def test_sin_beat_todavia_queda_sin_verificar(self):
        componente = self._componente('celery_beat')
        self.assertEqual(componente.estado, 'sin_dato')
        self.assertFalse(componente.verificable)

    # --- Respaldo ------------------------------------------------------------------------

    def test_el_respaldo_sin_registro_es_advertencia_y_no_silencio(self):
        """Una instalación nueva no tiene respaldo y eso no es una falla; tampoco es "al
        día". `verificar_salud` lo cuenta como OK a propósito, para no llenar de ruido el
        primer día de un despliegue — acá es una pantalla y el matiz cabe."""
        componente = self._componente('respaldo')
        self.assertEqual(componente.estado, 'warning')

    def test_un_respaldo_viejo_es_critico(self):
        self._latido('respaldo', hace_segundos=int((RESPALDO_UMBRAL_HORAS + 5) * 3600))
        self.assertEqual(self._componente('respaldo').estado, 'critical')

    # --- El agregado ----------------------------------------------------------------------

    def test_el_resumen_toma_el_peor_componente(self):
        """Con un worker caído, "4 de 5 bien" es cierto y es irrelevante."""
        self._latido('mqtt_worker', conectado=True)
        self._latido('meshcentral_worker', hace_segundos=WORKER_MQTT_UMBRAL_SEGUNDOS + 60)
        self._latido('celery_beat')
        self._latido('respaldo')

        resumen = resumen_salud(estado_latidos())
        self.assertEqual(resumen['estado'], 'critical')
        self.assertEqual(resumen['texto'], 'Plataforma degradada')
        self.assertEqual([c.clave for c in resumen['afectados']], ['meshcentral_worker'])

    def test_cuesta_una_sola_consulta(self):
        """Lo consume un context processor que corre en todas las páginas del panel."""
        self._latido('mqtt_worker', conectado=True)
        with self.assertNumQueries(1):
            estado_latidos()


class VerificarSaludLeeElMismoLugarQueElPanelTests(TestCase):
    """La consola y la pantalla no pueden discrepar sobre qué es "sano".

    Antes `verificar_salud` leía los umbrales del lugar correcto pero COMPARABA por su
    cuenta, así que había dos implementaciones de la misma regla. El síntoma de eso es
    silencioso: la consola dice "OK mqtt_worker", la pantalla lo pinta en rojo, y no hay
    forma de saber cuál de las dos miente.
    """

    def _correr(self, *args):
        import io

        from django.core.management import call_command

        salida = io.StringIO()
        try:
            call_command('verificar_salud', *args, stdout=salida, stderr=salida)
        except SystemExit as exc:
            return salida.getvalue(), exc.code
        return salida.getvalue(), 0

    def test_un_worker_desconectado_del_broker_tambien_falla_en_la_consola(self):
        """Es el caso nuevo: antes este worker salía OK por el código de salida y verde en
        la pantalla, con el canal MQTT roto."""
        WorkerHeartbeat.objects.create(
            nombre='mqtt_worker', ultimo_latido=timezone.now(), conectado=False,
        )
        WorkerHeartbeat.objects.create(
            nombre='meshcentral_worker', ultimo_latido=timezone.now(), conectado=True,
        )
        texto, codigo = self._correr()
        self.assertEqual(codigo, 1)
        self.assertIn('mqtt_worker', texto)
        self.assertIn('NO está conectado', texto)

    def test_el_contrato_del_codigo_de_salida_no_cambia(self):
        """Lo consume un temporizador de systemd y el healthcheck de cada contenedor."""
        WorkerHeartbeat.objects.create(
            nombre='mqtt_worker', ultimo_latido=timezone.now(), conectado=True,
        )
        WorkerHeartbeat.objects.create(
            nombre='meshcentral_worker', ultimo_latido=timezone.now(), conectado=True,
        )
        WorkerHeartbeat.objects.create(nombre='celery_beat', ultimo_latido=timezone.now())
        _, codigo = self._correr()
        self.assertEqual(codigo, 0)

    def test_sigue_pudiendo_medir_un_solo_componente(self):
        """Un contenedor tiene que medirse a SÍ MISMO: si el worker MQTT se marcara enfermo
        por un respaldo atrasado, Docker lo reiniciaría en loop sin arreglar nada."""
        WorkerHeartbeat.objects.create(
            nombre='mqtt_worker', ultimo_latido=timezone.now(), conectado=True,
        )
        _, codigo = self._correr('--solo', 'mqtt_worker')
        self.assertEqual(codigo, 0)


class CaidasDeEnlaceDuplicadasTests(TestCase):
    """Una caída abierta no puede sobrevivir a la recuperación del enlace.

    Nada impide dos `EventoEnlaceFarmacia` abiertos para la misma farmacia: no hay
    constraint, y `estaba_caida` es lo único que lo evita en la práctica. Hasta el
    10-oct-2026 la recuperación cerraba solo la más reciente, así que una segunda se
    volvía un fantasma — una caída abierta para siempre, en un sitio que responde, que
    ninguna pantalla podía hacer desaparecer (hallazgo D-04).
    """

    def setUp(self):
        from apps.catalogo.models import Farmacia, Grupo, UnidadNegocio

        self.farmacia = Farmacia.objects.create(
            codigo='DUP001', grupo=Grupo.objects.create(codigo='TRXDUP'),
            unidad_negocio=UnidadNegocio.objects.get(codigo='SG'), ip_router='10.60.0.1',
        )

    def _abrir(self, hace_horas):
        from apps.monitoreo.models import EventoEnlaceFarmacia

        return EventoEnlaceFarmacia.objects.create(
            farmacia=self.farmacia, inicio=timezone.now() - timedelta(hours=hace_horas),
        )

    def test_la_recuperacion_cierra_todas_las_abiertas(self):
        from apps.monitoreo.enlaces import registrar_sondeo
        from apps.monitoreo.models import EstadoEnlaceFarmacia, EventoEnlaceFarmacia

        EstadoEnlaceFarmacia.objects.create(
            farmacia=self.farmacia, alcanzable=False, respondio_alguna_vez=True,
            fallas_consecutivas=3, ultima_verificacion=timezone.now(),
        )
        vieja = self._abrir(hace_horas=5)
        fantasma = self._abrir(hace_horas=2)

        registrar_sondeo(self.farmacia, True, 12.5)

        vieja.refresh_from_db()
        fantasma.refresh_from_db()
        self.assertIsNotNone(vieja.fin)
        self.assertIsNotNone(fantasma.fin, 'la segunda abierta se quedaba abierta para siempre')
        self.assertFalse(
            EventoEnlaceFarmacia.objects.filter(farmacia=self.farmacia, fin__isnull=True).exists(),
        )

    def test_el_historial_no_se_borra(self):
        """La tabla es la línea base para discutir un SLA: un duplicado se CIERRA, nunca
        se elimina."""
        from apps.monitoreo.enlaces import registrar_sondeo
        from apps.monitoreo.models import EstadoEnlaceFarmacia, EventoEnlaceFarmacia

        EstadoEnlaceFarmacia.objects.create(
            farmacia=self.farmacia, alcanzable=False, respondio_alguna_vez=True,
            fallas_consecutivas=3, ultima_verificacion=timezone.now(),
        )
        self._abrir(hace_horas=5)
        self._abrir(hace_horas=2)

        registrar_sondeo(self.farmacia, True, 12.5)
        self.assertEqual(EventoEnlaceFarmacia.objects.filter(farmacia=self.farmacia).count(), 2)

    def test_el_comando_los_informa_sin_tocarlos(self):
        import io

        from django.core.management import call_command

        from apps.monitoreo.models import EventoEnlaceFarmacia

        self._abrir(hace_horas=5)
        self._abrir(hace_horas=2)

        salida = io.StringIO()
        with self.assertRaises(SystemExit) as capturado:
            call_command('revisar_caidas_duplicadas', stdout=salida, stderr=salida)

        self.assertEqual(capturado.exception.code, 1)
        self.assertIn('DUP001', salida.getvalue())
        self.assertEqual(
            EventoEnlaceFarmacia.objects.filter(farmacia=self.farmacia, fin__isnull=True).count(), 2,
            'el comando es de solo lectura: no cierra nada',
        )

    def test_sin_duplicados_el_comando_sale_con_cero(self):
        import io

        from django.core.management import call_command

        self._abrir(hace_horas=5)
        salida = io.StringIO()
        call_command('revisar_caidas_duplicadas', stdout=salida, stderr=salida)
        self.assertIn('Ninguna farmacia', salida.getvalue())
