"""CR-2: dos procesos evaluando a la vez no abren dos alertas iguales.

Hasta octubre de 2026 la única defensa era `_alerta_activa` en Python (consultar y después
crear): el worker MQTT y una tarea de Celery podían pasar los dos la consulta y abrir dos
alertas activas para la misma regla y estación, con dos avisos. Ahora lo decide el índice
único parcial `una_alerta_activa_por_regla_y_estacion`.

`TransactionTestCase` y hilos con su propia conexión, como `tests_ordenes_concurrencia`: con
`TestCase` todo corre en una transacción y no hay concurrencia que probar.
"""
import importlib
import threading
from unittest.mock import patch

from django.apps import apps as registro
from django.contrib.auth.models import User
from django.db import IntegrityError, connection, transaction
from django.test import TestCase, TransactionTestCase

from apps.catalogo.models import Estacion, Farmacia, Grupo, UnidadNegocio
from apps.monitoreo.models import Alerta, Metrica, ReglaAlerta
from apps.monitoreo.services import abrir_o_mantener_alerta

E = Alerta.Estado


def _escenario(sufijo):
    unidad = UnidadNegocio.objects.create(codigo=f'TAL{sufijo}')
    farmacia = Farmacia.objects.create(codigo=f'TA{sufijo}', grupo=Grupo.objects.create(codigo=f'TAG{sufijo}'),
                                       unidad_negocio=unidad)
    estacion = Estacion.objects.create(codigo=f'TA{sufijo}-A', farmacia=farmacia)
    usuario = User.objects.create_user(username=f'alertas{sufijo}', password='x')
    regla = ReglaAlerta.objects.create(
        nombre='CPU alta', metrica=Metrica.CPU_CARGA_PCT, operador=ReglaAlerta.Operador.GTE,
        umbral=90, duracion_minutos=10, creado_por=usuario,
    )
    return regla, estacion


class RestriccionTests(TestCase):
    def setUp(self):
        self.regla, self.estacion = _escenario('1')

    def test_la_base_impide_dos_activas(self):
        Alerta.objects.create(regla=self.regla, estacion=self.estacion, valor_disparador=95)
        for estado in (E.ABIERTA, E.RECONOCIDA):
            with self.subTest(estado=estado), self.assertRaises(IntegrityError), transaction.atomic():
                Alerta.objects.create(regla=self.regla, estacion=self.estacion, valor_disparador=96, estado=estado)

    def test_resuelta_la_anterior_se_puede_abrir_otra(self):
        Alerta.objects.create(regla=self.regla, estacion=self.estacion, valor_disparador=95, estado=E.RESUELTA)
        Alerta.objects.create(regla=self.regla, estacion=self.estacion, valor_disparador=95, estado=E.RESUELTA)
        self.assertIsNotNone(abrir_o_mantener_alerta(self.regla, self.estacion, valor=97))

    def test_el_choque_se_toma_como_ya_abierta_y_no_vuelve_a_avisar(self):
        """Simula al proceso que pierde la carrera: la consulta previa no la ve (otro la
        abrió justo después) y el INSERT choca contra el índice."""
        Alerta.objects.create(regla=self.regla, estacion=self.estacion, valor_disparador=95)
        with patch('apps.monitoreo.services._alerta_activa', return_value=None), \
                patch('apps.monitoreo.services.encolar_notificacion_alerta') as avisar:
            self.assertIsNone(abrir_o_mantener_alerta(self.regla, self.estacion, valor=96))
        avisar.assert_not_called()
        self.assertEqual(Alerta.objects.filter(estado=E.ABIERTA).count(), 1)


class AperturaConcurrenteTests(TransactionTestCase):
    def test_procesos_simultaneos_abren_una_sola_alerta_y_un_solo_aviso(self):
        regla, estacion = _escenario('2')
        for ronda in range(5):
            Alerta.objects.all().delete()
            hilos_n = 5
            barrera = threading.Barrier(hilos_n)
            abiertas, errores = [], []

            def evaluar():
                try:
                    barrera.wait(10)
                    alerta = abrir_o_mantener_alerta(regla, estacion, valor=95)
                    if alerta is not None:
                        abiertas.append(alerta.pk)
                except Exception as exc:  # cualquier error que no sea "ya estaba abierta"
                    errores.append(repr(exc))
                finally:
                    connection.close()

            with patch('apps.monitoreo.services.encolar_notificacion_alerta') as avisar:
                hilos = [threading.Thread(target=evaluar) for _ in range(hilos_n)]
                for hilo in hilos:
                    hilo.start()
                for hilo in hilos:
                    hilo.join(20)
            self.assertEqual(errores, [], f'ronda {ronda}')
            self.assertEqual(len(abiertas), 1, f'ronda {ronda}: se abrieron {abiertas}')
            self.assertEqual(Alerta.objects.filter(estado=E.ABIERTA).count(), 1, f'ronda {ronda}')
            self.assertEqual(avisar.call_count, 1, f'ronda {ronda}: avisos {avisar.call_count}')


class MigracionTests(TestCase):
    """La migración resuelve las activas duplicadas que ya existieran antes de crear el
    índice (si no, fallaría en producción)."""

    def test_deja_la_mas_vieja_y_resuelve_las_demas(self):
        # Simular la base de antes de la migración: sin el índice. Va primero, antes de
        # insertar nada (Postgres no altera una tabla con eventos de trigger pendientes en
        # la misma transacción). La transacción de la prueba lo deshace al terminar.
        with connection.cursor() as cursor:
            cursor.execute('DROP INDEX IF EXISTS una_alerta_activa_por_regla_y_estacion')
        regla, estacion = _escenario('3')
        migracion = importlib.import_module('apps.monitoreo.migrations.0050_una_alerta_activa_por_regla_y_estacion')
        primera = Alerta.objects.create(regla=regla, estacion=estacion, valor_disparador=95)
        segunda =Alerta.objects.create(regla=regla, estacion=estacion, valor_disparador=96, estado=E.RECONOCIDA)
        otra_estacion = Estacion.objects.create(codigo='TA3-B', farmacia=estacion.farmacia)
        aparte = Alerta.objects.create(regla=regla, estacion=otra_estacion, valor_disparador=95)

        migracion.resolver_duplicadas(registro, None)

        estados = dict(Alerta.objects.values_list('pk', 'estado'))
        self.assertEqual(estados[primera.pk], E.ABIERTA)
        self.assertEqual(estados[segunda.pk], E.RESUELTA)
        self.assertEqual(estados[aparte.pk], E.ABIERTA)
        self.assertIsNotNone(Alerta.objects.get(pk=segunda.pk).resuelta_en)
