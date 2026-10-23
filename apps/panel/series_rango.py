"""Series de 24 h, 7 días y 30 días para las tarjetas de `monitoreo_lista`.

Las tarjetas muestran CPU, memoria y disco a lo largo del rango elegido, con la línea de
umbral encima. Traer las muestras crudas no es opción: con el agente reportando cada
~30 s, 30 días de una página de 25 servidores son ~2 millones de filas. Por eso la
base agrupa por hora (o por día) y devuelve sumas y conteos; acá solo se reparten en
los cubos del rango.

Se agrupa con `timestamp__date` / `timestamp__hour` y no con `time_bucket` de
TimescaleDB porque las pruebas corren sobre SQLite, y Django resuelve esos dos lookups
con la zona horaria del proyecto en los dos motores.
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
    por_dia: bool  # True: la base agrupa por día; False: por hora
    descripcion: str


RANGOS = {
    '24h': Rango('24h', '24 h', 24, timedelta(hours=1), False, 'Últimas 24 horas · un punto por hora'),
    '7d': Rango('7d', '7 días', 42, timedelta(hours=4), False, 'Últimos 7 días · un punto cada 4 horas'),
    '30d': Rango('30d', '30 días', 30, timedelta(days=1), True, 'Últimos 30 días · un punto por día'),
}
RANGO_POR_DEFECTO = '24h'


def rango_de(request):
    return RANGOS.get(request.GET.get('rango'), RANGOS[RANGO_POR_DEFECTO])


def _inicio(rango, ahora):
    """Inicio del primer cubo. El último cubo contiene el momento actual."""
    local = timezone.localtime(ahora)
    if rango.por_dia:
        hoy = timezone.make_aware(datetime.combine(local.date(), time()))
        return hoy - rango.paso * (rango.cubos - 1)
    hora = local.replace(minute=0, second=0, microsecond=0)
    return hora + timedelta(hours=1) - rango.paso * rango.cubos


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
    campos = ['estacion_id', 'timestamp__date'] + ([] if rango.por_dia else ['timestamp__hour'])
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
            f['timestamp__date'], time(hour=0 if rango.por_dia else f['timestamp__hour']),
        ))
        indice = int((inicio_fila - inicio) / rango.paso)
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
