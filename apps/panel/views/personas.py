"""Colaboradores y visitas tecnicas.

Salio de activos.py, que habia llegado a 852 lineas y 28 vistas mezclando cuatro
dominios. Esto no son activos: son las personas a las que se les asigna un activo
y las visitas tecnicas que se les hacen.
"""
from django.contrib import messages
from django.contrib.auth.decorators import login_required, permission_required
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST
from apps.activos.forms import ColaboradorForm
from apps.activos.models import Colaborador
from apps.auditoria.models import registrar_evento
from apps.mantenimiento import services as mantenimiento_services
from apps.mantenimiento.forms import VisitaTecnicaForm
from apps.mantenimiento.models import VisitaTecnica
from apps.cuentas.services import scope_opcional_por_unidad_negocio_activa, verificar_acceso


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
