from django.contrib import messages
from django.contrib.auth.decorators import login_required, permission_required
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse

from apps.auditoria.models import registrar_evento
from apps.cuentas.services import scope_opcional_por_unidad_negocio_activa, verificar_acceso
from apps.mantenimiento import services as mantenimiento_services
from apps.mantenimiento.forms import (
    ActividadPlanificadaForm, CancelarMantenimientoForm, CerrarMantenimientoForm, CompletarActividadForm,
    FirmaMantenimientoForm, ImagenMantenimientoForm, MantenimientoManualForm, MantenimientoProgramadoForm,
    RepuestoUtilizadoForm,
)
from apps.mantenimiento.models import (
    ActividadChecklist, ActividadPlanificada, Mantenimiento, MantenimientoProgramado, Notificacion,
)
from apps.mantenimiento.tasks import generar_informe_pdf_task


def _resumen_mantenimiento(mantenimiento):
    """Contexto común para el card de resumen de accion_form.html en las acciones
    de un Mantenimiento puntual (cerrar/cancelar/adjuntar imagen/repuesto) -- antes
    esos formularios no mostraban nada sobre el mantenimiento que estaban afectando."""
    principal = mantenimiento.equipos.select_related('equipo').filter(es_principal=True).first()
    return {
        'resumen_tipo': 'mantenimiento',
        'resumen_titulo': f'Mantenimiento #{mantenimiento.pk}',
        'resumen_sub': f'{mantenimiento.cliente or "Sin cliente"} · {mantenimiento.get_estado_interno_display()}',
        'resumen_campos': [
            ('Equipo principal', principal.equipo if principal else '—'),
            ('Técnico', mantenimiento.tecnico or 'Sin asignar'),
            ('Programado para', mantenimiento.fecha_programada),
        ],
    }


@login_required
@permission_required('mantenimiento.view_mantenimiento', raise_exception=True)
def mantenimientos_lista(request):
    mantenimientos = scope_opcional_por_unidad_negocio_activa(
        Mantenimiento.objects.select_related('cliente', 'tecnico'), request, 'cliente__unidad_negocio',
    ).order_by('-fecha_programada')

    estado = request.GET.get('estado')
    if estado:
        mantenimientos = mantenimientos.filter(estado_interno=estado)

    return render(request, 'panel/mantenimientos_lista.html', {
        'mantenimientos': mantenimientos,
        'estados': Mantenimiento.EstadoInterno.choices,
        'filtro_estado': estado or '',
    })


@login_required
@permission_required('mantenimiento.add_mantenimiento', raise_exception=True)
def mantenimiento_crear(request):
    if request.method == 'POST':
        form = MantenimientoManualForm(request.POST)
        if form.is_valid():
            d = form.cleaned_data
            try:
                mantenimiento = mantenimiento_services.crear_mantenimiento_manual(
                    equipos=list(d['equipos']), cliente=d['cliente'], tecnico=d['tecnico'],
                    tipo_mantenimiento=d['tipo_mantenimiento'], descripcion=d['descripcion'],
                    fecha_programada=d['fecha_programada'], estado_general=d['estado_general'],
                    mantenimiento_programado=d['mantenimiento_programado'], usuario=request.user,
                )
            except ValueError as exc:
                form.add_error(None, str(exc))
            else:
                registrar_evento(
                    usuario=request.user, accion='mantenimiento.crear', objeto=mantenimiento, request=request,
                )
                messages.success(request, f'Mantenimiento #{mantenimiento.pk} creado.')
                return redirect('panel:mantenimiento_detalle', pk=mantenimiento.pk)
    else:
        form = MantenimientoManualForm()
    return render(request, 'panel/accion_form.html', {
        'form': form, 'titulo': 'Nuevo mantenimiento', 'boton': 'Crear mantenimiento',
        'volver_url': reverse('panel:mantenimientos_lista'),
    })


@login_required
@permission_required('mantenimiento.view_mantenimiento', raise_exception=True)
def mantenimiento_detalle(request, pk):
    mantenimiento = get_object_or_404(
        Mantenimiento.objects.select_related('cliente', 'tecnico', 'mantenimiento_programado'),
        pk=pk,
    )
    verificar_acceso(request.user, mantenimiento.unidad_negocio)
    equipos = mantenimiento.equipos.select_related('equipo')
    eventos = mantenimiento.eventos.select_related('usuario').order_by('-timestamp')

    checklist_items = ActividadChecklist.objects.filter(activo=True).order_by('orden', 'nombre')
    realizadas = {
        ar.actividad_id: ar.realizada
        for ar in mantenimiento.actividades_realizadas.all()
    }
    checklist = [{'item': item, 'realizada': realizadas.get(item.pk, False)} for item in checklist_items]
    repuestos = mantenimiento.repuestos_utilizados.select_related('tipo_consumible', 'bodega')

    return render(request, 'panel/mantenimiento_detalle.html', {
        'mantenimiento': mantenimiento, 'equipos': equipos, 'eventos': eventos, 'checklist': checklist,
        'repuestos': repuestos, 'costo_total_repuestos': mantenimiento.costo_total_repuestos,
    })


@login_required
@permission_required('mantenimiento.change_mantenimiento', raise_exception=True)
def mantenimiento_iniciar(request, pk):
    mantenimiento = get_object_or_404(Mantenimiento, pk=pk)
    verificar_acceso(request.user, mantenimiento.unidad_negocio)
    try:
        mantenimiento_services.iniciar_mantenimiento(mantenimiento=mantenimiento, usuario=request.user)
    except ValueError as exc:
        messages.error(request, str(exc))
    else:
        registrar_evento(
            usuario=request.user, accion='mantenimiento.iniciar', objeto=mantenimiento, request=request,
        )
        messages.success(request, f'Mantenimiento #{mantenimiento.pk} iniciado.')
    return redirect('panel:mantenimiento_detalle', pk=pk)


@login_required
@permission_required('mantenimiento.change_mantenimiento', raise_exception=True)
def mantenimiento_checklist_actualizar(request, pk):
    mantenimiento = get_object_or_404(Mantenimiento, pk=pk)
    verificar_acceso(request.user, mantenimiento.unidad_negocio)
    if request.method == 'POST':
        for item in ActividadChecklist.objects.filter(activo=True):
            realizada = request.POST.get(f'actividad_{item.pk}') == 'on'
            mantenimiento_services.registrar_actividad_checklist(
                mantenimiento=mantenimiento, actividad=item, realizada=realizada, usuario=request.user,
            )
        registrar_evento(
            usuario=request.user, accion='mantenimiento.checklist_actualizar', objeto=mantenimiento, request=request,
        )
        messages.success(request, 'Checklist actualizado.')
    return redirect('panel:mantenimiento_detalle', pk=pk)


@login_required
@permission_required('mantenimiento.change_mantenimiento', raise_exception=True)
def mantenimiento_cerrar(request, pk):
    mantenimiento = get_object_or_404(Mantenimiento, pk=pk)
    verificar_acceso(request.user, mantenimiento.unidad_negocio)
    if request.method == 'POST':
        form = CerrarMantenimientoForm(request.POST)
        if form.is_valid():
            try:
                mantenimiento_services.cerrar_mantenimiento(
                    mantenimiento=mantenimiento, resultado_tecnico=form.cleaned_data['resultado_tecnico'],
                    tiempo_real_minutos=form.cleaned_data['tiempo_real_minutos'],
                    estado_general=form.cleaned_data['estado_general'], usuario=request.user,
                )
            except ValueError as exc:
                form.add_error(None, str(exc))
            else:
                registrar_evento(
                    usuario=request.user, accion='mantenimiento.cerrar', objeto=mantenimiento, request=request,
                )
                messages.success(request, f'Mantenimiento #{mantenimiento.pk} cerrado.')
                return redirect('panel:mantenimiento_detalle', pk=pk)
    else:
        form = CerrarMantenimientoForm()
    return render(request, 'panel/accion_form.html', {
        'form': form, 'titulo': f'Cerrar mantenimiento #{mantenimiento.pk}', 'boton': 'Cerrar mantenimiento',
        'subtitulo': 'Si el resultado es "Requiere baja" o "Irreparable", se marcará baja_recomendada en los '
                     'equipos cubiertos. Si es "Reparado", "Sin falla" u otro resultado exitoso, los equipos que '
                     'este mantenimiento tenía "En reparación" vuelven a bodega con el estado general elegido.',
        **_resumen_mantenimiento(mantenimiento),
        'volver_url': reverse('panel:mantenimiento_detalle', args=[pk]),
    })


@login_required
@permission_required('mantenimiento.change_mantenimiento', raise_exception=True)
def mantenimiento_cancelar(request, pk):
    mantenimiento = get_object_or_404(Mantenimiento, pk=pk)
    verificar_acceso(request.user, mantenimiento.unidad_negocio)
    if request.method == 'POST':
        form = CancelarMantenimientoForm(request.POST)
        if form.is_valid():
            try:
                mantenimiento_services.cancelar_mantenimiento(
                    mantenimiento=mantenimiento, motivo=form.cleaned_data['motivo'], usuario=request.user,
                )
            except ValueError as exc:
                form.add_error(None, str(exc))
            else:
                registrar_evento(
                    usuario=request.user, accion='mantenimiento.cancelar', objeto=mantenimiento, request=request,
                )
                messages.success(request, f'Mantenimiento #{mantenimiento.pk} cancelado.')
                return redirect('panel:mantenimiento_detalle', pk=pk)
    else:
        form = CancelarMantenimientoForm()
    return render(request, 'panel/accion_form.html', {
        'form': form, 'titulo': f'Cancelar mantenimiento #{mantenimiento.pk}', 'boton': 'Cancelar mantenimiento',
        'tono': 'danger',
        **_resumen_mantenimiento(mantenimiento),
        'volver_url': reverse('panel:mantenimiento_detalle', args=[pk]),
    })


@login_required
@permission_required('mantenimiento.view_mantenimientoprogramado', raise_exception=True)
def mantenimientos_programados_lista(request):
    programados = scope_opcional_por_unidad_negocio_activa(
        MantenimientoProgramado.objects.select_related('equipo', 'tecnico'), request, 'equipo__unidad_negocio',
    ).order_by('fecha_proximo')
    return render(request, 'panel/mantenimientos_programados_lista.html', {'programados': programados})


@login_required
@permission_required('mantenimiento.add_mantenimientoprogramado', raise_exception=True)
def mantenimiento_programado_crear(request):
    if request.method == 'POST':
        form = MantenimientoProgramadoForm(request.POST)
        if form.is_valid():
            programado = form.save()
            registrar_evento(
                usuario=request.user, accion='mantenimiento_programado.crear', objeto=programado, request=request,
            )
            messages.success(request, 'Mantenimiento programado creado.')
            return redirect('panel:mantenimientos_programados_lista')
    else:
        form = MantenimientoProgramadoForm()
    return render(request, 'panel/accion_form.html', {
        'form': form, 'titulo': 'Nuevo mantenimiento programado', 'boton': 'Crear plan',
        'volver_url': reverse('panel:mantenimientos_programados_lista'),
    })


@login_required
@permission_required('mantenimiento.change_mantenimiento', raise_exception=True)
def mantenimiento_firmar(request, pk):
    mantenimiento = get_object_or_404(Mantenimiento, pk=pk)
    verificar_acceso(request.user, mantenimiento.unidad_negocio)
    if request.method == 'POST':
        form = FirmaMantenimientoForm(request.POST)
        if form.is_valid():
            mantenimiento_services.firmar_mantenimiento(
                mantenimiento=mantenimiento, tipo_firma=form.cleaned_data['tipo_firma'],
                firma_base64=form.cleaned_data['firma_base64'], usuario=request.user,
                ip_origen=request.META.get('REMOTE_ADDR'),
            )
            registrar_evento(
                usuario=request.user, accion='mantenimiento.firmar', objeto=mantenimiento, request=request,
            )
            messages.success(request, 'Firma registrada.')
            return redirect('panel:mantenimiento_detalle', pk=pk)
    else:
        form = FirmaMantenimientoForm()
    return render(request, 'panel/mantenimiento_firmar.html', {
        'form': form, 'mantenimiento': mantenimiento,
        'volver_url': reverse('panel:mantenimiento_detalle', args=[pk]),
    })


@login_required
@permission_required('mantenimiento.change_mantenimiento', raise_exception=True)
def mantenimiento_imagen_adjuntar(request, pk):
    mantenimiento = get_object_or_404(Mantenimiento, pk=pk)
    verificar_acceso(request.user, mantenimiento.unidad_negocio)
    if request.method == 'POST':
        form = ImagenMantenimientoForm(request.POST, request.FILES)
        if form.is_valid():
            mantenimiento_services.adjuntar_imagen_mantenimiento(
                mantenimiento=mantenimiento, archivo=form.cleaned_data['archivo'], usuario=request.user,
            )
            registrar_evento(
                usuario=request.user, accion='mantenimiento.imagen_adjuntar', objeto=mantenimiento, request=request,
            )
            messages.success(request, 'Imagen adjuntada.')
            return redirect('panel:mantenimiento_detalle', pk=pk)
    else:
        form = ImagenMantenimientoForm()
    return render(request, 'panel/accion_form.html', {
        'form': form, 'titulo': f'Adjuntar imagen a mantenimiento #{mantenimiento.pk}', 'boton': 'Adjuntar imagen',
        **_resumen_mantenimiento(mantenimiento),
        'volver_url': reverse('panel:mantenimiento_detalle', args=[pk]),
    })


@login_required
@permission_required('mantenimiento.change_mantenimiento', raise_exception=True)
def mantenimiento_repuesto_agregar(request, pk):
    mantenimiento = get_object_or_404(Mantenimiento, pk=pk)
    verificar_acceso(request.user, mantenimiento.unidad_negocio)
    if request.method == 'POST':
        form = RepuestoUtilizadoForm(request.POST)
        if form.is_valid():
            d = form.cleaned_data
            try:
                mantenimiento_services.registrar_repuesto_utilizado(
                    mantenimiento=mantenimiento, tipo_consumible=d['tipo_consumible'], cantidad=d['cantidad'],
                    bodega=d['bodega'], costo_unitario=d['costo_unitario'], usuario=request.user,
                )
            except ValueError as exc:
                form.add_error(None, str(exc))
            else:
                registrar_evento(
                    usuario=request.user, accion='mantenimiento.repuesto_agregar', objeto=mantenimiento, request=request,
                )
                messages.success(request, 'Repuesto registrado.')
                return redirect('panel:mantenimiento_detalle', pk=pk)
    else:
        form = RepuestoUtilizadoForm()
    return render(request, 'panel/accion_form.html', {
        'form': form, 'titulo': f'Registrar repuesto en mantenimiento #{mantenimiento.pk}', 'boton': 'Registrar repuesto',
        **_resumen_mantenimiento(mantenimiento),
        'volver_url': reverse('panel:mantenimiento_detalle', args=[pk]),
    })


@login_required
@permission_required('mantenimiento.view_mantenimiento', raise_exception=True)
def mantenimiento_orden_trabajo(request, pk):
    """Orden de trabajo imprimible (Ctrl+P del navegador) — vista rápida sin esperar a la
    generación async del PDF (ver mantenimiento_generar_informe_pdf/informe_pdf)."""
    mantenimiento = get_object_or_404(
        Mantenimiento.objects.select_related('cliente', 'tecnico'), pk=pk,
    )
    verificar_acceso(request.user, mantenimiento.unidad_negocio)
    equipos = mantenimiento.equipos.select_related('equipo')
    checklist_items = ActividadChecklist.objects.filter(activo=True).order_by('orden', 'nombre')
    realizadas = {ar.actividad_id: ar.realizada for ar in mantenimiento.actividades_realizadas.all()}
    checklist = [{'item': item, 'realizada': realizadas.get(item.pk, False)} for item in checklist_items]
    firmas = mantenimiento.firmas.select_related('firmado_por').order_by('tipo_firma')
    imagenes = mantenimiento.imagenes.all()
    repuestos = mantenimiento.repuestos_utilizados.select_related('tipo_consumible', 'bodega')

    return render(request, 'panel/mantenimiento_orden_trabajo.html', {
        'mantenimiento': mantenimiento, 'equipos': equipos, 'checklist': checklist,
        'firmas': firmas, 'imagenes': imagenes, 'repuestos': repuestos,
        'costo_total_repuestos': mantenimiento.costo_total_repuestos,
    })


@login_required
@permission_required('mantenimiento.change_mantenimiento', raise_exception=True)
def mantenimiento_generar_informe_pdf(request, pk):
    """Encola la generación del PDF (apps.mantenimiento.tasks.generar_informe_pdf_task).
    En dev (CELERY_TASK_ALWAYS_EAGER) queda listo antes del redirect; en producción el
    botón "Descargar informe PDF" de la plantilla no aparece hasta que celery_worker
    termine y el campo informe_pdf quede poblado."""
    mantenimiento = get_object_or_404(Mantenimiento, pk=pk)
    verificar_acceso(request.user, mantenimiento.unidad_negocio)
    if request.method == 'POST':
        generar_informe_pdf_task.delay(mantenimiento.pk)
        registrar_evento(
            usuario=request.user, accion='mantenimiento.generar_informe_pdf', objeto=mantenimiento, request=request,
        )
        messages.success(request, 'Generando informe PDF…')
    return redirect('panel:mantenimiento_detalle', pk=pk)


@login_required
@permission_required('mantenimiento.view_actividadplanificada', raise_exception=True)
def actividades_planificadas_lista(request):
    actividades = ActividadPlanificada.objects.filter(activo=True).select_related(
        'tecnico', 'equipo', 'ubicacion',
    ).order_by('fecha_inicio')

    tecnico = request.GET.get('tecnico')
    estado = request.GET.get('estado')
    if tecnico:
        actividades = actividades.filter(tecnico__username=tecnico)
    if estado:
        actividades = actividades.filter(estado=estado)

    return render(request, 'panel/actividades_planificadas_lista.html', {
        'actividades': actividades,
        'estados': ActividadPlanificada.Estado.choices,
        'filtro_tecnico': tecnico or '', 'filtro_estado': estado or '',
    })


@login_required
@permission_required('mantenimiento.add_actividadplanificada', raise_exception=True)
def actividad_planificada_crear(request):
    if request.method == 'POST':
        form = ActividadPlanificadaForm(request.POST)
        if form.is_valid():
            d = form.cleaned_data
            actividad = mantenimiento_services.crear_actividad_planificada(
                tecnico=d['tecnico'], creado_por=request.user, titulo=d['titulo'], descripcion=d['descripcion'],
                tipo_actividad=d['tipo_actividad'], prioridad=d['prioridad'], fecha_inicio=d['fecha_inicio'],
                fecha_fin=d['fecha_fin'], tiempo_estimado_minutos=d['tiempo_estimado_minutos'],
                equipo=d['equipo'], ubicacion=d['ubicacion'],
            )
            registrar_evento(
                usuario=request.user, accion='actividad_planificada.crear', objeto=actividad, request=request,
            )
            messages.success(request, f'Actividad "{actividad.titulo}" creada.')
            return redirect('panel:actividades_planificadas_lista')
    else:
        form = ActividadPlanificadaForm()
    return render(request, 'panel/accion_form.html', {
        'form': form, 'titulo': 'Nueva actividad planificada', 'boton': 'Crear actividad',
        'volver_url': reverse('panel:actividades_planificadas_lista'),
    })


@login_required
@permission_required('mantenimiento.change_actividadplanificada', raise_exception=True)
def actividad_planificada_completar(request, pk):
    actividad = get_object_or_404(ActividadPlanificada, pk=pk)
    if request.method == 'POST':
        form = CompletarActividadForm(request.POST)
        if form.is_valid():
            try:
                mantenimiento_services.completar_actividad_planificada(
                    actividad=actividad, tiempo_real_minutos=form.cleaned_data['tiempo_real_minutos'],
                )
            except ValueError as exc:
                form.add_error(None, str(exc))
            else:
                registrar_evento(
                    usuario=request.user, accion='actividad_planificada.completar', objeto=actividad, request=request,
                )
                messages.success(request, f'Actividad "{actividad.titulo}" completada.')
                return redirect('panel:actividades_planificadas_lista')
    else:
        form = CompletarActividadForm()
    return render(request, 'panel/accion_form.html', {
        'form': form, 'titulo': f'Completar actividad: {actividad.titulo}', 'boton': 'Marcar completada',
        'resumen_tipo': 'mantenimiento', 'resumen_titulo': actividad.titulo,
        'resumen_sub': f'Técnico: {actividad.tecnico}',
        'resumen_campos': [
            ('Prioridad', actividad.get_prioridad_display()), ('Vence', actividad.fecha_fin),
        ],
        'volver_url': reverse('panel:actividades_planificadas_lista'),
    })


@login_required
def notificaciones_lista(request):
    notificaciones = Notificacion.objects.filter(usuario=request.user).order_by('-creado_en')[:100]
    return render(request, 'panel/notificaciones_lista.html', {'notificaciones': notificaciones})


@login_required
def notificacion_marcar_leida(request, pk):
    notificacion = get_object_or_404(Notificacion, pk=pk, usuario=request.user)
    notificacion.leida = True
    notificacion.save(update_fields=['leida'])
    return redirect('panel:notificaciones_lista')
