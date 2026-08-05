"""Resolución de objetivos y seguimiento de una ActividadCumplimiento.

No reutiliza apps.catalogo.services.resolver_estaciones tal cual (esa función no
sabe de unidad_negocio), pero replica su mismo espíritu: un dispatcher simple por
tipo de objetivo. v1 es de atestación manual (alguien marca "completado"), no
verificación automática — eso queda para una fase futura.
"""
from django.utils import timezone

from apps.activos.models import Colaborador
from apps.auditoria.models import registrar_evento
from apps.catalogo.models import Estacion, Farmacia

from .models import (
    EstadoCumplimiento, ResultadoCumplimientoColaborador,
    ResultadoCumplimientoEstacion, ResultadoCumplimientoFarmacia, TipoObjetivoCumplimiento,
)

_MODELO_RESULTADO_POR_TIPO = {
    TipoObjetivoCumplimiento.ESTACIONES: (ResultadoCumplimientoEstacion, 'estacion'),
    TipoObjetivoCumplimiento.FARMACIAS: (ResultadoCumplimientoFarmacia, 'farmacia'),
    TipoObjetivoCumplimiento.COLABORADORES: (ResultadoCumplimientoColaborador, 'colaborador'),
}


def resolver_objetivos(actividad):
    """Devuelve el queryset de objetos (Estacion/Farmacia/Colaborador) en el alcance
    de `actividad`, según su tipo_objetivo, unidades_negocio y (si aplica) filtros
    adicionales (fecha de apertura de farmacia, cargos de colaborador)."""
    unidades = actividad.unidades_negocio.all()

    if actividad.tipo_objetivo == TipoObjetivoCumplimiento.ESTACIONES:
        return Estacion.objects.filter(
            estado_aprobacion=Estacion.EstadoAprobacion.APROBADA,
            farmacia__unidad_negocio__in=unidades,
        )

    if actividad.tipo_objetivo == TipoObjetivoCumplimiento.FARMACIAS:
        qs = Farmacia.objects.filter(unidad_negocio__in=unidades, activa=True)
        if actividad.farmacias_aperturadas_desde:
            qs = qs.filter(fecha_apertura__gte=actividad.farmacias_aperturadas_desde)
        return qs

    return Colaborador.objects.filter(
        activo=True, unidad_negocio__in=unidades, cargo__in=actividad.cargos.all(),
    )


def generar_resultados(actividad, usuario):
    """Crea las filas de resultado que falten para los objetivos actuales de
    `actividad` (idempotente: correrlo de nuevo no duplica filas existentes)."""
    modelo_resultado, campo = _MODELO_RESULTADO_POR_TIPO[actividad.tipo_objetivo]
    objetivos = resolver_objetivos(actividad)

    existentes = set(
        modelo_resultado.objects.filter(actividad=actividad).values_list(f'{campo}_id', flat=True),
    )
    nuevos = [
        modelo_resultado(actividad=actividad, **{campo: objetivo})
        for objetivo in objetivos if objetivo.pk not in existentes
    ]
    modelo_resultado.objects.bulk_create(nuevos)

    registrar_evento(
        usuario=usuario, accion='cumplimiento.generar_resultados', objeto=actividad,
        detalle={'nuevos': len(nuevos), 'tipo_objetivo': actividad.tipo_objetivo},
    )
    return nuevos


def marcar_completado(resultado, usuario, observacion=''):
    resultado.estado = EstadoCumplimiento.COMPLETADO
    resultado.fecha_completado = timezone.now()
    resultado.completado_por = usuario
    resultado.observacion = observacion
    resultado.save(update_fields=['estado', 'fecha_completado', 'completado_por', 'observacion'])

    registrar_evento(
        usuario=usuario, accion='cumplimiento.marcar_completado', objeto=resultado.actividad,
        detalle={'resultado_id': resultado.pk, 'observacion': observacion},
    )
    return resultado


def calcular_avance(actividad):
    """% de resultados completados sobre el total, para tarjetas/listas del panel."""
    modelo_resultado, _ = _MODELO_RESULTADO_POR_TIPO[actividad.tipo_objetivo]
    total = modelo_resultado.objects.filter(actividad=actividad).count()
    if not total:
        return None
    completados = modelo_resultado.objects.filter(
        actividad=actividad, estado=EstadoCumplimiento.COMPLETADO,
    ).count()
    return round(100 * completados / total)
