"""Equipos que intentaron enrolarse y el servidor rechazó.

Módulo propio y no dentro de `views/estaciones.py`, que ya estaba en 491 líneas y el
límite que fija `DivisionDeVistasTests` son 550.

Por qué existe esta pantalla: el rechazo por "el sitio del código no existe" solo
quedaba en el log del worker MQTT. El síntoma para quien instala es de los peores que
hay — se instala el agente, el técnico se va, y la estación **nunca aparece**, sin nada
que diga por qué. Con códigos escritos a mano en cada PC, eso no es una posibilidad
remota: es cuestión de tiempo.
"""
from django.contrib import messages
from django.contrib.auth.decorators import login_required, permission_required
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from apps.auditoria.models import registrar_evento
from apps.catalogo.models import Farmacia
from apps.mqtt_worker.models import EnrolamientoRechazado


@login_required
@permission_required('catalogo.view_estacion', raise_exception=True)
def enrolamientos_rechazados_lista(request):
    """Bandeja de triage: qué equipos no logran entrar, y con qué código.

    Por defecto solo los SIN revisar: es una bandeja de trabajo, no un historial. Los
    revisados quedan con `?todos=1`.

    NO se filtra por unidad de negocio, y es a propósito: un enrolamiento rechazado por
    definición **no tiene sitio**, así que no tiene unidad a la que pertenecer. Filtrarlo
    lo escondería de todos.
    """
    rechazados = EnrolamientoRechazado.objects.select_related('revisado_por')
    ver_todos = request.GET.get('todos') == '1'
    if not ver_todos:
        rechazados = rechazados.filter(revisado=False)

    rechazados = list(rechazados)
    # Se resuelve acá y no en la plantilla: "¿el sitio existe?" es LA pregunta de esta
    # pantalla, y hacerla por fila desde el template serían N consultas.
    codigos = {r.sitio_sugerido for r in rechazados if r.sitio_sugerido}
    existentes = set(
        Farmacia.objects.filter(codigo__in=codigos).values_list('codigo', flat=True),
    )
    for rechazado in rechazados:
        rechazado.sitio_existe = rechazado.sitio_sugerido in existentes

    return render(request, 'panel/enrolamientos_rechazados_lista.html', {
        'rechazados': rechazados,
        'ver_todos': ver_todos,
        'sin_revisar': EnrolamientoRechazado.objects.filter(revisado=False).count(),
    })


@login_required
@permission_required('catalogo.aprobar_estacion', raise_exception=True)
@require_POST
def enrolamiento_rechazado_revisar(request, pk):
    """Marca el intento como atendido.

    `aprobar_estacion` y no `view_estacion`: resolver esto termina casi siempre en crear
    un sitio o reinstalar el agente con otro código, que es intervención — mismo criterio
    que separa a Mesa de Ayuda de Soporte Técnico en el resto del proyecto.

    No se borra la fila: si el equipo sigue mal configurado va a reintentar, y el worker
    la vuelve a marcar sin revisar (ver `_registrar_enrolamiento_rechazado`). Esa vuelta
    es la que distingue "lo miré" de "lo resolví".
    """
    rechazado = get_object_or_404(EnrolamientoRechazado, pk=pk)
    EnrolamientoRechazado.objects.filter(pk=rechazado.pk).update(
        revisado=True, revisado_por=request.user, revisado_en=timezone.now(),
    )
    registrar_evento(
        usuario=request.user, accion='enrolamiento_rechazado.revisar', objeto=rechazado,
        request=request,
    )
    messages.success(
        request,
        f'Marcado como revisado: {rechazado.codigo_recibido}. Si el equipo sigue '
        'insistiendo con el mismo código, va a volver a aparecer acá.',
    )
    return redirect('panel:enrolamientos_rechazados_lista')
