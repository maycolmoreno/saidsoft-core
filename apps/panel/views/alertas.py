from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from apps.auditoria.models import registrar_evento
from apps.cuentas.services import (
    scope_opcional_por_unidad_negocio_activa, scope_por_unidad_negocio_activa, verificar_acceso,
)
from apps.monitoreo.forms import ReglaAlertaForm
from apps.monitoreo.models import Alerta, ReglaAlerta


@login_required
def alertas_lista(request):
    alertas = scope_por_unidad_negocio_activa(
        Alerta.objects.select_related('regla', 'estacion', 'estacion__farmacia'),
        request, 'estacion__farmacia__unidad_negocio',
    )
    solo_activas = request.GET.get('todas') != '1'
    if solo_activas:
        alertas = alertas.filter(estado__in=[Alerta.Estado.ABIERTA, Alerta.Estado.RECONOCIDA])
    return render(request, 'panel/alertas_lista.html', {'alertas': alertas, 'solo_activas': solo_activas})


@login_required
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
def reglas_alerta_lista(request):
    reglas = scope_opcional_por_unidad_negocio_activa(
        ReglaAlerta.objects.select_related('unidad_negocio'), request, 'unidad_negocio',
    ).order_by('nombre')
    return render(request, 'panel/reglas_alerta_lista.html', {'reglas': reglas})


@login_required
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
