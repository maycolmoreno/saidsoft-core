"""Reglas de negocio del control de viáticos.

Lo que se fija acá es el CRITERIO, no la implementación: qué bloquea, qué solo avisa,
y qué NO debe levantar alerta -- ese último grupo es el que evita que la bandeja del
coordinador se llene de falsos positivos y deje de mirarse.
"""
import datetime
from decimal import Decimal

from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.test import TestCase

from apps.activos.models import Colaborador
from apps.catalogo.models import Farmacia, Grupo, UnidadNegocio
from apps.mantenimiento.models import VisitaTecnica
from apps.viaticos import services
from apps.viaticos.models import (
    AlertaViatico, ColaboradorZona, EstadoReporteViatico, ReporteViatico, RubroViatico, TipoAlertaViatico,
)


class BaseViaticos(TestCase):
    """Un técnico con zona asignada, una farmacia suya y una ajena."""

    def setUp(self):
        self.sg = UnidadNegocio.objects.get(codigo='SG')
        grupo = Grupo.objects.create(codigo='TRX001', version_objetivo='4.2.1')
        self.mia = Farmacia.objects.create(codigo='ML006', grupo=grupo, unidad_negocio=self.sg)
        self.ajena = Farmacia.objects.create(codigo='ML099', grupo=grupo, unidad_negocio=self.sg)

        self.usuario = User.objects.create_user(username='tecnico_viaticos', password='x')
        self.tecnico = Colaborador.objects.create(
            nombre='Ana Pérez', cedula='0912345678', unidad_negocio=self.sg, usuario=self.usuario,
        )
        self.zona = ColaboradorZona.objects.create(colaborador=self.tecnico, zona_cobertura='Machala Norte')
        self.zona.farmacias_asignadas.add(self.mia)

    def _reporte(self, **kwargs):
        datos = {
            'colaborador': self.tecnico, 'fecha': datetime.date(2026, 9, 10),
            'farmacia_visitada': self.mia, 'rubro': RubroViatico.ALIMENTACION,
            'monto': Decimal('4.00'),
        }
        datos.update(kwargs)
        return ReporteViatico.objects.create(**datos)


class ReglasQueBloqueanTests(BaseViaticos):
    """Están en `clean()` a propósito: son datos que no deberían poder existir."""

    def test_movilizacion_sin_origen_ni_destino_no_se_guarda(self):
        reporte = ReporteViatico(
            colaborador=self.tecnico, fecha=datetime.date(2026, 9, 10), farmacia_visitada=self.mia,
            rubro=RubroViatico.MOVILIZACION, monto=Decimal('10.00'),
        )
        with self.assertRaises(ValidationError) as ctx:
            reporte.full_clean()
        self.assertIn('origen', ctx.exception.message_dict)
        self.assertIn('destino', ctx.exception.message_dict)

    def test_movilizacion_con_origen_y_destino_pasa(self):
        reporte = ReporteViatico(
            colaborador=self.tecnico, fecha=datetime.date(2026, 9, 10), farmacia_visitada=self.mia,
            rubro=RubroViatico.MOVILIZACION, monto=Decimal('10.00'),
            origen='Machala', destino='Pasaje',
        )
        reporte.full_clean()  # no lanza

    def test_hospedaje_sin_origen_destino_no_se_bloquea(self):
        """La regla es solo de movilización: exigirla en todos los rubros haría
        imposible cargar un hospedaje."""
        reporte = ReporteViatico(
            colaborador=self.tecnico, fecha=datetime.date(2026, 9, 10), farmacia_visitada=self.mia,
            rubro=RubroViatico.HOSPEDAJE, monto=Decimal('30.00'),
        )
        reporte.full_clean()

    def test_reembolso_parcial_no_se_guarda(self):
        reporte = ReporteViatico(
            colaborador=self.tecnico, fecha=datetime.date(2026, 9, 10), farmacia_visitada=self.mia,
            rubro=RubroViatico.HOSPEDAJE, monto=Decimal('20.00'), total_factura=Decimal('30.00'),
        )
        with self.assertRaises(ValidationError) as ctx:
            reporte.full_clean()
        self.assertIn('monto', ctx.exception.message_dict)

    def test_monto_igual_al_total_de_factura_pasa(self):
        reporte = ReporteViatico(
            colaborador=self.tecnico, fecha=datetime.date(2026, 9, 10), farmacia_visitada=self.mia,
            rubro=RubroViatico.HOSPEDAJE, monto=Decimal('30.00'), total_factura=Decimal('30.00'),
        )
        reporte.full_clean()


class AlertaFueraDeZonaTests(BaseViaticos):
    """El caso real que originó el módulo."""

    def test_farmacia_de_otra_zona_levanta_alerta(self):
        reporte = self._reporte(farmacia_visitada=self.ajena)
        services.evaluar_alertas(reporte)
        alerta = reporte.alertas.get(tipo_alerta=TipoAlertaViatico.FUERA_DE_ZONA)
        self.assertIn('ML099', alerta.detalle)
        self.assertFalse(alerta.resuelta)

    def test_farmacia_asignada_no_levanta_alerta(self):
        reporte = self._reporte(farmacia_visitada=self.mia)
        services.evaluar_alertas(reporte)
        self.assertFalse(reporte.alertas.filter(tipo_alerta=TipoAlertaViatico.FUERA_DE_ZONA).exists())

    def test_el_detalle_dice_a_quien_le_corresponde(self):
        """Es la primera pregunta del coordinador; tenerla en la alerta le evita
        buscarla farmacia por farmacia."""
        otro = Colaborador.objects.create(nombre='Luis Gómez', cedula='0999999999', unidad_negocio=self.sg)
        zona_otro = ColaboradorZona.objects.create(colaborador=otro, zona_cobertura='Machala Sur')
        zona_otro.farmacias_asignadas.add(self.ajena)

        reporte = self._reporte(farmacia_visitada=self.ajena)
        services.evaluar_alertas(reporte)
        detalle = reporte.alertas.get(tipo_alerta=TipoAlertaViatico.FUERA_DE_ZONA).detalle
        self.assertIn('Luis Gómez', detalle)
        self.assertIn('Machala Sur', detalle)

    def test_sin_zona_asignada_no_inventa_alerta(self):
        """Sin zona no hay contra qué comparar. Una alerta falsa es peor que ninguna:
        el coordinador deja de mirarlas."""
        self.zona.delete()
        reporte = self._reporte(farmacia_visitada=self.ajena)
        services.evaluar_alertas(reporte)
        self.assertFalse(reporte.alertas.filter(tipo_alerta=TipoAlertaViatico.FUERA_DE_ZONA).exists())

    def test_zona_sin_farmacias_cargadas_no_inventa_alerta(self):
        self.zona.farmacias_asignadas.clear()
        reporte = self._reporte(farmacia_visitada=self.ajena)
        services.evaluar_alertas(reporte)
        self.assertFalse(reporte.alertas.filter(tipo_alerta=TipoAlertaViatico.FUERA_DE_ZONA).exists())


class AlertaTopeTests(BaseViaticos):

    def test_hospedaje_sobre_30_levanta_alerta(self):
        reporte = self._reporte(rubro=RubroViatico.HOSPEDAJE, monto=Decimal('45.00'))
        services.evaluar_alertas(reporte)
        alerta = reporte.alertas.get(tipo_alerta=TipoAlertaViatico.EXCEDE_TOPE)
        self.assertIn('30.00', alerta.detalle)
        self.assertIn('por noche', alerta.detalle)

    def test_exactamente_en_el_tope_no_alerta(self):
        """El tope es el máximo permitido, no el primer valor prohibido."""
        reporte = self._reporte(rubro=RubroViatico.HOSPEDAJE, monto=Decimal('30.00'))
        services.evaluar_alertas(reporte)
        self.assertFalse(reporte.alertas.filter(tipo_alerta=TipoAlertaViatico.EXCEDE_TOPE).exists())

    def test_cada_rubro_usa_su_propio_tope(self):
        # $10 pasa en movilización (tope $25) y no en alimentación (tope $4).
        movilizacion = self._reporte(
            rubro=RubroViatico.MOVILIZACION, monto=Decimal('10.00'), origen='A', destino='B',
        )
        alimentacion = self._reporte(rubro=RubroViatico.ALIMENTACION, monto=Decimal('10.00'))
        services.evaluar_alertas(movilizacion)
        services.evaluar_alertas(alimentacion)
        self.assertFalse(movilizacion.alertas.filter(tipo_alerta=TipoAlertaViatico.EXCEDE_TOPE).exists())
        self.assertTrue(alimentacion.alertas.filter(tipo_alerta=TipoAlertaViatico.EXCEDE_TOPE).exists())


class AlertaMontoRepetidoTests(BaseViaticos):

    def test_tres_iguales_en_el_mes_levantan_alerta(self):
        self._reporte(monto=Decimal('4.00'), fecha=datetime.date(2026, 9, 1))
        self._reporte(monto=Decimal('4.00'), fecha=datetime.date(2026, 9, 2))
        tercero = self._reporte(monto=Decimal('4.00'), fecha=datetime.date(2026, 9, 3))
        services.evaluar_alertas(tercero)
        self.assertTrue(tercero.alertas.filter(tipo_alerta=TipoAlertaViatico.MONTO_REPETIDO).exists())

    def test_dos_iguales_no_alcanzan(self):
        """Dos almuerzos de $4.00 son lo esperable; alertar ahí sería ruido puro."""
        self._reporte(monto=Decimal('4.00'), fecha=datetime.date(2026, 9, 1))
        segundo = self._reporte(monto=Decimal('4.00'), fecha=datetime.date(2026, 9, 2))
        services.evaluar_alertas(segundo)
        self.assertFalse(segundo.alertas.filter(tipo_alerta=TipoAlertaViatico.MONTO_REPETIDO).exists())

    def test_no_cruza_el_limite_del_mes(self):
        self._reporte(monto=Decimal('4.00'), fecha=datetime.date(2026, 8, 30))
        self._reporte(monto=Decimal('4.00'), fecha=datetime.date(2026, 8, 31))
        de_septiembre = self._reporte(monto=Decimal('4.00'), fecha=datetime.date(2026, 9, 1))
        services.evaluar_alertas(de_septiembre)
        self.assertFalse(de_septiembre.alertas.filter(tipo_alerta=TipoAlertaViatico.MONTO_REPETIDO).exists())

    def test_los_rechazados_no_cuentan(self):
        self._reporte(monto=Decimal('4.00'), fecha=datetime.date(2026, 9, 1),
                      estado=EstadoReporteViatico.RECHAZADO)
        self._reporte(monto=Decimal('4.00'), fecha=datetime.date(2026, 9, 2))
        tercero = self._reporte(monto=Decimal('4.00'), fecha=datetime.date(2026, 9, 3))
        services.evaluar_alertas(tercero)
        self.assertFalse(tercero.alertas.filter(tipo_alerta=TipoAlertaViatico.MONTO_REPETIDO).exists())


class AlertaSinPlanificacionTests(BaseViaticos):

    def test_sin_visita_planificada_levanta_alerta(self):
        reporte = self._reporte()
        services.evaluar_alertas(reporte)
        self.assertTrue(reporte.alertas.filter(tipo_alerta=TipoAlertaViatico.SIN_PLANIFICACION).exists())

    def test_con_visita_ese_dia_no_alerta(self):
        VisitaTecnica.objects.create(
            farmacia=self.mia, tecnico=self.usuario, fecha_planificada=datetime.date(2026, 9, 10),
        )
        reporte = self._reporte(fecha=datetime.date(2026, 9, 10))
        services.evaluar_alertas(reporte)
        self.assertFalse(reporte.alertas.filter(tipo_alerta=TipoAlertaViatico.SIN_PLANIFICACION).exists())

    def test_una_visita_cancelada_no_justifica_el_gasto(self):
        VisitaTecnica.objects.create(
            farmacia=self.mia, tecnico=self.usuario, fecha_planificada=datetime.date(2026, 9, 10),
            estado=VisitaTecnica.Estado.CANCELADA,
        )
        reporte = self._reporte(fecha=datetime.date(2026, 9, 10))
        services.evaluar_alertas(reporte)
        self.assertTrue(reporte.alertas.filter(tipo_alerta=TipoAlertaViatico.SIN_PLANIFICACION).exists())

    def test_sin_usuario_de_panel_no_se_controla(self):
        """La planificación se guarda contra el User. Sin usuario no hay cómo cruzar,
        y alertar a todos los colaboradores sin login sería ruido garantizado."""
        self.tecnico.usuario = None
        self.tecnico.save(update_fields=['usuario'])
        reporte = self._reporte()
        services.evaluar_alertas(reporte)
        self.assertFalse(reporte.alertas.filter(tipo_alerta=TipoAlertaViatico.SIN_PLANIFICACION).exists())


class EvaluacionIdempotenteTests(BaseViaticos):

    def test_revalidar_no_duplica_alertas(self):
        reporte = self._reporte(rubro=RubroViatico.HOSPEDAJE, monto=Decimal('45.00'))
        services.evaluar_alertas(reporte)
        services.evaluar_alertas(reporte)
        self.assertEqual(reporte.alertas.filter(tipo_alerta=TipoAlertaViatico.EXCEDE_TOPE).count(), 1)

    def test_corregir_el_monto_marca_la_alerta_como_resuelta(self):
        reporte = self._reporte(rubro=RubroViatico.HOSPEDAJE, monto=Decimal('45.00'))
        services.evaluar_alertas(reporte)
        reporte.monto = Decimal('28.00')
        reporte.save(update_fields=['monto'])
        services.evaluar_alertas(reporte)

        alerta = reporte.alertas.get(tipo_alerta=TipoAlertaViatico.EXCEDE_TOPE)
        # Se marca en vez de borrarse: queda el rastro de que el reporte pasó por ahí.
        self.assertTrue(alerta.resuelta)
        self.assertFalse(reporte.alertas_abiertas.filter(tipo_alerta=TipoAlertaViatico.EXCEDE_TOPE).exists())


class FlujoDeAprobacionTests(BaseViaticos):

    def setUp(self):
        super().setUp()
        self.coordinador = User.objects.create_user(username='coordinador_viaticos', password='x')

    def test_aprobar_con_alerta_de_tope_sin_comentario_falla(self):
        reporte = self._reporte(rubro=RubroViatico.HOSPEDAJE, monto=Decimal('45.00'))
        services.evaluar_alertas(reporte)
        with self.assertRaises(services.JustificacionRequerida):
            services.aprobar_reporte(reporte=reporte, coordinador=self.coordinador, comentario='')
        reporte.refresh_from_db()
        self.assertEqual(reporte.estado, EstadoReporteViatico.PENDIENTE)

    def test_aprobar_con_alerta_y_justificacion_funciona(self):
        reporte = self._reporte(rubro=RubroViatico.HOSPEDAJE, monto=Decimal('45.00'))
        services.evaluar_alertas(reporte)
        services.aprobar_reporte(
            reporte=reporte, coordinador=self.coordinador, comentario='Único hotel disponible por feriado.',
        )
        reporte.refresh_from_db()
        self.assertEqual(reporte.estado, EstadoReporteViatico.APROBADO)
        self.assertEqual(reporte.revisado_por, self.coordinador)
        self.assertIsNotNone(reporte.revisado_en)

    def test_una_alerta_que_no_exige_justificacion_no_bloquea(self):
        """`sin_planificacion` avisa, pero no es de las que la política pone como
        condición para aprobar."""
        reporte = self._reporte()
        services.evaluar_alertas(reporte)
        self.assertTrue(reporte.alertas_abiertas.exists())
        services.aprobar_reporte(reporte=reporte, coordinador=self.coordinador, comentario='')
        reporte.refresh_from_db()
        self.assertEqual(reporte.estado, EstadoReporteViatico.APROBADO)

    def test_observar_exige_decir_que_corregir(self):
        reporte = self._reporte()
        with self.assertRaises(services.JustificacionRequerida):
            services.observar_reporte(reporte=reporte, coordinador=self.coordinador, comentario='  ')

    def test_rechazar_exige_motivo(self):
        reporte = self._reporte()
        with self.assertRaises(services.JustificacionRequerida):
            services.rechazar_reporte(reporte=reporte, coordinador=self.coordinador, comentario='')

    def test_no_se_aprueba_dos_veces(self):
        reporte = self._reporte()
        services.aprobar_reporte(reporte=reporte, coordinador=self.coordinador, comentario='ok')
        with self.assertRaises(services.TransicionInvalida):
            services.aprobar_reporte(reporte=reporte, coordinador=self.coordinador, comentario='ok')


class ConsolidadoTests(BaseViaticos):

    def test_suma_por_rubro_y_cuenta_alertas(self):
        self._reporte(rubro=RubroViatico.HOSPEDAJE, monto=Decimal('30.00'))
        self._reporte(rubro=RubroViatico.ALIMENTACION, monto=Decimal('4.00'))
        con_alerta = self._reporte(
            rubro=RubroViatico.MOVILIZACION, monto=Decimal('40.00'), origen='A', destino='B',
        )
        services.evaluar_alertas(con_alerta)

        filas = services.consolidado_mensual(ReporteViatico.objects.all(), 2026, 9)
        self.assertEqual(len(filas), 1)
        fila = filas[0]
        self.assertEqual(fila['hospedaje'], Decimal('30.00'))
        self.assertEqual(fila['alimentacion'], Decimal('4.00'))
        self.assertEqual(fila['movilizacion'], Decimal('40.00'))
        self.assertEqual(fila['total'], Decimal('74.00'))
        self.assertEqual(fila['reportes'], 3)
        self.assertGreaterEqual(fila['alertas'], 1)

    def test_las_alertas_no_inflan_los_montos(self):
        """Regresión: sumar montos y contar alertas en el mismo annotate hacía que el
        JOIN a alertas multiplicara las filas. Un reporte de $40 con dos alertas
        sumaba $80, y el coordinador decidía sobre un total falso."""
        reporte = self._reporte(
            rubro=RubroViatico.MOVILIZACION, monto=Decimal('40.00'), origen='A', destino='B',
            farmacia_visitada=self.ajena,
        )
        services.evaluar_alertas(reporte)
        self.assertGreaterEqual(reporte.alertas_abiertas.count(), 2)

        fila = services.consolidado_mensual(ReporteViatico.objects.all(), 2026, 9)[0]
        self.assertEqual(fila['movilizacion'], Decimal('40.00'))
        self.assertEqual(fila['total'], Decimal('40.00'))
        self.assertEqual(fila['reportes'], 1)

        tendencia = services.tendencia_ultimos_meses(ReporteViatico.objects.all(), 2026, 9, meses=1)
        self.assertEqual(tendencia[0]['total'], Decimal('40.00'))

    def test_los_rechazados_no_suman(self):
        """No se van a pagar: incluirlos infla el total con el que se decide."""
        self._reporte(rubro=RubroViatico.HOSPEDAJE, monto=Decimal('30.00'))
        self._reporte(rubro=RubroViatico.HOSPEDAJE, monto=Decimal('99.00'),
                      estado=EstadoReporteViatico.RECHAZADO)
        fila = services.consolidado_mensual(ReporteViatico.objects.all(), 2026, 9)[0]
        self.assertEqual(fila['total'], Decimal('30.00'))

    def test_no_mezcla_meses(self):
        self._reporte(rubro=RubroViatico.HOSPEDAJE, monto=Decimal('30.00'), fecha=datetime.date(2026, 8, 31))
        self._reporte(rubro=RubroViatico.HOSPEDAJE, monto=Decimal('25.00'), fecha=datetime.date(2026, 9, 1))
        fila = services.consolidado_mensual(ReporteViatico.objects.all(), 2026, 9)[0]
        self.assertEqual(fila['total'], Decimal('25.00'))

    def test_tendencia_devuelve_tres_meses_en_orden(self):
        self._reporte(monto=Decimal('4.00'), fecha=datetime.date(2026, 7, 15))
        self._reporte(monto=Decimal('4.00'), fecha=datetime.date(2026, 9, 15))
        tendencia = services.tendencia_ultimos_meses(ReporteViatico.objects.all(), 2026, 9, meses=3)
        self.assertEqual([t['etiqueta'] for t in tendencia], ['07/2026', '08/2026', '09/2026'])
        self.assertEqual(tendencia[0]['total'], Decimal('4.00'))
        self.assertEqual(tendencia[1]['total'], 0)

    def test_tendencia_cruza_el_cambio_de_anio(self):
        """Enero tiene que mirar noviembre y diciembre del año anterior."""
        tendencia = services.tendencia_ultimos_meses(ReporteViatico.objects.none(), 2026, 1, meses=3)
        self.assertEqual([t['etiqueta'] for t in tendencia], ['11/2025', '12/2025', '01/2026'])


class RegistrarReporteTests(BaseViaticos):
    """El alta por servicio: valida, guarda y levanta alertas en un solo paso."""

    def test_alta_valida_guarda_y_evalua(self):
        reporte = services.registrar_reporte(
            colaborador=self.tecnico, fecha=datetime.date(2026, 9, 10), farmacia_visitada=self.ajena,
            rubro=RubroViatico.HOSPEDAJE, monto=Decimal('45.00'),
        )
        self.assertIsNotNone(reporte.pk)
        tipos = set(reporte.alertas.values_list('tipo_alerta', flat=True))
        self.assertIn(TipoAlertaViatico.EXCEDE_TOPE, tipos)
        self.assertIn(TipoAlertaViatico.FUERA_DE_ZONA, tipos)

    def test_alta_invalida_no_deja_nada_a_medias(self):
        with self.assertRaises(ValidationError):
            services.registrar_reporte(
                colaborador=self.tecnico, fecha=datetime.date(2026, 9, 10), farmacia_visitada=self.mia,
                rubro=RubroViatico.MOVILIZACION, monto=Decimal('10.00'),
            )
        self.assertEqual(ReporteViatico.objects.count(), 0)
        self.assertEqual(AlertaViatico.objects.count(), 0)


class ColaboradorDeTests(BaseViaticos):

    def test_resuelve_por_el_onetoone_del_colaborador(self):
        self.assertEqual(services.colaborador_de(self.usuario), self.tecnico)

    def test_usuario_sin_colaborador_devuelve_none(self):
        suelto = User.objects.create_user(username='sin_colaborador', password='x')
        self.assertIsNone(services.colaborador_de(suelto))
