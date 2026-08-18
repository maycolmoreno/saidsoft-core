from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import IntegrityError, transaction
from django.test import TestCase
from django.utils import timezone

from apps.catalogo.models import Estacion, Farmacia, Grupo, UnidadNegocio

from .models import (
    AplicacionCatalogo, DestinoTipo, EstadoSolicitud, EventoInstalacion, InventarioProgramado,
    ResultadoInstalacion, SoftwareInstaladoDetectado, SolicitudInstalacion, TipoAccionInstalacion,
    VersionAplicacion,
)
from .services import (
    generar_escaneo_programado, generar_escaneos_vencidos, publicar_solicitud, verificar_completado,
)


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


class SoftwareInstaladoDetectadoTests(_BaseSoftwareTests):
    def test_no_permite_dos_filas_del_mismo_nombre_en_la_misma_estacion(self):
        estacion = self._crear_estacion('ML001-A')
        SoftwareInstaladoDetectado.objects.create(estacion=estacion, nombre='Google Chrome', version='118.0')
        with self.assertRaises(IntegrityError), transaction.atomic():
            SoftwareInstaladoDetectado.objects.create(estacion=estacion, nombre='Google Chrome', version='119.0')

    def test_el_mismo_nombre_en_otra_estacion_si_es_valido(self):
        e1 = self._crear_estacion('ML001-A')
        e2 = self._crear_estacion('ML001-B')
        SoftwareInstaladoDetectado.objects.create(estacion=e1, nombre='Google Chrome', version='118.0')
        SoftwareInstaladoDetectado.objects.create(estacion=e2, nombre='Google Chrome', version='118.0')
        self.assertEqual(SoftwareInstaladoDetectado.objects.count(), 2)


class GenerarEscaneoProgramadoTests(_BaseSoftwareTests):
    def setUp(self):
        super().setUp()
        self.estacion = self._crear_estacion('ML001-A')
        self.programado = InventarioProgramado.objects.create(
            unidad_negocio=self.sg, destino_tipo=DestinoTipo.CADENA,
            frecuencia_dias=7, fecha_proxima_ejecucion=timezone.now().date(), creado_por=self.usuario,
        )

    def test_dispara_el_comando_y_avanza_fechas(self):
        hoy = timezone.now().date()
        with patch('apps.catalogo.services.enviar_comando', return_value=True) as mock_enviar:
            enviados = generar_escaneo_programado(programado=self.programado)

        mock_enviar.assert_called_once_with(self.estacion, 'consultar_software_instalado')
        self.assertEqual(enviados, 1)
        self.programado.refresh_from_db()
        self.assertEqual(self.programado.fecha_ultima_ejecucion, hoy)
        self.assertEqual(self.programado.fecha_proxima_ejecucion, hoy + timedelta(days=7))

    def test_fallo_de_publish_no_cuenta_como_enviado_pero_igual_avanza_fecha(self):
        with patch('apps.catalogo.services.enviar_comando', return_value=False):
            enviados = generar_escaneo_programado(programado=self.programado)
        self.assertEqual(enviados, 0)
        self.programado.refresh_from_db()
        self.assertIsNotNone(self.programado.fecha_ultima_ejecucion)

    def test_generar_escaneos_vencidos_solo_recoge_las_vencidas_y_audita(self):
        from apps.auditoria.models import EventoAuditoria

        futuro = InventarioProgramado.objects.create(
            unidad_negocio=self.sg, destino_tipo=DestinoTipo.CADENA,
            frecuencia_dias=7, fecha_proxima_ejecucion=timezone.now().date() + timedelta(days=5),
            creado_por=self.usuario,
        )
        with patch('apps.catalogo.services.enviar_comando', return_value=True):
            total = generar_escaneos_vencidos()

        self.assertEqual(total, 1)
        futuro.refresh_from_db()
        self.assertIsNone(futuro.fecha_ultima_ejecucion)
        self.assertTrue(EventoAuditoria.objects.filter(accion='inventario_programado.disparar').exists())

    def test_inactivo_no_se_dispara(self):
        self.programado.activo = False
        self.programado.save(update_fields=['activo'])
        with patch('apps.catalogo.services.enviar_comando', return_value=True) as mock_enviar:
            total = generar_escaneos_vencidos()
        self.assertEqual(total, 0)
        mock_enviar.assert_not_called()
