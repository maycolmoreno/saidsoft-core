"""Las tres pantallas dicen el MISMO número de estaciones en línea.

Esta es la prueba que impide que vuelva el hallazgo D-01 de
docs/auditoria/centro-monitoreo/: hasta el 10-oct-2026 "estaciones en línea" se calculaba
de tres formas distintas en tres pantallas de la misma aplicación —5 minutos en el
tablero, 10 en el Centro, y el campo `estado_conexion` en el listado— así que una estación
con el último latido hace 7 minutos aparecía CAÍDA, EN LÍNEA y CAÍDA al mismo tiempo. Los
tres números eran defendibles por separado y se contradecían juntos.

Vive en un archivo propio y no en `tests.py` porque es una prueba de ACUERDO entre apps
(catalogo, monitoreo y panel), no de ninguna de las tres. Sigue el patrón y el motivo de
`EquivalenciaPredicadosEstacionTests` (en `apps/catalogo/tests.py`), que corre los dos
lados del mismo predicado sobre los mismos datos y falla si difieren:

    "Dos copias del mismo criterio se desincronizan, y el síntoma es silencioso: la
    tarjeta dice '12 desactualizadas', se la pulsa y salen 9. Nada falla; el número
    simplemente deja de ser confiable, y con él la pantalla."

Al cambiar el criterio hay que tocar un solo lugar —`apps.monitoreo.umbrales`— y esta
prueba es la que lo verifica.
"""
from datetime import timedelta

from django.contrib.auth.models import Permission, User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from apps.catalogo.models import Estacion, Farmacia, Grupo, UnidadNegocio
from apps.catalogo.services import estado_flota
from apps.cuentas.models import PerfilUsuario
from apps.monitoreo.services import resumen_operacion
from apps.monitoreo.umbrales import ALERTA_SIN_LATIDO_UMBRAL_MINUTOS, ONLINE_UMBRAL_MINUTOS


class EquivalenciaIndicadoresTests(TestCase):
    """Misma población, tres consumidores, un solo número."""

    def setUp(self):
        self.sg = UnidadNegocio.objects.get(codigo='SG')
        self.grupo = Grupo.objects.create(codigo='TRXEQ')
        self.farmacia = Farmacia.objects.create(
            codigo='EQ001', grupo=self.grupo, unidad_negocio=self.sg,
        )
        # El grupo PENDIENTE es el bucket de las farmacias cuyo nodo no se pudo resolver
        # (lo crea `importar_red_farmacias_xlsx`). `calcular_matriz_cumplimiento` lo
        # excluye —correcto para cumplimiento de versión— y el tablero sacaba de ahí su
        # KPI de flota, así que estas estaciones no existían para el número más visible
        # del panel (hallazgo D-02).
        self.grupo_pendiente = Grupo.objects.create(codigo='PENDIENTE')
        self.farmacia_sin_nodo = Farmacia.objects.create(
            codigo='EQ002', grupo=self.grupo_pendiente, unidad_negocio=self.sg,
        )

        ahora = timezone.now()
        self.viva = self._estacion('EQ001-A', self.farmacia, ahora)
        self.sin_nodo = self._estacion('EQ002-A', self.farmacia_sin_nodo, ahora)
        self.callada = self._estacion('EQ001-B', self.farmacia, ahora - timedelta(hours=3))
        self.nunca = self._estacion('EQ001-C', self.farmacia, None)
        # Pendiente de aprobación: no reporta porque todavía no le toca. No es flota.
        self.pendiente = self._estacion(
            'EQ001-D', self.farmacia, None, estado=Estacion.EstadoAprobacion.PENDIENTE,
        )

        self.usuario = User.objects.create_user(username='u_equivalencia', password='x')
        PerfilUsuario.objects.create(usuario=self.usuario, acceso_todas_unidades=True)
        self.usuario.user_permissions.add(
            Permission.objects.get(content_type__app_label='catalogo', codename='view_estacion'),
            Permission.objects.get(content_type__app_label='monitoreo', codename='view_alerta'),
        )
        self.client.force_login(self.usuario)

    def _estacion(self, codigo, farmacia, heartbeat, estado=Estacion.EstadoAprobacion.APROBADA):
        return Estacion.objects.create(
            codigo=codigo, farmacia=farmacia, estado_aprobacion=estado, ultimo_heartbeat=heartbeat,
        )

    # --- El acuerdo entre los tres consumidores ------------------------------------

    def test_el_tablero_y_el_centro_dicen_el_mismo_numero(self):
        """Es la prueba de fondo. Antes el tablero decía una cosa y el Centro otra."""
        tablero = self.client.get(reverse('panel:dashboard'))
        centro = self.client.get(reverse('panel:centro_monitoreo_partial'))

        self.assertEqual(
            (tablero.context['total_online'], tablero.context['total_estaciones']),
            (centro.context['r']['estaciones_en_linea'], centro.context['r']['estaciones_total']),
        )

    def test_los_tres_salen_de_la_misma_funcion(self):
        """Que coincidan hoy no alcanza: tienen que venir del MISMO cálculo, o el día que
        alguien ajuste uno el otro se queda con el viejo."""
        flota = estado_flota([self.sg])
        resumen = resumen_operacion([self.sg])
        tablero = self.client.get(reverse('panel:dashboard'))

        self.assertEqual(flota['en_linea'], resumen['estaciones_en_linea'])
        self.assertEqual(flota['total'], resumen['estaciones_total'])
        self.assertEqual(flota['en_linea'], tablero.context['total_online'])
        self.assertEqual(flota['total'], tablero.context['total_estaciones'])

    def test_una_estacion_en_la_zona_gris_no_se_contradice(self):
        """El caso exacto que delató el problema: un latido de hace 7 minutos caía entre
        los dos umbrales que convivían (5 y 10), así que el tablero la daba por caída y el
        Centro por viva, en el mismo instante."""
        self.assertLess(ALERTA_SIN_LATIDO_UMBRAL_MINUTOS, ONLINE_UMBRAL_MINUTOS)
        gris = (ALERTA_SIN_LATIDO_UMBRAL_MINUTOS + ONLINE_UMBRAL_MINUTOS) / 2
        self.viva.ultimo_heartbeat = timezone.now() - timedelta(minutes=gris)
        self.viva.save(update_fields=['ultimo_heartbeat'])

        tablero = self.client.get(reverse('panel:dashboard'))
        centro = self.client.get(reverse('panel:centro_monitoreo_partial'))

        self.assertEqual(
            tablero.context['total_online'], centro.context['r']['estaciones_en_linea'],
            'una estación en la zona gris vuelve a contarse distinto en cada pantalla',
        )

    # --- La población contada ------------------------------------------------------

    def test_no_cuenta_las_pendientes_de_aprobacion(self):
        """Una pendiente no reporta porque todavía no le toca. El tablero las sumaba al
        denominador, al lado de una tarjeta que las contaba aparte (hallazgo D-02)."""
        flota = estado_flota([self.sg])
        self.assertEqual(flota['total'], 4)  # viva, sin_nodo, callada, nunca
        self.assertNotIn(
            self.pendiente.pk,
            list(Estacion.objects.filter(
                estado_aprobacion=Estacion.EstadoAprobacion.APROBADA,
            ).values_list('pk', flat=True)),
        )

    def test_cuenta_las_estaciones_de_farmacias_sin_nodo_asignado(self):
        """El otro lado de D-02: salían del KPI porque la matriz de cumplimiento excluye
        el grupo PENDIENTE, y de ahí sacaba el tablero sus dos números."""
        flota = estado_flota([self.sg])
        self.assertEqual(flota['en_linea'], 2, 'la estación del grupo PENDIENTE también cuenta')

        from apps.catalogo.services import calcular_matriz_cumplimiento

        grupos = calcular_matriz_cumplimiento(UnidadNegocio.objects.filter(pk=self.sg.pk))
        self.assertNotIn(
            'PENDIENTE', [g.codigo for g in grupos],
            'la matriz de cumplimiento sigue excluyéndolo, que es lo correcto para ella',
        )

    def test_separa_nunca_reporto_de_dejaron_de_reportar(self):
        """Dos trabajos distintos: una instalación que nunca funcionó y una caída."""
        flota = estado_flota([self.sg])
        self.assertEqual(flota['nunca_reporto'], 1)
        self.assertEqual(flota['dejaron_de_reportar'], 1)
        self.assertEqual(
            flota['nunca_reporto'] + flota['dejaron_de_reportar'], flota['sin_reportar'],
            'las dos mitades tienen que sumar el total o una de las tres miente',
        )

    def test_el_tablero_y_el_centro_coinciden_en_el_desglose(self):
        tablero = self.client.get(reverse('panel:dashboard'))
        centro = self.client.get(reverse('panel:centro_monitoreo_partial'))

        self.assertEqual(
            (tablero.context['flota']['nunca_reporto'],
             tablero.context['flota']['dejaron_de_reportar']),
            (centro.context['r']['estaciones_nunca_reporto'],
             centro.context['r']['estaciones_dejaron_de_reportar']),
        )

    def test_las_administrativas_van_aparte(self):
        """Se apagan de noche: sumadas harían saltar "sin reportar" ~100 todas las noches
        y el número dejaría de significar "hay un problema"."""
        oficina = Farmacia.objects.create(
            codigo='EQADM', grupo=self.grupo, unidad_negocio=self.sg,
            tipo=Farmacia.Tipo.ADMINISTRATIVO,
        )
        self._estacion('EQADM-A', oficina, None)

        flota = estado_flota([self.sg])
        self.assertEqual(flota['admin_total'], 1)
        self.assertEqual(flota['admin_en_linea'], 0)
        self.assertEqual(flota['total'], 4, 'la administrativa no entra en el total de operación')
        self.assertEqual(flota['nunca_reporto'], 1, 'ni en el desglose de sin reportar')

    def test_informa_las_pausadas_sin_descontarlas(self):
        """Una pausada sigue latiendo por diseño —se quiere ver que está viva y bajo
        control— así que cuenta como en línea. Lo que faltaba era decir cuántas son
        (hallazgo D-09)."""
        self.viva.pausado = True
        self.viva.save(update_fields=['pausado'])

        flota = estado_flota([self.sg])
        self.assertEqual(flota['pausadas'], 1)
        self.assertEqual(flota['en_linea'], 2, 'sigue contando como en línea')

    # --- El umbral --------------------------------------------------------------------

    def test_el_umbral_sale_de_un_solo_lugar(self):
        """Si alguien vuelve a escribirlo en una vista, esto falla."""
        from pathlib import Path

        from django.conf import settings

        self.assertEqual(estado_flota([self.sg])['umbral_minutos'], ONLINE_UMBRAL_MINUTOS)

        raiz = Path(settings.BASE_DIR)
        vista = (raiz / 'apps' / 'panel' / 'views' / 'dashboard.py').read_text(encoding='utf-8')
        for linea in vista.splitlines():
            limpia = linea.strip()
            if limpia.startswith('#'):
                continue  # los comentarios cuentan la historia a propósito
            self.assertNotIn(
                'ONLINE_UMBRAL_MINUTOS =', limpia,
                'el umbral volvió a vivir en la vista: %r' % linea,
            )

    def test_la_pantalla_dice_con_que_umbral_cuenta(self):
        """Era lo que impedía notar la contradicción: ninguna de las tres pantallas decía
        cuál de los relojes estaba usando."""
        tablero = self.client.get(reverse('panel:dashboard'))
        centro = self.client.get(reverse('panel:centro_monitoreo_partial'))

        esperado = 'últimos %d min' % ONLINE_UMBRAL_MINUTOS
        self.assertContains(tablero, esperado)
        self.assertContains(centro, esperado)

    def test_el_umbral_de_alerta_es_mas_estricto_que_el_de_pantalla(self):
        """Son dos valores con nombre propio y motivo distinto, no un descuido: acá el
        valor ES la velocidad de detección, y una alerta que llega tarde no sirve."""
        self.assertLess(ALERTA_SIN_LATIDO_UMBRAL_MINUTOS, ONLINE_UMBRAL_MINUTOS)

    def test_marcar_offline_usa_el_umbral_de_alerta_y_no_el_de_pantalla(self):
        """Si arrastrara el de pantalla, la alerta `sin_heartbeat` se abriría cinco
        minutos más tarde que hoy — justo lo que no se quiso cambiar."""
        from apps.catalogo.services import marcar_estaciones_offline

        entre_los_dos = (ALERTA_SIN_LATIDO_UMBRAL_MINUTOS + ONLINE_UMBRAL_MINUTOS) / 2
        self.viva.estado_conexion = Estacion.EstadoConexion.ONLINE
        self.viva.ultimo_heartbeat = timezone.now() - timedelta(minutes=entre_los_dos)
        self.viva.save(update_fields=['estado_conexion', 'ultimo_heartbeat'])

        marcar_estaciones_offline()

        self.viva.refresh_from_db()
        self.assertEqual(
            self.viva.estado_conexion, Estacion.EstadoConexion.OFFLINE,
            'con el umbral de alerta (5 min) esta estación ya debería estar offline',
        )
