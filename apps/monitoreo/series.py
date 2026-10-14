"""Series históricas reconstruidas desde los eventos, para el tablero.

Por qué existe: el tablero sabía cuántas estaciones están en línea AHORA, pero no si eso
venía mejorando o empeorando. Cada número era el del instante, y con eso no se distingue
una caída que empezó hace diez minutos de una situación que lleva así todo el día.

**Se RECONSTRUYE, no se guarda.** `EventoMonitoreo` registra las TRANSICIONES de
`en_linea` —no una muestra por minuto— así que el estado en cualquier instante pasado es
el del último evento anterior a ese instante. Eso evita el modelo nuevo que haría falta
para persistir un snapshot por hora (lo que §10-AK dejó anotado como "decisión del
usuario"), y encima es más barato: medido el 13-oct-2026, 24 horas de flota son **103
filas**, no 24 × 71 snapshots.

**El umbral NO es un problema, y conviene dejarlo escrito porque parecía serlo.** La
auditoría señaló como riesgo mayor que las transiciones las escribe
`marcar_estaciones_offline` con el umbral de ALERTA (5 min) mientras las pantallas usan
el de VISUALIZACIÓN (10 min), así que el último punto de la curva no coincidiría con el
KPI de al lado — el hallazgo D-01 otra vez. Medido contra producción, los cuatro
criterios dan el MISMO número:

    KPI estado_flota (10 min, sin admin) : 35 / 71
    aprobadas con latido < 10 min        : 35 / 71
    aprobadas con latido <  5 min        : 35 / 71
    EstadoDispositivo mqtt en_linea=True : 35

El motivo es que una estación late cada minuto o lleva horas callada: la franja entre los
5 y los 10 minutos está casi siempre vacía. El riesgo era real en el papel y no en los
datos. Si algún día los números se separan, esta es la función que hay que revisar.

**Solo fuente MQTT.** `EventoMonitoreo` tiene dos (`mqtt` y `meshcentral`, 59 y 44
transiciones en 24 h) y mezclarlas contaría en línea a estaciones que el KPI cuenta
caídas — que es justamente el cruce con el que `evaluar_cruce_monitoreo` detecta "el
agente se cayó pero la red vive". El KPI sale de `Estacion.ultimo_heartbeat`, que es el
latido MQTT, así que la serie habla de lo mismo.

**Mismo recorte que `estado_flota`**: solo aprobadas y sin sitios administrativos. Si no,
la curva y el número que tiene encima contarían poblaciones distintas.
"""
from dataclasses import dataclass
from datetime import timedelta

from django.utils import timezone


@dataclass(frozen=True)
class PuntoDisponibilidad:
    """Una hora de la serie."""

    instante: object          # aware, en punto
    en_linea: int
    total: int
    # Cuántas de esas estaciones pertenecen a una farmacia que a esa hora debería estar
    # operando. No cambia el numerador ni el denominador de la curva: sirve para que la
    # pantalla pueda sombrear las horas en que casi todo está cerrado, y así la caída
    # nocturna se lea como lo que es. Ver `fuera_de_horario`.
    esperadas: int

    @property
    def pct(self) -> float:
        return round(self.en_linea * 100 / self.total, 1) if self.total else 0.0

    @property
    def fuera_de_horario(self) -> bool:
        """La mayoría de los locales están cerrados a esta hora.

        Medio aforo y no "ninguno abierto": hay una farmacia que abre 24 horas, así que
        exigir cero dejaría la madrugada entera sin marcar por un solo sitio.
        """
        return self.total > 0 and self.esperadas * 2 < self.total


def serie_disponibilidad(unidades=None, *, horas=24, instante=None) -> list[PuntoDisponibilidad]:
    """Estaciones en línea en cada hora de las últimas `horas`, de la más vieja a ahora.

    `unidades` = iterable de UnidadNegocio, o None para todas. El llamador decide el
    alcance; esta función no lee la sesión ni el request.

    **Tres consultas**, pase lo que pase: las estaciones con su farmacia, su estado MQTT
    actual, y los eventos de la ventana. La reconstrucción es en memoria —son decenas de
    filas, no miles— así que pedir 72 horas en vez de 24 cuesta lo mismo en consultas, y
    hay una prueba que lo fija.
    """
    from apps.catalogo.horarios import farmacia_deberia_estar_abierta
    from apps.catalogo.models import Estacion, Farmacia

    from .models import EstadoDispositivo, EventoMonitoreo

    ahora = (instante or timezone.now()).replace(minute=0, second=0, microsecond=0)
    desde = ahora - timedelta(hours=horas)

    estaciones = (
        Estacion.objects
        .filter(estado_aprobacion=Estacion.EstadoAprobacion.APROBADA)
        .exclude(farmacia__tipo=Farmacia.Tipo.ADMINISTRATIVO)
        .select_related('farmacia')
    )
    if unidades is not None:
        estaciones = estaciones.filter(farmacia__unidad_negocio__in=list(unidades))
    estaciones = list(estaciones)
    if not estaciones:
        return []

    ids = [e.id for e in estaciones]
    actual = dict(
        EstadoDispositivo.objects
        .filter(estacion_id__in=ids, fuente=EstadoDispositivo.Fuente.MQTT)
        .values_list('estacion_id', 'en_linea')
    )

    # Más viejo primero: la reconstrucción avanza con el reloj, aplicando cada transición
    # cuando el punto la alcanza.
    eventos_por_estacion = {}
    for ev in (
        EventoMonitoreo.objects
        .filter(estacion_id__in=ids, fuente=EstadoDispositivo.Fuente.MQTT, timestamp__gte=desde)
        .order_by('timestamp')
        .values('estacion_id', 'timestamp', 'en_linea')
    ):
        eventos_por_estacion.setdefault(ev['estacion_id'], []).append(ev)

    # Estado al principio de la ventana. Si la estación tuvo transiciones, antes de la
    # primera estaba en lo contrario de lo que esa transición declara —son cambios, no
    # muestras—. Si no tuvo ninguna, estuvo todo el rato como está ahora.
    estado = {
        e.id: (not eventos_por_estacion[e.id][0]['en_linea'])
        if eventos_por_estacion.get(e.id) else actual.get(e.id, False)
        for e in estaciones
    }
    siguiente = {e.id: 0 for e in estaciones}

    serie = []
    for paso in range(horas + 1):
        momento = desde + timedelta(hours=paso)
        for e in estaciones:
            pendientes = eventos_por_estacion.get(e.id, ())
            i = siguiente[e.id]
            while i < len(pendientes) and pendientes[i]['timestamp'] <= momento:
                estado[e.id] = pendientes[i]['en_linea']
                i += 1
            siguiente[e.id] = i
        serie.append(PuntoDisponibilidad(
            instante=momento,
            en_linea=sum(1 for e in estaciones if estado[e.id]),
            total=len(estaciones),
            esperadas=sum(
                1 for e in estaciones if farmacia_deberia_estar_abierta(e.farmacia, momento)
            ),
        ))
    return serie
