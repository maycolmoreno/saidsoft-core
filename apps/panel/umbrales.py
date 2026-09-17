"""Clasificación de un valor contra sus umbrales, para colorear la interfaz.

Vive acá y no dentro de una vista porque lo usan dos dominios distintos: los recursos
de una estación (CPU, RAM, disco) y el consumo del enlace de una farmacia. Cuando se
dividió `views/monitoreo.py` quedó a la vista que era compartido.

Mismo criterio que `paginacion.py`: una función que necesitan varias vistas no es de
ninguna de ellas.
"""

# Umbrales de color, en un solo lugar. Cuando `views/monitoreo.py` se dividió en tres,
# estas cinco líneas quedaron copiadas en los tres archivos — y un umbral escrito tres
# veces se desincroniza: la misma estación se vería amarilla en una pantalla y verde en
# otra, sin que nada falle de forma visible.
RED_FARMACIA_UMBRAL_WARNING_KBPS = 8000
RED_FARMACIA_UMBRAL_CRITICAL_KBPS = 15000
UMBRAL_CPU_WARNING_PCT, UMBRAL_CPU_CRITICAL_PCT = 75, 90
UMBRAL_RAM_WARNING_PCT, UMBRAL_RAM_CRITICAL_PCT = 80, 92
UMBRAL_DISCO_WARNING_PCT, UMBRAL_DISCO_CRITICAL_PCT = 85, 95



def clasificar(valor, umbral_warning, umbral_critico):
    """Devuelve 'ok', 'warning', 'critical' o 'sin_dato'.

    `None` es 'sin_dato' y no 'ok': un recurso que no se pudo medir no es un recurso
    sano, y pintarlo verde diría que está bien algo que nadie midió.
    """
    if valor is None:
        return 'sin_dato'
    if valor >= umbral_critico:
        return 'critical'
    if valor >= umbral_warning:
        return 'warning'
    return 'ok'
