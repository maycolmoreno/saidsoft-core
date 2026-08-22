from django.contrib.auth.decorators import login_required, permission_required
from django.shortcuts import render

from apps.auditoria.models import EventoAuditoria
from apps.cuentas.services import scope_opcional_por_unidad_negocio_activa


@login_required
@permission_required('auditoria.view_eventoauditoria', raise_exception=True)
def auditoria_lista(request):
    eventos = scope_opcional_por_unidad_negocio_activa(
        EventoAuditoria.objects.select_related('usuario'), request, 'unidad_negocio',
    ).order_by('-timestamp')[:200]
    return render(request, 'panel/auditoria_lista.html', {'eventos': eventos})
