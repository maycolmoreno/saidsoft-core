"""Seguimiento en vivo de una operación sobre la flota: despliegues del POS e instalaciones
de software.

Hasta ahora el avance era una barra y una tabla de estados sueltos. Esto arma, con lo que
ya se guarda:

- el **rastreador** de cada estación: los pasos que cumplió, al estilo del seguimiento de
  un paquete, con lo incierto destacado (`rastreador`);
- los **conteos por categoría** para las barras segmentadas (`conteos`);
- los **anillos** de un despliegue, que son los despliegues encadenados por "Promover al
  siguiente anillo" (`anillos`).

El paso al que llegó una estación sale de los eventos que reportó el agente (el mayor
alcanzado), no del estado resumido: un error dice así en qué paso falló.
"""
from collections import Counter
from dataclasses import dataclass

# Categorías, en orden de prioridad para el operador: lo que pide una decisión primero.
INCIERTO, ERROR, CURSO, ESPERA, LISTO = 'incierto', 'error', 'curso', 'espera', 'listo'
PRIORIDAD = [INCIERTO, ERROR, CURSO, ESPERA, LISTO]
NOMBRE_CATEGORIA = {
    LISTO: 'Listas', CURSO: 'En curso', INCIERTO: 'Inciertas', ERROR: 'Con error', ESPERA: 'Esperando',
}


@dataclass(frozen=True)
class Dominio:
    tipo_orden: str
    pasos: tuple
    nivel_evento: dict  # paso reportado -> cuántos pasos quedan cumplidos
    estado_ok: str
    estados_error: frozenset


DESPLIEGUE = Dominio(
    tipo_orden='despliegue',
    pasos=('Publicada', 'Recibida', 'Descargada', 'Aplicando', 'Lista'),
    nivel_evento={
        'publicado': 1, 'recibido': 2, 'descargado': 3, 'hash_verificado': 3,
        'pos_cerrado': 4, 'aplicado': 4, 'pos_relanzado': 4, 'ok': 5,
    },
    estado_ok='aplicado', estados_error=frozenset({'error', 'rollback'}),
)
SOFTWARE = Dominio(
    tipo_orden='software',
    pasos=('Publicada', 'Recibida', 'Descargada', 'Instalando', 'Lista'),
    nivel_evento={'publicado': 1, 'recibido': 2, 'descargado': 3, 'hash_verificado': 3, 'instalando': 4, 'instalado': 5},
    estado_ok='instalado', estados_error=frozenset({'error'}),
)

_TONO_PASO = {LISTO: 'good', CURSO: 'accent', INCIERTO: 'warning', ERROR: 'critical', ESPERA: 'neutral'}


def _ultima_orden_por_estacion(tipo_orden, origen_id):
    from apps.catalogo.models import OrdenAgente

    ultima = {}
    for o in OrdenAgente.objects.filter(tipo=tipo_orden, origen_id=origen_id).order_by('creada_en', 'pk'):
        ultima[o.estacion_id] = o
    return ultima


def rastreador(resultados, dominio: Dominio, *, origen_id, modo_aplicacion=''):
    """Una fila por estación, ordenadas por prioridad. `resultados` debe traer
    `estacion`, `estacion__farmacia` y `eventos` ya cargados (select/prefetch)."""
    from apps.catalogo.ordenes import INCIERTAS, VIVOS

    ordenes = _ultima_orden_por_estacion(dominio.tipo_orden, origen_id)
    filas = []
    for r in resultados:
        orden = ordenes.get(r.estacion_id)
        nivel = max((dominio.nivel_evento.get(e.paso, 0) for e in r.eventos.all()), default=0)
        if orden is not None and orden.estado not in ('pendiente', 'publicando', 'cancelada', 'caducada'):
            nivel = max(nivel, 1)  # el broker la aceptó: está publicada
        incierta = orden is not None and (
            orden.estado in INCIERTAS or (orden.requiere_revision and orden.estado in VIVOS)
        )
        if r.estado == dominio.estado_ok:
            categoria, nivel = LISTO, len(dominio.pasos)
        elif r.estado in dominio.estados_error:
            categoria = ERROR
        elif incierta:
            categoria = INCIERTO
        elif nivel >= 1:
            categoria = CURSO
        else:
            categoria = ESPERA

        if categoria == LISTO:
            texto = 'Lista'
        elif categoria == ERROR:
            texto = 'Error'
        elif categoria == INCIERTO:
            texto = orden.get_estado_display()
        elif categoria == CURSO and nivel == 3 and modo_aplicacion == 'cierre_pos':
            texto = 'Esperando cierre de caja'
        elif categoria == CURSO and nivel == 3 and modo_aplicacion == 'ventana':
            texto = 'Esperando la ventana programada'
        elif categoria == CURSO:
            texto = dominio.pasos[nivel - 1]
        else:
            texto = 'Esperando'

        pasos = []
        for i, nombre in enumerate(dominio.pasos):
            if i < nivel:
                tono = 'good'
            elif i == nivel and categoria in (CURSO, INCIERTO, ERROR):
                tono = _TONO_PASO[categoria]
            else:
                tono = 'neutral'
            pasos.append({'nombre': nombre, 'tono': tono})

        filas.append({
            'resultado': r, 'estacion': r.estacion, 'categoria': categoria, 'texto': texto, 'pasos': pasos,
            'detalle': getattr(r, 'detalle_error', '') or (orden.motivo_revision if incierta and orden else ''),
            'orden': orden if incierta else None,
        })
    filas.sort(key=lambda f: (PRIORIDAD.index(f['categoria']), f['estacion'].codigo))
    return filas


def conteos(filas):
    """`[(categoria, nombre, cantidad, pct)]` en orden de barra (listo primero), sin vacías."""
    total = len(filas) or 1
    cuenta = Counter(f['categoria'] for f in filas)
    orden = [LISTO, CURSO, INCIERTO, ERROR, ESPERA]
    return [(c, NOMBRE_CATEGORIA[c], cuenta[c], round(100 * cuenta[c] / total, 1)) for c in orden if cuenta[c]]


def anillos(despliegue):
    """Los despliegues de la misma cadena de anillos, del primero al último, cada uno con
    sus conteos. El de `despliegue` va marcado como actual."""
    raiz, vistos = despliegue, {despliegue.pk}
    while raiz.despliegue_origen_id and raiz.despliegue_origen_id not in vistos:
        raiz = raiz.despliegue_origen
        vistos.add(raiz.pk)
    cadena, pendientes = [], [raiz]
    while pendientes and len(cadena) < 20:
        actual = pendientes.pop(0)
        cadena.append(actual)
        pendientes.extend(actual.promovidos.order_by('fecha_creacion', 'pk'))
    resultado = []
    for i, d in enumerate(cadena):
        filas = rastreador(_resultados(d), DESPLIEGUE, origen_id=d.pk, modo_aplicacion=d.modo_aplicacion)
        resultado.append({
            'despliegue': d, 'numero': i + 1, 'actual': d.pk == despliegue.pk,
            'total': len(filas), 'conteos': conteos(filas),
        })
    return resultado


def _resultados(despliegue):
    return (despliegue.resultados.select_related('estacion', 'estacion__farmacia')
            .prefetch_related('eventos').order_by('estacion__codigo'))


def resultados_despliegue(despliegue):
    return _resultados(despliegue)
