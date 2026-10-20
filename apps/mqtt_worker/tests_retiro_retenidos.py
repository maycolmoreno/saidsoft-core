"""Retiro de los retenidos de despliegues y de instalaciones de software.

Hasta ahora se publicaban retenidos y nadie los borraba: con el retainer en RAM los
borraba el próximo reinicio del broker; con el retainer en disco (R-6) quedarían para
siempre y se le volverían a entregar a una estación tras reinstalarle el agente.

Lo que fijan estas pruebas:
- el reporte de recepción o de resultado final encola el retiro, y la tarea publica un
  retenido vacío en el tópico de ESA estación;
- los pasos intermedios no publican nada;
- si la estación tiene otra orden sin confirmar, el retenido se conserva (podría ser esa);
- un rechazo de EMQX o un fallo al encolar no rompen el registro del reporte.

En desarrollo Celery corre en modo inmediato (`CELERY_TASK_ALWAYS_EAGER`), así que el
reporte ejecuta la tarea en el acto. `_enviar_mqtt_v5` se reemplaza: no hay broker.
"""
from unittest.mock import patch

from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase

from apps.catalogo.models import Estacion, Farmacia, Grupo, UnidadNegocio
from apps.catalogo.services import PublicacionRechazada
from apps.despliegues.models import Despliegue, EventoDespliegue, ResultadoDespliegue
from apps.despliegues.tasks import retirar_retenido_despliegue_task
from apps.mqtt_worker.services import manejar_estado_despliegue, manejar_estado_instalacion
from apps.software.models import (
    AplicacionCatalogo, DestinoTipo, EventoInstalacion, ResultadoInstalacion, SolicitudInstalacion,
    VersionAplicacion,
)
from apps.software.tasks import retirar_retenido_instalacion_task

ENVIAR = 'apps.catalogo.services._enviar_mqtt_v5'


def _publicaciones_vacias(mock):
    """(tópico, retain) de cada publicación de payload vacío, o sea de cada retiro."""
    return [(c.args[0], c.kwargs.get('retain')) for c in mock.call_args_list if c.args[1] == '']


class BaseRetenidos(TestCase):
    def setUp(self):
        self.sg = UnidadNegocio.objects.get(codigo='SG')
        grupo = Grupo.objects.create(codigo='TRX001')
        farmacia = Farmacia.objects.create(codigo='ML001', grupo=grupo, unidad_negocio=self.sg)
        self.estacion = Estacion.objects.create(
            codigo='ML001-A', farmacia=farmacia, estado_aprobacion=Estacion.EstadoAprobacion.APROBADA,
        )
        self.otra_estacion = Estacion.objects.create(
            codigo='ML001-B', farmacia=farmacia, estado_aprobacion=Estacion.EstadoAprobacion.APROBADA,
        )
        self.usuario = User.objects.create_user(username='creador', password='x')


class RetiroDespliegueTests(BaseRetenidos):
    TOPICO = '/saidsof/agente/ML001-A/despliegue/'

    def setUp(self):
        super().setUp()
        self.despliegue = self._despliegue('4.3.0')

    def _despliegue(self, version):
        return Despliegue.objects.create(
            version=version, archivo=SimpleUploadedFile('pkg.zip', b'x'),
            modo_aplicacion=Despliegue.ModoAplicacion.INMEDIATO, destino_tipo=Despliegue.DestinoTipo.ESTACIONES,
            unidad_negocio=self.sg, estado=Despliegue.Estado.PUBLICANDO, umbral_error_pct=50,
            creado_por=self.usuario,
        )

    def _reportar(self, paso, despliegue=None, token=None):
        with patch(ENVIAR) as enviar:
            manejar_estado_despliegue(self.estacion.codigo, {
                'token': token or self.estacion.token_enrolamiento,
                'despliegue_id': (despliegue or self.despliegue).id, 'paso': paso,
            })
        return enviar

    def test_al_recibirlo_se_retira_el_retenido_de_esa_estacion(self):
        enviar = self._reportar(EventoDespliegue.Paso.RECIBIDO)
        self.assertEqual(_publicaciones_vacias(enviar), [(self.TOPICO, True)])

    def test_los_resultados_finales_tambien_lo_retiran(self):
        for paso in (EventoDespliegue.Paso.OK, EventoDespliegue.Paso.ERROR, EventoDespliegue.Paso.ROLLBACK):
            with self.subTest(paso=paso):
                self.assertEqual(_publicaciones_vacias(self._reportar(paso)), [(self.TOPICO, True)])

    def test_los_pasos_intermedios_no_publican_nada(self):
        for paso in (EventoDespliegue.Paso.DESCARGADO, EventoDespliegue.Paso.HASH_VERIFICADO,
                     EventoDespliegue.Paso.POS_CERRADO, EventoDespliegue.Paso.APLICADO,
                     EventoDespliegue.Paso.POS_RELANZADO):
            with self.subTest(paso=paso):
                self.assertEqual(self._reportar(paso).call_count, 0)

    def test_si_hay_otro_despliegue_sin_confirmar_se_conserva(self):
        """El retenido es la ÚLTIMA orden publicada y podría ser la pendiente: borrarlo
        dejaría sin entregar un despliegue que la estación todavía no recibió."""
        otro = self._despliegue('4.4.0')
        ResultadoDespliegue.objects.create(
            despliegue=otro, estacion=self.estacion, estado=ResultadoDespliegue.Estado.PENDIENTE,
        )
        self.assertEqual(self._reportar(EventoDespliegue.Paso.OK).call_count, 0)

        # Cuando la estación confirma también ese, se retira.
        enviar = self._reportar(EventoDespliegue.Paso.RECIBIDO, despliegue=otro)
        self.assertEqual(_publicaciones_vacias(enviar), [(self.TOPICO, True)])

    def test_un_pendiente_de_otra_estacion_no_impide_el_retiro(self):
        ResultadoDespliegue.objects.create(
            despliegue=self.despliegue, estacion=self.otra_estacion, estado=ResultadoDespliegue.Estado.PENDIENTE,
        )
        self.assertEqual(_publicaciones_vacias(self._reportar(EventoDespliegue.Paso.OK)), [(self.TOPICO, True)])

    def test_un_rechazo_de_emqx_no_rompe_el_registro_del_reporte(self):
        with patch(ENVIAR, side_effect=PublicacionRechazada('Not authorized')), self.assertLogs(
            'apps.catalogo.services', level='ERROR',
        ):
            manejar_estado_despliegue(self.estacion.codigo, {
                'token': self.estacion.token_enrolamiento, 'despliegue_id': self.despliegue.id,
                'paso': EventoDespliegue.Paso.OK, 'version_nueva': '4.3.0',
            })
        resultado = ResultadoDespliegue.objects.get(despliegue=self.despliegue, estacion=self.estacion)
        self.assertEqual(resultado.estado, ResultadoDespliegue.Estado.APLICADO)

    def test_si_no_se_puede_encolar_el_reporte_igual_se_registra(self):
        with patch('apps.despliegues.tasks.retirar_retenido_despliegue_task.delay', side_effect=OSError('sin broker')), \
                self.assertLogs('apps.mqtt_worker.services', level='ERROR'):
            manejar_estado_despliegue(self.estacion.codigo, {
                'token': self.estacion.token_enrolamiento, 'despliegue_id': self.despliegue.id,
                'paso': EventoDespliegue.Paso.RECIBIDO,
            })
        self.assertTrue(ResultadoDespliegue.objects.filter(despliegue=self.despliegue, estacion=self.estacion).exists())

    def test_token_invalido_no_retira_nada(self):
        self.assertEqual(self._reportar(EventoDespliegue.Paso.OK, token='malo').call_count, 0)

    def test_la_tarea_con_una_estacion_inexistente_no_publica(self):
        with patch(ENVIAR) as enviar:
            retirar_retenido_despliegue_task(999999)
        self.assertEqual(enviar.call_count, 0)


class RetiroInstalacionTests(BaseRetenidos):
    TOPICO = '/saidsof/agente/ML001-A/software/'

    def setUp(self):
        super().setUp()
        aplicacion = AplicacionCatalogo.objects.create(nombre='Google Chrome', creado_por=self.usuario)
        self.version = VersionAplicacion.objects.create(
            aplicacion=aplicacion, version='128.0.0', instalador=SimpleUploadedFile('chrome.msi', b'x'),
            comando_instalacion_silenciosa='msiexec /i "{archivo}" /qn',
        )
        self.solicitud = self._solicitud()

    def _solicitud(self):
        return SolicitudInstalacion.objects.create(
            version_aplicacion=self.version, unidad_negocio=self.sg,
            destino_tipo=DestinoTipo.ESTACIONES, creado_por=self.usuario,
        )

    def _reportar(self, paso, solicitud=None):
        with patch(ENVIAR) as enviar:
            manejar_estado_instalacion(self.estacion.codigo, {
                'token': self.estacion.token_enrolamiento,
                'solicitud_id': (solicitud or self.solicitud).id, 'paso': paso,
            })
        return enviar

    def test_al_recibirla_se_retira_el_retenido_de_esa_estacion(self):
        self.assertEqual(_publicaciones_vacias(self._reportar(EventoInstalacion.Paso.RECIBIDO)), [(self.TOPICO, True)])

    def test_una_desinstalacion_lo_retira_con_instalando(self):
        """La desinstalación no reporta `recibido`: arranca directamente en `instalando`."""
        self.assertEqual(_publicaciones_vacias(self._reportar(EventoInstalacion.Paso.INSTALANDO)), [(self.TOPICO, True)])

    def test_los_resultados_finales_tambien_lo_retiran(self):
        for paso in (EventoInstalacion.Paso.INSTALADO, EventoInstalacion.Paso.ERROR):
            with self.subTest(paso=paso):
                self.assertEqual(_publicaciones_vacias(self._reportar(paso)), [(self.TOPICO, True)])

    def test_los_pasos_intermedios_no_publican_nada(self):
        for paso in (EventoInstalacion.Paso.DESCARGADO, EventoInstalacion.Paso.HASH_VERIFICADO):
            with self.subTest(paso=paso):
                self.assertEqual(self._reportar(paso).call_count, 0)

    def test_si_hay_otra_instalacion_sin_confirmar_se_conserva(self):
        otra = self._solicitud()
        ResultadoInstalacion.objects.create(
            solicitud=otra, estacion=self.estacion, estado=ResultadoInstalacion.Estado.PENDIENTE,
        )
        self.assertEqual(self._reportar(EventoInstalacion.Paso.INSTALADO).call_count, 0)
        enviar = self._reportar(EventoInstalacion.Paso.RECIBIDO, solicitud=otra)
        self.assertEqual(_publicaciones_vacias(enviar), [(self.TOPICO, True)])

    def test_un_rechazo_de_emqx_no_rompe_el_registro_del_reporte(self):
        with patch(ENVIAR, side_effect=PublicacionRechazada('Not authorized')), self.assertLogs(
            'apps.catalogo.services', level='ERROR',
        ):
            manejar_estado_instalacion(self.estacion.codigo, {
                'token': self.estacion.token_enrolamiento, 'solicitud_id': self.solicitud.id,
                'paso': EventoInstalacion.Paso.INSTALADO,
            })
        resultado = ResultadoInstalacion.objects.get(solicitud=self.solicitud, estacion=self.estacion)
        self.assertEqual(resultado.estado, ResultadoInstalacion.Estado.INSTALADO)

    def test_la_tarea_con_una_estacion_inexistente_no_publica(self):
        with patch(ENVIAR) as enviar:
            retirar_retenido_instalacion_task(999999)
        self.assertEqual(enviar.call_count, 0)
