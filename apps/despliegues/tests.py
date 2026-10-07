import importlib
import json
from unittest.mock import patch

from django.conf import settings
from django.contrib.auth.models import Permission, User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.http import Http404
from django.test import TestCase, override_settings
from django.urls import clear_url_caches, resolve

from apps.catalogo.models import Estacion, Farmacia, Grupo, UnidadNegocio
from apps.cuentas.models import PerfilUsuario

from apps.catalogo.services import firmar_payload

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

    def _crear_estacion(self, codigo, version='', hmac_propio=False):
        # `hmac_propio` es lo que habilita la firma con el secreto propio, no la version:
        # una estacion actualizada a 0.21 que se enrolo antes nunca recibio el secreto.
        # Ver apps.catalogo.services.secreto_de.
        return Estacion.objects.create(
            codigo=codigo, farmacia=self.farmacia, estado_aprobacion=Estacion.EstadoAprobacion.APROBADA,
            version_agente=version, hmac_propio_confirmado=hmac_propio,
        )

    def _resultado(self, despliegue, estacion, estado):
        return ResultadoDespliegue.objects.create(despliegue=despliegue, estacion=estacion, estado=estado)


class PublicarDespliegueFirmaTests(_BaseDespliegueTests):
    """SEC-1 (auditoría 22-ago-2026): el mensaje de despliegue no llevaba ninguna
    firma HMAC — cualquiera con permiso de publish en el tópico podía forzar la
    instalación de un paquete arbitrario. Ahora lleva `timestamp` + `firma`, y el
    agente reconstruye la misma firma que apps.catalogo.services.firmar_payload."""

    def test_el_payload_lleva_timestamp_y_firma_valida(self):
        despliegue = self._crear_despliegue()
        despliegue.estaciones.set([self._crear_estacion('ML001-A')])

        with patch('apps.despliegues.services.mqtt_publish.multiple') as mock_multiple:
            publicar_despliegue(despliegue)

        payload = json.loads(mock_multiple.call_args.args[0][0]['payload'])
        self.assertIn('timestamp', payload)
        self.assertIn('firma', payload)
        firma_esperada = firmar_payload(
            comando='desplegar', despliegue_id=payload['despliegue_id'], version=payload['version'],
            url=payload['url'], sha256=payload['sha256'], modo_aplicacion=payload['modo_aplicacion'],
            ventana_fecha_hora=payload['ventana_fecha_hora'], timestamp=payload['timestamp'],
        )
        self.assertEqual(payload['firma'], firma_esperada)

    def test_manipular_el_payload_invalida_la_firma(self):
        despliegue = self._crear_despliegue()
        despliegue.estaciones.set([self._crear_estacion('ML001-A')])

        with patch('apps.despliegues.services.mqtt_publish.multiple') as mock_multiple:
            publicar_despliegue(despliegue)

        payload = json.loads(mock_multiple.call_args.args[0][0]['payload'])
        payload['url'] = 'https://atacante.example.com/malicioso.zip'  # simula un mensaje alterado/reenviado
        firma_recalculada = firmar_payload(
            comando='desplegar', despliegue_id=payload['despliegue_id'], version=payload['version'],
            url=payload['url'], sha256=payload['sha256'], modo_aplicacion=payload['modo_aplicacion'],
            ventana_fecha_hora=payload['ventana_fecha_hora'], timestamp=payload['timestamp'],
        )
        self.assertNotEqual(payload['firma'], firma_recalculada)


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
                # Se afirma el COMPORTAMIENTO, no qué función concreta atiende. Desde el
                # 2-oct-2026 la ruta la atiende `servir_media_publico`, que delega en
                # `django.views.static.serve` para todo /media/ salvo los subárboles
                # protegidos (ver PREFIJOS_MEDIA_PROTEGIDOS en config/urls.py). Afirmar
                # `func is serve` hacía fallar esta prueba por envolver la vista, aunque
                # lo que cuida —que los agentes no reciban 404— seguía intacto.
                self.assertEqual(coincidencia.func, config.urls.servir_media_publico)
                self.assertEqual(coincidencia.kwargs['path'], 'despliegues/x.zip')
                self.assertEqual(coincidencia.kwargs['document_root'], settings.MEDIA_ROOT)

                # Y el subárbol protegido NO se sirve por esta vía ni con DEBUG=False:
                # los informes y fotos de mantenimiento salen solo por la vista con
                # control de acceso. Acá viven las dos mitades de la misma ruta, así que
                # conviene comprobarlas juntas.
                protegida = resolve('/media/mantenimiento/informes/2026/10/x.pdf')
                self.assertEqual(protegida.func, config.urls.servir_media_publico)
                with self.assertRaises(Http404):
                    protegida.func(None, **protegida.kwargs)
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
        self.aprobador.user_permissions.add(
            Permission.objects.get(content_type__app_label='despliegues', codename='change_despliegue'),
        )

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


class FanOutPorEstacionTests(_BaseDespliegueTests):
    """Un despliegue a grupo o cadena se publica por estación, no en un tópico agregado.

    El motivo no es el ruteo sino la firma: un único payload en `/saidsof/despliegue/global/`
    lo tienen que poder verificar las 700 estaciones, y eso obliga a firmarlo con el
    `COMANDO_HMAC_SECRET` compartido — el mismo que hay que tipear a mano en cada config.txt
    y el que impide publicar el instalador completo (§10-Z). Publicando por estación, cada
    copia se firma con el secreto de su destinataria.

    Lo que NO puede cambiar: los campos que entran a la firma. El agente 0.20 reconstruye
    esa lista exacta para validar; agregarle `estacion` ahí dejaría sin desplegar a toda la
    flota que todavía no se actualizó.
    """

    def _publicar_a_la_cadena(self, estaciones):
        despliegue = self._crear_despliegue(destino_tipo=Despliegue.DestinoTipo.CADENA)
        with patch('apps.despliegues.services.mqtt_publish.multiple') as mock_multiple:
            publicar_despliegue(despliegue)
        return {m['topic']: json.loads(m['payload']) for m in mock_multiple.call_args.args[0]}

    def test_un_despliegue_a_la_cadena_va_al_topico_de_cada_estacion(self):
        self._crear_estacion('ML001-A')
        self._crear_estacion('ML001-B')
        publicado = self._publicar_a_la_cadena(None)
        self.assertEqual(
            set(publicado),
            {'/saidsof/agente/ML001-A/despliegue/', '/saidsof/agente/ML001-B/despliegue/'},
        )

    def test_ya_no_se_publica_en_ningun_topico_de_difusion(self):
        """Mientras exista un solo publish a un tópico compartido, el secreto compartido
        sigue siendo obligatorio en todos los config.txt."""
        self._crear_estacion('ML001-A')
        publicado = self._publicar_a_la_cadena(None)
        for topico in publicado:
            self.assertNotIn('/saidsof/despliegue/global/', topico)
            self.assertNotIn('/despliegue/grupo/', topico)
            self.assertNotIn('/despliegue/farmacia/', topico)

    def test_cada_estacion_recibe_su_propia_firma(self):
        a = self._crear_estacion('ML001-A', version='agente-prueba-0.21', hmac_propio=True)
        b = self._crear_estacion('ML001-B', version='agente-prueba-0.21', hmac_propio=True)
        publicado = self._publicar_a_la_cadena(None)
        firma_a = publicado['/saidsof/agente/ML001-A/despliegue/']['firma']
        firma_b = publicado['/saidsof/agente/ML001-B/despliegue/']['firma']
        self.assertNotEqual(firma_a, firma_b)
        self.assertEqual(
            firma_a,
            firmar_payload(
                a.hmac_secret, comando='desplegar',
                **{k: v for k, v in publicado['/saidsof/agente/ML001-A/despliegue/'].items()
                   if k not in ('timestamp', 'firma', 'usar_cache')},
                timestamp=publicado['/saidsof/agente/ML001-A/despliegue/']['timestamp'],
            ),
        )
        self.assertNotEqual(a.hmac_secret, b.hmac_secret)

    def test_una_estacion_con_agente_viejo_recibe_la_firma_compartida(self):
        """Convivencia: en la misma publicación, una en 0.20 y otra en 0.21. Si a la 0.20
        le llegara la firma con el secreto propio, no desplegaría y el panel la mostraría
        como pendiente sin ninguna pista del motivo."""
        self._crear_estacion('ML001-VIEJA', version='agente-prueba-0.20')
        self._crear_estacion('ML001-NUEVA', version='agente-prueba-0.21', hmac_propio=True)
        publicado = self._publicar_a_la_cadena(None)

        vieja = publicado['/saidsof/agente/ML001-VIEJA/despliegue/']
        campos = {k: v for k, v in vieja.items() if k not in ('timestamp', 'firma', 'usar_cache')}
        self.assertEqual(
            vieja['firma'],
            firmar_payload(comando='desplegar', **campos, timestamp=vieja['timestamp']),
        )

    def test_los_campos_firmados_no_cambiaron(self):
        """Blindaje del contrato con el agente 0.20: si alguien agrega un campo al payload
        firmado, todas las estaciones sin actualizar dejan de desplegar en silencio."""
        self._crear_estacion('ML001-A')
        publicado = self._publicar_a_la_cadena(None)
        payload = publicado['/saidsof/agente/ML001-A/despliegue/']
        self.assertEqual(
            set(payload),
            {'despliegue_id', 'version', 'url', 'sha256', 'modo_aplicacion',
             'ventana_fecha_hora', 'timestamp', 'usar_cache', 'firma'},
        )

    def test_se_crea_un_resultado_por_estacion_igual_que_antes(self):
        self._crear_estacion('ML001-A')
        self._crear_estacion('ML001-B')
        despliegue = self._crear_despliegue(destino_tipo=Despliegue.DestinoTipo.CADENA)
        with patch('apps.despliegues.services.mqtt_publish.multiple'):
            resultado = publicar_despliegue(despliegue)
        self.assertEqual(resultado.total_estaciones, 2)
        self.assertEqual(despliegue.resultados.count(), 2)


class IngestaDeEstadoDeDespliegueTests(_BaseDespliegueTests):
    """`registrar_estado_de_estacion`: punto 5 del plan de ingesta.

    Hasta el 2-oct-2026 el mapa paso->estado era una constante a nivel de módulo en
    `apps.mqtt_worker.services` (`_PASO_A_ESTADO`): el ciclo de vida de
    ResultadoDespliegue escrito en el worker de transporte.
    """

    def setUp(self):
        # Reusa `_crear_despliegue`/`_crear_estacion` de la base: armar un Despliegue a
        # mano duplicaba sus campos obligatorios y se desincronizaba con el primero que
        # cambiara. Me paso al escribir esto — invente un campo `paquete` que no existe,
        # el real es `archivo`.
        super().setUp()
        self.estacion = self._crear_estacion('ML001-A')
        self.despliegue = self._crear_despliegue()
        self.despliegue.estaciones.add(self.estacion)

    def test_traduce_el_paso_al_estado_agregado_y_deja_el_evento(self):
        from apps.despliegues.services import registrar_estado_de_estacion

        r = registrar_estado_de_estacion(
            despliegue_id=self.despliegue.pk, estacion=self.estacion,
            paso=EventoDespliegue.Paso.DESCARGADO,
        )

        self.assertEqual(r.estado, ResultadoDespliegue.Estado.DESCARGADO)
        self.assertEqual(r.eventos.count(), 1)
        self.assertEqual(r.eventos.first().paso, EventoDespliegue.Paso.DESCARGADO)

    def test_un_paso_desconocido_no_escribe_nada(self):
        from apps.despliegues.services import registrar_estado_de_estacion

        self.assertIsNone(registrar_estado_de_estacion(
            despliegue_id=self.despliegue.pk, estacion=self.estacion, paso='inventado',
        ))
        self.assertEqual(ResultadoDespliegue.objects.count(), 0)

    def test_el_error_guarda_su_detalle(self):
        from apps.despliegues.services import registrar_estado_de_estacion

        r = registrar_estado_de_estacion(
            despliegue_id=self.despliegue.pk, estacion=self.estacion,
            paso=EventoDespliegue.Paso.ERROR, datos={'detalle': 'hash no coincide'},
        )

        self.assertEqual(r.estado, ResultadoDespliegue.Estado.ERROR)
        self.assertEqual(r.detalle_error, 'hash no coincide')

    def test_repetir_el_paso_no_duplica_el_resultado(self):
        from apps.despliegues.services import registrar_estado_de_estacion

        for _ in range(3):
            registrar_estado_de_estacion(
                despliegue_id=self.despliegue.pk, estacion=self.estacion,
                paso=EventoDespliegue.Paso.RECIBIDO,
            )

        self.assertEqual(ResultadoDespliegue.objects.count(), 1)
        # La linea de tiempo SI acumula: es el historial de lo que fue pasando.
        self.assertEqual(EventoDespliegue.objects.count(), 3)

    def test_no_toca_la_estacion_eso_lo_coordina_el_worker(self):
        """Línea de diseño deliberada: que un despliegue termine OK también significa
        "esta caja está viva y corre tal versión del POS", pero esa conclusión cruza
        catalogo, facturacion y monitoreo. Traerla acá obligaría a `despliegues` a
        importar las otras dos — acoplamiento que hoy no existe. La sigue coordinando el
        worker, que es orquestación legítima.
        """
        from apps.despliegues.services import registrar_estado_de_estacion

        version_antes = self.estacion.version_pos
        registrar_estado_de_estacion(
            despliegue_id=self.despliegue.pk, estacion=self.estacion,
            paso=EventoDespliegue.Paso.OK, datos={'version_nueva': '9.9.9'},
        )

        self.estacion.refresh_from_db()
        self.assertEqual(self.estacion.version_pos, version_antes)

    def test_el_worker_ya_no_escribe_estas_tablas(self):
        from pathlib import Path

        from django.conf import settings

        fuente = (
            Path(settings.BASE_DIR) / 'apps' / 'mqtt_worker' / 'services.py'
        ).read_text(encoding='utf-8')
        self.assertNotIn('ResultadoDespliegue', fuente)
        self.assertNotIn('EventoDespliegue', fuente)
        self.assertNotIn('_PASO_A_ESTADO', fuente)


class BorrarEstacionNoSeLlevaElHistorialTests(TestCase):
    """`ResultadoDespliegue.estacion` es PROTECT desde el 6-oct-2026.

    Esa fila, con sus `EventoDespliegue`, es el acta de que una version llego (o no) a esa
    caja: alimenta el informe de despliegues y es lo unico que responde "¿esta estacion
    recibio la 2.5.0?". Con CASCADE, borrar la estacion desde el admin se la llevaba en
    silencio.

    El proyecto ya aplica el criterio contrario donde importa: `EventoAuditoria.usuario` es
    SET_NULL, asi que borrar a una persona no borra lo que hizo. El historial de despliegues
    era mas fragil que la auditoria, y no habia razon para que lo fuera.
    """

    def setUp(self):
        self.sg = UnidadNegocio.objects.get(codigo='SG')
        grupo = Grupo.objects.create(codigo='TRXPRO')
        self.farmacia = Farmacia.objects.create(
            codigo='PRO001', grupo=grupo, unidad_negocio=self.sg,
        )
        self.estacion = Estacion.objects.create(
            codigo='PRO001-A', farmacia=self.farmacia,
            estado_aprobacion=Estacion.EstadoAprobacion.APROBADA,
        )
        self.usuario = User.objects.create_user(username='u_protect', password='x')

    def _despliegue_con_resultado(self):
        from apps.despliegues.models import Despliegue, EventoDespliegue, ResultadoDespliegue

        despliegue = Despliegue.objects.create(
            version='2.5.0', sha256='a' * 64, unidad_negocio=self.sg, creado_por=self.usuario,
            destino_tipo='estaciones',
        )
        resultado = ResultadoDespliegue.objects.create(
            despliegue=despliegue, estacion=self.estacion,
        )
        EventoDespliegue.objects.create(resultado=resultado, paso=EventoDespliegue.Paso.OK)
        return despliegue, resultado

    def test_no_se_puede_borrar_una_estacion_con_despliegues(self):
        """PROTECT no es un bloqueo caprichoso: es la pausa para decidir que hacer con el
        historial antes de perderlo."""
        from django.db.models import ProtectedError

        self._despliegue_con_resultado()

        with self.assertRaises(ProtectedError):
            self.estacion.delete()

    def test_el_acta_sigue_ahi_despues_del_intento(self):
        """Un borrado rechazado no puede dejar nada a medias."""
        from apps.despliegues.models import EventoDespliegue, ResultadoDespliegue
        from django.db import transaction
        from django.db.models import ProtectedError

        self._despliegue_con_resultado()

        with transaction.atomic():
            with self.assertRaises(ProtectedError):
                self.estacion.delete()

        self.assertEqual(ResultadoDespliegue.objects.count(), 1)
        self.assertEqual(EventoDespliegue.objects.count(), 1)
        self.assertTrue(Estacion.objects.filter(pk=self.estacion.pk).exists())

    def test_una_estacion_sin_historial_si_se_borra(self):
        """La proteccion no puede volver indestructible a cualquier estacion: una que nunca
        recibio un despliegue no tiene acta que cuidar."""
        sin_historial = Estacion.objects.create(
            codigo='PRO001-B', farmacia=self.farmacia,
            estado_aprobacion=Estacion.EstadoAprobacion.APROBADA,
        )
        sin_historial.delete()
        self.assertFalse(Estacion.objects.filter(codigo='PRO001-B').exists())

    def test_borrar_el_despliegue_si_se_lleva_sus_resultados(self):
        """Lo que NO cambio, y es deliberado: ahi el agregado es el despliegue, y sus
        resultados no significan nada sin el. Solo se protegio el lado de la estacion."""
        from apps.despliegues.models import EventoDespliegue, ResultadoDespliegue

        despliegue, _ = self._despliegue_con_resultado()
        despliegue.delete()

        self.assertEqual(ResultadoDespliegue.objects.count(), 0)
        self.assertEqual(EventoDespliegue.objects.count(), 0)
        self.assertTrue(Estacion.objects.filter(pk=self.estacion.pk).exists())

    def test_las_series_de_tiempo_siguen_en_cascada(self):
        """Tampoco cambio, y tambien es deliberado: tienen retencion de 30 dias y no
        significan nada sin su estacion. Protegerlas dejaria millones de filas huerfanas."""
        from apps.monitoreo.models import MuestraMetrica

        otra = Estacion.objects.create(
            codigo='PRO001-C', farmacia=self.farmacia,
            estado_aprobacion=Estacion.EstadoAprobacion.APROBADA,
        )
        MuestraMetrica.objects.create(estacion=otra, cpu_carga_pct=42)

        otra.delete()
        self.assertEqual(MuestraMetrica.objects.count(), 0)
