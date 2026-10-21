from django.contrib import messages
from django.contrib.auth.decorators import login_required, permission_required
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from ..busqueda import buscar
from apps.auditoria.models import registrar_evento
from apps.cuentas.services import scope_por_unidad_negocio_activa, verificar_acceso
from apps.despliegues.models import Despliegue, ResultadoDespliegue
from apps.despliegues.services import (
    aprobar_despliegue, marcar_reanudado, pausar_despliegue, publicar_despliegue,
    reintentar_despliegue,
)

from ..forms import DespliegueForm, PromoverDespliegueForm
from ..progreso import resumen_de_progreso


@login_required
@permission_required('despliegues.view_despliegue', raise_exception=True)
def despliegues_lista(request):
    despliegues = scope_por_unidad_negocio_activa(
        Despliegue.objects.select_related('creado_por', 'aprobado_por').order_by('-fecha_creacion'),
        request, 'unidad_negocio',
    )
    # Buscador: un despliegue por version del POS. No crece rapido, pero no se borra
    # nunca, y "en que despliegue salio la 2.4.1" es una pregunta que se hace sola.
    despliegues, busqueda = buscar(despliegues, request, (
        'version', 'descripcion', 'estado', 'sha256', 'creado_por__username',
    ))
    return render(request, 'panel/despliegues_lista.html', {
        'despliegues': despliegues,
        'busqueda': busqueda,
        'busqueda_pista': 'Version, descripcion, estado o hash...',
    })


@login_required
@permission_required('despliegues.add_despliegue', raise_exception=True)
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
@permission_required('despliegues.view_despliegue', raise_exception=True)
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
@permission_required('despliegues.add_despliegue', raise_exception=True)
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

    return render(request, 'panel/despliegue_promover_form.html', {
        'form': form, 'titulo': f'Promover v{origen.version} al siguiente anillo',
        'subtitulo': f'Mismo paquete y versión que el despliegue #{origen.pk}; solo cambia a quién llega.',
        'volver_url': reverse('panel:despliegue_detalle', args=[pk]),
    })


@login_required
@permission_required('despliegues.view_despliegue', raise_exception=True)
def despliegue_progreso_partial(request, pk):
    despliegue = get_object_or_404(Despliegue, pk=pk)
    verificar_acceso(request.user, despliegue.unidad_negocio)
    resultados = despliegue.resultados.select_related('estacion', 'estacion__farmacia').order_by('estacion__codigo')

    estados = ResultadoDespliegue.Estado
    progreso = resumen_de_progreso(
        resultados, estados,
        estado_ok=estados.APLICADO,
        # El rollback cuenta como error: la estación quedó sin la versión nueva, aunque
        # el agente haya sabido volver atrás sin romper el POS.
        estados_error=(estados.ERROR, estados.ROLLBACK),
        etiqueta_completados='aplicados',
        estados_visibles=(estados.PENDIENTE, estados.DESCARGANDO, estados.APLICANDO),
    )
    return render(request, 'panel/despliegue_progreso_partial.html', {
        'despliegue': despliegue,
        'resultados': resultados,
        'progreso': progreso,
    })


@login_required
@permission_required('despliegues.aprobar_despliegue', raise_exception=True)
@require_POST
def despliegue_aprobar(request, pk):
    despliegue = get_object_or_404(Despliegue, pk=pk)
    verificar_acceso(request.user, despliegue.unidad_negocio)
    # El estado y la regla de cuatro ojos las valida el servicio (`D-5`): es gobernanza, y
    # en la vista la saltea cualquier otra vía. Acá quedan el permiso (decorador), el acceso
    # por unidad de negocio, la auditoría —con `request`, para no perder la IP— y el mensaje.
    try:
        aprobar_despliegue(despliegue=despliegue, usuario=request.user)
    except ValueError as exc:
        messages.error(request, str(exc))
    else:
        registrar_evento(usuario=request.user, accion='despliegue.aprobar', objeto=despliegue, request=request)
        messages.success(request, f'Despliegue {despliegue.version} aprobado.')
    return redirect('panel:despliegue_detalle', pk=pk)


@login_required
@permission_required('despliegues.change_despliegue', raise_exception=True)
@require_POST
def despliegue_publicar(request, pk):
    despliegue = get_object_or_404(Despliegue, pk=pk)
    verificar_acceso(request.user, despliegue.unidad_negocio)
    if despliegue.estado != Despliegue.Estado.APROBADO:
        messages.error(request, 'El despliegue debe estar aprobado antes de publicarse.')
    else:
        resultado = publicar_despliegue(despliegue)
        registrar_evento(
            usuario=request.user, accion='despliegue.publicar', objeto=despliegue,
            detalle={'estaciones_destino': resultado.total_estaciones, 'exitoso': resultado.exitoso,
                     **resultado.conteos()},
            request=request,
        )
        if resultado.confirmada:
            messages.success(
                request, f'Despliegue en curso ({resultado.total_estaciones} estación(es) destino): '
                         f'{resultado.resumen()}',
            )
        elif resultado.exitoso:
            # En curso, pero no todo confirmado: no se presenta como publicado.
            messages.warning(request, f'Despliegue en curso, con publicaciones sin confirmar: {resultado.resumen()}')
        else:
            messages.error(
                request,
                'No se pudo publicar: el broker MQTT no estaba disponible o rechazó la '
                'publicación (revisar la conexión, la credencial o la ACL del panel). El despliegue '
                'sigue en estado "Aprobado" — puede reintentar publicar cuando el broker esté disponible.',
            )
    return redirect('panel:despliegue_detalle', pk=pk)


@login_required
@permission_required('despliegues.change_despliegue', raise_exception=True)
@require_POST
def despliegue_pausar(request, pk):
    despliegue = get_object_or_404(Despliegue, pk=pk)
    verificar_acceso(request.user, despliegue.unidad_negocio)
    # Antes, si el despliegue no estaba publicando esta vista no hacía NADA y redirigía
    # igual: indistinguible de "se pausó". Ahora el servicio dice por qué no se pudo
    # (`D-5`, §10-BW) — misma corrección que ya se hizo en el lote de aprobación.
    try:
        pausar_despliegue(despliegue=despliegue)
    except ValueError as exc:
        messages.error(request, str(exc))
    else:
        registrar_evento(usuario=request.user, accion='despliegue.pausar', objeto=despliegue, request=request)
        messages.success(request, 'Despliegue pausado.')
    return redirect('panel:despliegue_detalle', pk=pk)


@login_required
@permission_required('despliegues.change_despliegue', raise_exception=True)
@require_POST
def despliegue_reanudar(request, pk):
    despliegue = get_object_or_404(Despliegue, pk=pk)
    verificar_acceso(request.user, despliegue.unidad_negocio)
    if despliegue.estado != Despliegue.Estado.PAUSADO:
        return redirect('panel:despliegue_detalle', pk=pk)

    # Antes esto solo cambiaba el estado a PUBLICANDO sin reenviar nada por MQTT —
    # "reanudar" no reintentaba de verdad, había que reiniciar el agente a mano para
    # que reconectara y recogiera el mensaje retenido de la publicación original.
    resultado = reintentar_despliegue(despliegue)
    if not resultado.exitoso:
        messages.error(
            request,
            'No se pudo reenviar el paquete: el broker MQTT no estaba disponible o rechazó la '
            'publicación (revisar la conexión, la credencial o la ACL del panel). '
            'El despliegue sigue pausado — reintentá "Reanudar" cuando el broker esté disponible.',
        )
        return redirect('panel:despliegue_detalle', pk=pk)

    # La transición (estado + `freno_omitido`) vive en el servicio (`D-5`), y se llama
    # SOLO acá, después de que el reenvío salió bien: si el broker falla, el despliegue
    # sigue pausado en vez de quedar "publicando" algo que nunca salió.
    marcar_reanudado(despliegue=despliegue)
    registrar_evento(
        usuario=request.user, accion='despliegue.reanudar', objeto=despliegue,
        detalle={'estaciones_reintentadas': resultado.total_estaciones, **resultado.conteos()}, request=request,
    )
    if resultado.total_estaciones and not resultado.confirmada:
        messages.warning(request, f'Despliegue reanudado, con publicaciones sin confirmar: {resultado.resumen()}')
    elif resultado.total_estaciones:
        messages.success(
            request, f'Despliegue reanudado ({resultado.total_estaciones} estación(es) pendiente(s)): '
                     f'{resultado.resumen()}',
        )
    else:
        messages.success(request, 'Despliegue reanudado. No quedaban estaciones pendientes de reintentar.')
    return redirect('panel:despliegue_detalle', pk=pk)
