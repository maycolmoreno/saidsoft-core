"""Series de 24 h, 7 días y 30 días para las tarjetas de `monitoreo_lista`.

Las tarjetas muestran CPU, memoria y disco a lo largo del rango elegido, con la línea de
umbral encima. Traer las muestras crudas no es opción: con equipos midiendo cada minuto,
30 días de una página de 25 son ~1 millón de filas. Por eso la base agrupa por minuto
(24 h) o por hora (7 y 30 días) y devuelve sumas y conteos; acá solo se reparten en los
cubos del rango.

Se agrupa con `timestamp__date` / `__hour` / `__minute` y no con `time_bucket` de
TimescaleDB para que funcione igual sin la extensión (SQLite en desarrollo), y porque
Django resuelve esos lookups con la zona horaria del proyecto en todos los motores.
"""
from dataclasses import dataclass
from datetime import datetime, time, timedelta

from django.db.models import Count, Max, Sum
from django.utils import timezone


@dataclass(frozen=True)
class Rango:
    clave: str
    etiqueta: str
    cubos: int
    paso: timedelta
    agrupar: str  # 'minuto' u 'hora': con qué resolución agrupa la base antes de repartir
    descripcion: str


# Cuántos puntos tiene cada gráfica. Con un punto por hora en 24 h, un equipo que empezó a
# reportar hace 40 minutos mostraba un solo punto y ninguna curva (ML001-C, 23-oct-2026):
# por eso 24 h va cada 10 minutos. 7 y 30 días agrupan por hora en la base, que es lo que
# mantiene acotadas las filas que viajan (168 y 720 por equipo como máximo).
RANGOS = {
    '24h': Rango('24h', '24 h', 144, timedelta(minutes=10), 'minuto', 'Últimas 24 horas · un punto cada 10 minutos'),
    '7d': Rango('7d', '7 días', 168, timedelta(hours=1), 'hora', 'Últimos 7 días · un punto por hora'),
    '30d': Rango('30d', '30 días', 120, timedelta(hours=6), 'hora', 'Últimos 30 días · un punto cada 6 horas'),
}
RANGO_POR_DEFECTO = '24h'


def rango_de(request):
    return RANGOS.get(request.GET.get('rango'), RANGOS[RANGO_POR_DEFECTO])


def _inicio(rango, ahora):
    """Inicio del primer cubo, alineado al paso en hora local. El último cubo contiene el
    momento actual."""
    local = timezone.localtime(ahora)
    dia = timezone.make_aware(datetime.combine(local.date(), time()))
    transcurrido = local - dia
    actual = dia + (transcurrido // rango.paso) * rango.paso
    return actual + rango.paso - rango.paso * rango.cubos


def series_en_rango(ids, rango, ahora=None):
    """`{estacion_id: {'cpu_carga_pct', 'ram_usada_pct', 'disco_usado_pct': [..],
    'cpu_pico': float|None}}` en UNA consulta para toda la página.

    Cada punto es el promedio del cubo (`None` si el cubo no tiene muestras: es un
    hueco, no un cero). `cpu_pico` es el máximo real del rango, no el máximo de los
    promedios: un pico de 5 minutos al 100 % no se ve en el promedio de la hora.
    """
    from apps.monitoreo.models import MuestraMetrica

    ids = list(ids)
    if not ids:
        return {}
    ahora = ahora or timezone.now()
    inicio = _inicio(rango, ahora)
    campos = ['estacion_id', 'timestamp__date', 'timestamp__hour']
    if rango.agrupar == 'minuto':
        campos.append('timestamp__minute')
    filas = (
        MuestraMetrica.objects
        .filter(estacion_id__in=ids, timestamp__gte=inicio)
        .values(*campos)
        .annotate(
            cpu_s=Sum('cpu_carga_pct'), cpu_n=Count('cpu_carga_pct'), cpu_max=Max('cpu_carga_pct'),
            ram_u=Sum('ram_usada'), ram_t=Sum('ram_total'),
            disco_t=Sum('disco_total_gb'), disco_l=Sum('disco_libre_gb'),
        )
        .order_by()
    )

    acumulado = {}
    for f in filas:
        inicio_fila = timezone.make_aware(datetime.combine(
            f['timestamp__date'], time(hour=f['timestamp__hour'], minute=f.get('timestamp__minute', 0)),
        ))
        indice = (inicio_fila - inicio) // rango.paso
        if not 0 <= indice < rango.cubos:
            continue
        cubos = acumulado.setdefault(f['estacion_id'], [None] * rango.cubos)
        c = cubos[indice] or {'cpu_s': 0, 'cpu_n': 0, 'cpu_max': None, 'ram_u': 0,
                              'ram_t': 0, 'disco_t': 0, 'disco_l': 0}
        c['cpu_s'] += f['cpu_s'] or 0
        c['cpu_n'] += f['cpu_n'] or 0
        if f['cpu_max'] is not None:
            c['cpu_max'] = f['cpu_max'] if c['cpu_max'] is None else max(c['cpu_max'], f['cpu_max'])
        for k in ('ram_u', 'ram_t', 'disco_t', 'disco_l'):
            c[k] += f[k] or 0
        cubos[indice] = c

    salida = {}
    for estacion_id, cubos in acumulado.items():
        picos = [c['cpu_max'] for c in cubos if c and c['cpu_max'] is not None]
        salida[estacion_id] = {
            'cpu_carga_pct': [round(c['cpu_s'] / c['cpu_n'], 1) if c and c['cpu_n'] else None for c in cubos],
            'ram_usada_pct': [round(100 * c['ram_u'] / c['ram_t'], 1) if c and c['ram_t'] else None for c in cubos],
            'disco_usado_pct': [
                round(100 * (c['disco_t'] - c['disco_l']) / c['disco_t'], 1) if c and c['disco_t'] else None
                for c in cubos
            ],
            'cpu_pico': max(picos) if picos else None,
        }
    return salida


# Lo que dice la tarjeta según la peor de sus tres métricas. Mismo vocabulario que el
# distintivo de estado, más corto para el encabezado de la tarjeta.
ESTADO_TARJETA = {
    'critical': ('Sobre el umbral', 'critical'),
    'warning': ('Atención', 'warning'),
    'ok': ('Normal', 'good'),
}


def estado_de_tarjeta(indicadores):
    estados = {i.estado for i in indicadores}
    for clave in ('critical', 'warning', 'ok'):
        if clave in estados:
            texto, tono = ESTADO_TARJETA[clave]
            return {'texto': texto, 'tono': tono, 'clave': clave}
    return {'texto': 'Sin datos', 'tono': 'neutral', 'clave': 'sin_dato'}
