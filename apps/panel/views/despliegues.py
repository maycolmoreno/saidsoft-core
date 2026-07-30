from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from apps.auditoria.models import registrar_evento
from apps.cuentas.services import scope_por_unidad_negocio_activa, verificar_acceso
from apps.despliegues.models import Despliegue, ResultadoDespliegue
from apps.despliegues.services import publicar_despliegue

from ..forms import DespliegueForm, PromoverDespliegueForm


@login_required
def despliegues_lista(request):
    despliegues = scope_por_unidad_negocio_activa(
        Despliegue.objects.select_related('creado_por', 'aprobado_por').order_by('-fecha_creacion'),
        request, 'unidad_negocio',
    )
    return render(request, 'panel/despliegues_lista.html', {'despliegues': despliegues})


@login_required
def despliegue_crear(request):
    if request.method == 'POST':
        form = DespliegueForm(request.POST, request.FILES, user=request.user)
        if form.is_valid():
            despliegue = form.save(commit=False)
            despliegue.creado_por = request.user
            despliegue.estado = Despliegue.Estado.PENDIENTE_APROBACION
            despliegue.save()
            form.save_m2m()
            registrar_evento(usuario=request.user, accion='despliegue.crear', objeto=despliegue, request=request)
            messages.success(request, f'Despliegue {despliegue.version} creado, pendiente de aprobación.')
            return redirect('panel:despliegue_detalle', pk=despliegue.pk)
    else:
        form = DespliegueForm(user=request.user)
    return render(request, 'panel/despliegue_form.html', {'form': form})


@login_required
def despliegue_detalle(request, pk):
    despliegue = get_object_or_404(
        Despliegue.objects
        .select_related('creado_por', 'aprobado_por', 'despliegue_origen')
        .prefetch_related('grupos', 'farmacias', 'promovidos'),
        pk=pk,
    )
    verificar_acceso(request.user, despliegue.unidad_negocio)
    puede_aprobar = (
        despliegue.estado == Despliegue.Estado.PENDIENTE_APROBACION
        and despliegue.creado_por_id != request.user.id
    )
    puede_promover = despliegue.estado == Despliegue.Estado.COMPLETADO
    return render(request, 'panel/despliegue_detalle.html', {
        'despliegue': despliegue,
        'puede_aprobar': puede_aprobar,
        'puede_promover': puede_promover,
    })


@login_required
def despliegue_promover(request, pk):
    origen = get_object_or_404(Despliegue, pk=pk)
    verificar_acceso(request.user, origen.unidad_negocio)
    if origen.estado != Despliegue.Estado.COMPLETADO:
        messages.error(request, 'Solo se puede promover un despliegue ya completado.')
        return redirect('panel:despliegue_detalle', pk=pk)

    if request.method == 'POST':
        form = PromoverDespliegueForm(request.POST, unidad_negocio=origen.unidad_negocio)
        if form.is_valid():
            nuevo = form.save(commit=False)
            nuevo.version = origen.version
            nuevo.archivo = origen.archivo
            nuevo.sha256 = origen.sha256
            nuevo.descripcion = f'Anillo siguiente de despliegue #{origen.pk} ({origen.get_destino_tipo_display()})'
            nuevo.modo_aplicacion = origen.modo_aplicacion
            nuevo.unidad_negocio = origen.unidad_negocio
            nuevo.creado_por = request.user
            nuevo.estado = Despliegue.Estado.PENDIENTE_APROBACION
            nuevo.despliegue_origen = origen
            nuevo.save()
            form.save_m2m()
            registrar_evento(usuario=request.user, accion='despliegue.promover', objeto=nuevo, request=request)
            messages.success(request, f'Anillo siguiente creado para v{nuevo.version}, pendiente de aprobación.')
            return redirect('panel:despliegue_detalle', pk=nuevo.pk)
    else:
        form = PromoverDespliegueForm(unidad_negocio=origen.unidad_negocio)

    return render(request, 'panel/accion_form.html', {
        'form': form, 'titulo': f'Promover v{origen.version} al siguiente anillo',
        'subtitulo': f'Mismo paquete y versión que el despliegue #{origen.pk}; solo cambia a quién llega.',
        'volver_url': reverse('panel:despliegue_detalle', args=[pk]),
    })


@login_required
def despliegue_progreso_partial(request, pk):
    despliegue = get_object_or_404(Despliegue, pk=pk)
    verificar_acceso(request.user, despliegue.unidad_negocio)
    resultados = despliegue.resultados.select_related('estacion', 'estacion__farmacia').order_by('estacion__codigo')

    total = resultados.count()
    conteo = {estado: 0 for estado, _ in ResultadoDespliegue.Estado.choices}
    for r in resultados:
        conteo[r.estado] += 1
    aplicados = conteo.get(ResultadoDespliegue.Estado.APLICADO, 0)
    errores = conteo.get(ResultadoDespliegue.Estado.ERROR, 0) + conteo.get(ResultadoDespliegue.Estado.ROLLBACK, 0)
    pct_completado = round(100 * aplicados / total) if total else 0

    return render(request, 'panel/despliegue_progreso_partial.html', {
        'despliegue': despliegue,
        'resultados': resultados,
        'total': total,
        'aplicados': aplicados,
        'errores': errores,
        'pct_completado': pct_completado,
        'conteo': conteo,
    })


@login_required
@require_POST
def despliegue_aprobar(request, pk):
    despliegue = get_object_or_404(Despliegue, pk=pk)
    verificar_acceso(request.user, despliegue.unidad_negocio)
    if despliegue.estado != Despliegue.Estado.PENDIENTE_APROBACION:
        messages.error(request, 'Este despliegue ya no está pendiente de aprobación.')
    elif despliegue.creado_por_id == request.user.id:
        messages.error(request, 'Quien crea un despliegue no puede aprobarlo (regla de cuatro ojos).')
    else:
        despliegue.estado = Despliegue.Estado.APROBADO
        despliegue.aprobado_por = request.user
        despliegue.save(update_fields=['estado', 'aprobado_por'])
        registrar_evento(usuario=request.user, accion='despliegue.aprobar', objeto=despliegue, request=request)
        messages.success(request, f'Despliegue {despliegue.version} aprobado.')
    return redirect('panel:despliegue_detalle', pk=pk)


@login_required
@require_POST
def despliegue_publicar(request, pk):
    despliegue = get_object_or_404(Despliegue, pk=pk)
    verificar_acceso(request.user, despliegue.unidad_negocio)
    if despliegue.estado != Despliegue.Estado.APROBADO:
        messages.error(request, 'El despliegue debe estar aprobado antes de publicarse.')
    else:
        total = publicar_despliegue(despliegue)
        registrar_evento(
            usuario=request.user, accion='despliegue.publicar', objeto=despliegue,
            detalle={'estaciones_destino': total}, request=request,
        )
        messages.success(request, f'Despliegue publicado a {total} estación(es).')
    return redirect('panel:despliegue_detalle', pk=pk)


@login_required
@require_POST
def despliegue_pausar(request, pk):
    despliegue = get_object_or_404(Despliegue, pk=pk)
    verificar_acceso(request.user, despliegue.unidad_negocio)
    if despliegue.estado == Despliegue.Estado.PUBLICANDO:
        despliegue.estado = Despliegue.Estado.PAUSADO
        despliegue.save(update_fields=['estado'])
        registrar_evento(usuario=request.user, accion='despliegue.pausar', objeto=despliegue, request=request)
        messages.success(request, 'Despliegue pausado.')
    return redirect('panel:despliegue_detalle', pk=pk)


@login_required
@require_POST
def despliegue_reanudar(request, pk):
    despliegue = get_object_or_404(Despliegue, pk=pk)
    verificar_acceso(request.user, despliegue.unidad_negocio)
    if despliegue.estado == Despliegue.Estado.PAUSADO:
        despliegue.estado = Despliegue.Estado.PUBLICANDO
        # Reanudar es una decisión consciente del operador: marca que ya vio los errores,
        # para que el próximo reporte de error no lo vuelva a frenar en un bucle.
        despliegue.freno_omitido = True
        despliegue.save(update_fields=['estado', 'freno_omitido'])
        registrar_evento(usuario=request.user, accion='despliegue.reanudar', objeto=despliegue, request=request)
        messages.success(request, 'Despliegue reanudado. No se volverá a frenar automáticamente por errores.')
    return redirect('panel:despliegue_detalle', pk=pk)
