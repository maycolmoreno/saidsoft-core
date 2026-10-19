"""Monitoreo → NetFlow: quién consume el enlace de una farmacia (§10-CQ).

El cálculo es `apps.monitoreo.netflow.resumen_netflow`, el mismo que el comando
`reporte_netflow`: la pantalla y la consola no pueden dar números distintos. Acá van el
alcance por unidad de negocio, los filtros y la presentación.

Solo se ofrecen las farmacias que exportaron algo en la retención (14 días): en las demás
no hay nada que mostrar, y listar 700 farmacias vacías escondería las tres que sí tienen.
"""
from datetime import timedelta

from django.contrib.auth.decorators import login_required, permission_required
from django.shortcuts import render
from django.utils import timezone

from apps.catalogo.models import Farmacia
from apps.cuentas.services import scope_por_unidad_negocio_activa
from apps.monitoreo.netflow import DIAS_RETENCION, resumen_netflow

RANGOS_HORAS = (1, 6, 24, 168)


@login_required
@permission_required('monitoreo.view_estadoenlacefarmacia', raise_exception=True)
def netflow_farmacias(request):
    alcance = scope_por_unidad_negocio_activa(Farmacia.objects.all(), request, 'unidad_negocio')
    con_netflow = list(
        alcance.filter(ventanas_netflow__inicio__gte=timezone.now() - timedelta(days=DIAS_RETENCION))
        .distinct().order_by('codigo')
    )
    codigo = (request.GET.get('farmacia') or '').upper()
    farmacia = next((f for f in con_netflow if f.codigo == codigo), con_netflow[0] if con_netflow else None)
    try:
        horas = int(request.GET.get('horas', 24))
    except ValueError:
        horas = 24
    if horas not in RANGOS_HORAS:
        horas = 24

    resumen = resumen_netflow(farmacia, horas=horas, top=15) if farmacia else None
    return render(request, 'panel/netflow_farmacias.html', {
        'farmacias': con_netflow, 'farmacia': farmacia, 'horas': horas, 'rangos': RANGOS_HORAS,
        'resumen': resumen,
        'saturaciones': farmacia.saturaciones_enlace.all()[:10] if farmacia else [],
    })
