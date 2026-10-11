"""Cola unificada de incidencias: una farmacia, una fila, ordenada por impacto.

Existe porque una farmacia con el enlace caído aparecía TRES veces en la misma pantalla
por un único hecho: el evento de enlace, una alerta `sin_heartbeat` por cada caja del
sitio (las abre `marcar_estaciones_offline`), y filas en "estaciones sin reportar". El
operador veía el mismo incidente repetido en tres bloques y no tenía forma de saber si
"27 advertencias" eran 27 sitios o uno solo con 27 síntomas — ver el apartado D de
docs/auditoria/centro-monitoreo/04-riesgos-del-encargo.md.

Módulo propio y no una función más en `services.py` por el mismo motivo que
`enlaces.py` y `salud.py`: es un concepto con sus propias reglas de prioridad, y
`services.py` ya tiene 1.600 líneas de cosas que no se parecen a esto.

No se pierde información: el síntoma de mayor impacto se muestra entero, los demás se
cuentan, y cada fila enlaza a la pantalla del dominio donde está el detalle y la acción.
"""
from django.utils import timezone

# Prioridad por IMPACTO OPERATIVO, no por severidad técnica. El orden sale de qué deja de
# poder hacer la farmacia, que es la pregunta que el técnico responde al elegir a dónde ir:
#
#   1. sin enlace          -> la farmacia no vende nada.
#   2. POS crítico caído   -> la caja no puede vender aunque haya red.
#   3. alerta crítica      -> algo se degrada y alguien definió una regla para eso.
#   4. POS no crítico      -> la caja sigue vendiendo (recargas, SRI...).
#   5. advertencia         -> hay que mirarlo, no hay que correr.
#
# Que una alerta sea CRITICAL **no** significa impacto confirmado: es una condición
# técnica que alguien configuró. El único dato con semántica de negocio en todo el sistema
# es `EstadoServicioPos.critico`, cuya etiqueta en el Centro es literalmente "la caja no
# puede vender", y por eso va por encima de cualquier alerta.
PRIORIDAD_SIN_ENLACE = 1
PRIORIDAD_POS_CRITICO = 2
PRIORIDAD_ALERTA_CRITICA = 3
PRIORIDAD_POS_NO_CRITICO = 4
PRIORIDAD_ADVERTENCIA = 5

_ESTADO_POR_PRIORIDAD = {
    PRIORIDAD_SIN_ENLACE: 'critical',
    PRIORIDAD_POS_CRITICO: 'critical',
    PRIORIDAD_ALERTA_CRITICA: 'critical',
    PRIORIDAD_POS_NO_CRITICO: 'warning',
    PRIORIDAD_ADVERTENCIA: 'warning',
}


class IncidenciaFarmacia:
    """Una farmacia con problemas, con su síntoma de mayor impacto al frente."""

    __slots__ = ('farmacia', 'prioridad', 'titulo', 'detalle', 'desde', 'sintomas', 'en_atencion')

    def __init__(self, farmacia, prioridad, titulo, detalle, desde, en_atencion=False):
        self.farmacia = farmacia
        self.prioridad = prioridad
        self.titulo = titulo
        self.detalle = detalle
        self.desde = desde
        self.sintomas = 1
        self.en_atencion = en_atencion

    @property
    def estado(self) -> str:
        """El vocabulario de `panel/_estado_chip.html`: color, forma y palabra."""
        return _ESTADO_POR_PRIORIDAD[self.prioridad]

    @property
    def otros_sintomas(self) -> int:
        return self.sintomas - 1

    def sumar(self, prioridad, titulo, detalle, desde, en_atencion=False):
        """Agrega un síntoma más del mismo sitio, quedándose con el de mayor impacto.

        A igual prioridad gana el MÁS VIEJO y no el más nuevo: es el que lleva más tiempo
        sin resolverse, y es el que ordena la cola.
        """
        self.sintomas += 1
        gana = prioridad < self.prioridad or (
            prioridad == self.prioridad
            and desde is not None
            and (self.desde is None or desde < self.desde)
        )
        if gana:
            self.prioridad = prioridad
            self.titulo = titulo
            self.detalle = detalle
            self.desde = desde
            self.en_atencion = en_atencion

    def __repr__(self):  # pragma: no cover - ayuda al depurar, no es salida de producto
        return '<IncidenciaFarmacia %s p%d %r>' % (
            getattr(self.farmacia, 'codigo', '?'), self.prioridad, self.titulo,
        )


def _detalle_enlace(evento, proveedor: str) -> str:
    """Proveedor y circuito de una caída: lo que el técnico necesita para abrir el ticket.

    El circuito sale de la copia del EVENTO si la tiene, y si no de la farmacia viva. No
    es indecisión: son dos datos con propósitos distintos.

    `EventoEnlaceFarmacia.circuito_proveedor` es una copia tomada al momento de la caída,
    para que el evento siga sirviendo si la farmacia cambia de circuito después. Pero una
    caída abierta ANTES de que el circuito se cargara en la farmacia tiene esa copia
    vacía — y eso dejó de ser hipotético el 3-oct-2026, cuando se importaron 702 circuitos
    de golpe desde el Excel de operaciones. Sin el respaldo, el técnico leería "circuito
    sin cargar" con el dato ya disponible a un clic.

    Vive en Python y no en la plantilla porque `nombre_proveedor` recibe un argumento y
    una plantilla de Django no puede llamarla. La lógica es la que ya estaba en
    `dashboard.html`, movida tal cual.
    """
    circuito = evento.circuito_proveedor or evento.farmacia.circuito_proveedor
    return '{} · {}'.format(proveedor, circuito or 'circuito sin cargar')


def cola_incidencias(unidades=None, limite: int | None = 12) -> list:
    """Lo que duele ahora, una fila por farmacia, por impacto y antigüedad.

    `unidades` = iterable de UnidadNegocio, o None para todas. El llamador decide el
    alcance; esta función no lee la sesión ni el request, igual que `resumen_operacion`.

    `limite` acota las filas devueltas. Con `None` devuelve todas, que es lo que necesita
    quien quiera contar cuántas farmacias hay en la cola sin traerlas dos veces.

    Tres consultas, una por fuente, con `select_related` puesto. Agrupar en Python sobre
    unos cientos de filas sale más barato que un UNION de tres caminos sobre las tablas
    grandes, y además deja que cada fuente conserve sus propias exclusiones —farmacias
    dadas de baja, las que nunca respondieron, servicios que nunca contestaron— sin
    reescribirlas en una consulta nueva que se desincronizaría.
    """
    from .enlaces import nombre_proveedor
    from .models import Alerta, EstadoServicioPos, EventoEnlaceFarmacia, ReglaAlerta
    from .services import _por_unidad

    por_farmacia: dict[int, IncidenciaFarmacia] = {}

    def registrar(farmacia, prioridad, titulo, detalle, desde, en_atencion=False):
        existente = por_farmacia.get(farmacia.pk)
        if existente is None:
            por_farmacia[farmacia.pk] = IncidenciaFarmacia(
                farmacia, prioridad, titulo, detalle, desde, en_atencion,
            )
        else:
            existente.sumar(prioridad, titulo, detalle, desde, en_atencion)

    # 1. Enlaces caídos. Las tres exclusiones son las mismas que ya aplican
    # `resumen_operacion`, el Centro y la lista del tablero, para que los números no
    # puedan discrepar: caída abierta, farmacia activa, y nunca-respondió excluido.
    eventos = (
        _por_unidad(
            EventoEnlaceFarmacia.objects
            .filter(fin__isnull=True, farmacia__activa=True)
            .exclude(farmacia__estado_enlace__respondio_alguna_vez=False),
            unidades, 'farmacia__unidad_negocio',
        )
        .select_related('farmacia')
        .order_by('inicio')
    )
    for evento in eventos:
        registrar(
            evento.farmacia, PRIORIDAD_SIN_ENLACE, 'Sin enlace',
            _detalle_enlace(evento, nombre_proveedor(evento.farmacia)), evento.inicio,
        )

    # 2 y 4. Servicios del POS que no responden. `ultima_respuesta__isnull=False` excluye
    # los que nunca contestaron: configuración pendiente, no una caída.
    servicios = (
        _por_unidad(
            EstadoServicioPos.objects.filter(disponible=False, ultima_respuesta__isnull=False),
            unidades, 'estacion__farmacia__unidad_negocio',
        )
        .select_related('estacion', 'estacion__farmacia')
    )
    for servicio in servicios:
        critico = servicio.critico
        registrar(
            servicio.estacion.farmacia,
            PRIORIDAD_POS_CRITICO if critico else PRIORIDAD_POS_NO_CRITICO,
            'La caja no puede vender' if critico else 'Servicio del POS sin responder',
            '{} · {}'.format(servicio.get_servicio_display(), servicio.estacion.codigo),
            servicio.ultima_respuesta,
        )

    # 3 y 5. Alertas. Entran también las RECONOCIDAS, marcadas como "en atención": una
    # alerta que alguien ya está atendiendo sigue siendo un problema del sitio, y
    # esconderla haría que la cola mostrara menos farmacias afectadas de las que hay. Lo
    # que cambia es la etiqueta, no la presencia.
    alertas = (
        _por_unidad(
            Alerta.objects.filter(estado__in=[Alerta.Estado.ABIERTA, Alerta.Estado.RECONOCIDA]),
            unidades, 'estacion__farmacia__unidad_negocio',
        )
        .select_related('regla', 'estacion', 'estacion__farmacia')
        .order_by('abierta_en')
    )
    for alerta in alertas:
        critica = alerta.regla.severidad == ReglaAlerta.Severidad.CRITICAL
        registrar(
            alerta.estacion.farmacia,
            PRIORIDAD_ALERTA_CRITICA if critica else PRIORIDAD_ADVERTENCIA,
            alerta.regla.nombre,
            alerta.estacion.codigo,
            alerta.abierta_en,
            en_atencion=alerta.estado == Alerta.Estado.RECONOCIDA,
        )

    # Impacto primero; a igual impacto, la más vieja. `desde` en None va al final de su
    # grupo en vez de hacer fallar la comparación: hoy ninguna fuente lo produce, pero el
    # contrato no lo garantiza y un TypeError acá tiraría el tablero entero.
    ahora = timezone.now()
    orden = sorted(
        por_farmacia.values(),
        key=lambda i: (i.prioridad, i.desde is None, i.desde or ahora),
    )
    return orden[:limite] if limite else orden
