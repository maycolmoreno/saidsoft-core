"""MFA obligatorio (S-07) y login de /admin/ por el panel. Ver apps.cuentas.mfa."""
from datetime import timedelta
from io import StringIO

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from django_otp.oath import TOTP
from django_otp.plugins.otp_totp.models import TOTPDevice

from apps.cuentas.models import PerfilUsuario

User = get_user_model()
CLAVE = 'clave-de-prueba-1'
AYER = (timezone.localdate() - timedelta(days=1)).isoformat()
MANANA = (timezone.localdate() + timedelta(days=1)).isoformat()


def _codigo(dispositivo):
    return TOTP(dispositivo.bin_key, dispositivo.step, dispositivo.t0, dispositivo.digits, dispositivo.drift).token()


class BaseMFA(TestCase):
    def setUp(self):
        self.usuario = User.objects.create_superuser(username='operador_mfa', password=CLAVE, email='o@x.ec')

    def _entrar(self, codigo=''):
        return self.client.post(reverse('panel:login'), {'username': self.usuario.username, 'password': CLAVE,
                                                         'otp_token': codigo})

    def _con_dispositivo(self):
        return TOTPDevice.objects.create(user=self.usuario, name='default', confirmed=True)

    def _autenticado(self):
        return '_auth_user_id' in self.client.session


class LoginDelAdminTests(BaseMFA):
    def test_el_login_del_admin_manda_al_del_panel_conservando_el_destino(self):
        respuesta = self.client.get(reverse('admin:login') + '?next=/admin/catalogo/')
        self.assertEqual(respuesta.status_code, 302)
        self.assertTrue(respuesta['Location'].startswith(reverse('panel:login')))
        self.assertIn('next=/admin/catalogo/', respuesta['Location'])

    def test_ya_no_se_puede_entrar_por_el_admin_sin_el_codigo(self):
        """El hueco: con el login estándar del admin, usuario y contraseña alcanzaban
        aunque la cuenta tuviera MFA, y la sesión abría también el panel."""
        self._con_dispositivo()
        self.client.post(reverse('admin:login'), {'username': self.usuario.username, 'password': CLAVE})
        self.assertFalse(self._autenticado())
        self.assertEqual(self.client.get(reverse('admin:index')).status_code, 302)

    def test_una_destino_externo_no_se_sigue(self):
        respuesta = self.client.get(reverse('admin:login') + '?next=https://otro.sitio/')
        self.assertNotIn('otro.sitio', respuesta['Location'])

    def test_con_sesion_de_staff_va_directo_al_admin(self):
        self._entrar()
        respuesta = self.client.get(reverse('admin:login'))
        self.assertEqual(respuesta['Location'], reverse('admin:index'))


class MFAOpcionalTests(BaseMFA):
    """Sin MFA_OBLIGATORIO, todo como antes."""

    def test_sin_dispositivo_entra_y_usa_el_panel(self):
        self._entrar()
        self.assertTrue(self._autenticado())
        respuesta = self.client.get(reverse('panel:dashboard'))
        self.assertNotEqual(respuesta.get('Location'), reverse('panel:mfa_configurar'))

    def test_con_dispositivo_el_codigo_sigue_siendo_obligatorio(self):
        dispositivo = self._con_dispositivo()
        self._entrar()
        self.assertFalse(self._autenticado())
        self._entrar(_codigo(dispositivo))
        self.assertTrue(self._autenticado())

    def test_se_puede_desactivar(self):
        self._con_dispositivo()
        self.client.force_login(self.usuario)
        self.client.post(reverse('panel:mfa_desactivar'), {'password': CLAVE})
        self.assertFalse(TOTPDevice.objects.filter(user=self.usuario).exists())


@override_settings(MFA_OBLIGATORIO=True, MFA_PLAZO_ENROLAMIENTO=MANANA)
class MFAObligatorioDentroDelPlazoTests(BaseMFA):
    def test_sin_dispositivo_entra_y_queda_obligado_a_configurarlo(self):
        self._entrar()
        self.assertTrue(self._autenticado())
        for nombre in ('panel:dashboard', 'panel:estaciones_lista'):
            with self.subTest(nombre=nombre):
                self.assertRedirects(self.client.get(reverse(nombre)), reverse('panel:mfa_configurar'),
                                     fetch_redirect_response=False)
        self.assertRedirects(self.client.get(reverse('admin:index')), reverse('panel:mfa_configurar'),
                             fetch_redirect_response=False)
        self.assertEqual(self.client.get(reverse('panel:mfa_configurar')).status_code, 200)

    def test_un_fragmento_htmx_recibe_hx_redirect(self):
        self._entrar()
        respuesta = self.client.get(reverse('panel:dashboard'), HTTP_HX_REQUEST='true')
        self.assertEqual((respuesta.status_code, respuesta['HX-Redirect']), (204, reverse('panel:mfa_configurar')))

    def test_la_api_y_los_archivos_no_se_redirigen(self):
        self._entrar()
        for ruta in ('/api/v1/rmm/estaciones/', '/media/no-existe.txt'):
            with self.subTest(ruta=ruta):
                self.assertNotEqual(self.client.get(ruta).get('Location'), reverse('panel:mfa_configurar'))

    def test_configurado_el_dispositivo_el_panel_queda_libre(self):
        self._entrar()
        self._con_dispositivo()
        self.assertNotEqual(self.client.get(reverse('panel:dashboard')).get('Location'),
                            reverse('panel:mfa_configurar'))

    def test_con_dispositivo_sigue_pidiendo_el_codigo(self):
        dispositivo = self._con_dispositivo()
        self._entrar()
        self.assertFalse(self._autenticado())
        self._entrar(_codigo(dispositivo))
        self.assertTrue(self._autenticado())

    def test_no_se_puede_desactivar(self):
        self._con_dispositivo()
        self.client.force_login(self.usuario)
        self.client.post(reverse('panel:mfa_desactivar'), {'password': CLAVE})
        self.assertTrue(TOTPDevice.objects.filter(user=self.usuario).exists())


@override_settings(MFA_OBLIGATORIO=True, MFA_PLAZO_ENROLAMIENTO=AYER)
class MFAObligatorioPlazoVencidoTests(BaseMFA):
    def test_sin_dispositivo_no_entra(self):
        respuesta = self._entrar()
        self.assertFalse(self._autenticado())
        # Mismo mensaje genérico que una contraseña equivocada (no confirma que acertó),
        # con la indicación para pedir la habilitación.
        self.assertContains(respuesta, 'Usuario, contraseña o código de verificación incorrectos')
        self.assertContains(respuesta, 'pedile a un administrador')
        self.assertEqual(self.client.post(reverse('panel:login'), {'username': self.usuario.username,
                                                                   'password': 'otra'}).status_code,
                         respuesta.status_code)

    def test_una_ventana_individual_lo_deja_entrar_a_configurarlo(self):
        PerfilUsuario.objects.create(usuario=self.usuario, mfa_enrolar_hasta=timezone.now() + timedelta(hours=2))
        self._entrar()
        self.assertTrue(self._autenticado())
        self.assertRedirects(self.client.get(reverse('panel:dashboard')), reverse('panel:mfa_configurar'),
                             fetch_redirect_response=False)

    def test_una_ventana_vencida_no_sirve(self):
        PerfilUsuario.objects.create(usuario=self.usuario, mfa_enrolar_hasta=timezone.now() - timedelta(minutes=1))
        self._entrar()
        self.assertFalse(self._autenticado())

    def test_con_dispositivo_entra_con_el_codigo(self):
        dispositivo = self._con_dispositivo()
        self._entrar(_codigo(dispositivo))
        self.assertTrue(self._autenticado())


class EstadoMFATests(BaseMFA):
    @override_settings(MFA_OBLIGATORIO=True, MFA_PLAZO_ENROLAMIENTO=AYER)
    def test_lista_quien_tiene_y_quien_no(self):
        otro = User.objects.create_user(username='sin_mfa', password=CLAVE)
        TOTPDevice.objects.create(user=otro, name='default', confirmed=True)
        salida = StringIO()
        call_command('estado_mfa', stdout=salida)
        texto = salida.getvalue()
        self.assertIn('MFA obligatorio: SÍ', texto)
        self.assertIn('Con MFA (1)', texto)
        self.assertIn('Sin MFA (1)', texto)
        self.assertIn('operador_mfa', texto)
        self.assertIn('NO puede entrar', texto)
