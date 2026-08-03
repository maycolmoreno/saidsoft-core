from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from apps.auditoria.models import registrar_evento
from apps.cuentas.services import (
    scope_opcional_por_unidad_negocio_activa, scope_por_unidad_negocio_activa, verificar_acceso,
)
from apps.software.forms import AplicacionCatalogoForm, SolicitudInstalacionForm, VersionAplicacionForm
from apps.software.models import AplicacionCatalogo, EstadoSolicitud, ResultadoInstalacion, SolicitudInstalacion
from apps.software.services import publicar_solicitud


@login_required
def aplicaciones_lista(request):
    aplicaciones = scope_opcional_por_unidad_negocio_activa(
        AplicacionCatalogo.objects.filter(activo=True).prefetch_related('versiones'), request, 'unidad_negocio',
    ).order_by('nombre')
    return render(request, 'panel/aplicaciones_lista.html', {'aplicaciones': aplicaciones})


@login_required
def aplicacion_crear(request):
    if request.method == 'POST':
        form = AplicacionCatalogoForm(request.POST, user=request.user)
        if form.is_valid():
            aplicacion = form.save(commit=False)
            aplicacion.creado_por = request.user
            aplicacion.save()
            registrar_evento(usuario=request.user, accion='aplicacion_catalogo.crear', objeto=aplicacion, request=request)
            messages.success(request, f'Aplicación "{aplicacion.nombre}" agregada al catálogo.')
            return redirect('panel:aplicacion_detalle', pk=aplicacion.pk)
    else:
        form = AplicacionCatalogoForm(user=request.user)
    return render(request, 'panel/accion_form.html', {
        'form': form, 'titulo': 'Nueva aplicación de catálogo', 'volver_url': reverse('panel:aplicaciones_lista'),
    })


@login_required
def aplicacion_detalle(request, pk):
    aplicacion = get_object_or_404(AplicacionCatalogo, pk=pk)
    verificar_acceso(request.user, aplicacion.unidad_negocio)
    versiones = aplicacion.versiones.order_by('-fecha_publicacion')
    return render(request, 'panel/aplicacion_detalle.html', {'aplicacion': aplicacion, 'versiones': versiones})


@login_required
def version_crear(request, pk):
    aplicacion = get_object_or_404(AplicacionCatalogo, pk=pk)
    verificar_acceso(request.user, aplicacion.unidad_negocio)
    if request.method == 'POST':
        form = VersionAplicacionForm(request.POST, request.FILES)
        if form.is_valid():
            version = form.save(commit=False)
            version.aplicacion = aplicacion
            version.save()
            registrar_evento(
                usuario=request.user, accion='version_aplicacion.crear', objeto=version, request=request,
            )
            messages.success(request, f'Versión {version.version} agregada.')
            return redirect('panel:aplicacion_detalle', pk=aplicacion.pk)
    else:
        form = VersionAplicacionForm()
    return render(request, 'panel/accion_form.html', {
        'form': form, 'titulo': f'Nueva versión de {aplicacion.nombre}',
        'volver_url': reverse('panel:aplicacion_detalle', args=[pk]),
    })


@login_required
def solicitudes_instalacion_lista(request):
    solicitudes = scope_por_unidad_negocio_activa(
        SolicitudInstalacion.objects.select_related('version_aplicacion__aplicacion', 'creado_por')
        .order_by('-fecha_creacion'),
        request, 'unidad_negocio',
    )
    return render(request, 'panel/solicitudes_instalacion_lista.html', {'solicitudes': solicitudes})


@login_required
def solicitud_instalacion_crear(request):
    if request.method == 'POST':
        form = SolicitudInstalacionForm(request.POST, user=request.user)
        if form.is_valid():
            d = form.cleaned_data
            solicitud = SolicitudInstalacion.objects.create(
                version_aplicacion=d['version_aplicacion'], accion=d['accion'], unidad_negocio=d['unidad_negocio'],
                destino_tipo=d['destino_tipo'], creado_por=request.user,
            )
            solicitud.grupos.set(d['grupos'])
            solicitud.farmacias.set(d['farmacias'])
            solicitud.estaciones.set(d['estaciones'])
            registrar_evento(
                usuario=request.user, accion='solicitud_instalacion.crear', objeto=solicitud, request=request,
            )
            messages.success(request, 'Solicitud creada. Publícala cuando quieras que se distribuya.')
            return redirect('panel:solicitud_instalacion_detalle', pk=solicitud.pk)
    else:
        form = SolicitudInstalacionForm(user=request.user)
    return render(request, 'panel/accion_form.html', {
        'form': form, 'titulo': 'Nueva solicitud de instalación',
        'subtitulo': 'No se envía nada todavía — queda en borrador hasta que la publiques desde su ficha.',
        'volver_url': reverse('panel:solicitudes_instalacion_lista'),
    })


@login_required
def solicitud_instalacion_detalle(request, pk):
    solicitud = get_object_or_404(
        SolicitudInstalacion.objects.select_related('version_aplicacion__aplicacion', 'creado_por')
        .prefetch_related('grupos', 'farmacias'),
        pk=pk,
    )
    verificar_acceso(request.user, solicitud.unidad_negocio)
    return render(request, 'panel/solicitud_instalacion_detalle.html', {'solicitud': solicitud})


@login_required
def solicitud_instalacion_progreso_partial(request, pk):
    solicitud = get_object_or_404(SolicitudInstalacion, pk=pk)
    verificar_acceso(request.user, solicitud.unidad_negocio)
    resultados = solicitud.resultados.select_related('estacion', 'estacion__farmacia').order_by('estacion__codigo')

    total = resultados.count()
    conteo = {estado: 0 for estado, _ in ResultadoInstalacion.Estado.choices}
    for r in resultados:
        conteo[r.estado] += 1
    instalados = conteo.get(ResultadoInstalacion.Estado.INSTALADO, 0)
    errores = conteo.get(ResultadoInstalacion.Estado.ERROR, 0)
    pct_completado = round(100 * instalados / total) if total else 0

    return render(request, 'panel/solicitud_instalacion_progreso_partial.html', {
        'solicitud': solicitud, 'resultados': resultados, 'total': total,
        'instalados': instalados, 'errores': errores, 'pct_completado': pct_completado, 'conteo': conteo,
    })


@login_required
@require_POST
def solicitud_instalacion_publicar(request, pk):
    solicitud = get_object_or_404(SolicitudInstalacion, pk=pk)
    verificar_acceso(request.user, solicitud.unidad_negocio)
    if solicitud.estado != EstadoSolicitud.BORRADOR:
        messages.error(request, 'Esta solicitud ya no está en borrador.')
    else:
        resultado = publicar_solicitud(solicitud)
        registrar_evento(
            usuario=request.user, accion='solicitud_instalacion.publicar', objeto=solicitud,
            detalle={'estaciones_destino': resultado.total_estaciones, 'exitoso': resultado.exitoso},
            request=request,
        )
        if resultado.exitoso:
            messages.success(request, f'Solicitud publicada a {resultado.total_estaciones} estación(es).')
        else:
            messages.error(
                request,
                'No se pudo publicar: falló la conexión con el broker MQTT. La solicitud sigue '
                'en borrador — puede reintentar cuando el broker esté disponible.',
            )
    return redirect('panel:solicitud_instalacion_detalle', pk=pk)
