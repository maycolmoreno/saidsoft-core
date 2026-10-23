"""Disponibilidad de 24 h de los enlaces de farmacia, sacada de `EventoEnlaceFarmacia`.

Cada caída queda registrada con su inicio y su fin (`fin` vacío = sigue caída), así que
la disponibilidad se calcula exacta, al segundo, sin depender de cuántas muestras haya.
La franja de la tabla son 48 bloques de 30 minutos: un bloque es rojo si hubo caída en
cualquier momento de esa media hora, para que una caída de 3 minutos no desaparezca.

Lo que NO se puede saber con este dato es si el sondeo estuvo andando: no se guarda un
historial de sondeos, solo las caídas. Por eso "sin sondeo" (gris) se usa para la
farmacia entera cuando nunca se sondeó o nunca respondió, no por bloque.
"""
from collections import Counter
from datetime import timedelta
from types import SimpleNamespace

from django.db.models import Q
from django.utils import timezone

BLOQUES = 48
VENTANA = timedelta(hours=24)
PASO = VENTANA / BLOQUES
SEGUNDOS_VENTANA = VENTANA.total_seconds()


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


def franja(intervalos, ahora=None):
    """Lista de 48 estados 'arriba' / 'caido', del más viejo al más nuevo."""
    ahora = ahora or timezone.now()
    desde = ahora - VENTANA
    bloques = []
    for i in range(BLOQUES):
        b_ini = desde + PASO * i
        b_fin = b_ini + PASO
        caido = any(inicio < b_fin and fin > b_ini for inicio, fin in intervalos)
        bloques.append('caido' if caido else 'arriba')
    return bloques


def resumen_de_fila(intervalos, sondeada, ahora=None):
    """Lo que la fila de la tabla necesita: franja, % y tono. `sondeada` False (nunca
    sondeada o nunca respondió) da la franja gris y sin porcentaje: decir 100 % de un
    sitio que nunca contestó sería inventar."""
    if not sondeada:
        return {'franja': ['sin'] * BLOQUES, 'pct': None, 'tono': 'neutral', 'caidas': 0}
    pct = pct_disponible(intervalos)
    return {
        'franja': franja(intervalos, ahora), 'pct': pct,
        'tono': tono_disponibilidad(pct), 'caidas': len(intervalos),
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
