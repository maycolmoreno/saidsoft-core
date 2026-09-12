"""Resumen de progreso de un envío a varias estaciones.

Lo comparten el despliegue de POS (`apps.despliegues`) y la instalación de software
(`apps.software`): los dos mandan algo a N estaciones y muestran cuántas terminaron,
cuántas fallaron y en qué paso está el resto. La aritmética era idéntica en las dos
vistas y el fragmento de la barra casi idéntico en las dos plantillas.

No lo usa `apps.scripts`: su pantalla de ejecución muestra un estado general y la salida
de cada estación, sin barra ni desglose. Forzarla a este molde sería inventar una
duplicación que no existe — la auditoría del 12-sep-2026 la contó como tercera por error.

`apps.aperturas` tampoco: su avance no es "N de M estaciones" sino pasos de distinta
naturaleza (script, software, verificación, manual) sobre estaciones distintas.
"""
from dataclasses import dataclass, field


@dataclass
class ResumenProgreso:
    """Lo que necesita `panel/_barra_progreso.html` para dibujarse."""

    total: int
    completados: int
    errores: int
    pct_completado: int
    etiqueta_completados: str
    desglose: list = field(default_factory=list)


def resumen_de_progreso(resultados, estados, *, estado_ok, estados_error,
                        etiqueta_completados, estados_visibles=()):
    """Cuenta los resultados de un envío y arma el resumen para la barra.

    `resultados` es el queryset de Resultado* del envío; `estados` su TextChoices.
    `estado_ok` es el único que cuenta como terminado bien; `estados_error` los que
    cuentan como falla (el despliegue suma ERROR y ROLLBACK, la instalación solo ERROR).
    `estados_visibles` son los pasos intermedios que se listan en el desglose, en orden.

    El conteo se hace en una sola pasada sobre el queryset ya traído, no con un `.count()`
    por estado: la plantilla itera igual esos mismos resultados en la tabla de abajo, así
    que evaluarlo una vez es más barato que N consultas de agregación.
    """
    conteo = {valor: 0 for valor, _ in estados.choices}
    total = 0
    for resultado in resultados:
        conteo[resultado.estado] += 1
        total += 1

    completados = conteo.get(estado_ok, 0)
    errores = sum(conteo.get(e, 0) for e in estados_error)
    etiquetas = dict(estados.choices)

    return ResumenProgreso(
        total=total,
        completados=completados,
        errores=errores,
        # Sin resultados el porcentaje es 0 y no una división por cero: un envío recién
        # creado todavía no publicó a ninguna estación.
        pct_completado=round(100 * completados / total) if total else 0,
        etiqueta_completados=etiqueta_completados,
        desglose=[
            {'etiqueta': etiquetas[e], 'cantidad': conteo.get(e, 0)}
            for e in estados_visibles
        ],
    )
