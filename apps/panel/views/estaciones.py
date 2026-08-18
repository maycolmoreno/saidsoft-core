from django.contrib import messages
from django.contrib.auth.decorators import login_required, permission_required
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from apps.auditoria.models import registrar_evento
from apps.catalogo.models import Estacion, Grupo
from apps.catalogo.services import (
    enviar_comando, obtener_clave_bitlocker_descifrada, url_escritorio_remoto_meshcentral,
    url_grabaciones_meshcentral, url_terminal_remoto_meshcentral,
)
from apps.cuentas.services import scope_por_unidad_negocio, scope_por_unidad_negocio_activa, verificar_acceso
from apps.monitoreo.services import ventana_mantenimiento_activa


def _render_info_modal(request, estacion, **extra):
    contexto = {'estacion': estacion, 'ventana_mantenimiento': ventana_mantenimiento_activa(estacion), **extra}
    return render(request, 'panel/estacion_info_modal.html', contexto)


@login_required
def estaciones_lista(request):
    estaciones = scope_por_unidad_negocio_activa(
        Estacion.objects.select_related('farmacia', 'farmacia__grupo').order_by('codigo'),
        request, 'farmacia__unidad_negocio',
    )

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
def estaciones_pendientes_partial(request, aviso=''):
    pendientes = scope_por_unidad_negocio(
        Estacion.objects.select_related('farmacia', 'farmacia__grupo').filter(
            estado_aprobacion=Estacion.EstadoAprobacion.PENDIENTE,
        ),
        request.user, 'farmacia__unidad_negocio',
    ).order_by('codigo')
    # `aviso` se renderiza dentro del propio partial: las vistas de abajo devuelven
    # este HTML por HTMX (sin recargar la página), así que un messages.error() no se
    # vería hasta la próxima navegación completa.
    return render(request, 'panel/estaciones_pendientes_partial.html', {
        'pendientes': pendientes, 'aviso': aviso,
    })


@login_required
@permission_required('catalogo.aprobar_estacion', raise_exception=True)
@require_POST
def estacion_aprobar(request, pk):
    estacion = get_object_or_404(Estacion, pk=pk)
    verificar_acceso(request.user, estacion.farmacia.unidad_negocio)
    estacion.estado_aprobacion = Estacion.EstadoAprobacion.APROBADA
    estacion.save(update_fields=['estado_aprobacion'])
    registrar_evento(usuario=request.user, accion='estacion.aprobar', objeto=estacion, request=request)
    return estaciones_pendientes_partial(request)


@login_required
@permission_required('catalogo.aprobar_estacion', raise_exception=True)
@require_POST
def estacion_rechazar(request, pk):
    estacion = get_object_or_404(Estacion, pk=pk)
    verificar_acceso(request.user, estacion.farmacia.unidad_negocio)
    estacion.estado_aprobacion = Estacion.EstadoAprobacion.RECHAZADA
    estacion.save(update_fields=['estado_aprobacion'])
    registrar_evento(usuario=request.user, accion='estacion.rechazar', objeto=estacion, request=request)
    return estaciones_pendientes_partial(request)


@login_required
@permission_required('catalogo.reiniciar_estacion', raise_exception=True)
@require_POST
def estacion_reiniciar(request, pk):
    estacion = get_object_or_404(Estacion, pk=pk)
    verificar_acceso(request.user, estacion.farmacia.unidad_negocio)
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
    verificar_acceso(request.user, estacion.farmacia.unidad_negocio)
    return _render_info_modal(request, estacion)


@login_required
@permission_required('catalogo.consultar_info_estacion', raise_exception=True)
@require_POST
def estacion_info_solicitar(request, pk):
    estacion = get_object_or_404(Estacion, pk=pk)
    verificar_acceso(request.user, estacion.farmacia.unidad_negocio)
    solicitado = False
    if estacion.estado_aprobacion == Estacion.EstadoAprobacion.APROBADA and enviar_comando(estacion, 'consultar_info'):
        registrar_evento(usuario=request.user, accion='estacion.consultar_info', objeto=estacion, request=request)
        solicitado = True
    return _render_info_modal(request, estacion, solicitado=solicitado)


@login_required
@permission_required('catalogo.escanear_actualizaciones_estacion', raise_exception=True)
@require_POST
def estacion_windows_update_solicitar(request, pk):
    """Dispara un escaneo puntual de actualizaciones de Windows pendientes — v1 es solo
    escaneo/reporte, el agente nunca instala ni reinicia solo (ver
    apps.mqtt_worker.services.manejar_windows_update)."""
    estacion = get_object_or_404(Estacion, pk=pk)
    verificar_acceso(request.user, estacion.farmacia.unidad_negocio)
    solicitado_wu = False
    if (
        estacion.estado_aprobacion == Estacion.EstadoAprobacion.APROBADA
        and enviar_comando(estacion, 'escanear_actualizaciones')
    ):
        registrar_evento(usuario=request.user, accion='estacion.escanear_actualizaciones', objeto=estacion, request=request)
        solicitado_wu = True
    return _render_info_modal(request, estacion, solicitado_wu=solicitado_wu)


@login_required
@permission_required('catalogo.consultar_info_estacion', raise_exception=True)
@require_POST
def estacion_software_instalado_solicitar(request, pk):
    """Dispara un escaneo puntual de software instalado (ver
    apps.mqtt_worker.services.manejar_software_instalado). Mismo permiso que
    "Actualizar ahora" (consultar_info_estacion): es diagnóstico, no una acción de
    riesgo — no hace falta un permiso propio."""
    estacion = get_object_or_404(Estacion, pk=pk)
    verificar_acceso(request.user, estacion.farmacia.unidad_negocio)
    solicitado_sw = False
    if (
        estacion.estado_aprobacion == Estacion.EstadoAprobacion.APROBADA
        and enviar_comando(estacion, 'consultar_software_instalado')
    ):
        registrar_evento(
            usuario=request.user, accion='estacion.consultar_software_instalado', objeto=estacion, request=request,
        )
        solicitado_sw = True
    return _render_info_modal(request, estacion, solicitado_sw=solicitado_sw)


@login_required
@permission_required('catalogo.acceso_remoto_estacion', raise_exception=True)
@require_POST
def estacion_meshcentral_vincular(request, pk):
    estacion = get_object_or_404(Estacion, pk=pk)
    verificar_acceso(request.user, estacion.farmacia.unidad_negocio)
    node_id = request.POST.get('meshcentral_node_id', '').strip()
    estacion.meshcentral_node_id = node_id
    estacion.meshcentral_vinculado_en = timezone.now() if node_id else None
    estacion.save(update_fields=['meshcentral_node_id', 'meshcentral_vinculado_en'])
    registrar_evento(
        usuario=request.user, accion='estacion.meshcentral_vincular', objeto=estacion,
        detalle={'meshcentral_node_id': node_id}, request=request,
    )
    return _render_info_modal(request, estacion)


@login_required
@permission_required('catalogo.acceso_remoto_estacion', raise_exception=True)
@require_POST
def estacion_meshcentral_escritorio(request, pk):
    estacion = get_object_or_404(Estacion, pk=pk)
    verificar_acceso(request.user, estacion.farmacia.unidad_negocio)
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
    verificar_acceso(request.user, estacion.farmacia.unidad_negocio)
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
@permission_required('catalogo.supervision_auditoria_estacion', raise_exception=True)
@require_POST
def estacion_supervision_grabaciones(request, pk):
    """Auditoría de atención al cliente por grabación (no en vivo): permiso separado de
    acceso_remoto_estacion a propósito — ver soporte remoto en vivo y revisar grabaciones
    de sesión son capacidades distintas, no todo el que tiene una debería tener la otra."""
    estacion = get_object_or_404(Estacion, pk=pk)
    verificar_acceso(request.user, estacion.farmacia.unidad_negocio)
    url = url_grabaciones_meshcentral(estacion)
    if not url:
        messages.error(request, f'{estacion.codigo} todavía no tiene un node_id de MeshCentral vinculado.')
        return redirect('panel:estaciones_lista')
    registrar_evento(
        usuario=request.user, accion='estacion.supervision_grabacion_ver', objeto=estacion,
        detalle={'meshcentral_node_id': estacion.meshcentral_node_id}, request=request,
    )
    return redirect(url)


@login_required
@permission_required('catalogo.ver_clave_bitlocker', raise_exception=True)
@require_POST
def estacion_bitlocker_ver_clave(request, pk):
    """Revela la clave de recuperación de BitLocker en el modal. Permiso propio y
    auditado a propósito: con la clave se descifra el disco completo — más sensible
    que soporte remoto en vivo o supervisión por grabación, no se hereda de ninguno."""
    estacion = get_object_or_404(Estacion, pk=pk)
    verificar_acceso(request.user, estacion.farmacia.unidad_negocio)
    clave = obtener_clave_bitlocker_descifrada(estacion)
    if clave is not None:
        registrar_evento(
            usuario=request.user, accion='estacion.bitlocker_clave_ver', objeto=estacion, request=request,
        )
    return _render_info_modal(request, estacion, bitlocker_clave_revelada=clave, bitlocker_solicitada=True)


@login_required
@permission_required('catalogo.aprobar_estacion', raise_exception=True)
@require_POST
def estaciones_aprobar_lote(request):
    ids = request.POST.getlist('estacion_ids')
    if not ids:
        # Antes esto no aprobaba nada y devolvía la tabla igual, sin decir por qué:
        # indistinguible de "se aprobó y falló en silencio" para quien lo usa.
        return estaciones_pendientes_partial(
            request, aviso='No seleccionaste ninguna estación: marcá al menos una casilla antes de aprobar en lote.',
        )
    estaciones = scope_por_unidad_negocio(
        Estacion.objects.filter(pk__in=ids, estado_aprobacion=Estacion.EstadoAprobacion.PENDIENTE),
        request.user, 'farmacia__unidad_negocio',
    )
    aprobadas = 0
    for estacion in estaciones:
        estacion.estado_aprobacion = Estacion.EstadoAprobacion.APROBADA
        estacion.save(update_fields=['estado_aprobacion'])
        registrar_evento(usuario=request.user, accion='estacion.aprobar', objeto=estacion, request=request)
        aprobadas += 1
    aviso = ''
    if aprobadas < len(ids):
        # Puede pasar si otro operador ya las aprobó, o si el filtro por unidad de
        # negocio dejó afuera alguna de las que llegaron en el POST.
        aviso = f'Se aprobaron {aprobadas} de {len(ids)} estaciones seleccionadas.'
    return estaciones_pendientes_partial(request, aviso=aviso)
