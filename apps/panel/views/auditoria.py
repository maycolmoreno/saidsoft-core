from django.contrib.auth.decorators import login_required
from django.shortcuts import render

from apps.auditoria.models import EventoAuditoria


@login_required
def auditoria_lista(request):
    eventos = EventoAuditoria.objects.select_related('usuario').order_by('-timestamp')[:200]
    return render(request, 'panel/auditoria_lista.html', {'eventos': eventos})
