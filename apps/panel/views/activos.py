from django.contrib import messages
from django.contrib.auth.decorators import login_required, permission_required
from django.db.models import Q
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from apps.activos import services as activos_services
from apps.activos.forms import (
    ActivoIngresoForm, AjusteStockForm, AnularRecepcionForm, AsignacionForm, BajaForm, ColaboradorForm,
    ConsumibleEntregadoForm, DevolucionForm, EnvioReparacionForm, OrdenCompraForm, OrdenCompraLineaForm,
    RecepcionLoteForm, RecibirOrdenCompraForm, RetornoReparacionForm, StockIngresoForm, UbicarFarmaciaForm,
)
from apps.activos.models import (
    Activo, Bodega, Colaborador, MovimientoInventario, OrdenCompra, OrdenCompraDetalle, RecepcionLote, Ubicacion,
)
from apps.activos.services import ConcurrencyError
from apps.auditoria.models import registrar_evento
from apps.mantenimiento import services as mantenimiento_services
from apps.mantenimiento.forms import VisitaTecnicaForm
from apps.mantenimiento.models import Mantenimiento, VisitaTecnica
from apps.cuentas.services import (
    scope_opcional_por_unidad_negocio, scope_opcional_por_unidad_negocio_activa, usuario_puede_ver,
    verificar_acceso,
)


@login_required
@permission_required('activos.view_colaborador', raise_exception=True)
def colaboradores_lista(request):
    colaboradores = scope_opcional_por_unidad_negocio_activa(
        Colaborador.objects.order_by('nombre'), request, 'unidad_negocio',
    )
    return render(request, 'panel/colaboradores_lista.html', {'colaboradores': colaboradores})


@login_required
@permission_required('activos.add_colaborador', raise_exception=True)
def colaborador_crear(request):
    if request.method == 'POST':
        form = ColaboradorForm(request.POST, user=request.user)
        if form.is_valid():
            colaborador = form.save()
            registrar_evento(usuario=request.user, accion='colaborador.crear', objeto=colaborador, request=request)
            messages.success(request, f'Colaborador {colaborador.nombre} registrado.')
            return redirect('panel:colaboradores_lista')
    else:
        form = ColaboradorForm(user=request.user)
    return render(request, 'panel/accion_form.html', {
        'form': form, 'titulo': 'Nuevo colaborador', 'boton': 'Registrar colaborador',
        'subtitulo': 'Carga manual mientras se integra con RRHH/nómina.',
        'volver_url': reverse('panel:colaboradores_lista'),
    })


@login_required
@permission_required('mantenimiento.view_visitatecnica', raise_exception=True)
def visita_tecnica_lista(request):
    """Visitas planificadas/realizadas por farmacia.

    Reemplaza al reporte anterior, que agrupaba colaboradores por `activos.Ubicacion`
    -- una tabla que en producción está vacía, así que no mostraba nada -- y que
    además no dejaba ningún rastro de si la visita se hizo.
    """
    visitas = scope_opcional_por_unidad_negocio_activa(
        VisitaTecnica.objects.select_related('farmacia', 'tecnico'), request, 'farmacia__unidad_negocio',
    )
    estado = request.GET.get('estado')
    if estado:
        visitas = visitas.filter(estado=estado)
    return render(request, 'panel/visita_tecnica_lista.html', {
        'visitas': visitas,
        'estados': VisitaTecnica.Estado.choices,
        'filtro_estado': estado or '',
    })


@login_required
@permission_required('mantenimiento.add_visitatecnica', raise_exception=True)
def visita_tecnica_crear(request):
    if request.method == 'POST':
        form = VisitaTecnicaForm(request.POST, user=request.user)
        if form.is_valid():
            d = form.cleaned_data
            verificar_acceso(request.user, d['farmacia'].unidad_negocio)
            visita = mantenimiento_services.crear_visita_tecnica(
                farmacia=d['farmacia'], tecnico=d['tecnico'],
                fecha_planificada=d['fecha_planificada'], motivo=d['motivo'], usuario=request.user,
            )
            registrar_evento(usuario=request.user, accion='visita.crear', objeto=visita, request=request)
            messages.success(request, f'Visita a {visita.farmacia.codigo} planificada.')
            return redirect('panel:visita_tecnica_lista')
    else:
        form = VisitaTecnicaForm(user=request.user)
    return render(request, 'panel/accion_form.html', {
        'form': form, 'titulo': 'Planificar visita técnica',
        'volver_url': reverse('panel:visita_tecnica_lista'),
    })


@login_required
@permission_required('mantenimiento.change_visitatecnica', raise_exception=True)
@require_POST
def visita_tecnica_accion(request, pk, accion):
    """Transiciones de la visita en una sola vista: los tres botones comparten el mismo
    andamiaje (validar acceso, llamar al servicio, auditar, avisar) y separarlos serían
    tres copias de lo mismo."""
    visita = get_object_or_404(VisitaTecnica, pk=pk)
    verificar_acceso(request.user, visita.farmacia.unidad_negocio)
    servicios = {
        'iniciar': mantenimiento_services.iniciar_visita_tecnica,
        'cerrar': mantenimiento_services.cerrar_visita_tecnica,
        'cancelar': mantenimiento_services.cancelar_visita_tecnica,
    }
    if accion not in servicios:
        messages.error(request, 'Acción no reconocida.')
        return redirect('panel:visita_tecnica_lista')

    kwargs = {'visita': visita, 'usuario': request.user}
    if accion == 'cerrar':
        kwargs['observaciones'] = request.POST.get('observaciones', '')
    elif accion == 'cancelar':
        kwargs['motivo'] = request.POST.get('motivo', '')
    try:
        servicios[accion](**kwargs)
    except ValueError as exc:
        messages.error(request, str(exc))
    else:
        registrar_evento(usuario=request.user, accion=f'visita.{accion}', objeto=visita, request=request)
        if accion == 'cerrar':
            visita.refresh_from_db()
            if visita.presencia_en_sitio == 'fuera_de_rango':
                messages.warning(
                    request,
                    f'Visita cerrada, pero el GPS ubicó al técnico a '
                    f'{visita.distancia_verificacion_metros:.0f} m de la farmacia.',
                )
            else:
                messages.success(request, 'Visita cerrada.')
        else:
            messages.success(request, 'Visita actualizada.')
    return redirect('panel:visita_tecnica_lista')


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


@login_required
@permission_required('activos.view_activo', raise_exception=True)
def activos_lista(request):
    activos = scope_opcional_por_unidad_negocio_activa(
        Activo.objects.select_related('bodega_actual', 'colaborador_actual', 'marca', 'categoria'),
        request, 'unidad_negocio',
    ).order_by('codigo')

    tipo = request.GET.get('tipo')
    estado = request.GET.get('estado')
    bodega = request.GET.get('bodega')
    ubicacion = request.GET.get('ubicacion')
    if tipo:
        activos = activos.filter(tipo=tipo)
    if estado:
        activos = activos.filter(estado=estado)
    if bodega:
        activos = activos.filter(bodega_actual__codigo=bodega)
    if ubicacion == 'farmacia':
        activos = activos.filter(farmacia__isnull=False)
    elif ubicacion == 'administrativo':
        activos = activos.filter(farmacia__isnull=True)

    return render(request, 'panel/activos_lista.html', {
        'activos': activos.select_related('farmacia'),
        'tipos': Activo.Tipo.choices,
        'estados': Activo.Estado.choices,
        'bodegas': scope_opcional_por_unidad_negocio(Bodega.objects.all(), request.user, 'unidad_negocio').order_by('codigo'),
        'filtro_tipo': tipo or '', 'filtro_estado': estado or '', 'filtro_bodega': bodega or '',
        'filtro_ubicacion': ubicacion or '',
    })


@login_required
@permission_required('activos.add_activo', raise_exception=True)
def especificaciones_por_serie_partial(request):
    """Precarga las especificaciones de cómputo desde la estación RMM que ya reporta
    ese número de serie (HTMX, al salir del campo "numero_serie").

    El agente ya sabe procesador/RAM/disco de cada equipo: volver a tipearlos es
    trabajo duplicado y, peor, dos fuentes que terminan diciendo cosas distintas del
    mismo equipo.

    Se devuelven los campos del formulario ya rellenos, no un JSON: así el HTML sigue
    siendo el que arma Django (widgets, clases, errores) y no hay que duplicar el
    render en JavaScript.
    """
    serie = request.GET.get('numero_serie', '')
    datos = activos_services.datos_hardware_desde_estacion(serie)

    # Se conserva lo que el usuario ya haya escrito: la estación solo COMPLETA lo
    # que falta, nunca pisa un dato cargado a mano.
    inicial = {
        campo: request.GET.get(campo) or (datos or {}).get(campo)
        for campo in ('procesador', 'ram_gb', 'almacenamiento_gb')
    }
    form = ActivoIngresoForm(user=request.user, initial=inicial)
    return render(request, 'panel/_especificaciones_computo.html', {
        'form': form,
        'estacion': (datos or {}).get('estacion'),
    })


@login_required
@permission_required('activos.add_activo', raise_exception=True)
def activo_crear(request):
    if request.method == 'POST':
        form = ActivoIngresoForm(request.POST, user=request.user)
        if form.is_valid():
            d = form.cleaned_data
            activo = activos_services.registrar_ingreso(
                tipo=d['tipo'], marca=d['marca'], categoria=d['categoria'],
                modelo=d['modelo'], numero_serie=d['numero_serie'],
                procesador=d['procesador'], ram_gb=d['ram_gb'], almacenamiento_gb=d['almacenamiento_gb'],
                codigo_sap=d['codigo_sap'], condicion_al_recibir=d['condicion_al_recibir'],
                fecha_compra=d['fecha_compra'], vencimiento_garantia=d['vencimiento_garantia'],
                orden_compra=d['orden_compra'], bodega=d['bodega'], farmacia=d['farmacia'], usuario=request.user,
            )
            registrar_evento(usuario=request.user, accion='activo.ingreso', objeto=activo, request=request)
            messages.success(request, f'Activo {activo.codigo} registrado en {activo.bodega_actual.codigo}.')
            return redirect('panel:activo_detalle', pk=activo.pk)
    else:
        initial = {}
        oc_id = request.GET.get('oc')
        if oc_id:
            initial['orden_compra'] = oc_id
        form = ActivoIngresoForm(user=request.user, initial=initial)
    return render(request, 'panel/activo_form.html', {
        'form': form, 'titulo': 'Registrar ingreso de activo',
        'subtitulo': 'Genera el código CR-TIPO-NNNN automáticamente y lo deja "En bodega".',
        'volver_url': reverse('panel:activos_lista'),
    })


@login_required
@permission_required('activos.view_activo', raise_exception=True)
def activo_detalle(request, pk):
    activo = get_object_or_404(
        Activo.objects.select_related(
            'bodega_actual', 'colaborador_actual', 'orden_compra', 'marca', 'categoria',
            'estacion', 'estacion__farmacia', 'farmacia',
        ), pk=pk,
    )
    verificar_acceso(request.user, activo.unidad_negocio)
    eventos = activo.eventos.select_related('usuario').order_by('-timestamp')
    mantenimiento_abierto = Mantenimiento.objects.filter(
        equipos__equipo=activo,
        estado_interno__in=[Mantenimiento.EstadoInterno.PENDIENTE, Mantenimiento.EstadoInterno.EN_PROCESO],
    ).order_by('-fecha_creacion').first()
    return render(request, 'panel/activo_detalle.html', {
        'activo': activo, 'eventos': eventos, 'mantenimiento_abierto': mantenimiento_abierto,
    })


@login_required
@permission_required('activos.change_activo', raise_exception=True)
def activo_asignar(request, pk):
    activo = get_object_or_404(Activo, pk=pk)
    verificar_acceso(request.user, activo.unidad_negocio)
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
        'form': form, 'titulo': f'Asignar {activo.codigo}', 'boton': 'Asignar',
        'resumen_tipo': 'activo', 'resumen_titulo': f'{activo.codigo} — {activo.get_tipo_display()}',
        'resumen_sub': f'{activo.marca or ""} {activo.modelo}'.strip(),
        'resumen_campos': [
            ('Estado', activo.get_estado_display()), ('Bodega actual', activo.bodega_actual),
        ],
        'volver_url': reverse('panel:activo_detalle', args=[pk]),
    })


@login_required
@permission_required('activos.change_activo', raise_exception=True)
def activo_ubicar_farmacia(request, pk):
    """Marca en qué farmacia está físicamente un activo -- independiente de su ciclo de
    vida (bodega/asignado/reparación), a diferencia de "asignar", que sí exige
    En bodega. Un PDV no tiene "colaborador asignado" en el mismo sentido que un
    equipo de oficina."""
    activo = get_object_or_404(Activo, pk=pk)
    verificar_acceso(request.user, activo.unidad_negocio)
    if activo.estado == Activo.Estado.DADO_DE_BAJA:
        messages.error(request, 'Un activo dado de baja no puede reubicarse.')
        return redirect('panel:activo_detalle', pk=pk)
    if activo.estacion_id:
        messages.error(
            request,
            'Este activo tiene una Estación RMM vinculada -- su farmacia se sincroniza sola, no se puede '
            'cambiar a mano.',
        )
        return redirect('panel:activo_detalle', pk=pk)
    if request.method == 'POST':
        form = UbicarFarmaciaForm(request.POST, user=request.user)
        if form.is_valid():
            activos_services.registrar_ubicacion_farmacia(
                activo=activo, farmacia=form.cleaned_data['farmacia'], usuario=request.user,
            )
            registrar_evento(usuario=request.user, accion='activo.ubicar_farmacia', objeto=activo, request=request)
            messages.success(request, f'{activo.codigo} actualizado.')
            return redirect('panel:activo_detalle', pk=pk)
    else:
        form = UbicarFarmaciaForm(user=request.user, initial={'farmacia': activo.farmacia_id})
    return render(request, 'panel/accion_form.html', {
        'form': form, 'titulo': f'Ubicar {activo.codigo} en una farmacia', 'boton': 'Guardar',
        'subtitulo': 'Deja el campo vacío para marcarlo como administrativo/oficina.',
        'resumen_tipo': 'activo', 'resumen_titulo': f'{activo.codigo} — {activo.get_tipo_display()}',
        'resumen_sub': f'{activo.marca or ""} {activo.modelo}'.strip(),
        'resumen_campos': [
            ('Farmacia actual', activo.farmacia or 'Administrativo / sin ubicar'),
        ],
        'volver_url': reverse('panel:activo_detalle', args=[pk]),
    })


@login_required
@permission_required('activos.change_activo', raise_exception=True)
def activo_devolver(request, pk):
    activo = get_object_or_404(Activo, pk=pk)
    verificar_acceso(request.user, activo.unidad_negocio)
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
        'form': form, 'titulo': f'Registrar devolución de {activo.codigo}', 'boton': 'Registrar devolución',
        'resumen_tipo': 'activo', 'resumen_titulo': f'{activo.codigo} — {activo.get_tipo_display()}',
        'resumen_sub': f'{activo.marca or ""} {activo.modelo}'.strip(),
        'resumen_campos': [
            ('Asignado a', activo.colaborador_actual),
            ('Estado físico actual', activo.get_estado_fisico_actual_display() or '—'),
        ],
        'volver_url': reverse('panel:activo_detalle', args=[pk]),
    })


@login_required
@permission_required('activos.change_activo', raise_exception=True)
def activo_reparacion_enviar(request, pk):
    activo = get_object_or_404(Activo, pk=pk)
    verificar_acceso(request.user, activo.unidad_negocio)
    if activo.estado == Activo.Estado.DADO_DE_BAJA:
        messages.error(request, 'Un activo dado de baja no puede enviarse a reparación.')
        return redirect('panel:activo_detalle', pk=pk)
    if request.method == 'POST':
        form = EnvioReparacionForm(request.POST)
        if form.is_valid():
            try:
                mantenimiento = mantenimiento_services.iniciar_reparacion_desde_activo(
                    activo=activo, motivo=form.cleaned_data['motivo'],
                    detalle_motivo=form.cleaned_data['detalle_motivo'], usuario=request.user,
                )
            except ValueError as exc:
                form.add_error(None, str(exc))
            else:
                registrar_evento(
                    usuario=request.user, accion='activo.enviar_reparacion', objeto=activo, request=request,
                    detalle={'mantenimiento_id': mantenimiento.pk},
                )
                messages.success(
                    request,
                    f'{activo.codigo} enviado a reparación — se abrió el mantenimiento #{mantenimiento.pk} '
                    'para hacer seguimiento (checklist, firma, repuestos, informe).',
                )
                return redirect('panel:mantenimiento_detalle', pk=mantenimiento.pk)
    else:
        form = EnvioReparacionForm()
    return render(request, 'panel/accion_form.html', {
        'form': form, 'titulo': f'Enviar a reparación {activo.codigo}', 'boton': 'Enviar a reparación',
        'tono': 'warning',
        'subtitulo': 'Esto abre un Mantenimiento vinculado a este activo para hacer seguimiento completo '
                     '(checklist, firma, repuestos, informe PDF) — no es solo un cambio de estado.',
        'resumen_tipo': 'activo', 'resumen_titulo': f'{activo.codigo} — {activo.get_tipo_display()}',
        'resumen_sub': f'{activo.marca or ""} {activo.modelo}'.strip(),
        'resumen_campos': [
            ('Estado actual', activo.get_estado_display()),
            ('Asignado a / Bodega', activo.colaborador_actual or activo.bodega_actual),
        ],
        'volver_url': reverse('panel:activo_detalle', args=[pk]),
    })


@login_required
@permission_required('activos.change_activo', raise_exception=True)
def activo_reparacion_retorno(request, pk):
    activo = get_object_or_404(Activo, pk=pk)
    verificar_acceso(request.user, activo.unidad_negocio)
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
        'form': form, 'titulo': f'Retorno de reparación: {activo.codigo}', 'boton': 'Registrar retorno',
        'subtitulo': 'Este activo quedó en reparación antes de que "enviar a reparación" abriera un '
                     'Mantenimiento vinculado — registro directo, sin checklist ni firma.',
        'resumen_tipo': 'activo', 'resumen_titulo': f'{activo.codigo} — {activo.get_tipo_display()}',
        'resumen_sub': f'{activo.marca or ""} {activo.modelo}'.strip(),
        'resumen_campos': [('Estado actual', activo.get_estado_display())],
        'volver_url': reverse('panel:activo_detalle', args=[pk]),
    })


@login_required
@permission_required('activos.change_activo', raise_exception=True)
def activo_baja(request, pk):
    activo = get_object_or_404(Activo, pk=pk)
    verificar_acceso(request.user, activo.unidad_negocio)
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
        'form': form, 'titulo': f'Dar de baja {activo.codigo}', 'boton': 'Confirmar baja', 'tono': 'danger',
        'subtitulo': 'El activo permanece en el sistema para auditoría; nunca se elimina.',
        'resumen_tipo': 'activo', 'resumen_titulo': f'{activo.codigo} — {activo.get_tipo_display()}',
        'resumen_sub': f'{activo.marca or ""} {activo.modelo}'.strip(),
        'resumen_campos': [
            ('Estado actual', activo.get_estado_display()),
            ('Asignado a / Bodega', activo.colaborador_actual or activo.bodega_actual),
        ],
        'volver_url': reverse('panel:activo_detalle', args=[pk]),
    })


@login_required
@permission_required('activos.change_activo', raise_exception=True)
def activo_consumible_entregar(request, pk):
    activo = get_object_or_404(Activo, pk=pk)
    verificar_acceso(request.user, activo.unidad_negocio)
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
        'form': form, 'titulo': f'Entregar consumible a {activo.colaborador_actual}', 'boton': 'Entregar',
        'resumen_tipo': 'activo', 'resumen_titulo': f'{activo.codigo} — {activo.get_tipo_display()}',
        'resumen_sub': f'Asignado a {activo.colaborador_actual}',
        'resumen_campos': [('Bodega de descuento', activo.bodega_actual)],
        'volver_url': reverse('panel:activo_detalle', args=[pk]),
    })


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


@login_required
@permission_required('activos.view_activo', raise_exception=True)
def activos_avisos(request):
    """Panel de visibilidad v1 (sin correo/Alerta todavía, decisión del usuario):
    garantías vencidas/por vencer, stock de consumibles bajo mínimo, y las anomalías
    detectadas cruzando Activo.numero_serie contra el número de serie que reporta el
    agente RMM (activo dado de baja que sigue conectado, equipo movido sin registro)."""
    garantias = scope_opcional_por_unidad_negocio_activa(
        activos_services.activos_por_vencer_garantia(), request, 'unidad_negocio',
    )
    stock_bajo = scope_opcional_por_unidad_negocio_activa(
        activos_services.stock_bajo_minimo(), request, 'bodega__unidad_negocio',
    )
    dados_de_baja_conectados = scope_opcional_por_unidad_negocio_activa(
        activos_services.activos_dados_de_baja_pero_conectados(), request, 'estacion__farmacia__unidad_negocio',
    )
    movidos_sin_registro = scope_opcional_por_unidad_negocio_activa(
        activos_services.activos_movidos_sin_registro(), request, 'estacion__farmacia__unidad_negocio',
    )
    return render(request, 'panel/activos_avisos.html', {
        'garantias': garantias, 'stock_bajo': stock_bajo,
        'dados_de_baja_conectados': dados_de_baja_conectados, 'movidos_sin_registro': movidos_sin_registro,
        'hoy': timezone.now().date(),
    })
