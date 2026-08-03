from unittest.mock import patch

from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase

from apps.catalogo.models import Estacion, Farmacia, Grupo, UnidadNegocio

from .models import Despliegue, EventoDespliegue, ResultadoDespliegue
from .services import evaluar_freno_automatico, publicar_despliegue, verificar_completado


class _BaseDespliegueTests(TestCase):
    def setUp(self):
        self.sg = UnidadNegocio.objects.get(codigo='SG')
        self.grupo = Grupo.objects.create(codigo='TRX001')
        self.farmacia = Farmacia.objects.create(codigo='ML001', grupo=self.grupo, unidad_negocio=self.sg)
        self.usuario = User.objects.create_user(username='creador', password='x')

    def _crear_despliegue(self, **kwargs):
        defaults = dict(
            version='1.0.0',
            archivo=SimpleUploadedFile('pkg.zip', b'contenido-falso'),
            modo_aplicacion=Despliegue.ModoAplicacion.INMEDIATO,
            unidad_negocio=self.sg,
            destino_tipo=Despliegue.DestinoTipo.ESTACIONES,
            estado=Despliegue.Estado.APROBADO,
            creado_por=self.usuario,
        )
        defaults.update(kwargs)
        return Despliegue.objects.create(**defaults)

    def _crear_estacion(self, codigo):
        return Estacion.objects.create(
            codigo=codigo, farmacia=self.farmacia, estado_aprobacion=Estacion.EstadoAprobacion.APROBADA,
        )

    def _resultado(self, despliegue, estacion, estado):
        return ResultadoDespliegue.objects.create(despliegue=despliegue, estacion=estacion, estado=estado)


class PublicarDespliegueTests(_BaseDespliegueTests):
    def test_publicacion_exitosa_usa_una_sola_llamada_a_multiple_y_avanza_estado(self):
        despliegue = self._crear_despliegue()
        despliegue.estaciones.set([self._crear_estacion(f'ML001-{i}') for i in range(3)])

        with patch('apps.despliegues.services.mqtt_publish.multiple') as mock_multiple:
            resultado = publicar_despliegue(despliegue)

        mock_multiple.assert_called_once()
        mensajes = mock_multiple.call_args.args[0]
        self.assertEqual(len(mensajes), 3)  # un tópico por estación, una sola conexión (no publish.single x3)

        self.assertTrue(resultado.exitoso)
        self.assertEqual(resultado.total_estaciones, 3)

        despliegue.refresh_from_db()
        self.assertEqual(despliegue.estado, Despliegue.Estado.PUBLICANDO)
        self.assertIsNotNone(despliegue.fecha_publicacion)
        self.assertEqual(EventoDespliegue.objects.filter(paso=EventoDespliegue.Paso.PUBLICADO).count(), 3)

    def test_broker_caido_no_avanza_estado_ni_registra_evento_publicado(self):
        despliegue = self._crear_despliegue()
        despliegue.estaciones.set([self._crear_estacion('ML001-A')])

        with patch(
            'apps.despliegues.services.mqtt_publish.multiple', side_effect=ConnectionRefusedError('sin broker'),
        ):
            resultado = publicar_despliegue(despliegue)

        self.assertFalse(resultado.exitoso)
        self.assertEqual(resultado.total_estaciones, 1)

        despliegue.refresh_from_db()
        self.assertEqual(despliegue.estado, Despliegue.Estado.APROBADO)  # no avanzó
        self.assertIsNone(despliegue.fecha_publicacion)
        self.assertFalse(EventoDespliegue.objects.filter(paso=EventoDespliegue.Paso.PUBLICADO).exists())

    def test_publicacion_crea_resultado_pendiente_por_estacion_incluso_si_falla_el_broker(self):
        despliegue = self._crear_despliegue()
        despliegue.estaciones.set([self._crear_estacion('ML001-A')])

        with patch('apps.despliegues.services.mqtt_publish.multiple', side_effect=OSError):
            publicar_despliegue(despliegue)

        # Los ResultadoDespliegue se crean antes de intentar publicar: si el broker vuelve
        # y alguien reintenta, la fila ya existe en PENDIENTE en vez de tener que recrearla.
        self.assertEqual(
            ResultadoDespliegue.objects.filter(
                despliegue=despliegue, estado=ResultadoDespliegue.Estado.PENDIENTE,
            ).count(),
            1,
        )


class FrenoAutomaticoTests(_BaseDespliegueTests):
    def test_no_frena_si_nadie_ha_reportado_todavia(self):
        despliegue = self._crear_despliegue(estado=Despliegue.Estado.PUBLICANDO, umbral_error_pct=10)
        for i in range(5):
            self._resultado(despliegue, self._crear_estacion(f'ML001-{i}'), ResultadoDespliegue.Estado.PENDIENTE)

        self.assertFalse(evaluar_freno_automatico(despliegue))
        despliegue.refresh_from_db()
        self.assertEqual(despliegue.estado, Despliegue.Estado.PUBLICANDO)

    def test_frena_por_error_alto_entre_los_que_ya_reportaron_aunque_la_mayoria_siga_pendiente(self):
        """Antes del fix, el denominador incluía las 8 PENDIENTE y 2/10=20% no cruzaba
        el umbral de 50%; con el fix, el denominador son solo las que ya reportaron
        (2/2=100%) y sí frena — exactamente el escenario que motivó el cambio."""
        despliegue = self._crear_despliegue(estado=Despliegue.Estado.PUBLICANDO, umbral_error_pct=50)
        self._resultado(despliegue, self._crear_estacion('ML001-A'), ResultadoDespliegue.Estado.ERROR)
        self._resultado(despliegue, self._crear_estacion('ML001-B'), ResultadoDespliegue.Estado.ERROR)
        for i in range(8):
            self._resultado(despliegue, self._crear_estacion(f'ML001-P{i}'), ResultadoDespliegue.Estado.PENDIENTE)

        self.assertTrue(evaluar_freno_automatico(despliegue))
        despliegue.refresh_from_db()
        self.assertEqual(despliegue.estado, Despliegue.Estado.PAUSADO)

    def test_no_frena_bajo_el_umbral(self):
        despliegue = self._crear_despliegue(estado=Despliegue.Estado.PUBLICANDO, umbral_error_pct=50)
        self._resultado(despliegue, self._crear_estacion('ML001-A'), ResultadoDespliegue.Estado.ERROR)
        self._resultado(despliegue, self._crear_estacion('ML001-B'), ResultadoDespliegue.Estado.APLICADO)
        self._resultado(despliegue, self._crear_estacion('ML001-C'), ResultadoDespliegue.Estado.APLICADO)

        self.assertFalse(evaluar_freno_automatico(despliegue))

    def test_freno_omitido_no_vuelve_a_pausar(self):
        despliegue = self._crear_despliegue(
            estado=Despliegue.Estado.PUBLICANDO, umbral_error_pct=10, freno_omitido=True,
        )
        self._resultado(despliegue, self._crear_estacion('ML001-A'), ResultadoDespliegue.Estado.ERROR)

        self.assertFalse(evaluar_freno_automatico(despliegue))
        despliegue.refresh_from_db()
        self.assertEqual(despliegue.estado, Despliegue.Estado.PUBLICANDO)

    def test_no_actua_si_el_despliegue_no_esta_publicando(self):
        despliegue = self._crear_despliegue(estado=Despliegue.Estado.APROBADO, umbral_error_pct=10)
        self._resultado(despliegue, self._crear_estacion('ML001-A'), ResultadoDespliegue.Estado.ERROR)

        self.assertFalse(evaluar_freno_automatico(despliegue))
        despliegue.refresh_from_db()
        self.assertEqual(despliegue.estado, Despliegue.Estado.APROBADO)


class VerificarCompletadoTests(_BaseDespliegueTests):
    def test_marca_completado_cuando_no_quedan_pendientes_ni_en_curso(self):
        despliegue = self._crear_despliegue(estado=Despliegue.Estado.PUBLICANDO)
        self._resultado(despliegue, self._crear_estacion('ML001-A'), ResultadoDespliegue.Estado.APLICADO)
        self._resultado(despliegue, self._crear_estacion('ML001-B'), ResultadoDespliegue.Estado.ERROR)

        self.assertTrue(verificar_completado(despliegue))
        despliegue.refresh_from_db()
        self.assertEqual(despliegue.estado, Despliegue.Estado.COMPLETADO)

    def test_no_completa_si_hay_resultados_en_curso(self):
        despliegue = self._crear_despliegue(estado=Despliegue.Estado.PUBLICANDO)
        self._resultado(despliegue, self._crear_estacion('ML001-A'), ResultadoDespliegue.Estado.APLICADO)
        self._resultado(despliegue, self._crear_estacion('ML001-B'), ResultadoDespliegue.Estado.DESCARGANDO)

        self.assertFalse(verificar_completado(despliegue))
        despliegue.refresh_from_db()
        self.assertEqual(despliegue.estado, Despliegue.Estado.PUBLICANDO)
