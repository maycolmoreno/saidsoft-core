"""Panel de aperturas autoprovisionadas.

Mismo patrón que `apps/panel/views/despliegues.py`: la lógica de negocio vive en
`apps.aperturas.services`, acá solo va el control de acceso, el armado del contexto y
los mensajes al operador.

La plantilla de apertura (qué estaciones se esperan y qué pasos corren) se edita por
ahora en el admin (`/admin/aperturas/plantillaapertura/`), no acá: se define una vez por
formato de farmacia y casi no cambia, mientras que la apertura en sí es la operación del
día a día. Si eso se vuelve molesto, es una pantalla más siguiendo este mismo archivo.
"""
from django.contrib import messages
from django.contrib.auth.decorators import login_required, permission_required
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from apps.aperturas.models import Apertura, PasoApertura, TokenApertura
from apps.aperturas.services import (
    aprobar_apertura,
    completar_paso_manual,
    ejecutar_paso,
    emitir_tokens,
    recalcular_estado_apertura,
    revocar_token,
    sincronizar_apertura,
)
from apps.auditoria.models import registrar_evento
from apps.cuentas.services import scope_por_unidad_negocio_activa, verificar_acceso

from ..forms import AperturaForm

_TENANT = 'farmacia__unidad_negocio'


def _apertura_visible(request, pk):
    apertura = get_object_or_404(
        Apertura.objects.select_related(
            'farmacia__unidad_negocio', 'farmacia__grupo', 'plantilla', 'creado_por', 'aprobado_por',
        ),
        pk=pk,
    )
    verificar_acceso(request.user, apertura.farmacia.unidad_negocio)
    return apertura


@login_required
@permission_required('aperturas.view_apertura', raise_exception=True)
def aperturas_lista(request):
    aperturas = scope_por_unidad_negocio_activa(
        Apertura.objects.select_related('farmacia', 'plantilla', 'creado_por', 'aprobado_por'),
        request, _TENANT,
    )
    return render(request, 'panel/aperturas_lista.html', {'aperturas': aperturas})


@login_required
@permission_required('aperturas.add_apertura', raise_exception=True)
def apertura_crear(request):
    if request.method == 'POST':
        form = AperturaForm(request.POST, user=request.user)
        if form.is_valid():
            try:
                apertura = form.crear(usuario=request.user)
            except ValueError as exc:
                # Las reglas de negocio (tenant cruzado, apertura vigente duplicada,
                # plantilla desactivada) viven en el servicio, no en el form: el form no
                # es el único que las tiene que respetar (el enrolamiento por MQTT también
                # pasa por ahí). Acá se traducen a un mensaje en vez de un 500.
                form.add_error(None, str(exc))
            else:
                messages.success(
                    request,
                    f'Apertura de {apertura.farmacia.codigo} creada, pendiente de aprobación.',
                )
                return redirect('panel:apertura_detalle', pk=apertura.pk)
    else:
        form = AperturaForm(user=request.user)
    return render(request, 'panel/apertura_form.html', {'form': form})


@login_required
@permission_required('aperturas.view_apertura', raise_exception=True)
def apertura_detalle(request, pk):
    apertura = _apertura_visible(request, pk)
    puede_aprobar = (
        apertura.estado == Apertura.Estado.PENDIENTE_APROBACION
        and apertura.creado_por_id != request.user.id
        and request.user.has_perm('aperturas.aprobar_apertura')
    )
    perfiles = apertura.plantilla.perfiles_estacion.all()
    tokens = apertura.tokens.select_related('perfil', 'estacion').all()
    sufijos_con_token = {t.perfil_id for t in tokens if t.vigente or t.usado_en is not None}
    return render(request, 'panel/apertura_detalle.html', {
        'apertura': apertura,
        'puede_aprobar': puede_aprobar,
        'puede_emitir': (
            apertura.estado in (Apertura.Estado.APROBADA, Apertura.Estado.EN_CURSO)
            and request.user.has_perm('aperturas.emitir_token_apertura')
            and any(p.pk not in sufijos_con_token for p in perfiles)
        ),
        'perfiles': perfiles,
        'tokens': tokens,
    })


@login_required
@permission_required('aperturas.view_apertura', raise_exception=True)
def apertura_pasos_partial(request, pk):
    """Fragmento que se repinta solo (polling HTMX, igual que el progreso de un despliegue).

    Sincroniza antes de renderizar: los pasos no se enteran solos de su resultado — el
    agente le responde por MQTT a `apps.mqtt_worker`, que actualiza
    `ResultadoEjecucionScript`/`ResultadoInstalacion` sin saber que pertenecen a una
    apertura. Es una escritura en un GET, sí, pero idempotente: solo adelanta el estado
    a lo que los módulos de origen ya reportaron, nunca inventa trabajo. El comando
    `sincronizar_aperturas` hace lo mismo por cron para que una apertura se cierre aunque
    nadie tenga esta pantalla abierta.
    """
    apertura = _apertura_visible(request, pk)
    if apertura.estado in (Apertura.Estado.APROBADA, Apertura.Estado.EN_CURSO):
        sincronizar_apertura(apertura)
        apertura.refresh_from_db()

    pasos = list(
        apertura.pasos
        .select_related('estacion', 'paso_plantilla')
        .prefetch_related('eventos')
        .order_by('estacion__codigo', 'orden')
    )
    total = len(pasos)
    completados = sum(1 for p in pasos if p.estado == PasoApertura.Estado.COMPLETADO)
    errores = sum(1 for p in pasos if p.estado == PasoApertura.Estado.ERROR)

    # Qué estaciones obligatorias todavía no aparecieron. Es el dato que responde la
    # pregunta real del operador ("¿por qué no cierra esta apertura?"), y sin esto hay
    # que deducirlo comparando la lista de perfiles contra la de pasos a ojo.
    enroladas = {p.estacion.codigo.partition('-')[2] for p in pasos if p.estacion_id}
    faltantes = [
        perfil for perfil in apertura.plantilla.perfiles_estacion.filter(obligatoria=True)
        if perfil.sufijo not in enroladas
    ]

    return render(request, 'panel/apertura_pasos_partial.html', {
        'apertura': apertura,
        'pasos': pasos,
        'total': total,
        'completados': completados,
        'errores': errores,
        'pct_completado': round(100 * completados / total) if total else 0,
        'faltantes': faltantes,
    })


@login_required
@permission_required('aperturas.aprobar_apertura', raise_exception=True)
@require_POST
def apertura_aprobar(request, pk):
    apertura = _apertura_visible(request, pk)
    try:
        # El evento de auditoría lo registra el propio servicio: aprobar una apertura tiene
        # que quedar asentado venga de donde venga (panel, shell, comando), no solo cuando
        # pasa por esta vista. No registrarlo también acá — se duplicaba la fila.
        aprobar_apertura(apertura=apertura, usuario=request.user)
    except ValueError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, f'Apertura de {apertura.farmacia.codigo} aprobada. Ya se pueden emitir tokens.')
    return redirect('panel:apertura_detalle', pk=pk)


@login_required
@permission_required('aperturas.emitir_token_apertura', raise_exception=True)
@require_POST
def apertura_emitir_tokens(request, pk):
    """Emite los tokens faltantes y los muestra UNA vez, en claro.

    Renderiza directo en vez de redirigir: el valor en claro solo existe en esta
    respuesta (de `TokenApertura` se guarda el hash, ver §10-Z), así que no hay a dónde
    redirigir que lo pueda volver a mostrar. Reenviar el POST no duplica nada —
    `emitir_tokens` saltea los perfiles que ya tienen un token vigente o usado.
    """
    apertura = _apertura_visible(request, pk)
    try:
        emitidos = emitir_tokens(apertura=apertura, usuario=request.user)
    except ValueError as exc:
        messages.error(request, str(exc))
        return redirect('panel:apertura_detalle', pk=pk)

    if not emitidos:
        messages.info(request, 'Todos los perfiles de esta apertura ya tienen su token.')
        return redirect('panel:apertura_detalle', pk=pk)

    return render(request, 'panel/apertura_tokens.html', {'apertura': apertura, 'emitidos': emitidos})


@login_required
@permission_required('aperturas.emitir_token_apertura', raise_exception=True)
@require_POST
def apertura_token_revocar(request, pk, token_pk):
    apertura = _apertura_visible(request, pk)
    token = get_object_or_404(TokenApertura, pk=token_pk, apertura=apertura)
    if token.usado_en is not None:
        messages.error(request, 'Ese token ya se usó: revocarlo no cambia nada. La estación ya está enrolada.')
    else:
        revocar_token(token=token, usuario=request.user, motivo='Revocado desde el panel')
        messages.success(request, f'Token {token.prefijo}… revocado. Podés emitir uno nuevo para ese perfil.')
    return redirect('panel:apertura_detalle', pk=pk)


@login_required
@permission_required('aperturas.change_apertura', raise_exception=True)
@require_POST
def apertura_paso_completar(request, pk, paso_pk):
    """Cierra a mano un paso manual (AD, 2FA, circuito del proveedor)."""
    apertura = _apertura_visible(request, pk)
    paso = get_object_or_404(PasoApertura, pk=paso_pk, apertura=apertura)
    detalle = request.POST.get('detalle', '').strip()
    try:
        completar_paso_manual(paso=paso, usuario=request.user, detalle=detalle)
    except ValueError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, f'"{paso.nombre}" marcado como completado.')
    return redirect('panel:apertura_detalle', pk=pk)


@login_required
@permission_required('aperturas.change_apertura', raise_exception=True)
@require_POST
def apertura_paso_reintentar(request, pk, paso_pk):
    """Reintenta un paso que quedó en error (broker caído, número de serie sin reportar).

    No reintenta solo a propósito: un paso que falla en una farmacia que está por abrir
    merece que alguien mire por qué antes de repetir la acción sobre el equipo.
    """
    apertura = _apertura_visible(request, pk)
    paso = get_object_or_404(PasoApertura, pk=paso_pk, apertura=apertura)
    if paso.estado != PasoApertura.Estado.ERROR:
        messages.error(request, 'Solo se reintentan pasos que quedaron en error.')
        return redirect('panel:apertura_detalle', pk=pk)
    ejecutar_paso(paso)
    recalcular_estado_apertura(apertura)
    registrar_evento(
        usuario=request.user, accion='apertura.reintentar_paso', objeto=apertura,
        detalle={'paso': paso.nombre, 'estacion': paso.estacion.codigo if paso.estacion_id else None},
        request=request,
    )
    messages.success(request, f'"{paso.nombre}" reintentado.')
    return redirect('panel:apertura_detalle', pk=pk)


@login_required
@permission_required('aperturas.change_apertura', raise_exception=True)
@require_POST
def apertura_cancelar(request, pk):
    apertura = _apertura_visible(request, pk)
    if apertura.estado in (Apertura.Estado.COMPLETADA, Apertura.Estado.CANCELADA):
        messages.error(request, 'Esta apertura ya está cerrada.')
        return redirect('panel:apertura_detalle', pk=pk)

    # Cancelar deja tokens sin usar dando vueltas: cada uno todavía sirve para enrolar una
    # estación ya aprobada. Se revocan todos, o la cancelación es solo cosmética.
    revocados = 0
    for token in apertura.tokens.filter(usado_en__isnull=True, revocado=False):
        revocar_token(token=token, usuario=request.user, motivo='Apertura cancelada')
        revocados += 1

    apertura.estado = Apertura.Estado.CANCELADA
    apertura.save(update_fields=['estado'])
    registrar_evento(
        usuario=request.user, accion='apertura.cancelar', objeto=apertura,
        detalle={'tokens_revocados': revocados}, request=request,
    )
    messages.success(
        request,
        f'Apertura cancelada. {revocados} token(s) sin usar fueron revocados.' if revocados
        else 'Apertura cancelada.',
    )
    return redirect('panel:apertura_detalle', pk=pk)
