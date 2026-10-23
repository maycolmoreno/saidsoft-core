"""Pantallas que acompañan a la ficha de estación: lista de estaciones, alertas, detalle de
activo y cabecera de despliegue (bloque 1 a 4 de la evaluación de interfaz)."""
from datetime import timedelta

from django.contrib.auth.models import Permission, User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from apps.activos.models import Activo
from apps.catalogo.models import Estacion, Farmacia, Grupo, UnidadNegocio
from apps.cuentas.models import PerfilUsuario
from apps.despliegues.models import Despliegue
from apps.monitoreo.models import Alerta, Metrica, ReglaAlerta
from apps.panel.views.activos import _estado_garantia
from apps.panel.views.despliegues import _pasos_aprobacion


class Base(TestCase):
    def setUp(self):
        self.sg = UnidadNegocio.objects.get(codigo='SG')
        farmacia = Farmacia.objects.create(codigo='GSD15', grupo=Grupo.objects.create(codigo='TRXR1'), unidad_negocio=self.sg)
        self.estacion = Estacion.objects.create(
            codigo='GSD15-ADM', farmacia=farmacia, estado_aprobacion=Estacion.EstadoAprobacion.APROBADA,
            estado_conexion=Estacion.EstadoConexion.ONLINE, ultimo_heartbeat=timezone.now() - timedelta(minutes=2),
        )
        self.usuario = User.objects.create_user(username='u_relacionadas', password='x')
        PerfilUsuario.objects.create(usuario=self.usuario, acceso_todas_unidades=True)
        self.usuario.user_permissions.add(*Permission.objects.filter(codename__in=[
            'view_estacion', 'view_alerta', 'view_activo', 'view_despliegue',
        ]))
        self.client.force_login(self.usuario)

    def _alerta(self, severidad='critical', hace=timedelta(hours=30)):
        regla = ReglaAlerta.objects.create(
            nombre='CPU alta', metrica=Metrica.CPU_CARGA_PCT, operador=ReglaAlerta.Operador.GTE, umbral=90,
            duracion_minutos=10, creado_por=self.usuario, severidad=severidad,
        )
        alerta = Alerta.objects.create(regla=regla, estacion=self.estacion, valor_disparador=95)
        Alerta.objects.filter(pk=alerta.pk).update(abierta_en=timezone.now() - hace)
        return alerta


class ListaDeEstacionesTests(Base):
    def test_codigo_a_la_ficha_salud_y_latido(self):
        self._alerta('critical')
        resp = self.client.get(reverse('panel:estaciones_lista'))
        self.assertContains(resp, reverse('panel:estacion_ficha', args=[self.estacion.pk]))
        self.assertContains(resp, 'Vista rápida de GSD15-ADM')
        fila = resp.context['estaciones'][0]
        self.assertEqual(fila.salud.puntaje, 80)  # una crítica abierta
        self.assertEqual(fila.latido['tono'], 'good')
        self.assertContains(resp, 'En línea · hace 2')

    def test_latido_viejo_en_rojo(self):
        Estacion.objects.filter(pk=self.estacion.pk).update(ultimo_heartbeat=timezone.now() - timedelta(hours=3))
        fila = self.client.get(reverse('panel:estaciones_lista')).context['estaciones'][0]
        self.assertEqual(fila.latido['tono'], 'critical')


class AlertasTests(Base):
    def test_estacion_a_la_ficha_y_antiguedad_en_color(self):
        self._alerta('critical', hace=timedelta(hours=30))
        resp = self.client.get(reverse('panel:alertas_lista'))
        self.assertContains(resp, reverse('panel:estacion_ficha', args=[self.estacion.pk]))
        self.assertContains(resp, 'alerta-fila tono-critical')
        self.assertContains(resp, 'chip-edad tono-warning')  # más de 24 h


class DetalleDeActivoTests(Base):
    def test_garantia(self):
        hoy = timezone.localdate()
        self.assertEqual(_estado_garantia(None)['tono'], 'neutral')
        self.assertEqual(_estado_garantia(hoy - timedelta(days=5)), {'texto': 'Vencida hace 5 días', 'tono': 'critical'})
        self.assertEqual(_estado_garantia(hoy + timedelta(days=30))['tono'], 'warning')
        self.assertEqual(_estado_garantia(hoy + timedelta(days=400))['tono'], 'good')

    def test_enlaza_a_la_ficha_y_usa_la_linea_de_vida(self):
        activo = Activo.objects.create(codigo='CR-DSK-0202', tipo=Activo.Tipo.DESKTOP, estacion=self.estacion,
                                       unidad_negocio=self.sg, vencimiento_garantia=timezone.localdate() - timedelta(days=2))
        resp = self.client.get(reverse('panel:activo_detalle', args=[activo.pk]))
        self.assertContains(resp, reverse('panel:estacion_ficha', args=[self.estacion.pk]))
        self.assertContains(resp, 'Vencida hace 2 días')
        self.assertContains(resp, 'class="vida"')


class CabeceraDeDespliegueTests(Base):
    def _despliegue(self, **extra):
        return Despliegue.objects.create(
            version='4.3.0', archivo=SimpleUploadedFile('pkg.zip', b'x'), unidad_negocio=self.sg,
            modo_aplicacion=Despliegue.ModoAplicacion.INMEDIATO, destino_tipo=Despliegue.DestinoTipo.ESTACIONES,
            creado_por=self.usuario, **extra,
        )

    def test_pasos_segun_el_estado(self):
        pendiente = _pasos_aprobacion(self._despliegue(estado=Despliegue.Estado.PENDIENTE_APROBACION))
        self.assertEqual([p['tono'] for p in pendiente], ['good', 'accent', 'neutral', 'neutral'])
        self.assertEqual(pendiente[1]['detalle'], 'falta una segunda persona')

        otro = User.objects.create_user(username='aprueba', password='x')
        frenado = _pasos_aprobacion(self._despliegue(
            estado=Despliegue.Estado.PAUSADO, aprobado_por=otro, fecha_publicacion=timezone.now(),
        ))
        self.assertEqual([p['tono'] for p in frenado], ['good', 'good', 'good', 'critical'])
        self.assertEqual(frenado[3]['titulo'], 'Frenado')

    def test_la_pagina_muestra_los_pasos(self):
        d = self._despliegue(estado=Despliegue.Estado.PENDIENTE_APROBACION)
        resp = self.client.get(reverse('panel:despliegue_detalle', args=[d.pk]))
        self.assertContains(resp, 'Cadena del despliegue')
        self.assertContains(resp, 'falta una segunda persona')


class PantallaDeParedTests(Base):
    def test_sin_menu_con_pantalla_completa_y_refresco(self):
        resp = self.client.get(reverse('panel:centro_monitoreo'))
        self.assertEqual(resp.status_code, 200)
        self.assertNotContains(resp, 'class="sidebar"')
        self.assertContains(resp, 'Pantalla completa')
        self.assertContains(resp, 'Volver al panel')

    def test_el_interior_trae_mosaico_y_que_atender(self):
        self._alerta('critical')
        resp = self.client.get(reverse('panel:centro_monitoreo_partial'))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'Estado por farmacia')
        self.assertContains(resp, 'Qué atender')
        self.assertIn('incidencias', resp.context)
        self.assertIn('mosaico', resp.context)
