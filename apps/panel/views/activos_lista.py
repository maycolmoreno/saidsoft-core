"""Inventario de activos (/activos/): la lista con sus tarjetas de resumen.

Aparte de `activos.py` por tamaño (ver DivisionDeVistasTests). Las tarjetas cuentan el
inventario completo y se pulsan para filtrar; la tabla muestra cada equipo con su tipo,
dónde está, su conexión y su garantía.
"""
from datetime import timedelta

from django.contrib.auth.decorators import login_required, permission_required
from django.db.models import Q
from django.shortcuts import render
from django.utils import timezone

from apps.activos.models import Activo, Bodega
from apps.cuentas.services import scope_opcional_por_unidad_negocio, scope_opcional_por_unidad_negocio_activa
from apps.monitoreo.umbrales import ONLINE_UMBRAL_MINUTOS

from ..busqueda import buscar
from ..ficha_estacion import edad
from ..paginacion import paginar

GARANTIA_POR_VENCER_DIAS = 90


def _estado_garantia(vencimiento):
    """`{'texto', 'tono'}` de la garantía: vencida en rojo, por vencer (90 días) en ámbar."""
    if not vencimiento:
        return {'texto': 'Sin dato', 'tono': 'neutral'}
    dias = (vencimiento - timezone.localdate()).days
    if dias < 0:
        return {'texto': f'Vencida hace {-dias} días', 'tono': 'critical'}
    if dias <= GARANTIA_POR_VENCER_DIAS:
        return {'texto': f'Vence en {dias} días', 'tono': 'warning'}
    return {'texto': f'Vigente hasta {vencimiento:%d/%m/%Y}', 'tono': 'good'}


def _vence_hasta(dias):
    return timezone.localdate() + timedelta(days=dias)


# Filtros de garantía de las tarjetas. Funciones y no diccionarios fijos: la fecha de hoy
# se calcula en cada pedido.
_FILTROS_GARANTIA = {
    'vencida': lambda: {'vencimiento_garantia__lt': timezone.localdate()},
    'por_vencer': lambda: {'vencimiento_garantia__gte': timezone.localdate(),
                           'vencimiento_garantia__lte': _vence_hasta(GARANTIA_POR_VENCER_DIAS)},
}


def _resumen_inventario(activos):
    """Conteos por estado, composición por tipo y garantías, en tres consultas agregadas.
    Los dados de baja no cuentan para las garantías: ya no importan."""
    from django.db.models import Count

    por_estado = dict(activos.values_list('estado').annotate(n=Count('id')).values_list('estado', 'n'))
    total = sum(por_estado.values())
    tipos = dict(Activo.Tipo.choices)
    composicion = [
        {'tipo': t, 'nombre': tipos.get(t, t), 'n': n, 'pct': round(100 * n / total, 1) if total else 0}
        for t, n in activos.values_list('tipo').annotate(n=Count('id')).order_by('-n').values_list('tipo', 'n')
    ]
    vigentes = activos.exclude(estado=Activo.Estado.DADO_DE_BAJA)
    return {
        'total': total,
        'estados': [(valor, etiqueta, por_estado.get(valor, 0)) for valor, etiqueta in Activo.Estado.choices],
        'composicion': composicion,
        'garantia_vencida': vigentes.filter(**_FILTROS_GARANTIA['vencida']()).count(),
        'garantia_por_vencer': vigentes.filter(**_FILTROS_GARANTIA['por_vencer']()).count(),
    }


@login_required
@permission_required('activos.view_activo', raise_exception=True)
def activos_lista(request):
    activos = scope_opcional_por_unidad_negocio_activa(
        Activo.objects.select_related('bodega_actual', 'colaborador_actual', 'marca', 'categoria'),
        request, 'unidad_negocio',
    ).order_by('codigo')

    # Conteos sobre el inventario COMPLETO de las unidades del usuario, antes de filtrar:
    # son tarjetas que se pulsan para filtrar, así que "12 en reparación" tiene que seguir
    # diciendo 12 aunque la tabla muestre otra cosa (mismo criterio que estaciones_lista).
    resumen = _resumen_inventario(activos)

    tipo = request.GET.get('tipo')
    estado = request.GET.get('estado')
    garantia = request.GET.get('garantia')
    bodega = request.GET.get('bodega')
    ubicacion = request.GET.get('ubicacion')
    # Texto libre y no un desplegable: son ~700 farmacias, y un <select> con 700 opciones
    # es peor que no tener filtro. Busca por código y también por nombre, porque no todo
    # el mundo se acuerda del código (busca "machala" y encuentra MMAC1).
    farmacia = (request.GET.get('farmacia') or '').strip()
    if tipo:
        activos = activos.filter(tipo=tipo)
    if estado:
        activos = activos.filter(estado=estado)
    if garantia in _FILTROS_GARANTIA:
        activos = activos.filter(**_FILTROS_GARANTIA[garantia]())
    if bodega:
        activos = activos.filter(bodega_actual__codigo=bodega)
    if ubicacion == 'farmacia':
        activos = activos.filter(farmacia__isnull=False)
    elif ubicacion == 'administrativo':
        activos = activos.filter(farmacia__isnull=True)
    if farmacia:
        activos = activos.filter(
            Q(farmacia__codigo__icontains=farmacia) | Q(farmacia__nombre__icontains=farmacia),
        )

    # Búsqueda libre sobre el EQUIPO. Convive con el filtro `farmacia` de arriba y no lo
    # reemplaza: ese pregunta "dónde está" y este "cuál es", y se combinan. Hasta el
    # 4-oct-2026 solo existía el primero, así que con miles de activos no había forma de
    # llegar a uno por su serie o su código SAP desde el panel.
    #
    # `ip` es `inet`: el helper lo detecta y lo convierte solo (ver apps.panel.busqueda).
    activos, busqueda = buscar(activos, request, (
        'codigo', 'numero_serie', 'codigo_sap', 'modelo', 'marca__nombre',
        'mac', 'ip', 'estacion__codigo',
    ))

    # Se pagina con 18 filas en producción y no cuando haya 1.800: el parque son ~1.800
    # estaciones más sus periféricos, y paginar una lista corta es barato — hacerlo sobre
    # una tabla ya grande, no (mismo criterio con el que se paginó estaciones_lista).
    pagina, query_filtros = paginar(
        # `estacion` va en el select_related porque la columna "Puesto / IP" usa
        # `ip_efectiva`, que para un activo vinculado lee `estacion.ip_lan`: sin esto
        # sería una consulta por fila (el mismo N+1 que la auditoría sacó de
        # monitoreo_lista).
        # `estado_red` en el select_related por el mismo motivo que `estacion`: la
        # columna lo consulta por fila y sin esto serían 18 consultas por página.
        activos.select_related('farmacia', 'estacion', 'estado_red'), request,
    )
    ahora = timezone.now()
    for a in pagina.object_list:
        a.garantia = _estado_garantia(a.vencimiento_garantia)
        if a.estacion_id:
            a.latido = edad(a.estacion.ultimo_heartbeat, ahora=ahora, vieja_tras=timedelta(minutes=ONLINE_UMBRAL_MINUTOS))
    return render(request, 'panel/activos_lista.html', {
        'resumen': resumen, 'filtro_garantia': garantia or '',
        'sin_filtro_de_tarjeta': not (estado or garantia or tipo),
        'activos': pagina.object_list,
        'pagina': pagina,
        'query_filtros': query_filtros,
        'tipos': Activo.Tipo.choices,
        'estados': Activo.Estado.choices,
        'bodegas': scope_opcional_por_unidad_negocio(Bodega.objects.all(), request.user, 'unidad_negocio').order_by('codigo'),
        'filtro_tipo': tipo or '', 'filtro_estado': estado or '', 'filtro_bodega': bodega or '',
        'filtro_ubicacion': ubicacion or '', 'filtro_farmacia': farmacia,
        'busqueda': busqueda,
        'busqueda_pista': 'Código, serie, SAP, modelo, marca o IP…',
    })
