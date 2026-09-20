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
    """Devuelve 'ok', 'warning', 'critical', 'sin_dato' o 'sin_umbral'.

    `None` es 'sin_dato' y no 'ok': un recurso que no se pudo medir no es un recurso
    sano, y pintarlo verde diría que está bien algo que nadie midió.

    Los umbrales pueden venir en `None` (métrica sin regla configurada y sin defecto):
    eso es 'sin_umbral', que tampoco es 'ok'.
    """
    if valor is None:
        return 'sin_dato'
    if umbral_critico is not None and valor >= umbral_critico:
        return 'critical'
    if umbral_warning is not None and valor >= umbral_warning:
        return 'warning'
    if umbral_warning is None and umbral_critico is None:
        # Ni regla ni defecto: el valor se muestra, pero SIN color. Pintarlo verde
        # afirmaría que está bien algo contra lo que no hay con qué comparar.
        return 'sin_umbral'
    return 'ok'


# ---------------------------------------------------------------------------
# Umbrales configurados: lo que ReglaAlerta ya dice, en vez de una segunda verdad
# ---------------------------------------------------------------------------
#
# Las constantes de arriba son el DEFECTO, no la verdad. La verdad de "cuánto es
# mucho" ya está en `ReglaAlerta`: es el valor con el que una alerta se abre de
# verdad y le llega a alguien por Telegram. Cuando el panel pintaba con 75/90 y la
# regla estaba en 85, la tarjeta se ponía amarilla antes que el sistema de alertas
# y nadie sabía cuál de los dos números era el bueno.
#
# Desde el rediseño de gráficas (19-sep-2026) la línea de umbral que se dibuja y el
# color del valor salen de acá: primero la regla activa, y solo si no hay regla para
# esa métrica, la constante de defecto.

# Porcentaje del ancho CONTRATADO a partir del cual el consumo de una farmacia se
# marca. Estaba escrito a mano dentro de `enlace_farmacia_modal.html` (`>= 90` /
# `>= 70`), al lado de un valor que se coloreaba con los umbrales ABSOLUTOS en kbps de
# más arriba: dos criterios distintos para el mismo número, en la misma tarjeta.
BW_CONTRATADO_UMBRAL_WARNING_PCT = 70
BW_CONTRATADO_UMBRAL_CRITICAL_PCT = 90

# Solo se leen las reglas "≥": las pantallas de recursos son medidores donde más es
# peor, y una regla "≤" (no hay ninguna hoy) necesitaría el coloreado invertido. Se
# ignora explícitamente en vez de mezclarla y pintar al revés.
_OPERADOR_MEDIDOR = 'gte'


def umbrales_de_reglas(unidades=None):
    """`{metrica: {'warning': x, 'critical': y}}` según las ReglaAlerta ACTIVAS.

    `unidades`: iterable de UnidadNegocio en foco. Las reglas globales
    (`unidad_negocio=None`) siempre entran. Con varias unidades a la vista se toma el
    límite que **dispara primero** (el más bajo), porque el panel muestra las dos
    juntas: pintar de verde algo que ya abrió alerta para uno de los clientes sería
    peor que pintar de amarillo algo que para el otro todavía está bien.

    Una sola consulta, sin importar cuántas métricas se pidan.
    """
    from django.db.models import Q

    from apps.monitoreo.models import ReglaAlerta

    reglas = ReglaAlerta.objects.filter(activo=True, operador=_OPERADOR_MEDIDOR)
    if unidades is not None:
        reglas = reglas.filter(Q(unidad_negocio__isnull=True) | Q(unidad_negocio__in=unidades))

    por_metrica = {}
    for metrica, severidad, umbral in reglas.values_list('metrica', 'severidad', 'umbral'):
        nivel = por_metrica.setdefault(metrica, {})
        if severidad not in nivel or umbral < nivel[severidad]:
            nivel[severidad] = umbral
    return por_metrica


def limites(reglas, metrica, warning_defecto=None, critical_defecto=None):
    """El par (warning, critical) efectivo de una métrica.

    `None` en cualquiera de los dos significa "no hay umbral para esto" — y eso el
    panel lo dice, no lo rellena: latencia y red por estación no tienen constante de
    defecto, así que hasta que alguien cree la regla la tarjeta muestra el número sin
    color y sin línea, en vez de inventar dónde está el límite.
    """
    de_regla = reglas.get(metrica, {})
    return (
        de_regla.get('warning', warning_defecto),
        de_regla.get('critical', critical_defecto),
    )


def limites_de_enlace(ancho_contratado_mbps):
    """`(warning, critical)` en kbps para el consumo del enlace de una farmacia.

    Un mismo número, un mismo criterio. Hasta el 19-sep-2026 el modal de una farmacia
    coloreaba el consumo con los umbrales ABSOLUTOS de arriba (8.000 / 15.000 kbps) y,
    tres líneas más abajo, dibujaba una barra coloreada por el PORCENTAJE del ancho
    contratado (70 / 90): sobre un enlace de 4 Mbps el número salía verde y la barra
    roja, al mismo tiempo y para el mismo dato.

    Cuando se conoce el ancho contratado, ése es el criterio bueno —"mucho" es relativo
    a lo que se compró—. Cuando no se conoce, se cae a los absolutos, que es lo único
    que queda, y la pantalla lo dice.
    """
    if ancho_contratado_mbps:
        kbps = ancho_contratado_mbps * 1000
        return (
            kbps * BW_CONTRATADO_UMBRAL_WARNING_PCT / 100,
            kbps * BW_CONTRATADO_UMBRAL_CRITICAL_PCT / 100,
        )
    return RED_FARMACIA_UMBRAL_WARNING_KBPS, RED_FARMACIA_UMBRAL_CRITICAL_KBPS
