"""Actividades planificadas.

No son mantenimientos: son trabajo previsto que alguien completa, sin equipo ni
checklist ni firma. Compartian archivo con mantenimiento por cercania tematica,
no por logica.
"""
from django.contrib import messages
from django.contrib.auth.decorators import login_required, permission_required
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from apps.auditoria.models import registrar_evento
from apps.cuentas.services import (
    scope_opcional_por_unidad_negocio_activa, verificar_acceso,
)
from apps.mantenimiento import services as mantenimiento_services
from apps.mantenimiento.forms import ActividadPlanificadaForm, CompletarActividadForm
from apps.mantenimiento.models import ActividadPlanificada


@login_required
@permission_required('mantenimiento.view_actividadplanificada', raise_exception=True)
def actividades_planificadas_lista(request):
    # `scope_opcional_*` y no el estricto: las actividades internas (sin unidad) tienen
    # que seguir viendose, igual que un Script global. El estricto las ocultaria a todos.
    actividades = scope_opcional_por_unidad_negocio_activa(
        ActividadPlanificada.objects.filter(activo=True).select_related(
            'tecnico', 'equipo', 'ubicacion',
        ),
        request, 'unidad_negocio',
    ).order_by('fecha_inicio')

    tecnico = request.GET.get('tecnico')
    estado = request.GET.get('estado')
    if tecnico:
        actividades = actividades.filter(tecnico__username=tecnico)
    if estado:
        actividades = actividades.filter(estado=estado)

    return render(request, 'panel/actividades_planificadas_lista.html', {
        'actividades': actividades,
        'estados': ActividadPlanificada.Estado.choices,
        'filtro_tecnico': tecnico or '', 'filtro_estado': estado or '',
    })


@login_required
@permission_required('mantenimiento.add_actividadplanificada', raise_exception=True)
def actividad_planificada_crear(request):
    if request.method == 'POST':
        form = ActividadPlanificadaForm(request.POST, user=request.user)
        if form.is_valid():
            d = form.cleaned_data
            actividad = mantenimiento_services.crear_actividad_planificada(
                tecnico=d['tecnico'], creado_por=request.user, titulo=d['titulo'], descripcion=d['descripcion'],
                tipo_actividad=d['tipo_actividad'], prioridad=d['prioridad'], fecha_inicio=d['fecha_inicio'],
                fecha_fin=d['fecha_fin'], tiempo_estimado_minutos=d['tiempo_estimado_minutos'],
                equipo=d['equipo'], ubicacion=d['ubicacion'], unidad_negocio=d['unidad_negocio'],
            )
            registrar_evento(
                usuario=request.user, accion='actividad_planificada.crear', objeto=actividad, request=request,
            )
            messages.success(request, f'Actividad "{actividad.titulo}" creada.')
            return redirect('panel:actividades_planificadas_lista')
    else:
        form = ActividadPlanificadaForm(user=request.user)
    return render(request, 'panel/accion_form.html', {
        'form': form, 'titulo': 'Nueva actividad planificada', 'boton': 'Crear actividad',
        'volver_url': reverse('panel:actividades_planificadas_lista'),
    })


@login_required
@permission_required('mantenimiento.change_actividadplanificada', raise_exception=True)
def actividad_planificada_completar(request, pk):
    actividad = get_object_or_404(ActividadPlanificada, pk=pk)
    # Sin esto, cambiar el id en la URL dejaba cerrar la actividad de otro cliente.
    if actividad.unidad_negocio_id is not None:
        verificar_acceso(request.user, actividad.unidad_negocio)
    if request.method == 'POST':
        form = CompletarActividadForm(request.POST)
        if form.is_valid():
            try:
                mantenimiento_services.completar_actividad_planificada(
                    actividad=actividad, tiempo_real_minutos=form.cleaned_data['tiempo_real_minutos'],
                )
            except ValueError as exc:
                form.add_error(None, str(exc))
            else:
                registrar_evento(
                    usuario=request.user, accion='actividad_planificada.completar', objeto=actividad, request=request,
                )
                messages.success(request, f'Actividad "{actividad.titulo}" completada.')
                return redirect('panel:actividades_planificadas_lista')
    else:
        form = CompletarActividadForm()
    return render(request, 'panel/accion_form.html', {
        'form': form, 'titulo': f'Completar actividad: {actividad.titulo}', 'boton': 'Marcar completada',
        'resumen_tipo': 'mantenimiento', 'resumen_titulo': actividad.titulo,
        'resumen_sub': f'Técnico: {actividad.tecnico}',
        'resumen_campos': [
            ('Prioridad', actividad.get_prioridad_display()), ('Vence', actividad.fecha_fin),
        ],
        'volver_url': reverse('panel:actividades_planificadas_lista'),
    })
