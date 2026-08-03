from unittest.mock import patch

from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase

from apps.catalogo.models import Estacion, Farmacia, Grupo, UnidadNegocio

from .models import (
    AplicacionCatalogo, DestinoTipo, EstadoSolicitud, EventoInstalacion, ResultadoInstalacion, SolicitudInstalacion,
    TipoAccionInstalacion, VersionAplicacion,
)
from .services import publicar_solicitud, verificar_completado


class _BaseSoftwareTests(TestCase):
    def setUp(self):
        self.sg = UnidadNegocio.objects.get(codigo='SG')
        self.grupo = Grupo.objects.create(codigo='TRX001')
        self.farmacia = Farmacia.objects.create(codigo='ML001', grupo=self.grupo, unidad_negocio=self.sg)
        self.usuario = User.objects.create_user(username='creador', password='x')
        self.aplicacion = AplicacionCatalogo.objects.create(nombre='Google Chrome', creado_por=self.usuario)
        self.version = VersionAplicacion.objects.create(
            aplicacion=self.aplicacion, version='128.0.0',
            instalador=SimpleUploadedFile('chrome.msi', b'contenido-falso'),
            comando_instalacion_silenciosa='msiexec /i "{archivo}" /qn',
        )

    def _crear_estacion(self, codigo):
        return Estacion.objects.create(
            codigo=codigo, farmacia=self.farmacia, estado_aprobacion=Estacion.EstadoAprobacion.APROBADA,
        )

    def _crear_solicitud(self, **kwargs):
        defaults = dict(
            version_aplicacion=self.version, accion=TipoAccionInstalacion.INSTALAR,
            unidad_negocio=self.sg, destino_tipo=DestinoTipo.ESTACIONES,
            estado=EstadoSolicitud.BORRADOR, creado_por=self.usuario,
        )
        defaults.update(kwargs)
        return SolicitudInstalacion.objects.create(**defaults)


class VersionAplicacionTests(_BaseSoftwareTests):
    def test_calcula_sha256_y_tamanio_al_guardar(self):
        self.assertTrue(self.version.sha256)
        self.assertEqual(self.version.tamanio_bytes, len(b'contenido-falso'))

    def test_no_recalcula_el_hash_si_ya_existe(self):
        hash_original = self.version.sha256
        self.version.comando_desinstalacion = 'msiexec /x {archivo} /qn'
        self.version.save()
        self.assertEqual(self.version.sha256, hash_original)


class PublicarSolicitudTests(_BaseSoftwareTests):
    def test_publicacion_exitosa_usa_una_sola_llamada_a_multiple_y_avanza_estado(self):
        solicitud = self._crear_solicitud()
        solicitud.estaciones.set([self._crear_estacion(f'ML001-{i}') for i in range(3)])

        with patch('apps.software.services.mqtt_publish.multiple') as mock_multiple:
            resultado = publicar_solicitud(solicitud)

        mock_multiple.assert_called_once()
        mensajes = mock_multiple.call_args.args[0]
        self.assertEqual(len(mensajes), 3)

        self.assertTrue(resultado.exitoso)
        self.assertEqual(resultado.total_estaciones, 3)

        solicitud.refresh_from_db()
        self.assertEqual(solicitud.estado, EstadoSolicitud.PUBLICANDO)
        self.assertIsNotNone(solicitud.fecha_publicacion)
        self.assertEqual(EventoInstalacion.objects.filter(paso=EventoInstalacion.Paso.PUBLICADO).count(), 3)

    def test_broker_caido_no_avanza_estado_ni_registra_evento_publicado(self):
        solicitud = self._crear_solicitud()
        solicitud.estaciones.set([self._crear_estacion('ML001-A')])

        with patch('apps.software.services.mqtt_publish.multiple', side_effect=ConnectionRefusedError('sin broker')):
            resultado = publicar_solicitud(solicitud)

        self.assertFalse(resultado.exitoso)
        solicitud.refresh_from_db()
        self.assertEqual(solicitud.estado, EstadoSolicitud.BORRADOR)
        self.assertFalse(EventoInstalacion.objects.filter(paso=EventoInstalacion.Paso.PUBLICADO).exists())

    def test_no_devuelve_estaciones_de_otro_tenant_aunque_el_grupo_sea_compartido(self):
        mia = UnidadNegocio.objects.get(codigo='MIA')
        farmacia_mia = Farmacia.objects.create(codigo='MAM01', grupo=self.grupo, unidad_negocio=mia)
        Estacion.objects.create(
            codigo='MAM01-A', farmacia=farmacia_mia, estado_aprobacion=Estacion.EstadoAprobacion.APROBADA,
        )
        self._crear_estacion('ML001-A')
        solicitud = self._crear_solicitud(destino_tipo=DestinoTipo.GRUPOS)
        solicitud.grupos.set([self.grupo])

        with patch('apps.software.services.mqtt_publish.multiple'):
            resultado = publicar_solicitud(solicitud)

        self.assertEqual(resultado.total_estaciones, 1)  # solo ML001-A, nunca MAM01-A


class VerificarCompletadoTests(_BaseSoftwareTests):
    def _resultado(self, solicitud, estacion, estado):
        return ResultadoInstalacion.objects.create(solicitud=solicitud, estacion=estacion, estado=estado)

    def test_marca_completado_cuando_no_quedan_pendientes_ni_en_curso(self):
        solicitud = self._crear_solicitud(estado=EstadoSolicitud.PUBLICANDO)
        self._resultado(solicitud, self._crear_estacion('ML001-A'), ResultadoInstalacion.Estado.INSTALADO)
        self._resultado(solicitud, self._crear_estacion('ML001-B'), ResultadoInstalacion.Estado.ERROR)

        self.assertTrue(verificar_completado(solicitud))
        solicitud.refresh_from_db()
        self.assertEqual(solicitud.estado, EstadoSolicitud.COMPLETADO)

    def test_no_completa_si_hay_resultados_en_curso(self):
        solicitud = self._crear_solicitud(estado=EstadoSolicitud.PUBLICANDO)
        self._resultado(solicitud, self._crear_estacion('ML001-A'), ResultadoInstalacion.Estado.INSTALADO)
        self._resultado(solicitud, self._crear_estacion('ML001-B'), ResultadoInstalacion.Estado.DESCARGANDO)

        self.assertFalse(verificar_completado(solicitud))
        solicitud.refresh_from_db()
        self.assertEqual(solicitud.estado, EstadoSolicitud.PUBLICANDO)
