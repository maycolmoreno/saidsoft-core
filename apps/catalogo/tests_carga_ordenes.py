"""Carga del procesamiento periódico de la cola (`ordenes.procesar`, cada minuto).

Se saltea salvo que se pida: CARGA_ORDENES=1000,3000,5000 python manage.py test
apps.catalogo.tests_carga_ordenes --noinput

Escenario por estación (el peor caso normal para el despacho):
- una instalación `entregada` sin acuse, con la estación apagada (no se reentrega);
- un despliegue `en_ejecucion` con avance reciente (no se interrumpe);
- otra instalación `pendiente`, esperando la ranura ocupada (el despacho la intenta y no
  puede).

Mide el tiempo de una pasada y la cantidad de consultas. Son mediciones de la máquina donde
corre la prueba, contra el PostgreSQL local; NO son la capacidad del NUC.
"""
import os
import secrets
import time
from datetime import timedelta
from unittest import skipUnless

from django.db import connection
from django.test import TestCase
from django.utils import timezone

from apps.catalogo import ordenes
from apps.catalogo.models import Estacion, Farmacia, Grupo, OrdenAgente, UnidadNegocio

TAMANIOS = [int(n) for n in os.environ.get('CARGA_ORDENES', '').split(',') if n.strip()]
E = OrdenAgente.Estado


@skipUnless(TAMANIOS, 'prueba de carga: definir CARGA_ORDENES=1000,3000,5000')
class CargaProcesarOrdenesTests(TestCase):
    def _sembrar(self, n):
        OrdenAgente.objects.all().delete()
        Estacion.objects.filter(codigo__startswith='CRG').delete()
        unidad = UnidadNegocio.objects.get(codigo='SG')
        grupo, _ = Grupo.objects.get_or_create(codigo='CRGG')
        farmacia, _ = Farmacia.objects.get_or_create(codigo='CRG', defaults={'grupo': grupo, 'unidad_negocio': unidad})
        ahora = timezone.now()
        estaciones = Estacion.objects.bulk_create([
            Estacion(codigo=f'CRG-{i:05d}', farmacia=farmacia, estado_aprobacion=Estacion.EstadoAprobacion.APROBADA,
                     token_enrolamiento=secrets.token_hex(16), hmac_secret=secrets.token_hex(16),
                     ultimo_heartbeat=ahora - timedelta(hours=2))
            for i in range(n)
        ])
        base = dict(topico='t', payload='{}', repetible=True, plazo_sin_avance_s=6 * 3600,
                    expira_en=ahora + timedelta(days=14))
        filas = []
        for e in estaciones:
            filas += [
                OrdenAgente(estacion=e, tipo='software', origen_id=1, clave_idempotencia='software:1:1',
                            estado=E.ENTREGADA, entregas=1, ultima_entrega_en=ahora - timedelta(minutes=30), **base),
                OrdenAgente(estacion=e, tipo='despliegue', origen_id=1, clave_idempotencia='despliegue:1:1',
                            estado=E.EN_EJECUCION, ultimo_reporte_en=ahora - timedelta(minutes=5), **base),
                OrdenAgente(estacion=e, tipo='software', origen_id=2, clave_idempotencia='software:2:1',
                            estado=E.PENDIENTE, **base),
            ]
        OrdenAgente.objects.bulk_create(filas, batch_size=2000)
        return ahora

    def test_una_pasada_de_procesar(self):
        resultados = []
        for n in TAMANIOS:
            ahora = self._sembrar(n)
            # Contador propio y no CaptureQueriesContext: el registro de Django guarda como
            # máximo 9.000 consultas y por encima de eso el conteo deja de servir.
            contador = {'n': 0}

            def contar(execute, sql, params, many, context):
                contador['n'] += 1
                return execute(sql, params, many, context)

            with connection.execute_wrapper(contar):
                inicio = time.perf_counter()
                resumen = ordenes.procesar(ahora=ahora)
                duracion = time.perf_counter() - inicio
            resultados.append((n, 3 * n, duracion, contador['n'], resumen))
            # Nada cambia de estado en este escenario: si algo se mueve, el escenario está mal.
            self.assertNotIn('omitida', resumen)
            self.assertEqual({k: v for k, v in resumen.items() if v}, {})
            # Tiempo por fase, sobre el mismo escenario (nada cambia de estado, así que se repite).
            fases = {}
            for nombre, fase in (('caducar', ordenes.caducar), ('revisar_publicando', ordenes.revisar_publicando),
                                 ('revisar_entregadas', ordenes.revisar_entregadas),
                                 ('detectar_interrumpidas', ordenes.detectar_interrumpidas),
                                 ('despachar', lambda a: ordenes.despachar(ahora=a))):
                inicio = time.perf_counter()
                fase(ahora)
                fases[nombre] = time.perf_counter() - inicio
            resultados[-1] = (*resultados[-1][:4], fases)
        print('\n  estaciones  órdenes   segundos  consultas  consultas/estación')
        for n, total, duracion, cant, _ in resultados:
            print(f'  {n:>10}  {total:>7}  {duracion:>9.2f}  {cant:>9}  {cant / n:>18.1f}')
        print('  por fase (s): ' + ' | '.join(
            f'{n}: ' + ', '.join(f'{k} {v:.2f}' for k, v in fases.items()) for n, *_, fases in resultados
        ))

    def test_pasada_con_trabajo_masivo(self):
        """Peor caso: cada estación con una instalación esperando y la ranura libre (despacho
        masivo) y un despliegue en ejecución vencido con la estación encendida (interrupción
        masiva). La publicación MQTT se reemplaza (EMQX acepta todo): se mide el costo en la
        base.

        Cada fase procesa como mucho `ordenes.LIMITE_POR_FASE` filas por pasada: se mide la
        primera pasada y cuántas pasadas (de un minuto) hacen falta para drenar todo."""
        from unittest.mock import patch

        def broker_acepta(mensajes):
            return ['aceptado'] * len(mensajes)

        resultados = []
        for n in TAMANIOS:
            ahora = self._sembrar(n)
            OrdenAgente.objects.filter(tipo='software', origen_id=1).delete()  # ranura libre
            OrdenAgente.objects.filter(tipo='despliegue').update(ultimo_reporte_en=ahora - timedelta(hours=7))
            Estacion.objects.filter(codigo__startswith='CRG').update(ultimo_heartbeat=ahora)
            contador = {'n': 0}

            def contar(execute, sql, params, many, context):
                contador['n'] += 1
                return execute(sql, params, many, context)

            tope = min(n, ordenes.LIMITE_POR_FASE)
            with patch('apps.catalogo.ordenes.publicar_lote', side_effect=broker_acepta) as publicar, \
                    connection.execute_wrapper(contar):
                inicio = time.perf_counter()
                resumen = ordenes.procesar(ahora=ahora)
                duracion = time.perf_counter() - inicio
                self.assertEqual((resumen['despachadas'], resumen['interrumpidas']), (tope, tope))
                self.assertEqual(publicar.call_count, 1)  # una sola conexión para todo el lote
                consultas = contador['n']
                pasadas, inicio_total = 1, time.perf_counter()
                while OrdenAgente.objects.filter(estado=E.PENDIENTE, tipo='software').exists() \
                        or OrdenAgente.objects.filter(tipo='despliegue', estado=E.EN_EJECUCION).exists():
                    pasadas += 1
                    ordenes.procesar(ahora=ahora + timedelta(seconds=pasadas))
                drenado = duracion + time.perf_counter() - inicio_total
            self.assertEqual(OrdenAgente.objects.filter(tipo='software', estado=E.ENTREGADA).count(), n)
            self.assertEqual(OrdenAgente.objects.filter(tipo='despliegue', estado=E.INTERRUMPIDA).count(), n)
            resultados.append((n, duracion, consultas, pasadas, drenado))
        print('\n  [trabajo masivo] estaciones  1ª pasada (s)  consultas  pasadas hasta drenar  total (s)')
        for n, duracion, cant, pasadas, drenado in resultados:
            print(f'  {n:>27}  {duracion:>13.2f}  {cant:>9}  {pasadas:>20}  {drenado:>9.2f}')

    def test_publicar_una_campania_desde_el_panel(self):
        """Costo de `publicar_solicitud` a N estaciones dentro de la petición HTTP: crea y
        reserva una orden por estación. Antes de la cola eran un bulk_create y una
        publicación. La publicación MQTT se reemplaza."""
        from unittest.mock import patch

        from django.contrib.auth.models import User
        from django.core.files.uploadedfile import SimpleUploadedFile

        from apps.software.models import AplicacionCatalogo, DestinoTipo, SolicitudInstalacion, VersionAplicacion
        from apps.software.services import publicar_solicitud

        usuario = User.objects.create_user(username='carga', password='x')
        version = VersionAplicacion.objects.create(
            aplicacion=AplicacionCatalogo.objects.create(nombre='Carga', creado_por=usuario), version='1',
            instalador=SimpleUploadedFile('c.msi', b'x'), comando_instalacion_silenciosa='x',
        )
        from apps.software.models import ResultadoInstalacion

        resultados = []
        for n in TAMANIOS:
            ResultadoInstalacion.objects.filter(estacion__codigo__startswith='CRG').delete()
            self._sembrar(n)
            OrdenAgente.objects.all().delete()
            solicitud = SolicitudInstalacion.objects.create(
                version_aplicacion=version, unidad_negocio=UnidadNegocio.objects.get(codigo='SG'),
                destino_tipo=DestinoTipo.ESTACIONES, creado_por=usuario,
            )
            solicitud.estaciones.set(Estacion.objects.filter(codigo__startswith='CRG'))
            with patch('apps.catalogo.services._enviar_lote_v5', side_effect=lambda m, **_: ['aceptado'] * len(m)):
                inicio = time.perf_counter()
                self.assertTrue(publicar_solicitud(solicitud).exitoso)
                duracion = time.perf_counter() - inicio
            resultados.append((n, duracion))
        print('\n  [campaña desde el panel] estaciones  segundos')
        for n, duracion in resultados:
            print(f'  {n:>35}  {duracion:>8.2f}')
