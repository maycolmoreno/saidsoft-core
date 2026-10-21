"""Concurrencia real sobre PostgreSQL: dos procesos no pueden reservar la misma ranura, y la
pasada periódica de la cola corre de a una.

`TransactionTestCase` y no `TestCase`: con `TestCase` todo corre en una sola transacción y
no hay concurrencia que probar. Cada hilo usa su propia conexión a la base.

Qué da la garantía de la ranura. No es la comprobación previa de `reservar_ranura` (un
`exists()` en Python, que dos procesos pueden pasar a la vez), sino el índice ÚNICO PARCIAL
`orden_agente_una_entregada_por_ranura`: PostgreSQL hace esperar a la segunda transacción
que intenta dejar otra fila `publicando` o `entregada` en la misma (estación, tipo) hasta
que la primera termina, y si la primera confirmó, la segunda falla con violación de
unicidad. Un índice común no haría nada de esto; uno único sí, y es lo que estas pruebas
comprueban.

Qué da la exclusión de la pasada periódica: `pg_try_advisory_lock` en la conexión de la
pasada (ver `ordenes.procesar`). Vale entre procesos y máquinas, y PostgreSQL lo libera solo
si la conexión se cierra (un worker que muere).
"""
import threading
from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth.models import User
from django.db import IntegrityError, OperationalError, connection, connections, transaction
from django.test import TransactionTestCase
from django.utils import timezone

from apps.catalogo import ordenes
from apps.catalogo.models import Estacion, Farmacia, Grupo, OrdenAgente, UnidadNegocio

E = OrdenAgente.Estado


class ReservaConcurrenteTests(TransactionTestCase):
    def setUp(self):
        unidad = UnidadNegocio.objects.create(codigo='TCON')
        farmacia = Farmacia.objects.create(codigo='TC001', grupo=Grupo.objects.create(codigo='TCG'), unidad_negocio=unidad)
        self.estacion = Estacion.objects.create(codigo='TC001-A', farmacia=farmacia)
        self.usuario = User.objects.create_user(username='concurrencia', password='x')

    def _orden(self, origen):
        return ordenes.crear_orden(
            estacion=self.estacion, tipo='software', origen_id=origen, topico='t', payload_str='{}',
            repetible=True, ronda=1,
        )

    def test_el_indice_unico_hace_esperar_a_la_segunda_transaccion_y_la_rechaza(self):
        a, b = self._orden(1), self._orden(2)
        primera_escribio, soltar_primera = threading.Event(), threading.Event()
        segunda_termino = threading.Event()
        resultado = {}

        def primera():
            try:
                with transaction.atomic():
                    OrdenAgente.objects.filter(pk=a.pk).update(estado=E.ENTREGADA)
                    primera_escribio.set()
                    soltar_primera.wait(10)
            finally:
                connection.close()

        def segunda():
            try:
                primera_escribio.wait(10)
                with transaction.atomic():
                    OrdenAgente.objects.filter(pk=b.pk).update(estado=E.ENTREGADA)
                resultado['segunda'] = 'confirmó'
            except IntegrityError:
                resultado['segunda'] = 'violación de unicidad'
            finally:
                segunda_termino.set()
                connection.close()

        h1, h2 = threading.Thread(target=primera), threading.Thread(target=segunda)
        h1.start()
        h2.start()
        # Mientras la primera no confirma, la segunda queda BLOQUEADA en el índice.
        self.assertFalse(segunda_termino.wait(1.5), 'la segunda no esperó a la primera')
        soltar_primera.set()
        h1.join(10)
        h2.join(10)
        self.assertEqual(resultado.get('segunda'), 'violación de unicidad')
        self.assertEqual(
            OrdenAgente.objects.filter(estacion=self.estacion, estado=E.ENTREGADA).count(), 1,
        )

    def test_reservas_simultaneas_solo_una_gana_la_ranura(self):
        """Diez rondas con cinco hilos a la vez, cada uno con su orden para la misma ranura."""
        for ronda in range(10):
            OrdenAgente.objects.all().delete()
            pendientes = [self._orden(100 * ronda + i) for i in range(5)]
            barrera = threading.Barrier(len(pendientes))
            ganadoras, errores = [], []

            def reservar(orden):
                try:
                    barrera.wait(10)
                    if ordenes.reservar_ranura(orden):
                        ganadoras.append(orden.pk)
                except Exception as exc:  # cualquier error que no sea "perdí la ranura"
                    errores.append(repr(exc))
                finally:
                    connection.close()

            hilos = [threading.Thread(target=reservar, args=(o,)) for o in pendientes]
            for h in hilos:
                h.start()
            for h in hilos:
                h.join(20)
            self.assertEqual(errores, [], f'ronda {ronda}')
            self.assertEqual(len(ganadoras), 1, f'ronda {ronda}: ganaron {ganadoras}')
            self.assertEqual(
                OrdenAgente.objects.filter(estacion=self.estacion, estado=E.PUBLICANDO).count(), 1,
                f'ronda {ronda}',
            )
            self.assertEqual(
                OrdenAgente.objects.filter(estacion=self.estacion, estado=E.PENDIENTE).count(), 4,
                f'ronda {ronda}: las perdedoras siguen pendientes, sin intentos sumados',
            )
            self.assertEqual(
                sorted(OrdenAgente.objects.filter(estacion=self.estacion).values_list('entregas', flat=True)),
                [0, 0, 0, 0, 1], f'ronda {ronda}',
            )


def broker_acepta(mensajes):
    return ['aceptado'] * len(mensajes)


class PasadaPeriodicaTests(TransactionTestCase):
    """`procesar` (la corre `procesar_ordenes_task` cada minuto)."""

    def setUp(self):
        unidad = UnidadNegocio.objects.create(codigo='TPAS')
        self.farmacia = Farmacia.objects.create(
            codigo='TP001', grupo=Grupo.objects.create(codigo='TPG'), unidad_negocio=unidad,
        )
        self.publicar = patch('apps.catalogo.ordenes.publicar_lote', side_effect=broker_acepta).start()
        self.addCleanup(patch.stopall)

    def _pendientes(self, n):
        estaciones = Estacion.objects.bulk_create([
            Estacion(codigo=f'TP001-{i:04d}', farmacia=self.farmacia, token_enrolamiento=f'tok-{i}',
                     hmac_secret=f'sec-{i}')
            for i in range(n)
        ])
        ahora = timezone.now()
        OrdenAgente.objects.bulk_create([
            OrdenAgente(estacion=e, tipo='software', origen_id=1, clave_idempotencia='software:1:1', topico=f't/{e.pk}',
                        payload='{}', repetible=True, plazo_sin_avance_s=3600, expira_en=ahora + timedelta(days=1))
            for e in estaciones
        ])

    def _otra_conexion(self):
        otra = connections.create_connection('default')
        self.addCleanup(otra.close)
        return otra

    def _tomar_cerrojo(self, conexion):
        with conexion.cursor() as cursor:
            cursor.execute('SELECT pg_try_advisory_lock(%s)', [ordenes.CERROJO_PROCESAR])
            return cursor.fetchone()[0]

    def test_dos_workers_a_la_vez_solo_uno_procesa(self):
        self._pendientes(30)
        barrera = threading.Barrier(2)
        resumenes, errores = [], []
        original = ordenes.despachar

        def despachar_lento(*args, **kwargs):
            # Ensancha la ventana: la pasada que ganó sigue adentro mientras la otra intenta.
            import time
            time.sleep(1.0)
            return original(*args, **kwargs)

        def worker():
            try:
                barrera.wait(10)
                resumenes.append(ordenes.procesar())
            except Exception as exc:
                errores.append(repr(exc))
            finally:
                connection.close()

        with patch('apps.catalogo.ordenes.despachar', side_effect=despachar_lento):
            hilos = [threading.Thread(target=worker) for _ in range(2)]
            for h in hilos:
                h.start()
            for h in hilos:
                h.join(30)
        self.assertEqual(errores, [])
        self.assertEqual(sorted('omitida' in r for r in resumenes), [False, True])
        self.assertEqual(sum(r.get('despachadas', 0) for r in resumenes), 30)
        self.assertEqual(self.publicar.call_count, 1)
        self.assertEqual(OrdenAgente.objects.filter(estado=E.ENTREGADA).count(), 30)

    def test_una_pasada_mas_larga_que_el_intervalo_hace_omitir_la_siguiente(self):
        """La pasada anterior (otra conexión, como otro proceso de Celery) todavía tiene el
        cerrojo: la de este minuto no hace nada y no toca la base."""
        self._pendientes(5)
        anterior = self._otra_conexion()
        self.assertTrue(self._tomar_cerrojo(anterior))
        self.assertEqual(ordenes.procesar(), {'omitida': True})
        self.assertEqual(OrdenAgente.objects.filter(estado=E.PENDIENTE).count(), 5)
        with anterior.cursor() as cursor:  # termina la anterior
            cursor.execute('SELECT pg_advisory_unlock(%s)', [ordenes.CERROJO_PROCESAR])
        self.assertEqual(ordenes.procesar()['despachadas'], 5)

    def test_un_worker_que_muere_no_deja_el_cerrojo_tomado(self):
        self._pendientes(3)
        muerto = self._otra_conexion()
        self.assertTrue(self._tomar_cerrojo(muerto))
        self.assertEqual(ordenes.procesar(), {'omitida': True})
        muerto.close()  # el proceso murió: PostgreSQL cierra su sesión y suelta el cerrojo
        self.assertEqual(ordenes.procesar()['despachadas'], 3)

    def test_una_pasada_que_falla_suelta_el_cerrojo(self):
        self._pendientes(2)
        with patch('apps.catalogo.ordenes.caducar', side_effect=OperationalError('server closed the connection')):
            with self.assertRaises(OperationalError):
                ordenes.procesar()
        otra = self._otra_conexion()
        self.assertTrue(self._tomar_cerrojo(otra))  # quedó libre
        with otra.cursor() as cursor:
            cursor.execute('SELECT pg_advisory_unlock(%s)', [ordenes.CERROJO_PROCESAR])

    def test_error_transitorio_de_la_base_en_la_tarea(self):
        """La tarea no revienta ni reintenta en caliente: cierra la conexión (lo que libera el
        cerrojo aunque el unlock no haya podido correr) y la pasada siguiente retoma."""
        from apps.catalogo.tasks import procesar_ordenes_task

        self._pendientes(4)
        with patch('apps.catalogo.ordenes.despachar', side_effect=OperationalError('could not connect to server')):
            self.assertEqual(procesar_ordenes_task(), {'error_transitorio': True})
        self.assertEqual(OrdenAgente.objects.filter(estado=E.PENDIENTE).count(), 4)  # nada a medias
        otra = self._otra_conexion()
        self.assertTrue(self._tomar_cerrojo(otra))
        with otra.cursor() as cursor:
            cursor.execute('SELECT pg_advisory_unlock(%s)', [ordenes.CERROJO_PROCESAR])
        self.assertEqual(procesar_ordenes_task()['despachadas'], 4)

    def test_alto_volumen_se_procesa_por_tandas_sin_perder_ni_duplicar(self):
        self._pendientes(25)
        pasadas = []
        for i in range(10):
            resumen = ordenes.procesar(ahora=timezone.now() + timedelta(seconds=i), limite=10)
            pasadas.append(resumen['despachadas'])
            if not resumen['despachadas']:
                break
        self.assertEqual(pasadas, [10, 10, 5, 0])
        self.assertEqual(OrdenAgente.objects.filter(estado=E.ENTREGADA).count(), 25)
        self.assertEqual(OrdenAgente.objects.filter(entregas=1).count(), 25)  # una publicación por orden
        mensajes = [m['topic'] for c in self.publicar.call_args_list for m in c.args[0]]
        self.assertEqual(len(mensajes), len(set(mensajes)))
