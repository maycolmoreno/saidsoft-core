from django.contrib import messages
from django.contrib.auth.decorators import login_required, permission_required
from django.core.exceptions import PermissionDenied
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from apps.auditoria.models import registrar_evento
from apps.cumplimiento import services as cumplimiento_services
from apps.cumplimiento.forms import ActividadCumplimientoForm
from apps.cumplimiento.models import (
    ActividadCumplimiento, ResultadoCumplimientoColaborador, ResultadoCumplimientoEstacion,
    ResultadoCumplimientoFarmacia, TipoObjetivoCumplimiento,
)
from apps.cuentas.services import (
    scope_opcional_por_unidad_negocio_activa, unidades_negocio_visibles, usuario_tiene_acceso_total,
)

_MODELO_RESULTADO_POR_TIPO = {
    TipoObjetivoCumplimiento.ESTACIONES: ResultadoCumplimientoEstacion,
    TipoObjetivoCumplimiento.FARMACIAS: ResultadoCumplimientoFarmacia,
    TipoObjetivoCumplimiento.COLABORADORES: ResultadoCumplimientoColaborador,
}


def _verificar_acceso_actividad(user, actividad):
    """Como apps.cuentas.services.verificar_acceso, pero para ActividadCumplimiento,
    que se dirige a varias unidades de negocio a la vez (M2M) en vez de una sola FK.
    Sin unidades_negocio asignadas = compartida, visible para todos."""
    if usuario_tiene_acceso_total(user):
        return
    if not actividad.unidades_negocio.exists():
        return
    if not actividad.unidades_negocio.filter(pk__in=unidades_negocio_visibles(user)).exists():
        raise PermissionDenied('No tiene acceso a esta actividad de cumplimiento.')


@login_required
@permission_required('cumplimiento.view_actividadcumplimiento', raise_exception=True)
def cumplimiento_lista(request):
    actividades = scope_opcional_por_unidad_negocio_activa(
        ActividadCumplimiento.objects.select_related('creado_por').prefetch_related('unidades_negocio'),
        request, 'unidades_negocio',
    ).distinct()
    for actividad in actividades:
        actividad.avance = cumplimiento_services.calcular_avance(actividad)
    return render(request, 'panel/cumplimiento_lista.html', {'actividades': actividades})


@login_required
@permission_required('cumplimiento.add_actividadcumplimiento', raise_exception=True)
def cumplimiento_crear(request):
    if request.method == 'POST':
        form = ActividadCumplimientoForm(request.POST, user=request.user)
        if form.is_valid():
            actividad = form.save(commit=False)
            actividad.creado_por = request.user
            actividad.save()
            form.save_m2m()
            cumplimiento_services.generar_resultados(actividad, request.user)
            registrar_evento(usuario=request.user, accion='cumplimiento.crear', objeto=actividad, request=request)
            messages.success(request, f'Actividad "{actividad.nombre}" creada.')
            return redirect('panel:cumplimiento_detalle', pk=actividad.pk)
    else:
        form = ActividadCumplimientoForm(user=request.user)
    return render(request, 'panel/accion_form.html', {
        'form': form, 'titulo': 'Nueva actividad de cumplimiento',
        'volver_url': reverse('panel:cumplimiento_lista'),
    })


@login_required
@permission_required('cumplimiento.view_actividadcumplimiento', raise_exception=True)
def cumplimiento_detalle(request, pk):
    actividad = get_object_or_404(ActividadCumplimiento, pk=pk)
    _verificar_acceso_actividad(request.user, actividad)
    modelo_resultado = _MODELO_RESULTADO_POR_TIPO[actividad.tipo_objetivo]
    campo_objetivo = {
        TipoObjetivoCumplimiento.ESTACIONES: 'estacion',
        TipoObjetivoCumplimiento.FARMACIAS: 'farmacia',
        TipoObjetivoCumplimiento.COLABORADORES: 'colaborador',
    }[actividad.tipo_objetivo]
    resultados = modelo_resultado.objects.filter(actividad=actividad).select_related(campo_objetivo)
    return render(request, 'panel/cumplimiento_detalle.html', {
        'actividad': actividad, 'resultados': resultados,
        'avance': cumplimiento_services.calcular_avance(actividad),
    })


@login_required
@permission_required('cumplimiento.change_actividadcumplimiento', raise_exception=True)
@require_POST
def cumplimiento_resultado_completar(request, pk, resultado_pk):
    actividad = get_object_or_404(ActividadCumplimiento, pk=pk)
    _verificar_acceso_actividad(request.user, actividad)
    modelo_resultado = _MODELO_RESULTADO_POR_TIPO[actividad.tipo_objetivo]
    resultado = get_object_or_404(modelo_resultado, pk=resultado_pk, actividad=actividad)
    cumplimiento_services.marcar_completado(resultado, request.user, request.POST.get('observacion', ''))
    return redirect('panel:cumplimiento_detalle', pk=actividad.pk)
