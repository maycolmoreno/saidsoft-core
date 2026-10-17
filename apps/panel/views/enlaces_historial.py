"""Historial de enlaces: caídas de un período, disponibilidad por farmacia y por proveedor,
y su exportación. El cálculo vive en `apps.monitoreo.historial_enlaces`; acá van los
filtros, el alcance por unidad de negocio y la presentación. Ver PLAN_MODERNIZACION.md
§10-CK.
"""
import csv
from datetime import datetime, time, timedelta

from django.contrib.auth.decorators import login_required, permission_required
from django.db.models import Q
from django.http import HttpResponse
from django.shortcuts import render
from django.utils import timezone

from apps.catalogo.models import Farmacia, Grupo
from apps.cuentas.services import scope_por_unidad_negocio_activa
from apps.monitoreo.historial_enlaces import ETIQUETA_CLASE, historial_enlaces
from apps.panel.paginacion import paginar

DIAS_POR_DEFECTO = 30


def _fecha(texto, por_defecto):
    try:
        return datetime.strptime(texto, '%Y-%m-%d').date()
    except (TypeError, ValueError):
        return por_defecto


def _filtros(request):
    hoy = timezone.localdate()
    desde = _fecha(request.GET.get('desde'), hoy - timedelta(days=DIAS_POR_DEFECTO))
    hasta = _fecha(request.GET.get('hasta'), hoy)
    if hasta < desde:
        desde, hasta = hasta, desde
    try:
        minimo = max(int(request.GET.get('minimo') or 0), 0)
    except ValueError:
        minimo = 0
    return {
        'desde': desde, 'hasta': hasta, 'minimo': minimo,
        'q': request.GET.get('q', '').strip(),
        'grupo': request.GET.get('grupo', ''),
        'proveedor': request.GET.get('proveedor', ''),
        'vista': 'eventos' if request.GET.get('vista') == 'eventos' else 'farmacias',
    }


def _calcular(request, filtros):
    farmacias = scope_por_unidad_negocio_activa(
        Farmacia.objects.exclude(ip_router__isnull=True), request, 'unidad_negocio',
    )
    if filtros['q']:
        farmacias = farmacias.filter(Q(codigo__icontains=filtros['q']) | Q(nombre__icontains=filtros['q']))
    if filtros['grupo']:
        farmacias = farmacias.filter(grupo__codigo=filtros['grupo'])
    zona = timezone.get_current_timezone()
    desde = timezone.make_aware(datetime.combine(filtros['desde'], time.min), zona)
    hasta = timezone.make_aware(datetime.combine(filtros['hasta'] + timedelta(days=1), time.min), zona)
    return farmacias, historial_enlaces(
        farmacias, desde, hasta, proveedor=filtros['proveedor'], minimo_minutos=filtros['minimo'],
    )


@login_required
@permission_required('monitoreo.view_estadoenlacefarmacia', raise_exception=True)
def enlaces_historial(request):
    filtros = _filtros(request)
    if request.GET.get('formato') == 'csv':
        return _exportar(request, filtros)
    farmacias, historial = _calcular(request, filtros)
    filas = historial.caidas if filtros['vista'] == 'eventos' else historial.por_farmacia
    pagina, query_filtros = paginar(filas, request)
    proveedores = sorted(set(farmacias.exclude(tipo_enlace='').values_list('tipo_enlace', flat=True)))
    return render(request, 'panel/enlaces_historial.html', {
        'filtros': filtros, 'historial': historial, 'pagina': pagina, 'query_filtros': query_filtros,
        'etiquetas': ETIQUETA_CLASE, 'proveedores': proveedores,
        'grupos': Grupo.objects.filter(farmacias__in=farmacias).distinct().order_by('codigo'),
        'total_caidas': len(historial.caidas),
    })


def _exportar(request, filtros):
    _farmacias, historial = _calcular(request, filtros)
    nombre = f'enlaces_{filtros["vista"]}_{filtros["desde"]:%Y%m%d}_{filtros["hasta"]:%Y%m%d}.csv'
    resp = HttpResponse(content_type='text/csv; charset=utf-8')
    resp['Content-Disposition'] = f'attachment; filename="{nombre}"'
    resp.write('﻿')  # BOM: Excel lee las tildes
    escritor = csv.writer(resp)
    if filtros['vista'] == 'eventos':
        escritor.writerow(['farmacia', 'cliente', 'nodo', 'proveedor', 'circuito', 'inicio', 'fin',
                           'minutos_en_el_periodo', 'tipo', 'cuenta_para_sla'])
        for c in historial.caidas:
            e = c.evento
            escritor.writerow([
                e.farmacia.codigo, e.farmacia.unidad_negocio.codigo, e.farmacia.grupo.codigo, c.proveedor,
                e.circuito_proveedor, timezone.localtime(e.inicio).strftime('%Y-%m-%d %H:%M'),
                timezone.localtime(e.fin).strftime('%Y-%m-%d %H:%M') if e.fin else 'en curso',
                round(c.minutos), ETIQUETA_CLASE[c.clase], 'sí' if c.cuenta_para_sla else 'no',
            ])
    else:
        escritor.writerow(['farmacia', 'nombre', 'cliente', 'nodo', 'proveedor', 'caidas', 'minutos_caido',
                           'caidas_sla', 'minutos_sla', 'disponibilidad_sla_pct'])
        for r in historial.por_farmacia:
            f = r.farmacia
            escritor.writerow([
                f.codigo, f.nombre, f.unidad_negocio.codigo, f.grupo.codigo, r.proveedor, r.caidas,
                round(r.minutos), r.caidas_sla, round(r.minutos_sla), r.disponibilidad_sla,
            ])
    return resp
