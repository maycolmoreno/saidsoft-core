"""Lee el horario de atención de una farmacia desde `Farmacia.horario`, que es texto libre.

Por qué existe: el tablero muestra "N estaciones sin reportar" y, de noche, la mayoría de
esas N son locales cerrados. Medido el 12-oct-2026 a las 23:29: 37 calladas de 66, y
ninguna era una avería. El número estaba bien; lo que faltaba era distinguir la estación
que calla porque cerraron de la que calla porque se rompió — que es la única accionable.

**Por qué se parsea texto libre en vez de un campo estructurado.** Porque el dato ya
existe y está cargado: 691 de 702 farmacias activas tienen horario escrito, y ahí está la
información real —incluidas las 8 que abren 24 horas y las 42 que cierran después de las
22:00—. Un campo estructurado sería mejor y sigue siendo el final del camino (haría falta
migración de datos sobre 691 filas), pero hasta entonces esto usa lo que hay en vez de
aproximar con una franja global, que es lo que hacía antes y era falso para unas 50
farmacias: decía "fuera de horario" a las 22:30 a las que cierran a las 23:30, y lo decía
toda la noche a las que no cierran nunca.

**Los formatos son muchos y el parseo tiene trampas.** Sobre los 364 valores distintos que
hay cargados hoy conviven, al menos:

    '07H00 a 22H00'
    '07:00 am A 22:00 pm'
    'LUNES A VIERNES: 07:00 - 21:00 / SABADO: 08:00:00 - 21:00 / DOMINGO  1: 08:00 - 13:00 / DOMINGO  2: - - -'
    '07H30 a 21H30Sab y Dom: 07H30 a 21H00'
    '24 HORAS'
    'LUNES A VIERNES: 24 HORAS - 24 HORAS / SABADO: 24 HORAS - 24 HORAS / ...'
    'Lunes a viernes de 7 am a 8pm / Sábado de 7am a 8pm / Domingo de 8 am a 5pm'
    'De lunes a domingo horario 7 a 9 pm'

Dos trampas concretas, encontradas midiendo contra los datos reales y fijadas en pruebas:

1. **`'07:00 AM - 20:00 PM'`**: un patrón ingenuo de hora+am/pm captura los MINUTOS
   (`00 AM`) y devuelve las 00:00 como apertura. De ahí el `(?<![\\d:])` del lookbehind.
2. **`'DOMINGO  1: 08:00'`**: el `1:` del nombre del día parece una hora (01:08). Por eso
   los nombres de día con numeral se limpian antes de buscar horas.

**Lo que se extrae es la ENVOLVENTE del sitio, no el horario de cada día**: la apertura
más temprana y el cierre más tardío de todo el texto. Para decidir si una estación callada
es esperable, eso alcanza y se equivoca del lado seguro — un domingo de horario corto se
considera abierto hasta la hora del sábado, así que la estación NO se excusa y queda a la
vista. Distinguir por día requeriría el campo estructurado.

**Ante la duda, no se excusa.** Si del texto no sale un rango coherente —una sola hora
suelta, apertura igual o posterior al cierre, campo vacío— el resultado es
`determinado=False` y el consumidor trata la estación como si estuviera en horario. Un
dato malo no puede servir para silenciar una avería; como mucho, para no explicarla.
"""
import re
from dataclasses import dataclass
from functools import lru_cache

# Nombres de día, para limpiar el `N:` que los sigue en el formato estructurado antes de
# buscar horas. Sin esto, 'DOMINGO  1: 08:00' entra como la hora 01:08.
_DIAS = r'LUNES|MARTES|MI[EÉ]RCOLES|JUEVES|VIERNES|S[AÁ]BADO|SABADO|DOMINGO|TODOS LOS D[IÍ]AS'
# El `(?!\d{2})` del final distingue el índice del día de una HORA pegada al nombre, y hace
# falta: `'LUNES A DOMINGO 7:00 A 21:00'` —tres farmacias— se leía como un solo horario de
# las 21:00 porque la limpieza se comía el `7:`. En `'DOMINGO  1: 08:00'` tras los dos
# puntos viene un espacio, así que el numeral sí se limpia; en `'DOMINGO 7:00'` vienen dos
# dígitos y se deja intacto para que lo lea `_RE_HORA_MINUTO`.
_RE_DIA_CON_NUMERAL = re.compile(r'(' + _DIAS + r')\s*\d?\s*:(?!\d{2})', re.IGNORECASE)

# Abierto siempre. Ojo: NO incluye 'a 24H00', que es cerrar a medianoche y es otra cosa
# —hay cinco farmacias con '06H00 a 24H00'—, así que el patrón exige la palabra.
_RE_24H = re.compile(r'24\s*HORAS|TODO\s+EL\s+D[IÍ]A', re.IGNORECASE)

# `HH:MM`, `HHhMM`, `HH:MM:SS` (del formato estructurado, donde los segundos se ignoran
# solos porque el match consume `08:00` y deja `:00` sin dígito delante).
_RE_HORA_MINUTO = re.compile(r'(?<!\d)(\d{1,2})\s*[:Hh]\s*(\d{2})(?!\d)')

# `8 am`, `9pm`. El lookbehind excluye los dígitos Y los dos puntos: sin él, el `00` de
# `07:00 AM` entra como hora 0 (trampa 1 del docstring).
_RE_HORA_AMPM = re.compile(r'(?<![\d:])(\d{1,2})\s*(am|pm)', re.IGNORECASE)


@dataclass(frozen=True)
class HorarioFarmacia:
    """Lo que se pudo leer del horario de un sitio, en horas decimales (21.5 = 21:30)."""

    es_24h: bool
    apertura: float | None
    cierre: float | None
    determinado: bool

    def abierta_a(self, hora_decimal: float) -> bool:
        """¿Debería estar abierto el sitio a esa hora local?

        **No determinado cuenta como ABIERTO**, a propósito: el consumidor usa esto para
        decidir si una estación callada es esperable, y un horario ilegible no puede
        servir de excusa. Ver el último párrafo del docstring del módulo.
        """
        if self.es_24h or not self.determinado:
            return True
        return self.apertura <= hora_decimal < self.cierre


_SIN_DATO = HorarioFarmacia(es_24h=False, apertura=None, cierre=None, determinado=False)
_VEINTICUATRO = HorarioFarmacia(es_24h=True, apertura=None, cierre=None, determinado=True)


@lru_cache(maxsize=2048)
def leer_horario(texto: str) -> HorarioFarmacia:
    """Interpreta el texto de `Farmacia.horario`.

    Con caché por texto y no por farmacia: hay 364 valores distintos para 691 farmacias,
    así que las repeticiones se resuelven una sola vez por proceso. El texto es el único
    argumento justamente para que la caché sea válida — si el horario de una farmacia
    cambia, cambia el texto y con él la clave.
    """
    if not texto or not texto.strip():
        return _SIN_DATO
    if _RE_24H.search(texto):
        return _VEINTICUATRO

    limpio = _RE_DIA_CON_NUMERAL.sub(lambda m: m.group(1) + ' ', texto)

    horas = [
        int(h) + int(m) / 60
        for h, m in _RE_HORA_MINUTO.findall(limpio)
        if int(h) <= 24 and int(m) <= 59
    ]
    for valor, meridiano in _RE_HORA_AMPM.findall(limpio):
        hora = int(valor)
        if meridiano.lower() == 'pm' and hora < 12:
            hora += 12
        elif meridiano.lower() == 'am' and hora == 12:
            hora = 0
        if hora <= 24:
            horas.append(float(hora))

    # Hace falta un rango, no una hora. `'De lunes a domingo horario 7 a 9 pm'` deja una
    # sola hora reconocible (21:00) y con ella la apertura saldría a las 21:00, que es
    # peor que no saber: diría que el local abre cuando en realidad está cerrando.
    if len(horas) < 2:
        return _SIN_DATO

    apertura, cierre = min(horas), max(horas)
    if apertura >= cierre:
        return _SIN_DATO
    return HorarioFarmacia(es_24h=False, apertura=apertura, cierre=cierre, determinado=True)


def farmacia_deberia_estar_abierta(farmacia, instante=None) -> bool:
    """¿Se espera que `farmacia` esté operando en `instante` (por defecto, ahora)?

    La hora se compara en zona local (`TIME_ZONE = 'America/Guayaquil'`) porque los
    horarios están escritos en hora de Ecuador y la base guarda UTC. Comparar sin
    convertir da el resultado contrario justo de noche, que es cuando esto se usa.
    """
    from django.utils import timezone

    local = timezone.localtime(instante or timezone.now())
    return leer_horario(getattr(farmacia, 'horario', '') or '').abierta_a(local.hour + local.minute / 60)


def separar_calladas_por_horario(estaciones, instante=None) -> dict:
    """Reparte estaciones calladas según si su farmacia debería estar abierta.

    `estaciones` es un iterable YA materializado y con `farmacia` precargada
    (`select_related`): esta función no consulta nada, para que el llamador sea el dueño
    de las consultas y el costo se vea en su propio presupuesto.

    Devuelve `dentro` (la farmacia debería estar operando: esto es una avería y hay que
    mirarlo), `fuera` (el local está cerrado: esperable) y los totales. La lista que se
    devuelve es la de `dentro`, porque es la única sobre la que alguien actúa.
    """
    dentro, fuera = [], []
    for estacion in estaciones:
        destino = dentro if farmacia_deberia_estar_abierta(estacion.farmacia, instante) else fuera
        destino.append(estacion)
    return {
        'dentro': dentro,
        'total_dentro': len(dentro),
        'total_fuera': len(fuera),
    }
