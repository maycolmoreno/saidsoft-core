from datetime import datetime, timedelta

from django.contrib.auth.decorators import login_required
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, render
from django.utils import timezone

from apps.catalogo.models import Grupo
from apps.cuentas.services import scope_por_unidad_negocio, unidades_negocio_en_foco, verificar_acceso
from apps.despliegues.models import Despliegue


def _csv_response(nombre):
    resp = HttpResponse(content_type='text/csv; charset=utf-8')
    resp['Content-Disposition'] = f'attachment; filename="{nombre}"'
    resp.write('﻿')  # BOM para que Excel reconozca UTF-8 (tildes)
    return resp


@login_required
def reportes_index(request):
    despliegues = scope_por_unidad_negocio(
        Despliegue.objects.order_by('-fecha_creacion'), request.user, 'unidad_negocio',
    )[:100]
    # Un Grupo (canal TRX) puede estar compartido por farmacias de varias unidades de
    # negocio: solo se ofrecen los que tienen al menos una farmacia visible (mismo
    # criterio que el dashboard).
    grupos = Grupo.objects.filter(
        farmacias__unidad_negocio__in=unidades_negocio_en_foco(request),
    ).distinct().order_by('codigo')
    return render(request, 'panel/reportes_index.html', {
        'grupos': grupos,
        'despliegues': despliegues,
    })


@login_required
def reporte_cumplimiento_csv(request):
    from apps.panel import reportes
    resp = _csv_response(reportes.nombre_archivo('cumplimiento'))
    reportes.reporte_cumplimiento(resp, request, grupo_codigo=request.GET.get('grupo') or None)
    return resp


@login_required
def reporte_despliegue_csv(request, pk):
    from apps.panel import reportes
    despliegue = get_object_or_404(Despliegue, pk=pk)
    verificar_acceso(request.user, despliegue.unidad_negocio)
    resp = _csv_response(reportes.nombre_archivo(f'despliegue_{despliegue.version}'))
    reportes.reporte_despliegue(resp, despliegue)
    return resp


@login_required
def reporte_auditoria_csv(request):
    from apps.panel import reportes

    def _parse(nombre):
        valor = request.GET.get(nombre)
        if not valor:
            return None
        try:
            return timezone.make_aware(datetime.strptime(valor, '%Y-%m-%d'))
        except ValueError:
            return None

    desde = _parse('desde')
    hasta = _parse('hasta')
    if hasta:
        hasta = hasta + timedelta(days=1)  # incluir todo el día 'hasta'
    resp = _csv_response(reportes.nombre_archivo('auditoria'))
    reportes.reporte_auditoria(resp, request, desde=desde, hasta=hasta)
    return resp
