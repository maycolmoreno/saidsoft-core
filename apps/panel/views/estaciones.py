from django.contrib import messages
from django.contrib.auth.decorators import login_required, permission_required
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from apps.auditoria.models import registrar_evento
from apps.catalogo.models import Estacion, Grupo
from apps.catalogo.services import (
    enviar_comando, url_escritorio_remoto_meshcentral, url_terminal_remoto_meshcentral,
)


@login_required
def estaciones_lista(request):
    estaciones = Estacion.objects.select_related('farmacia', 'farmacia__grupo').order_by('codigo')

    grupo = request.GET.get('grupo')
    estado_conexion = request.GET.get('estado_conexion')
    solo_desactualizadas = request.GET.get('desactualizadas')

    if grupo:
        estaciones = estaciones.filter(farmacia__grupo__codigo=grupo)
    if estado_conexion:
        estaciones = estaciones.filter(estado_conexion=estado_conexion)
    if solo_desactualizadas:
        estaciones = [e for e in estaciones if e.desactualizada]

    return render(request, 'panel/estaciones_lista.html', {
        'estaciones': estaciones,
        'grupos': Grupo.objects.order_by('codigo'),
        'filtro_grupo': grupo or '',
        'filtro_estado': estado_conexion or '',
        'filtro_desactualizadas': solo_desactualizadas or '',
    })


@login_required
def estaciones_pendientes_partial(request):
    pendientes = Estacion.objects.select_related('farmacia', 'farmacia__grupo').filter(
        estado_aprobacion=Estacion.EstadoAprobacion.PENDIENTE,
    ).order_by('codigo')
    return render(request, 'panel/estaciones_pendientes_partial.html', {'pendientes': pendientes})


@login_required
@require_POST
def estacion_aprobar(request, pk):
    estacion = get_object_or_404(Estacion, pk=pk)
    estacion.estado_aprobacion = Estacion.EstadoAprobacion.APROBADA
    estacion.save(update_fields=['estado_aprobacion'])
    registrar_evento(usuario=request.user, accion='estacion.aprobar', objeto=estacion, request=request)
    return estaciones_pendientes_partial(request)


@login_required
@require_POST
def estacion_rechazar(request, pk):
    estacion = get_object_or_404(Estacion, pk=pk)
    estacion.estado_aprobacion = Estacion.EstadoAprobacion.RECHAZADA
    estacion.save(update_fields=['estado_aprobacion'])
    registrar_evento(usuario=request.user, accion='estacion.rechazar', objeto=estacion, request=request)
    return estaciones_pendientes_partial(request)


@login_required
@require_POST
def estacion_reiniciar(request, pk):
    estacion = get_object_or_404(Estacion, pk=pk)
    if estacion.estado_aprobacion != Estacion.EstadoAprobacion.APROBADA:
        messages.error(request, 'La estación no está aprobada.')
    elif estacion.estado_conexion != Estacion.EstadoConexion.ONLINE:
        messages.error(request, f'{estacion.codigo} no está en línea; no se envió el reinicio.')
    elif enviar_comando(estacion, 'reiniciar'):
        registrar_evento(usuario=request.user, accion='estacion.reiniciar', objeto=estacion, request=request)
        messages.success(request, f'Reinicio enviado a {estacion.codigo}.')
    else:
        messages.error(request, f'No se pudo enviar el reinicio a {estacion.codigo} (broker MQTT no disponible).')
    return redirect('panel:estaciones_lista')


@login_required
def estacion_info_modal(request, pk):
    estacion = get_object_or_404(Estacion, pk=pk)
    return render(request, 'panel/estacion_info_modal.html', {'estacion': estacion})


@login_required
@require_POST
def estacion_info_solicitar(request, pk):
    estacion = get_object_or_404(Estacion, pk=pk)
    solicitado = False
    if estacion.estado_aprobacion == Estacion.EstadoAprobacion.APROBADA and enviar_comando(estacion, 'consultar_info'):
        registrar_evento(usuario=request.user, accion='estacion.consultar_info', objeto=estacion, request=request)
        solicitado = True
    return render(request, 'panel/estacion_info_modal.html', {'estacion': estacion, 'solicitado': solicitado})


@login_required
@permission_required('catalogo.acceso_remoto_estacion', raise_exception=True)
@require_POST
def estacion_meshcentral_vincular(request, pk):
    estacion = get_object_or_404(Estacion, pk=pk)
    node_id = request.POST.get('meshcentral_node_id', '').strip()
    estacion.meshcentral_node_id = node_id
    estacion.meshcentral_vinculado_en = timezone.now() if node_id else None
    estacion.save(update_fields=['meshcentral_node_id', 'meshcentral_vinculado_en'])
    registrar_evento(
        usuario=request.user, accion='estacion.meshcentral_vincular', objeto=estacion,
        detalle={'meshcentral_node_id': node_id}, request=request,
    )
    return render(request, 'panel/estacion_info_modal.html', {'estacion': estacion})


@login_required
@permission_required('catalogo.acceso_remoto_estacion', raise_exception=True)
@require_POST
def estacion_meshcentral_escritorio(request, pk):
    estacion = get_object_or_404(Estacion, pk=pk)
    url = url_escritorio_remoto_meshcentral(estacion)
    if not url:
        messages.error(request, f'{estacion.codigo} todavía no tiene un node_id de MeshCentral vinculado.')
        return redirect('panel:estaciones_lista')
    registrar_evento(
        usuario=request.user, accion='estacion.meshcentral_abrir_escritorio', objeto=estacion,
        detalle={'meshcentral_node_id': estacion.meshcentral_node_id}, request=request,
    )
    return redirect(url)


@login_required
@permission_required('catalogo.acceso_remoto_estacion', raise_exception=True)
@require_POST
def estacion_meshcentral_terminal(request, pk):
    estacion = get_object_or_404(Estacion, pk=pk)
    url = url_terminal_remoto_meshcentral(estacion)
    if not url:
        messages.error(request, f'{estacion.codigo} todavía no tiene un node_id de MeshCentral vinculado.')
        return redirect('panel:estaciones_lista')
    registrar_evento(
        usuario=request.user, accion='estacion.meshcentral_abrir_terminal', objeto=estacion,
        detalle={'meshcentral_node_id': estacion.meshcentral_node_id}, request=request,
    )
    return redirect(url)


@login_required
@require_POST
def estaciones_aprobar_lote(request):
    ids = request.POST.getlist('estacion_ids')
    estaciones = Estacion.objects.filter(pk__in=ids, estado_aprobacion=Estacion.EstadoAprobacion.PENDIENTE)
    for estacion in estaciones:
        estacion.estado_aprobacion = Estacion.EstadoAprobacion.APROBADA
        estacion.save(update_fields=['estado_aprobacion'])
        registrar_evento(usuario=request.user, accion='estacion.aprobar', objeto=estacion, request=request)
    return estaciones_pendientes_partial(request)
