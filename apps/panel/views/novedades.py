"""Bandeja de la mesa de ayuda: lo que reportaron o no contestaron las farmacias por WhatsApp.

Es lo mismo que llega al chat de Telegram de la mesa (`apps.whatsapp.mesa`), con los mismos
botones: tomar el caso, registrar la causa que se averiguó llamando, cerrar. Ver
PLAN_MODERNIZACION.md §10-CM.
"""
from django.contrib.auth.decorators import login_required, permission_required
from django.db.models import Case, Value, When
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from apps.auditoria.models import registrar_evento
from apps.cuentas.services import scope_por_unidad_negocio_activa, verificar_acceso
from apps.panel.paginacion import paginar
from apps.whatsapp.models import Novedad
from apps.whatsapp.servicios import aplicar_respuesta, cerrar, tomar

FILTROS = {
    'mesa': (Novedad.Estado.ESCALADA, Novedad.Estado.EN_ATENCION),
    'esperando': (Novedad.Estado.ESPERANDO,),
    'registradas': (Novedad.Estado.REGISTRADA,),
    'cerradas': (Novedad.Estado.CERRADA,),
}


@login_required
@permission_required('whatsapp.view_novedad', raise_exception=True)
def novedades_lista(request):
    filtro = request.GET.get('ver', 'mesa')
    if filtro not in FILTROS:
        filtro = 'mesa'
    base = scope_por_unidad_negocio_activa(
        Novedad.objects.select_related('farmacia', 'farmacia__tecnico_asignado', 'evento_enlace', 'estacion',
                                       'tomado_por'),
        request, 'farmacia__unidad_negocio',
    )
    conteos = {clave: base.filter(estado__in=estados).count() for clave, estados in FILTROS.items()}
    # Lo que espera a la mesa: primero lo que nadie tomó, y lo más viejo arriba. Con un
    # `Case` y no `order_by('estado')`: alfabéticamente 'en_atencion' va antes que
    # 'escalada', así que lo ya tomado quedaba arriba (auditoría 17-oct-2026, BD-11).
    if filtro == 'mesa':
        orden = (
            Case(When(estado=Novedad.Estado.ESCALADA, then=Value(0)), default=Value(1)),
            'escalado_en',
        )
    else:
        orden = ('-creado_en',)
    pagina, query_filtros = paginar(base.filter(estado__in=FILTROS[filtro]).order_by(*orden), request)
    return render(request, 'panel/novedades_lista.html', {
        'pagina': pagina, 'query_filtros': query_filtros, 'filtro': filtro, 'conteos': conteos,
    })


@login_required
@permission_required('whatsapp.change_novedad', raise_exception=True)
@require_POST
def novedad_accion(request, pk):
    novedad = get_object_or_404(Novedad.objects.select_related('farmacia'), pk=pk)
    verificar_acceso(request.user, novedad.farmacia.unidad_negocio)
    accion = request.POST.get('accion', '')
    quien = request.user.get_username()
    if accion == 'tomar':
        tomar(novedad, usuario=request.user, quien=quien)
    elif accion in ('luz', 'internet'):
        causa = Novedad.Causa.LUZ if accion == 'luz' else Novedad.Causa.INTERNET
        aplicar_respuesta(novedad, causa, por_mesa=True, usuario=request.user, quien=quien)
    elif accion == 'cerrar':
        cerrar(novedad, (request.POST.get('nota') or f'Cerrado por {quien}.')[:200])
    else:
        return redirect('panel:novedades_lista')  # acción desconocida: no se hace nada
    registrar_evento(usuario=request.user, accion=f'novedad.{accion}', objeto=novedad, request=request)
    # Vuelve a la misma pestaña de la bandeja. Solo un nombre de filtro conocido, nunca una
    # URL que venga en el formulario (eso sería una redirección abierta).
    ver = request.POST.get('ver', 'mesa')
    return redirect(f"{reverse('panel:novedades_lista')}?ver={ver if ver in FILTROS else 'mesa'}")
