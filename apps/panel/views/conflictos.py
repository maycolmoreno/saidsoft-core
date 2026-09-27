"""Cierres de campo que no se pudieron aplicar, y su bandeja de revisión.

Módulo propio y no dentro de `views/mantenimiento.py`, que ya estaba en el límite de
tamaño que fija `DivisionDeVistasTests`: esto es un dominio distinto —triage de algo que
quedó en conflicto— y no una acción más del ciclo de vida de un mantenimiento.

Pantalla propia y NO dentro del Centro de Monitoreo: el Centro es deliberadamente de
solo lectura ("una acción destructiva a un clic de distancia en algo que se mira de
reojo es una mala idea"), así que ahí va el CONTADOR y acá las dos acciones, igual que
hace cada fila de ese tablero con su pantalla de detalle.
"""
from django.contrib import messages
from django.contrib.auth.decorators import login_required, permission_required
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from apps.auditoria.models import registrar_evento
from apps.cuentas.services import scope_opcional_por_unidad_negocio_activa, verificar_acceso
from apps.mantenimiento import services as mantenimiento_services
from apps.mantenimiento.forms import DescartarCierreEnConflictoForm
from apps.mantenimiento.models import CierreEnConflicto


@login_required
@permission_required('mantenimiento.view_cierreenconflicto', raise_exception=True)
def cierres_en_conflicto_lista(request):
    """Lo que un técnico cerró en campo y no se pudo aplicar, esperando una decisión.

    Por defecto muestra solo los SIN revisar: es una bandeja de trabajo, no un
    historial. Los revisados quedan accesibles con `?todos=1` porque el payload
    rechazado es la única constancia de que hubo un trabajo de campo que no se
    contabilizó.
    """
    conflictos = scope_opcional_por_unidad_negocio_activa(
        CierreEnConflicto.objects.select_related(
            'mantenimiento', 'mantenimiento__cliente', 'tecnico', 'revisado_por',
        ),
        request, 'mantenimiento__cliente__unidad_negocio',
    )
    ver_todos = request.GET.get('todos') == '1'
    if not ver_todos:
        conflictos = conflictos.filter(revisado=False)
    return render(request, 'panel/cierres_en_conflicto_lista.html', {
        'conflictos': conflictos,
        'ver_todos': ver_todos,
        'sin_revisar': CierreEnConflicto.objects.filter(revisado=False).count(),
        'horas_escalamiento': mantenimiento_services.HORAS_ESCALAMIENTO_CONFLICTO,
    })


def _resumen_conflicto(conflicto):
    payload = conflicto.payload_rechazado
    return {
        'resumen_tipo': 'mantenimiento',
        'resumen_titulo': f'Cierre en conflicto - Mantenimiento #{conflicto.mantenimiento_id}',
        'resumen_sub': f'{conflicto.tecnico} · cerró en campo el {conflicto.ocurrido_en:%d/%m/%Y %H:%M}',
        'resumen_campos': [
            ('Resultado que puso el técnico', payload.get('resultado_tecnico') or '—'),
            ('Tiempo real declarado', f'{payload.get("tiempo_real_minutos")} min'
             if payload.get('tiempo_real_minutos') else '—'),
            ('Estado del equipo', payload.get('estado_general') or '—'),
            ('Estado al llegar al servidor', conflicto.estado_al_llegar),
            ('Motivo del rechazo', conflicto.motivo),
        ],
    }


@login_required
@permission_required('mantenimiento.change_cierreenconflicto', raise_exception=True)
@require_POST
def cierre_en_conflicto_aplicar(request, pk):
    """Le da la razón al técnico: reabre el mantenimiento y lo cierra con su payload.

    `change_cierreenconflicto` y no `view_`: Mesa de Ayuda VE la bandeja (primera línea
    diagnostica) pero no interviene, mismo criterio que con las alertas.
    """
    conflicto = get_object_or_404(CierreEnConflicto, pk=pk)
    verificar_acceso(request.user, conflicto.mantenimiento.unidad_negocio)
    try:
        mantenimiento_services.aplicar_cierre_en_conflicto(conflicto=conflicto, usuario=request.user)
    except ValueError as exc:
        messages.error(request, str(exc))
    else:
        registrar_evento(
            usuario=request.user, accion='cierre_conflicto.aplicar', objeto=conflicto, request=request,
        )
        messages.success(
            request,
            f'Se aplicó el cierre de campo del mantenimiento #{conflicto.mantenimiento_id}.',
        )
    return redirect('panel:cierres_en_conflicto_lista')


@login_required
@permission_required('mantenimiento.change_cierreenconflicto', raise_exception=True)
def cierre_en_conflicto_descartar(request, pk):
    """Le da la razón al panel. El payload NO se borra: queda la constancia de que hubo
    un trabajo de campo que no se contabilizó."""
    conflicto = get_object_or_404(CierreEnConflicto, pk=pk)
    verificar_acceso(request.user, conflicto.mantenimiento.unidad_negocio)
    if request.method == 'POST':
        form = DescartarCierreEnConflictoForm(request.POST)
        if form.is_valid():
            try:
                mantenimiento_services.descartar_cierre_en_conflicto(
                    conflicto=conflicto, usuario=request.user, motivo=form.cleaned_data['motivo'],
                )
            except ValueError as exc:
                form.add_error(None, str(exc))
            else:
                registrar_evento(
                    usuario=request.user, accion='cierre_conflicto.descartar', objeto=conflicto,
                    request=request,
                )
                messages.success(request, 'Cierre de campo descartado, con el motivo registrado.')
                return redirect('panel:cierres_en_conflicto_lista')
    else:
        form = DescartarCierreEnConflictoForm()
    return render(request, 'panel/accion_form.html', {
        'form': form,
        'titulo': f'Descartar el cierre de campo del mantenimiento #{conflicto.mantenimiento_id}',
        'boton': 'Descartar cierre', 'tono': 'danger',
        **_resumen_conflicto(conflicto),
        'volver_url': reverse('panel:cierres_en_conflicto_lista'),
    })
