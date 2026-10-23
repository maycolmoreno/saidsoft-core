"""Disponibilidad de 24 h de los enlaces de farmacia, sacada de `EventoEnlaceFarmacia`.

Cada caída queda registrada con su inicio y su fin (`fin` vacío = sigue caída), así que
la disponibilidad se calcula exacta, al segundo, sin depender de cuántas muestras haya.
La franja de la tabla son 48 bloques de 30 minutos: un bloque es rojo si hubo caída en
cualquier momento de esa media hora, para que una caída de 3 minutos no desaparezca.

Desde la migración monitoreo/0051 también se guarda cada sondeo (`MuestraEnlaceFarmacia`):
latencia y pérdida de paquetes. Con eso un bloque que NO tuvo caída pero sí pérdida o
latencia muy por encima de lo normal de esa farmacia se pinta "degradado" (ámbar). Es el
caso que el cajero ve como "el POS está lento" y que antes la franja mostraba en verde.
Un bloque sin ningún sondeo guardado se pinta gris ("sin sondeo"), salvo que haya habido
caída: la caída sale de los eventos y vale aunque falten muestras.
"""
from collections import Counter
from datetime import timedelta
from statistics import median
from types import SimpleNamespace

from django.db.models import Q
from django.utils import timezone

BLOQUES = 48
VENTANA = timedelta(hours=24)
PASO = VENTANA / BLOQUES
SEGUNDOS_VENTANA = VENTANA.total_seconds()

# Cuándo un bloque de 30 minutos sin caída cuenta como DEGRADADO. Los sondeos son de 3
# paquetes cada 2 minutos (~15 por bloque), y un paquete suelto perdido es normal en un
# enlace WAN (ver monitoreo.enlaces.PAQUETES_POR_SONDEO), así que no alcanza con uno:
# - pérdida promedio del bloque de 10 % o más (≈ 5 paquetes de 45), o
# - un sondeo que perdió 2 de sus 3 paquetes (sin llegar a perderlos todos), o
# - un pico de latencia de 4 veces la mediana de esa farmacia en el día, y nunca por
#   debajo de 300 ms (un enlace que da 20 ms y salta a 80 no es un problema).
PERDIDA_PROMEDIO_DEGRADADO = 10
PERDIDA_SONDEO_DEGRADADO = 66
FACTOR_LATENCIA_DEGRADADO = 4
LATENCIA_MINIMA_DEGRADADO_MS = 300


def intervalos_24h(farmacia_ids, ahora=None):
    """`{farmacia_id: [(inicio, fin)]}` de las caídas que tocan las últimas 24 h, ya
    recortadas a la ventana. Una consulta para todas."""
    from apps.monitoreo.models import EventoEnlaceFarmacia

    ahora = ahora or timezone.now()
    desde = ahora - VENTANA
    salida = {}
    for farmacia_id, inicio, fin in (
        EventoEnlaceFarmacia.objects
        .filter(farmacia_id__in=farmacia_ids, inicio__lt=ahora)
        .filter(Q(fin__isnull=True) | Q(fin__gt=desde))
        .values_list('farmacia_id', 'inicio', 'fin')
    ):
        salida.setdefault(farmacia_id, []).append((max(inicio, desde), min(fin or ahora, ahora)))
    return salida


def muestras_24h(farmacia_ids, ahora=None):
    """`{farmacia_id: [(timestamp, latencia_ms, latencia_max_ms, perdida_pct)]}` de las
    últimas 24 h, en una consulta. Se usa solo con las farmacias de la página: son ~720
    filas por farmacia."""
    from apps.monitoreo.models import MuestraEnlaceFarmacia

    ahora = ahora or timezone.now()
    salida = {}
    for fila in (
        MuestraEnlaceFarmacia.objects
        .filter(farmacia_id__in=farmacia_ids, timestamp__gte=ahora - VENTANA, timestamp__lte=ahora)
        .order_by('timestamp')
        .values_list('farmacia_id', 'timestamp', 'latencia_ms', 'latencia_max_ms', 'perdida_pct')
    ):
        salida.setdefault(fila[0], []).append(fila[1:])
    return salida


def segundos_caido(intervalos):
    """Suma sin contar dos veces los tramos que se superponen (no debería haberlos, pero
    un evento mal cerrado no puede dar una disponibilidad negativa)."""
    total = 0.0
    fin_previo = None
    for inicio, fin in sorted(intervalos):
        if fin_previo is not None and inicio < fin_previo:
            inicio = fin_previo
        if fin > inicio:
            total += (fin - inicio).total_seconds()
            fin_previo = fin
    return total


def pct_disponible(intervalos):
    return round(100 * (1 - segundos_caido(intervalos) / SEGUNDOS_VENTANA), 1)


def tono_disponibilidad(pct):
    if pct is None:
        return 'neutral'
    if pct < 95:
        return 'critical'
    if pct < 99:
        return 'warning'
    return 'good'


def _pico(muestra):
    _t, latencia, maxima, _p = muestra
    return maxima if maxima is not None else latencia


def _degradado(muestras, umbral_latencia):
    perdidas = [p for *_x, p in muestras if p is not None]
    if perdidas and sum(perdidas) / len(perdidas) >= PERDIDA_PROMEDIO_DEGRADADO:
        return True
    if any(PERDIDA_SONDEO_DEGRADADO <= p < 100 for p in perdidas):
        return True
    picos = [_pico(m) for m in muestras if _pico(m) is not None]
    return bool(umbral_latencia and picos and max(picos) >= umbral_latencia)


def umbral_latencia_de(muestras):
    """El pico que cuenta como "lento" para ESTA farmacia: 4 veces su mediana del día,
    nunca menos de 300 ms. None si no hay latencias con qué comparar."""
    latencias = [m[1] for m in muestras or [] if m[1] is not None]
    if not latencias:
        return None
    return max(LATENCIA_MINIMA_DEGRADADO_MS, FACTOR_LATENCIA_DEGRADADO * median(latencias))


def franja(intervalos, ahora=None, muestras=None):
    """48 bloques del más viejo al más nuevo, cada uno `{'estado', 'titulo'}`.

    `estado`: 'caido' (hubo caída), 'degradado' (sin caída pero con pérdida o lentitud),
    'arriba', o 'sin' (ningún sondeo guardado en ese bloque). Con `muestras=None` (no se
    pasó historial) los bloques sin caída salen 'arriba', como antes de que existiera.
    """
    ahora = ahora or timezone.now()
    desde = ahora - VENTANA
    umbral = umbral_latencia_de(muestras)
    bloques = []
    for i in range(BLOQUES):
        b_ini = desde + PASO * i
        b_fin = b_ini + PASO
        rango = '%s–%s' % (timezone.localtime(b_ini).strftime('%H:%M'), timezone.localtime(b_fin).strftime('%H:%M'))
        if any(inicio < b_fin and fin > b_ini for inicio, fin in intervalos):
            bloques.append({'estado': 'caido', 'titulo': f'{rango} · caído'})
            continue
        if muestras is None:
            bloques.append({'estado': 'arriba', 'titulo': f'{rango} · arriba'})
            continue
        del_bloque = [m for m in muestras if b_ini <= m[0] < b_fin]
        if not del_bloque:
            bloques.append({'estado': 'sin', 'titulo': f'{rango} · sin sondeo'})
            continue
        perdidas = [m[3] for m in del_bloque if m[3] is not None]
        picos = [_pico(m) for m in del_bloque if _pico(m) is not None]
        detalle = []
        if perdidas:
            detalle.append('pérdida %s %%' % round(sum(perdidas) / len(perdidas)))
        if picos:
            detalle.append('pico %s ms' % round(max(picos)))
        estado = 'degradado' if _degradado(del_bloque, umbral) else 'arriba'
        texto = 'con pérdida o lento' if estado == 'degradado' else 'arriba'
        bloques.append({'estado': estado, 'titulo': '%s · %s (%s)' % (rango, texto, ', '.join(detalle))})
    return bloques


def resumen_de_fila(intervalos, sondeada, ahora=None, muestras=None):
    """Lo que la fila de la tabla necesita: franja, % y tono, más la calidad del día
    (pérdida promedio, latencia mediana y pico) cuando hay historial. `sondeada` False
    (nunca sondeada o nunca respondió) da la franja gris y sin porcentaje: decir 100 % de
    un sitio que nunca contestó sería inventar."""
    if not sondeada:
        return {
            'franja': [{'estado': 'sin', 'titulo': 'sin sondeo'}] * BLOQUES, 'pct': None,
            'tono': 'neutral', 'caidas': 0, 'degradados': 0,
            'perdida': None, 'latencia_mediana': None, 'latencia_pico': None,
        }
    pct = pct_disponible(intervalos)
    bloques = franja(intervalos, ahora, muestras)
    con_respuesta = [m for m in muestras or [] if m[1] is not None]
    # La pérdida del día se mide sobre los sondeos que NO fueron caída total: la caída ya
    # cuenta en la disponibilidad, y sumarla acá la contaría dos veces.
    perdidas = [m[3] for m in muestras or [] if m[3] is not None and m[3] < 100]
    return {
        'franja': bloques, 'pct': pct,
        'tono': tono_disponibilidad(pct), 'caidas': len(intervalos),
        'degradados': sum(1 for b in bloques if b['estado'] == 'degradado'),
        'perdida': round(sum(perdidas) / len(perdidas), 1) if perdidas else None,
        'latencia_mediana': round(median(m[1] for m in con_respuesta)) if con_respuesta else None,
        'latencia_pico': round(max(_pico(m) for m in con_respuesta)) if con_respuesta else None,
    }


def caidas_por_proveedor(farmacia_ids, ahora=None):
    """`[(proveedor, n)]` de caídas que EMPEZARON en las últimas 24 h, de más a menos,
    con el ancho de barra relativo al mayor. El proveedor sale de `nombre_proveedor`,
    el mismo criterio que el resto de Monitoreo."""
    from apps.monitoreo.enlaces import nombre_proveedor
    from apps.monitoreo.models import EventoEnlaceFarmacia

    ahora = ahora or timezone.now()
    cuenta = Counter(
        nombre_proveedor(SimpleNamespace(tipo_enlace=tipo))
        for tipo in EventoEnlaceFarmacia.objects.filter(
            farmacia_id__in=farmacia_ids, inicio__gte=ahora - VENTANA,
        ).values_list('farmacia__tipo_enlace', flat=True)
    )
    tope = max(cuenta.values(), default=0)
    return [
        {'nombre': nombre, 'n': n, 'pct': round(100 * n / tope) if tope else 0}
        for nombre, n in cuenta.most_common(6)
    ]
