"""Un "indicador" = etiqueta + valor actual + estado + umbral dibujado + sparkline.

Es el patrón único con el que el panel muestra una métrica, y existe para que las
gráficas se entiendan **de un vistazo**: sin abrir el detalle y sin pasar el mouse.
Antes cada pantalla lo armaba a mano y cada una comunicaba otra cosa — `monitoreo_lista`
mostraba cinco números crudos sin decir si estaban bien, `monitoreo_detalle_partial`
dibujaba RAM en verde y latencia en ámbar *siempre* (el color distinguía la serie, no el
estado: una RAM al 95% se veía verde), y `tendencia_flota` escondía el valor actual en
una línea de 11 px debajo del gráfico.

Lo que aporta cada indicador, y por qué:

- **Valor grande y coloreado por estado.** El estado sale de `umbrales.clasificar`, con
  los límites que resuelve `umbrales.limites` — o sea, de la ReglaAlerta activa. No hay
  un segundo criterio de color.
- **Marca de umbral sobre el gráfico.** La línea se dibuja donde está el límite real,
  así que la distancia entre la curva y la línea *es* el margen que queda. Sin eso hay
  que saber de memoria cuánto es mucho.
- **Flecha de tendencia con palabra.** "mejorando"/"empeorando" y no solo una flecha:
  si subir es bueno o malo depende de la métrica, y el lector no tiene por qué deducirlo.
- **Sparkline de contexto reciente.** Distingue un pico puntual de una meseta, que es la
  diferencia entre "se resuelve solo" y "mandá a alguien".

Los colores de estado son reservados: acá solo se usan para el estado, nunca para
distinguir una serie de otra (la curva va siempre en `--serie`, un gris azulado que no
es ninguno de los tres), y siempre van acompañados de icono y texto.
"""
from dataclasses import dataclass
from datetime import timedelta

from django.utils.formats import number_format

from apps.monitoreo.graficos import construir_grafico

from .umbrales import (
    UMBRAL_CPU_CRITICAL_PCT, UMBRAL_CPU_WARNING_PCT, UMBRAL_DISCO_CRITICAL_PCT,
    UMBRAL_DISCO_WARNING_PCT, UMBRAL_RAM_CRITICAL_PCT, UMBRAL_RAM_WARNING_PCT,
    clasificar, limites,
)

# Centinela: `valores={'cpu_carga_pct': None}` significa "esta semana no hay dato", que
# es distinto de "no me pasaron un valor explícito, usá el último de la serie".
SIN_VALOR = object()


@dataclass(frozen=True)
class Metrica:
    """Metadatos de presentación: cómo se llama, en qué unidad y con qué eje se dibuja.

    Los umbrales NO están acá: salen de `_umbrales_por_defecto` y de la ReglaAlerta
    activa, para que no haya una segunda copia del número.

    `mejor_si` no es cosmético: decide si una flecha para arriba dice "mejorando" o
    "empeorando". Para CPU es 'baja'; para "alertas resueltas por semana", 'sube'.
    """

    clave: str
    etiqueta: str
    unidad: str
    decimales: int = 0
    escala_fija: float | None = None
    mejor_si: str = 'baja'


# Las cinco métricas de recursos que reporta el agente, en el orden en que se leen.
#
# Un decimal, que es la precisión con la que el panel ya las mostraba: el rediseño
# cambia cómo se comunica el número, no el número. (El valor se imprime con
# `floatformat`, así que respeta la coma decimal de es-EC.)
METRICAS_RECURSOS = (
    Metrica('cpu_carga_pct', 'CPU', '%', 1, 100),
    Metrica('ram_usada_pct', 'RAM usada', '%', 1, 100),
    Metrica('disco_usado_pct', 'Disco usado', '%', 1, 100),
    Metrica('latencia_ms', 'Latencia', ' ms', 1),
    Metrica('red_total_kbps', 'Red', ' kbps', 1),
)


def _umbrales_por_defecto(clave):
    """El `(warning, critical)` que se usa cuando NO hay una ReglaAlerta para la métrica.

    Se arma al llamar y no una sola vez al importar: así `umbrales.py` sigue siendo el
    único lugar donde los números están escritos, y una prueba puede sustituirlos sin
    tener que reconstruir la tabla de métricas.

    Latencia y red devuelven `(None, None)` a propósito. CPU/RAM/disco tienen defecto
    porque ya existía uno acordado; para esas dos no hay ningún número que alguien haya
    decidido, e inventarlo haría que la tarjeta afirmara "esto está bien" sobre una
    comparación que nadie hizo. Sin regla se muestran sin color y sin línea, y la
    tarjeta dice por qué.
    """
    return {
        'cpu_carga_pct': (UMBRAL_CPU_WARNING_PCT, UMBRAL_CPU_CRITICAL_PCT),
        'ram_usada_pct': (UMBRAL_RAM_WARNING_PCT, UMBRAL_RAM_CRITICAL_PCT),
        'disco_usado_pct': (UMBRAL_DISCO_WARNING_PCT, UMBRAL_DISCO_CRITICAL_PCT),
    }.get(clave, (None, None))


def estado_de(clave, valor, reglas=None):
    """El estado de un valor suelto, por el MISMO camino que usa el gráfico.

    Existe para las pantallas que muestran el número sin serie (las tarjetas del
    listado ya resueltas, los colores heredados de `estados_de_recursos`): que el color
    de un número y el color del punto de su curva no puedan salir de dos cálculos
    distintos es justamente lo que se quiso garantizar.
    """
    warning, critical = limites(reglas or {}, clave, *_umbrales_por_defecto(clave))
    return clasificar(valor, warning, critical)


# Flecha + palabra. La palabra importa más que la flecha: "▲" al lado de "alertas
# resueltas" y al lado de "disco usado" significan lo contrario.
_FLECHA = {'sube': '▲', 'baja': '▼', 'estable': '→', 'sin_dato': ''}
_SENTIDO = {
    ('sube', 'baja'): 'peor', ('baja', 'baja'): 'mejor',
    ('sube', 'sube'): 'mejor', ('baja', 'sube'): 'peor',
}
_PALABRA = {'mejor': 'mejorando', 'peor': 'empeorando'}


@dataclass
class Indicador:
    """Lo que el template necesita para pintar una tarjeta, ya resuelto."""

    clave: str
    etiqueta: str
    valor: float | None
    unidad: str
    decimales: int
    estado: str
    grafico: object
    warning: float | None
    critical: float | None
    flecha: str = ''
    tendencia_texto: str = ''
    sentido: str = ''
    nota: str = ''
    # Métricas que NO son medidores: un conteo de alertas por semana no tiene "umbral",
    # así que la tarjeta no muestra distintivo de estado ni colorea el número. Fingir un
    # estado sobre un conteo sería inventar un juicio que nadie definió.
    mostrar_estado: bool = True
    # El umbral configurado queda fuera del eje del gráfico (ej. una regla de latencia
    # en 500 ms sobre una ventana que no pasa de 40): la línea no se dibuja y la nota
    # lo aclara, en vez de pegarla al borde y hacerla mentir.
    umbral_fuera_de_escala: bool = False


def _texto_tendencia(grafico, metrica):
    if grafico is None or grafico.tendencia in ('sin_dato', ''):
        return '', '', ''
    if grafico.tendencia == 'estable':
        return _FLECHA['estable'], 'estable', 'igual'
    sentido = _SENTIDO[(grafico.tendencia, metrica.mejor_si)]
    return _FLECHA[grafico.tendencia], _PALABRA[sentido], sentido


def _etiqueta_umbral(valor, metrica):
    """El número que se dibuja al lado de la línea de umbral.

    Sin los decimales de la métrica: los umbrales son redondos (75, 90, 7000) y la
    etiqueta mide 9 px — "75,0%" ocupa más y no dice nada más. Los que sí tienen parte
    decimal (una regla en 7,5) la conservan, localizada como el resto del panel.
    """
    if valor is None:
        return ''
    redondo = float(valor).is_integer()
    return number_format(int(valor) if redondo else valor) + metrica.unidad


def indicador(metrica, serie, reglas=None, ancho=280, alto=64, valor=SIN_VALOR,
              nota='', referencias=(), limites_fijos=None, mostrar_estado=True,
              dibujar_umbrales=True):
    """Arma un `Indicador` a partir de la serie cronológica de una métrica.

    `serie`: valores de más viejo a más nuevo (los `None` son huecos, no ceros).
    `reglas`: lo que devuelve `umbrales.umbrales_de_reglas()`.
    `valor`: el valor "de ahora" cuando no es el último no nulo de la serie — pasa en
    `tendencia_flota`, donde la semana en curso puede no tener muestras todavía y el
    último no nulo sería de hace tres semanas, rotulado como si fuera de hoy.
    `referencias`: marcas extra que no son umbrales, ej. el promedio del período.
    `limites_fijos`: `(warning, critical)` ya resueltos, para las métricas cuyo umbral
    no sale de ReglaAlerta sino del propio objeto (el % del ancho contratado de una
    farmacia, que es distinto en cada una).
    `mostrar_estado`: False para conteos (alertas por semana), que no tienen umbral.
    `dibujar_umbrales`: False cuando el límite existe pero el EJE no permite dibujarlo
    con honestidad. Pasa en el consumo de una farmacia sin ancho contratado: el eje es
    el pico de la ventana —una escala distinta en cada farmacia—, así que una línea en
    8.000 kbps aplastaría la curva y no significaría lo mismo dos veces seguidas. El
    color del número sí sigue saliendo de ese límite, que es lo único que hay.
    """
    if limites_fijos is not None:
        warning, critical = limites_fijos
    else:
        warning, critical = limites(
            reglas or {}, metrica.clave, *_umbrales_por_defecto(metrica.clave),
        )
    marcas = [*referencias]
    if dibujar_umbrales:
        marcas[:0] = [
            (warning, 'warning', _etiqueta_umbral(warning, metrica)),
            (critical, 'critical', _etiqueta_umbral(critical, metrica)),
        ]
    grafico = construir_grafico(
        serie, ancho=ancho, alto=alto, escala_fija=metrica.escala_fija, marcas=marcas,
    )
    actual = grafico.ultimo_valor if valor is SIN_VALOR else valor
    flecha, texto, sentido = _texto_tendencia(grafico, metrica)
    return Indicador(
        clave=metrica.clave, etiqueta=metrica.etiqueta, valor=actual,
        unidad=metrica.unidad, decimales=metrica.decimales,
        estado=clasificar(actual, warning, critical),
        grafico=grafico, warning=warning, critical=critical,
        flecha=flecha, tendencia_texto=texto, sentido=sentido, nota=nota,
        mostrar_estado=mostrar_estado,
        umbral_fuera_de_escala=any(not m.dentro for m in grafico.marcas),
    )


# Las tres series semanales de alertas de `tendencia_flota`. No son medidores: son
# conteos de eventos, y no existe un número acordado de "alertas críticas por semana"
# que esté bien. Lo que sí se puede decir es si esta semana está por encima o por debajo
# de lo habitual, y eso es lo que dibuja la línea de referencia.
METRICAS_ALERTAS = (
    Metrica('abiertas_warning', 'Abiertas · Advertencia', ''),
    Metrica('abiertas_critical', 'Abiertas · Crítica', ''),
    # Única de las tres donde subir es bueno: por eso la flecha necesita `mejor_si`.
    Metrica('resueltas', 'Resueltas', '', mejor_si='sube'),
)


def indicador_de_conteo(metrica, serie, etiqueta_referencia='media del período'):
    """Un conteo por período: sin umbral, con el promedio del período como referencia.

    Sustituye al sparkline pelado que tenía `tendencia_flota`, donde "Resueltas" se
    dibujaba en verde (el color decía "bien" aunque la serie estuviera cayendo) y el
    valor de la semana en curso vivía en una línea de 11 px debajo del gráfico.
    """
    limpios = [v for v in serie if v is not None]
    media = round(sum(limpios) / len(limpios), 1) if limpios else None
    referencias = []
    if media is not None:
        referencias.append((media, 'referencia', f'{etiqueta_referencia}: {media:g}'))
    return indicador(
        metrica, serie, referencias=referencias, mostrar_estado=False,
        limites_fijos=(None, None),
    )


def indicadores_de_recursos(series, reglas, ancho=280, alto=64, valores=None,
                            notas=None, etiquetas=None):
    """Los cinco indicadores de recursos de una estación (o de la flota).

    `series`: `{clave: [valores]}`. Una sola función para la lista, el detalle y la
    tendencia: si el criterio de color o el umbral dibujado cambian, cambian en las tres
    a la vez — el mismo motivo por el que `estados_de_recursos` existe.
    """
    valores = valores or {}
    notas = notas or {}
    etiquetas = etiquetas or {}
    salida = []
    for base in METRICAS_RECURSOS:
        metrica = base
        if base.clave in etiquetas:
            metrica = Metrica(
                base.clave, etiquetas[base.clave], base.unidad, base.decimales,
                base.escala_fija, base.mejor_si,
            )
        salida.append(indicador(
            metrica, series.get(base.clave, []), reglas, ancho=ancho, alto=alto,
            valor=valores.get(base.clave, SIN_VALOR),
            nota=notas.get(base.clave, ''),
        ))
    return salida


# ---------------------------------------------------------------------------
# Series recientes de muchas estaciones, para los sparklines del listado
# ---------------------------------------------------------------------------

# Ventana y resolución del sparkline del LISTADO (el detalle usa las 60 muestras que ya
# carga). Diez puntos sobre veinte minutos alcanzan para distinguir un pico de una
# meseta, que es lo único que esa tarjeta chica puede comunicar.
VENTANA_SPARKLINE_MINUTOS = 20
PUNTOS_SPARKLINE = 10
# Tope de filas traídas, para que el costo no dependa de cada cuánto reporte el agente:
# con el agente actual (~30 s) entran ~40 muestras por estación en la ventana y solo se
# necesitan 10. El corte se hace en la base, no en Python.
_FACTOR_MUESTRAS = 3

_CAMPOS = (
    'estacion_id', 'timestamp', 'cpu_carga_pct', 'ram_total', 'ram_usada',
    'disco_total_gb', 'disco_libre_gb', 'latencia_ms', 'red_recibido_kbps',
    'red_enviado_kbps',
)


def series_por_estacion(ids, ventana_minutos=VENTANA_SPARKLINE_MINUTOS,
                        puntos=PUNTOS_SPARKLINE):
    """`{estacion_id: {clave: [valores]}}` en UNA consulta, no una por tarjeta.

    Mismo criterio que el resto de `monitoreo_lista`: la pantalla muestra todas las
    estaciones monitoreadas, así que cualquier consulta por tarjeta crece con la flota.

    Los porcentajes se calculan acá y no se leen de las properties de `MuestraMetrica`
    porque no se instancian modelos: a ~1.800 equipos, construir 18.000 objetos Django
    por carga de pantalla cuesta más que la consulta. La fórmula es la misma que la de
    las properties — si alguna cambia, hay que tocar las dos (lo fija una prueba).
    """
    from django.utils import timezone

    from apps.monitoreo.models import MuestraMetrica

    ids = list(ids)
    if not ids:
        return {}

    corte = timezone.now() - timedelta(minutes=ventana_minutos)
    filas = (
        MuestraMetrica.objects
        .filter(estacion_id__in=ids, timestamp__gte=corte)
        .order_by('-timestamp')
        .values_list(*_CAMPOS)[:len(ids) * puntos * _FACTOR_MUESTRAS]
    )

    crudas = {}
    for fila in filas:
        crudas.setdefault(fila[0], []).append(fila)

    series = {}
    for estacion_id, muestras in crudas.items():
        # Vienen de la más nueva a la más vieja: se recortan las `puntos` más recientes
        # y se da vuelta, que es el orden que espera `construir_grafico`.
        recientes = muestras[:puntos][::-1]
        series[estacion_id] = {
            'cpu_carga_pct': [m[2] for m in recientes],
            'ram_usada_pct': [_pct(m[4], m[3]) for m in recientes],
            'disco_usado_pct': [_pct_usado(m[5], m[6]) for m in recientes],
            'latencia_ms': [m[7] for m in recientes],
            'red_total_kbps': [_total(m[8], m[9]) for m in recientes],
        }
    return series


def _pct(parte, total):
    if total and parte is not None:
        return round(100 * parte / total, 1)
    return None


def _pct_usado(total, libre):
    if total and libre is not None:
        return round(100 * (total - libre) / total, 1)
    return None


def _total(rx, tx):
    if rx is None and tx is None:
        return None
    return round((rx or 0) + (tx or 0), 1)
