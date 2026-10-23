"""Búsqueda rápida del panel (Ctrl K).

Hasta el 23-oct-2026 la barra superior mostraba "Buscar estación, activo, script… Ctrl K"
pero era solo un texto: no buscaba nada. Esto la hace real: un término y aparecen, en
grupos, las estaciones, farmacias, activos y despliegues que coinciden, más las páginas
del panel a las que se puede saltar.

Respeta lo mismo que las listas: cada grupo exige el permiso de ver su modelo y queda
acotado a las unidades de negocio del usuario. Una página aparece solo si el usuario
puede entrar.
"""
from django.contrib.auth.decorators import login_required
from django.db.models import Q
from django.shortcuts import render
from django.urls import reverse

from apps.cuentas.services import scope_opcional_por_unidad_negocio, scope_por_unidad_negocio

LIMITE_POR_GRUPO = 6
MINIMO_CARACTERES = 2

# (nombre, url, permiso necesario o None). Lo que más se usa, no todo el menú.
PAGINAS = [
    ('Tablero', 'panel:dashboard', None),
    ('Estaciones', 'panel:estaciones_lista', 'catalogo.view_estacion'),
    ('Actualizar agente', 'panel:estaciones_agente', 'catalogo.actualizar_agente_estacion'),
    ('Despliegues del POS', 'panel:despliegues_lista', 'despliegues.view_despliegue'),
    ('Instalaciones de software', 'panel:solicitudes_instalacion_lista', 'software.view_solicitudinstalacion'),
    ('Centro de monitoreo', 'panel:centro_monitoreo', 'monitoreo.view_alerta'),
    ('Alertas', 'panel:alertas_lista', 'monitoreo.view_alerta'),
    ('Enlaces de farmacias', 'panel:enlaces_farmacias_lista', 'monitoreo.view_estadoenlacefarmacia'),
    ('Activos', 'panel:activos_lista', 'activos.view_activo'),
    ('Registrar activo', 'panel:activo_crear', 'activos.add_activo'),
    ('Scripts', 'panel:scripts_lista', 'scripts.view_script'),
    ('Reportes', 'panel:reportes_index', None),
    ('Verificación en dos pasos', 'panel:mfa_estado', None),
]


def _paginas(user, termino):
    t = termino.lower()
    return [
        {'titulo': nombre, 'url': reverse(url)}
        for nombre, url, permiso in PAGINAS
        if t in nombre.lower() and (permiso is None or user.has_perm(permiso))
    ][:LIMITE_POR_GRUPO]


def resultados(user, termino):
    """`[(grupo, [{'titulo', 'detalle', 'url'}])]`, solo los grupos con algo."""
    from apps.activos.models import Activo
    from apps.catalogo.models import Estacion, Farmacia
    from apps.despliegues.models import Despliegue

    grupos = []
    if user.has_perm('catalogo.view_estacion'):
        estaciones = scope_por_unidad_negocio(
            Estacion.objects.select_related('farmacia'), user, 'farmacia__unidad_negocio',
        ).filter(Q(codigo__icontains=termino) | Q(hostname__icontains=termino) | Q(numero_serie__icontains=termino))
        grupos.append(('Estaciones', [
            {'titulo': e.codigo, 'detalle': f'Farmacia {e.farmacia.codigo} · {e.get_estado_conexion_display().lower()}',
             'url': reverse('panel:estacion_ficha', args=[e.pk])}
            for e in estaciones.order_by('codigo')[:LIMITE_POR_GRUPO]
        ]))
        farmacias = scope_por_unidad_negocio(Farmacia.objects.all(), user, 'unidad_negocio').filter(
            Q(codigo__icontains=termino) | Q(nombre__icontains=termino),
        )
        grupos.append(('Farmacias', [
            {'titulo': f.codigo, 'detalle': f.nombre or 'Ver sus estaciones',
             'url': reverse('panel:estaciones_lista') + f'?q={f.codigo}'}
            for f in farmacias.order_by('codigo')[:LIMITE_POR_GRUPO]
        ]))
    if user.has_perm('activos.view_activo'):
        activos = scope_opcional_por_unidad_negocio(Activo.objects.all(), user, 'unidad_negocio').filter(
            Q(codigo__icontains=termino) | Q(numero_serie__icontains=termino) | Q(modelo__icontains=termino),
        )
        grupos.append(('Activos', [
            {'titulo': a.codigo, 'detalle': f'{a.get_tipo_display()} · {a.modelo or a.numero_serie or ""}'.strip(' ·'),
             'url': reverse('panel:activo_detalle', args=[a.pk])}
            for a in activos.order_by('codigo')[:LIMITE_POR_GRUPO]
        ]))
    if user.has_perm('despliegues.view_despliegue'):
        despliegues = scope_por_unidad_negocio(Despliegue.objects.all(), user, 'unidad_negocio').filter(
            Q(version__icontains=termino) | Q(descripcion__icontains=termino),
        )
        grupos.append(('Despliegues', [
            {'titulo': f'POS {d.version}', 'detalle': f'#{d.pk} · {d.get_estado_display().lower()}',
             'url': reverse('panel:despliegue_detalle', args=[d.pk])}
            for d in despliegues.order_by('-fecha_creacion')[:LIMITE_POR_GRUPO]
        ]))
    grupos.append(('Ir a', _paginas(user, termino)))
    return [(nombre, filas) for nombre, filas in grupos if filas]


@login_required
def paleta_resultados(request):
    termino = (request.GET.get('q') or '').strip()
    grupos = resultados(request.user, termino) if len(termino) >= MINIMO_CARACTERES else []
    return render(request, 'panel/_paleta_resultados.html', {
        'grupos': grupos, 'termino': termino, 'corto': len(termino) < MINIMO_CARACTERES,
    })
