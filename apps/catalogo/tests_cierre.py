"""T-4: cerrar una instalación o un despliegue con estaciones sin respuesta, sin borrar nada,
y reintentar después solo las que no terminaron bien.

Escenario base: dos estaciones. A ya recibió la orden (la está ejecutando); B la tiene
entregada en el broker y todavía no respondió.
"""
from django.contrib.auth.models import Permission
from django.urls import reverse

from apps.auditoria.models import EventoAuditoria
from apps.cuentas.models import PerfilUsuario
from apps.catalogo.models import OrdenAgente
from apps.catalogo.tests_ordenes import BaseCola
from apps.despliegues.models import Despliegue, ResultadoDespliegue
from apps.despliegues.services import cerrar_despliegue, pausar_despliegue, publicar_despliegue
from apps.despliegues.services import reintentar_pendientes as reintentar_despliegue_cerrado
from apps.software.models import EstadoSolicitud, ResultadoInstalacion
from apps.software.services import cerrar_solicitud, publicar_solicitud
from apps.software.services import reintentar_pendientes as reintentar_solicitud_cerrada

E = OrdenAgente.Estado


class CierreDeSolicitudTests(BaseCola):
    def _en_curso(self):
        solicitud = self._solicitud(self.a, self.b)
        publicar_solicitud(solicitud)
        self._rep_software(self.a, solicitud, 'recibido')
        return solicitud

    def test_cierra_retira_lo_que_no_llego_y_deja_cada_resultado_como_estaba(self):
        solicitud = self._en_curso()
        antes = dict(solicitud.resultados.values_list('estacion__codigo', 'estado'))

        resumen = cerrar_solicitud(solicitud, usuario=self.usuario, motivo='B apagada hace días')

        self.assertEqual(solicitud.estado, EstadoSolicitud.CERRADO)
        self.assertEqual((solicitud.cerrado_por, solicitud.resumen_cierre['motivo']), (self.usuario, 'B apagada hace días'))
        self.assertIsNotNone(solicitud.cerrado_en)
        self.assertEqual(dict(solicitud.resultados.values_list('estacion__codigo', 'estado')), antes)
        self.assertEqual((resumen['ordenes_canceladas'], resumen['en_ejecucion_al_cerrar']), (1, 1))
        # B: retirada del broker y cancelada. A: la está ejecutando, sigue.
        self.assertEqual(self._orden(self.b, 'software', solicitud.id).estado, E.CANCELADA)
        self.assertIn(self._orden(self.b, 'software', solicitud.id).topico, self._retiros())
        self.assertEqual(self._orden(self.a, 'software', solicitud.id).estado, E.RECIBIDA)

    def test_lo_que_ya_corria_sigue_reportando_y_la_solicitud_sigue_cerrada(self):
        solicitud = self._en_curso()
        cerrar_solicitud(solicitud, usuario=self.usuario)
        self._rep_software(self.a, solicitud, 'instalado')
        solicitud.refresh_from_db()
        self.assertEqual(solicitud.estado, EstadoSolicitud.CERRADO)
        self.assertEqual(solicitud.resultados.get(estacion=self.a).estado, ResultadoInstalacion.Estado.INSTALADO)
        self.assertEqual(self._orden(self.a, 'software', solicitud.id).estado, E.COMPLETADA)

    def test_si_el_broker_no_acepta_el_retiro_no_se_cierra(self):
        solicitud = self._en_curso()
        self.respuesta = lambda m: 'rechazado' if m['payload'] == '' else 'aceptado'
        with self.assertRaisesMessage(ValueError, 'No se cerró'):
            cerrar_solicitud(solicitud, usuario=self.usuario)
        solicitud.refresh_from_db()
        self.assertEqual(solicitud.estado, EstadoSolicitud.PUBLICANDO)
        self.assertEqual(self._orden(self.b, 'software', solicitud.id).estado, E.ENTREGADA)

    def test_una_publicacion_en_vuelo_impide_cerrar(self):
        solicitud = self._en_curso()
        OrdenAgente.objects.filter(estacion=self.b).update(estado=E.PUBLICANDO)
        with self.assertRaisesMessage(ValueError, 'No se cerró'):
            cerrar_solicitud(solicitud, usuario=self.usuario)
        self.assertEqual(self._orden(self.b, 'software', solicitud.id).estado, E.PUBLICANDO)

    def test_una_orden_pendiente_detras_de_otra_se_cancela_sin_tocar_el_broker(self):
        ocupa = self._solicitud(self.b)
        publicar_solicitud(ocupa)                       # ocupa la ranura de B
        solicitud = self._solicitud(self.b)
        publicar_solicitud(solicitud)                   # queda pendiente detrás
        self.assertEqual(self._orden(self.b, 'software', solicitud.id).estado, E.PENDIENTE)
        retiros = len(self._retiros())
        cerrar_solicitud(solicitud, usuario=self.usuario)
        self.assertEqual(self._orden(self.b, 'software', solicitud.id).estado, E.CANCELADA)
        self.assertEqual(len(self._retiros()), retiros)
        self.assertEqual(self._orden(self.b, 'software', ocupa.id).estado, E.ENTREGADA)  # la otra, intacta

    def test_solo_se_cierra_una_en_curso(self):
        solicitud = self._solicitud(self.a)
        with self.assertRaisesMessage(ValueError, 'borrador'):
            cerrar_solicitud(solicitud, usuario=self.usuario)

    def test_reintentar_reenvia_solo_a_las_que_no_terminaron_bien(self):
        solicitud = self._en_curso()
        self._rep_software(self.a, solicitud, 'instalado')
        cerrar_solicitud(solicitud, usuario=self.usuario)
        orden_a = self._orden(self.a, 'software', solicitud.id)

        resultado = reintentar_solicitud_cerrada(solicitud)

        solicitud.refresh_from_db()
        self.assertTrue(resultado.exitoso)
        self.assertEqual(resultado.total_estaciones, 1)
        self.assertEqual(solicitud.estado, EstadoSolicitud.PUBLICANDO)
        self.assertEqual(self._orden(self.a, 'software', solicitud.id).pk, orden_a.pk)  # A no se toca
        nueva_b = self._orden(self.b, 'software', solicitud.id)
        self.assertEqual(nueva_b.estado, E.ENTREGADA)
        self.assertEqual(solicitud.resultados.get(estacion=self.b).estado, ResultadoInstalacion.Estado.PENDIENTE)

    def test_reintentar_con_el_broker_caido_la_deja_cerrada(self):
        solicitud = self._en_curso()
        cerrar_solicitud(solicitud, usuario=self.usuario)
        self.fallo = ConnectionError('broker caído')
        self.assertFalse(reintentar_solicitud_cerrada(solicitud).exitoso)
        solicitud.refresh_from_db()
        self.assertEqual(solicitud.estado, EstadoSolicitud.CERRADO)

    def test_reintentar_cuando_ya_terminaron_todas_la_completa(self):
        solicitud = self._en_curso()
        cerrar_solicitud(solicitud, usuario=self.usuario)
        ResultadoInstalacion.objects.filter(solicitud=solicitud).update(estado=ResultadoInstalacion.Estado.INSTALADO)
        self.assertEqual(reintentar_solicitud_cerrada(solicitud).total_estaciones, 0)
        solicitud.refresh_from_db()
        self.assertEqual(solicitud.estado, EstadoSolicitud.COMPLETADO)


class CierreDeDespliegueTests(BaseCola):
    def _en_curso(self):
        despliegue = self._despliegue(self.a, self.b)
        publicar_despliegue(despliegue)
        self._rep_despliegue(self.a, despliegue, 'recibido')
        despliegue.refresh_from_db()
        return despliegue

    def test_se_cierra_tambien_pausado_y_se_reintenta(self):
        despliegue = self._en_curso()
        pausar_despliegue(despliegue=despliegue)
        resumen = cerrar_despliegue(despliegue, usuario=self.usuario)
        self.assertEqual(despliegue.estado, Despliegue.Estado.CERRADO)
        self.assertEqual(resumen['ordenes_canceladas'], 1)
        self.assertEqual(self._orden(self.b, 'despliegue', despliegue.id).estado, E.CANCELADA)

        self._rep_despliegue(self.a, despliegue, 'ok', version_nueva='4.3.0')
        resultado = reintentar_despliegue_cerrado(despliegue)
        despliegue.refresh_from_db()
        self.assertTrue(resultado.exitoso)
        self.assertEqual(despliegue.estado, Despliegue.Estado.PUBLICANDO)
        self.assertEqual(self._orden(self.b, 'despliegue', despliegue.id).estado, E.ENTREGADA)
        self.assertEqual(despliegue.resultados.get(estacion=self.a).estado, ResultadoDespliegue.Estado.APLICADO)

    def test_si_el_broker_no_acepta_el_retiro_no_se_cierra(self):
        despliegue = self._en_curso()
        self.respuesta = lambda m: 'rechazado' if m['payload'] == '' else 'aceptado'
        with self.assertRaisesMessage(ValueError, 'No se cerró'):
            cerrar_despliegue(despliegue, usuario=self.usuario)
        despliegue.refresh_from_db()
        self.assertEqual(despliegue.estado, Despliegue.Estado.PUBLICANDO)


class VistasDeCierreTests(BaseCola):
    def _operador(self, *codenames):
        self.usuario.user_permissions.add(*Permission.objects.filter(codename__in=codenames))
        PerfilUsuario.objects.get_or_create(usuario=self.usuario, defaults={'acceso_todas_unidades': True})
        self.usuario.is_superuser = False
        self.usuario.save()
        self.client.force_login(self.usuario)

    def test_cerrar_y_reintentar_desde_el_panel_quedan_auditados(self):
        self._operador('view_solicitudinstalacion', 'change_solicitudinstalacion')
        solicitud = self._solicitud(self.a, self.b)
        publicar_solicitud(solicitud)
        respuesta = self.client.post(reverse('panel:solicitud_instalacion_cerrar', args=[solicitud.pk]),
                                     {'motivo': 'fin de la ventana'})
        self.assertRedirects(respuesta, reverse('panel:solicitud_instalacion_detalle', args=[solicitud.pk]),
                             fetch_redirect_response=False)
        solicitud.refresh_from_db()
        self.assertEqual(solicitud.estado, EstadoSolicitud.CERRADO)
        detalle = self.client.get(reverse('panel:solicitud_instalacion_detalle', args=[solicitud.pk]))
        self.assertContains(detalle, 'Reintentar pendientes')
        self.assertContains(detalle, 'fin de la ventana')

        self.client.post(reverse('panel:solicitud_instalacion_reintentar_pendientes', args=[solicitud.pk]))
        solicitud.refresh_from_db()
        self.assertEqual(solicitud.estado, EstadoSolicitud.PUBLICANDO)
        self.assertEqual(
            set(EventoAuditoria.objects.values_list('accion', flat=True)) & {
                'solicitud_instalacion.cerrar', 'solicitud_instalacion.reintentar_pendientes'},
            {'solicitud_instalacion.cerrar', 'solicitud_instalacion.reintentar_pendientes'},
        )

    def test_sin_permiso_de_cambio_no_se_cierra(self):
        self._operador('view_despliegue')
        despliegue = self._despliegue(self.a)
        publicar_despliegue(despliegue)
        respuesta = self.client.post(reverse('panel:despliegue_cerrar', args=[despliegue.pk]))
        self.assertEqual(respuesta.status_code, 403)
        despliegue.refresh_from_db()
        self.assertEqual(despliegue.estado, Despliegue.Estado.PUBLICANDO)

    def test_el_detalle_del_despliegue_ofrece_cerrar(self):
        self._operador('view_despliegue', 'change_despliegue')
        despliegue = self._despliegue(self.a)
        publicar_despliegue(despliegue)
        self.assertContains(self.client.get(reverse('panel:despliegue_detalle', args=[despliegue.pk])),
                            'Cerrar sin terminar')
