"""Cola persistente de órdenes a los agentes (fase 1). Ver apps/catalogo/ordenes.py.

Cubre las pruebas obligatorias de la fase:
- entrega y recepción;
- confirmaciones duplicadas;
- pérdida de conexión antes y después de recibir;
- reinicio del agente y del servidor;
- caducidad y cancelación;
- error durante un despliegue y recuperación después de una interrupción;
- prevención de ejecuciones duplicadas y compatibilidad con los flujos existentes;
- aceptación de la publicación por el broker (MQTT 5): aceptada, rechazada por la ACL, sin
  conexión, corte en medio de la publicación y caída del proceso entre el PUBACK y el
  guardado del resultado.

El transporte sigue siendo MQTT y no hay broker en las pruebas: se reemplazan
`_enviar_lote_v5` (publicación en lote de software, despliegues, despacho, reentrega y
retiros periódicos) y `_enviar_mqtt_v5` (un mensaje: retirar al cancelar, scripts). El
reemplazo de `_enviar_lote_v5` responde por mensaje lo que cada prueba define
(`self.respuesta`). Celery corre en modo inmediato.
"""
import json
from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import IntegrityError, transaction
from django.test import TestCase
from django.utils import timezone

from apps.catalogo import ordenes
from apps.catalogo.models import Estacion, Farmacia, Grupo, OrdenAgente, UnidadNegocio
from apps.catalogo.services import PublicacionIncierta, PublicacionRechazada, SinConexion
from apps.despliegues.models import Despliegue, EventoDespliegue, ResultadoDespliegue
from apps.despliegues.services import publicar_despliegue, reintentar_despliegue
from apps.mqtt_worker.services import manejar_estado_despliegue, manejar_estado_instalacion, manejar_estado_script
from apps.scripts.models import ResultadoEjecucionScript, Script, TipoScript
from apps.scripts.services import registrar_ejecucion_script
from apps.software.models import (
    AplicacionCatalogo, DestinoTipo, EstadoSolicitud, EventoInstalacion, ResultadoInstalacion, SolicitudInstalacion,
    VersionAplicacion,
)
from apps.software.services import publicar_solicitud

E = OrdenAgente.Estado


class BaseCola(TestCase):
    def setUp(self):
        self.sg = UnidadNegocio.objects.get(codigo='SG')
        farmacia = Farmacia.objects.create(codigo='ML001', grupo=Grupo.objects.create(codigo='TRX001'), unidad_negocio=self.sg)
        self.a = Estacion.objects.create(codigo='ML001-A', farmacia=farmacia, estado_aprobacion=Estacion.EstadoAprobacion.APROBADA)
        self.b = Estacion.objects.create(codigo='ML001-B', farmacia=farmacia, estado_aprobacion=Estacion.EstadoAprobacion.APROBADA)
        self.usuario = User.objects.create_user(username='operador', password='x')
        self.aprobador = User.objects.create_user(username='aprobador', password='x')
        self.app = AplicacionCatalogo.objects.create(nombre='Google Chrome', creado_por=self.usuario)
        self.version = VersionAplicacion.objects.create(
            aplicacion=self.app, version='128.0.0', instalador=SimpleUploadedFile('chrome.msi', b'x'),
            comando_instalacion_silenciosa='msiexec /i "{archivo}" /qn',
        )
        # Sin broker. Software, despliegues y la cola publican por el MISMO transporte en
        # lote (`_enviar_lote_v5`): un solo reemplazo los cubre a todos. `self.respuesta`
        # decide el PUBACK de cada mensaje; `self.fallo`, si no se pudo conectar.
        self.respuesta = lambda mensaje: 'aceptado'
        self.fallo = None
        self.pub = patch('apps.catalogo.services._enviar_lote_v5', side_effect=self._broker).start()
        self.v5 = patch('apps.catalogo.services._enviar_mqtt_v5').start()
        self.addCleanup(patch.stopall)

    def _broker(self, mensajes, **_):
        if self.fallo is not None:
            raise self.fallo
        return [self.respuesta(m) for m in mensajes]

    # --- ayudas ---
    def _latido(self, estacion, cuando):
        Estacion.objects.filter(pk=estacion.pk).update(ultimo_heartbeat=cuando)
        estacion.refresh_from_db()

    def _solicitud(self, *estaciones):
        solicitud = SolicitudInstalacion.objects.create(
            version_aplicacion=self.version, unidad_negocio=self.sg, destino_tipo=DestinoTipo.ESTACIONES,
            creado_por=self.usuario,
        )
        solicitud.estaciones.set(estaciones)
        return solicitud

    def _despliegue(self, *estaciones):
        despliegue = Despliegue.objects.create(
            version='4.3.0', archivo=SimpleUploadedFile('pkg.zip', b'x'),
            modo_aplicacion=Despliegue.ModoAplicacion.INMEDIATO, destino_tipo=Despliegue.DestinoTipo.ESTACIONES,
            unidad_negocio=self.sg, estado=Despliegue.Estado.APROBADO, umbral_error_pct=100,
            creado_por=self.usuario, aprobado_por=self.aprobador,
        )
        despliegue.estaciones.set(estaciones)
        return despliegue

    def _rep_software(self, estacion, solicitud, paso):
        manejar_estado_instalacion(estacion.codigo, {
            'token': estacion.token_enrolamiento, 'solicitud_id': solicitud.id, 'paso': paso,
        })

    def _rep_despliegue(self, estacion, despliegue, paso, **extra):
        manejar_estado_despliegue(estacion.codigo, {
            'token': estacion.token_enrolamiento, 'despliegue_id': despliegue.id, 'paso': paso, **extra,
        })

    def _orden(self, estacion, tipo, origen_id):
        return ordenes.ultima_orden(estacion, tipo, origen_id)

    def _mensajes(self):
        return [m for llamada in self.pub.call_args_list for m in llamada.args[0]]

    def _payloads(self):
        """Lo publicado como orden (sin los retiros, que son payload vacío)."""
        return [m['payload'] for m in self._mensajes() if m['payload']]

    def _retiros(self):
        return [m['topic'] for m in self._mensajes() if m['payload'] == '' and m['retain']]

    def _estados(self, orden):
        orden.refresh_from_db()
        return [h['estado'] for h in orden.historial]


class EntregaYRecepcionTests(BaseCola):
    def test_entrega_y_recepcion_de_una_orden(self):
        solicitud = self._solicitud(self.a)
        self.assertTrue(publicar_solicitud(solicitud).exitoso)

        orden = self._orden(self.a, 'software', solicitud.id)
        self.assertEqual(orden.estado, E.ENTREGADA)
        self.assertEqual(orden.entregas, 1)
        self.assertEqual(self._payloads(), [orden.payload])  # la base guarda lo publicado

        self._rep_software(self.a, solicitud, 'recibido')
        orden.refresh_from_db()
        self.assertEqual(orden.estado, E.RECIBIDA)
        self._rep_software(self.a, solicitud, 'descargado')
        orden.refresh_from_db()
        self.assertEqual(orden.estado, E.EN_EJECUCION)
        self._rep_software(self.a, solicitud, 'instalado')
        orden.refresh_from_db()
        self.assertEqual(orden.estado, E.COMPLETADA)
        self.assertIsNotNone(orden.finalizada_en)
        # El modelo de resultado del dominio se sigue actualizando igual.
        self.assertEqual(
            ResultadoInstalacion.objects.get(solicitud=solicitud, estacion=self.a).estado,
            ResultadoInstalacion.Estado.INSTALADO,
        )

    def test_entregada_no_es_completada_sin_el_resultado_final(self):
        solicitud = self._solicitud(self.a)
        publicar_solicitud(solicitud)
        self._rep_software(self.a, solicitud, 'recibido')
        self._rep_software(self.a, solicitud, 'instalando')
        self.assertEqual(self._orden(self.a, 'software', solicitud.id).estado, E.EN_EJECUCION)


class DuplicadosTests(BaseCola):
    def test_confirmaciones_duplicadas_y_fuera_de_orden_no_cambian_nada(self):
        solicitud = self._solicitud(self.a)
        publicar_solicitud(solicitud)
        for paso in ('recibido', 'recibido', 'instalado', 'instalado', 'descargado', 'error'):
            self._rep_software(self.a, solicitud, paso)
        orden = self._orden(self.a, 'software', solicitud.id)
        self.assertEqual(orden.estado, E.COMPLETADA)
        self.assertEqual(self._estados(orden), [E.PENDIENTE, E.PUBLICANDO, E.ENTREGADA, E.RECIBIDA, E.COMPLETADA])

    def test_la_base_impide_dos_ordenes_en_la_misma_ranura(self):
        """Ni dos entregadas, ni dos publicándose, ni una de cada: todas pueden estar en el
        broker y la segunda pisaría a la primera."""
        solicitud = self._solicitud(self.a)
        publicar_solicitud(solicitud)  # entregada
        for estado in (E.ENTREGADA, E.PUBLICANDO):
            otra = ordenes.crear_orden(
                estacion=self.a, tipo='software', origen_id=999, topico='t', payload_str='{}', repetible=True,
                ronda=1,
            )
            otra.estado = estado
            with self.subTest(estado=estado), self.assertRaises(IntegrityError), transaction.atomic():
                otra.save()
        OrdenAgente.objects.filter(estacion=self.a, origen_id=solicitud.id).update(estado=E.PUBLICANDO)
        otra.estado = E.ENTREGADA
        with self.assertRaises(IntegrityError), transaction.atomic():
            otra.save()

    def test_publicar_dos_veces_la_misma_solicitud_no_duplica_la_orden_en_curso(self):
        solicitud = self._solicitud(self.a)
        publicar_solicitud(solicitud)
        publicar_solicitud(solicitud)
        self.assertEqual(OrdenAgente.objects.filter(estacion=self.a, origen_id=solicitud.id).count(), 1)
        self.assertEqual(self.pub.call_count, 1)


class DesconexionTests(BaseCola):
    def test_estacion_apagada_la_segunda_orden_espera_y_sale_al_recibir_la_primera(self):
        """Antes la segunda pisaba a la primera en el retenido y la primera se perdía."""
        self._latido(self.a, timezone.now() - timedelta(hours=3))
        s1, s2 = self._solicitud(self.a), self._solicitud(self.a)
        publicar_solicitud(s1)
        publicar_solicitud(s2)
        o1, o2 = self._orden(self.a, 'software', s1.id), self._orden(self.a, 'software', s2.id)
        self.assertEqual((o1.estado, o2.estado), (E.ENTREGADA, E.PENDIENTE))
        self.assertEqual(self.pub.call_count, 1)  # la segunda no salió

        self._rep_software(self.a, s1, 'recibido')  # la estación volvió y tomó la primera
        o2.refresh_from_db()
        self.assertEqual(o2.estado, E.ENTREGADA)
        self.assertEqual(self._payloads(), [o1.payload, o2.payload])

    def test_estacion_apagada_no_se_reentrega(self):
        solicitud = self._solicitud(self.a)
        self._latido(self.a, timezone.now() - timedelta(hours=3))
        publicar_solicitud(solicitud)
        antes = self.pub.call_count
        ordenes.procesar(ahora=timezone.now() + timedelta(hours=2))
        self.assertEqual(self.pub.call_count, antes)
        self.assertEqual(self._orden(self.a, 'software', solicitud.id).estado, E.ENTREGADA)

    def test_estacion_viva_sin_acuse_recibe_el_mismo_payload_y_despues_no_entregable(self):
        solicitud = self._solicitud(self.a)
        t0 = timezone.now()
        publicar_solicitud(solicitud)
        orden = self._orden(self.a, 'software', solicitud.id)
        for vuelta in range(1, ordenes.MAX_ENTREGAS):
            ahora = t0 + timedelta(minutes=10 * vuelta)
            self._latido(self.a, ahora)
            ordenes.procesar(ahora=ahora)
        orden.refresh_from_db()
        self.assertEqual(orden.entregas, ordenes.MAX_ENTREGAS)
        self.assertEqual(set(self._payloads()), {orden.payload})  # siempre el mismo, firmado igual

        ahora = t0 + timedelta(minutes=10 * ordenes.MAX_ENTREGAS)
        self._latido(self.a, ahora)
        ordenes.procesar(ahora=ahora)
        orden.refresh_from_db()
        self.assertEqual(orden.estado, E.NO_ENTREGABLE)
        self.assertEqual(self._retiros(), [orden.topico])  # no puede ejecutarse al reconectar

    def test_reentrega_rechazada_no_cuenta_como_entrega(self):
        solicitud = self._solicitud(self.a)
        t0 = timezone.now()
        publicar_solicitud(solicitud)
        self.respuesta = lambda m: 'rechazado'
        ahora = t0 + timedelta(minutes=10)
        self._latido(self.a, ahora)
        ordenes.procesar(ahora=ahora)
        orden = self._orden(self.a, 'software', solicitud.id)
        self.assertEqual((orden.estado, orden.entregas), (E.ENTREGADA, 1))

    def test_perdida_de_conexion_despues_de_recibir(self):
        solicitud = self._solicitud(self.a)
        publicar_solicitud(solicitud)
        self._rep_software(self.a, solicitud, 'recibido')
        self._latido(self.a, timezone.now() - timedelta(hours=1))  # se apagó
        antes = self.pub.call_count
        ordenes.procesar(ahora=timezone.now() + timedelta(days=2))
        orden = self._orden(self.a, 'software', solicitud.id)
        self.assertEqual(orden.estado, E.RECIBIDA)  # ni interrumpida ni reenviada
        self.assertEqual(self.pub.call_count, antes)
        self._rep_software(self.a, solicitud, 'instalado')  # al volver termina
        orden.refresh_from_db()
        self.assertEqual(orden.estado, E.COMPLETADA)


class ReinicioTests(BaseCola):
    def test_reinicio_del_agente_durante_la_ejecucion_marca_interrumpida_sin_reenviar(self):
        solicitud = self._solicitud(self.a)
        publicar_solicitud(solicitud)
        self._rep_software(self.a, solicitud, 'recibido')
        self._rep_software(self.a, solicitud, 'descargado')
        despues = timezone.now() + timedelta(hours=7)
        self._latido(self.a, despues)  # la estación sigue viva pero la orden no avanza
        antes = self.pub.call_count
        ordenes.procesar(ahora=despues)
        orden = self._orden(self.a, 'software', solicitud.id)
        self.assertEqual(orden.estado, E.INTERRUMPIDA)
        self.assertEqual(self.pub.call_count, antes)

    def test_recuperacion_la_evidencia_tardia_corrige_la_interrumpida(self):
        solicitud = self._solicitud(self.a)
        publicar_solicitud(solicitud)
        self._rep_software(self.a, solicitud, 'recibido')
        despues = timezone.now() + timedelta(hours=7)
        self._latido(self.a, despues)
        ordenes.procesar(ahora=despues)
        self._rep_software(self.a, solicitud, 'instalado')
        orden = self._orden(self.a, 'software', solicitud.id)
        self.assertEqual(orden.estado, E.COMPLETADA)
        self.assertIn('evidencia tardía', orden.detalle)

    def test_reinicio_del_servidor_retoma_desde_la_base(self):
        """Todo el estado vive en la base: una pasada nueva despacha lo pendiente y no
        duplica lo ya entregado."""
        self._latido(self.a, timezone.now() - timedelta(hours=3))
        s1, s2 = self._solicitud(self.a, self.b), self._solicitud(self.a)
        publicar_solicitud(s1)
        publicar_solicitud(s2)
        # "Reinicio": se descartan los objetos en memoria y se procesa desde cero.
        antes = self.pub.call_count
        ordenes.procesar()
        self.assertEqual(self.pub.call_count, antes)  # A sigue con s1 entregada sin latir: s2 espera
        self.assertEqual(self._orden(self.a, 'software', s2.id).estado, E.PENDIENTE)
        OrdenAgente.objects.filter(estacion=self.a, origen_id=s1.id).update(estado=E.RECIBIDA)
        ordenes.procesar()
        self.assertEqual(self._orden(self.a, 'software', s2.id).estado, E.ENTREGADA)
        self.assertEqual(self.pub.call_count, antes + 1)


class CaducidadYCancelacionTests(BaseCola):
    def test_pendiente_vencida_caduca_y_nunca_se_publica(self):
        self._latido(self.a, timezone.now())
        s1, s2 = self._solicitud(self.a), self._solicitud(self.a)
        publicar_solicitud(s1)
        publicar_solicitud(s2)  # queda pendiente detrás de s1
        futuro = timezone.now() + ordenes.VIGENCIA + timedelta(hours=1)
        OrdenAgente.objects.filter(estacion=self.a, origen_id=s1.id).update(estado=E.RECIBIDA)
        antes = self.pub.call_count
        ordenes.procesar(ahora=futuro)
        self.assertEqual(self._orden(self.a, 'software', s2.id).estado, E.CADUCADA)
        self.assertEqual(self.pub.call_count, antes)

    def test_entregada_vencida_se_retira_del_broker_antes_de_caducar(self):
        solicitud = self._solicitud(self.a)
        publicar_solicitud(solicitud)
        ordenes.procesar(ahora=timezone.now() + ordenes.VIGENCIA + timedelta(hours=1))
        orden = self._orden(self.a, 'software', solicitud.id)
        self.assertEqual(orden.estado, E.CADUCADA)
        self.assertEqual(self._retiros(), [orden.topico])

    def test_si_no_se_puede_retirar_del_broker_no_se_da_por_caducada(self):
        solicitud = self._solicitud(self.a)
        publicar_solicitud(solicitud)
        self.respuesta = lambda m: 'rechazado' if m['payload'] == '' else 'aceptado'
        ordenes.procesar(ahora=timezone.now() + ordenes.VIGENCIA + timedelta(hours=1))
        self.assertEqual(self._orden(self.a, 'software', solicitud.id).estado, E.ENTREGADA)
        self.respuesta = lambda m: 'incierto'  # tampoco sin confirmación
        ordenes.procesar(ahora=timezone.now() + ordenes.VIGENCIA + timedelta(hours=2))
        self.assertEqual(self._orden(self.a, 'software', solicitud.id).estado, E.ENTREGADA)

    def test_una_caducada_no_se_ejecuta_al_reconectar(self):
        solicitud = self._solicitud(self.a)
        publicar_solicitud(solicitud)
        futuro = timezone.now() + ordenes.VIGENCIA + timedelta(hours=1)
        ordenes.procesar(ahora=futuro)
        antes = self.pub.call_count
        self._latido(self.a, futuro + timedelta(minutes=30))  # la estación vuelve
        ordenes.procesar(ahora=futuro + timedelta(minutes=30))
        self.assertEqual(self.pub.call_count, antes)  # ni reentrega ni despacho

    def test_cancelar_entregada_la_retira_y_despacha_la_siguiente(self):
        s1, s2 = self._solicitud(self.a), self._solicitud(self.a)
        publicar_solicitud(s1)
        publicar_solicitud(s2)
        ordenes.cancelar_orden(self._orden(self.a, 'software', s1.id), usuario=self.usuario)
        self.assertEqual(self._orden(self.a, 'software', s1.id).estado, E.CANCELADA)
        self.assertEqual(self._orden(self.a, 'software', s2.id).estado, E.ENTREGADA)

    def test_cancelar_pendiente(self):
        s1, s2 = self._solicitud(self.a), self._solicitud(self.a)
        publicar_solicitud(s1)
        publicar_solicitud(s2)
        ordenes.cancelar_orden(self._orden(self.a, 'software', s2.id))
        self.assertEqual(self._orden(self.a, 'software', s2.id).estado, E.CANCELADA)

    def test_no_se_cancela_una_recibida_ni_una_que_no_se_pudo_retirar(self):
        s1 = self._solicitud(self.a)
        publicar_solicitud(s1)
        for error in (PublicacionRechazada('Not authorized'), PublicacionIncierta('sin PUBACK')):
            self.v5.side_effect = error
            with self.subTest(error=error), self.assertRaises(ordenes.TransicionInvalida):
                ordenes.cancelar_orden(self._orden(self.a, 'software', s1.id))
            self.assertEqual(self._orden(self.a, 'software', s1.id).estado, E.ENTREGADA)
        self.v5.side_effect = None
        self._rep_software(self.a, s1, 'recibido')
        with self.assertRaises(ordenes.TransicionInvalida):
            ordenes.cancelar_orden(self._orden(self.a, 'software', s1.id))


class DespliegueTests(BaseCola):
    def test_error_durante_el_despliegue_y_rollback_dejan_la_orden_fallida(self):
        despliegue = self._despliegue(self.a)
        self.assertTrue(publicar_despliegue(despliegue).exitoso)
        for paso in ('recibido', 'descargado', 'pos_cerrado', 'error', 'rollback'):
            self._rep_despliegue(self.a, despliegue, paso, detalle='falló al aplicar')
        orden = self._orden(self.a, 'despliegue', despliegue.id)
        self.assertEqual(orden.estado, E.FALLIDA)
        self.assertEqual(
            ResultadoDespliegue.objects.get(despliegue=despliegue, estacion=self.a).estado,
            ResultadoDespliegue.Estado.ROLLBACK,
        )

    def test_reintentar_no_reenvia_a_la_estacion_que_lo_esta_aplicando(self):
        """Prevención de ejecución duplicada: un payload con timestamp nuevo el agente lo
        tomaría como orden nueva y volvería a cerrar el POS."""
        despliegue = self._despliegue(self.a, self.b)
        publicar_despliegue(despliegue)
        self._rep_despliegue(self.a, despliegue, 'recibido')
        self._rep_despliegue(self.a, despliegue, 'pos_cerrado')
        self._rep_despliegue(self.b, despliegue, 'error')
        self.pub.reset_mock()

        resultado = reintentar_despliegue(despliegue)
        self.assertTrue(resultado.exitoso)
        self.assertEqual(resultado.total_estaciones, 1)
        publicados = [m['topic'] for m in self.pub.call_args.args[0]]
        self.assertEqual(publicados, [f'/saidsof/agente/{self.b.codigo}/despliegue/'])
        self.assertEqual(
            ResultadoDespliegue.objects.get(despliegue=despliegue, estacion=self.a).estado,
            ResultadoDespliegue.Estado.APLICANDO,  # no se reseteó a pendiente
        )

    def test_recuperacion_una_interrumpida_se_resuelve_a_mano_antes_de_reintentar(self):
        """El reintento automático la omite: el agente puede seguir con ella y su reporte
        sería indistinguible del de una orden nueva. Después de verificar en la estación y
        resolverla, el reintento crea la orden nueva."""
        despliegue = self._despliegue(self.a)
        publicar_despliegue(despliegue)
        self._rep_despliegue(self.a, despliegue, 'recibido')
        despues = timezone.now() + timedelta(hours=7)
        self._latido(self.a, despues)
        ordenes.procesar(ahora=despues)
        interrumpida = self._orden(self.a, 'despliegue', despliegue.id)
        self.assertEqual(interrumpida.estado, E.INTERRUMPIDA)
        self.assertTrue(interrumpida.requiere_revision)

        self.assertEqual(reintentar_despliegue(despliegue).total_estaciones, 0)
        self.assertEqual(OrdenAgente.objects.filter(estacion=self.a, origen_id=despliegue.id).count(), 1)

        with self.assertRaises(ordenes.TransicionInvalida):
            ordenes.resolver_orden(interrumpida, resultado='fallida', usuario=self.usuario, nota='')
        ordenes.resolver_orden(interrumpida, resultado='fallida', usuario=self.usuario,
                               nota='POS en 4.2.0 en la caja; el paquete no se aplicó')
        interrumpida.refresh_from_db()
        self.assertEqual((interrumpida.estado, interrumpida.requiere_revision), (E.FALLIDA, False))
        self.assertNotIn('evidencia tardía', interrumpida.detalle)

        reintentar_despliegue(despliegue)
        ordenes_a = OrdenAgente.objects.filter(estacion=self.a, origen_id=despliegue.id).order_by('creada_en', 'pk')
        self.assertEqual([o.estado for o in ordenes_a], [E.FALLIDA, E.ENTREGADA])

    def test_modo_cierre_pos_no_se_da_por_interrumpido_antes_de_su_plazo(self):
        despliegue = self._despliegue(self.a)
        Despliegue.objects.filter(pk=despliegue.pk).update(modo_aplicacion=Despliegue.ModoAplicacion.CIERRE_POS)
        despliegue.refresh_from_db()
        publicar_despliegue(despliegue)
        self._rep_despliegue(self.a, despliegue, 'recibido')
        despues = timezone.now() + timedelta(hours=24)  # el POS todavía no se cerró
        self._latido(self.a, despues)
        ordenes.procesar(ahora=despues)
        self.assertEqual(self._orden(self.a, 'despliegue', despliegue.id).estado, E.RECIBIDA)


class ScriptTests(BaseCola):
    def _ejecucion(self):
        script = Script.objects.create(
            nombre='Diagnóstico', tipo=TipoScript.POWERSHELL, contenido='Get-Date', creado_por=self.usuario,
            unidad_negocio=self.sg,
        )
        return registrar_ejecucion_script(
            script=script, destino_tipo='estaciones', usuario=self.usuario, unidad_negocio=self.sg,
            estaciones=[self.a], omitir_aprobacion=True,
        )

    def test_el_script_queda_como_orden_no_repetible_y_sigue_sus_reportes(self):
        ejecucion = self._ejecucion()
        resultado = ResultadoEjecucionScript.objects.get(ejecucion=ejecucion)
        self.assertEqual(resultado.estado, ResultadoEjecucionScript.Estado.ENVIADO)
        orden = self._orden(self.a, 'script', resultado.pk)
        self.assertEqual((orden.estado, orden.repetible), (E.ENTREGADA, False))
        manejar_estado_script(self.a.codigo, {'token': self.a.token_enrolamiento, 'resultado_id': resultado.pk, 'estado': 'ejecutando'})
        manejar_estado_script(self.a.codigo, {'token': self.a.token_enrolamiento, 'resultado_id': resultado.pk, 'estado': 'completado', 'exit_code': 0})
        orden.refresh_from_db()
        self.assertEqual(orden.estado, E.COMPLETADA)

    def test_un_script_sin_reporte_nunca_se_reenvia(self):
        """Prevención de ejecución duplicada: el agente no deduplica scripts por id."""
        self._ejecucion()
        envios = self.v5.call_count
        despues = timezone.now() + timedelta(hours=2)
        self._latido(self.a, despues)
        ordenes.procesar(ahora=despues)
        orden = OrdenAgente.objects.get(estacion=self.a, tipo='script')
        self.assertEqual(orden.estado, E.NO_ENTREGABLE)
        self.assertIn('NO se reintenta', orden.detalle)
        self.assertEqual(self.v5.call_count, envios)
        self.assertFalse(self.pub.called)

    def test_si_el_broker_rechaza_el_script_queda_cancelado_y_no_se_reenvia(self):
        """Rechazo confirmado: es seguro que no salió. La orden se cancela (no requiere
        revisión: no hay nada incierto) y el resultado queda en ERROR, el contrato de siempre
        para "no se envió", con el motivo que lo distingue de un script que corrió y falló."""
        self.v5.side_effect = PublicacionRechazada('Not authorized')
        ejecucion = self._ejecucion()
        orden = OrdenAgente.objects.get(estacion=self.a, tipo='script')
        self.assertEqual((orden.estado, orden.pudo_llegar, orden.requiere_revision), (E.CANCELADA, False, False))
        resultado = ResultadoEjecucionScript.objects.get(ejecucion=ejecucion)
        self.assertEqual(resultado.estado, ResultadoEjecucionScript.Estado.ERROR)
        self.assertIn('No se envió', resultado.motivo_sin_respuesta)
        envios = self.v5.call_count
        ordenes.procesar(ahora=timezone.now() + timedelta(hours=1))
        self.assertEqual(self.v5.call_count, envios)


class CompatibilidadTests(BaseCola):
    def test_broker_caido_al_publicar_conserva_el_contrato_de_antes(self):
        self.fallo = SinConexion('sin broker')
        solicitud = self._solicitud(self.a)
        self.assertFalse(publicar_solicitud(solicitud).exitoso)
        solicitud.refresh_from_db()
        self.assertNotEqual(solicitud.estado, EstadoSolicitud.PUBLICANDO)
        self.assertEqual(self._orden(self.a, 'software', solicitud.id).estado, E.CANCELADA)
        self.fallo = None
        antes = self.pub.call_count
        ordenes.procesar()
        self.assertEqual(self.pub.call_count, antes)  # no queda nada que se publique solo después

    def test_reportes_de_publicaciones_anteriores_a_la_cola_se_procesan_igual(self):
        solicitud = self._solicitud(self.a)
        ResultadoInstalacion.objects.create(solicitud=solicitud, estacion=self.a)  # publicada sin cola
        self._rep_software(self.a, solicitud, 'instalado')
        self.assertFalse(OrdenAgente.objects.exists())
        self.assertEqual(
            ResultadoInstalacion.objects.get(solicitud=solicitud, estacion=self.a).estado,
            ResultadoInstalacion.Estado.INSTALADO,
        )

    def test_el_payload_publicado_es_el_mismo_contrato_firmado_de_antes(self):
        solicitud = self._solicitud(self.a)
        publicar_solicitud(solicitud)
        mensaje = self.pub.call_args.args[0][0]
        self.assertTrue(mensaje['retain'])
        payload = json.loads(mensaje['payload'])
        self.assertEqual(payload['solicitud_id'], solicitud.id)
        self.assertIn('firma', payload)
        self.assertNotIn('uid', payload)  # el agente no conoce la orden: su protocolo no cambia


class AceptacionDelBrokerTests(BaseCola):
    """`entregada` significa que EMQX ACEPTÓ el mensaje (PUBACK de MQTT 5 con código de
    éxito). Antes la orden se registraba `entregada` antes de publicar, y con MQTT 3.1.1 un
    rechazo de la ACL pasaba como éxito."""

    def test_publicacion_aceptada(self):
        solicitud = self._solicitud(self.a)
        self.assertTrue(publicar_solicitud(solicitud).exitoso)
        orden = self._orden(self.a, 'software', solicitud.id)
        self.assertEqual(orden.estado, E.ENTREGADA)
        self.assertEqual(self._estados(orden), [E.PENDIENTE, E.PUBLICANDO, E.ENTREGADA])
        self.assertIn('aceptada por el broker', orden.historial[-1]['detalle'])

    def test_rechazo_de_la_acl_en_todas_conserva_el_contrato_de_fallo(self):
        """EMQX responde "Not authorized" en el PUBACK: no quedó nada en el broker. Mismo
        contrato que un broker caído: `exitoso=False`, la solicitud no avanza y nada se
        publica solo después."""
        self.respuesta = lambda m: 'rechazado'
        solicitud = self._solicitud(self.a, self.b)
        self.assertFalse(publicar_solicitud(solicitud).exitoso)
        solicitud.refresh_from_db()
        self.assertNotEqual(solicitud.estado, EstadoSolicitud.PUBLICANDO)
        self.assertFalse(EventoInstalacion.objects.filter(paso=EventoInstalacion.Paso.PUBLICADO).exists())
        estados = set(OrdenAgente.objects.filter(origen_id=solicitud.id).values_list('estado', flat=True))
        self.assertEqual(estados, {E.CANCELADA})
        antes = self.pub.call_count
        ordenes.procesar(ahora=timezone.now() + timedelta(minutes=10))
        self.assertEqual(self.pub.call_count, antes)

    def test_rechazo_parcial_cancela_esa_orden_sin_reintento_automatico(self):
        """Un rechazo de autorización se repite igual: no se reintenta solo. Es seguro que no
        salió, así que la orden se cancela, el resultado de esa estación queda en error con el
        motivo, y la publicación informa la rechazada sin presentarla como publicada."""
        self.respuesta = lambda m: 'rechazado' if self.b.codigo in m['topic'] else 'aceptado'
        solicitud = self._solicitud(self.a, self.b)
        publicacion = publicar_solicitud(solicitud)
        self.assertTrue(publicacion.exitoso)
        self.assertFalse(publicacion.confirmada)
        self.assertEqual((publicacion.aceptadas, publicacion.rechazadas), (1, 1))
        oa, ob = self._orden(self.a, 'software', solicitud.id), self._orden(self.b, 'software', solicitud.id)
        self.assertEqual((oa.estado, ob.estado), (E.ENTREGADA, E.CANCELADA))
        self.assertFalse(ob.pudo_llegar)
        rb = ResultadoInstalacion.objects.get(solicitud=solicitud, estacion=self.b)
        self.assertEqual(rb.estado, ResultadoInstalacion.Estado.ERROR)
        self.assertIn('EMQX rechazó', rb.detalle_error)
        publicados = EventoInstalacion.objects.filter(paso=EventoInstalacion.Paso.PUBLICADO)
        self.assertEqual([e.resultado.estacion_id for e in publicados], [self.a.pk])
        antes = self.pub.call_count
        ordenes.procesar(ahora=timezone.now() + timedelta(minutes=10))
        self.assertEqual(self.pub.call_count, antes)
        self.assertEqual(OrdenAgente.objects.filter(estacion=self.b).count(), 1)

    def test_error_de_conexion_no_deja_nada_en_curso(self):
        for error in (SinConexion('connection refused'), ConnectionRefusedError('sin broker')):
            with self.subTest(error=error):
                self.fallo = error
                despliegue = self._despliegue(self.a)
                self.assertFalse(publicar_despliegue(despliegue).exitoso)
                self.assertEqual(self._orden(self.a, 'despliegue', despliegue.id).estado, E.CANCELADA)

    def test_corte_en_medio_de_la_publicacion_queda_publicando_y_se_recupera_con_el_mismo_payload(self):
        """Se cortó la conexión después de enviar y antes del PUBACK: no se sabe si EMQX lo
        tiene. No se registra como entregada ni como fallida, y la recuperación republica el
        MISMO payload firmado (el agente lo descarta si ya lo procesó)."""
        self.respuesta = lambda m: 'incierto'
        despliegue = self._despliegue(self.a)
        self.assertTrue(publicar_despliegue(despliegue).exitoso)
        orden = self._orden(self.a, 'despliegue', despliegue.id)
        self.assertEqual(orden.estado, E.PUBLICANDO)
        self.assertNotIn(E.ENTREGADA, self._estados(orden))

        ordenes.procesar(ahora=timezone.now() + timedelta(seconds=30))  # todavía dentro de la espera
        self.assertEqual(self.pub.call_count, 1)

        self.respuesta = lambda m: 'aceptado'
        ordenes.procesar(ahora=timezone.now() + ordenes.ESPERA_CONFIRMACION + timedelta(minutes=1))
        orden.refresh_from_db()
        self.assertEqual((orden.estado, orden.entregas), (E.ENTREGADA, 2))
        self.assertEqual(self._payloads(), [orden.payload, orden.payload])
        self.assertEqual(OrdenAgente.objects.filter(estacion=self.a, origen_id=despliegue.id).count(), 1)

    def test_sin_confirmacion_persistente_termina_no_entregable_y_retirada(self):
        self.respuesta = lambda m: 'incierto' if m['payload'] else 'aceptado'
        solicitud = self._solicitud(self.a)
        publicar_solicitud(solicitud)
        t = timezone.now()
        for vuelta in range(1, ordenes.MAX_ENTREGAS + 1):
            ordenes.procesar(ahora=t + vuelta * (ordenes.ESPERA_CONFIRMACION + timedelta(minutes=1)))
        orden = self._orden(self.a, 'software', solicitud.id)
        self.assertEqual(orden.estado, E.NO_ENTREGABLE)
        self.assertEqual(self._retiros(), [orden.topico])
        self.assertNotIn(E.ENTREGADA, self._estados(orden))

    def test_caida_del_proceso_entre_el_puback_y_el_guardado(self):
        """EMQX aceptó, pero el proceso murió antes de guardar el resultado. La orden queda
        `publicando` (reservada, sin afirmar nada) y la recuperación la republica idéntica:
        no se crea otra reserva ni otra orden."""
        solicitud = self._solicitud(self.a)
        with patch('apps.catalogo.ordenes.aplicar_resultados', side_effect=SystemExit('el proceso murió')):
            with self.assertRaises(SystemExit):
                publicar_solicitud(solicitud)
        orden = self._orden(self.a, 'software', solicitud.id)
        self.assertEqual((orden.estado, orden.entregas), (E.PUBLICANDO, 1))

        ordenes.procesar(ahora=timezone.now() + ordenes.ESPERA_CONFIRMACION + timedelta(minutes=1))
        orden.refresh_from_db()
        self.assertEqual(orden.estado, E.ENTREGADA)
        self.assertEqual(set(self._payloads()), {orden.payload})
        self.assertEqual(OrdenAgente.objects.filter(estacion=self.a).count(), 1)

    def test_caida_despues_de_reservar_y_antes_de_publicar(self):
        """La reserva (`publicando`) se guarda ANTES de publicar. Si el proceso muere en el
        medio, la orden no se pierde y no se afirma que el broker la tenga."""
        self.fallo = SystemExit('el proceso murió')  # no la atrapa `except Exception`
        solicitud = self._solicitud(self.a)
        with self.assertRaises(SystemExit):
            publicar_solicitud(solicitud)
        orden = self._orden(self.a, 'software', solicitud.id)
        self.assertEqual(orden.estado, E.PUBLICANDO)
        self.fallo = None
        ordenes.procesar(ahora=timezone.now() + ordenes.ESPERA_CONFIRMACION + timedelta(minutes=1))
        orden.refresh_from_db()
        self.assertEqual(orden.estado, E.ENTREGADA)
        self.assertEqual(self._payloads()[-1], orden.payload)

    def test_el_agente_reporta_antes_de_que_se_guarde_el_puback(self):
        """El reporte es evidencia: avanza la orden y libera la ranura. Un resultado del
        broker que llega después no la mueve para atrás."""
        solicitud = self._solicitud(self.a)
        with patch('apps.catalogo.ordenes.aplicar_resultados', side_effect=SystemExit):
            with self.assertRaises(SystemExit):
                publicar_solicitud(solicitud)
        self._rep_software(self.a, solicitud, 'recibido')
        orden = self._orden(self.a, 'software', solicitud.id)
        self.assertEqual(orden.estado, E.RECIBIDA)
        ordenes.aplicar_resultados([orden], ['aceptado'])
        ordenes.aplicar_resultados([orden], ['rechazado'])
        orden.refresh_from_db()
        self.assertEqual(orden.estado, E.RECIBIDA)

    def test_despacho_diferido_rechazado_se_cancela_y_queda_en_error(self):
        """Mismo criterio que en la publicación inicial: el rechazo no se reintenta solo, y
        como nadie está mirando en ese momento, queda registrado en el resultado de dominio."""
        self._latido(self.a, timezone.now() - timedelta(hours=3))
        s1, s2 = self._solicitud(self.a), self._solicitud(self.a)
        publicar_solicitud(s1)
        publicar_solicitud(s2)
        self.respuesta = lambda m: 'rechazado'
        self._rep_software(self.a, s1, 'recibido')  # libera la ranura y despacha s2
        o2 = self._orden(self.a, 'software', s2.id)
        self.assertEqual((o2.estado, o2.entregas, o2.pudo_llegar), (E.CANCELADA, 1, False))
        r2 = ResultadoInstalacion.objects.get(solicitud=s2, estacion=self.a)
        self.assertEqual(r2.estado, ResultadoInstalacion.Estado.ERROR)
        self.assertFalse(r2.eventos.filter(paso=EventoInstalacion.Paso.PUBLICADO).exists())

    def test_despacho_sin_conexion_devuelve_la_reserva(self):
        self._latido(self.a, timezone.now() - timedelta(hours=3))
        s1, s2 = self._solicitud(self.a), self._solicitud(self.a)
        publicar_solicitud(s1)
        publicar_solicitud(s2)
        self.fallo = SinConexion('sin broker')
        self._rep_software(self.a, s1, 'recibido')
        o2 = self._orden(self.a, 'software', s2.id)
        self.assertEqual((o2.estado, o2.entregas), (E.PENDIENTE, 0))
        self.fallo = None
        ordenes.procesar()
        o2.refresh_from_db()
        self.assertEqual(o2.estado, E.ENTREGADA)


class ScriptsInciertosTests(BaseCola):
    def _ejecucion(self):
        script = Script.objects.create(
            nombre='Diag', tipo=TipoScript.POWERSHELL, contenido='Get-Date', creado_por=self.usuario, unidad_negocio=self.sg,
        )
        return registrar_ejecucion_script(
            script=script, destino_tipo='estaciones', usuario=self.usuario, unidad_negocio=self.sg,
            estaciones=[self.a], omitir_aprobacion=True,
        )

    def test_sin_confirmacion_queda_para_revision_y_no_se_reenvia(self):
        self._ejecucion()
        envios = self.v5.call_count
        ordenes.procesar(ahora=timezone.now() + timedelta(hours=1))
        orden = OrdenAgente.objects.get(estacion=self.a, tipo='script')
        self.assertEqual(orden.estado, E.NO_ENTREGABLE)
        self.assertTrue(orden.requiere_revision)
        self.assertEqual(self.v5.call_count, envios)

    def test_publicacion_incierta_del_script_no_se_registra_como_enviada_ni_se_reenvia(self):
        """Salió sin PUBACK: puede ejecutarse. Ni enviado ni error en el dominio; la orden
        queda `publicando` y marcada, y pasado su plazo sin reportes, incierta."""
        self.v5.side_effect = PublicacionIncierta('se cortó antes del PUBACK')
        ejecucion = self._ejecucion()
        resultado = ResultadoEjecucionScript.objects.get(ejecucion=ejecucion)
        self.assertEqual(resultado.estado, ResultadoEjecucionScript.Estado.PENDIENTE)
        orden = OrdenAgente.objects.get(estacion=self.a, tipo='script')
        self.assertEqual((orden.estado, orden.requiere_revision), (E.PUBLICANDO, True))
        self.assertNotIn(E.ENTREGADA, self._estados(orden))
        envios = self.v5.call_count
        ordenes.procesar(ahora=timezone.now() + timedelta(minutes=5))
        ordenes.procesar(ahora=timezone.now() + timedelta(hours=1))
        orden.refresh_from_db()
        self.assertEqual(orden.estado, E.NO_ENTREGABLE)
        self.assertEqual(self.v5.call_count, envios)
        self.assertFalse(self.pub.called)

    def test_caida_despues_de_reservar_el_script_nunca_se_republica(self):
        orden = ordenes.crear_orden(
            estacion=self.a, tipo='script', origen_id=4242, topico='t', repetible=False, ronda=1, timeout_segundos=60,
        )
        self.assertTrue(ordenes.reservar_ranura(orden))  # y el proceso muere acá
        ordenes.procesar(ahora=timezone.now() + timedelta(hours=1))
        orden.refresh_from_db()
        self.assertEqual((orden.estado, orden.requiere_revision), (E.NO_ENTREGABLE, True))
        self.assertFalse(self.pub.called)
        self.assertFalse(self.v5.called)

    def test_empezo_y_no_termino_con_la_estacion_apagada_queda_interrumpido_para_revision(self):
        """Reinicio del agente o del worker en medio: el final nunca llega. Un script no se
        retoma, así que pasado su plazo queda incierto aunque la estación esté apagada."""
        ejecucion = self._ejecucion()
        resultado = ResultadoEjecucionScript.objects.get(ejecucion=ejecucion)
        manejar_estado_script(self.a.codigo, {'token': self.a.token_enrolamiento, 'resultado_id': resultado.pk, 'estado': 'ejecutando'})
        self._latido(self.a, timezone.now() - timedelta(hours=2))  # apagada
        envios = self.v5.call_count
        ordenes.procesar(ahora=timezone.now() + timedelta(hours=1))
        orden = OrdenAgente.objects.get(estacion=self.a, tipo='script')
        self.assertEqual((orden.estado, orden.requiere_revision), (E.INTERRUMPIDA, True))
        self.assertNotEqual(orden.estado, E.COMPLETADA)
        self.assertEqual(self.v5.call_count, envios)

    def test_si_otro_proceso_ocupa_la_ranura_durante_el_lote_el_indice_lo_detecta(self):
        """La inserción en lote decide "ranura libre" con una consulta previa. Si otro proceso
        reserva una orden en la misma ranura justo después, el índice único rechaza el lote y
        se sigue de a una: la orden nueva queda esperando, sin publicarse."""
        solicitud = self._solicitud(self.a)
        original = ordenes.plazos_de
        estado = {'hecho': False}

        def otro_proceso_se_adelanta(*args, **kwargs):
            if not estado['hecho']:
                estado['hecho'] = True
                ajena = ordenes.crear_orden(
                    estacion=self.a, tipo='software', origen_id=777, topico='t', payload_str='{}',
                    repetible=True, ronda=1,
                )
                OrdenAgente.objects.filter(pk=ajena.pk).update(estado=E.PUBLICANDO)
            return original(*args, **kwargs)

        with patch('apps.catalogo.ordenes.plazos_de', side_effect=otro_proceso_se_adelanta):
            self.assertTrue(publicar_solicitud(solicitud).exitoso)
        self.assertEqual(self._orden(self.a, 'software', solicitud.id).estado, E.PENDIENTE)
        self.assertEqual(OrdenAgente.objects.filter(estacion=self.a, estado__in=ordenes.OCUPAN_RANURA).count(), 1)
        self.assertFalse(self.pub.called)


class CorrelacionTests(BaseCola):
    """El agente reporta por despliegue_id / solicitud_id / resultado_id, no por orden. Con
    dos órdenes del MISMO origen a la misma estación (un reintento), un reporte no dice de
    cuál es."""

    def _interrumpir(self, despliegue):
        despues = timezone.now() + timedelta(hours=7)
        self._latido(self.a, despues)
        ordenes.procesar(ahora=despues)

    def test_reproduccion_un_reporte_atrasado_de_A_no_completa_a_B(self):
        despliegue = self._despliegue(self.a)
        publicar_despliegue(despliegue)                       # 1. se entrega A
        self._rep_despliegue(self.a, despliegue, 'recibido')
        self._interrumpir(despliegue)                         # 2. A queda interrumpida
        reintentar_despliegue(despliegue)                     # 3. ¿se entrega B?
        self._rep_despliegue(self.a, despliegue, 'ok', version_nueva='4.3.0')  # 4. llega el ok de A
        ordenes_a = list(OrdenAgente.objects.filter(estacion=self.a, origen_id=despliegue.id).order_by('creada_en', 'pk'))
        self.assertNotIn(E.COMPLETADA, [o.estado for o in ordenes_a[1:]],
                         'un reporte que puede ser de A no debe completar una orden posterior')

    def test_un_reporte_que_puede_ser_de_una_orden_cancelada_no_se_aplica_y_queda_para_revision(self):
        """Caso que sí puede ocurrir: A se canceló (o caducó) creyendo que el agente no la
        tenía, se publicó B del mismo origen, y llega un reporte. Puede ser de A —si el
        agente la recibió y su acuse se perdió— o de B. No se adivina."""
        solicitud = self._solicitud(self.a)
        publicar_solicitud(solicitud)                                   # A entregada
        ordenes.cancelar_orden(self._orden(self.a, 'software', solicitud.id), usuario=self.usuario)
        publicar_solicitud(solicitud)                                   # B entregada
        self._rep_software(self.a, solicitud, 'instalado')             # ¿de A o de B?
        a, b = OrdenAgente.objects.filter(estacion=self.a, origen_id=solicitud.id).order_by('creada_en', 'pk')
        self.assertEqual((a.estado, b.estado), (E.CANCELADA, E.ENTREGADA))
        self.assertTrue(b.requiere_revision)
        self.assertIn('ambiguo', b.motivo_revision)
        # El modelo de resultado del dominio se sigue actualizando como siempre: su contrato
        # no cambia (un ResultadoInstalacion por solicitud y estación).
        self.assertEqual(
            ResultadoInstalacion.objects.get(solicitud=solicitud, estacion=self.a).estado,
            ResultadoInstalacion.Estado.INSTALADO,
        )

    def test_sin_ordenes_previas_cerradas_por_el_servidor_la_correlacion_es_directa(self):
        s1, s2 = self._solicitud(self.a), self._solicitud(self.a)
        publicar_solicitud(s1)
        publicar_solicitud(s2)
        self._rep_software(self.a, s1, 'instalado')  # otro origen: no hay ambigüedad
        self.assertEqual(self._orden(self.a, 'software', s1.id).estado, E.COMPLETADA)
        self.assertFalse(self._orden(self.a, 'software', s2.id).requiere_revision)


class ResolucionManualTests(BaseCola):
    def _incierta(self):
        despliegue = self._despliegue(self.a)
        publicar_despliegue(despliegue)
        self._rep_despliegue(self.a, despliegue, 'recibido')
        despues = timezone.now() + timedelta(hours=7)
        self._latido(self.a, despues)
        ordenes.procesar(ahora=despues)
        return self._orden(self.a, 'despliegue', despliegue.id)

    def test_solo_se_resuelven_las_inciertas_o_marcadas(self):
        solicitud = self._solicitud(self.a)
        publicar_solicitud(solicitud)
        entregada = self._orden(self.a, 'software', solicitud.id)
        self.assertFalse(ordenes.puede_resolverse(entregada))
        with self.assertRaises(ordenes.TransicionInvalida):
            ordenes.resolver_orden(entregada, resultado='completada', usuario=self.usuario, nota='vi la caja')
        self.assertEqual(self._orden(self.a, 'software', solicitud.id).estado, E.ENTREGADA)

    def test_nota_y_resultado_obligatorios(self):
        orden = self._incierta()
        for resultado, nota in (('completada', '   '), ('cancelada', 'algo'), ('', 'algo')):
            with self.subTest(resultado=resultado, nota=nota), self.assertRaises(ordenes.TransicionInvalida):
                ordenes.resolver_orden(orden, resultado=resultado, usuario=self.usuario, nota=nota)
        orden.refresh_from_db()
        self.assertEqual(orden.estado, E.INTERRUMPIDA)

    def test_la_resolucion_queda_en_el_historial_con_quien_y_por_que(self):
        orden = self._incierta()
        ordenes.resolver_orden(orden, resultado='completada', usuario=self.usuario, nota='POS en 4.3.0 en la caja')
        orden.refresh_from_db()
        self.assertEqual((orden.estado, orden.requiere_revision), (E.COMPLETADA, False))
        self.assertIn('resuelta por operador: POS en 4.3.0 en la caja', orden.historial[-1]['detalle'])
        with self.assertRaises(ordenes.TransicionInvalida):  # una vez cerrada, no se vuelve a resolver
            ordenes.resolver_orden(orden, resultado='fallida', usuario=self.usuario, nota='otra')

    def test_publicando_sin_marca_no_se_resuelve_a_mano(self):
        """Una publicación sin confirmar de software o despliegue se recupera sola
        (republicando el mismo payload): no es un caso para cerrar a mano."""
        self.respuesta = lambda m: 'incierto'
        solicitud = self._solicitud(self.a)
        publicar_solicitud(solicitud)
        orden = self._orden(self.a, 'software', solicitud.id)
        self.assertEqual(orden.estado, E.PUBLICANDO)
        self.assertFalse(ordenes.puede_resolverse(orden))


class ResolucionDesdeElAdminTests(BaseCola):
    """`OrdenAgenteAdmin.resolver_view`: una orden a la vez, con permiso propio, dentro de la
    unidad de negocio del usuario, con nota obligatoria y evento de auditoría."""

    def setUp(self):
        super().setUp()
        from django.contrib.auth.models import Permission

        from apps.cuentas.models import PerfilUsuario

        self.orden = ResolucionManualTests._incierta(self)
        ver = Permission.objects.get(codename='view_ordenagente')
        resolver = Permission.objects.get(codename='resolver_ordenagente')
        self.tecnico = User.objects.create_user(username='tecnico', password='x', is_staff=True)
        self.tecnico.user_permissions.add(ver, resolver)
        PerfilUsuario.objects.create(usuario=self.tecnico).unidades_negocio.add(self.sg)
        self.solo_ve = User.objects.create_user(username='solo_ve', password='x', is_staff=True)
        self.solo_ve.user_permissions.add(ver)
        PerfilUsuario.objects.create(usuario=self.solo_ve).unidades_negocio.add(self.sg)
        self.otra_unidad = User.objects.create_user(username='otra_unidad', password='x', is_staff=True)
        self.otra_unidad.user_permissions.add(ver, resolver)
        PerfilUsuario.objects.create(usuario=self.otra_unidad).unidades_negocio.add(
            UnidadNegocio.objects.get(codigo='MIA'),
        )

    def _url(self, orden=None):
        from django.urls import reverse

        return reverse('admin:catalogo_ordenagente_resolver', args=[(orden or self.orden).pk])

    def _post(self, **datos):
        return self.client.post(self._url(), {'resultado': 'fallida', 'nota': 'POS en 4.2.0 en la caja', **datos})

    def test_muestra_el_formulario_con_el_historial(self):
        self.client.force_login(self.tecnico)
        resp = self.client.get(self._url())
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'Resolver orden incierta')
        self.assertContains(resp, 'interrumpida')

    def test_resuelve_y_deja_auditoria_con_usuario_decision_y_nota(self):
        from apps.auditoria.models import EventoAuditoria

        self.client.force_login(self.tecnico)
        resp = self._post()
        self.assertEqual(resp.status_code, 302)
        self.orden.refresh_from_db()
        self.assertEqual((self.orden.estado, self.orden.requiere_revision), (E.FALLIDA, False))
        evento = EventoAuditoria.objects.get(accion='orden_agente.resolver')
        self.assertEqual(evento.usuario, self.tecnico)
        self.assertEqual(evento.objeto_id, str(self.orden.pk))
        self.assertEqual(evento.detalle['decision'], 'fallida')
        self.assertEqual(evento.detalle['nota'], 'POS en 4.2.0 en la caja')
        self.assertEqual(evento.detalle['estado_anterior'], E.INTERRUMPIDA)
        self.assertIsNotNone(evento.timestamp)
        self.assertEqual(evento.unidad_negocio, self.sg)

    def test_sin_el_permiso_de_resolver_da_403_y_no_cambia_nada(self):
        self.client.force_login(self.solo_ve)
        self.assertEqual(self.client.get(self._url()).status_code, 403)
        self.assertEqual(self._post().status_code, 403)
        self.orden.refresh_from_db()
        self.assertEqual(self.orden.estado, E.INTERRUMPIDA)

    def test_otra_unidad_de_negocio_no_la_encuentra(self):
        self.client.force_login(self.otra_unidad)
        self.assertEqual(self._post().status_code, 404)
        self.orden.refresh_from_db()
        self.assertEqual(self.orden.estado, E.INTERRUMPIDA)

    def test_nota_vacia_o_corta_no_resuelve(self):
        from apps.auditoria.models import EventoAuditoria

        self.client.force_login(self.tecnico)
        for nota in ('', '   ', 'ok'):
            with self.subTest(nota=nota):
                resp = self._post(nota=nota)
                self.assertEqual(resp.status_code, 200)  # vuelve el formulario con el error
        resp = self._post(resultado='cancelada')
        self.assertEqual(resp.status_code, 200)
        self.orden.refresh_from_db()
        self.assertEqual(self.orden.estado, E.INTERRUMPIDA)
        self.assertFalse(EventoAuditoria.objects.filter(accion='orden_agente.resolver').exists())

    def test_una_orden_que_no_es_incierta_no_se_resuelve(self):
        solicitud = self._solicitud(self.a)
        publicar_solicitud(solicitud)
        entregada = self._orden(self.a, 'software', solicitud.id)
        self.client.force_login(self.tecnico)
        resp = self.client.post(self._url(entregada), {'resultado': 'completada', 'nota': 'la vi instalada en la caja'})
        self.assertEqual(resp.status_code, 302)
        entregada.refresh_from_db()
        self.assertEqual(entregada.estado, E.ENTREGADA)

    def test_resolver_dos_veces_la_segunda_no_hace_nada(self):
        from apps.auditoria.models import EventoAuditoria

        self.client.force_login(self.tecnico)
        self._post()
        self._post(resultado='completada', nota='otra persona, otra conclusión')
        self.orden.refresh_from_db()
        self.assertEqual(self.orden.estado, E.FALLIDA)
        self.assertEqual(EventoAuditoria.objects.filter(accion='orden_agente.resolver').count(), 1)

    # --- Coherencia: orden, resultado de dominio y auditoría juntos ---

    def test_fallida_actualiza_el_resultado_del_despliegue(self):
        self.client.force_login(self.tecnico)
        self._post()
        self.assertEqual(ResultadoDespliegue.objects.get(estacion=self.a).estado, ResultadoDespliegue.Estado.ERROR)

    def test_desconocido_registra_la_revision_sin_cambiar_nada_mas(self):
        from apps.auditoria.models import EventoAuditoria

        estado_dominio = ResultadoDespliegue.objects.get(estacion=self.a).estado
        self.client.force_login(self.tecnico)
        resp = self._post(resultado='desconocido', nota='sin acceso remoto a la caja hoy')
        self.assertEqual(resp.status_code, 302)
        self.orden.refresh_from_db()
        self.assertEqual((self.orden.estado, self.orden.requiere_revision), (E.INTERRUMPIDA, True))
        self.assertEqual(ResultadoDespliegue.objects.get(estacion=self.a).estado, estado_dominio)
        evento = EventoAuditoria.objects.get(accion='orden_agente.resolver')
        self.assertEqual((evento.detalle['decision'], evento.detalle['estado_anterior']), ('desconocido', E.INTERRUMPIDA))

    def test_si_la_auditoria_falla_no_queda_nada_a_medias(self):
        self.client.force_login(self.tecnico)
        estado_dominio = ResultadoDespliegue.objects.get(estacion=self.a).estado
        with patch('apps.catalogo.admin.registrar_evento', side_effect=RuntimeError('auditoría caída')):
            with self.assertRaises(RuntimeError):
                self._post()
        self.orden.refresh_from_db()
        self.assertEqual((self.orden.estado, self.orden.requiere_revision), (E.INTERRUMPIDA, True))
        self.assertEqual(ResultadoDespliegue.objects.get(estacion=self.a).estado, estado_dominio)


class RechazosPorTipoTests(BaseCola):
    """Reglas frente al broker, verificadas por tipo de orden y por caso. Ver el
    encabezado de apps/catalogo/ordenes.py."""

    def _script(self):
        script = Script.objects.create(
            nombre='Diag', tipo=TipoScript.POWERSHELL, contenido='Get-Date', creado_por=self.usuario, unidad_negocio=self.sg,
        )
        ejecucion = registrar_ejecucion_script(
            script=script, destino_tipo='estaciones', usuario=self.usuario, unidad_negocio=self.sg,
            estaciones=[self.a], omitir_aprobacion=True,
        )
        return ResultadoEjecucionScript.objects.get(ejecucion=ejecucion), OrdenAgente.objects.get(tipo='script')

    # --- Rechazo confirmado de la ACL ---

    def test_despliegue_rechazado_queda_en_error_y_se_puede_reintentar(self):
        self.respuesta = lambda m: 'rechazado' if self.b.codigo in m['topic'] else 'aceptado'
        despliegue = self._despliegue(self.a, self.b)
        publicacion = publicar_despliegue(despliegue)
        self.assertEqual((publicacion.exitoso, publicacion.rechazadas, publicacion.aceptadas), (True, 1, 1))
        ob = self._orden(self.b, 'despliegue', despliegue.id)
        self.assertEqual((ob.estado, ob.pudo_llegar), (E.CANCELADA, False))
        rb = ResultadoDespliegue.objects.get(despliegue=despliegue, estacion=self.b)
        self.assertEqual(rb.estado, ResultadoDespliegue.Estado.ERROR)
        # El freno cuenta solo las estaciones que ya salieron de pendiente: acá B es la única,
        # así que el despliegue se pausa, igual que con un error reportado por el agente.
        despliegue.refresh_from_db()
        self.assertEqual(despliegue.estado, Despliegue.Estado.PAUSADO)

        # Corregida la ACL, el reintento crea una orden nueva para B (la cancelada no bloquea,
        # porque es seguro que nunca salió) y su reporte se aplica sin ambigüedad.
        self.respuesta = lambda m: 'aceptado'
        reintento = reintentar_despliegue(despliegue)
        self.assertEqual((reintento.total_estaciones, reintento.aceptadas), (1, 1))
        nueva = self._orden(self.b, 'despliegue', despliegue.id)
        self.assertNotEqual(nueva.pk, ob.pk)
        self._rep_despliegue(self.b, despliegue, 'recibido')
        nueva.refresh_from_db()
        self.assertEqual((nueva.estado, nueva.requiere_revision), (E.RECIBIDA, False))

    def test_despliegue_rechazado_en_masa_activa_el_freno(self):
        self.respuesta = lambda m: 'rechazado' if self.b.codigo in m['topic'] else 'aceptado'
        despliegue = self._despliegue(self.a, self.b)
        Despliegue.objects.filter(pk=despliegue.pk).update(umbral_error_pct=50)
        despliegue.refresh_from_db()
        publicar_despliegue(despliegue)
        despliegue.refresh_from_db()
        self.assertEqual(despliegue.estado, Despliegue.Estado.PAUSADO)

    def test_script_rechazado_por_acl(self):
        self.v5.side_effect = PublicacionRechazada('Not authorized')
        resultado, orden = self._script()
        self.assertEqual((orden.estado, orden.pudo_llegar), (E.CANCELADA, False))
        self.assertEqual(resultado.estado, ResultadoEjecucionScript.Estado.ERROR)

    # --- Error de conexión antes de publicar ---

    def test_sin_conexion_al_publicar_software_y_despliegue(self):
        self.fallo = SinConexion('connection refused')
        solicitud, despliegue = self._solicitud(self.a), self._despliegue(self.b)
        self.assertFalse(publicar_solicitud(solicitud).exitoso)
        self.assertFalse(publicar_despliegue(despliegue).exitoso)
        for orden in (self._orden(self.a, 'software', solicitud.id), self._orden(self.b, 'despliegue', despliegue.id)):
            self.assertEqual((orden.estado, orden.pudo_llegar), (E.CANCELADA, False))
        # Contrato de siempre: nada registrado en el dominio, la operación no avanzó.
        self.assertEqual(ResultadoInstalacion.objects.get(solicitud=solicitud).estado,
                         ResultadoInstalacion.Estado.PENDIENTE)
        self.assertFalse(EventoInstalacion.objects.exists())

    def test_sin_conexion_al_publicar_un_script(self):
        self.v5.side_effect = SinConexion('connection refused')
        resultado, orden = self._script()
        self.assertEqual((orden.estado, orden.pudo_llegar), (E.CANCELADA, False))
        self.assertEqual(resultado.estado, ResultadoEjecucionScript.Estado.ERROR)

    def test_sin_conexion_en_la_recuperacion_sigue_publicando_y_no_cuenta_el_intento(self):
        self.respuesta = lambda m: 'incierto'
        solicitud = self._solicitud(self.a)
        publicar_solicitud(solicitud)
        self.fallo = SinConexion('sin broker')
        ordenes.procesar(ahora=timezone.now() + ordenes.ESPERA_CONFIRMACION + timedelta(minutes=1))
        orden = self._orden(self.a, 'software', solicitud.id)
        self.assertEqual((orden.estado, orden.entregas, orden.pudo_llegar), (E.PUBLICANDO, 1, True))

    # --- Timeout sin PUBACK ---

    def test_timeout_software_no_registra_publicado(self):
        self.respuesta = lambda m: 'incierto'
        solicitud = self._solicitud(self.a)
        publicacion = publicar_solicitud(solicitud)
        self.assertEqual((publicacion.exitoso, publicacion.confirmada, publicacion.sin_confirmar), (True, False, 1))
        self.assertIn('SIN confirmación', publicacion.resumen())
        orden = self._orden(self.a, 'software', solicitud.id)
        self.assertEqual((orden.estado, orden.pudo_llegar), (E.PUBLICANDO, True))
        self.assertFalse(EventoInstalacion.objects.filter(paso=EventoInstalacion.Paso.PUBLICADO).exists())
        self.assertEqual(ResultadoInstalacion.objects.get(solicitud=solicitud).estado,
                         ResultadoInstalacion.Estado.PENDIENTE)

    def test_timeout_script_queda_pendiente_y_el_barrido_lo_cierra_como_timeout_no_como_error(self):
        from apps.scripts.services import caducar_resultados_vencidos

        self.v5.side_effect = PublicacionIncierta('sin PUBACK')
        resultado, orden = self._script()
        self.assertEqual(resultado.estado, ResultadoEjecucionScript.Estado.PENDIENTE)
        self.assertIn('sin confirmación', resultado.motivo_sin_respuesta)
        self.assertEqual((orden.estado, orden.requiere_revision), (E.PUBLICANDO, True))
        ResultadoEjecucionScript.objects.filter(pk=resultado.pk).update(fecha_envio=timezone.now() - timedelta(hours=2))
        caducar_resultados_vencidos()
        resultado.refresh_from_db()
        self.assertEqual(resultado.estado, ResultadoEjecucionScript.Estado.TIMEOUT)

    # --- Recuperación de una publicación incierta ---

    def test_recuperacion_confirmada_registra_publicado_con_el_mismo_payload(self):
        self.respuesta = lambda m: 'incierto'
        despliegue = self._despliegue(self.a)
        publicar_despliegue(despliegue)
        self.respuesta = lambda m: 'aceptado'
        ordenes.procesar(ahora=timezone.now() + ordenes.ESPERA_CONFIRMACION + timedelta(minutes=1))
        orden = self._orden(self.a, 'despliegue', despliegue.id)
        self.assertEqual(orden.estado, E.ENTREGADA)
        self.assertEqual(set(self._payloads()), {orden.payload})
        evento = EventoDespliegue.objects.get(paso=EventoDespliegue.Paso.PUBLICADO)
        self.assertIn('al recuperarla', evento.detalle)

    def test_recuperacion_rechazada_no_se_cancela_porque_el_intento_anterior_pudo_quedar(self):
        self.respuesta = lambda m: 'incierto'
        solicitud = self._solicitud(self.a)
        publicar_solicitud(solicitud)
        self.respuesta = lambda m: 'rechazado'
        ordenes.procesar(ahora=timezone.now() + ordenes.ESPERA_CONFIRMACION + timedelta(minutes=1))
        orden = self._orden(self.a, 'software', solicitud.id)
        self.assertEqual((orden.estado, orden.requiere_revision), (E.NO_ENTREGABLE, True))
        # Incierto: no se afirma que falló.
        self.assertEqual(ResultadoInstalacion.objects.get(solicitud=solicitud).estado,
                         ResultadoInstalacion.Estado.PENDIENTE)

    def test_el_script_incierto_nunca_se_republica(self):
        self.v5.side_effect = PublicacionIncierta('sin PUBACK')
        _, orden = self._script()
        envios = self.v5.call_count
        for minutos in (3, 30, 120):
            ordenes.procesar(ahora=timezone.now() + timedelta(minutes=minutos))
        orden.refresh_from_db()
        self.assertEqual(orden.estado, E.NO_ENTREGABLE)
        self.assertEqual(self.v5.call_count, envios)
        self.assertFalse(self.pub.called)

    # --- Agotamiento de intentos ---

    def test_agotamiento_por_publicaciones_sin_confirmar(self):
        self.respuesta = lambda m: 'incierto' if m['payload'] else 'aceptado'
        despliegue = self._despliegue(self.a)
        publicar_despliegue(despliegue)
        t = timezone.now()
        for vuelta in range(1, ordenes.MAX_ENTREGAS + 1):
            ordenes.procesar(ahora=t + vuelta * (ordenes.ESPERA_CONFIRMACION + timedelta(minutes=1)))
        orden = self._orden(self.a, 'despliegue', despliegue.id)
        self.assertEqual((orden.estado, orden.entregas, orden.requiere_revision),
                         (E.NO_ENTREGABLE, ordenes.MAX_ENTREGAS, True))
        self.assertEqual(self._retiros(), [orden.topico])
        self.assertEqual(len(self._payloads()), ordenes.MAX_ENTREGAS)
        self.assertEqual(ResultadoDespliegue.objects.get(despliegue=despliegue).estado,
                         ResultadoDespliegue.Estado.PENDIENTE)  # incierto: no se afirma que falló
        self.assertEqual(reintentar_despliegue(despliegue).total_estaciones, 0)  # bloqueado hasta resolver


class PublicacionYEventosDeDominioTests(BaseCola):
    """El evento «publicado» del dominio se registra solo con evidencia de aceptación."""

    def test_en_espera_registra_publicado_recien_al_despacharse(self):
        self._latido(self.a, timezone.now() - timedelta(hours=3))
        s1, s2 = self._solicitud(self.a), self._solicitud(self.a)
        publicar_solicitud(s1)
        publicacion = publicar_solicitud(s2)
        self.assertEqual((publicacion.en_espera, publicacion.aceptadas), (1, 0))
        r2 = ResultadoInstalacion.objects.get(solicitud=s2)
        self.assertFalse(r2.eventos.filter(paso=EventoInstalacion.Paso.PUBLICADO).exists())
        self._rep_software(self.a, s1, 'recibido')  # libera la ranura: se despacha s2
        evento = r2.eventos.get(paso=EventoInstalacion.Paso.PUBLICADO)
        self.assertIn('Despachada desde la cola', evento.detalle)

    def test_reintento_de_despliegue_registra_republicado_solo_si_el_broker_acepta(self):
        despliegue = self._despliegue(self.a, self.b)
        publicar_despliegue(despliegue)
        for estacion in (self.a, self.b):
            self._rep_despliegue(estacion, despliegue, 'error')
        self.respuesta = lambda m: 'incierto' if self.b.codigo in m['topic'] else 'aceptado'
        reintento = reintentar_despliegue(despliegue)
        self.assertEqual((reintento.aceptadas, reintento.sin_confirmar, reintento.confirmada), (1, 1, False))
        republicados = EventoDespliegue.objects.filter(detalle='Republicado (reintento manual)')
        self.assertEqual([e.resultado.estacion_id for e in republicados], [self.a.pk])

    def test_la_vista_del_panel_no_presenta_lo_incierto_como_publicado(self):
        from django.contrib.auth.models import Permission
        from django.contrib.messages import get_messages
        from django.urls import reverse

        from apps.auditoria.models import EventoAuditoria
        from apps.cuentas.models import PerfilUsuario

        operador = User.objects.create_user(username='panel_op', password='x')
        operador.user_permissions.add(*Permission.objects.filter(
            content_type__app_label='software', codename__in=['change_solicitudinstalacion', 'view_solicitudinstalacion'],
        ))
        PerfilUsuario.objects.create(usuario=operador, acceso_todas_unidades=True)
        self.client.force_login(operador)
        self.respuesta = lambda m: 'incierto'
        solicitud = self._solicitud(self.a)
        resp = self.client.post(reverse('panel:solicitud_instalacion_publicar', args=[solicitud.pk]))
        mensajes = [(m.level_tag, str(m)) for m in get_messages(resp.wsgi_request)]
        self.assertEqual(len(mensajes), 1)
        self.assertEqual(mensajes[0][0], 'warning')
        self.assertIn('sin confirmar', mensajes[0][1])
        self.assertNotIn('publicada a', mensajes[0][1])
        evento = EventoAuditoria.objects.get(accion='solicitud_instalacion.publicar')
        self.assertEqual((evento.detalle['sin_confirmar'], evento.detalle['aceptadas']), (1, 0))


class ResolucionPorDominioTests(BaseCola):
    """La resolución manual mantiene coherentes la orden y el resultado de dominio, y no
    inventa un resultado cuando no se pudo determinar."""

    def _interrumpir(self, tipo):
        despues = timezone.now() + timedelta(hours=7)
        if tipo == 'software':
            origen = self._solicitud(self.a)
            publicar_solicitud(origen)
            self._rep_software(self.a, origen, 'recibido')
        else:
            origen = self._despliegue(self.a)
            publicar_despliegue(origen)
            self._rep_despliegue(self.a, origen, 'recibido')
        self._latido(self.a, despues)
        ordenes.procesar(ahora=despues)
        orden = self._orden(self.a, tipo, origen.id)
        self.assertEqual(orden.estado, E.INTERRUMPIDA)
        return origen, orden

    def _script_incierto(self):
        self.v5.side_effect = PublicacionIncierta('sin PUBACK')
        script = Script.objects.create(
            nombre='Diag', tipo=TipoScript.POWERSHELL, contenido='Get-Date', creado_por=self.usuario, unidad_negocio=self.sg,
        )
        ejecucion = registrar_ejecucion_script(
            script=script, destino_tipo='estaciones', usuario=self.usuario, unidad_negocio=self.sg,
            estaciones=[self.a], omitir_aprobacion=True,
        )
        self.v5.side_effect = None
        return ejecucion, OrdenAgente.objects.get(tipo='script')

    def _resolver(self, orden, decision, nota='verificado en la estación por acceso remoto'):
        return ordenes.resolver_orden(orden, resultado=decision, usuario=self.usuario, nota=nota)

    # --- Software ---

    def test_software_completada(self):
        solicitud, orden = self._interrumpir('software')
        self._resolver(orden, 'completada', nota='Chrome 128 instalado según el panel de programas')
        resultado = ResultadoInstalacion.objects.get(solicitud=solicitud)
        self.assertEqual(resultado.estado, ResultadoInstalacion.Estado.INSTALADO)
        evento = resultado.eventos.order_by('-pk').first()
        self.assertEqual(evento.paso, EventoInstalacion.Paso.INSTALADO)
        self.assertIn('Resolución manual de operador', evento.detalle)
        solicitud.refresh_from_db()
        self.assertEqual(solicitud.estado, EstadoSolicitud.COMPLETADO)

    def test_software_fallida(self):
        solicitud, orden = self._interrumpir('software')
        self._resolver(orden, 'fallida')
        self.assertEqual(ResultadoInstalacion.objects.get(solicitud=solicitud).estado,
                         ResultadoInstalacion.Estado.ERROR)
        orden.refresh_from_db()
        self.assertEqual(orden.estado, E.FALLIDA)
        # Desbloquea el origen: la cola acepta una orden nueva.
        cola = ordenes.encolar_y_publicar(
            tipo='software', origen_id=solicitud.id, envios=[(self.a, orden.topico, orden.payload)],
            publicar=lambda mensajes: ['aceptado'] * len(mensajes),
        )
        self.assertEqual((len(cola['aceptadas']), len(cola['inciertas'])), (1, 0))

    def test_software_desconocido_conserva_la_incertidumbre_y_el_bloqueo(self):
        solicitud, orden = self._interrumpir('software')
        antes = ResultadoInstalacion.objects.get(solicitud=solicitud)
        eventos_antes = antes.eventos.count()
        self._resolver(orden, 'desconocido', nota='la caja está apagada, no se pudo mirar')
        orden.refresh_from_db()
        self.assertEqual((orden.estado, orden.requiere_revision), (E.INTERRUMPIDA, True))
        self.assertIn('resultado desconocido', orden.motivo_revision)
        self.assertIn('no se pudo determinar', orden.historial[-1]['detalle'])
        despues = ResultadoInstalacion.objects.get(solicitud=solicitud)
        self.assertEqual((despues.estado, despues.eventos.count()), (antes.estado, eventos_antes))
        cola = ordenes.encolar_y_publicar(
            tipo='software', origen_id=solicitud.id, envios=[(self.a, orden.topico, orden.payload)],
            publicar=lambda mensajes: ['aceptado'] * len(mensajes),
        )
        self.assertEqual(len(cola['inciertas']), 1)  # sigue bloqueado
        self.assertTrue(ordenes.puede_resolverse(orden))  # se puede volver a revisar

    # --- Despliegues ---

    def test_despliegue_completada_no_se_reintenta_y_no_toca_la_version_del_pos(self):
        despliegue, orden = self._interrumpir('despliegue')
        version_antes = Estacion.objects.get(pk=self.a.pk).version_pos
        self._resolver(orden, 'completada', nota='POS en 4.3.0 en la caja')
        self.assertEqual(ResultadoDespliegue.objects.get(despliegue=despliegue).estado,
                         ResultadoDespliegue.Estado.APLICADO)
        self.assertEqual(Estacion.objects.get(pk=self.a.pk).version_pos, version_antes)
        self.assertEqual(reintentar_despliegue(despliegue).total_estaciones, 0)
        self.assertEqual(OrdenAgente.objects.filter(origen_id=despliegue.id, tipo='despliegue').count(), 1)

    def test_despliegue_fallida_se_puede_reintentar(self):
        despliegue, orden = self._interrumpir('despliegue')
        self._resolver(orden, 'fallida', nota='POS en 4.2.0: el paquete no se aplicó')
        self.assertEqual(ResultadoDespliegue.objects.get(despliegue=despliegue).estado,
                         ResultadoDespliegue.Estado.ERROR)
        reintento = reintentar_despliegue(despliegue)
        self.assertEqual((reintento.total_estaciones, reintento.aceptadas), (1, 1))
        estados = list(OrdenAgente.objects.filter(origen_id=despliegue.id, tipo='despliegue')
                       .order_by('creada_en', 'pk').values_list('estado', flat=True))
        self.assertEqual(estados, [E.FALLIDA, E.ENTREGADA])

    def test_despliegue_desconocido_no_se_reintenta(self):
        despliegue, orden = self._interrumpir('despliegue')
        self._resolver(orden, 'desconocido', nota='sin acceso remoto a la caja')
        self.assertNotIn(ResultadoDespliegue.objects.get(despliegue=despliegue).estado,
                         (ResultadoDespliegue.Estado.APLICADO, ResultadoDespliegue.Estado.ERROR))
        self.assertEqual(reintentar_despliegue(despliegue).total_estaciones, 0)

    # --- Scripts ---

    def test_script_completada_y_fallida(self):
        for decision, esperado in (('completada', ResultadoEjecucionScript.Estado.COMPLETADO),
                                   ('fallida', ResultadoEjecucionScript.Estado.ERROR)):
            with self.subTest(decision=decision):
                OrdenAgente.objects.all().delete()
                ejecucion, orden = self._script_incierto()
                envios = self.v5.call_count
                self._resolver(orden, decision, nota='revisé el log del agente en la estación')
                resultado = ResultadoEjecucionScript.objects.get(ejecucion=ejecucion)
                self.assertEqual(resultado.estado, esperado)
                self.assertIn('Resolución manual', resultado.motivo_sin_respuesta)
                self.assertIsNotNone(resultado.fecha_fin)
                ejecucion.refresh_from_db()
                self.assertNotEqual(ejecucion.estado, ejecucion.Estado.EN_PROGRESO)
                self.assertEqual(self.v5.call_count, envios)  # resolver nunca reenvía

    def test_script_desconocido_no_se_confunde_con_error(self):
        ejecucion, orden = self._script_incierto()
        self._resolver(orden, 'desconocido', nota='la estación no tiene log de esa hora')
        resultado = ResultadoEjecucionScript.objects.get(ejecucion=ejecucion)
        self.assertEqual(resultado.estado, ResultadoEjecucionScript.Estado.PENDIENTE)
        orden.refresh_from_db()
        self.assertEqual((orden.estado, orden.requiere_revision), (E.PUBLICANDO, True))

    # --- Atomicidad y broker ---

    def test_si_el_dominio_falla_la_orden_no_queda_resuelta(self):
        solicitud, orden = self._interrumpir('software')
        with patch('apps.software.services.orden_resuelta_a_mano', side_effect=RuntimeError('falla del dominio')):
            with self.assertRaises(RuntimeError):
                self._resolver(orden, 'completada')
        orden.refresh_from_db()
        self.assertEqual((orden.estado, orden.requiere_revision), (E.INTERRUMPIDA, True))
        self.assertNotEqual(ResultadoInstalacion.objects.get(solicitud=solicitud).estado,
                            ResultadoInstalacion.Estado.INSTALADO)

    def test_una_orden_que_puede_estar_retenida_se_retira_antes_de_resolverla(self):
        solicitud = self._solicitud(self.a)
        publicar_solicitud(solicitud)
        OrdenAgente.objects.filter(estacion=self.a).update(requiere_revision=True, motivo_revision='reporte ambiguo')
        orden = self._orden(self.a, 'software', solicitud.id)
        self.v5.side_effect = PublicacionIncierta('sin PUBACK')
        with self.assertRaises(ordenes.TransicionInvalida):
            self._resolver(orden, 'fallida')
        orden.refresh_from_db()
        self.assertEqual(orden.estado, E.ENTREGADA)
        self.v5.side_effect = None
        self._resolver(orden, 'fallida')
        orden.refresh_from_db()
        self.assertEqual(orden.estado, E.FALLIDA)
        self.assertEqual(self.v5.call_args.args[:2], (orden.topico, ''))


class AvisosSinResultadoDeDominioTests(BaseCola):
    """Una orden cuyo origen ya no existe (la solicitud se borró) no tumba el lote: el aviso
    al dominio la ignora y las demás se registran."""

    def test_el_despacho_sigue_con_una_orden_huerfana_en_el_lote(self):
        huerfana = ordenes.crear_orden(
            estacion=self.b, tipo='software', origen_id=987654, topico='/saidsof/agente/ML001-B/software/',
            payload_str='{}', repetible=True, ronda=1,
        )
        self._latido(self.a, timezone.now() - timedelta(hours=3))
        s1, s2 = self._solicitud(self.a), self._solicitud(self.a)
        publicar_solicitud(s1)
        publicar_solicitud(s2)
        OrdenAgente.objects.filter(estacion=self.a, origen_id=s1.id).update(estado=E.RECIBIDA)
        self.assertEqual(ordenes.despachar(), 2)
        huerfana.refresh_from_db()
        self.assertEqual(huerfana.estado, E.ENTREGADA)
        self.assertTrue(ResultadoInstalacion.objects.get(solicitud=s2).eventos
                        .filter(paso=EventoInstalacion.Paso.PUBLICADO).exists())
        self.assertFalse(ResultadoInstalacion.objects.filter(solicitud_id=987654).exists())


class ConsumidoresDeLaPublicacionTests(BaseCola):
    """Ningún consumidor de `ResultadoPublicacion` presenta lo incierto como publicado."""

    def test_accion_del_admin_no_cuenta_lo_incierto_como_publicado(self):
        from django.contrib.messages import get_messages
        from django.urls import reverse

        admin_user = User.objects.create_superuser(username='admin_pub', password='x')
        self.client.force_login(admin_user)
        self.respuesta = lambda m: 'incierto'
        solicitud = self._solicitud(self.a)
        resp = self.client.post(reverse('admin:software_solicitudinstalacion_changelist'), {
            'action': 'publicar_solicitudes', '_selected_action': [solicitud.pk],
        })
        mensajes = [(m.level_tag, str(m)) for m in get_messages(resp.wsgi_request)]
        self.assertEqual([nivel for nivel, _ in mensajes], ['warning'])
        self.assertIn('SIN confirmación', mensajes[0][1])
        self.assertFalse(any('publicada(s) por MQTT' in texto for _, texto in mensajes))

    def test_apertura_no_registra_enviado_sin_aceptacion_del_broker(self):
        from unittest.mock import MagicMock

        from apps.aperturas import services as aperturas

        paso = MagicMock()
        for publicacion, enviado in (
            (ordenes.ResultadoPublicacion(1, True, aceptadas=1), True),
            (ordenes.ResultadoPublicacion(1, True, sin_confirmar=1), False),
            (ordenes.ResultadoPublicacion(1, True, en_espera=1), False),
        ):
            with self.subTest(publicacion=publicacion), patch.object(aperturas, '_registrar') as registrar:
                aperturas._registrar_publicacion(paso, publicacion, 'Despliegue 4.3.0')
                self.assertEqual(registrar.called, enviado)
                if not enviado:
                    self.assertIn('sin confirmación de envío', paso.detalle)
