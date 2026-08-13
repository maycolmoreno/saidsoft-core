from django.test import TestCase

from apps.catalogo.models import Estacion, Farmacia, Grupo, UnidadNegocio
from apps.facturacion.models import ActividadMensualEstacion
from apps.facturacion.services import estaciones_facturables, registrar_actividad_mensual, resumen_facturacion


class RegistrarActividadMensualTests(TestCase):
    def setUp(self):
        sg = UnidadNegocio.objects.get(codigo='SG')
        grupo = Grupo.objects.create(codigo='TRX001')
        farmacia = Farmacia.objects.create(codigo='ML001', grupo=grupo, unidad_negocio=sg)
        self.estacion = Estacion.objects.create(codigo='ML001-A', farmacia=farmacia)

    def test_primer_registro_crea_la_fila_del_mes(self):
        registrar_actividad_mensual(self.estacion, cuando=None)
        self.assertEqual(ActividadMensualEstacion.objects.filter(estacion=self.estacion).count(), 1)

    def test_es_idempotente_dentro_del_mismo_mes(self):
        registrar_actividad_mensual(self.estacion)
        registrar_actividad_mensual(self.estacion)
        self.assertEqual(ActividadMensualEstacion.objects.filter(estacion=self.estacion).count(), 1)

    def test_meses_distintos_generan_filas_distintas(self):
        from datetime import datetime

        from django.utils import timezone

        registrar_actividad_mensual(self.estacion, cuando=timezone.make_aware(datetime(2026, 1, 15)))
        registrar_actividad_mensual(self.estacion, cuando=timezone.make_aware(datetime(2026, 2, 1)))
        self.assertEqual(ActividadMensualEstacion.objects.filter(estacion=self.estacion).count(), 2)


class ResumenFacturacionTests(TestCase):
    def setUp(self):
        self.sg = UnidadNegocio.objects.get(codigo='SG')
        self.mia = UnidadNegocio.objects.get(codigo='MIA')
        grupo = Grupo.objects.create(codigo='TRX001')
        farmacia_sg = Farmacia.objects.create(codigo='ML001', grupo=grupo, unidad_negocio=self.sg)
        farmacia_mia = Farmacia.objects.create(codigo='ML002', grupo=grupo, unidad_negocio=self.mia)
        self.estacion_sg_1 = Estacion.objects.create(codigo='ML001-A', farmacia=farmacia_sg)
        self.estacion_sg_2 = Estacion.objects.create(codigo='ML001-B', farmacia=farmacia_sg)
        self.estacion_mia = Estacion.objects.create(codigo='ML002-A', farmacia=farmacia_mia)

        for estacion in (self.estacion_sg_1, self.estacion_sg_2, self.estacion_mia):
            ActividadMensualEstacion.objects.create(estacion=estacion, anio=2026, mes=8)
        # Actividad de un mes distinto: no debe contar en el resumen de agosto.
        ActividadMensualEstacion.objects.create(estacion=self.estacion_sg_1, anio=2026, mes=7)

    def test_cuenta_solo_las_estaciones_de_la_unidad_de_negocio_en_el_periodo(self):
        self.assertEqual(resumen_facturacion(self.sg, 2026, 8), 2)
        self.assertEqual(resumen_facturacion(self.mia, 2026, 8), 1)

    def test_periodo_sin_actividad_da_cero(self):
        self.assertEqual(resumen_facturacion(self.sg, 2025, 1), 0)

    def test_estaciones_facturables_devuelve_el_queryset_scopado(self):
        codigos = list(estaciones_facturables(self.sg, 2026, 8).values_list('codigo', flat=True))
        self.assertCountEqual(codigos, ['ML001-A', 'ML001-B'])
