from datetime import timedelta

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Count, F, Q
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone

from apps.activos import services as activos_services
from apps.activos.forms import (
    ActivoIngresoForm, AsignacionForm, BajaForm, ColaboradorForm, ConsumibleEntregadoForm,
    DevolucionForm, EnvioReparacionForm, OrdenCompraForm, RecibirOrdenCompraForm,
    RetornoReparacionForm, StockIngresoForm,
)
from apps.activos.models import Activo, Bodega, Colaborador, OrdenCompra
from apps.auditoria.models import EventoAuditoria, registrar_evento
from apps.catalogo.models import Estacion, Grupo
from apps.despliegues.models import Despliegue, ResultadoDespliegue
from apps.despliegues.services import publicar_despliegue

from .forms import DespliegueForm, PromoverDespliegueForm

ONLINE_UMBRAL_MINUTOS = 5


@login_required
def dashboard(request):
    umbral_online = timezone.now() - timedelta(minutes=ONLINE_UMBRAL_MINUTOS)

    grupos = Grupo.objects.annotate(
        total_estaciones=Count('farmacias__estaciones', distinct=True),
        total_farmacias=Count('farmacias', distinct=True),
        conformes=Count(
            'farmacias__estaciones',
            filter=Q(farmacias__estaciones__version_pos=F('version_objetivo')),
            distinct=True,
        ),
        online=Count(
            'farmacias__estaciones',
            filter=Q(farmacias__estaciones__ultimo_heartbeat__gte=umbral_online),
            distinct=True,
        ),
        pendientes=Count(
            'farmacias__estaciones',
            filter=Q(farmacias__estaciones__estado_aprobacion=Estacion.EstadoAprobacion.PENDIENTE),
            distinct=True,
        ),
    ).order_by('codigo')

    for g in grupos:
        g.pct_conforme = round(100 * g.conformes / g.total_estaciones) if g.total_estaciones else None

    despliegues_activos = (
        Despliegue.objects
        .filter(estado__in=[Despliegue.Estado.PUBLICANDO, Despliegue.Estado.PAUSADO])
        .order_by('-fecha_publicacion')
    )
    total_pendientes = Estacion.objects.filter(estado_aprobacion=Estacion.EstadoAprobacion.PENDIENTE).count()

    return render(request, 'panel/dashboard.html', {
        'grupos': grupos,
        'despliegues_activos': despliegues_activos,
        'total_pendientes': total_pendientes,
    })


@login_required
def estaciones_lista(request):
    estaciones = Estacion.objects.select_related('farmacia', 'farmacia__grupo').order_by('codigo')

    grupo = request.GET.get('grupo')
    estado_conexion = request.GET.get('estado_conexion')
    solo_desactualizadas = request.GET.get('desactualizadas')

    if grupo:
        estaciones = estaciones.filter(farmacia__grupo__codigo=grupo)
    if estado_conexion:
        estaciones = estaciones.filter(estado_conexion=estado_conexion)
    if solo_desactualizadas:
        estaciones = [e for e in estaciones if e.desactualizada]

    return render(request, 'panel/estaciones_lista.html', {
        'estaciones': estaciones,
        'grupos': Grupo.objects.order_by('codigo'),
        'filtro_grupo': grupo or '',
        'filtro_estado': estado_conexion or '',
        'filtro_desactualizadas': solo_desactualizadas or '',
    })


@login_required
def estaciones_pendientes_partial(request):
    pendientes = Estacion.objects.select_related('farmacia', 'farmacia__grupo').filter(
        estado_aprobacion=Estacion.EstadoAprobacion.PENDIENTE,
    ).order_by('codigo')
    return render(request, 'panel/estaciones_pendientes_partial.html', {'pendientes': pendientes})


@login_required
def estacion_aprobar(request, pk):
    estacion = get_object_or_404(Estacion, pk=pk)
    estacion.estado_aprobacion = Estacion.EstadoAprobacion.APROBADA
    estacion.save(update_fields=['estado_aprobacion'])
    registrar_evento(usuario=request.user, accion='estacion.aprobar', objeto=estacion, request=request)
    return estaciones_pendientes_partial(request)


@login_required
def estacion_rechazar(request, pk):
    estacion = get_object_or_404(Estacion, pk=pk)
    estacion.estado_aprobacion = Estacion.EstadoAprobacion.RECHAZADA
    estacion.save(update_fields=['estado_aprobacion'])
    registrar_evento(usuario=request.user, accion='estacion.rechazar', objeto=estacion, request=request)
    return estaciones_pendientes_partial(request)


@login_required
def estaciones_aprobar_lote(request):
    if request.method == 'POST':
        ids = request.POST.getlist('estacion_ids')
        estaciones = Estacion.objects.filter(pk__in=ids, estado_aprobacion=Estacion.EstadoAprobacion.PENDIENTE)
        for estacion in estaciones:
            estacion.estado_aprobacion = Estacion.EstadoAprobacion.APROBADA
            estacion.save(update_fields=['estado_aprobacion'])
            registrar_evento(usuario=request.user, accion='estacion.aprobar', objeto=estacion, request=request)
    return estaciones_pendientes_partial(request)


@login_required
def despliegues_lista(request):
    despliegues = Despliegue.objects.select_related('creado_por', 'aprobado_por').order_by('-fecha_creacion')
    return render(request, 'panel/despliegues_lista.html', {'despliegues': despliegues})


@login_required
def despliegue_crear(request):
    if request.method == 'POST':
        form = DespliegueForm(request.POST, request.FILES)
        if form.is_valid():
            despliegue = form.save(commit=False)
            despliegue.creado_por = request.user
            despliegue.estado = Despliegue.Estado.PENDIENTE_APROBACION
            despliegue.save()
            form.save_m2m()
            registrar_evento(usuario=request.user, accion='despliegue.crear', objeto=despliegue, request=request)
            messages.success(request, f'Despliegue {despliegue.version} creado, pendiente de aprobación.')
            return redirect('panel:despliegue_detalle', pk=despliegue.pk)
    else:
        form = DespliegueForm()
    return render(request, 'panel/despliegue_form.html', {'form': form})


@login_required
def despliegue_detalle(request, pk):
    despliegue = get_object_or_404(
        Despliegue.objects
        .select_related('creado_por', 'aprobado_por', 'despliegue_origen')
        .prefetch_related('grupos', 'farmacias', 'promovidos'),
        pk=pk,
    )
    puede_aprobar = (
        despliegue.estado == Despliegue.Estado.PENDIENTE_APROBACION
        and despliegue.creado_por_id != request.user.id
    )
    puede_promover = despliegue.estado == Despliegue.Estado.COMPLETADO
    return render(request, 'panel/despliegue_detalle.html', {
        'despliegue': despliegue,
        'puede_aprobar': puede_aprobar,
        'puede_promover': puede_promover,
    })


@login_required
def despliegue_promover(request, pk):
    origen = get_object_or_404(Despliegue, pk=pk)
    if origen.estado != Despliegue.Estado.COMPLETADO:
        messages.error(request, 'Solo se puede promover un despliegue ya completado.')
        return redirect('panel:despliegue_detalle', pk=pk)

    if request.method == 'POST':
        form = PromoverDespliegueForm(request.POST)
        if form.is_valid():
            nuevo = form.save(commit=False)
            nuevo.version = origen.version
            nuevo.archivo = origen.archivo
            nuevo.sha256 = origen.sha256
            nuevo.descripcion = f'Anillo siguiente de despliegue #{origen.pk} ({origen.get_destino_tipo_display()})'
            nuevo.modo_aplicacion = origen.modo_aplicacion
            nuevo.creado_por = request.user
            nuevo.estado = Despliegue.Estado.PENDIENTE_APROBACION
            nuevo.despliegue_origen = origen
            nuevo.save()
            form.save_m2m()
            registrar_evento(usuario=request.user, accion='despliegue.promover', objeto=nuevo, request=request)
            messages.success(request, f'Anillo siguiente creado para v{nuevo.version}, pendiente de aprobación.')
            return redirect('panel:despliegue_detalle', pk=nuevo.pk)
    else:
        form = PromoverDespliegueForm()

    return render(request, 'panel/accion_form.html', {
        'form': form, 'titulo': f'Promover v{origen.version} al siguiente anillo',
        'subtitulo': f'Mismo paquete y versión que el despliegue #{origen.pk}; solo cambia a quién llega.',
        'volver_url': reverse('panel:despliegue_detalle', args=[pk]),
    })


@login_required
def despliegue_progreso_partial(request, pk):
    despliegue = get_object_or_404(Despliegue, pk=pk)
    resultados = despliegue.resultados.select_related('estacion', 'estacion__farmacia').order_by('estacion__codigo')

    total = resultados.count()
    conteo = {estado: 0 for estado, _ in ResultadoDespliegue.Estado.choices}
    for r in resultados:
        conteo[r.estado] += 1
    aplicados = conteo.get(ResultadoDespliegue.Estado.APLICADO, 0)
    errores = conteo.get(ResultadoDespliegue.Estado.ERROR, 0) + conteo.get(ResultadoDespliegue.Estado.ROLLBACK, 0)
    pct_completado = round(100 * aplicados / total) if total else 0

    return render(request, 'panel/despliegue_progreso_partial.html', {
        'despliegue': despliegue,
        'resultados': resultados,
        'total': total,
        'aplicados': aplicados,
        'errores': errores,
        'pct_completado': pct_completado,
        'conteo': conteo,
    })


@login_required
def despliegue_aprobar(request, pk):
    despliegue = get_object_or_404(Despliegue, pk=pk)
    if despliegue.estado != Despliegue.Estado.PENDIENTE_APROBACION:
        messages.error(request, 'Este despliegue ya no está pendiente de aprobación.')
    elif despliegue.creado_por_id == request.user.id:
        messages.error(request, 'Quien crea un despliegue no puede aprobarlo (regla de cuatro ojos).')
    else:
        despliegue.estado = Despliegue.Estado.APROBADO
        despliegue.aprobado_por = request.user
        despliegue.save(update_fields=['estado', 'aprobado_por'])
        registrar_evento(usuario=request.user, accion='despliegue.aprobar', objeto=despliegue, request=request)
        messages.success(request, f'Despliegue {despliegue.version} aprobado.')
    return redirect('panel:despliegue_detalle', pk=pk)


@login_required
def despliegue_publicar(request, pk):
    despliegue = get_object_or_404(Despliegue, pk=pk)
    if despliegue.estado != Despliegue.Estado.APROBADO:
        messages.error(request, 'El despliegue debe estar aprobado antes de publicarse.')
    else:
        total = publicar_despliegue(despliegue)
        registrar_evento(
            usuario=request.user, accion='despliegue.publicar', objeto=despliegue,
            detalle={'estaciones_destino': total}, request=request,
        )
        messages.success(request, f'Despliegue publicado a {total} estación(es).')
    return redirect('panel:despliegue_detalle', pk=pk)


@login_required
def despliegue_pausar(request, pk):
    despliegue = get_object_or_404(Despliegue, pk=pk)
    if despliegue.estado == Despliegue.Estado.PUBLICANDO:
        despliegue.estado = Despliegue.Estado.PAUSADO
        despliegue.save(update_fields=['estado'])
        registrar_evento(usuario=request.user, accion='despliegue.pausar', objeto=despliegue, request=request)
        messages.success(request, 'Despliegue pausado.')
    return redirect('panel:despliegue_detalle', pk=pk)


@login_required
def despliegue_reanudar(request, pk):
    despliegue = get_object_or_404(Despliegue, pk=pk)
    if despliegue.estado == Despliegue.Estado.PAUSADO:
        despliegue.estado = Despliegue.Estado.PUBLICANDO
        despliegue.save(update_fields=['estado'])
        registrar_evento(usuario=request.user, accion='despliegue.reanudar', objeto=despliegue, request=request)
        messages.success(request, 'Despliegue reanudado.')
    return redirect('panel:despliegue_detalle', pk=pk)


@login_required
def auditoria_lista(request):
    eventos = EventoAuditoria.objects.select_related('usuario').order_by('-timestamp')[:200]
    return render(request, 'panel/auditoria_lista.html', {'eventos': eventos})


# ---------------------------------------------------------------------------
# Módulo de Activos (Fase 2b)
# ---------------------------------------------------------------------------

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
    activos = Activo.objects.select_related('bodega_actual', 'colaborador_actual').order_by('codigo')

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
                tipo=d['tipo'], marca=d['marca'], modelo=d['modelo'], numero_serie=d['numero_serie'],
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
        Activo.objects.select_related('bodega_actual', 'colaborador_actual', 'orden_compra'), pk=pk,
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
