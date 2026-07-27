from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse

from apps.auditoria.models import registrar_evento
from apps.scripts import services as scripts_services
from apps.scripts.forms import EjecutarScriptAdhocForm, EjecutarScriptForm, ScriptForm
from apps.scripts.models import EjecucionScript, Script


@login_required
def scripts_lista(request):
    scripts = Script.objects.filter(es_adhoc=False).order_by('nombre')
    return render(request, 'panel/scripts_lista.html', {'scripts': scripts})


@login_required
def script_crear(request):
    if request.method == 'POST':
        form = ScriptForm(request.POST)
        if form.is_valid():
            script = form.save(commit=False)
            script.creado_por = request.user
            script.save()
            registrar_evento(usuario=request.user, accion='script.crear', objeto=script, request=request)
            messages.success(request, f'Script "{script.nombre}" creado.')
            return redirect('panel:script_detalle', pk=script.pk)
    else:
        form = ScriptForm()
    return render(request, 'panel/accion_form.html', {
        'form': form, 'titulo': 'Nuevo script', 'volver_url': reverse('panel:scripts_lista'),
    })


@login_required
def script_detalle(request, pk):
    script = get_object_or_404(Script, pk=pk)
    ejecuciones = script.ejecuciones.order_by('-fecha_creacion')[:20]
    return render(request, 'panel/script_detalle.html', {'script': script, 'ejecuciones': ejecuciones})


def _crear_ejecucion(request, form, script):
    d = form.cleaned_data
    ejecucion = scripts_services.registrar_ejecucion_script(
        script=script, destino_tipo=d['destino_tipo'], timeout_segundos=d['timeout_segundos'],
        grupos=d['grupos'], farmacias=d['farmacias'], estaciones=d['estaciones'], usuario=request.user,
    )
    registrar_evento(usuario=request.user, accion='script.ejecutar', objeto=script, request=request)
    messages.success(request, f'Ejecución #{ejecucion.pk} lanzada contra {ejecucion.resultados.count()} estación(es).')
    return ejecucion


@login_required
def script_ejecutar(request, pk):
    script = get_object_or_404(Script, pk=pk)
    if request.method == 'POST':
        form = EjecutarScriptForm(request.POST)
        if form.is_valid():
            ejecucion = _crear_ejecucion(request, form, script)
            return redirect('panel:ejecucion_detalle', pk=ejecucion.pk)
    else:
        form = EjecutarScriptForm()
    return render(request, 'panel/accion_form.html', {
        'form': form, 'titulo': f'Ejecutar "{script.nombre}"',
        'volver_url': reverse('panel:script_detalle', args=[pk]),
    })


@login_required
def script_ejecutar_adhoc(request):
    if request.method == 'POST':
        form = EjecutarScriptAdhocForm(request.POST)
        if form.is_valid():
            d = form.cleaned_data
            script = scripts_services.crear_script_adhoc(
                nombre=d['nombre'], tipo=d['tipo'], contenido=d['contenido'], usuario=request.user,
            )
            ejecucion = _crear_ejecucion(request, form, script)
            return redirect('panel:ejecucion_detalle', pk=ejecucion.pk)
    else:
        form = EjecutarScriptAdhocForm()
    return render(request, 'panel/accion_form.html', {
        'form': form, 'titulo': 'Ejecutar script sin guardar en biblioteca',
        'subtitulo': 'Se guarda igual como un Script auditado (marcado ad-hoc), pero no aparece en la biblioteca.',
        'volver_url': reverse('panel:scripts_lista'),
    })


@login_required
def ejecuciones_lista(request):
    ejecuciones = EjecucionScript.objects.select_related('script', 'creado_por').order_by('-fecha_creacion')
    return render(request, 'panel/ejecuciones_lista.html', {'ejecuciones': ejecuciones})


@login_required
def ejecucion_detalle(request, pk):
    ejecucion = get_object_or_404(EjecucionScript.objects.select_related('script'), pk=pk)
    return render(request, 'panel/ejecucion_detalle.html', {'ejecucion': ejecucion})


@login_required
def ejecucion_progreso_partial(request, pk):
    ejecucion = get_object_or_404(EjecucionScript, pk=pk)
    resultados = ejecucion.resultados.select_related('estacion').order_by('estacion__codigo')
    return render(request, 'panel/ejecucion_progreso_partial.html', {
        'ejecucion': ejecucion, 'resultados': resultados,
    })
