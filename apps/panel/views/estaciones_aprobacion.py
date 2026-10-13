"""Flujo de aprobación de estaciones: la bandeja de pendientes, aprobar, rechazar y el lote.

Salió de `estaciones.py` el 12-oct-2026 por dos motivos que empujaban en la misma dirección:

- **El módulo llegó al techo.** `DivisionDeVistasTests` pone un límite de 550 líneas por
  módulo de vistas y dice explícitamente que *"si un módulo lo pasa, la respuesta es dividirlo,
  no subir el límite"*. `estaciones.py` venía peleando con ese techo en cada cambio —§10-BL
  tuvo que compactar comentarios para entrar— y eso es la señal, no el obstáculo.
- **`D-5` de la auditoría por agentes** señala estas mismas vistas: transiciones de dominio
  escritas en la vista. Las transiciones viven ahora en `catalogo.services`
  (`aprobar_estacion`, `rechazar_estacion`) y lo que queda acá es lo que le toca a una vista:
  permisos, scoping por unidad de negocio, auditoría y qué se le dice al operador.

Las cuatro se mueven juntas porque son un solo flujo y se llaman entre sí: las tres acciones
devuelven la bandeja renderizada, que es lo que HTMX reemplaza en la pantalla.
"""
from django.contrib.auth.decorators import login_required, permission_required
from django.shortcuts import get_object_or_404, render
from django.views.decorators.http import require_POST

from apps.auditoria.models import registrar_evento
from apps.catalogo.models import Estacion
from apps.catalogo.services import aprobar_estacion, rechazar_estacion
from apps.cuentas.services import (
    scope_por_unidad_negocio, scope_por_unidad_negocio_activa, verificar_acceso,
)


@login_required
@permission_required('catalogo.view_estacion', raise_exception=True)
def estaciones_pendientes_partial(request, aviso=''):
    # `_activa` como su marco: sin eso, con un cliente fijado el aviso contaba las pendientes
    # de TODOS mientras la tabla mostraba uno (ver AvisoDePendientesSigueAlClienteActivoTests).
    pendientes = scope_por_unidad_negocio_activa(
        Estacion.objects.select_related('farmacia', 'farmacia__grupo').filter(
            estado_aprobacion=Estacion.EstadoAprobacion.PENDIENTE,
        ),
        request, 'farmacia__unidad_negocio',
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
    venia_rechazada = aprobar_estacion(estacion)  # transición en el servicio (`D-5`)
    registrar_evento(usuario=request.user, accion='estacion.aprobar', objeto=estacion,
                     request=request, detalle={'venia_rechazada': venia_rechazada})
    # Volver de un rechazo no es gratis: su credencial fue revocada (§10-BL, §10-BV).
    return estaciones_pendientes_partial(request, aviso='' if not venia_rechazada else (
        f'{estacion.codigo} quedó aprobada, pero venía RECHAZADA y su credencial MQTT fue '
        f'revocada: vuelve sola solo si puede conectarse; si no, borrarle identidad.json.'))


@login_required
@permission_required('catalogo.aprobar_estacion', raise_exception=True)
@require_POST
def estacion_rechazar(request, pk):
    estacion = get_object_or_404(Estacion, pk=pk)
    verificar_acceso(request.user, estacion.farmacia.unidad_negocio)
    revocada = rechazar_estacion(estacion)  # revoca su credencial del broker (`B-1`, §10-BL)
    registrar_evento(usuario=request.user, accion='estacion.rechazar', objeto=estacion, request=request, detalle={'credencial_mqtt_revocada': revocada})
    return estaciones_pendientes_partial(request, aviso='' if revocada is not False else (
        f'{estacion.codigo} quedó rechazada, pero su credencial MQTT no se pudo revocar: sigue '
        f'pudiendo conectarse al broker. Reintentá cuando EMQX responda.'))


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
        # El queryset filtra por PENDIENTE: acá nunca hay una RECHAZADA (`D-5`).
        aprobar_estacion(estacion)
        registrar_evento(usuario=request.user, accion='estacion.aprobar', objeto=estacion, request=request)
        aprobadas += 1
    aviso = ''
    if aprobadas < len(ids):
        # Puede pasar si otro operador ya las aprobó, o si el filtro por unidad de
        # negocio dejó afuera alguna de las que llegaron en el POST.
        aviso = f'Se aprobaron {aprobadas} de {len(ids)} estaciones seleccionadas.'
    return estaciones_pendientes_partial(request, aviso=aviso)
