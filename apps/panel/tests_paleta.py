"""Búsqueda rápida (Ctrl K) y decisiones pendientes del tablero (fase 3 de la interfaz)."""
from django.contrib.auth.models import Permission, User
from django.test import TestCase
from django.urls import reverse

from apps.activos.models import Activo
from apps.catalogo import ordenes
from apps.catalogo.models import Estacion, Farmacia, Grupo, OrdenAgente, UnidadNegocio
from apps.cuentas.models import PerfilUsuario
from apps.despliegues.models import Despliegue


class PaletaTests(TestCase):
    def setUp(self):
        self.sg = UnidadNegocio.objects.get(codigo='SG')
        self.otra = UnidadNegocio.objects.exclude(pk=self.sg.pk).first() or UnidadNegocio.objects.create(codigo='OTRA')
        farmacia = Farmacia.objects.create(codigo='GSD15', nombre='Sauces', grupo=Grupo.objects.create(codigo='TRXP1'),
                                           unidad_negocio=self.sg)
        ajena = Farmacia.objects.create(codigo='GSD99', grupo=Grupo.objects.create(codigo='TRXP2'), unidad_negocio=self.otra)
        self.estacion = Estacion.objects.create(codigo='GSD15-ADM', farmacia=farmacia, numero_serie='T4ARYZ0041549DJ')
        Estacion.objects.create(codigo='GSD99-A', farmacia=ajena)
        self.activo = Activo.objects.create(codigo='CR-DSK-0101', tipo=Activo.Tipo.DESKTOP, numero_serie='T4ARYZ0041549DJ',
                                            unidad_negocio=self.sg)
        self.usuario = User.objects.create_user(username='u_paleta', password='x')
        perfil = PerfilUsuario.objects.create(usuario=self.usuario)
        perfil.unidades_negocio.add(self.sg)
        self.usuario.user_permissions.add(*Permission.objects.filter(codename__in=['view_estacion', 'view_activo']))
        self.client.force_login(self.usuario)
        self.url = reverse('panel:paleta_resultados')

    def _buscar(self, q):
        return self.client.get(self.url, {'q': q})

    def test_pide_al_menos_dos_caracteres(self):
        self.assertContains(self._buscar('g'), 'al menos 2 caracteres')

    def test_encuentra_estacion_farmacia_y_activo_con_sus_enlaces(self):
        resp = self._buscar('gsd15')
        self.assertContains(resp, reverse('panel:estacion_ficha', args=[self.estacion.pk]))
        self.assertContains(resp, reverse('panel:estaciones_lista') + '?q=GSD15')
        por_serie = self._buscar('T4ARYZ')
        self.assertContains(por_serie, 'GSD15-ADM')
        self.assertContains(por_serie, reverse('panel:activo_detalle', args=[self.activo.pk]))

    def test_no_muestra_otra_unidad_de_negocio(self):
        self.assertNotContains(self._buscar('GSD99'), 'GSD99-A')

    def test_cada_grupo_exige_su_permiso(self):
        self.usuario.user_permissions.remove(Permission.objects.get(codename='view_activo'))
        self.assertNotContains(self._buscar('T4ARYZ'), 'CR-DSK-0101')

    def test_las_paginas_solo_si_puede_entrar(self):
        resp = self._buscar('estaciones')
        self.assertContains(resp, reverse('panel:estaciones_lista'))
        self.assertNotContains(self._buscar('despliegues'), reverse('panel:despliegues_lista'))

    def test_sin_resultados_lo_dice(self):
        self.assertContains(self._buscar('zzzz-no-existe'), 'Nada coincide')

    def test_la_barra_superior_abre_la_busqueda(self):
        pagina = self.client.get(reverse('panel:estaciones_lista'))
        self.assertContains(pagina, 'id="paleta-abrir"')
        self.assertContains(pagina, 'hx-get="' + self.url + '"')


class DecisionesPendientesTests(TestCase):
    def setUp(self):
        sg = UnidadNegocio.objects.get(codigo='SG')
        farmacia = Farmacia.objects.create(codigo='ML031', grupo=Grupo.objects.create(codigo='TRXP3'), unidad_negocio=sg)
        self.estacion = Estacion.objects.create(
            codigo='ML031-A', farmacia=farmacia, estado_aprobacion=Estacion.EstadoAprobacion.APROBADA,
        )
        self.usuario = User.objects.create_user(username='u_decide', password='x')
        PerfilUsuario.objects.create(usuario=self.usuario, acceso_todas_unidades=True)
        self.client.force_login(self.usuario)

    def test_aparece_solo_si_hay_algo(self):
        url = reverse('panel:dashboard_vivo_partial')
        self.assertNotContains(self.client.get(url), 'Esperan una decisión')

        orden = ordenes.crear_orden(estacion=self.estacion, tipo='software', origen_id=9, topico='t', repetible=True, ronda=1)
        OrdenAgente.objects.filter(pk=orden.pk).update(estado=OrdenAgente.Estado.INTERRUMPIDA)
        Despliegue.objects.create(
            version='4.3.0', unidad_negocio=self.estacion.farmacia.unidad_negocio, estado=Despliegue.Estado.PAUSADO,
            modo_aplicacion=Despliegue.ModoAplicacion.INMEDIATO, destino_tipo=Despliegue.DestinoTipo.ESTACIONES,
            creado_por=self.usuario,
        )
        resp = self.client.get(url)
        self.assertContains(resp, 'Esperan una decisión')
        self.assertEqual(resp.context['decisiones']['ordenes_inciertas'], 1)
        self.assertEqual(resp.context['decisiones']['despliegues_pausados'], 1)
