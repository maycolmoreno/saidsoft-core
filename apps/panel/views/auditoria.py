from django.contrib.auth.decorators import login_required, permission_required
from django.shortcuts import render

from apps.auditoria.models import EventoAuditoria

from ..paginacion import paginar
from apps.cuentas.services import scope_opcional_por_unidad_negocio_activa


@login_required
@permission_required('auditoria.view_eventoauditoria', raise_exception=True)
def auditoria_lista(request):
    # Antes esto cortaba con `[:200]`. No era una optimización sino una pérdida: al
    # llegar a 257 eventos había 57 que existían y no se podían ver desde ninguna parte
    # de la aplicación, sin aviso. Un registro de auditoría que esconde filas en silencio
    # deja de servir como registro — y es la única tabla que solo crece.
    eventos = scope_opcional_por_unidad_negocio_activa(
        EventoAuditoria.objects.select_related('usuario'), request, 'unidad_negocio',
    ).order_by('-timestamp')
    pagina, query_filtros = paginar(eventos, request)
    return render(request, 'panel/auditoria_lista.html', {
        # `eventos` sigue siendo el nombre que usa la plantilla: ahora es la página
        # actual en vez del queryset recortado.
        'eventos': pagina.object_list,
        'pagina': pagina,
        'query_filtros': query_filtros,
    })
