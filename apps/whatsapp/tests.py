"""Chatbot de WhatsApp con las farmacias (PLAN_MODERNIZACION.md §10-CM).

La API de Meta y el envío a Telegram se simulan: `_enviar` y `_enviar_telegram` se
reemplazan por dobles que guardan qué se mandó. Las farmacias de prueba tienen horario de
24 horas para que las pruebas no dependan de la hora a la que se corren.
"""
import hashlib
import hmac
import json
from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth.models import Permission, User
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.catalogo.models import Estacion, Farmacia, Grupo, UnidadNegocio
from apps.cuentas.models import PerfilUsuario
from apps.monitoreo.models import EstadoEnlaceFarmacia, EventoEnlaceFarmacia
from apps.whatsapp import cliente
from apps.whatsapp.models import MensajeWhatsApp, Novedad

Estado, Causa, Motivo = Novedad.Estado, Novedad.Causa, Novedad.Motivo
SECRETO = 'secreto-de-la-app'


class _Base(TestCase):
    def setUp(self):
        self.sg = UnidadNegocio.objects.get(codigo='SG')
        self.grupo = Grupo.objects.create(codigo='TRXW01')
        self.n = 0
        self.enviados = []
        self.telegram = []
        p1 = patch('apps.whatsapp.cliente._enviar', side_effect=self._enviar)
        p2 = patch('apps.monitoreo.services._enviar_telegram', side_effect=self._telegram)
        p1.start(); p2.start()
        self.addCleanup(p1.stop); self.addCleanup(p2.stop)

    def _enviar(self, wa, cuerpo):
        self.enviados.append((wa, cuerpo))
        return True, f'wamid.{len(self.enviados)}'

    def _telegram(self, chat, texto, teclado=None):
        self.telegram.append((chat, texto, teclado))
        return True

    def _farmacia(self, telefono=None, **kw):
        self.n += 1
        f = Farmacia.objects.create(
            codigo=f'GW{self.n:03d}', grupo=self.grupo, unidad_negocio=self.sg, tipo_enlace='TELCONET',
            ip_router=f'10.4.4.{self.n}', horario='24 HORAS',
            telefono=telefono if telefono is not None else f'09{self.n:08d}', **kw)
        EstadoEnlaceFarmacia.objects.create(farmacia=f, alcanzable=True, respondio_alguna_vez=True)
        return f

    def _caida(self, farmacia, hace_min=20, abierta=True):
        inicio = timezone.now() - timedelta(minutes=hace_min)
        EstadoEnlaceFarmacia.objects.filter(farmacia=farmacia).update(alcanzable=not abierta)
        return EventoEnlaceFarmacia.objects.create(
            farmacia=farmacia, inicio=inicio, fin=None if abierta else inicio + timedelta(minutes=5))

    def _entrada(self, farmacia, *, texto='', boton='', tipo=None, wa=None, foto=''):
        from apps.whatsapp import bot

        wa = wa or cliente.numero_wa(farmacia.telefono)
        if boton:
            mensaje = {'from': wa, 'type': 'button', 'button': {'payload': boton}} if boton.startswith('r:') else \
                {'from': wa, 'type': 'interactive', 'interactive': {'button_reply': {'id': boton}}}
        elif foto:
            mensaje = {'from': wa, 'type': 'image', 'image': {'id': foto, 'caption': texto}}
        else:
            mensaje = {'from': wa, 'type': 'text', 'text': {'body': texto}}
        registro = MensajeWhatsApp.objects.create(direccion='entrada', telefono=wa, contenido=mensaje)
        with self.captureOnCommitCallbacks(execute=True):
            bot.procesar(registro)

    def _ultimo_texto(self):
        cuerpo = self.enviados[-1][1]
        return (cuerpo.get('text') or {}).get('body') or cuerpo.get('interactive', {}).get('body', {}).get('text', '')


class NumeroTests(TestCase):
    def test_celular_ecuatoriano_a_formato_whatsapp(self):
        self.assertEqual(cliente.numero_wa('0981653574'), '593981653574')
        self.assertEqual(cliente.numero_wa('098 165 3574'), '593981653574')
        self.assertEqual(cliente.numero_wa('+593 98 165 3574'), '593981653574')

    def test_un_fijo_o_un_numero_a_medio_cargar_no_se_usa(self):
        self.assertEqual(cliente.numero_wa('042123456'), '')
        self.assertEqual(cliente.numero_wa('09816'), '')
        self.assertEqual(cliente.numero_wa(''), '')

    @override_settings(WHATSAPP_MODO='apagado', WHATSAPP_TOKEN='t', WHATSAPP_PHONE_NUMBER_ID='1')
    def test_en_modo_apagado_no_sale_nada(self):
        with patch('urllib.request.urlopen') as abrir:
            self.assertEqual(cliente.enviar_texto('593981653574', 'hola'), (False, 'modo apagado'))
        abrir.assert_not_called()

    @override_settings(WHATSAPP_MODO='activo', WHATSAPP_TOKEN='t', WHATSAPP_PHONE_NUMBER_ID='1',
                       WHATSAPP_REDIRIGIR_A='593999999999')
    def test_redirigir_manda_todo_al_numero_de_prueba(self):
        with patch('urllib.request.urlopen') as abrir:
            abrir.return_value.__enter__.return_value.read.return_value = b'{"messages":[{"id":"wamid.1"}]}'
            cliente.enviar_texto('593981653574', 'hola')
        cuerpo = json.loads(abrir.call_args[0][0].data)
        self.assertEqual(cuerpo['to'], '593999999999')


@override_settings(WHATSAPP_APP_SECRET=SECRETO, WHATSAPP_VERIFY_TOKEN='verifica')
class WebhookTests(TestCase):
    URL = '/whatsapp/webhook/'

    def _post(self, cuerpo, firma=None):
        datos = json.dumps(cuerpo).encode()
        firma = firma or 'sha256=' + hmac.new(SECRETO.encode(), datos, hashlib.sha256).hexdigest()
        return self.client.post(self.URL, data=datos, content_type='application/json',
                                HTTP_X_HUB_SIGNATURE_256=firma)

    def _mensaje(self, wa_id='wamid.A'):
        return {'entry': [{'changes': [{'value': {'messages': [
            {'from': '593981653574', 'id': wa_id, 'type': 'text', 'text': {'body': 'hola'}}]}}]}]}

    def test_alta_del_webhook_devuelve_el_challenge_solo_con_el_token(self):
        ok = self.client.get(self.URL, {'hub.mode': 'subscribe', 'hub.verify_token': 'verifica', 'hub.challenge': '123'})
        self.assertEqual(ok.content, b'123')
        mal = self.client.get(self.URL, {'hub.mode': 'subscribe', 'hub.verify_token': 'otro', 'hub.challenge': '123'})
        self.assertEqual(mal.status_code, 403)

    def test_sin_firma_valida_no_se_procesa(self):
        resp = self._post(self._mensaje(), firma='sha256=falsa')
        self.assertEqual(resp.status_code, 403)
        self.assertFalse(MensajeWhatsApp.objects.exists())

    @override_settings(WHATSAPP_APP_SECRET='')
    def test_sin_secreto_configurado_no_se_acepta_nada(self):
        self.assertEqual(self._post(self._mensaje(), firma='sha256=x').status_code, 403)

    def test_guarda_y_encola_una_sola_vez_aunque_meta_reintente(self):
        with patch('apps.whatsapp.tasks.procesar_mensaje_whatsapp_task.delay') as encolar:
            with self.captureOnCommitCallbacks(execute=True):
                self.assertEqual(self._post(self._mensaje()).status_code, 200)
            with self.captureOnCommitCallbacks(execute=True):
                self.assertEqual(self._post(self._mensaje()).status_code, 200)
        self.assertEqual(MensajeWhatsApp.objects.count(), 1)
        self.assertEqual(encolar.call_count, 1)


@override_settings(MESA_AYUDA_TELEGRAM_CHAT_ID='-100')
class BotTests(_Base):
    def test_un_numero_que_no_es_de_ninguna_farmacia(self):
        self._farmacia()
        self._entrada(None, texto='hola', wa='593900000000')
        self.assertIn('no está registrado', self._ultimo_texto())
        self.assertFalse(Novedad.objects.exists())
        self.assertIn('593900000000', self.telegram[-1][1])

    def test_un_mensaje_cualquiera_ofrece_el_menu(self):
        f = self._farmacia()
        self._entrada(f, texto='hola')
        botones = self.enviados[-1][1]['interactive']['action']['buttons']
        self.assertEqual([b['reply']['id'] for b in botones], ['rep:luz', 'rep:internet', 'rep:equipo'])

    def test_se_fue_la_luz_queda_registrada_y_pegada_a_la_caida(self):
        f = self._farmacia()
        caida = self._caida(f)
        self._entrada(f, boton='rep:luz')
        n = Novedad.objects.get()
        self.assertEqual((n.motivo, n.causa, n.estado, n.evento_enlace), (Motivo.REPORTE_LUZ, Causa.LUZ, Estado.REGISTRADA, caida))
        self.assertTrue(self.telegram)  # aviso informativo al NOC
        self.assertIsNone(self.telegram[-1][2])  # sin botones: no hay nada que tomar

    def test_sin_internet_escala_a_la_mesa(self):
        f = self._farmacia()
        self._entrada(f, boton='rep:internet')
        n = Novedad.objects.get()
        self.assertEqual((n.estado, n.motivo_escalada), (Estado.ESCALADA, 'reporte_internet'))
        self.assertIn('Tomo el caso', str(self.telegram[-1][2]))

    def test_un_equipo_que_falla_pide_cual_y_que_le_pasa(self):
        f = self._farmacia()
        self._entrada(f, boton='rep:equipo')
        self.assertEqual(self.enviados[-1][1]['interactive']['type'], 'list')
        self._entrada(f, boton='eq:impresora')
        self.assertIn('qué le pasa', self._ultimo_texto())
        self._entrada(f, texto='no imprime la factura')
        n = Novedad.objects.get()
        self.assertEqual((n.equipo, n.descripcion, n.estado), ('Impresora', 'no imprime la factura', Estado.ESCALADA))
        self.assertIn(f'N° {n.pk}', self._ultimo_texto())
        self.assertIn('no imprime la factura', self.telegram[-1][1])

    def test_el_equipo_se_puede_reportar_con_foto(self):
        f = self._farmacia()
        self._entrada(f, boton='rep:equipo')
        self._entrada(f, boton='eq:caja')
        self._entrada(f, foto='media.123', texto='')
        self.assertEqual(Novedad.objects.get().foto_media_id, 'media.123')

    def _pregunta(self, f, motivo=Motivo.ENLACE_CAIDO, **kw):
        return Novedad.objects.create(farmacia=f, origen='sistema', motivo=motivo, estado=Estado.ESPERANDO,
                                      preguntado_en=timezone.now(), **kw)

    def test_responder_luz_a_la_pregunta_no_escala(self):
        f = self._farmacia()
        n = self._pregunta(f, evento_enlace=self._caida(f))
        self._entrada(f, boton=f'r:{n.pk}:luz')
        n.refresh_from_db()
        self.assertEqual((n.causa, n.estado), (Causa.LUZ, Estado.REGISTRADA))

    def test_responder_internet_escala_para_reclamar(self):
        f = self._farmacia()
        n = self._pregunta(f, evento_enlace=self._caida(f))
        self._entrada(f, boton=f'r:{n.pk}:internet')
        n.refresh_from_db()
        self.assertEqual((n.estado, n.motivo_escalada), (Estado.ESCALADA, 'reclamo_proveedor'))

    def test_todo_funciona_con_el_enlace_caido_lo_revisa_ti(self):
        f = self._farmacia()
        n = self._pregunta(f, evento_enlace=self._caida(f))
        self._entrada(f, boton=f'r:{n.pk}:todo_funciona')
        n.refresh_from_db()
        self.assertEqual((n.estado, n.motivo_escalada), (Estado.ESCALADA, 'farmacia_no_ve_problema'))

    def test_contestar_con_texto_lo_lee_una_persona(self):
        f = self._farmacia()
        n = self._pregunta(f, evento_enlace=self._caida(f))
        self._entrada(f, texto='creo que es el router')
        n.refresh_from_db()
        self.assertEqual((n.estado, n.motivo_escalada, n.descripcion), (Estado.ESCALADA, 'respuesta_no_entendida', 'creo que es el router'))

    def test_un_boton_de_otra_farmacia_no_se_aplica(self):
        f, otra = self._farmacia(), self._farmacia()
        n = self._pregunta(otra, evento_enlace=self._caida(otra))
        self._entrada(f, boton=f'r:{n.pk}:luz')
        n.refresh_from_db()
        self.assertEqual(n.estado, Estado.ESPERANDO)

    def test_reportar_luz_con_una_pregunta_pendiente_es_la_respuesta(self):
        f = self._farmacia()
        n = self._pregunta(f, evento_enlace=self._caida(f))
        self._entrada(f, boton='rep:luz')
        self.assertEqual(Novedad.objects.count(), 1)
        n.refresh_from_db()
        self.assertEqual(n.causa, Causa.LUZ)

    def test_caja_apagada_se_registra_y_se_pide_encenderla(self):
        f = self._farmacia()
        est = Estacion.objects.create(codigo=f'{f.codigo}-A', farmacia=f)
        n = self._pregunta(f, motivo=Motivo.ESTACION_SIN_REPORTAR, estacion=est)
        self._entrada(f, boton=f'r:{n.pk}:caja_apagada')
        n.refresh_from_db()
        self.assertEqual(n.estado, Estado.REGISTRADA)
        self.assertIn('enciéndala', self._ultimo_texto())


@override_settings(WHATSAPP_MODO='piloto', MESA_AYUDA_TELEGRAM_CHAT_ID='-100')
class ReglasTests(_Base):
    def _evaluar(self):
        from apps.whatsapp.reglas import evaluar

        with self.captureOnCommitCallbacks(execute=True):
            return evaluar()

    def _piloto(self, *farmacias):
        return override_settings(WHATSAPP_FARMACIAS_PILOTO=[f.codigo for f in farmacias])

    def test_pregunta_por_una_caida_sola_de_mas_de_15_minutos(self):
        f = self._farmacia()
        caida = self._caida(f, hace_min=20)
        with self._piloto(f):
            self._evaluar()
        n = Novedad.objects.get()
        self.assertEqual((n.motivo, n.estado, n.evento_enlace), (Motivo.ENLACE_CAIDO, Estado.ESPERANDO, caida))
        plantilla = self.enviados[-1][1]['template']
        payloads = [c['parameters'][0]['payload'] for c in plantilla['components'] if c['type'] == 'button']
        self.assertEqual(payloads, [f'r:{n.pk}:luz', f'r:{n.pk}:internet', f'r:{n.pk}:todo_funciona'])

    def test_no_pregunta_dos_veces_por_la_misma_caida(self):
        f = self._farmacia()
        self._caida(f, hace_min=20)
        with self._piloto(f):
            self._evaluar(); self._evaluar()
        self.assertEqual(Novedad.objects.count(), 1)

    def test_no_pregunta_antes_de_15_minutos(self):
        f = self._farmacia()
        self._caida(f, hace_min=5)
        with self._piloto(f):
            self._evaluar()
        self.assertFalse(Novedad.objects.exists())

    def test_fuera_del_piloto_no_pregunta(self):
        f, otra = self._farmacia(), self._farmacia()
        self._caida(f, hace_min=20)
        with self._piloto(otra):
            self._evaluar()
        self.assertFalse(Novedad.objects.exists())

    def test_en_un_corte_masivo_no_se_le_pregunta_a_cada_farmacia(self):
        """Un corte del proveedor es un solo aviso al NOC, no 30 mensajes a 30 farmacias."""
        farmacias = [self._farmacia() for _ in range(3)]
        for f in farmacias:
            self._caida(f, hace_min=20)
        with self._piloto(*farmacias):
            self._evaluar()
        self.assertFalse(Novedad.objects.exists())

    def test_fuera_de_horario_no_pregunta(self):
        f = self._farmacia()
        Farmacia.objects.filter(pk=f.pk).update(horario='08:00 a 08:01')
        self._caida(f, hace_min=20)
        with self._piloto(f):
            self._evaluar()
        self.assertFalse(Novedad.objects.exists())

    def test_maximo_dos_preguntas_por_farmacia_por_dia(self):
        f = self._farmacia()
        with self._piloto(f):
            for _ in range(3):
                caida = self._caida(f, hace_min=20)
                self._evaluar()
                EventoEnlaceFarmacia.objects.filter(pk=caida.pk).update(fin=timezone.now())
        self.assertEqual(Novedad.objects.filter(origen='sistema').count(), 2)

    def test_sin_celular_va_directo_a_la_mesa(self):
        f = self._farmacia(telefono='')
        self._caida(f, hace_min=20)
        with self._piloto(f):
            self._evaluar()
        n = Novedad.objects.get()
        self.assertEqual((n.estado, n.motivo_escalada), (Estado.ESCALADA, 'sin_telefono'))

    def test_si_no_responde_en_15_minutos_escala_a_la_mesa(self):
        f = self._farmacia()
        self._caida(f, hace_min=20)
        with self._piloto(f):
            self._evaluar()
            Novedad.objects.update(preguntado_en=timezone.now() - timedelta(minutes=16))
            self._evaluar()
        n = Novedad.objects.get()
        self.assertEqual((n.estado, n.motivo_escalada), (Estado.ESCALADA, 'sin_respuesta'))
        self.assertIn(f.telefono, self.telegram[-1][1])  # el número para llamarla

    def test_si_la_caida_vuelve_se_cierra_y_avisa_solo_si_la_farmacia_contesto(self):
        f = self._farmacia()
        caida = self._caida(f, hace_min=20)
        with self._piloto(f):
            self._evaluar()
        n = Novedad.objects.get()
        Novedad.objects.filter(pk=n.pk).update(respondido_en=timezone.now(), causa=Causa.LUZ, estado=Estado.REGISTRADA)
        EventoEnlaceFarmacia.objects.filter(pk=caida.pk).update(fin=timezone.now())
        antes = len(self.enviados)
        self._evaluar()
        n.refresh_from_db()
        self.assertEqual(n.estado, Estado.CERRADA)
        self.assertEqual(len(self.enviados), antes + 1)
        self.assertIn('volvió', self._ultimo_texto())

    def test_luz_que_dura_mas_de_dos_horas_escala(self):
        f = self._farmacia()
        caida = self._caida(f, hace_min=200)
        Novedad.objects.create(farmacia=f, origen='farmacia', motivo=Motivo.REPORTE_LUZ, causa=Causa.LUZ,
                               estado=Estado.REGISTRADA, evento_enlace=caida,
                               respondido_en=timezone.now() - timedelta(hours=3))
        self._evaluar()
        self.assertEqual(Novedad.objects.get().motivo_escalada, 'luz_prolongada')

    @override_settings(WHATSAPP_MODO='apagado')
    def test_en_modo_apagado_no_pregunta(self):
        f = self._farmacia()
        self._caida(f, hace_min=20)
        with self._piloto(f):
            self._evaluar()
        self.assertFalse(Novedad.objects.exists())


@override_settings(MESA_AYUDA_TELEGRAM_CHAT_ID='-100', TELEGRAM_CHAT_IDS_AUTORIZADOS=['-100'])
class MesaTests(_Base):
    def _novedad(self, **kw):
        f = self._farmacia()
        return Novedad.objects.create(farmacia=f, origen='sistema', motivo=Motivo.ENLACE_CAIDO,
                                      estado=Estado.ESCALADA, motivo_escalada='sin_respuesta',
                                      evento_enlace=self._caida(f), **kw)

    def test_los_botones_de_telegram_toman_registran_y_cierran(self):
        from apps.monitoreo.telegram_bot import responder_a_callback

        n = self._novedad()
        responder_a_callback(f'mesa:tomar:{n.pk}', '-100')
        n.refresh_from_db()
        self.assertEqual((n.estado, n.tomado_por_texto), (Estado.EN_ATENCION, 'chat -100'))
        responder_a_callback(f'mesa:luz:{n.pk}', '-100')
        n.refresh_from_db()
        self.assertEqual((n.causa, n.respondido_por_mesa), (Causa.LUZ, True))
        responder_a_callback(f'mesa:cerrar:{n.pk}', '-100')
        n.refresh_from_db()
        self.assertEqual(n.estado, Estado.CERRADA)

    def test_la_bandeja_del_panel_muestra_y_atiende_los_casos(self):
        n = self._novedad()
        u = User.objects.create_user(username='mesa1', password='x')
        u.user_permissions.add(*Permission.objects.filter(codename__in=['view_novedad', 'change_novedad']))
        PerfilUsuario.objects.create(usuario=u, acceso_todas_unidades=True)
        self.client.force_login(u)
        resp = self.client.get(reverse('panel:novedades_lista'))
        self.assertContains(resp, n.farmacia.codigo)
        self.assertContains(resp, 'La farmacia no respondió el WhatsApp')
        self.client.post(reverse('panel:novedad_accion', args=[n.pk]), {'accion': 'internet', 'ver': 'mesa'})
        n.refresh_from_db()
        self.assertEqual((n.causa, n.estado, n.tomado_por), (Causa.INTERNET, Estado.EN_ATENCION, u))

    def test_la_bandeja_pone_primero_lo_que_nadie_tomo(self):
        """Ordenaba por `estado` alfabético: 'en_atencion' < 'escalada', así que lo ya
        tomado salía arriba aunque fuera más nuevo (auditoría 17-oct-2026, BD-11)."""
        tomada = self._novedad(escalado_en=timezone.now() - timedelta(hours=2))
        Novedad.objects.filter(pk=tomada.pk).update(estado=Estado.EN_ATENCION)
        sin_tomar = self._novedad(escalado_en=timezone.now() - timedelta(minutes=5))
        u = User.objects.create_user(username='mesa3', password='x')
        u.user_permissions.add(*Permission.objects.filter(codename__in=['view_novedad']))
        PerfilUsuario.objects.create(usuario=u, acceso_todas_unidades=True)
        self.client.force_login(u)
        filas = list(self.client.get(reverse('panel:novedades_lista')).context['pagina'])
        self.assertEqual([n.pk for n in filas], [sin_tomar.pk, tomada.pk])

    def test_la_bandeja_no_redirige_a_una_url_ajena(self):
        n = self._novedad()
        u = User.objects.create_user(username='mesa2', password='x')
        u.user_permissions.add(*Permission.objects.filter(codename__in=['view_novedad', 'change_novedad']))
        PerfilUsuario.objects.create(usuario=u, acceso_todas_unidades=True)
        self.client.force_login(u)
        resp = self.client.post(reverse('panel:novedad_accion', args=[n.pk]), {'accion': 'tomar', 'ver': 'https://malo.test'})
        self.assertEqual(resp['Location'], reverse('panel:novedades_lista') + '?ver=mesa')

    def test_sin_permiso_no_se_ve(self):
        u = User.objects.create_user(username='nadie', password='x')
        PerfilUsuario.objects.create(usuario=u, acceso_todas_unidades=True)
        self.client.force_login(u)
        self.assertEqual(self.client.get(reverse('panel:novedades_lista')).status_code, 403)

    def test_un_corte_de_luz_confirmado_sale_del_sla(self):
        from apps.monitoreo.historial_enlaces import historial_enlaces

        n = self._novedad()
        Novedad.objects.filter(pk=n.pk).update(causa=Causa.LUZ)
        hasta = timezone.now()
        h = historial_enlaces(Farmacia.objects.all(), hasta - timedelta(days=1), hasta)
        self.assertEqual(h.excluidas_por_luz, 1)
        self.assertEqual(h.por_farmacia[0].disponibilidad_sla, 100.0)
