from django.contrib import messages
from django.contrib.auth.decorators import login_required, permission_required
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse

from apps.auditoria.models import registrar_evento
from apps.catalogo.models import Estacion
from apps.catalogo.services import generar_comando_instalacion_meshcentral
from apps.cuentas.services import scope_por_unidad_negocio_activa, scope_scripts_visibles_activa, verificar_acceso
from apps.scripts import services as scripts_services
from apps.scripts.forms import EjecutarScriptAdhocForm, EjecutarScriptForm, ScriptForm, ScriptProgramadoForm
from apps.scripts.models import EjecucionScript, Script, ScriptProgramado, TipoScript


@login_required
def scripts_lista(request):
    scripts = scope_scripts_visibles_activa(
        Script.objects.filter(es_adhoc=False), request,
    ).order_by('nombre')
    return render(request, 'panel/scripts_lista.html', {'scripts': scripts})


@login_required
@permission_required('scripts.add_script', raise_exception=True)
def script_crear(request):
    if request.method == 'POST':
        form = ScriptForm(request.POST, user=request.user)
        if form.is_valid():
            script = form.save(commit=False)
            script.creado_por = request.user
            script.save()
            registrar_evento(usuario=request.user, accion='script.crear', objeto=script, request=request)
            messages.success(request, f'Script "{script.nombre}" creado.')
            return redirect('panel:script_detalle', pk=script.pk)
    else:
        form = ScriptForm(user=request.user)
    return render(request, 'panel/accion_form.html', {
        'form': form, 'titulo': 'Nuevo script', 'volver_url': reverse('panel:scripts_lista'),
    })


@login_required
def script_detalle(request, pk):
    script = get_object_or_404(Script, pk=pk)
    verificar_acceso(request.user, script.unidad_negocio)
    ejecuciones = script.ejecuciones.order_by('-fecha_creacion')[:20]
    return render(request, 'panel/script_detalle.html', {'script': script, 'ejecuciones': ejecuciones})


def _crear_ejecucion(request, form, script):
    d = form.cleaned_data
    ejecucion = scripts_services.registrar_ejecucion_script(
        script=script, destino_tipo=d['destino_tipo'], unidad_negocio=d['unidad_negocio'],
        timeout_segundos=d['timeout_segundos'],
        grupos=d['grupos'], farmacias=d['farmacias'], estaciones=d['estaciones'], usuario=request.user,
    )
    registrar_evento(usuario=request.user, accion='script.ejecutar', objeto=script, request=request)
    messages.success(request, f'Ejecución #{ejecucion.pk} lanzada contra {ejecucion.resultados.count()} estación(es).')
    return ejecucion


@login_required
@permission_required('scripts.add_ejecucionscript', raise_exception=True)
def script_ejecutar(request, pk):
    script = get_object_or_404(Script, pk=pk)
    verificar_acceso(request.user, script.unidad_negocio)
    if request.method == 'POST':
        form = EjecutarScriptForm(request.POST, user=request.user)
        if form.is_valid():
            ejecucion = _crear_ejecucion(request, form, script)
            return redirect('panel:ejecucion_detalle', pk=ejecucion.pk)
    else:
        initial = {'unidad_negocio': script.unidad_negocio_id} if script.unidad_negocio_id else {}
        form = EjecutarScriptForm(initial=initial, user=request.user)
    return render(request, 'panel/accion_form.html', {
        'form': form, 'titulo': f'Ejecutar "{script.nombre}"',
        'volver_url': reverse('panel:script_detalle', args=[pk]),
    })


@login_required
@permission_required(('scripts.add_script', 'scripts.add_ejecucionscript'), raise_exception=True)
def script_ejecutar_adhoc(request):
    if request.method == 'POST':
        form = EjecutarScriptAdhocForm(request.POST, user=request.user)
        if form.is_valid():
            d = form.cleaned_data
            script = scripts_services.crear_script_adhoc(
                nombre=d['nombre'], tipo=d['tipo'], contenido=d['contenido'],
                unidad_negocio=d['unidad_negocio'], usuario=request.user,
            )
            ejecucion = _crear_ejecucion(request, form, script)
            return redirect('panel:ejecucion_detalle', pk=ejecucion.pk)
    else:
        initial = {}
        estacion_id = request.GET.get('estacion')
        if estacion_id:
            # Hand-off desde "Instalar agente ahora" en el modal de la estación
            # (acceso remoto MeshCentral) — precarga el one-liner de instalación
            # apuntado a esa única estación, sin tocar registrar_ejecucion_script.
            estacion = get_object_or_404(Estacion, pk=estacion_id)
            verificar_acceso(request.user, estacion.farmacia.unidad_negocio)
            initial = {
                'nombre': f'Instalar agente MeshCentral en {estacion.codigo}',
                'tipo': TipoScript.POWERSHELL,
                'contenido': generar_comando_instalacion_meshcentral(estacion),
                'unidad_negocio': estacion.farmacia.unidad_negocio_id,
                'destino_tipo': EjecucionScript.DestinoTipo.ESTACIONES,
                'estaciones': [estacion.pk],
            }
        form = EjecutarScriptAdhocForm(initial=initial, user=request.user)
    return render(request, 'panel/accion_form.html', {
        'form': form, 'titulo': 'Ejecutar script sin guardar en biblioteca',
        'subtitulo': 'Se guarda igual como un Script auditado (marcado ad-hoc), pero no aparece en la biblioteca.',
        'volver_url': reverse('panel:scripts_lista'),
    })


@login_required
def ejecuciones_lista(request):
    ejecuciones = scope_por_unidad_negocio_activa(
        EjecucionScript.objects.select_related('script', 'creado_por').order_by('-fecha_creacion'),
        request, 'unidad_negocio',
    )
    return render(request, 'panel/ejecuciones_lista.html', {'ejecuciones': ejecuciones})


@login_required
def ejecucion_detalle(request, pk):
    ejecucion = get_object_or_404(EjecucionScript.objects.select_related('script'), pk=pk)
    verificar_acceso(request.user, ejecucion.unidad_negocio)
    return render(request, 'panel/ejecucion_detalle.html', {'ejecucion': ejecucion})


@login_required
def ejecucion_progreso_partial(request, pk):
    ejecucion = get_object_or_404(EjecucionScript, pk=pk)
    verificar_acceso(request.user, ejecucion.unidad_negocio)
    resultados = ejecucion.resultados.select_related('estacion').order_by('estacion__codigo')
    return render(request, 'panel/ejecucion_progreso_partial.html', {
        'ejecucion': ejecucion, 'resultados': resultados,
    })


@login_required
def scripts_programados_lista(request):
    programados = list(scope_por_unidad_negocio_activa(
        ScriptProgramado.objects.select_related('script', 'unidad_negocio').order_by('fecha_proxima_ejecucion'),
        request, 'unidad_negocio',
    ))
    # Última ejecución generada por cada programado, para mostrar ok/con errores sin
    # inventar un modelo de "cumplimiento" nuevo — EjecucionScript ya lo tiene. Sin
    # distinct(programado_id) (no existe en SQLite): se recorre ya ordenado por fecha
    # descendente y se queda con la primera ocurrencia de cada programado.
    ultimas = {}
    for e in EjecucionScript.objects.filter(
        programado__in=programados,
    ).order_by('-fecha_creacion'):
        ultimas.setdefault(e.programado_id, e)
    for p in programados:
        p.ultima_ejecucion = ultimas.get(p.pk)
    return render(request, 'panel/scripts_programados_lista.html', {'programados': programados})


@login_required
@permission_required('scripts.add_scriptprogramado', raise_exception=True)
def script_programado_crear(request):
    if request.method == 'POST':
        form = ScriptProgramadoForm(request.POST, user=request.user)
        if form.is_valid():
            programado = form.save(commit=False)
            programado.creado_por = request.user
            programado.save()
            form.save_m2m()
            registrar_evento(
                usuario=request.user, accion='script_programado.crear', objeto=programado, request=request,
            )
            messages.success(request, f'Programación de "{programado.script.nombre}" creada.')
            return redirect('panel:scripts_programados_lista')
    else:
        form = ScriptProgramadoForm(user=request.user)
    return render(request, 'panel/accion_form.html', {
        'form': form, 'titulo': 'Nueva programación', 'volver_url': reverse('panel:scripts_programados_lista'),
    })
