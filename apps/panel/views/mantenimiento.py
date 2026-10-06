from django.contrib import messages
from django.contrib.auth.decorators import login_required, permission_required
from django.db.models import Q
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST
from apps.activos.models import Activo, Colaborador
from apps.auditoria.models import registrar_evento
from ..paginacion import paginar
from ..busqueda import buscar
from apps.cuentas.services import (
    scope_opcional_por_unidad_negocio, scope_opcional_por_unidad_negocio_activa, verificar_acceso,
)
from apps.mantenimiento import services as mantenimiento_services
from apps.mantenimiento.forms import CancelarMantenimientoForm, CerrarMantenimientoForm, DescartarCierreEnConflictoForm, FirmaMantenimientoForm, ImagenMantenimientoForm, MantenimientoManualForm, MantenimientoProgramadoForm, RepuestoUtilizadoForm
from apps.mantenimiento.models import (
    ActividadChecklist, CierreEnConflicto, Mantenimiento, MantenimientoProgramado, Notificacion,
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
    """Ordena por URGENCIA por defecto, no por fecha (26-sep-2026).

    Antes ordenaba por `-fecha_programada` mientras la app ordenaba por SLA: la misma
    lista en dos ordenes distintos, asi que mesa de ayuda y el tecnico hablaban de "lo
    primero de la lista" mirando cosas distintas. La regla ahora vive una sola vez, en
    `services.ordenar_por_urgencia`.

    El orden por fecha queda en el selector: "que entro hoy" es otra pregunta, y sigue
    siendo legitima.
    """
    mantenimientos = scope_opcional_por_unidad_negocio_activa(
        Mantenimiento.objects.select_related('cliente', 'tecnico'), request, 'cliente__unidad_negocio',
    )

    estado = request.GET.get('estado')
    if estado:
        mantenimientos = mantenimientos.filter(estado_interno=estado)

    # El buscador va ANTES de ordenar, y acá eso no es un detalle: con el orden por
    # urgencia, `ordenar_por_urgencia` trae todo y lo ordena en PYTHON (es un `sorted()`
    # sobre `orden_de_urgencia`). Filtrar antes reduce lo que hay que ordenar; filtrar
    # después no habría reducido nada.
    mantenimientos, busqueda = buscar(mantenimientos, request, (
        'descripcion', 'cliente__nombre', 'cliente__cedula',
        'tecnico__first_name', 'tecnico__last_name', 'tecnico__username',
    ))

    # Los dos órdenes paginan, pero el ORDEN de las operaciones no es el mismo, y esa es
    # justamente la diferencia que este código tenía mal hasta el 5-oct-2026:
    #
    #   - Por FECHA se pagina PRIMERO y se precargan los acuerdos sobre la página. Antes
    #     se hacía al revés, y como `precargar_acuerdos_sla` termina en `list(...)`, el
    #     queryset se materializaba completo: el SQL salía SIN LIMIT y traía la tabla
    #     entera a memoria para mostrar 25 filas. El comentario que estaba acá afirmaba lo
    #     contrario ("por fecha es un queryset, así que la página se trae con LIMIT:
    #     ahorro real") — verificado con 10, 60 y 200 filas, no había LIMIT en ninguno de
    #     los tres modos.
    #   - Por URGENCIA sigue materializando, y es inevitable: `estado_sla` se deriva de la
    #     hora actual y del estado, no es una columna, así que ordenar por eso exige traer
    #     las filas. Paginar acota lo que se RENDERIZA pero no lo que se consulta.
    #
    # Llevar el orden por urgencia a SQL sí ahorraría, pero obligaría a reimplementar
    # `orden_de_urgencia` en el ORM, y esa regla vive en un solo lugar a propósito: en
    # `services`, justo para que el panel y la API muestren el mismo trabajo en el mismo
    # orden (ver el docstring de arriba). Duplicarla para ganar una consulta es cambiar un
    # costo medible por un riesgo peor: que las dos superficies se desincronicen.
    #
    # Precargar sobre la página no cuesta menos que sobre todo: `AcuerdoNivelServicio` son
    # cuatro filas y se cargan una vez igual. Lo que cambia es cuántos mantenimientos se
    # traen.
    orden = 'fecha' if request.GET.get('orden') == 'fecha' else 'urgencia'
    if orden == 'fecha':
        pagina, query_filtros = paginar(mantenimientos.order_by('-fecha_programada'), request)
        en_pagina = mantenimiento_services.precargar_acuerdos_sla(pagina.object_list)
    else:
        pagina, query_filtros = paginar(
            mantenimiento_services.ordenar_por_urgencia(mantenimientos), request,
        )
        en_pagina = pagina.object_list

    return render(request, 'panel/mantenimientos_lista.html', {
        'mantenimientos': en_pagina,
        'pagina': pagina,
        'query_filtros': query_filtros,
        'busqueda': busqueda,
        'busqueda_pista': 'Descripción, cliente, cédula o técnico…',
        'estados': Mantenimiento.EstadoInterno.choices,
        'filtro_estado': estado or '',
        'orden': orden,
    })


@login_required
@permission_required('mantenimiento.add_mantenimiento', raise_exception=True)
def equipos_por_cliente_partial(request):
    """Repuebla las <option> de `equipos` a partir de una búsqueda.

    Antes solo filtraba por el cliente elegido, lo que obligaba a saber DE QUIÉN es el
    equipo para poder cargarle un mantenimiento. Un POS de farmacia no siempre tiene
    custodio, y quien carga suele conocer la farmacia o el número de serie, no a la
    persona.

    La búsqueda cubre lo que alguien puede tener a mano: código o serie del equipo,
    modelo, código o nombre de la farmacia, y nombre o CÉDULA del custodio.
    """
    termino = request.GET.get('buscar', '').strip()
    cliente_id = request.GET.get('cliente')

    equipos = Activo.objects.exclude(estado=Activo.Estado.DADO_DE_BAJA).select_related(
        'farmacia', 'colaborador_actual',
    ).order_by('codigo')
    equipos = scope_opcional_por_unidad_negocio(equipos, request.user, 'unidad_negocio')

    if cliente_id:
        colaborador = get_object_or_404(Colaborador, pk=cliente_id)
        verificar_acceso(request.user, colaborador.unidad_negocio)
        equipos = equipos.filter(colaborador_actual_id=cliente_id)
    if termino:
        equipos = equipos.filter(
            Q(codigo__icontains=termino)
            | Q(numero_serie__icontains=termino)
            | Q(modelo__icontains=termino)
            | Q(farmacia__codigo__icontains=termino)
            | Q(farmacia__nombre__icontains=termino)
            | Q(colaborador_actual__nombre__icontains=termino)
            | Q(colaborador_actual__cedula__icontains=termino),
        )
    elif not cliente_id:
        # Sin criterio no se listan cientos de equipos: el <select> quedaría
        # inmanejable y nadie elige asi.
        equipos = Activo.objects.none()

    return render(request, 'panel/_equipos_por_cliente_options.html', {
        'equipos': equipos[:100],
        'busco': bool(termino or cliente_id),
    })


@login_required
@permission_required('mantenimiento.add_mantenimiento', raise_exception=True)
def mantenimiento_crear(request):
    if request.method == 'POST':
        form = MantenimientoManualForm(request.POST, user=request.user)
        if form.is_valid():
            d = form.cleaned_data
            try:
                mantenimiento = mantenimiento_services.crear_mantenimiento_manual(
                    equipos=list(d['equipos']), cliente=d['cliente'], tecnico=d['tecnico'],
                    tipo_mantenimiento=d['tipo_mantenimiento'], descripcion=d['descripcion'],
                    fecha_programada=d['fecha_programada'], estado_general=d['estado_general'],
                    mantenimiento_programado=d['mantenimiento_programado'], prioridad=d['prioridad'],
                    usuario=request.user,
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
        form = MantenimientoManualForm(user=request.user)
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
    programados, busqueda = buscar(programados, request, (
        'equipo__codigo', 'equipo__numero_serie', 'observaciones', 'tecnico__first_name', 'tecnico__last_name', 'tecnico__username',
    ))
    return render(request, 'panel/mantenimientos_programados_lista.html', {
        'programados': programados,
        'busqueda': busqueda,
        'busqueda_pista': 'Equipo, serie o tecnico...',
    })


@login_required
@permission_required('mantenimiento.add_mantenimientoprogramado', raise_exception=True)
def mantenimiento_programado_crear(request):
    if request.method == 'POST':
        form = MantenimientoProgramadoForm(request.POST, user=request.user)
        if form.is_valid():
            programado = form.save()
            registrar_evento(
                usuario=request.user, accion='mantenimiento_programado.crear', objeto=programado, request=request,
            )
            messages.success(request, 'Mantenimiento programado creado.')
            return redirect('panel:mantenimientos_programados_lista')
    else:
        form = MantenimientoProgramadoForm(user=request.user)
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
def notificaciones_lista(request):
    # Cortaba con `[:100]`: el TERCER corte silencioso de esta familia, despues del
    # `[:200]` de auditoria y el `[:500]` de movimientos de inventario. Siempre el mismo
    # sintoma — pasadas N filas hay datos que existen y no se pueden ver desde ninguna
    # parte, sin aviso. Se reemplaza por paginacion, que acota lo que se trae SIN esconder
    # que hay mas.
    notificaciones = Notificacion.objects.filter(usuario=request.user).order_by('-creado_en')
    notificaciones, busqueda = buscar(notificaciones, request, ('mensaje',))
    pagina, query_filtros = paginar(notificaciones, request)
    return render(request, 'panel/notificaciones_lista.html', {
        'notificaciones': pagina.object_list,
        'pagina': pagina,
        'query_filtros': query_filtros,
        'busqueda': busqueda,
        'busqueda_pista': 'Texto de la notificacion...',
    })


@login_required
def notificacion_marcar_leida(request, pk):
    notificacion = get_object_or_404(Notificacion, pk=pk, usuario=request.user)
    notificacion.leida = True
    notificacion.save(update_fields=['leida'])
    return redirect('panel:notificaciones_lista')
