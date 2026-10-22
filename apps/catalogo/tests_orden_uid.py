"""Identificador de orden en los reportes (agente 0.38).

El servidor agrega `orden_uid` a cada orden publicada, fuera de la firma, y el agente 0.38
lo devuelve en sus reportes. Con él, `registrar_reporte` asocia el reporte a ESA orden y
desaparece la ambigüedad entre dos órdenes del mismo origen. Sin él (agentes viejos), la
correlación sigue siendo por (estación, tipo, origen), y esas pruebas siguen en
`tests_ordenes`.
"""
import json
import uuid

from apps.catalogo import ordenes
from apps.catalogo.models import OrdenAgente
from apps.catalogo.services import firmar_payload, secreto_de
from apps.catalogo.tests_ordenes import BaseCola
from apps.despliegues.services import publicar_despliegue
from apps.mqtt_worker.services import manejar_estado_instalacion, manejar_estado_script
from apps.scripts.models import ResultadoEjecucionScript, Script, TipoScript
from apps.scripts.services import registrar_ejecucion_script
from apps.software.services import publicar_solicitud

E = OrdenAgente.Estado

# Los campos que firma `apps.software.services._payload` (y que el agente reconstruye).
CAMPOS_SOFTWARE = (
    'solicitud_id', 'aplicacion', 'version', 'accion', 'url', 'sha256', 'comando_instalacion_silenciosa',
    'comando_desinstalacion', 'argumentos_adicionales', 'comando_deteccion',
)


class OrdenUidTests(BaseCola):
    def _rep(self, solicitud, paso, orden_uid=None):
        cuerpo = {'token': self.a.token_enrolamiento, 'solicitud_id': solicitud.id, 'paso': paso}
        if orden_uid is not None:
            cuerpo['orden_uid'] = str(orden_uid)
        manejar_estado_instalacion(self.a.codigo, cuerpo)

    def _cancelada_y_nueva(self):
        """A se canceló creyendo que el agente no la tenía y se publicó B del mismo origen:
        el caso que, sin identificador, deja el reporte ambiguo."""
        solicitud = self._solicitud(self.a)
        publicar_solicitud(solicitud)
        ordenes.cancelar_orden(self._orden(self.a, 'software', solicitud.id), usuario=self.usuario)
        publicar_solicitud(solicitud)
        a, b = OrdenAgente.objects.filter(estacion=self.a, origen_id=solicitud.id).order_by('creada_en', 'pk')
        return solicitud, a, b

    def test_lo_publicado_lleva_el_uid_de_su_orden_y_la_firma_no_cambia(self):
        solicitud = self._solicitud(self.a)
        publicar_solicitud(solicitud)
        orden = self._orden(self.a, 'software', solicitud.id)
        publicado = json.loads(self._payloads()[-1])
        self.assertEqual(publicado['orden_uid'], str(orden.uid))
        self.assertEqual(json.loads(orden.payload)['orden_uid'], str(orden.uid))
        # Fuera de la firma: un agente viejo reconstruye los mismos campos y sigue validando.
        firma = firmar_payload(
            secreto_de(self.a), comando='instalar_software',
            **{c: publicado[c] for c in CAMPOS_SOFTWARE}, timestamp=publicado['timestamp'],
        )
        self.assertEqual(publicado['firma'], firma)

    def test_cada_orden_lleva_su_propio_uid(self):
        solicitud, a, b = self._cancelada_y_nueva()
        self.assertNotEqual(a.uid, b.uid)
        self.assertEqual(json.loads(a.payload)['orden_uid'], str(a.uid))
        self.assertEqual(json.loads(b.payload)['orden_uid'], str(b.uid))

    def test_el_despliegue_tambien_lo_lleva(self):
        despliegue = self._despliegue(self.a)
        publicar_despliegue(despliegue)
        orden = self._orden(self.a, 'despliegue', despliegue.id)
        self.assertEqual(json.loads(self._payloads()[-1])['orden_uid'], str(orden.uid))

    def test_con_el_uid_de_la_nueva_se_aplica_sin_ambiguedad(self):
        solicitud, a, b = self._cancelada_y_nueva()
        self._rep(solicitud, 'instalado', orden_uid=b.uid)
        a.refresh_from_db(); b.refresh_from_db()
        self.assertEqual((a.estado, b.estado), (E.CANCELADA, E.COMPLETADA))
        self.assertFalse(b.requiere_revision)

    def test_con_el_uid_de_la_anterior_va_a_esa_como_evidencia_y_la_nueva_no_se_toca(self):
        solicitud, a, b = self._cancelada_y_nueva()
        self._rep(solicitud, 'instalado', orden_uid=a.uid)
        a.refresh_from_db(); b.refresh_from_db()
        self.assertEqual(a.estado, E.COMPLETADA)
        self.assertIn('evidencia tardía', a.historial[-1]['detalle'])
        self.assertEqual((b.estado, b.requiere_revision), (E.ENTREGADA, False))

    def test_sin_uid_sigue_como_antes_ambiguo_y_para_revision(self):
        """Agente anterior a 0.38: nada cambia."""
        solicitud, a, b = self._cancelada_y_nueva()
        self._rep(solicitud, 'instalado')
        b.refresh_from_db()
        self.assertEqual(b.estado, E.ENTREGADA)
        self.assertTrue(b.requiere_revision)
        self.assertIn('ambiguo', b.motivo_revision)

    def test_un_uid_ajeno_o_mal_formado_se_ignora_y_se_correlaciona_por_el_origen(self):
        otra = self._solicitud(self.b)  # el uid de una orden de OTRA estación
        publicar_solicitud(otra)
        ajeno = self._orden(self.b, 'software', otra.id).uid
        for uid in (ajeno, uuid.uuid4(), 'no-es-un-uuid'):
            with self.subTest(uid=uid):
                solicitud, a, b = self._cancelada_y_nueva()
                self._rep(solicitud, 'instalado', orden_uid=uid)
                b.refresh_from_db()
                self.assertEqual((b.estado, b.requiere_revision), (E.ENTREGADA, True))
                ordenes.cancelar_orden(b, usuario=self.usuario)  # libera la ranura para la vuelta siguiente
        self.assertEqual(self._orden(self.b, 'software', otra.id).estado, E.ENTREGADA)

    def test_reenviar_el_payload_de_otra_orden_le_pone_el_uid_nuevo(self):
        """`encolar_y_publicar` puede recibir un payload que ya traía uid (p. ej. el de una
        orden anterior): la orden nueva lo reemplaza por el suyo."""
        solicitud = self._solicitud(self.a)
        publicar_solicitud(solicitud)
        vieja = self._orden(self.a, 'software', solicitud.id)
        ordenes.cancelar_orden(vieja, usuario=self.usuario)
        ordenes.encolar_y_publicar(
            tipo='software', origen_id=solicitud.id, envios=[(self.a, vieja.topico, vieja.payload)],
            publicar=lambda mensajes: ['aceptado'] * len(mensajes),
        )
        nueva = self._orden(self.a, 'software', solicitud.id)
        self.assertNotEqual(nueva.pk, vieja.pk)
        self.assertEqual(json.loads(nueva.payload)['orden_uid'], str(nueva.uid))


class OrdenUidScriptTests(BaseCola):
    def test_el_script_lleva_el_uid_y_el_reporte_lo_usa(self):
        script = Script.objects.create(
            nombre='Diag', tipo=TipoScript.POWERSHELL, contenido='Get-Date', creado_por=self.usuario,
            unidad_negocio=self.sg,
        )
        ejecucion = registrar_ejecucion_script(
            script=script, destino_tipo='estaciones', usuario=self.usuario, unidad_negocio=self.sg,
            estaciones=[self.a], omitir_aprobacion=True,
        )
        resultado = ResultadoEjecucionScript.objects.get(ejecucion=ejecucion)
        orden = self._orden(self.a, 'script', resultado.pk)
        publicado = json.loads(self.v5.call_args.args[1])
        self.assertEqual(publicado['orden_uid'], str(orden.uid))
        manejar_estado_script(self.a.codigo, {
            'token': self.a.token_enrolamiento, 'resultado_id': resultado.pk, 'estado': 'completado',
            'exit_code': 0, 'orden_uid': str(orden.uid),
        })
        orden.refresh_from_db()
        self.assertEqual(orden.estado, E.COMPLETADA)
