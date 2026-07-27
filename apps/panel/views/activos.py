from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse

from apps.activos import services as activos_services
from apps.activos.forms import (
    ActivoIngresoForm, AsignacionForm, BajaForm, ColaboradorForm, ConsumibleEntregadoForm,
    DevolucionForm, EnvioReparacionForm, OrdenCompraForm, RecibirOrdenCompraForm,
    RetornoReparacionForm, StockIngresoForm,
)
from apps.activos.models import Activo, Bodega, Colaborador, OrdenCompra
from apps.auditoria.models import registrar_evento


@login_required
def colaboradores_lista(request):
    colaboradores = Colaborador.objects.order_by('nombre')
    return render(request, 'panel/colaboradores_lista.html', {'colaboradores': colaboradores})


@login_required
def colaborador_crear(request):
    if request.method == 'POST':
        form = ColaboradorForm(request.POST)
        if form.is_valid():
            colaborador = form.save()
            registrar_evento(usuario=request.user, accion='colaborador.crear', objeto=colaborador, request=request)
            messages.success(request, f'Colaborador {colaborador.nombre} registrado.')
            return redirect('panel:colaboradores_lista')
    else:
        form = ColaboradorForm()
    return render(request, 'panel/accion_form.html', {
        'form': form, 'titulo': 'Nuevo colaborador',
        'subtitulo': 'Carga manual mientras se integra con RRHH/nómina.',
        'volver_url': reverse('panel:colaboradores_lista'),
    })


@login_required
def ordenes_compra_lista(request):
    ordenes = OrdenCompra.objects.prefetch_related('bodegas_destino').order_by('-fecha_creacion')
    return render(request, 'panel/ordenes_compra_lista.html', {'ordenes': ordenes})


@login_required
def orden_compra_crear(request):
    if request.method == 'POST':
        form = OrdenCompraForm(request.POST)
        if form.is_valid():
            oc = form.save()
            registrar_evento(usuario=request.user, accion='orden_compra.crear', objeto=oc, request=request)
            messages.success(request, f'OC {oc.numero_oc} creada.')
            return redirect('panel:orden_compra_detalle', pk=oc.pk)
    else:
        form = OrdenCompraForm()
    return render(request, 'panel/accion_form.html', {
        'form': form, 'titulo': 'Nueva orden de compra',
        'volver_url': reverse('panel:ordenes_compra_lista'),
    })


@login_required
def orden_compra_detalle(request, pk):
    oc = get_object_or_404(OrdenCompra.objects.prefetch_related('bodegas_destino', 'activos'), pk=pk)
    return render(request, 'panel/orden_compra_detalle.html', {'oc': oc})


@login_required
def orden_compra_recibir(request, pk):
    oc = get_object_or_404(OrdenCompra, pk=pk)
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
        'form': form, 'titulo': f'Recibir OC {oc.numero_oc}',
        'volver_url': reverse('panel:orden_compra_detalle', args=[pk]),
    })


@login_required
def activos_lista(request):
    activos = Activo.objects.select_related(
        'bodega_actual', 'colaborador_actual', 'marca', 'categoria',
    ).order_by('codigo')

    tipo = request.GET.get('tipo')
    estado = request.GET.get('estado')
    bodega = request.GET.get('bodega')
    if tipo:
        activos = activos.filter(tipo=tipo)
    if estado:
        activos = activos.filter(estado=estado)
    if bodega:
        activos = activos.filter(bodega_actual__codigo=bodega)

    return render(request, 'panel/activos_lista.html', {
        'activos': activos,
        'tipos': Activo.Tipo.choices,
        'estados': Activo.Estado.choices,
        'bodegas': Bodega.objects.order_by('codigo'),
        'filtro_tipo': tipo or '', 'filtro_estado': estado or '', 'filtro_bodega': bodega or '',
    })


@login_required
def activo_crear(request):
    if request.method == 'POST':
        form = ActivoIngresoForm(request.POST)
        if form.is_valid():
            d = form.cleaned_data
            activo = activos_services.registrar_ingreso(
                tipo=d['tipo'], marca=d['marca'], categoria=d['categoria'],
                modelo=d['modelo'], numero_serie=d['numero_serie'],
                procesador=d['procesador'], ram_gb=d['ram_gb'], almacenamiento_gb=d['almacenamiento_gb'],
                codigo_sap=d['codigo_sap'], condicion_al_recibir=d['condicion_al_recibir'],
                fecha_compra=d['fecha_compra'], vencimiento_garantia=d['vencimiento_garantia'],
                orden_compra=d['orden_compra'], bodega=d['bodega'], usuario=request.user,
            )
            registrar_evento(usuario=request.user, accion='activo.ingreso', objeto=activo, request=request)
            messages.success(request, f'Activo {activo.codigo} registrado en {activo.bodega_actual.codigo}.')
            return redirect('panel:activo_detalle', pk=activo.pk)
    else:
        initial = {}
        oc_id = request.GET.get('oc')
        if oc_id:
            initial['orden_compra'] = oc_id
        form = ActivoIngresoForm(initial=initial)
    return render(request, 'panel/accion_form.html', {
        'form': form, 'titulo': 'Registrar ingreso de activo',
        'subtitulo': 'Genera el código CR-TIPO-NNNN automáticamente y lo deja "En bodega".',
        'volver_url': reverse('panel:activos_lista'),
    })


@login_required
def activo_detalle(request, pk):
    activo = get_object_or_404(
        Activo.objects.select_related(
            'bodega_actual', 'colaborador_actual', 'orden_compra', 'marca', 'categoria',
        ), pk=pk,
    )
    eventos = activo.eventos.select_related('usuario').order_by('-timestamp')
    return render(request, 'panel/activo_detalle.html', {'activo': activo, 'eventos': eventos})


@login_required
def activo_asignar(request, pk):
    activo = get_object_or_404(Activo, pk=pk)
    if activo.estado != Activo.Estado.EN_BODEGA:
        messages.error(request, 'Solo se pueden asignar activos que están en bodega.')
        return redirect('panel:activo_detalle', pk=pk)
    if request.method == 'POST':
        form = AsignacionForm(request.POST)
        if form.is_valid():
            activos_services.registrar_asignacion(
                activo=activo, colaborador=form.cleaned_data['colaborador'],
                estado_fisico_entrega=form.cleaned_data['estado_fisico_entrega'], usuario=request.user,
            )
            registrar_evento(usuario=request.user, accion='activo.asignar', objeto=activo, request=request)
            messages.success(request, f'{activo.codigo} asignado.')
            return redirect('panel:activo_detalle', pk=pk)
    else:
        form = AsignacionForm()
    return render(request, 'panel/accion_form.html', {
        'form': form, 'titulo': f'Asignar {activo.codigo}',
        'volver_url': reverse('panel:activo_detalle', args=[pk]),
    })


@login_required
def activo_devolver(request, pk):
    activo = get_object_or_404(Activo, pk=pk)
    if activo.estado != Activo.Estado.ASIGNADO:
        messages.error(request, 'El activo no está asignado actualmente.')
        return redirect('panel:activo_detalle', pk=pk)
    if request.method == 'POST':
        form = DevolucionForm(request.POST)
        if form.is_valid():
            activos_services.registrar_devolucion(
                activo=activo, estado_fisico_devolucion=form.cleaned_data['estado_fisico_devolucion'],
                requiere_reparacion=form.cleaned_data['requiere_reparacion'], usuario=request.user,
            )
            registrar_evento(usuario=request.user, accion='activo.devolver', objeto=activo, request=request)
            messages.success(request, f'{activo.codigo} devuelto.')
            return redirect('panel:activo_detalle', pk=pk)
    else:
        form = DevolucionForm()
    return render(request, 'panel/accion_form.html', {
        'form': form, 'titulo': f'Registrar devolución de {activo.codigo}',
        'subtitulo': f'Colaborador actual: {activo.colaborador_actual}',
        'volver_url': reverse('panel:activo_detalle', args=[pk]),
    })


@login_required
def activo_reparacion_enviar(request, pk):
    activo = get_object_or_404(Activo, pk=pk)
    if activo.estado == Activo.Estado.DADO_DE_BAJA:
        messages.error(request, 'Un activo dado de baja no puede enviarse a reparación.')
        return redirect('panel:activo_detalle', pk=pk)
    if request.method == 'POST':
        form = EnvioReparacionForm(request.POST)
        if form.is_valid():
            activos_services.registrar_envio_reparacion(
                activo=activo, motivo=form.cleaned_data['motivo'],
                detalle_motivo=form.cleaned_data['detalle_motivo'], usuario=request.user,
            )
            registrar_evento(usuario=request.user, accion='activo.enviar_reparacion', objeto=activo, request=request)
            messages.success(request, f'{activo.codigo} enviado a reparación.')
            return redirect('panel:activo_detalle', pk=pk)
    else:
        form = EnvioReparacionForm()
    return render(request, 'panel/accion_form.html', {
        'form': form, 'titulo': f'Enviar a reparación {activo.codigo}',
        'volver_url': reverse('panel:activo_detalle', args=[pk]),
    })


@login_required
def activo_reparacion_retorno(request, pk):
    activo = get_object_or_404(Activo, pk=pk)
    if activo.estado != Activo.Estado.EN_REPARACION:
        messages.error(request, 'El activo no está en reparación.')
        return redirect('panel:activo_detalle', pk=pk)
    if request.method == 'POST':
        form = RetornoReparacionForm(request.POST)
        if form.is_valid():
            activos_services.registrar_retorno_reparacion(
                activo=activo, estado_fisico=form.cleaned_data['estado_fisico'],
                proveedor_tecnico=form.cleaned_data['proveedor_tecnico'], usuario=request.user,
            )
            registrar_evento(usuario=request.user, accion='activo.retorno_reparacion', objeto=activo, request=request)
            messages.success(request, f'{activo.codigo} de vuelta en bodega.')
            return redirect('panel:activo_detalle', pk=pk)
    else:
        form = RetornoReparacionForm()
    return render(request, 'panel/accion_form.html', {
        'form': form, 'titulo': f'Retorno de reparación: {activo.codigo}',
        'volver_url': reverse('panel:activo_detalle', args=[pk]),
    })


@login_required
def activo_baja(request, pk):
    activo = get_object_or_404(Activo, pk=pk)
    if activo.estado == Activo.Estado.DADO_DE_BAJA:
        messages.error(request, 'El activo ya está dado de baja.')
        return redirect('panel:activo_detalle', pk=pk)
    if request.method == 'POST':
        form = BajaForm(request.POST)
        if form.is_valid():
            activos_services.registrar_baja(
                activo=activo, motivo=form.cleaned_data['motivo'],
                detalle_motivo=form.cleaned_data['detalle_motivo'], usuario=request.user,
            )
            registrar_evento(usuario=request.user, accion='activo.baja', objeto=activo, request=request)
            messages.success(request, f'{activo.codigo} dado de baja.')
            return redirect('panel:activo_detalle', pk=pk)
    else:
        form = BajaForm()
    return render(request, 'panel/accion_form.html', {
        'form': form, 'titulo': f'Dar de baja {activo.codigo}',
        'subtitulo': 'El activo permanece en el sistema para auditoría; nunca se elimina.',
        'volver_url': reverse('panel:activo_detalle', args=[pk]),
    })


@login_required
def activo_consumible_entregar(request, pk):
    activo = get_object_or_404(Activo, pk=pk)
    if activo.estado != Activo.Estado.ASIGNADO:
        messages.error(request, 'El activo debe estar asignado para entregarle consumibles.')
        return redirect('panel:activo_detalle', pk=pk)
    if request.method == 'POST':
        form = ConsumibleEntregadoForm(request.POST)
        if form.is_valid():
            try:
                activos_services.registrar_consumible_entregado(
                    activo=activo, tipo_consumible=form.cleaned_data['tipo_consumible'],
                    cantidad=form.cleaned_data['cantidad'], usuario=request.user,
                )
            except ValueError as exc:
                form.add_error(None, str(exc))
            else:
                registrar_evento(usuario=request.user, accion='activo.consumible_entregar', objeto=activo, request=request)
                messages.success(request, 'Consumible entregado y descontado del stock.')
                return redirect('panel:activo_detalle', pk=pk)
    else:
        form = ConsumibleEntregadoForm()
    return render(request, 'panel/accion_form.html', {
        'form': form, 'titulo': f'Entregar consumible a {activo.colaborador_actual}',
        'subtitulo': f'Se descuenta del stock de {activo.bodega_actual}.',
        'volver_url': reverse('panel:activo_detalle', args=[pk]),
    })


@login_required
def bodegas_lista(request):
    bodegas = Bodega.objects.prefetch_related('stock__tipo_consumible').order_by('codigo')
    return render(request, 'panel/bodegas_lista.html', {'bodegas': bodegas})


@login_required
def bodega_stock_ingresar(request, pk):
    bodega = get_object_or_404(Bodega, pk=pk)
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
        'form': form, 'titulo': f'Ingresar consumibles a {bodega.codigo}',
        'volver_url': reverse('panel:bodegas_lista'),
    })
