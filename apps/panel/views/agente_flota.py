"""Rollout del agente a la flota desde el panel, por tandas.

Hasta el 17-oct-2026 había dos caminos: el botón de la ficha de cada estación (uno por clic,
y solo si está en línea) y el comando `actualizar_agente_flota` (todas de una, pero por SSH
al servidor). Desplegar la 0.35 a 79 estaciones mostró el hueco: con el botón son 79 clics,
y con el comando hay que tener acceso al servidor para algo que es operación diaria.

Esta pantalla usa el mismo criterio que el comando (`estaciones_para_actualizar_agente`) y
le agrega lo que el comando solo recomienda, porque acá no hay una salida de texto que
alguien lea antes de apretar:

- **Canario obligatorio.** Mientras ninguna estación de la flota reporta la versión nueva,
  la tanda es de `AGENTE_CANARIO_MAXIMO`. Un build malo deja a cada estación que lo recibe
  SIN agente, y recuperarla es ir al equipo.
- **Tandas acotadas** a `AGENTE_TANDA_MAXIMA`: cada estación baja ~19 MB por el enlace de su
  farmacia, y la primera tanda grande real (12-oct-2026) dejó una sin aplicar por congestión.

A diferencia del botón de la ficha, acepta estaciones apagadas: la orden va retenida y la
aplican al encender, igual que con el comando. Cada orden se audita por estación, como el
botón: acá hay una persona concreta decidiendo.
"""
from datetime import timedelta

from django.contrib import messages
from django.contrib.auth.decorators import login_required, permission_required
from django.shortcuts import redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from apps.auditoria.models import registrar_evento
from apps.catalogo.models import Estacion, VersionAgente
from apps.catalogo.services import (
    AGENTE_CANARIO_MAXIMO, AGENTE_TANDA_MAXIMA, enviar_actualizacion_agente,
    estaciones_para_actualizar_agente,
)
from apps.cuentas.services import scope_por_unidad_negocio, scope_por_unidad_negocio_activa
from apps.monitoreo.umbrales import ONLINE_UMBRAL_MINUTOS


def _tope_de_tanda(version):
    """El canario se decide sobre la flota ENTERA, no sobre lo que ve el usuario: si la
    versión ya corre bien en otra unidad de negocio, el build está probado."""
    _, ya_estaban = estaciones_para_actualizar_agente(version)
    return (AGENTE_CANARIO_MAXIMO, True) if ya_estaban == 0 else (AGENTE_TANDA_MAXIMA, False)


@login_required
@permission_required('catalogo.actualizar_agente_estacion', raise_exception=True)
def estaciones_agente(request):
    version = VersionAgente.objects.order_by('-fecha_creacion').first()
    contexto = {'version': version}
    if version is not None:
        visibles = scope_por_unidad_negocio_activa(Estacion.objects.all(), request, 'farmacia__unidad_negocio')
        desactualizadas, al_dia = estaciones_para_actualizar_agente(version, visibles)
        limite = timezone.now() - timedelta(minutes=ONLINE_UMBRAL_MINUTOS)
        filas = list(desactualizadas)
        for estacion in filas:
            estacion.en_linea = bool(estacion.ultimo_heartbeat and estacion.ultimo_heartbeat >= limite)
        # En línea primero: son las que confirman en minutos si el build sirve.
        filas.sort(key=lambda e: (not e.en_linea, e.codigo))
        tope, es_canario = _tope_de_tanda(version)
        contexto.update({
            'filas': filas,
            'al_dia': al_dia,
            'en_linea': sum(1 for e in filas if e.en_linea),
            'apagadas': sum(1 for e in filas if not e.en_linea),
            'tope': tope,
            'tope_tanda': AGENTE_TANDA_MAXIMA,
            'es_canario': es_canario,
        })
    return render(request, 'panel/estaciones_agente.html', contexto)


@login_required
@permission_required('catalogo.actualizar_agente_estacion', raise_exception=True)
@require_POST
def estaciones_agente_actualizar(request):
    version = VersionAgente.objects.order_by('-fecha_creacion').first()
    if version is None:
        messages.error(request, 'No hay ninguna versión de agente cargada (Admin → Versiones de agente).')
        return redirect('panel:estaciones_agente')

    # Solo dígitos: `pk__in` con un valor no numérico revienta con un 500 en vez de ignorarlo.
    ids = [i for i in request.POST.getlist('estacion_ids') if i.isdigit()]
    if not ids:
        messages.error(request, 'No seleccionaste ninguna estación.')
        return redirect('panel:estaciones_agente')

    tope, es_canario = _tope_de_tanda(version)
    if len(ids) > tope:
        # Se rechaza la tanda entera en vez de mandar las primeras `tope`: cuáles serían
        # "las primeras" depende del orden del formulario, y el operador no lo eligió.
        messages.error(request, (
            f'Seleccionaste {len(ids)} y el máximo es {tope}. '
            + ('Ninguna estación reporta todavía esta versión: la primera tanda es el canario.'
               if es_canario else 'Las tandas grandes congestionan los enlaces de las farmacias.')
        ))
        return redirect('panel:estaciones_agente')

    # El scope se aplica sobre el USUARIO, no sobre la unidad activa en sesión: lo que
    # importa acá es que no pueda mandarle una orden a una estación que no le corresponde.
    desactualizadas, _ = estaciones_para_actualizar_agente(
        version, scope_por_unidad_negocio(Estacion.objects.all(), request.user, 'farmacia__unidad_negocio'),
    )
    enviadas, fallaron = [], []
    for estacion in desactualizadas.filter(pk__in=ids):
        if enviar_actualizacion_agente(estacion, version):
            registrar_evento(
                usuario=request.user, accion='estacion.actualizar_agente', objeto=estacion,
                request=request, detalle={'version': version.version, 'por_tanda': True},
            )
            enviadas.append(estacion.codigo)
        else:
            fallaron.append(estacion.codigo)

    if enviadas:
        messages.success(request, (
            f'Orden de actualizar a {version.version} enviada a {len(enviadas)}: {", ".join(enviadas)}. '
            'Publicar no es aplicar: lo confirma la versión que cada estación reporta en su latido, '
            'en unos minutos las que están en línea y al encender las apagadas.'
        ))
    if fallaron:
        messages.error(request, (
            f'No se pudo enviar a {len(fallaron)} ({", ".join(fallaron)}): el broker MQTT no respondió.'
        ))
    omitidas = len(ids) - len(enviadas) - len(fallaron)
    if omitidas:
        messages.warning(request, (
            f'{omitidas} de las seleccionadas no se enviaron: ya reportan la versión o no te corresponden.'
        ))
    return redirect('panel:estaciones_agente')
