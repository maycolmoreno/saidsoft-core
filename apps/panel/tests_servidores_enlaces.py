"""Monitoreo de servidores con rango (24 h / 7 / 30 días) y enlaces de farmacia con la
franja de disponibilidad de 24 h. Ver apps/panel/series_rango.py y disponibilidad.py."""
from datetime import timedelta

from django.contrib.auth.models import Permission, User
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from apps.catalogo.models import Estacion, Farmacia, Grupo, UnidadNegocio
from apps.cuentas.models import PerfilUsuario
from apps.monitoreo.models import EstadoEnlaceFarmacia, EventoEnlaceFarmacia, MuestraMetrica
from apps.panel import disponibilidad
from apps.panel.series_rango import RANGOS, series_en_rango


def _usuario(nombre, *codenames):
    u = User.objects.create_user(username=nombre, password='x')
    PerfilUsuario.objects.create(usuario=u, acceso_todas_unidades=True)
    u.user_permissions.add(*Permission.objects.filter(codename__in=codenames))
    return u


class ServidoresConRangoTests(TestCase):
    def setUp(self):
        self.farmacia = Farmacia.objects.create(
            codigo='ML001', grupo=Grupo.objects.create(codigo='TRX001'),
            unidad_negocio=UnidadNegocio.objects.get(codigo='SG'),
        )
        self.estacion = Estacion.objects.create(
            codigo='ML001-SRV', farmacia=self.farmacia, monitorear_recursos=True,
            estado_aprobacion=Estacion.EstadoAprobacion.APROBADA,
        )
        self.client.force_login(_usuario('u_srv', 'view_muestrametrica'))

    def _muestra(self, hace, cpu=30.0, ram_pct=50, disco_pct=40.0, estacion=None):
        # La fecha va en el INSERT: en TimescaleDB un UPDATE no puede mover la fila de chunk.
        return MuestraMetrica.objects.create(
            estacion=estacion or self.estacion, cpu_carga_pct=cpu, ram_total=1000, ram_usada=ram_pct * 10,
            disco_total_gb=100.0, disco_libre_gb=100.0 - disco_pct, temperatura_c=58.0,
            timestamp=timezone.now() - hace,
        )

    def test_agrupa_cada_10_minutos_y_deja_huecos_donde_no_hubo_muestras(self):
        from apps.panel.series_rango import _inicio

        ahora = timezone.now()
        rango = RANGOS['24h']
        # Dos muestras dentro del MISMO cubo de 10 minutos, hace ~5 h (se calcula desde
        # el inicio del cubo para que no caigan a los dos lados de un borde).
        cubo = _inicio(rango, ahora) + rango.paso * 114
        for minuto, cpu in ((1, 20), (3, 40)):
            MuestraMetrica.objects.create(
                estacion=self.estacion, cpu_carga_pct=cpu, ram_total=1000, ram_usada=500,
                disco_total_gb=100.0, disco_libre_gb=60.0, timestamp=cubo + timedelta(minutes=minuto),
            )
        self._muestra(timedelta(seconds=30), cpu=70)
        serie = series_en_rango([self.estacion.pk], rango, ahora)[self.estacion.pk]
        cpu = serie['cpu_carga_pct']
        self.assertEqual(len(cpu), 144)
        self.assertEqual(cpu[-1], 70)
        self.assertEqual(cpu[114], 30)  # promedio de 20 y 40
        self.assertEqual(sum(1 for v in cpu if v is not None), 2)
        self.assertEqual(serie['cpu_pico'], 70)

    def test_un_equipo_que_empezo_hace_40_minutos_ya_tiene_curva(self):
        """Con un punto por hora mostraba un solo punto y ninguna línea (ML001-C)."""
        for minutos in range(1, 41):
            self._muestra(timedelta(minutes=minutos))
        cpu = series_en_rango([self.estacion.pk], RANGOS['24h'])[self.estacion.pk]['cpu_carga_pct']
        self.assertGreaterEqual(sum(1 for v in cpu if v is not None), 4)

    def test_el_pico_es_el_maximo_real_no_el_maximo_de_los_promedios(self):
        self._muestra(timedelta(minutes=10), cpu=100)
        self._muestra(timedelta(minutes=9), cpu=10)
        serie = series_en_rango([self.estacion.pk], RANGOS['24h'])[self.estacion.pk]
        self.assertEqual(serie['cpu_pico'], 100)

    def test_siete_y_treinta_dias_tienen_su_cantidad_de_puntos(self):
        self._muestra(timedelta(days=6), cpu=10)
        self._muestra(timedelta(days=20), cpu=10)
        siete = series_en_rango([self.estacion.pk], RANGOS['7d'])[self.estacion.pk]['cpu_carga_pct']
        treinta = series_en_rango([self.estacion.pk], RANGOS['30d'])[self.estacion.pk]['cpu_carga_pct']
        self.assertEqual(len(siete), 168)
        self.assertEqual(sum(1 for v in siete if v is not None), 1)  # la de 20 días queda fuera
        self.assertEqual(len(treinta), 120)
        self.assertEqual(sum(1 for v in treinta if v is not None), 2)

    def test_la_tarjeta_toma_el_estado_de_la_peor_metrica(self):
        self._muestra(timedelta(minutes=1), cpu=20, disco_pct=95.0)
        resp = self.client.get(reverse('panel:monitoreo_lista'))
        tarjeta = resp.context['tarjetas'][0]
        self.assertEqual([g.etiqueta for g in tarjeta['graficas']], ['CPU', 'Memoria', 'Disco C:'])
        self.assertEqual(tarjeta['estado_tarjeta']['clave'], 'critical')
        self.assertContains(resp, 'Sobre el umbral')
        self.assertContains(resp, 'servidor-tarjeta tono-critical')

    def test_el_selector_de_rango_cambia_la_ventana_y_conserva_la_busqueda(self):
        self._muestra(timedelta(days=3), cpu=15)
        resp = self.client.get(reverse('panel:monitoreo_lista'), {'rango': '7d', 'q': 'ML001'})
        self.assertEqual(resp.context['rango'].clave, '7d')
        self.assertEqual(resp.context['tarjetas'][0]['cpu_pico'], 15)
        self.assertContains(resp, '?rango=30d&amp;q=ML001')
        # Un rango desconocido no rompe: vuelve a 24 h.
        self.assertEqual(self.client.get(reverse('panel:monitoreo_lista'), {'rango': 'x'}).context['rango'].clave, '24h')

    def test_el_pie_muestra_temperatura_y_pico(self):
        self._muestra(timedelta(minutes=1), cpu=42)
        resp = self.client.get(reverse('panel:monitoreo_lista'))
        self.assertContains(resp, '58 °C')
        self.assertContains(resp, 'Pico CPU 24 h')

    def test_el_pie_muestra_la_latencia_al_nodo_y_no_inventa_guiones(self):
        """La latencia que sirve en una caja es la de su base del POS (pg_local = el
        nodo). Latencia y Red del agente vienen vacías en las cajas: no se muestran con
        un guion que parezca dato."""
        from apps.monitoreo.models import EstadoServicioPos

        self._muestra(timedelta(minutes=1))
        EstadoServicioPos.objects.create(
            estacion=self.estacion, servicio='pg_local', disponible=True, latencia_ms=94,
            ultima_verificacion=timezone.now(),
        )
        resp = self.client.get(reverse('panel:monitoreo_lista'))
        self.assertEqual(resp.context['tarjetas'][0]['latencia_nodo'], 94)
        self.assertContains(resp, 'Nodo POS <b>94 ms</b>', html=False)
        self.assertNotContains(resp, 'Latencia <b')

    def test_las_series_del_rango_no_suman_una_consulta_por_servidor(self):
        self._muestra(timedelta(minutes=1))
        with CaptureQueriesContext(connection) as ctx:
            self.client.get(reverse('panel:monitoreo_lista'), {'rango': '30d'})
        con_uno = len(ctx.captured_queries)
        for i in range(4):
            otra = Estacion.objects.create(
                codigo=f'ML001-S{i}', farmacia=self.farmacia, monitorear_recursos=True,
                estado_aprobacion=Estacion.EstadoAprobacion.APROBADA,
            )
            self._muestra(timedelta(days=i + 1), estacion=otra)
        with CaptureQueriesContext(connection) as ctx:
            self.client.get(reverse('panel:monitoreo_lista'), {'rango': '30d'})
        self.assertEqual(len(ctx.captured_queries), con_uno)


class DisponibilidadCalculoTests(TestCase):
    def test_porcentaje_exacto_y_franja(self):
        ahora = timezone.now()
        caida = [(ahora - timedelta(hours=3), ahora - timedelta(hours=2, minutes=30))]
        self.assertEqual(disponibilidad.pct_disponible(caida), round(100 * (1 - 1800 / 86400), 1))
        estados = [b['estado'] for b in disponibilidad.franja(caida, ahora)]
        self.assertEqual(len(estados), 48)
        self.assertIn('caido', estados)
        self.assertLessEqual(estados.count('caido'), 2)

    def test_tramos_superpuestos_no_cuentan_dos_veces(self):
        ahora = timezone.now()
        a = ahora - timedelta(hours=2)
        dos = [(a, a + timedelta(hours=1)), (a + timedelta(minutes=30), a + timedelta(hours=1))]
        self.assertEqual(disponibilidad.segundos_caido(dos), 3600)

    def test_tonos(self):
        self.assertEqual(disponibilidad.tono_disponibilidad(94.9), 'critical')
        self.assertEqual(disponibilidad.tono_disponibilidad(98.5), 'warning')
        self.assertEqual(disponibilidad.tono_disponibilidad(99.5), 'good')
        self.assertEqual(disponibilidad.tono_disponibilidad(None), 'neutral')

    def test_sin_sondeo_no_inventa_un_porcentaje(self):
        r = disponibilidad.resumen_de_fila([], sondeada=False)
        self.assertIsNone(r['pct'])
        self.assertEqual({b['estado'] for b in r['franja']}, {'sin'})


class EnlacesConDisponibilidadTests(TestCase):
    def setUp(self):
        grupo = Grupo.objects.create(codigo='TRX001')
        sg = UnidadNegocio.objects.get(codigo='SG')
        self.ahora = timezone.now()

        def farmacia(codigo, ip, tipo, alcanzable=True, respondio=True):
            f = Farmacia.objects.create(codigo=codigo, grupo=grupo, unidad_negocio=sg, ip_router=ip, tipo_enlace=tipo)
            EstadoEnlaceFarmacia.objects.create(
                farmacia=f, alcanzable=alcanzable, respondio_alguna_vez=respondio,
                ultima_verificacion=self.ahora, latencia_ms=30.0 if alcanzable else None,
            )
            return f

        self.sana = farmacia('ML001', '10.0.0.1', 'TELCONET')
        self.inestable = farmacia('ML002', '10.0.0.2', 'TELCONET')
        self.mala = farmacia('ML003', '10.0.0.3', 'PUNTO NET')
        self.caida = farmacia('ML004', '10.0.0.4', 'CNT', alcanzable=False)
        self.nunca = farmacia('ML005', '10.0.0.5', 'CNT', alcanzable=False, respondio=False)

        def evento(f, desde_h, hasta_h=None):
            EventoEnlaceFarmacia.objects.create(
                farmacia=f, inicio=self.ahora - timedelta(hours=desde_h),
                fin=None if hasta_h is None else self.ahora - timedelta(hours=hasta_h),
            )

        evento(self.inestable, 5, 4.5)       # 30 min caída: 97,9 %
        evento(self.mala, 10, 6)             # 4 h caída: 83,3 %
        evento(self.caida, 3)                # caída en curso hace 3 h
        evento(self.sana, 40, 30)            # fuera de la ventana: no cuenta
        evento(self.nunca, 2)                # nunca respondió: no entra al promedio
        self.client.force_login(_usuario('u_enl', 'view_estadoenlacefarmacia'))

    def _get(self, **params):
        return self.client.get(reverse('panel:enlaces_farmacias_lista'), params)

    def test_orden_caidas_primero_y_despues_menor_disponibilidad(self):
        codigos = [f['farmacia'].codigo for f in self._get().context['filas']]
        self.assertEqual(codigos[:4], ['ML004', 'ML003', 'ML002', 'ML001'])

    def test_cada_fila_trae_su_franja_y_su_porcentaje(self):
        filas = {f['farmacia'].codigo: f['disponibilidad'] for f in self._get().context['filas']}
        self.assertEqual(filas['ML001']['pct'], 100.0)
        self.assertEqual(filas['ML002']['pct'], 97.9)
        self.assertEqual(filas['ML002']['tono'], 'warning')
        self.assertEqual(filas['ML003']['tono'], 'critical')
        self.assertEqual(filas['ML004']['franja'][-1]['estado'], 'caido')
        self.assertIsNone(filas['ML005']['pct'])

    def test_kpis_de_24_horas(self):
        resp = self._get()
        # 4 farmacias medibles (ML005 nunca respondió): caídas 0,5 + 4 + 3 h.
        esperado = round(100 - (100 * 7.5 / 24) / 4, 2)
        self.assertEqual(resp.context['medibles'], 4)
        self.assertAlmostEqual(resp.context['disponibilidad_red'], esperado, places=1)
        self.assertAlmostEqual(
            (self.ahora - resp.context['caida_mas_vieja']).total_seconds(), 3 * 3600, delta=5,
        )
        proveedores = {p['nombre']: p['n'] for p in resp.context['caidas_por_proveedor']}
        self.assertEqual(proveedores, {'TELCONET': 1, 'PUNTO NET': 1, 'CNT': 2})
        self.assertContains(resp, 'Disponibilidad de la red · 24 h')
        self.assertContains(resp, 'franja-caido')

    def test_el_orden_respeta_los_filtros(self):
        codigos = [f['farmacia'].codigo for f in self._get(estado='activos').context['filas']]
        self.assertEqual(codigos, ['ML003', 'ML002', 'ML001'])


class HistorialDeEnlaceTests(TestCase):
    """Latencia y pérdida de cada sondeo de enlace (MuestraEnlaceFarmacia, monitoreo 0051)."""

    def setUp(self):
        self.grupo = Grupo.objects.create(codigo='TRX006')
        self.sg = UnidadNegocio.objects.get(codigo='SG')
        self.farmacia = Farmacia.objects.create(
            codigo='ML001', grupo=self.grupo, unidad_negocio=self.sg, ip_router='10.0.0.1', tipo_enlace='TELCONET',
        )

    def _ping(self, salida, returncode=0):
        from unittest.mock import patch

        from apps.monitoreo.enlaces import sondear_enlace

        with patch('apps.monitoreo.enlaces.subprocess.run') as mock_run:
            mock_run.return_value.returncode = returncode
            mock_run.return_value.stdout = salida
            return sondear_enlace('10.0.0.1')

    def test_cuenta_los_paquetes_perdidos_y_el_pico(self):
        r = self._ping(
            '64 bytes from 10.0.0.1: icmp_seq=1 ttl=61 time=40.0 ms\n'
            '64 bytes from 10.0.0.1: icmp_seq=3 ttl=61 time=800.0 ms\n'
        )
        # Sigue siendo la tupla de siempre para quien la desempaqueta...
        self.assertEqual(r, (True, 40.0))
        # ...con la calidad del sondeo aparte.
        self.assertEqual(r.perdida_pct, 33.3)
        self.assertEqual(r.latencia_max_ms, 800.0)
        self.assertEqual(r.latencia_promedio_ms, 420.0)

    def test_en_windows_en_espanol_tambien(self):
        r = self._ping(
            'Respuesta desde 10.0.0.1: bytes=32 tiempo=12ms TTL=61\n'
            'Respuesta desde 10.0.0.1: bytes=32 tiempo=14ms TTL=61\n'
            'Respuesta desde 10.0.0.1: bytes=32 tiempo<1m TTL=61\n'
        )
        self.assertEqual(r.perdida_pct, 0.0)

    def test_sin_respuesta_es_perdida_total(self):
        r = self._ping('', returncode=1)
        self.assertEqual(r, (False, None))
        self.assertEqual(r.perdida_pct, 100.0)

    def _barrer(self, resultado, farmacias=None):
        from unittest.mock import patch

        from apps.monitoreo import enlaces

        with patch.object(enlaces, 'verificar_ping_disponible'):
            with patch.object(enlaces, 'sondear_enlace', return_value=resultado):
                return enlaces.sondear_enlaces_farmacias(farmacias or [self.farmacia])

    def test_el_barrido_guarda_una_muestra_por_farmacia(self):
        from apps.monitoreo.enlaces import ResultadoSondeo
        from apps.monitoreo.models import MuestraEnlaceFarmacia

        r = ResultadoSondeo(True, 40.0, 33.3, 800.0)
        r.latencia_promedio_ms = 420.0
        self._barrer(r)
        m = MuestraEnlaceFarmacia.objects.get()
        self.assertEqual(
            (m.farmacia, m.latencia_ms, m.latencia_max_ms, m.perdida_pct), (self.farmacia, 420.0, 800.0, 33.3),
        )

    def test_acepta_la_tupla_de_siempre(self):
        """Lo que devuelve un sondeo sustituido (o la API) sigue sirviendo: sin pérdida
        conocida se guarda None."""
        from apps.monitoreo.models import MuestraEnlaceFarmacia

        otra = Farmacia.objects.create(codigo='ML002', grupo=self.grupo, unidad_negocio=self.sg, ip_router='10.0.0.2')
        self._barrer((True, 30.0), [self.farmacia, otra])
        self.assertEqual(list(MuestraEnlaceFarmacia.objects.values_list('perdida_pct', flat=True)), [None, None])

    def test_un_barrido_abortado_no_guarda_nada(self):
        """Si falló casi todo, el problema es la ruta desde el servidor: guardar 100 % de
        pérdida en cada farmacia ensuciaría la evidencia con un fallo nuestro."""
        from apps.monitoreo.models import MuestraEnlaceFarmacia

        resumen = self._barrer((False, None))
        self.assertTrue(resumen['abortado'])
        self.assertFalse(MuestraEnlaceFarmacia.objects.exists())

    def _muestra(self, hace, latencia=40.0, maxima=None, perdida=0.0):
        from apps.monitoreo.models import MuestraEnlaceFarmacia

        return MuestraEnlaceFarmacia.objects.create(
            farmacia=self.farmacia, timestamp=timezone.now() - hace,
            latencia_ms=latencia, latencia_max_ms=maxima or latencia, perdida_pct=perdida,
        )

    def test_la_franja_marca_degradado_por_perdida_o_por_pico(self):
        ahora = timezone.now()
        for minutos in range(2, 24 * 60, 2):
            self._muestra(timedelta(minutes=minutos))
        # Hace ~1 h: un sondeo que perdió 2 de 3. Hace ~5 h: un pico de 1,2 s (30 veces la mediana).
        self._muestra(timedelta(minutes=61), perdida=66.7)
        self._muestra(timedelta(hours=5, minutes=1), maxima=1200.0)
        muestras = disponibilidad.muestras_24h([self.farmacia.pk], ahora)[self.farmacia.pk]
        bloques = disponibilidad.franja([], ahora, muestras)
        estados = [b['estado'] for b in bloques]
        self.assertEqual(estados.count('degradado'), 2)
        self.assertNotIn('sin', estados)
        self.assertEqual(len([b for b in bloques if 'pico 1200 ms' in b['titulo']]), 1)

    def test_un_paquete_suelto_perdido_no_es_degradado(self):
        ahora = timezone.now()
        for minutos in range(2, 60, 2):
            self._muestra(timedelta(minutes=minutos))
        self._muestra(timedelta(minutes=31), perdida=33.3)
        muestras = disponibilidad.muestras_24h([self.farmacia.pk], ahora)[self.farmacia.pk]
        self.assertNotIn('degradado', [b['estado'] for b in disponibilidad.franja([], ahora, muestras)])

    def test_bloque_sin_sondeos_es_gris_pero_la_caida_manda(self):
        ahora = timezone.now()
        self._muestra(timedelta(minutes=5))
        muestras = disponibilidad.muestras_24h([self.farmacia.pk], ahora)[self.farmacia.pk]
        caida = [(ahora - timedelta(hours=10), ahora - timedelta(hours=9, minutes=50))]
        estados = [b['estado'] for b in disponibilidad.franja(caida, ahora, muestras)]
        self.assertEqual(estados[-1], 'arriba')
        self.assertIn('caido', estados)
        self.assertEqual(estados[0], 'sin')

    def test_la_fila_de_la_pantalla_trae_perdida_y_latencia_del_dia(self):
        EstadoEnlaceFarmacia.objects.create(
            farmacia=self.farmacia, alcanzable=True, respondio_alguna_vez=True,
            ultima_verificacion=timezone.now(), latencia_ms=40.0,
        )
        self._muestra(timedelta(minutes=10), latencia=40.0)
        self._muestra(timedelta(minutes=8), latencia=60.0, maxima=900.0, perdida=33.3)
        self._muestra(timedelta(minutes=6), perdida=100.0, latencia=None)
        self.client.force_login(_usuario('u_hist', 'view_estadoenlacefarmacia'))
        resp = self.client.get(reverse('panel:enlaces_farmacias_lista'))
        d = resp.context['filas'][0]['disponibilidad']
        # 0 y 33,3: el sondeo de caída total no cuenta (ya resta disponibilidad).
        self.assertEqual(d['perdida'], 16.6)
        self.assertEqual((d['latencia_mediana'], d['latencia_pico']), (50, 900))
        self.assertContains(resp, 'pérdida 16,6 %')
        self.assertContains(resp, 'franja-degradado')

    def test_purga_conserva_lo_reciente(self):
        from apps.monitoreo.models import MuestraEnlaceFarmacia
        from apps.monitoreo.services import purgar_muestras_enlace_antiguas

        self._muestra(timedelta(minutes=5))
        purgar_muestras_enlace_antiguas(dias=30)
        self.assertEqual(MuestraEnlaceFarmacia.objects.count(), 1)
