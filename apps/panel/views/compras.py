"""Ordenes de compra, recepcion de lotes y movimientos de inventario.

El camino por el que un activo ENTRA al inventario: desde que se compra hasta que
se recibe. Tiene sus propias reglas (recepcion parcial, anulacion de lote) que no
comparten nada con el ciclo de vida posterior del equipo.
"""
from django.contrib import messages
from django.contrib.auth.decorators import login_required, permission_required
from django.db.models import Q
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from apps.activos import services as activos_services
from apps.activos.forms import AnularRecepcionForm, OrdenCompraForm, OrdenCompraLineaForm, RecepcionLoteForm, RecibirOrdenCompraForm
from apps.activos.models import Bodega, MovimientoInventario, OrdenCompra, OrdenCompraDetalle, RecepcionLote
from apps.activos.services import ConcurrencyError
from apps.auditoria.models import registrar_evento
from apps.cuentas.services import scope_opcional_por_unidad_negocio, scope_opcional_por_unidad_negocio_activa, verificar_acceso


@login_required
@permission_required('activos.view_ordencompra', raise_exception=True)
def ordenes_compra_lista(request):
    ordenes = scope_opcional_por_unidad_negocio_activa(
        OrdenCompra.objects.prefetch_related('bodegas_destino'), request, 'unidad_negocio',
    ).order_by('-fecha_creacion')
    return render(request, 'panel/ordenes_compra_lista.html', {'ordenes': ordenes})


@login_required
@permission_required('activos.add_ordencompra', raise_exception=True)
def orden_compra_crear(request):
    if request.method == 'POST':
        form = OrdenCompraForm(request.POST, user=request.user)
        if form.is_valid():
            oc = form.save()
            registrar_evento(usuario=request.user, accion='orden_compra.crear', objeto=oc, request=request)
            messages.success(request, f'OC {oc.numero_oc} creada.')
            return redirect('panel:orden_compra_detalle', pk=oc.pk)
    else:
        form = OrdenCompraForm(user=request.user)
    return render(request, 'panel/accion_form.html', {
        'form': form, 'titulo': 'Nueva orden de compra', 'boton': 'Crear orden',
        'volver_url': reverse('panel:ordenes_compra_lista'),
    })


@login_required
@permission_required('activos.view_ordencompra', raise_exception=True)
def orden_compra_detalle(request, pk):
    oc = get_object_or_404(
        OrdenCompra.objects.prefetch_related(
            'bodegas_destino', 'activos',
            'detalles__categoria', 'detalles__marca', 'detalles__tipo_consumible',
            'detalles__recepciones',
        ),
        pk=pk,
    )
    verificar_acceso(request.user, oc.unidad_negocio)
    return render(request, 'panel/orden_compra_detalle.html', {'oc': oc})


@login_required
@permission_required('activos.add_ordencompradetalle', raise_exception=True)
def orden_compra_linea_crear(request, pk):
    oc = get_object_or_404(OrdenCompra, pk=pk)
    verificar_acceso(request.user, oc.unidad_negocio)
    if request.method == 'POST':
        form = OrdenCompraLineaForm(request.POST)
        if form.is_valid():
            d = form.cleaned_data
            detalle = activos_services.registrar_linea_orden_compra(
                orden_compra=oc, tipo_item=d['tipo_item'], cantidad_solicitada=d['cantidad_solicitada'],
                descripcion=d['descripcion'], modelo=d['modelo'], categoria=d['categoria'], marca=d['marca'],
                tipo_consumible=d['tipo_consumible'], precio_unitario=d['precio_unitario'],
                unidad_medida=d['unidad_medida'],
            )
            registrar_evento(usuario=request.user, accion='orden_compra.linea_crear', objeto=detalle, request=request)
            messages.success(request, f'Línea agregada a la OC {oc.numero_oc}.')
            return redirect('panel:orden_compra_detalle', pk=pk)
    else:
        form = OrdenCompraLineaForm()
    return render(request, 'panel/accion_form.html', {
        'form': form, 'titulo': f'Nueva línea para OC {oc.numero_oc}', 'boton': 'Agregar línea',
        'resumen_tipo': 'orden_compra', 'resumen_titulo': f'OC {oc.numero_oc}', 'resumen_sub': oc.proveedor,
        'resumen_campos': [
            ('Estado', oc.get_estado_display()), ('Fecha de emisión', oc.fecha_emision),
        ],
        'volver_url': reverse('panel:orden_compra_detalle', args=[pk]),
    })


@login_required
@permission_required('activos.add_recepcionlote', raise_exception=True)
def orden_compra_linea_recibir(request, pk):
    detalle = get_object_or_404(OrdenCompraDetalle.objects.select_related('orden_compra'), pk=pk)
    verificar_acceso(request.user, detalle.orden_compra.unidad_negocio)
    if detalle.estado == OrdenCompraDetalle.Estado.COMPLETO:
        messages.error(request, 'Esta línea ya fue recibida por completo.')
        return redirect('panel:orden_compra_detalle', pk=detalle.orden_compra_id)
    if request.method == 'POST':
        form = RecepcionLoteForm(request.POST, user=request.user)
        if form.is_valid():
            try:
                activos_services.registrar_recepcion_lote(
                    detalle=detalle, cantidad=form.cleaned_data['cantidad'], bodega=form.cleaned_data['bodega'],
                    custodio_receptor=form.cleaned_data['custodio_receptor'],
                    numero_lote=form.cleaned_data['numero_lote'], usuario=request.user,
                )
            except (ValueError, ConcurrencyError) as exc:
                form.add_error(None, str(exc))
            else:
                registrar_evento(
                    usuario=request.user, accion='orden_compra.linea_recibir', objeto=detalle.orden_compra,
                    request=request,
                )
                messages.success(request, 'Recepción registrada.')
                return redirect('panel:orden_compra_detalle', pk=detalle.orden_compra_id)
    else:
        form = RecepcionLoteForm(user=request.user, initial={
            'cantidad': detalle.cantidad_solicitada - detalle.cantidad_recibida,
        })
    return render(request, 'panel/accion_form.html', {
        'form': form, 'titulo': f'Recibir línea de OC {detalle.orden_compra.numero_oc}', 'boton': 'Registrar recepción',
        'resumen_tipo': 'orden_compra', 'resumen_titulo': str(detalle), 'resumen_sub': f'OC {detalle.orden_compra.numero_oc}',
        'resumen_campos': [
            ('Solicitado', detalle.cantidad_solicitada), ('Recibido hasta ahora', detalle.cantidad_recibida),
            ('Pendiente', detalle.cantidad_solicitada - detalle.cantidad_recibida),
        ],
        'volver_url': reverse('panel:orden_compra_detalle', args=[detalle.orden_compra_id]),
    })


@login_required
@permission_required('activos.change_recepcionlote', raise_exception=True)
def recepcion_lote_anular(request, pk):
    """BUG-3 de la auditoría de gobernanza (22-ago-2026): antes no había forma de
    revertir una recepción mal cargada salvo tocando la base a mano."""
    recepcion = get_object_or_404(
        RecepcionLote.objects.select_related('orden_compra', 'orden_compra_detalle'), pk=pk,
    )
    verificar_acceso(request.user, recepcion.orden_compra.unidad_negocio)
    if recepcion.estado == RecepcionLote.Estado.ANULADO:
        messages.error(request, 'Esta recepción ya está anulada.')
        return redirect('panel:orden_compra_detalle', pk=recepcion.orden_compra_id)
    if request.method == 'POST':
        form = AnularRecepcionForm(request.POST)
        if form.is_valid():
            try:
                activos_services.anular_recepcion_lote(
                    recepcion=recepcion, usuario=request.user, motivo=form.cleaned_data['motivo'],
                )
            except (ValueError, ConcurrencyError) as exc:
                form.add_error(None, str(exc))
            else:
                registrar_evento(
                    usuario=request.user, accion='recepcion_lote.anular', objeto=recepcion.orden_compra,
                    request=request,
                )
                messages.success(request, 'Recepción anulada.')
                return redirect('panel:orden_compra_detalle', pk=recepcion.orden_compra_id)
    else:
        form = AnularRecepcionForm()
    return render(request, 'panel/accion_form.html', {
        'form': form, 'titulo': f'Anular recepción de {recepcion.orden_compra.numero_oc}',
        'boton': 'Anular recepción', 'tono': 'danger',
        'resumen_tipo': 'orden_compra', 'resumen_titulo': str(recepcion),
        'resumen_sub': f'OC {recepcion.orden_compra.numero_oc}',
        'resumen_campos': [
            ('Lote', recepcion.numero_lote or '—'), ('Cantidad recibida', recepcion.cantidad_recibida),
            ('Bodega destino', recepcion.bodega_destino),
        ],
        'volver_url': reverse('panel:orden_compra_detalle', args=[recepcion.orden_compra_id]),
    })


@login_required
@permission_required('activos.view_movimientoinventario', raise_exception=True)
def movimientos_inventario_lista(request):
    movimientos = activos_services.scope_movimientos_visibles(
        MovimientoInventario.objects.select_related(
            'tipo_consumible', 'bodega_origen', 'bodega_destino', 'realizado_por', 'orden_compra',
        ),
        request.user,
    ).order_by('-fecha_efectiva')

    bodega = request.GET.get('bodega')
    tipo = request.GET.get('tipo')
    if bodega:
        movimientos = movimientos.filter(
            Q(bodega_origen__codigo=bodega) | Q(bodega_destino__codigo=bodega),
        )
    if tipo:
        movimientos = movimientos.filter(tipo_movimiento=tipo)

    bodegas_visibles = scope_opcional_por_unidad_negocio(Bodega.objects.all(), request.user, 'unidad_negocio')
    return render(request, 'panel/movimientos_inventario_lista.html', {
        'movimientos': movimientos[:500],
        'bodegas': bodegas_visibles.order_by('codigo'),
        'tipos': MovimientoInventario.TipoMovimiento.choices,
        'filtro_bodega': bodega or '', 'filtro_tipo': tipo or '',
    })


@login_required
@permission_required('activos.change_ordencompra', raise_exception=True)
def orden_compra_recibir(request, pk):
    oc = get_object_or_404(OrdenCompra, pk=pk)
    verificar_acceso(request.user, oc.unidad_negocio)
    if oc.estado == OrdenCompra.Estado.RECIBIDA:
        messages.error(request, 'Esta OC ya fue marcada como recibida.')
        return redirect('panel:orden_compra_detalle', pk=pk)
    if request.method == 'POST':
        form = RecibirOrdenCompraForm(request.POST)
        if form.is_valid():
            activos_services.recibir_orden_compra(
                orden_compra=oc, novedad_recepcion=form.cleaned_data['novedad_recepcion'], usuario=request.user,
            )
            registrar_evento(usuario=request.user, accion='orden_compra.recibir', objeto=oc, request=request)
            messages.success(request, f'OC {oc.numero_oc} marcada como recibida.')
            return redirect('panel:orden_compra_detalle', pk=pk)
    else:
        form = RecibirOrdenCompraForm()
    return render(request, 'panel/accion_form.html', {
        'form': form, 'titulo': f'Recibir OC {oc.numero_oc}', 'boton': 'Marcar como recibida',
        'resumen_tipo': 'orden_compra', 'resumen_titulo': f'OC {oc.numero_oc}', 'resumen_sub': oc.proveedor,
        'resumen_campos': [('Estado actual', oc.get_estado_display()), ('Fecha de emisión', oc.fecha_emision)],
        'volver_url': reverse('panel:orden_compra_detalle', args=[pk]),
    })
