"""Reportes del agente con QoS 1 (0.38, E-01): el servidor tolera que lleguen repetidos.

QoS 1 es "al menos una vez": si se pierde el acuse, el mismo reporte llega dos veces. El
historial de eventos los acumula a propósito (decisión fijada en
`apps.despliegues.tests.IngestaDeEstadoDeDespliegueTests.test_repetir_el_paso_no_duplica_el_resultado`:
"la línea de tiempo SÍ acumula"). Lo que no puede pasar es que un repetido cambie el
resultado o la orden: eso es lo que fijan estas pruebas.
"""
from apps.catalogo import ordenes
from apps.catalogo.models import OrdenAgente
from apps.catalogo.tests_ordenes import BaseCola
from apps.despliegues.models import ResultadoDespliegue
from apps.despliegues.services import publicar_despliegue
from apps.software.models import ResultadoInstalacion
from apps.software.services import publicar_solicitud

E = OrdenAgente.Estado


class ReportesRepetidosTests(BaseCola):
    def test_pasos_de_instalacion_repetidos_no_cambian_el_resultado_ni_la_orden(self):
        solicitud = self._solicitud(self.a)
        publicar_solicitud(solicitud)
        for paso in ('recibido', 'recibido', 'descargado', 'descargado', 'instalado', 'instalado'):
            self._rep_software(self.a, solicitud, paso)
        resultado = ResultadoInstalacion.objects.get(solicitud=solicitud, estacion=self.a)
        self.assertEqual(resultado.estado, ResultadoInstalacion.Estado.INSTALADO)
        orden = ordenes.ultima_orden(self.a, 'software', solicitud.id)
        self.assertEqual(orden.estado, E.COMPLETADA)
        self.assertEqual([h['estado'] for h in orden.historial],
                         [E.PENDIENTE, E.PUBLICANDO, E.ENTREGADA, E.RECIBIDA, E.EN_EJECUCION, E.COMPLETADA])

    def test_un_ok_repetido_no_cambia_el_resultado_ni_la_orden(self):
        despliegue = self._despliegue(self.a)
        publicar_despliegue(despliegue)
        for paso in ('recibido', 'recibido', 'pos_cerrado', 'ok', 'ok'):
            self._rep_despliegue(self.a, despliegue, paso, version_nueva='4.3.0')
        resultado = ResultadoDespliegue.objects.get(despliegue=despliegue, estacion=self.a)
        self.assertEqual(resultado.estado, ResultadoDespliegue.Estado.APLICADO)
        self.assertEqual(ordenes.ultima_orden(self.a, 'despliegue', despliegue.id).estado, E.COMPLETADA)

    def test_un_error_repetido_despues_del_ok_no_lo_pisa(self):
        """Un reporte viejo repetido que llega tarde no puede deshacer un final."""
        despliegue = self._despliegue(self.a)
        publicar_despliegue(despliegue)
        for paso in ('recibido', 'ok', 'recibido'):
            self._rep_despliegue(self.a, despliegue, paso, version_nueva='4.3.0')
        self.assertEqual(ordenes.ultima_orden(self.a, 'despliegue', despliegue.id).estado, E.COMPLETADA)
