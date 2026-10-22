"""Alta de activo (/activos/nuevo/): revisión del 22-oct-2026.

- El tipo ya no arranca en "Laptop".
- Avisa si la serie ya existe y pide confirmar para registrarlo igual.
- Procesador/RAM/almacenamiento no se guardan en un equipo que no es de cómputo.
- La garantía no puede vencer antes de la compra.
- Etiquetas legibles y buscador de farmacias.
"""
from datetime import date

from django.contrib.auth.models import Permission, User
from django.test import TestCase
from django.urls import reverse

from apps.activos.forms import ActivoIngresoForm
from apps.activos.models import Activo, Bodega
from apps.catalogo.models import UnidadNegocio
from apps.cuentas.models import PerfilUsuario


class AltaDeActivoTests(TestCase):
    def setUp(self):
        self.sg = UnidadNegocio.objects.get(codigo='SG')
        self.bodega = Bodega.objects.create(codigo='BOD-AF', nombre='Bodega', unidad_negocio=self.sg)
        self.usuario = User.objects.create_user(username='u_alta_form', password='x')
        PerfilUsuario.objects.create(usuario=self.usuario, acceso_todas_unidades=True)
        self.usuario.user_permissions.add(*Permission.objects.filter(
            content_type__app_label='activos', codename__in=['add_activo', 'view_activo'],
        ))
        self.client.force_login(self.usuario)

    def _alta(self, **extra):
        datos = {'tipo': Activo.Tipo.IMPRESORA, 'modelo': 'MP C3004', 'bodega': self.bodega.pk}
        datos.update(extra)
        return self.client.post(reverse('panel:activo_crear'), datos)

    def test_el_tipo_no_viene_preseleccionado_y_es_obligatorio(self):
        pagina = self.client.get(reverse('panel:activo_crear'))
        self.assertContains(pagina, 'Elegí el tipo de equipo…')
        self.assertIsNone(ActivoIngresoForm(user=self.usuario)['tipo'].value())
        resp = self._alta(tipo='')
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(Activo.objects.exists())

    def test_una_serie_repetida_avisa_y_se_registra_solo_confirmando(self):
        existente = Activo.objects.create(codigo='CR-IMP-0001', tipo=Activo.Tipo.IMPRESORA, numero_serie='ABC123')
        resp = self._alta(numero_serie='abc123')
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'Esta serie ya está registrada')
        self.assertContains(resp, reverse('panel:activo_detalle', args=[existente.pk]))
        self.assertEqual(Activo.objects.count(), 1)

        resp = self._alta(numero_serie='abc123', confirmar_serie_duplicada='on')
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(Activo.objects.filter(numero_serie__iexact='abc123').count(), 2)

    def test_una_serie_de_un_activo_dado_de_baja_no_cuenta(self):
        Activo.objects.create(codigo='CR-IMP-0002', tipo=Activo.Tipo.IMPRESORA, numero_serie='BAJA1',
                              estado=Activo.Estado.DADO_DE_BAJA)
        self.assertEqual(self._alta(numero_serie='BAJA1').status_code, 302)

    def test_la_serie_de_otra_unidad_se_cuenta_sin_mostrarla(self):
        otra = UnidadNegocio.objects.exclude(pk=self.sg.pk).first() or UnidadNegocio.objects.create(codigo='OTRA')
        Activo.objects.create(codigo='CR-IMP-0003', tipo=Activo.Tipo.IMPRESORA, numero_serie='AJENA',
                              unidad_negocio=otra)
        limitado = User.objects.create_user(username='u_alta_limitado', password='x')
        perfil = PerfilUsuario.objects.create(usuario=limitado)
        perfil.unidades_negocio.add(self.sg)
        form = ActivoIngresoForm(
            {'tipo': Activo.Tipo.IMPRESORA, 'numero_serie': 'AJENA', 'bodega': self.bodega.pk}, user=limitado,
        )
        self.assertFalse(form.is_valid())
        self.assertEqual((form.duplicados, form.duplicados_ocultos), ([], 1))

    def test_los_datos_de_computo_no_se_guardan_en_una_impresora(self):
        self._alta(procesador='Core i5', ram_gb=8, almacenamiento_gb=256)
        activo = Activo.objects.get()
        self.assertEqual((activo.procesador, activo.ram_gb, activo.almacenamiento_gb), ('', None, None))

    def test_en_una_laptop_si_se_guardan(self):
        self._alta(tipo=Activo.Tipo.LAPTOP, procesador='Core i5', ram_gb=8, almacenamiento_gb=256)
        activo = Activo.objects.get()
        self.assertEqual((activo.procesador, activo.ram_gb, activo.almacenamiento_gb), ('Core i5', 8, 256))

    def test_la_garantia_no_puede_vencer_antes_de_la_compra(self):
        resp = self._alta(fecha_compra=date(2026, 5, 1), vencimiento_garantia=date(2026, 4, 1))
        self.assertContains(resp, 'La garantía no puede vencer antes de la fecha de compra.')
        self.assertFalse(Activo.objects.exists())

    def test_etiquetas_y_buscador_de_farmacias(self):
        pagina = self.client.get(reverse('panel:activo_crear'))
        for etiqueta in ('Categoría', 'Número de serie', 'Condición al recibir', 'Fecha de compra',
                         'Vencimiento de la garantía', 'Orden de compra'):
            self.assertContains(pagina, etiqueta)
        self.assertContains(pagina, 'data-filtrar-select="id_farmacia"')
        self.assertContains(pagina, 'data-tipo-computo="LAP,DSK,SRV,TAB"')
