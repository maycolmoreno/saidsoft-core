from datetime import date

from django.contrib.auth.models import User
from django.test import TestCase

from apps.activos.models import Cargo, Colaborador, Departamento
from apps.auditoria.models import EventoAuditoria
from apps.catalogo.models import Estacion, Farmacia, Grupo, UnidadNegocio

from .models import (
    ActividadCumplimiento, EstadoCumplimiento, ResultadoCumplimientoColaborador,
    ResultadoCumplimientoEstacion, TipoObjetivoCumplimiento,
)
from .services import calcular_avance, generar_resultados, marcar_completado, resolver_objetivos


class ResolverObjetivosTests(TestCase):
    def setUp(self):
        self.usuario = User.objects.create_user(username='u', password='x')
        # La migración de datos de catalogo ya siembra SG/MIA — reutilizarlas, no recrearlas.
        self.sg = UnidadNegocio.objects.get(codigo='SG')
        self.mia = UnidadNegocio.objects.get(codigo='MIA')
        grupo = Grupo.objects.create(codigo='TRX001')

        self.farmacia_sg = Farmacia.objects.create(
            codigo='ML001', grupo=grupo, unidad_negocio=self.sg, fecha_apertura=date(2026, 8, 1),
        )
        self.farmacia_sg_vieja = Farmacia.objects.create(
            codigo='ML002', grupo=grupo, unidad_negocio=self.sg, fecha_apertura=date(2026, 1, 1),
        )
        self.farmacia_mia = Farmacia.objects.create(codigo='MAM01', grupo=grupo, unidad_negocio=self.mia)

        self.estacion_sg = Estacion.objects.create(
            codigo='ML001-A', farmacia=self.farmacia_sg, estado_aprobacion=Estacion.EstadoAprobacion.APROBADA,
        )
        self.estacion_mia = Estacion.objects.create(
            codigo='MAM01-A', farmacia=self.farmacia_mia, estado_aprobacion=Estacion.EstadoAprobacion.APROBADA,
        )

        depto = Departamento.objects.create(nombre='Operaciones')
        self.cargo_lider = Cargo.objects.create(nombre='Líder', departamento=depto)
        self.cargo_vendedor = Cargo.objects.create(nombre='Vendedor', departamento=depto)
        self.colaborador_lider = Colaborador.objects.create(
            nombre='Ana', cedula='001', cargo=self.cargo_lider, unidad_negocio=self.sg,
        )
        self.colaborador_vendedor = Colaborador.objects.create(
            nombre='Beto', cedula='002', cargo=self.cargo_vendedor, unidad_negocio=self.sg,
        )

    def _crear_actividad(self, **kwargs):
        actividad = ActividadCumplimiento.objects.create(creado_por=self.usuario, **kwargs)
        return actividad

    def test_resolver_estaciones_filtra_por_unidad_negocio(self):
        actividad = self._crear_actividad(
            nombre='AD', tipo_objetivo=TipoObjetivoCumplimiento.ESTACIONES, fecha_limite=date(2026, 10, 30),
        )
        actividad.unidades_negocio.add(self.sg)

        objetivos = list(resolver_objetivos(actividad))
        self.assertEqual(objetivos, [self.estacion_sg])

    def test_resolver_farmacias_filtra_por_fecha_apertura(self):
        actividad = self._crear_actividad(
            nombre='Checklist de Apertura', tipo_objetivo=TipoObjetivoCumplimiento.FARMACIAS,
            fecha_limite=date(2026, 10, 7), farmacias_aperturadas_desde=date(2026, 7, 1),
        )
        actividad.unidades_negocio.add(self.sg, self.mia)

        objetivos = set(resolver_objetivos(actividad))
        # farmacia_sg_vieja se abrió en enero -> queda fuera; farmacia_mia no tiene fecha -> también fuera.
        self.assertEqual(objetivos, {self.farmacia_sg})

    def test_resolver_colaboradores_filtra_por_cargo(self):
        actividad = self._crear_actividad(
            nombre='2FA', tipo_objetivo=TipoObjetivoCumplimiento.COLABORADORES, fecha_limite=date(2026, 10, 7),
        )
        actividad.unidades_negocio.add(self.sg)
        actividad.cargos.add(self.cargo_lider)

        objetivos = list(resolver_objetivos(actividad))
        self.assertEqual(objetivos, [self.colaborador_lider])

    def test_generar_resultados_es_idempotente(self):
        actividad = self._crear_actividad(
            nombre='AD', tipo_objetivo=TipoObjetivoCumplimiento.ESTACIONES, fecha_limite=date(2026, 10, 30),
        )
        actividad.unidades_negocio.add(self.sg)

        generar_resultados(actividad, self.usuario)
        generar_resultados(actividad, self.usuario)

        self.assertEqual(ResultadoCumplimientoEstacion.objects.filter(actividad=actividad).count(), 1)
        self.assertTrue(EventoAuditoria.objects.filter(accion='cumplimiento.generar_resultados').exists())

    def test_marcar_completado_actualiza_estado_y_audita(self):
        actividad = self._crear_actividad(
            nombre='AD', tipo_objetivo=TipoObjetivoCumplimiento.ESTACIONES, fecha_limite=date(2026, 10, 30),
        )
        actividad.unidades_negocio.add(self.sg)
        generar_resultados(actividad, self.usuario)
        resultado = ResultadoCumplimientoEstacion.objects.get(actividad=actividad, estacion=self.estacion_sg)

        marcar_completado(resultado, self.usuario, observacion='Instalado correctamente')

        resultado.refresh_from_db()
        self.assertEqual(resultado.estado, EstadoCumplimiento.COMPLETADO)
        self.assertIsNotNone(resultado.fecha_completado)
        self.assertEqual(resultado.completado_por, self.usuario)
        self.assertTrue(EventoAuditoria.objects.filter(accion='cumplimiento.marcar_completado').exists())

    def test_calcular_avance(self):
        actividad = self._crear_actividad(
            nombre='2FA', tipo_objetivo=TipoObjetivoCumplimiento.COLABORADORES, fecha_limite=date(2026, 10, 7),
        )
        actividad.unidades_negocio.add(self.sg)
        actividad.cargos.add(self.cargo_lider, self.cargo_vendedor)
        generar_resultados(actividad, self.usuario)

        self.assertEqual(calcular_avance(actividad), 0)

        resultado = ResultadoCumplimientoColaborador.objects.get(actividad=actividad, colaborador=self.colaborador_lider)
        marcar_completado(resultado, self.usuario)

        self.assertEqual(calcular_avance(actividad), 50)

    def test_calcular_avance_sin_objetivos_es_none(self):
        actividad = self._crear_actividad(
            nombre='Sin objetivos', tipo_objetivo=TipoObjetivoCumplimiento.FARMACIAS, fecha_limite=date(2026, 10, 7),
        )
        self.assertIsNone(calcular_avance(actividad))
