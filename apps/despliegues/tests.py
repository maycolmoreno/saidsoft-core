import importlib
import json
from unittest.mock import patch

from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import clear_url_caches, resolve
from django.views.static import serve

from apps.catalogo.models import Estacion, Farmacia, Grupo, UnidadNegocio
from apps.cuentas.models import PerfilUsuario

from .models import Despliegue, EventoDespliegue, ResultadoDespliegue
from .services import evaluar_freno_automatico, publicar_despliegue, reintentar_despliegue, verificar_completado


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


class UrlDescargaAgenteTests(_BaseDespliegueTests):
    """La URL que se le manda al agente para descargar el paquete.

    Los dos casos de abajo salieron del primer despliegue real del piloto: el agente
    reportaba "No se pudo descargar/verificar el paquete de ninguna fuente" sin más
    detalle, y el 404 subyacente no se veía en ninguna parte del panel.
    """

    def _url_publicada(self, despliegue):
        despliegue.estaciones.set([self._crear_estacion('ML001-A')])
        with patch('apps.despliegues.services.mqtt_publish.multiple') as mock_multiple:
            publicar_despliegue(despliegue)
        return json.loads(mock_multiple.call_args.args[0][0]['payload'])['url']

    @override_settings(ARCHIVOS_BASE_URL='http://10.0.0.1:8080')
    def test_url_absoluta_bien_formada(self):
        url = self._url_publicada(self._crear_despliegue())
        self.assertTrue(url.startswith('http://10.0.0.1:8080/media/'), url)
        self.assertNotIn('//media/', url)

    @override_settings(ARCHIVOS_BASE_URL='http://10.0.0.1:8080/')
    def test_barra_final_en_archivos_base_url_no_produce_doble_barra(self):
        # Con '//media/...' el patrón de URL no matchea y el agente recibe un 404.
        url = self._url_publicada(self._crear_despliegue())
        self.assertNotIn('//media/', url)
        self.assertTrue(url.startswith('http://10.0.0.1:8080/media/'), url)


class MediaServidoEnProduccionTests(TestCase):
    def test_media_se_sirve_con_debug_false(self):
        """static() devuelve [] con DEBUG=False: sin una ruta explícita nadie servía
        /media/ en producción y todas las descargas de los agentes daban 404.

        Hay que recargar el URLconf dentro del override: las rutas se arman una sola
        vez al importar config.urls, y la suite corre con DEBUG=True (donde static()
        SÍ agrega la ruta) — sin recargar, este test pasaría aunque el bug siguiera.
        """
        import config.urls
        try:
            with override_settings(DEBUG=False):
                importlib.reload(config.urls)
                clear_url_caches()
                coincidencia = resolve('/media/despliegues/x.zip')
                self.assertEqual(coincidencia.func, serve)
                self.assertEqual(coincidencia.kwargs['path'], 'despliegues/x.zip')
        finally:
            # Restaurar el URLconf con el DEBUG real para no afectar al resto de la suite.
            importlib.reload(config.urls)
            clear_url_caches()


class ReintentarDespliegueTests(_BaseDespliegueTests):
    """`despliegue_reanudar` antes solo cambiaba el estado a PUBLICANDO sin reenviar
    nada por MQTT — el operador "reanudaba" un despliegue que nunca reintentaba de
    verdad (encontrado en el primer despliegue real del piloto, 6-ago-2026)."""

    def test_republica_solo_a_estaciones_no_aplicadas(self):
        despliegue = self._crear_despliegue(estado=Despliegue.Estado.PAUSADO)
        ok = self._crear_estacion('ML001-OK')
        error = self._crear_estacion('ML001-ERROR')
        pendiente = self._crear_estacion('ML001-PEND')
        despliegue.estaciones.set([ok, error, pendiente])
        self._resultado(despliegue, ok, ResultadoDespliegue.Estado.APLICADO)
        self._resultado(despliegue, error, ResultadoDespliegue.Estado.ERROR)
        self._resultado(despliegue, pendiente, ResultadoDespliegue.Estado.PENDIENTE)

        with patch('apps.despliegues.services.mqtt_publish.multiple') as mock_multiple:
            resultado = reintentar_despliegue(despliegue)

        self.assertTrue(resultado.exitoso)
        self.assertEqual(resultado.total_estaciones, 2)  # error + pendiente, no la aplicada

        mensajes = mock_multiple.call_args.args[0]
        topicos = {m['topic'] for m in mensajes}
        # Tópico individual por estación, no el agregado de grupo/farmacia/cadena: así
        # no se le reenvía el paquete a ML001-OK, que ya lo aplicó con éxito.
        self.assertEqual(topicos, {'/saidsof/agente/ML001-ERROR/despliegue/', '/saidsof/agente/ML001-PEND/despliegue/'})

        # La que ya había fallado vuelve a PENDIENTE (para no arrastrar el error viejo
        # al próximo cálculo del freno automático); la ya aplicada queda intacta.
        self.assertEqual(
            despliegue.resultados.get(estacion=error).estado, ResultadoDespliegue.Estado.PENDIENTE,
        )
        self.assertEqual(
            despliegue.resultados.get(estacion=ok).estado, ResultadoDespliegue.Estado.APLICADO,
        )

    def test_nada_pendiente_no_publica(self):
        despliegue = self._crear_despliegue(estado=Despliegue.Estado.PAUSADO)
        ok = self._crear_estacion('ML001-OK')
        despliegue.estaciones.set([ok])
        self._resultado(despliegue, ok, ResultadoDespliegue.Estado.APLICADO)

        with patch('apps.despliegues.services.mqtt_publish.multiple') as mock_multiple:
            resultado = reintentar_despliegue(despliegue)

        mock_multiple.assert_not_called()
        self.assertTrue(resultado.exitoso)
        self.assertEqual(resultado.total_estaciones, 0)

    def test_broker_caido_no_resetea_estados(self):
        despliegue = self._crear_despliegue(estado=Despliegue.Estado.PAUSADO)
        error = self._crear_estacion('ML001-ERROR')
        despliegue.estaciones.set([error])
        self._resultado(despliegue, error, ResultadoDespliegue.Estado.ERROR)

        with patch('apps.despliegues.services.mqtt_publish.multiple', side_effect=OSError):
            resultado = reintentar_despliegue(despliegue)

        self.assertFalse(resultado.exitoso)
        # Si el publish falló, el estado de error se conserva tal cual — no hay que
        # mostrar "pendiente" para algo que en realidad nunca se reenvió.
        self.assertEqual(
            despliegue.resultados.get(estacion=error).estado, ResultadoDespliegue.Estado.ERROR,
        )


class DespliegueReanudarVistaTests(_BaseDespliegueTests):
    def setUp(self):
        super().setUp()
        self.aprobador = User.objects.create_user(username='aprobador', password='x')
        PerfilUsuario.objects.create(usuario=self.aprobador, acceso_todas_unidades=True)

    def test_reanudar_republica_y_marca_freno_omitido(self):
        despliegue = self._crear_despliegue(estado=Despliegue.Estado.PAUSADO)
        estacion = self._crear_estacion('ML001-A')
        despliegue.estaciones.set([estacion])
        self._resultado(despliegue, estacion, ResultadoDespliegue.Estado.ERROR)

        self.client.force_login(self.aprobador)
        with patch('apps.despliegues.services.mqtt_publish.multiple') as mock_multiple:
            response = self.client.post(f'/despliegues/{despliegue.pk}/reanudar/')

        self.assertEqual(response.status_code, 302)
        mock_multiple.assert_called_once()
        despliegue.refresh_from_db()
        self.assertEqual(despliegue.estado, Despliegue.Estado.PUBLICANDO)
        self.assertTrue(despliegue.freno_omitido)

    def test_reanudar_con_broker_caido_mantiene_pausado(self):
        despliegue = self._crear_despliegue(estado=Despliegue.Estado.PAUSADO)
        estacion = self._crear_estacion('ML001-A')
        despliegue.estaciones.set([estacion])
        self._resultado(despliegue, estacion, ResultadoDespliegue.Estado.ERROR)

        self.client.force_login(self.aprobador)
        with patch('apps.despliegues.services.mqtt_publish.multiple', side_effect=OSError):
            self.client.post(f'/despliegues/{despliegue.pk}/reanudar/')

        despliegue.refresh_from_db()
        self.assertEqual(despliegue.estado, Despliegue.Estado.PAUSADO)
        self.assertFalse(despliegue.freno_omitido)
