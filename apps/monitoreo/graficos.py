"""Genera datos de gráficos SVG (line charts) a partir de series de métricas.

El panel es autocontenido (sin CDN ni librerías JS), así que los gráficos se dibujan
como SVG inline. Aquí se hace la matemática — normalizar valores al área de dibujo —
y el template solo pinta el <polyline> resultante.

Desde el rediseño "autosuficiente" (19-sep-2026, ver PLAN_MODERNIZACION §10-AK) el
gráfico además calcula **todo lo que hace falta para entenderlo sin abrir el detalle**:
dónde cae cada umbral sobre el dibujo, dónde está el último punto y hacia dónde viene
la serie. Eso vive acá y no en el template por dos motivos:

1. Los sparklines se dibujan con `preserveAspectRatio="none"` —la curva se estira para
   llenar la tarjeta—, así que **cualquier texto o círculo dentro del <svg> sale
   deformado**. Las etiquetas y el punto final se posicionan como HTML sobre el SVG, y
   para eso hace falta el porcentaje, no la coordenada del viewBox: por eso cada punto
   trae su `pct_x`/`pct_y` ya calculado.
2. Un umbral dibujado a ojo en el template (había dos `y1="32"`/`y1="10"` literales en
   `enlace_farmacia_modal.html`) deja de corresponder al dato en cuanto alguien cambia
   `alto` o `pad`, y nadie se entera.
"""
from dataclasses import dataclass, field

# Cuánto tiene que apartarse el último valor del promedio de la ventana para que la
# flecha diga "sube"/"baja" en vez de "estable". 2% de la escala: por debajo de eso la
# diferencia no se ve en un sparkline de 64 px, y una flecha que se da vuelta en cada
# refresco de 10 s es ruido — la pantalla de detalle se deja abierta.
_TOLERANCIA_TENDENCIA = 0.02

# Cuánto aire se deja por encima del umbral más alto cuando el eje es automático. Sin
# esto la línea de referencia queda pegada al borde superior y no se lee como línea.
_AIRE_SOBRE_UMBRAL = 1.08

# Hasta cuánto se deja estirar el eje automático para que entre un umbral. Con una regla
# de latencia en 500 ms y una ventana que no pasa de 40, estirar el eje aplastaría la
# curva contra el piso y el gráfico dejaría de mostrar su forma —que es lo único para lo
# que sirve una serie tan por debajo del límite—. Pasado este factor, el umbral se marca
# como fuera de escala y la tarjeta lo dice con un número en vez de dibujarlo.
_TOPE_ESTIRADO = 3


@dataclass(frozen=True)
class Marca:
    """Una línea de referencia sobre el gráfico: un umbral, o un promedio del período.

    `nivel` es el que decide el color y el trazo en el template ('warning', 'critical'
    o 'referencia'), y `etiqueta` el texto que se dibuja al lado — una línea sin
    etiqueta obliga a saber de memoria qué representa, que es justo lo que se quiso
    sacar.
    """

    valor: float
    nivel: str
    etiqueta: str
    y: float
    pct_y: float
    # El umbral cae dentro del eje. Cuando no (una regla en 500 ms sobre un eje que
    # llega a 80), dibujarlo igual lo pegaría al borde y mentiría: el template lo
    # omite y lo dice con texto.
    dentro: bool


@dataclass
class Grafico:
    puntos: str          # "x1,y1 x2,y2 ..." para el <polyline>
    area: str            # mismos puntos + cierre inferior para el relleno
    ancho: int
    alto: int
    ultimo_valor: float | None
    max_valor: float | None

    # --- Contexto para que el gráfico se entienda solo (todo opcional: un Grafico
    # sin datos se sigue construyendo con los seis campos de arriba) ---
    marcas: list = field(default_factory=list)
    # Posición del último punto, en % del ancho/alto — para el marcador y su etiqueta,
    # que van en HTML porque el SVG va estirado (ver docstring del módulo).
    ultimo_pct_x: float | None = None
    ultimo_pct_y: float | None = None
    # 'sube' | 'baja' | 'estable' | 'sin_dato'. Qué es "bueno" no se decide acá: eso
    # depende de la métrica (CPU que sube es malo, alertas resueltas que suben no).
    tendencia: str = 'sin_dato'


def construir_grafico(valores, ancho=280, alto=64, pad=4, escala_fija=None, marcas=()):
    """Convierte una lista de valores (más viejo → más nuevo) en un Grafico.

    escala_fija: si se da (ej. 100 para porcentajes), fija el tope del eje Y; si no,
    usa el máximo de la serie —estirado hasta el umbral más alto, si hay uno, para que
    la línea de referencia entre en el dibujo.

    marcas: iterable de `(valor, nivel, etiqueta)`. Los valores `None` se descartan:
    una métrica sin umbral configurado no dibuja línea en vez de inventar una.
    """
    limpios = [(i, v) for i, v in enumerate(valores) if v is not None]
    marcas = [m for m in marcas if m[0] is not None]
    if not limpios:
        return Grafico('', '', ancho, alto, None, None)

    n = len(valores)
    if escala_fija is not None:
        max_v = escala_fija
    else:
        max_v = max(v for _, v in limpios)
        # El eje se estira hasta el umbral más alto: con autoescala pura una serie
        # tranquila llena igual el alto de la tarjeta y la línea de umbral queda
        # afuera — justo el caso en que el gráfico tiene que decir "esto está lejos
        # del límite". Salvo que el umbral esté tan lejos que estirar destruya la
        # forma de la curva: ver _TOPE_ESTIRADO.
        tope = max((m[0] for m in marcas), default=0) * _AIRE_SOBRE_UMBRAL
        if tope <= max_v * _TOPE_ESTIRADO:
            max_v = max(max_v, tope)
    if not max_v or max_v <= 0:
        max_v = 1

    area_ancho = ancho - 2 * pad
    area_alto = alto - 2 * pad

    def x(i):
        return pad + (area_ancho * i / (n - 1) if n > 1 else area_ancho / 2)

    def y(v):
        return pad + area_alto * (1 - min(v, max_v) / max_v)

    puntos = ' '.join(f'{x(i):.1f},{y(v):.1f}' for i, v in limpios)

    primer_x = x(limpios[0][0])
    ultimo_x = x(limpios[-1][0])
    area = f'{primer_x:.1f},{alto - pad:.1f} {puntos} {ultimo_x:.1f},{alto - pad:.1f}'

    serie = [v for _, v in limpios]
    ultimo = serie[-1]
    ultimo_y = y(ultimo)

    # Tendencia contra el promedio del resto de la ventana, no contra el punto
    # anterior: con muestras cada 30 s, comparar dos puntos consecutivos convierte
    # cualquier oscilación normal en una flecha, y la flecha deja de significar nada.
    previos = serie[:-1]
    tendencia = 'sin_dato'
    if previos:
        base = sum(previos) / len(previos)
        if abs(ultimo - base) <= _TOLERANCIA_TENDENCIA * max_v:
            tendencia = 'estable'
        else:
            tendencia = 'sube' if ultimo > base else 'baja'

    marcas_calculadas = [
        Marca(
            valor=valor, nivel=nivel, etiqueta=etiqueta,
            y=round(y(valor), 1), pct_y=round(100 * y(valor) / alto, 1),
            dentro=0 <= valor <= max_v,
        )
        for valor, nivel, etiqueta in marcas
    ]

    return Grafico(
        puntos=puntos, area=area, ancho=ancho, alto=alto,
        ultimo_valor=ultimo, max_valor=max_v,
        marcas=marcas_calculadas,
        ultimo_pct_x=round(100 * ultimo_x / ancho, 2),
        ultimo_pct_y=round(100 * ultimo_y / alto, 2),
        tendencia=tendencia,
    )
