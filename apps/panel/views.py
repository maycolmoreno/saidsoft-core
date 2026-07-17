from datetime import timedelta

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Count, F, Q
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

from apps.auditoria.models import EventoAuditoria, registrar_evento
from apps.catalogo.models import Estacion, Grupo
from apps.despliegues.models import Despliegue, ResultadoDespliegue
from apps.despliegues.services import publicar_despliegue

from .forms import DespliegueForm

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
        Despliegue.objects.select_related('creado_por', 'aprobado_por').prefetch_related('grupos', 'farmacias'),
        pk=pk,
    )
    puede_aprobar = (
        despliegue.estado == Despliegue.Estado.PENDIENTE_APROBACION
        and despliegue.creado_por_id != request.user.id
    )
    return render(request, 'panel/despliegue_detalle.html', {
        'despliegue': despliegue,
        'puede_aprobar': puede_aprobar,
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
