from datetime import datetime, timedelta

from django.contrib.auth.decorators import login_required, permission_required
from django.db.models import Count
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

from apps.activos.models import Activo
from apps.catalogo.models import Grupo, UnidadNegocio
from apps.catalogo.services import calcular_matriz_cumplimiento
from apps.cuentas.services import (
    scope_por_unidad_negocio, unidad_negocio_activa, unidades_negocio_en_foco, unidades_negocio_visibles,
    usuario_tiene_acceso_total, verificar_acceso,
)
from apps.despliegues.models import Despliegue, ResultadoDespliegue
from apps.facturacion.services import resumen_facturacion
from apps.mantenimiento import services as mantenimiento_services
from apps.monitoreo.models import Alerta, ReglaAlerta


def _csv_response(nombre):
    resp = HttpResponse(content_type='text/csv; charset=utf-8')
    resp['Content-Disposition'] = f'attachment; filename="{nombre}"'
    resp.write('﻿')  # BOM para que Excel reconozca UTF-8 (tildes)
    return resp


def _resolver_unidad_negocio(request):
    """Unidad de negocio pedida por querystring (?unidad_negocio=<pk>, siempre
    verificada contra lo que el usuario puede ver), o si no se especificó, la activa
    en sesión, o si tampoco hay, la única visible (si el usuario solo tiene acceso a
    una). None si no hay ninguna resoluble sin ambigüedad."""
    visibles = unidades_negocio_visibles(request.user)
    unidad_id = request.GET.get('unidad_negocio')
    if unidad_id:
        return get_object_or_404(visibles, pk=unidad_id)
    activa = unidad_negocio_activa(request)
    if activa is not None:
        return activa
    if visibles.count() == 1:
        return visibles.first()
    return None


def _rango_fechas(request):
    """Rango de fechas de un reporte: ?desde=YYYY-MM-DD&hasta=YYYY-MM-DD, por defecto
    el mes en curso completo. Devuelve (desde, hasta_exclusivo)."""
    def _parse(nombre, default):
        valor = request.GET.get(nombre)
        if not valor:
            return default
        try:
            return timezone.make_aware(datetime.strptime(valor, '%Y-%m-%d'))
        except ValueError:
            return default

    ahora = timezone.now()
    desde = _parse('desde', ahora.replace(day=1, hour=0, minute=0, second=0, microsecond=0))
    hasta = _parse('hasta', ahora) + timedelta(days=1)  # incluir todo el día 'hasta'
    return desde, hasta


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
        'unidades_negocio': unidades_negocio_visibles(request.user),
    })


@login_required
@permission_required('cumplimiento.view_actividadcumplimiento', raise_exception=True)
def reporte_cumplimiento_csv(request):
    from apps.panel import reportes
    visibles = unidades_negocio_visibles(request.user)
    unidad_id = request.GET.get('unidad_negocio')
    if unidad_id:
        unidades = [get_object_or_404(visibles, pk=unidad_id)]
    elif usuario_tiene_acceso_total(request.user):
        unidades = None  # sin acotar: puede ver todo, y no pidió un cliente puntual
    else:
        unidades = visibles  # sin elegir cliente: se acota a todo lo que sí puede ver
    resp = _csv_response(reportes.nombre_archivo('cumplimiento'))
    reportes.reporte_cumplimiento(resp, unidades_negocio=unidades, grupo_codigo=request.GET.get('grupo') or None)
    return resp


@login_required
@permission_required('despliegues.view_despliegue', raise_exception=True)
def reporte_despliegue_csv(request, pk):
    from apps.panel import reportes
    despliegue = get_object_or_404(Despliegue, pk=pk)
    verificar_acceso(request.user, despliegue.unidad_negocio)
    resp = _csv_response(reportes.nombre_archivo(f'despliegue_{despliegue.version}'))
    reportes.reporte_despliegue(resp, despliegue)
    return resp


@login_required
@permission_required('auditoria.view_eventoauditoria', raise_exception=True)
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


@login_required
@permission_required('activos.view_activo', raise_exception=True)
def reporte_activos_csv(request):
    from apps.panel import reportes
    unidad = _resolver_unidad_negocio(request)
    if unidad is None:
        return redirect('panel:reportes_index')
    resp = _csv_response(reportes.nombre_archivo(f'activos_{unidad.codigo}'))
    reportes.reporte_activos(resp, unidad)
    return resp


@login_required
@permission_required('monitoreo.view_alerta', raise_exception=True)
def reporte_alertas_csv(request):
    from apps.panel import reportes
    unidad = _resolver_unidad_negocio(request)
    if unidad is None:
        return redirect('panel:reportes_index')
    desde, hasta = _rango_fechas(request)
    resp = _csv_response(reportes.nombre_archivo(f'alertas_{unidad.codigo}'))
    reportes.reporte_alertas(resp, unidad, desde=desde, hasta=hasta)
    return resp


def _resolver_periodo(request):
    """(anio, mes) pedido por querystring (?periodo=YYYY-MM), por defecto el mes en curso."""
    valor = request.GET.get('periodo')
    if valor:
        try:
            anio_str, mes_str = valor.split('-')
            return int(anio_str), int(mes_str)
        except ValueError:
            pass
    ahora = timezone.now()
    return ahora.year, ahora.month


@login_required
@permission_required('facturacion.view_actividadmensualestacion', raise_exception=True)
def reporte_facturacion_csv(request):
    from apps.panel import reportes
    unidad = _resolver_unidad_negocio(request)
    if unidad is None:
        return redirect('panel:reportes_index')
    anio, mes = _resolver_periodo(request)
    resp = _csv_response(reportes.nombre_archivo(f'facturacion_{unidad.codigo}_{anio}-{mes:02d}'))
    reportes.reporte_facturacion(resp, unidad, anio, mes)
    return resp


@login_required
@permission_required('software.view_softwareinstaladodetectado', raise_exception=True)
def reporte_software_instalado_csv(request):
    from apps.panel import reportes
    unidad = _resolver_unidad_negocio(request)
    if unidad is None:
        return redirect('panel:reportes_index')
    nombre_filtro = request.GET.get('q', '').strip()
    resp = _csv_response(reportes.nombre_archivo(f'software_instalado_{unidad.codigo}'))
    reportes.reporte_software_instalado(resp, unidad, nombre_filtro=nombre_filtro)
    return resp


@login_required
@permission_required('mantenimiento.view_mantenimiento', raise_exception=True)
def reporte_mantenimiento_csv(request):
    from apps.panel import reportes
    unidad = _resolver_unidad_negocio(request)
    if unidad is None:
        return redirect('panel:reportes_index')
    desde, hasta = _rango_fechas(request)
    resp = _csv_response(reportes.nombre_archivo(f'mantenimiento_{unidad.codigo}'))
    reportes.reporte_mantenimiento(resp, unidad, desde=desde, hasta=hasta)
    return resp


@login_required
def reporte_cliente_resumen(request):
    unidades_negocio = unidades_negocio_visibles(request.user)
    unidad = _resolver_unidad_negocio(request)
    if unidad is None:
        return render(request, 'panel/reporte_cliente.html', {
            'unidad': None, 'unidades_negocio': unidades_negocio,
        })
    verificar_acceso(request.user, unidad)  # defensa adicional por si ?unidad_negocio= se manipuló
    desde, hasta = _rango_fechas(request)

    grupos = calcular_matriz_cumplimiento(UnidadNegocio.objects.filter(pk=unidad.pk))
    total_estaciones = sum(g.total_estaciones for g in grupos)
    total_conformes = sum(g.conformes for g in grupos)
    total_online = sum(g.online for g in grupos)
    total_pendientes = sum(g.pendientes for g in grupos)
    pct_conforme = round(100 * total_conformes / total_estaciones) if total_estaciones else None

    despliegues_rango = Despliegue.objects.filter(unidad_negocio=unidad, fecha_publicacion__range=(desde, hasta))
    despliegues_completados = despliegues_rango.filter(estado=Despliegue.Estado.COMPLETADO).count()
    despliegues_con_error = despliegues_rango.filter(
        resultados__estado=ResultadoDespliegue.Estado.ERROR,
    ).distinct().count()

    alertas_periodo = Alerta.objects.filter(
        estacion__farmacia__unidad_negocio=unidad, abierta_en__range=(desde, hasta),
    )
    conteo_severidad = dict(alertas_periodo.values_list('regla__severidad').annotate(n=Count('id')))
    alertas_por_severidad = [
        {'label': label, 'total': conteo_severidad.get(valor, 0)} for valor, label in ReglaAlerta.Severidad.choices
    ]
    conteo_estado_activo = dict(Activo.objects.filter(unidad_negocio=unidad).values_list('estado').annotate(n=Count('id')))
    activos_por_estado = [
        {'label': label, 'total': conteo_estado_activo.get(valor, 0)} for valor, label in Activo.Estado.choices
    ]

    # La facturación es por mes calendario; se usa el mes de `desde` (por defecto, el
    # mes en curso) aunque el usuario haya pedido un rango de fechas más amplio.
    endpoints_facturables = resumen_facturacion(unidad, desde.year, desde.month)

    resumen_mantenimiento = mantenimiento_services.resumen_mantenimiento_periodo(unidad, desde, hasta)

    return render(request, 'panel/reporte_cliente.html', {
        'unidad': unidad,
        'unidades_negocio': unidades_negocio,
        'desde': desde,
        'hasta': hasta - timedelta(days=1),  # mostrar el último día incluido, no el límite exclusivo
        'grupos': grupos,
        'total_estaciones': total_estaciones,
        'total_online': total_online,
        'total_pendientes': total_pendientes,
        'pct_conforme': pct_conforme,
        'despliegues_total': despliegues_rango.count(),
        'despliegues_completados': despliegues_completados,
        'despliegues_con_error': despliegues_con_error,
        'alertas_abiertas_ahora': Alerta.objects.filter(
            estacion__farmacia__unidad_negocio=unidad, estado__in=[Alerta.Estado.ABIERTA, Alerta.Estado.RECONOCIDA],
        ).count(),
        'alertas_resueltas_periodo': alertas_periodo.filter(estado=Alerta.Estado.RESUELTA).count(),
        'alertas_por_severidad': alertas_por_severidad,
        'activos_por_estado': activos_por_estado,
        'total_activos': sum(item['total'] for item in activos_por_estado),
        'endpoints_facturables': endpoints_facturables,
        'periodo_facturacion': desde,
        'resumen_mantenimiento': resumen_mantenimiento,
    })
