from django.test import TestCase

from apps.catalogo.models import Estacion, Farmacia, Grupo, UnidadNegocio

from .connectors import ConectorExterno, conectores_registrados, obtener_conector, registrar_conector
from .models import DireccionSync, EstadoSync, EventoSyncExterno, SincronizacionExterna
from .services import ejecutar_sync, registrar_sync_pendiente


@registrar_conector('conector_ok_test')
class _ConectorOkTest(ConectorExterno):
    def enviar(self, sincronizacion):
        return {'id_externo': 42}


@registrar_conector('conector_falla_test')
class _ConectorFallaTest(ConectorExterno):
    def enviar(self, sincronizacion):
        raise RuntimeError('el sistema externo rechazó el payload')


class RegistroConectoresTests(TestCase):
    def test_conector_registrado_se_puede_obtener_por_nombre(self):
        instancia = obtener_conector('conector_ok_test')
        self.assertIsInstance(instancia, _ConectorOkTest)

    def test_nombre_no_registrado_lanza_key_error(self):
        with self.assertRaises(KeyError):
            obtener_conector('no-existe')

    def test_conectores_registrados_incluye_los_de_prueba(self):
        self.assertIn('conector_ok_test', conectores_registrados())
        self.assertIn('conector_falla_test', conectores_registrados())


class RegistrarSyncPendienteTests(TestCase):
    def setUp(self):
        sg = UnidadNegocio.objects.get(codigo='SG')
        grupo = Grupo.objects.create(codigo='TRX001')
        farmacia = Farmacia.objects.create(codigo='ML001', grupo=grupo, unidad_negocio=sg)
        self.estacion = Estacion.objects.create(codigo='ML001-A', farmacia=farmacia)

    def test_crea_sincronizacion_pendiente_con_tenant_derivado(self):
        sincronizacion = registrar_sync_pendiente(conector='conector_ok_test', objeto=self.estacion)

        self.assertEqual(sincronizacion.estado, EstadoSync.PENDIENTE)
        self.assertEqual(sincronizacion.direccion, DireccionSync.SALIENTE)
        self.assertEqual(sincronizacion.modelo, 'catalogo.Estacion')
        self.assertEqual(sincronizacion.objeto_id, str(self.estacion.pk))
        self.assertEqual(sincronizacion.unidad_negocio, self.estacion.farmacia.unidad_negocio)
        self.assertEqual(sincronizacion.eventos.count(), 1)

    def test_reencolar_el_mismo_objeto_no_duplica_la_fila(self):
        registrar_sync_pendiente(conector='conector_ok_test', objeto=self.estacion)
        registrar_sync_pendiente(conector='conector_ok_test', objeto=self.estacion)

        self.assertEqual(SincronizacionExterna.objects.count(), 1)
        self.assertEqual(EventoSyncExterno.objects.count(), 2)  # una línea de tiempo por intento


class EjecutarSyncTests(TestCase):
    def setUp(self):
        sg = UnidadNegocio.objects.get(codigo='SG')
        grupo = Grupo.objects.create(codigo='TRX001')
        farmacia = Farmacia.objects.create(codigo='ML001', grupo=grupo, unidad_negocio=sg)
        self.estacion = Estacion.objects.create(codigo='ML001-A', farmacia=farmacia)

    def test_conector_exitoso_marca_enviado_y_guarda_respuesta(self):
        sincronizacion = registrar_sync_pendiente(conector='conector_ok_test', objeto=self.estacion)

        ejecutar_sync(sincronizacion)

        sincronizacion.refresh_from_db()
        self.assertEqual(sincronizacion.estado, EstadoSync.ENVIADO)
        self.assertEqual(sincronizacion.intentos, 1)
        self.assertEqual(sincronizacion.respuesta, {'id_externo': 42})
        self.assertEqual(sincronizacion.eventos.last().estado, EstadoSync.ENVIADO)

    def test_conector_que_falla_marca_error_y_relanza(self):
        sincronizacion = registrar_sync_pendiente(conector='conector_falla_test', objeto=self.estacion)

        with self.assertRaises(RuntimeError):
            ejecutar_sync(sincronizacion)

        sincronizacion.refresh_from_db()
        self.assertEqual(sincronizacion.estado, EstadoSync.ERROR)
        self.assertEqual(sincronizacion.intentos, 1)
        self.assertIn('rechazó el payload', sincronizacion.ultimo_error)
        self.assertEqual(sincronizacion.eventos.last().estado, EstadoSync.ERROR)


class SincronizarTaskTests(TestCase):
    """CELERY_TASK_ALWAYS_EAGER=True hace que .delay() corra sincrónico en el test."""

    def setUp(self):
        sg = UnidadNegocio.objects.get(codigo='SG')
        grupo = Grupo.objects.create(codigo='TRX001')
        farmacia = Farmacia.objects.create(codigo='ML001', grupo=grupo, unidad_negocio=sg)
        self.estacion = Estacion.objects.create(codigo='ML001-A', farmacia=farmacia)

    def test_delay_ejecuta_el_conector_y_marca_enviado(self):
        from apps.integraciones.tasks import sincronizar_task

        sincronizacion = registrar_sync_pendiente(conector='conector_ok_test', objeto=self.estacion)

        resultado = sincronizar_task.delay(sincronizacion.pk)

        sincronizacion.refresh_from_db()
        self.assertEqual(sincronizacion.estado, EstadoSync.ENVIADO)
        self.assertIn('conector_ok_test', resultado.get())
