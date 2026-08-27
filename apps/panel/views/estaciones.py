from django.contrib import messages
from django.contrib.auth.decorators import login_required, permission_required
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from apps.auditoria.models import registrar_evento
from apps.catalogo.models import Estacion, Grupo, VersionAgente
from apps.catalogo.services import (
    enviar_actualizacion_agente, enviar_comando, enviar_configurar_nodo_pos, obtener_clave_bitlocker_descifrada,
    obtener_password_nodo,
    url_escritorio_remoto_meshcentral, url_grabaciones_meshcentral, url_terminal_remoto_meshcentral,
)
from apps.cuentas.services import scope_por_unidad_negocio, scope_por_unidad_negocio_activa, verificar_acceso
from apps.monitoreo.services import ventana_mantenimiento_activa


def _render_info_modal(request, estacion, **extra):
    ultima_version_agente = VersionAgente.objects.order_by('-fecha_creacion').first()
    agente_desactualizado = bool(
        ultima_version_agente and estacion.version_agente
        and estacion.version_agente != ultima_version_agente.version,
    )
    contexto = {
        'estacion': estacion, 'ventana_mantenimiento': ventana_mantenimiento_activa(estacion),
        'ultima_version_agente': ultima_version_agente, 'agente_desactualizado': agente_desactualizado,
        **extra,
    }
    return render(request, 'panel/estacion_info_modal.html', contexto)


@login_required
@permission_required('catalogo.view_estacion', raise_exception=True)
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
@permission_required('catalogo.view_estacion', raise_exception=True)
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
@permission_required('catalogo.view_estacion', raise_exception=True)
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
@permission_required('catalogo.actualizar_agente_estacion', raise_exception=True)
@require_POST
def estacion_actualizar_agente_solicitar(request, pk):
    """Dispara la actualización remota del agente a la última VersionAgente cargada —
    el agente se detiene, reemplaza su propio ejecutable y vuelve a arrancar solo (ver
    agente-prueba/agente_prueba.py). Acción de riesgo: si el reemplazo falla a mitad de
    camino, la estación queda sin agente hasta arreglarla a mano (queda un .bak junto al
    ejecutable para poder revertir)."""
    estacion = get_object_or_404(Estacion, pk=pk)
    verificar_acceso(request.user, estacion.farmacia.unidad_negocio)
    version = VersionAgente.objects.order_by('-fecha_creacion').first()
    solicitado_agente = False
    error_agente = ''
    if not version:
        error_agente = 'No hay ninguna versión de agente cargada todavía (Admin → Versiones de agente).'
    elif estacion.estado_aprobacion != Estacion.EstadoAprobacion.APROBADA:
        error_agente = 'La estación no está aprobada.'
    elif estacion.estado_conexion != Estacion.EstadoConexion.ONLINE:
        error_agente = f'{estacion.codigo} no está en línea; no se envió la actualización.'
    elif enviar_actualizacion_agente(estacion, version):
        registrar_evento(usuario=request.user, accion='estacion.actualizar_agente', objeto=estacion, request=request)
        solicitado_agente = True
    else:
        error_agente = f'No se pudo enviar la actualización a {estacion.codigo} (broker MQTT no disponible).'
    return _render_info_modal(request, estacion, solicitado_agente=solicitado_agente, error_agente=error_agente)


@login_required
@permission_required('catalogo.change_farmacia', raise_exception=True)
@require_POST
def farmacia_aplicar_nodo_pos(request, pk):
    """Reapunta el POS de TODAS las estaciones de la farmacia de `pk` al nodo (Grupo)
    que tiene asignado — el paso de "empujar la config" del balanceo de carga.

    Se hace a nivel farmacia y no de estación porque el balanceo mueve sitios enteros:
    dejar media farmacia en un nodo y media en otro sería un estado incoherente.

    Permiso change_farmacia (no consultar_info_estacion): esto reescribe la config del
    POS de producción, no es diagnóstico. El agente igual espera a que el POS se cierre
    antes de tocar el archivo (ver enviar_configurar_nodo_pos), así que el cambio no
    interrumpe ventas ni se confirma al instante: se ve cuando el heartbeat empieza a
    reportar el nodo nuevo.
    """
    estacion = get_object_or_404(Estacion, pk=pk)
    farmacia = estacion.farmacia
    verificar_acceso(request.user, farmacia.unidad_negocio)
    grupo = farmacia.grupo

    password = obtener_password_nodo(grupo)
    if not (grupo.pos_servidor and grupo.pos_puerto and password):
        faltan = [n for n, v in (
            ('servidor', grupo.pos_servidor), ('puerto', grupo.pos_puerto), ('contraseña', password),
        ) if not v]
        return _render_info_modal(request, estacion, error_nodo=(
            f'Al nodo "{grupo.codigo}" le falta cargar: {", ".join(faltan)} '
            f'(Admin → Catálogo → Grupos). Sin eso el POS no podría conectarse.'
        ))

    destinatarias = list(farmacia.estaciones.filter(
        estado_aprobacion=Estacion.EstadoAprobacion.APROBADA,
        estado_conexion=Estacion.EstadoConexion.ONLINE,
    ))
    enviadas = sum(
        1 for e in destinatarias
        if enviar_configurar_nodo_pos(
            e, servidor=grupo.pos_servidor, bdd=grupo.codigo, puerto=grupo.pos_puerto, password=password,
        )
    )
    registrar_evento(
        usuario=request.user, accion='farmacia.aplicar_nodo_pos', objeto=farmacia,
        detalle={'nodo': grupo.codigo, 'servidor': grupo.pos_servidor, 'estaciones': enviadas},
        request=request,
    )
    offline = farmacia.estaciones.filter(estado_aprobacion=Estacion.EstadoAprobacion.APROBADA).count() - len(destinatarias)
    return _render_info_modal(request, estacion, nodo_enviado={
        'nodo': grupo.codigo, 'servidor': grupo.pos_servidor, 'puerto': grupo.pos_puerto,
        'enviadas': enviadas, 'offline': offline,
    })


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
@permission_required('catalogo.consultar_info_estacion', raise_exception=True)
@require_POST
def estacion_perifericos_solicitar(request, pk):
    """Dispara un escaneo puntual de periféricos USB (ver
    apps.mqtt_worker.services.manejar_perifericos). Mismo permiso que el escaneo de
    software instalado: es diagnóstico, no una acción de riesgo."""
    estacion = get_object_or_404(Estacion, pk=pk)
    verificar_acceso(request.user, estacion.farmacia.unidad_negocio)
    solicitado_perifericos = False
    if (
        estacion.estado_aprobacion == Estacion.EstadoAprobacion.APROBADA
        and enviar_comando(estacion, 'consultar_perifericos')
    ):
        registrar_evento(
            usuario=request.user, accion='estacion.consultar_perifericos', objeto=estacion, request=request,
        )
        solicitado_perifericos = True
    return _render_info_modal(request, estacion, solicitado_perifericos=solicitado_perifericos)


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
