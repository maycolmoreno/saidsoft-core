"""Genera datos de gráficos SVG (line charts) a partir de series de métricas.

El panel es autocontenido (sin CDN ni librerías JS), así que los gráficos se dibujan
como SVG inline. Aquí se hace la matemática — normalizar valores al área de dibujo —
y el template solo pinta el <polyline> resultante.
"""
from dataclasses import dataclass


@dataclass
class Grafico:
    puntos: str          # "x1,y1 x2,y2 ..." para el <polyline>
    area: str            # mismos puntos + cierre inferior para el relleno
    ancho: int
    alto: int
    ultimo_valor: float | None
    max_valor: float | None


def construir_grafico(valores, ancho=280, alto=64, pad=4, escala_fija=None):
    """Convierte una lista de valores (más viejo → más nuevo) en un Grafico.

    escala_fija: si se da (ej. 100 para porcentajes), fija el tope del eje Y; si no,
    usa el máximo de la serie.
    """
    limpios = [(i, v) for i, v in enumerate(valores) if v is not None]
    if not limpios:
        return Grafico('', '', ancho, alto, None, None)

    n = len(valores)
    max_v = escala_fija if escala_fija is not None else max(v for _, v in limpios)
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

    return Grafico(puntos, area, ancho, alto, limpios[-1][1], max_v)
