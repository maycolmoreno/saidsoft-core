"""Bodegas: stock, ingresos y ajustes.

Una bodega maneja cantidades de consumibles, no equipos individuales con numero de
serie. Son dos modelos mentales distintos y por eso viven separados.
"""
from django.contrib import messages
from django.contrib.auth.decorators import login_required, permission_required
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from apps.activos import services as activos_services
from apps.activos.forms import AjusteStockForm, StockIngresoForm
from apps.activos.models import Bodega
from apps.auditoria.models import registrar_evento
from apps.cuentas.services import scope_opcional_por_unidad_negocio_activa, verificar_acceso


@login_required
@permission_required('activos.view_bodega', raise_exception=True)
def bodegas_lista(request):
    bodegas = scope_opcional_por_unidad_negocio_activa(
        Bodega.objects.prefetch_related('stock__tipo_consumible'), request, 'unidad_negocio',
    ).order_by('codigo')
    return render(request, 'panel/bodegas_lista.html', {'bodegas': bodegas})


@login_required
@permission_required('activos.change_stockbodega', raise_exception=True)
def bodega_stock_ingresar(request, pk):
    bodega = get_object_or_404(Bodega, pk=pk)
    verificar_acceso(request.user, bodega.unidad_negocio)
    if request.method == 'POST':
        form = StockIngresoForm(request.POST)
        if form.is_valid():
            activos_services.registrar_ingreso_stock(
                bodega=bodega, tipo_consumible=form.cleaned_data['tipo_consumible'],
                cantidad=form.cleaned_data['cantidad'],
            )
            registrar_evento(
                usuario=request.user, accion='stock.ingresar', objeto=bodega,
                detalle={'tipo_consumible': form.cleaned_data['tipo_consumible'].nombre,
                         'cantidad': form.cleaned_data['cantidad']},
                request=request,
            )
            messages.success(request, f'Stock actualizado en {bodega.codigo}.')
            return redirect('panel:bodegas_lista')
    else:
        form = StockIngresoForm()
    return render(request, 'panel/accion_form.html', {
        'form': form, 'titulo': f'Ingresar consumibles a {bodega.codigo}', 'boton': 'Ingresar stock',
        'resumen_tipo': 'bodega', 'resumen_titulo': bodega.codigo, 'resumen_sub': bodega.nombre or 'Sin nombre',
        'resumen_campos': [('Unidad de negocio', bodega.unidad_negocio or 'Compartida')],
        'volver_url': reverse('panel:bodegas_lista'),
    })


@login_required
@permission_required('activos.change_stockbodega', raise_exception=True)
def bodega_ajuste_stock(request, pk):
    """BUG-3 de la auditoría de gobernanza (22-ago-2026): antes no había forma de
    corregir el stock (conteo físico, merma) salvo tocando la base a mano — mismo
    permiso que el ingreso simple, ambos modifican StockBodega directamente."""
    bodega = get_object_or_404(Bodega, pk=pk)
    verificar_acceso(request.user, bodega.unidad_negocio)
    if request.method == 'POST':
        form = AjusteStockForm(request.POST)
        if form.is_valid():
            try:
                activos_services.registrar_ajuste_inventario(
                    bodega=bodega, tipo_consumible=form.cleaned_data['tipo_consumible'],
                    cantidad_delta=form.cleaned_data['cantidad_delta'], motivo=form.cleaned_data['motivo'],
                    usuario=request.user,
                )
            except ValueError as exc:
                form.add_error(None, str(exc))
            else:
                registrar_evento(
                    usuario=request.user, accion='stock.ajustar', objeto=bodega,
                    detalle={'tipo_consumible': form.cleaned_data['tipo_consumible'].nombre,
                             'cantidad_delta': form.cleaned_data['cantidad_delta'],
                             'motivo': form.cleaned_data['motivo']},
                    request=request,
                )
                messages.success(request, f'Ajuste registrado en {bodega.codigo}.')
                return redirect('panel:bodegas_lista')
    else:
        form = AjusteStockForm()
    return render(request, 'panel/accion_form.html', {
        'form': form, 'titulo': f'Ajustar stock de {bodega.codigo}', 'boton': 'Registrar ajuste',
        'subtitulo': 'Corrige el stock por conteo físico, merma o error de carga — queda registrado en el kardex.',
        'resumen_tipo': 'bodega', 'resumen_titulo': bodega.codigo, 'resumen_sub': bodega.nombre or 'Sin nombre',
        'resumen_campos': [('Unidad de negocio', bodega.unidad_negocio or 'Compartida')],
        'volver_url': reverse('panel:bodegas_lista'),
    })
