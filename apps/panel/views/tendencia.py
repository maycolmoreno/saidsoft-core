"""Tendencia de la flota: series semanales de alertas y recursos.

Salio de monitoreo.py, que mezclaba tres dominios. Este es el unico que hace
agregacion historica, y sus ayudantes de reparto por semana no los usa nadie mas.
"""
from datetime import timedelta
from django.contrib.auth.decorators import login_required, permission_required
from django.shortcuts import render
from django.utils import timezone
from apps.catalogo.models import Estacion
from apps.cuentas.services import scope_por_unidad_negocio_activa, unidades_negocio_en_foco
from apps.monitoreo.models import Alerta, MuestraMetrica, ReglaAlerta
from .alertas import _top_mensajes_pos_errores
from ..indicadores import METRICAS_ALERTAS, indicador_de_conteo, indicadores_de_recursos
from ..umbrales import umbrales_de_reglas

# Lo que dice el pie de una tarjeta de recursos cuando la semana en curso todavía no
# tiene ninguna muestra. El gráfico sí sigue mostrando las semanas viejas: lo que no se
# puede es rotular un promedio de hace tres semanas como si fuera el de ahora.
SIN_DATO_SEMANA = 'Sin datos esta semana'


def _semanas_recientes(n=12):
    """Últimas n semanas (lunes a domingo, más vieja primero), incluyendo la semana
    actual (parcial). Buckets calculados a mano en vez de TruncWeek de la ORM — evita
    depender de cómo trunca semanas cada backend (SQLite en dev, Postgres/TimescaleDB
    en producción) por un cálculo que de todas formas es sobre pocos puntos (12)."""
    hoy = timezone.localdate()
    lunes_actual = hoy - timedelta(days=hoy.weekday())
    return [
        (lunes_actual - timedelta(weeks=i), lunes_actual - timedelta(weeks=i - 1))
        for i in range(n - 1, -1, -1)
    ]


def _repartir_en_semanas(semanas, filas, campo_fecha, campos):
    """Reparte filas YA agregadas por día en los buckets semanales de `semanas`.

    Los buckets siguen siendo los de `_semanas_recientes`, calculados a mano: no se usa
    `TruncWeek` a propósito, por lo mismo que explica su docstring — el corte de semana
    depende del backend (SQLite en dev, Postgres/TimescaleDB en producción) y acá
    queremos el mismo lunes en los dos. Lo que cambia es de dónde salen los números: una
    sola consulta agrupada por día, en vez de una consulta por semana.

    Devuelve una lista paralela a `semanas` con los `campos` sumados.
    """
    acumulado = [{c: 0 for c in campos} for _ in semanas]
    for fila in filas:
        fecha = fila[campo_fecha]
        if fecha is None:
            continue
        for bucket, (inicio, fin) in zip(acumulado, semanas):
            if inicio <= fecha < fin:
                for c in campos:
                    bucket[c] += fila[c] or 0
                break
    return acumulado


def _promedio(suma, cantidad):
    """`Avg` reconstruido desde suma y cantidad: son exactamente eso, y así se puede
    agregar por día y recomponer por semana sin cambiar el número."""
    return suma / cantidad if cantidad else None


def _series_semanales(semanas, alertas, metricas):
    """Las siete series de `tendencia_flota`, en 3 consultas fijas.

    Separada de la vista para poder compararla contra el cálculo semana por semana sin
    pasar por HTTP: `Grafico` no expone la serie cruda, así que desde el contexto de la
    respuesta solo se podía verificar el último valor.
    """
    from django.db.models import Count, Sum

    desde, hasta = semanas[0][0], semanas[-1][1]

    filas_abiertas = list(
        alertas.filter(abierta_en__date__gte=desde, abierta_en__date__lt=hasta)
        .values('abierta_en__date', 'regla__severidad')
        .annotate(n=Count('id')),
    )
    filas_resueltas = list(
        alertas.filter(resuelta_en__date__gte=desde, resuelta_en__date__lt=hasta)
        .values('resuelta_en__date')
        .annotate(n=Count('id')),
    )
    filas_metricas = list(
        metricas.filter(timestamp__date__gte=desde, timestamp__date__lt=hasta)
        .values('timestamp__date')
        .annotate(
            cpu_suma=Sum('cpu_carga_pct'), cpu_n=Count('cpu_carga_pct'),
            ram_total_suma=Sum('ram_total'), ram_total_n=Count('ram_total'),
            ram_usada_suma=Sum('ram_usada'), ram_usada_n=Count('ram_usada'),
            disco_total_suma=Sum('disco_total_gb'), disco_total_n=Count('disco_total_gb'),
            disco_libre_suma=Sum('disco_libre_gb'), disco_libre_n=Count('disco_libre_gb'),
            red_rx_suma=Sum('red_recibido_kbps'), red_rx_n=Count('red_recibido_kbps'),
            red_tx_suma=Sum('red_enviado_kbps'), red_tx_n=Count('red_enviado_kbps'),
        ),
    )

    severidad = ReglaAlerta.Severidad
    por_semana_warning = _repartir_en_semanas(
        semanas, [f for f in filas_abiertas if f['regla__severidad'] == severidad.WARNING],
        'abierta_en__date', ('n',),
    )
    por_semana_critical = _repartir_en_semanas(
        semanas, [f for f in filas_abiertas if f['regla__severidad'] == severidad.CRITICAL],
        'abierta_en__date', ('n',),
    )
    por_semana_resueltas = _repartir_en_semanas(semanas, filas_resueltas, 'resuelta_en__date', ('n',))
    por_semana_metricas = _repartir_en_semanas(
        semanas, filas_metricas, 'timestamp__date',
        ('cpu_suma', 'cpu_n', 'ram_total_suma', 'ram_total_n', 'ram_usada_suma', 'ram_usada_n',
         'disco_total_suma', 'disco_total_n', 'disco_libre_suma', 'disco_libre_n',
         'red_rx_suma', 'red_rx_n', 'red_tx_suma', 'red_tx_n'),
    )

    abiertas_warning = [b['n'] for b in por_semana_warning]
    abiertas_critical = [b['n'] for b in por_semana_critical]
    resueltas = [b['n'] for b in por_semana_resueltas]

    cpu_prom, ram_prom, disco_prom, red_prom = [], [], [], []
    for b in por_semana_metricas:
        cpu_prom.append(_promedio(b['cpu_suma'], b['cpu_n']))

        # Promedio de la flota = razón de promedios (avg(ram_usada)/avg(ram_total)), no
        # promedio de razones por muestra — evita traer cada MuestraMetrica a Python
        # para calcular su .ram_usada_pct/.disco_usado_pct fila por fila (inviable a
        # ~1.900 estaciones × muestras cada 5-10 min sobre 12 semanas).
        ram_total = _promedio(b['ram_total_suma'], b['ram_total_n'])
        ram_usada = _promedio(b['ram_usada_suma'], b['ram_usada_n'])
        ram_prom.append(100 * ram_usada / ram_total if ram_total else None)

        disco_total = _promedio(b['disco_total_suma'], b['disco_total_n'])
        disco_libre = _promedio(b['disco_libre_suma'], b['disco_libre_n'])
        disco_prom.append(
            100 * (disco_total - disco_libre) / disco_total if disco_total else None
        )

        # Red no necesita esa razón (ya es una tasa, no un % derivado de dos cantidades)
        # — promedio directo de los kbps ya calculados por el agente.
        red_rx = _promedio(b['red_rx_suma'], b['red_rx_n'])
        red_tx = _promedio(b['red_tx_suma'], b['red_tx_n'])
        red_prom.append(None if red_rx is None and red_tx is None else round((red_rx or 0) + (red_tx or 0), 1))

    return {
        'abiertas_warning': abiertas_warning, 'abiertas_critical': abiertas_critical,
        'resueltas': resueltas, 'cpu': cpu_prom, 'ram': ram_prom,
        'disco': disco_prom, 'red': red_prom,
    }


@login_required
@permission_required('monitoreo.view_alerta', raise_exception=True)
def tendencia_flota(request):
    """M5 del roadmap de monitoreo: series semanales a nivel de flota (no por
    estación) — alertas abiertas/resueltas por severidad y promedio de CPU/RAM/disco
    de los servidores monitoreados. Los errores del POS se muestran como "top actual"
    (mismo dato que pos_errores_flota), no como tendencia: PosErrorDetectado solo
    guarda un contador acumulado de por vida, no hay con qué armar una serie semanal
    real sin agregar un modelo nuevo — decisión explícita del usuario, ver
    PLAN_MODERNIZACION.md.

    Consultas: 3 para las series, constantes. Antes eran 4 por semana (tres `.count()`
    y un `.aggregate()` dentro del bucle de 12) = **48**, y crecían con la ventana: pasar
    a 26 semanas las habría duplicado. Ahora los datos vienen agrupados por día en una
    consulta por serie y se reparten en semanas en Python, sobre a lo sumo 84 filas.

    Los promedios se recomponen desde suma y cantidad en vez de promediar promedios
    diarios: `Avg` ES suma/cantidad, así que sumar las dos partes por semana da el mismo
    número exacto que el `Avg` semanal de antes. Promediar los promedios de cada día
    daría distinto cuando los días tienen distinta cantidad de muestras.
    """
    semanas = _semanas_recientes(12)

    alertas = scope_por_unidad_negocio_activa(
        Alerta.objects.all(), request, 'estacion__farmacia__unidad_negocio',
    )
    servidores = scope_por_unidad_negocio_activa(
        Estacion.objects.filter(monitorear_recursos=True), request, 'farmacia__unidad_negocio',
    )
    metricas = MuestraMetrica.objects.filter(estacion__in=servidores)

    series = _series_semanales(semanas, alertas, metricas)
    abiertas_warning = series['abiertas_warning']
    abiertas_critical = series['abiertas_critical']
    resueltas = series['resueltas']
    cpu_prom, ram_prom = series['cpu'], series['ram']
    disco_prom, red_prom = series['disco'], series['red']

    filas_pos, _detectados = _top_mensajes_pos_errores(request)

    # Mismos umbrales que el detalle de una estación: la línea que se dibuja sobre el
    # promedio de la flota tiene que ser la misma que abre la alerta en cada equipo. Con
    # varias unidades de negocio a la vista se toma la más estricta (ver
    # `umbrales.umbrales_de_reglas`).
    reglas = umbrales_de_reglas(unidades_negocio_en_foco(request))

    series_recursos = {
        'cpu_carga_pct': cpu_prom, 'ram_usada_pct': ram_prom,
        'disco_usado_pct': disco_prom, 'red_total_kbps': red_prom,
    }
    # `valores` explícito y no el último punto del gráfico: `Grafico.ultimo_valor` es el
    # último valor NO NULO de la serie —puede venir de una semana vieja si la actual
    # todavía no tiene muestras— y la tarjeta lo rotula como el de ahora.
    valores = {clave: serie[-1] for clave, serie in series_recursos.items()}
    valores['latencia_ms'] = None
    notas = {clave: SIN_DATO_SEMANA for clave, v in valores.items() if v is None}

    indicadores_recursos = [
        i for i in indicadores_de_recursos(
            series_recursos, reglas, valores=valores, notas=notas,
            etiquetas={
                'cpu_carga_pct': 'CPU promedio', 'ram_usada_pct': 'RAM usada promedio',
                'disco_usado_pct': 'Disco usado promedio', 'red_total_kbps': 'Red promedio',
            },
        )
        # La latencia no se agrega por semana en `_series_semanales`: no hay serie que
        # mostrar, y una tarjeta vacía en la fila se lee como un dato que falta.
        if i.clave != 'latencia_ms'
    ]

    series_alertas = {
        'abiertas_warning': abiertas_warning,
        'abiertas_critical': abiertas_critical,
        'resueltas': resueltas,
    }
    return render(request, 'panel/tendencia_flota.html', {
        'indicadores_alertas': [
            indicador_de_conteo(m, series_alertas[m.clave], 'media 12 sem')
            for m in METRICAS_ALERTAS
        ],
        'indicadores_recursos': indicadores_recursos,
        'total_abiertas_periodo': sum(abiertas_warning) + sum(abiertas_critical),
        'total_resueltas_periodo': sum(resueltas),
        'semana_desde': semanas[0][0],
        'semana_hasta': semanas[-1][1] - timedelta(days=1),
        'top_pos_errores': filas_pos[:5],
    })
