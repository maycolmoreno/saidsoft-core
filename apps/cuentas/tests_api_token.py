"""Tokens de la API con MFA y vencimiento (S-05, S-06). Ver apps.cuentas.api_auth."""
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone
from django_otp.oath import TOTP
from django_otp.plugins.otp_totp.models import TOTPDevice
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient

User = get_user_model()
CLAVE = 'clave-de-prueba-1'
URL_TOKEN = '/api/v1/auth/token/'
# Un endpoint que exige estar autenticado y no pide más permisos que eso.
URL_PROTEGIDA = '/api/v1/auth/yo/'


def _codigo(dispositivo):
    return TOTP(dispositivo.bin_key, dispositivo.step, dispositivo.t0, dispositivo.digits, dispositivo.drift).token()


class BaseToken(TestCase):
    def setUp(self):
        self.usuario = User.objects.create_user(username='tecnico_api', password=CLAVE)
        self.api = APIClient()

    def _pedir(self, codigo=''):
        return self.api.post(URL_TOKEN, {'username': 'tecnico_api', 'password': CLAVE, 'otp_token': codigo})

    def _usar(self, clave):
        self.api.credentials(HTTP_AUTHORIZATION=f'Token {clave}')
        return self.api.get(URL_PROTEGIDA)


class PedirTokenTests(BaseToken):
    def test_sin_mfa_y_sin_obligatorio_funciona_como_antes(self):
        respuesta = self._pedir()
        self.assertEqual(respuesta.status_code, 200)
        self.assertEqual(self._usar(respuesta.data['token']).status_code, 200)

    def test_con_mfa_sin_codigo_o_con_uno_equivocado_no_hay_token(self):
        """S-05: antes usuario y contraseña alcanzaban aunque la cuenta tuviera MFA."""
        TOTPDevice.objects.create(user=self.usuario, name='default', confirmed=True)
        self.assertEqual(self._pedir().status_code, 400)
        self.assertEqual(self._pedir('000000').status_code, 400)
        self.assertFalse(Token.objects.filter(user=self.usuario).exists())

    def test_con_mfa_y_el_codigo_correcto_hay_token(self):
        # En una prueba aparte: después de un código equivocado, django-otp frena el
        # dispositivo unos segundos (defensa contra fuerza bruta) y rechaza también el bueno.
        dispositivo = TOTPDevice.objects.create(user=self.usuario, name='default', confirmed=True)
        respuesta = self._pedir(_codigo(dispositivo))
        self.assertEqual(respuesta.status_code, 200)
        self.assertEqual(self._usar(respuesta.data['token']).status_code, 200)

    @override_settings(MFA_OBLIGATORIO=True)
    def test_con_mfa_obligatorio_una_cuenta_sin_mfa_no_obtiene_token(self):
        respuesta = self._pedir()
        self.assertEqual(respuesta.status_code, 400)
        self.assertIn('obligatoria', str(respuesta.data))

    def test_contrasena_equivocada(self):
        respuesta = self.api.post(URL_TOKEN, {'username': 'tecnico_api', 'password': 'otra'})
        self.assertEqual(respuesta.status_code, 400)


@override_settings(API_TOKEN_DIAS=30)
class VencimientoTests(BaseToken):
    def _token_de_hace(self, dias):
        token = Token.objects.create(user=self.usuario)
        Token.objects.filter(pk=token.pk).update(created=timezone.now() - timedelta(days=dias))
        return Token.objects.get(pk=token.pk)

    def test_un_token_vigente_sirve(self):
        self.assertEqual(self._usar(self._token_de_hace(29).key).status_code, 200)

    def test_un_token_vencido_responde_401(self):
        """S-06: antes un token servía para siempre."""
        respuesta = self._usar(self._token_de_hace(31).key)
        self.assertEqual(respuesta.status_code, 401)
        self.assertIn('venció', str(respuesta.data))

    def test_pedirlo_de_nuevo_renueva_el_vencido(self):
        viejo = self._token_de_hace(31)
        nuevo = self._pedir().data['token']
        self.assertNotEqual(nuevo, viejo.key)
        self.assertEqual(self._usar(nuevo).status_code, 200)
        self.assertEqual(self._usar(viejo.key).status_code, 401)

    def test_pedirlo_de_nuevo_devuelve_el_vigente(self):
        vigente = self._token_de_hace(1)
        self.assertEqual(self._pedir().data['token'], vigente.key)

    def test_el_de_una_cuenta_de_servicio_creado_en_el_admin_tambien_vence(self):
        servicio = User.objects.create_user(username='integracion_inventario')
        token = Token.objects.create(user=servicio)
        self.assertEqual(self._usar(token.key).status_code, 200)
        Token.objects.filter(pk=token.pk).update(created=timezone.now() - timedelta(days=31))
        self.assertEqual(self._usar(token.key).status_code, 401)
