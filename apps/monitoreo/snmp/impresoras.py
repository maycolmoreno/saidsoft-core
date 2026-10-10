"""Interpretación de Printer-MIB: de columnas crudas a lecturas con nombre.

Todo lo de acá son funciones puras sobre diccionarios `{indice: valor}`. No tocan la red
ni la base, así que se prueban con la salida real de un equipo guardada como fixture — y
eso es lo que hace que las dos trampas de abajo tengan una prueba que falla en rojo si
alguien las reintroduce.

**Trampa 1: el índice de la tabla de suministros NO es CMYK.** En la RICOH MP C2503 medida
el 8-oct-2026 las cinco filas son:

    .1.1  tipo=3 (tóner)           black     80 %
    .1.2  tipo=4 (tóner RESIDUAL)  other    100 %   <- no es el cian
    .1.3  tipo=3                   cyan      90 %
    .1.4  tipo=3                   magenta   80 %
    .1.5  tipo=3                   yellow    90 %

Cablear `...9.1.1` a `...9.1.4` como CMYK habría leído el residual como "cian al 100 %" y
corrido todos los colores un lugar. La clave de cada lectura se arma con
`prtMarkerSuppliesType` y `prtMarkerColorantValue`, nunca con la posición.

**Trampa 2: el nivel no es un porcentaje.** Ver `normalizar.interpretar_nivel`.
"""
from .normalizar import LecturaSnmp, interpretar_nivel, limpiar_texto, nombre_de_unidad

# prtMarkerSuppliesType (RFC 3805). Solo los que aparecen en equipos reales; el resto cae
# en el `else` y se nombra por su número, que es mejor que inventarle una etiqueta.
TIPOS_SUMINISTRO = {
    '1': 'otro',
    '2': 'desconocido',
    '3': 'toner',
    '4': 'residual',
    '5': 'tinta',
    '6': 'cartucho',
    '9': 'revelador',
    '10': 'fusor',
    '15': 'cinta',
    '18': 'solido',
    '21': 'mantenimiento',
}

#: prtMarkerSuppliesClass: 3 = se consume, 4 = se llena (un recipiente de residuos).
#: Importa para el umbral: en un recipiente que se llena, 100 % es el problema, no la
#: salud. Por eso el residual NO se puede alertar con la misma regla que el tóner.
CLASE_SE_CONSUME = '3'
CLASE_SE_LLENA = '4'

#: prtMarkerCounterUnit. 8 = hojas: a doble faz una hoja son DOS impresiones.
ESTADO_IMPRESORA = {
    '1': 'other', '2': 'desconocido', '3': 'inactiva', '4': 'imprimiendo', '5': 'calentando',
}
ESTADO_DISPOSITIVO = {
    '1': 'desconocido', '2': 'funcionando', '3': 'advertencia', '4': 'en prueba', '5': 'caido',
}

#: Los colorantes que no nombran un color. Se omiten de la clave para que el residual no
#: quede como `toner.other.nivel`.
COLORANTES_SIN_COLOR = {'', 'other', 'none', 'unknown'}


def interpretar_suministros(columnas) -> list:
    """`[LecturaSnmp]` a partir de las siete columnas de la tabla de suministros.

    `columnas` llega en el orden que declara el catálogo: tipo, descripción, unidad,
    máximo, nivel, colorante, clase. Se recorre por los índices que trajo el NIVEL, que es
    la columna sin la cual una fila no sirve para nada.
    """
    tipos, descrs, unidades, maximos, niveles, colorantes, clases = _siete(columnas)

    lecturas = []
    for indice in sorted(niveles, key=_orden_de_indice):
        tipo = TIPOS_SUMINISTRO.get(str(tipos.get(indice, '')), 'tipo%s' % tipos.get(indice, '?'))
        color = str(colorantes.get(indice, '')).strip().lower()
        unidad = str(unidades.get(indice, ''))
        valor, crudo = interpretar_nivel(niveles.get(indice), maximos.get(indice), unidad)

        lecturas.append(LecturaSnmp(
            clave=_clave_de_suministro(tipo, color, indice),
            valor=valor,
            crudo=crudo,
            unidad='porcentaje' if valor is not None else nombre_de_unidad(unidad),
            texto=limpiar_texto(descrs.get(indice, '')),
        ))
        # La clase va aparte y como texto: es lo que distingue "se consume" de "se llena",
        # y sin eso nadie puede saber que en el residual el 100 % es el problema.
        clase = str(clases.get(indice, ''))
        if clase in (CLASE_SE_CONSUME, CLASE_SE_LLENA):
            lecturas.append(LecturaSnmp(
                clave=_clave_de_suministro(tipo, color, indice, sufijo='clase'),
                unidad='enum',
                texto='se_llena' if clase == CLASE_SE_LLENA else 'se_consume',
            ))
    return lecturas


def interpretar_alertas(columnas) -> list:
    """`[LecturaSnmp]` con lo que el equipo declara como alerta activa.

    En la Ricoh medida esto trajo "No hay papel: Bandeja 2" **ya en español**, que es más
    accionable que cualquier enum. No reemplaza a `bandeja.N.nivel`: el texto le dice a una
    persona qué pasa, el número le dice a una `ReglaAlerta` cuándo disparar.
    """
    descripciones = columnas[0] if columnas else {}
    return [
        LecturaSnmp(clave='alerta.%s' % indice, unidad='texto', texto=limpiar_texto(texto))
        for indice, texto in sorted(descripciones.items(), key=lambda par: _orden_de_indice(par[0]))
        if limpiar_texto(texto)
    ]


def describir_estado(lecturas_por_clave) -> str:
    """Una línea legible del estado, combinando las fuentes que SÍ son confiables.

    No usa `estado.errores_bitmask`: `hrPrinterDetectedErrorState` devolvió `0x00` en una
    RICOH MP C2503 **estando sin papel**. Ese cero no significa "sin problemas", así que
    tomarlo como estado sería afirmar algo falso.
    """
    partes = []
    dispositivo = ESTADO_DISPOSITIVO.get(str(lecturas_por_clave.get('estado.dispositivo', '')))
    impresora = ESTADO_IMPRESORA.get(str(lecturas_por_clave.get('estado.impresora', '')))
    if dispositivo:
        partes.append(dispositivo)
    if impresora and impresora != dispositivo:
        partes.append(impresora)
    consola = str(lecturas_por_clave.get('equipo.consola', '')).strip()
    if consola:
        partes.append(consola)
    return ' · '.join(partes)


def bitmask_sin_informacion(crudo) -> bool:
    """True si `hrPrinterDetectedErrorState` vino todo en ceros.

    Se expone para que quien lo lea pueda **decirlo** en vez de interpretarlo como
    "sin problemas": medido contra un equipo con falta de papel y el bitmask en `0x00`.
    """
    if crudo is None:
        return True
    if isinstance(crudo, (bytes, bytearray)):
        return not any(crudo)
    return not str(crudo).strip()


# --- internos ---------------------------------------------------------------------

def _siete(columnas):
    """Las siete columnas, tolerando que falte alguna al final.

    Un equipo puede no publicar `prtMarkerSuppliesClass` sin que eso invalide el resto, y
    un `IndexError` acá dejaría sin tóner a una impresora por una columna opcional.
    """
    faltantes = 7 - len(columnas)
    return tuple(columnas) + tuple({} for _ in range(max(0, faltantes)))


def _clave_de_suministro(tipo, color, indice, sufijo='nivel') -> str:
    """`toner.black.nivel`, `residual.nivel`, `fusor.nivel`.

    El índice entra en la clave **solo** cuando no hay con qué distinguir dos filas del
    mismo tipo sin color — si no, dos cartuchos de mantenimiento se pisarían entre sí.
    """
    if color and color not in COLORANTES_SIN_COLOR:
        return '%s.%s.%s' % (tipo, color, sufijo)
    return '%s.%s.%s' % (tipo, indice, sufijo) if _tipo_repetible(tipo) else '%s.%s' % (tipo, sufijo)


def _tipo_repetible(tipo) -> bool:
    """Los tipos que un equipo puede tener más de uno sin que lleven color."""
    return tipo in {'mantenimiento', 'fusor', 'otro', 'desconocido'} or tipo.startswith('tipo')


def _orden_de_indice(indice):
    """Ordena `1.10` después de `1.9`, que un sort de texto pone antes.

    Los índices de estas tablas son `markerIndex.supplyIndex` ('1.1', '1.2', …) y con diez
    suministros el orden alfabético deja de coincidir con el del equipo.
    """
    partes = str(indice).split('.')
    return tuple(int(p) if p.isdigit() else 0 for p in partes)
