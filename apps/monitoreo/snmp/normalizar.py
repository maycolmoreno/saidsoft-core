"""La forma común de un valor leído por SNMP, y las dos reglas que es fácil equivocar.

Esto es lo que separa "el OID devolvió 80" de "el tóner negro está al 80 %". No habla con
la red ni con la base: son funciones puras, y por eso se prueban sin un equipo prendido.

`LecturaSnmp` guarda **el valor interpretado y el crudo por separado**. No es
redundancia: es la diferencia entre "la impresora no sabe cuánto queda" y "está vacía".
Con una sola columna, un `-2` se convierte en `-2 %` o en `0 %`, y las dos son mentira.
"""
from dataclasses import dataclass

# --- Unidades de prtMarkerSuppliesSupplyUnit y prtMarkerCounterUnit (RFC 3805) ---
# Solo las que aparecen en la práctica. El número es el que manda el equipo.
UNIDAD_IMPRESIONES = '7'
UNIDAD_HOJAS = '8'
UNIDAD_DECIMAS_DE_GRAMO = '13'
UNIDAD_PORCENTAJE = '19'

NOMBRE_DE_UNIDAD = {
    UNIDAD_IMPRESIONES: 'impresiones',
    UNIDAD_HOJAS: 'hojas',
    UNIDAD_DECIMAS_DE_GRAMO: 'décimas de gramo',
    UNIDAD_PORCENTAJE: 'porcentaje',
}

# Valores especiales de prtMarkerSuppliesLevel / MaxCapacity. RFC 3805 los define como
# `other(-1)`, `unknown(-2)` y `partial(-3)`: "queda algo, pero la cantidad es
# indeterminada". NO son niveles. Vas a encontrar foros que dicen "-3 = OK" y
# "-2 = casi vacío": es folklore, la norma dice lo de arriba.
CENTINELAS = {
    -1: 'other',
    -2: 'desconocido',
    -3: 'queda algo (cantidad indeterminada)',
}


@dataclass(frozen=True)
class LecturaSnmp:
    """Un valor leído, ya con nombre propio y unidad.

    `clave` es lo que entiende el resto del sistema (`toner.black.nivel`,
    `paginas.total`, `bandeja.1.nivel`) y nunca un OID: el panel no tiene por qué saber
    que el tóner negro vive en 1.3.6.1.2.1.43.11.1.1.9.1.1.

    `valor` es None cuando el equipo no sabe o no se puede interpretar; `crudo` conserva
    lo que vino, incluido el centinela, para que se pueda distinguir "no medido" de
    "medido en cero".
    """

    clave: str
    valor: float | None = None
    crudo: int | None = None
    unidad: str = ''
    texto: str = ''

    @property
    def medido(self) -> bool:
        """False si el equipo no pudo dar el dato.

        Importa porque un recurso que no se pudo medir **no es un recurso sano**: mismo
        criterio que `apps.panel.umbrales.clasificar`, que devuelve 'sin_dato' y no 'ok'
        para un None, justamente para no pintar de verde algo que nadie midió.
        """
        return self.valor is not None


def interpretar_nivel(nivel, maximo=None, unidad='') -> tuple[float | None, int | None]:
    """`(valor, crudo)` para un nivel de suministro o de bandeja.

    Las tres reglas, en orden, y las tres salieron de la norma o de un equipo real:

    1. **Un centinela negativo no es un nivel.** Devuelve `(None, -2)`, no `(-2, -2)`.
    2. **Si la unidad ya es porcentaje, el nivel se usa tal cual.** Es el caso de la
       RICOH MP C2503 medida el 8-oct-2026: `SupplyUnit=19` y `MaxCapacity=100`.
    3. **Si no, se calcula contra la capacidad** — y solo si hay una capacidad utilizable.
       Dividir por un máximo ausente o en cero daría un porcentaje inventado, así que en
       ese caso se devuelve el valor crudo sin convertir: 250 hojas son 250 hojas.
    """
    crudo = _entero(nivel)
    if crudo is None:
        return None, None
    if crudo < 0:
        return None, crudo
    if unidad == UNIDAD_PORCENTAJE:
        return float(crudo), crudo

    tope = _entero(maximo)
    if tope is not None and tope > 0:
        return round(crudo / tope * 100, 1), crudo
    # Sin denominador no hay porcentaje. El valor sigue sirviendo con su unidad.
    return float(crudo), crudo


def limpiar_texto(valor) -> str:
    """El texto de un equipo SNMP, sin los bytes que PostgreSQL no acepta.

    **No es paranoia defensiva: pasó.** `hrPrinterDetectedErrorState` es un BITS, y en una
    RICOH MP C2503 vale `0x00`. Guardar eso como texto revienta con

        DataError: PostgreSQL text fields cannot contain NUL (0x00) bytes

    y lo peor es que **SQLite lo acepta**, así que las pruebas pasaban y la primera
    escritura real contra producción fallaba. Es exactamente la familia de bugs que
    CLAUDE.md advierte —probar en un motor y desplegar en otro— en la dirección inversa a
    la documentada.

    Se aplica a TODO texto que venga de un equipo, no solo al bitmask: la descripción de
    un suministro, el texto de una consola y una alerta vienen de firmware arbitrario, y
    cualquiera puede traer bytes de control. Se limpia en el borde, una vez, en vez de
    confiar en que cada intérprete se acuerde.
    """
    if valor is None:
        return ''
    texto = valor if isinstance(valor, str) else str(valor)
    # Se quitan NUL y el resto de los controles C0 salvo tab/salto, que sí son texto.
    return ''.join(c for c in texto if c >= ' ' or c in '\t\n').strip()


def a_hexadecimal(valor) -> str:
    """Un OCTET STRING como hex imprimible: `'\\x00'` -> `'00'`.

    Para los campos que son BITS y no texto. Convertirlos con `limpiar_texto` los dejaría
    vacíos y se perdería la diferencia entre "todo en ceros" y "no vino" — que para
    `hrPrinterDetectedErrorState` es justo la distinción que importa: ese `0x00` NO
    significa "sin problemas".
    """
    if valor is None:
        return ''
    texto = valor if isinstance(valor, str) else str(valor)
    try:
        return ''.join('%02x' % ord(c) for c in texto)
    except (TypeError, ValueError):
        return ''


def es_centinela(crudo) -> bool:
    valor = _entero(crudo)
    return valor is not None and valor in CENTINELAS


def describir_crudo(crudo) -> str:
    """El texto de un centinela, o '' si es un número normal."""
    valor = _entero(crudo)
    return CENTINELAS.get(valor, '') if valor is not None else ''


def nombre_de_unidad(unidad) -> str:
    return NOMBRE_DE_UNIDAD.get(str(unidad or ''), str(unidad or ''))


def _entero(valor):
    """El entero que representa `valor`, o None.

    Acepta el texto que devuelve pysnmp y también un int ya convertido. No usa
    `int(valor)` a secas porque los equipos mandan de todo en estos campos: cadenas
    vacías, 'N/A', y en un caso medido el marcador "No Such Object" (que
    `apps.monitoreo.mikrotik._texto_snmp` ya filtra a '', pero no hay que depender de eso).
    """
    if valor is None:
        return None
    if isinstance(valor, bool):
        return None
    if isinstance(valor, int):
        return valor
    texto = str(valor).strip()
    if not texto:
        return None
    negativo = texto.startswith('-')
    digitos = texto[1:] if negativo else texto
    if not digitos.isdigit():
        return None
    return -int(digitos) if negativo else int(digitos)
