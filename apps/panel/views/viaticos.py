"""Panel de control de viáticos: alta del técnico y bandeja de aprobación del coordinador."""
import csv
from datetime import date
from decimal import Decimal

from django.contrib import messages
from django.contrib.auth.decorators import login_required, permission_required
from django.core.exceptions import PermissionDenied
from django.db.models import Count, Q
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from apps.auditoria.models import registrar_evento
from apps.catalogo.models import Farmacia
from apps.cuentas.services import (
    scope_opcional_por_unidad_negocio, scope_opcional_por_unidad_negocio_activa, scope_por_unidad_negocio,
    verificar_acceso,
)
from apps.viaticos import services as viaticos_services
from apps.viaticos.forms import ColaboradorZonaForm, ReporteViaticoForm
from apps.viaticos.models import (
    EstadoReporteViatico, ReporteViatico, RubroViatico, TipoAlertaViatico,
)


def _reportes_visibles(request):
    """Base de toda consulta de la bandeja. Un solo lugar donde se decide qué ve
    quién, para que la lista, el detalle, el consolidado y el CSV no puedan
    desincronizarse -- que es el bug clásico de "se filtró la lista pero no el CSV".

    `Colaborador.unidad_negocio` es opcional (None = compartido, ej. nómina central),
    así que aplica el criterio "compartido o del tenant".
    """
    return scope_opcional_por_unidad_negocio(
        ReporteViatico.objects.select_related(
            'colaborador', 'farmacia_visitada', 'revisado_por',
        ).prefetch_related('alertas'),
        request.user, 'colaborador__unidad_negocio',
    )


def _monto(valor):
    """Dinero con dos decimales SIEMPRE. `Sum()` sobre un DecimalField devuelve la
    escala que le dio el motor, y un consolidado contable que exporta "45" en vez de
    "45.00" obliga a reformatear la columna a mano en Excel."""
    return f'{Decimal(valor or 0):.2f}'


def _mes_pedido(request):
    """(anio, mes) de ?mes=YYYY-MM, o el mes en curso. Una fecha mal escrita cae al
    mes actual en vez de romper la pantalla."""
    crudo = request.GET.get('mes', '')
    hoy = date.today()
    if crudo:
        try:
            anio, mes = crudo.split('-')
            anio, mes = int(anio), int(mes)
            if 1 <= mes <= 12 and 2000 <= anio <= 2100:
                return anio, mes
        except (ValueError, TypeError):
            pass
    return hoy.year, hoy.month


# --- Alta y corrección por parte del técnico ---------------------------------


@login_required
@permission_required('viaticos.add_reporteviatico', raise_exception=True)
def viatico_crear(request):
    colaborador = viaticos_services.colaborador_de(request.user)
    if colaborador is None:
        # Sin Colaborador no se puede saber de quién es el gasto ni contra qué zona
        # validarlo. Se dice qué falta en vez de guardar algo sin dueño.
        return render(request, 'panel/viaticos_sin_colaborador.html', status=409)

    if request.method == 'POST':
        form = ReporteViaticoForm(request.POST, request.FILES, user=request.user)
        if form.is_valid():
            reporte = form.save(commit=False)
            reporte.colaborador = colaborador
            reporte.save()
            alertas = viaticos_services.evaluar_alertas(reporte)
            registrar_evento(usuario=request.user, accion='viatico.crear', objeto=reporte, request=request)
            if alertas:
                messages.warning(
                    request,
                    f'Reporte registrado con {len(alertas)} alerta(s). Tu coordinador va a tener que '
                    f'justificarlas para aprobarlo.',
                )
            else:
                messages.success(request, 'Reporte de viático registrado.')
            return redirect('panel:viaticos_mis_reportes')
    else:
        form = ReporteViaticoForm(user=request.user)

    return render(request, 'panel/viatico_form.html', {
        'form': form, 'colaborador': colaborador, 'titulo': 'Nuevo reporte de viático',
    })


@login_required
@permission_required('viaticos.view_reporteviatico', raise_exception=True)
def viaticos_mis_reportes(request):
    colaborador = viaticos_services.colaborador_de(request.user)
    if colaborador is None:
        return render(request, 'panel/viaticos_sin_colaborador.html', status=409)
    reportes = _reportes_visibles(request).filter(colaborador=colaborador)
    return render(request, 'panel/viaticos_mis_reportes.html', {
        'reportes': reportes, 'colaborador': colaborador,
    })


@login_required
@permission_required('viaticos.view_reporteviatico', raise_exception=True)
def viaticos_farmacias_partial(request):
    """Repuebla las <option> de `farmacia_visitada` a partir de una búsqueda.

    Sin término no devuelve nada: 700 farmacias en un <select> dejan la pantalla
    inusable, que fue el problema que ya se corrigió en el alta de mantenimiento.
    """
    termino = request.GET.get('buscar_farmacia', '').strip()
    farmacias = Farmacia.objects.none()
    if termino:
        farmacias = scope_por_unidad_negocio(
            Farmacia.objects.filter(activa=True), request.user, 'unidad_negocio',
        ).filter(
            Q(codigo__icontains=termino) | Q(nombre__icontains=termino) | Q(ubicacion__icontains=termino),
        ).order_by('codigo')[:100]
    return render(request, 'panel/_viaticos_farmacias_options.html', {
        'farmacias': farmacias, 'busco': bool(termino),
    })


# --- Bandeja del coordinador --------------------------------------------------


@login_required
@permission_required('viaticos.change_reporteviatico', raise_exception=True)
def viaticos_bandeja(request):
    """Todos los reportes del equipo, filtrables, con las alertas resaltadas."""
    reportes = _reportes_visibles(request).annotate(
        alertas_count=Count('alertas', filter=Q(alertas__resuelta=False), distinct=True),
    )

    colaborador_id = request.GET.get('colaborador', '')
    zona = request.GET.get('zona', '')
    estado = request.GET.get('estado', '')
    mes = request.GET.get('mes', '')
    solo_alertas = request.GET.get('solo_alertas') == '1'

    if colaborador_id:
        reportes = reportes.filter(colaborador_id=colaborador_id)
    if zona:
        reportes = reportes.filter(colaborador__zona_asignada__zona_cobertura=zona)
    if estado:
        reportes = reportes.filter(estado=estado)
    if mes:
        anio_m, mes_m = _mes_pedido(request)
        desde, hasta = viaticos_services.rango_del_mes(anio_m, mes_m)
        reportes = reportes.filter(fecha__gte=desde, fecha__lt=hasta)
    if solo_alertas:
        reportes = reportes.filter(alertas_count__gt=0)

    contexto = {
        'reportes': reportes[:300],
        'estados': EstadoReporteViatico.choices,
        'tipos_alerta': TipoAlertaViatico.choices,
        # Los filtros se ofrecen solo sobre gente/zonas que el usuario ya puede ver:
        # un desplegable que liste colaboradores de otro tenant filtra datos por sí solo.
        'colaboradores': _colaboradores_visibles(request),
        'zonas': _zonas_visibles(request),
        'filtros': {
            'colaborador': colaborador_id, 'zona': zona, 'estado': estado,
            'mes': mes, 'solo_alertas': solo_alertas,
        },
    }
    # Respuesta parcial para el refresco HTMX de la tabla, completa para la primera carga.
    plantilla = 'panel/_viaticos_bandeja_tabla.html' if request.headers.get('HX-Request') else \
        'panel/viaticos_bandeja.html'
    return render(request, plantilla, contexto)


def _colaboradores_visibles(request):
    from apps.activos.models import Colaborador
    return scope_opcional_por_unidad_negocio_activa(
        Colaborador.objects.filter(reportes_viatico__isnull=False).distinct().order_by('nombre'),
        request, 'unidad_negocio',
    )


def _zonas_visibles(request):
    from apps.viaticos.models import ColaboradorZona
    return scope_opcional_por_unidad_negocio(
        ColaboradorZona.objects.filter(activa=True), request.user, 'colaborador__unidad_negocio',
    ).values_list('zona_cobertura', flat=True).distinct().order_by('zona_cobertura')


@login_required
@permission_required('viaticos.view_reporteviatico', raise_exception=True)
def viatico_detalle(request, pk):
    reporte = get_object_or_404(
        ReporteViatico.objects.select_related('colaborador', 'farmacia_visitada', 'revisado_por'), pk=pk,
    )
    # El listado ya filtra, pero alguien puede forzar el pk por URL.
    verificar_acceso(request.user, reporte.colaborador.unidad_negocio)
    return render(request, 'panel/viatico_detalle.html', {
        'reporte': reporte, 'alertas': reporte.alertas.all(),
        'puede_revisar': request.user.has_perm('viaticos.change_reporteviatico'),
    })


@login_required
@permission_required('viaticos.change_reporteviatico', raise_exception=True)
@require_POST
def viatico_revisar(request, pk, accion):
    """Aprobar / observar / rechazar. La decisión de si el comentario es obligatorio
    la toma el servicio, no la vista."""
    reporte = get_object_or_404(ReporteViatico.objects.select_related('colaborador'), pk=pk)
    verificar_acceso(request.user, reporte.colaborador.unidad_negocio)
    comentario = request.POST.get('comentario', '')

    acciones = {
        'aprobar': viaticos_services.aprobar_reporte,
        'observar': viaticos_services.observar_reporte,
        'rechazar': viaticos_services.rechazar_reporte,
    }
    if accion not in acciones:
        raise PermissionDenied('Acción no reconocida.')

    try:
        acciones[accion](reporte=reporte, coordinador=request.user, comentario=comentario)
    except (viaticos_services.JustificacionRequerida, viaticos_services.TransicionInvalida) as exc:
        messages.error(request, str(exc))
    else:
        registrar_evento(usuario=request.user, accion=f'viatico.{accion}', objeto=reporte, request=request)
        messages.success(request, f'Reporte {reporte.get_estado_display().lower()}.')
    return redirect('panel:viatico_detalle', pk=reporte.pk)


# --- Consolidado mensual ------------------------------------------------------


@login_required
@permission_required('viaticos.view_reporteviatico', raise_exception=True)
def viaticos_consolidado(request):
    anio, mes = _mes_pedido(request)
    visibles = _reportes_visibles(request)
    return render(request, 'panel/viaticos_consolidado.html', {
        'filas': viaticos_services.consolidado_mensual(visibles, anio, mes),
        'tendencia': viaticos_services.tendencia_ultimos_meses(visibles, anio, mes, meses=3),
        'anio': anio, 'mes': mes, 'mes_iso': f'{anio}-{mes:02d}',
        'rubros': RubroViatico.choices,
    })


@login_required
@permission_required('viaticos.view_reporteviatico', raise_exception=True)
def viaticos_consolidado_csv(request):
    anio, mes = _mes_pedido(request)
    filas = viaticos_services.consolidado_mensual(_reportes_visibles(request), anio, mes)

    resp = HttpResponse(content_type='text/csv; charset=utf-8')
    resp['Content-Disposition'] = f'attachment; filename="viaticos_consolidado_{anio}-{mes:02d}.csv"'
    resp.write('﻿')  # BOM para que Excel reconozca UTF-8 (tildes)
    escritor = csv.writer(resp)
    escritor.writerow([
        'Colaborador', 'Cédula', 'Hospedaje', 'Alimentación', 'Movilización',
        'Total', 'Reportes', 'Alertas abiertas',
    ])
    for fila in filas:
        escritor.writerow([
            fila['colaborador__nombre'], fila['colaborador__cedula'],
            _monto(fila['hospedaje']), _monto(fila['alimentacion']), _monto(fila['movilizacion']),
            _monto(fila['total']), fila['reportes'], fila['alertas'],
        ])
    registrar_evento(usuario=request.user, accion='viatico.exportar_consolidado', request=request)
    return resp


# --- Zonas de cobertura -------------------------------------------------------


@login_required
@permission_required('viaticos.view_colaboradorzona', raise_exception=True)
def zonas_lista(request):
    from apps.viaticos.models import ColaboradorZona
    zonas = scope_opcional_por_unidad_negocio(
        ColaboradorZona.objects.select_related('colaborador').prefetch_related('farmacias_asignadas'),
        request.user, 'colaborador__unidad_negocio',
    )
    return render(request, 'panel/viaticos_zonas_lista.html', {'zonas': zonas})


@login_required
@permission_required('viaticos.add_colaboradorzona', raise_exception=True)
def zona_crear(request):
    if request.method == 'POST':
        form = ColaboradorZonaForm(request.POST, user=request.user)
        if form.is_valid():
            zona = form.save()
            registrar_evento(usuario=request.user, accion='viatico.zona_crear', objeto=zona, request=request)
            messages.success(request, f'Zona "{zona.zona_cobertura}" asignada a {zona.colaborador.nombre}.')
            return redirect('panel:viaticos_zonas_lista')
    else:
        form = ColaboradorZonaForm(user=request.user)
    return render(request, 'panel/accion_form.html', {
        'form': form, 'titulo': 'Asignar zona de cobertura', 'boton': 'Guardar zona',
        'subtitulo': 'Las farmacias asignadas son contra lo que se valida "fuera de zona".',
        'volver_url': reverse('panel:viaticos_zonas_lista'),
    })
