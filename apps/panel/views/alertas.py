from django.contrib import messages
from django.contrib.auth.decorators import login_required, permission_required
from django.db.models import Count, Max, Sum
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from apps.auditoria.models import registrar_evento
from apps.cuentas.services import (
    scope_opcional_por_unidad_negocio_activa, scope_por_unidad_negocio_activa, verificar_acceso,
)
from apps.monitoreo.forms import ReglaAlertaForm
from apps.monitoreo.models import Alerta, PosErrorDetectado, ReglaAlerta


@login_required
@permission_required('monitoreo.view_alerta', raise_exception=True)
def alertas_lista(request):
    alertas = scope_por_unidad_negocio_activa(
        Alerta.objects.select_related('regla', 'estacion', 'estacion__farmacia'),
        request, 'estacion__farmacia__unidad_negocio',
    )
    regla_id = request.GET.get('regla')
    if regla_id:
        alertas = alertas.filter(regla_id=regla_id)
    solo_activas = request.GET.get('todas') != '1'
    if solo_activas:
        alertas = alertas.filter(estado__in=[Alerta.Estado.ABIERTA, Alerta.Estado.RECONOCIDA])

    # Vista agrupada: "cuántas estaciones tienen esta regla activa ahora mismo" — sin
    # esto, un bug sistémico (ej. un error de esquema del POS en 40 farmacias) generaba
    # 40 filas idénticas en vez de una, entrenando al operador a ignorar la lista a
    # escala. Siempre sobre alertas ACTIVAS (no depende de `todas`/`regla`): agrupar
    # alertas ya resueltas no tiene el mismo sentido de "esto está pasando ahora".
    vista_agrupada = request.GET.get('vista') == 'agrupada'
    agrupadas = None
    if vista_agrupada:
        activas = scope_por_unidad_negocio_activa(
            Alerta.objects.filter(estado__in=[Alerta.Estado.ABIERTA, Alerta.Estado.RECONOCIDA]),
            request, 'estacion__farmacia__unidad_negocio',
        )
        agrupadas = list(
            activas.values('regla_id', 'regla__nombre', 'regla__severidad', 'regla__metrica')
            .annotate(n_estaciones=Count('estacion', distinct=True))
            .order_by('-n_estaciones')
        )
        for fila in agrupadas:
            fila['severidad_display'] = ReglaAlerta.Severidad(fila['regla__severidad']).label
            fila['es_pos_errores'] = fila['regla__metrica'] == 'pos_errores'

    return render(request, 'panel/alertas_lista.html', {
        'alertas': alertas, 'solo_activas': solo_activas,
        'vista_agrupada': vista_agrupada, 'agrupadas': agrupadas,
    })


def _top_mensajes_pos_errores(request, *, q=''):
    """Rollup de PosErrorDetectado por mensaje exacto, escopeado por tenant — factorizado
    de pos_errores_flota para que apps.panel.views.monitoreo.tendencia_flota (M5) pueda
    mostrar un top acotado sin duplicar la query. Devuelve (filas, queryset_detectados)."""
    detectados = PosErrorDetectado.objects.filter(categoria=PosErrorDetectado.Categoria.SISTEMA)
    detectados = scope_por_unidad_negocio_activa(detectados, request, 'estacion__farmacia__unidad_negocio')
    if q:
        detectados = detectados.filter(mensaje__icontains=q)

    filas = list(
        detectados.values('mensaje')
        .annotate(
            n_estaciones=Count('estacion', distinct=True),
            total=Sum('cantidad_total'),
            ultima_vez=Max('ultima_vez'),
        )
        .order_by('-n_estaciones')
    )
    maximo = filas[0]['n_estaciones'] if filas else 0
    for fila in filas:
        fila['pct_barra'] = round(100 * fila['n_estaciones'] / maximo) if maximo else 0
    return filas, detectados


@login_required
@permission_required('monitoreo.view_poserrordetectado', raise_exception=True)
def pos_errores_flota(request):
    """Rollup de errores del POS por mensaje exacto — a diferencia de agrupar por
    regla (arriba), esto distingue *qué* error puntual está afectando cuántas
    estaciones: la regla "Errores del POS" sola no alcanza para eso (una farmacia
    puede tener un timeout de conexión y otra el bug de fidelización, ambos bajo la
    misma regla). Solo categoría "sistema" — los de "negocio" (ej. venta sin lote) no
    son una falla real, no tiene sentido rankearlos acá."""
    q = request.GET.get('q', '').strip()
    filas, detectados = _top_mensajes_pos_errores(request, q=q)

    return render(request, 'panel/pos_errores_flota.html', {
        'filas': filas, 'q': q,
        'total_mensajes': len(filas),
        'total_estaciones': detectados.values('estacion_id').distinct().count(),
        'total_errores': sum(f['total'] for f in filas),
    })


@login_required
@permission_required('monitoreo.change_alerta', raise_exception=True)
@require_POST
def alerta_reconocer(request, pk):
    alerta = get_object_or_404(Alerta.objects.select_related('estacion__farmacia'), pk=pk)
    verificar_acceso(request.user, alerta.estacion.farmacia.unidad_negocio)
    if alerta.estado == Alerta.Estado.ABIERTA:
        alerta.estado = Alerta.Estado.RECONOCIDA
        alerta.reconocida_en = timezone.now()
        alerta.reconocida_por = request.user
        alerta.save(update_fields=['estado', 'reconocida_en', 'reconocida_por'])
        registrar_evento(usuario=request.user, accion='alerta.reconocer', objeto=alerta, request=request)
        messages.success(request, 'Alerta reconocida.')
    return redirect('panel:alertas_lista')


@login_required
@permission_required('monitoreo.change_alerta', raise_exception=True)
@require_POST
def alerta_resolver(request, pk):
    alerta = get_object_or_404(Alerta.objects.select_related('estacion__farmacia'), pk=pk)
    verificar_acceso(request.user, alerta.estacion.farmacia.unidad_negocio)
    if alerta.estado != Alerta.Estado.RESUELTA:
        alerta.estado = Alerta.Estado.RESUELTA
        alerta.resuelta_en = timezone.now()
        alerta.save(update_fields=['estado', 'resuelta_en'])
        registrar_evento(usuario=request.user, accion='alerta.resolver', objeto=alerta, request=request)
        messages.success(request, 'Alerta resuelta manualmente.')
    return redirect('panel:alertas_lista')


@login_required
@permission_required('monitoreo.view_reglaalerta', raise_exception=True)
def reglas_alerta_lista(request):
    reglas = scope_opcional_por_unidad_negocio_activa(
        ReglaAlerta.objects.select_related('unidad_negocio'), request, 'unidad_negocio',
    ).order_by('nombre')
    return render(request, 'panel/reglas_alerta_lista.html', {'reglas': reglas})


@login_required
@permission_required('monitoreo.add_reglaalerta', raise_exception=True)
def regla_alerta_crear(request):
    if request.method == 'POST':
        form = ReglaAlertaForm(request.POST, user=request.user)
        if form.is_valid():
            regla = form.save(commit=False)
            regla.creado_por = request.user
            regla.save()
            registrar_evento(usuario=request.user, accion='regla_alerta.crear', objeto=regla, request=request)
            messages.success(request, f'Regla "{regla.nombre}" creada.')
            return redirect('panel:reglas_alerta_lista')
    else:
        form = ReglaAlertaForm(user=request.user)
    return render(request, 'panel/accion_form.html', {
        'form': form, 'titulo': 'Nueva regla de alerta', 'volver_url': reverse('panel:reglas_alerta_lista'),
    })
