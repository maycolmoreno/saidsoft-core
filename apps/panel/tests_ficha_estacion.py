"""Ficha completa de una estación (/estaciones/<pk>/) y su lógica (apps.panel.ficha_estacion)."""
from datetime import timedelta
from types import SimpleNamespace

from django.contrib.auth.models import Permission, User
from django.test import SimpleTestCase, TestCase
from django.urls import reverse
from django.utils import timezone

from apps.auditoria.models import registrar_evento
from apps.catalogo import ordenes
from apps.catalogo.models import Estacion, Farmacia, Grupo, OrdenAgente, UnidadNegocio
from apps.cuentas.models import PerfilUsuario
from apps.monitoreo.models import Alerta, EventoMonitoreo, Metrica, MuestraMetrica, ReglaAlerta
from apps.panel import ficha_estacion as ficha


def _estacion(conexion='online', version='agente-prueba-0.38'):
    return SimpleNamespace(estado_conexion=conexion, EstadoConexion=Estacion.EstadoConexion, version_agente=version)


def _alerta(severidad):
    return SimpleNamespace(regla=SimpleNamespace(severidad=severidad))


class SaludTests(SimpleTestCase):
    def _salud(self, estacion=None, alertas=(), ultima='agente-prueba-0.38', catalogos=(), disco=None):
        return ficha.salud(
            estacion or _estacion(), alertas_abiertas=list(alertas),
            ultima_version_agente=SimpleNamespace(version=ultima) if ultima else None,
            catalogos=list(catalogos), disco_pct=disco,
        )

    def test_sin_problemas_es_100_y_verde(self):
        s = self._salud()
        self.assertEqual((s.puntaje, s.tono, s.motivos), (100, ficha.BIEN, []))

    def test_cada_motivo_resta_lo_suyo_y_se_explica(self):
        s = self._salud(
            estacion=_estacion(conexion='offline', version='agente-prueba-0.37'),
            alertas=[_alerta('critical'), _alerta('warning')],
            catalogos=[('Servicios', 'abc', False)], disco=93,
        )
        self.assertEqual(s.puntaje, 100 - 40 - 20 - 10 - 10 - 10 - 10)
        self.assertEqual(s.tono, ficha.CRITICO)
        self.assertEqual(len(s.motivos), 6)

    def test_las_alertas_tienen_tope_y_el_puntaje_no_baja_de_cero(self):
        s = self._salud(estacion=_estacion(conexion='offline'), alertas=[_alerta('critical')] * 5 + [_alerta('warning')] * 5)
        restado = dict(s.motivos)
        self.assertEqual(restado['5 alerta(s) crítica(s) abierta(s)'], ficha.TOPE_CRITICAS)
        self.assertEqual(restado['5 advertencia(s) abierta(s)'], ficha.TOPE_ADVERTENCIAS)
        self.assertEqual(s.puntaje, 0)

    def test_un_catalogo_sin_dato_no_resta(self):
        self.assertEqual(self._salud(catalogos=[('Servicios', '', None)]).puntaje, 100)


class EdadYRecursosTests(SimpleTestCase):
    def test_edad(self):
        ahora = timezone.now()
        self.assertEqual(ficha.edad(None), {'texto': 'nunca', 'tono': ficha.NEUTRO})
        self.assertEqual(ficha.edad(ahora - timedelta(hours=3), ahora=ahora)['tono'], ficha.BIEN)
        vieja = ficha.edad(ahora - timedelta(days=9), ahora=ahora)
        self.assertEqual(vieja['tono'], ficha.ATENCION)
        self.assertTrue(vieja['texto'].startswith('hace '))

    def test_recursos(self):
        muestra = SimpleNamespace(cpu_carga_pct=94.4, ram_usada=4000, ram_total=8000, disco_total_gb=100, disco_libre_gb=4)
        filas = {r['nombre']: (r['pct'], r['tono']) for r in ficha.recursos(muestra)}
        self.assertEqual(filas, {'CPU': (94, ficha.ATENCION), 'Memoria': (50, ficha.BIEN), 'Disco C:': (96, ficha.CRITICO)})
        self.assertEqual(ficha.recursos(None), [])


class FichaVistaTests(TestCase):
    def setUp(self):
        self.sg = UnidadNegocio.objects.get(codigo='SG')
        farmacia = Farmacia.objects.create(codigo='GSD15', grupo=Grupo.objects.create(codigo='TRXF1'), unidad_negocio=self.sg)
        self.estacion = Estacion.objects.create(
            codigo='GSD15-ADM', farmacia=farmacia, estado_aprobacion=Estacion.EstadoAprobacion.APROBADA,
            estado_conexion=Estacion.EstadoConexion.ONLINE, numero_serie='T4ARYZ0041549DJ',
        )
        self.usuario = User.objects.create_user(username='u_ficha', password='x')
        self.perfil = PerfilUsuario.objects.create(usuario=self.usuario, acceso_todas_unidades=True)
        self.usuario.user_permissions.add(Permission.objects.get(codename='view_estacion'))
        self.client.force_login(self.usuario)
        self.url = reverse('panel:estacion_ficha', args=[self.estacion.pk])

    def _sembrar_historia(self):
        regla = ReglaAlerta.objects.create(
            nombre='CPU alta', metrica=Metrica.CPU_CARGA_PCT, operador=ReglaAlerta.Operador.GTE, umbral=90,
            duracion_minutos=10, creado_por=self.usuario,
        )
        Alerta.objects.create(regla=regla, estacion=self.estacion, valor_disparador=94)
        orden = ordenes.crear_orden(estacion=self.estacion, tipo='software', origen_id=5, topico='t', repetible=True, ronda=1)
        OrdenAgente.objects.filter(pk=orden.pk).update(estado=OrdenAgente.Estado.COMPLETADA, finalizada_en=timezone.now())
        EventoMonitoreo.objects.create(estacion=self.estacion, fuente='mqtt', en_linea=False)
        registrar_evento(usuario=self.usuario, accion='estacion.reiniciar', objeto=self.estacion)
        MuestraMetrica.objects.create(estacion=self.estacion, cpu_carga_pct=94, ram_total=8000, ram_usada=4000)

    def test_muestra_salud_recursos_y_linea_de_vida(self):
        self._sembrar_historia()
        resp = self.client.get(self.url)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.context['salud'].puntaje, 90)  # una advertencia abierta
        for texto in ('GSD15-ADM', 'Índice de salud 90 de 100', 'Alerta: CPU alta', 'Quedó fuera de línea',
                      'Reinicio enviado', 'Acciones y detalle', 'T4ARYZ0041549DJ'):
            self.assertContains(resp, texto)
        self.assertContains(resp, '#5: completada')

    def test_el_filtro_deja_solo_esa_categoria_y_uno_invalido_se_ignora(self):
        self._sembrar_historia()
        resp = self.client.get(self.url, {'ver': 'alertas'})
        self.assertEqual({e.categoria for e in resp.context['eventos']}, {'alertas'})
        self.assertEqual(self.client.get(self.url, {'ver': 'cualquiera'}).context['categoria'], '')

    def test_sin_historia_no_se_rompe(self):
        resp = self.client.get(self.url)
        self.assertContains(resp, 'Nada registrado en los últimos 30 días.')
        self.assertContains(resp, 'todavía no envió métricas')

    def test_otra_unidad_de_negocio_no_entra(self):
        self.perfil.acceso_todas_unidades = False
        self.perfil.save()
        self.assertEqual(self.client.get(self.url).status_code, 403)

    def test_sin_permiso_no_entra(self):
        self.usuario.user_permissions.clear()
        self.assertEqual(self.client.get(self.url).status_code, 403)

    def test_se_llega_desde_la_lista_y_desde_la_ventana(self):
        self.assertContains(self.client.get(reverse('panel:estaciones_lista')), self.url)
        self.assertContains(self.client.get(reverse('panel:estacion_info_modal', args=[self.estacion.pk])), self.url)
