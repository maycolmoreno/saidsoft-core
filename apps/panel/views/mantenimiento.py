from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse

from apps.auditoria.models import registrar_evento
from apps.mantenimiento import services as mantenimiento_services
from apps.mantenimiento.forms import (
    CancelarMantenimientoForm, CerrarMantenimientoForm, MantenimientoManualForm, MantenimientoProgramadoForm,
)
from apps.mantenimiento.models import ActividadChecklist, Mantenimiento, MantenimientoProgramado


@login_required
def mantenimientos_lista(request):
    mantenimientos = Mantenimiento.objects.select_related('cliente', 'tecnico').order_by('-fecha_programada')

    estado = request.GET.get('estado')
    if estado:
        mantenimientos = mantenimientos.filter(estado_interno=estado)

    return render(request, 'panel/mantenimientos_lista.html', {
        'mantenimientos': mantenimientos,
        'estados': Mantenimiento.EstadoInterno.choices,
        'filtro_estado': estado or '',
    })


@login_required
def mantenimiento_crear(request):
    if request.method == 'POST':
        form = MantenimientoManualForm(request.POST)
        if form.is_valid():
            d = form.cleaned_data
            mantenimiento = mantenimiento_services.crear_mantenimiento_manual(
                equipos=list(d['equipos']), cliente=d['cliente'], tecnico=d['tecnico'], empresa=d['empresa'],
                tipo_mantenimiento=d['tipo_mantenimiento'], descripcion=d['descripcion'],
                fecha_programada=d['fecha_programada'], usuario=request.user,
            )
            registrar_evento(
                usuario=request.user, accion='mantenimiento.crear', objeto=mantenimiento, request=request,
            )
            messages.success(request, f'Mantenimiento #{mantenimiento.pk} creado.')
            return redirect('panel:mantenimiento_detalle', pk=mantenimiento.pk)
    else:
        form = MantenimientoManualForm()
    return render(request, 'panel/accion_form.html', {
        'form': form, 'titulo': 'Nuevo mantenimiento',
        'volver_url': reverse('panel:mantenimientos_lista'),
    })


@login_required
def mantenimiento_detalle(request, pk):
    mantenimiento = get_object_or_404(
        Mantenimiento.objects.select_related('cliente', 'tecnico', 'empresa', 'mantenimiento_programado'),
        pk=pk,
    )
    equipos = mantenimiento.equipos.select_related('equipo')
    eventos = mantenimiento.eventos.select_related('usuario').order_by('-timestamp')

    checklist_items = ActividadChecklist.objects.filter(activo=True).order_by('orden', 'nombre')
    realizadas = {
        ar.actividad_id: ar.realizada
        for ar in mantenimiento.actividades_realizadas.all()
    }
    checklist = [{'item': item, 'realizada': realizadas.get(item.pk, False)} for item in checklist_items]

    return render(request, 'panel/mantenimiento_detalle.html', {
        'mantenimiento': mantenimiento, 'equipos': equipos, 'eventos': eventos, 'checklist': checklist,
    })


@login_required
def mantenimiento_iniciar(request, pk):
    mantenimiento = get_object_or_404(Mantenimiento, pk=pk)
    try:
        mantenimiento_services.iniciar_mantenimiento(mantenimiento=mantenimiento, usuario=request.user)
    except ValueError as exc:
        messages.error(request, str(exc))
    else:
        registrar_evento(
            usuario=request.user, accion='mantenimiento.iniciar', objeto=mantenimiento, request=request,
        )
        messages.success(request, f'Mantenimiento #{mantenimiento.pk} iniciado.')
    return redirect('panel:mantenimiento_detalle', pk=pk)


@login_required
def mantenimiento_checklist_actualizar(request, pk):
    mantenimiento = get_object_or_404(Mantenimiento, pk=pk)
    if request.method == 'POST':
        for item in ActividadChecklist.objects.filter(activo=True):
            realizada = request.POST.get(f'actividad_{item.pk}') == 'on'
            mantenimiento_services.registrar_actividad_checklist(
                mantenimiento=mantenimiento, actividad=item, realizada=realizada, usuario=request.user,
            )
        registrar_evento(
            usuario=request.user, accion='mantenimiento.checklist_actualizar', objeto=mantenimiento, request=request,
        )
        messages.success(request, 'Checklist actualizado.')
    return redirect('panel:mantenimiento_detalle', pk=pk)


@login_required
def mantenimiento_cerrar(request, pk):
    mantenimiento = get_object_or_404(Mantenimiento, pk=pk)
    if request.method == 'POST':
        form = CerrarMantenimientoForm(request.POST)
        if form.is_valid():
            try:
                mantenimiento_services.cerrar_mantenimiento(
                    mantenimiento=mantenimiento, resultado_tecnico=form.cleaned_data['resultado_tecnico'],
                    usuario=request.user,
                )
            except ValueError as exc:
                form.add_error(None, str(exc))
            else:
                registrar_evento(
                    usuario=request.user, accion='mantenimiento.cerrar', objeto=mantenimiento, request=request,
                )
                messages.success(request, f'Mantenimiento #{mantenimiento.pk} cerrado.')
                return redirect('panel:mantenimiento_detalle', pk=pk)
    else:
        form = CerrarMantenimientoForm()
    return render(request, 'panel/accion_form.html', {
        'form': form, 'titulo': f'Cerrar mantenimiento #{mantenimiento.pk}',
        'subtitulo': 'Si el resultado es "Requiere baja", se marcará baja_recomendada en los equipos cubiertos.',
        'volver_url': reverse('panel:mantenimiento_detalle', args=[pk]),
    })


@login_required
def mantenimiento_cancelar(request, pk):
    mantenimiento = get_object_or_404(Mantenimiento, pk=pk)
    if request.method == 'POST':
        form = CancelarMantenimientoForm(request.POST)
        if form.is_valid():
            try:
                mantenimiento_services.cancelar_mantenimiento(
                    mantenimiento=mantenimiento, motivo=form.cleaned_data['motivo'], usuario=request.user,
                )
            except ValueError as exc:
                form.add_error(None, str(exc))
            else:
                registrar_evento(
                    usuario=request.user, accion='mantenimiento.cancelar', objeto=mantenimiento, request=request,
                )
                messages.success(request, f'Mantenimiento #{mantenimiento.pk} cancelado.')
                return redirect('panel:mantenimiento_detalle', pk=pk)
    else:
        form = CancelarMantenimientoForm()
    return render(request, 'panel/accion_form.html', {
        'form': form, 'titulo': f'Cancelar mantenimiento #{mantenimiento.pk}',
        'volver_url': reverse('panel:mantenimiento_detalle', args=[pk]),
    })


@login_required
def mantenimientos_programados_lista(request):
    programados = MantenimientoProgramado.objects.select_related('equipo', 'tecnico').order_by('fecha_proximo')
    return render(request, 'panel/mantenimientos_programados_lista.html', {'programados': programados})


@login_required
def mantenimiento_programado_crear(request):
    if request.method == 'POST':
        form = MantenimientoProgramadoForm(request.POST)
        if form.is_valid():
            programado = form.save()
            registrar_evento(
                usuario=request.user, accion='mantenimiento_programado.crear', objeto=programado, request=request,
            )
            messages.success(request, 'Mantenimiento programado creado.')
            return redirect('panel:mantenimientos_programados_lista')
    else:
        form = MantenimientoProgramadoForm()
    return render(request, 'panel/accion_form.html', {
        'form': form, 'titulo': 'Nuevo mantenimiento programado',
        'volver_url': reverse('panel:mantenimientos_programados_lista'),
    })
