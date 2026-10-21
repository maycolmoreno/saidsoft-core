"""Cola persistente de órdenes a los agentes (fase 1: PostgreSQL como fuente de verdad,
MQTT como transporte). Modelo: `apps.catalogo.models.OrdenAgente`. Diseño:
docs/consolidacion/PROPUESTA_AGENTE_HTTPS.md §5.

Máquina de estados (TRANSICIONES):

    pendiente ─▶ publicando ─▶ entregada ─▶ recibida ─▶ en_ejecucion ─▶ completada | fallida
        ▲            │  │  │        │           └─────────────┴──────▶ interrumpida
        └─sin conexión┘  │  │        ├─▶ no_entregable
         (despacho)      │  └────────┴─▶ caducada | cancelada   (solo ANTES de recibida)
                         └─▶ cancelada (rechazo confirmado, nunca pudo llegar)
                         └─▶ no_entregable (rechazo, pero un intento anterior quedó incierto)

Qué responde el broker y qué hace la cola (`aplicar_resultados`), igual para los tres tipos
salvo donde se indica:

- ACEPTADO (PUBACK de MQTT 5 con código de éxito) → `entregada`. El dominio registra
  «publicado» recién acá: nunca antes de la aceptación.
- RECHAZADO (PUBACK con código de error: ACL, credencial) → no se reintenta solo, porque un
  rechazo de autorización se repite igual en el intento siguiente.
  · Si la orden nunca pudo llegar (`pudo_llegar` False) → `cancelada`: es SEGURO que el
    agente no la tiene. No bloquea un reintento ni vuelve ambiguos reportes futuros. El
    dominio lo registra como no publicada (software y despliegue: error con el motivo;
    script: error, el contrato de siempre para «no se envió»).
  · Si un intento anterior quedó sin confirmar → `no_entregable`, para revisión: el broker
    rechazó ESTE intento, pero el anterior puede estar retenido.
- SIN CONEXIÓN (no se llegó a publicar) → nada salió.
  · En una publicación pedida por una persona (`encolar_y_publicar`): se cancela todo y la
    excepción sube (`exitoso=False`), el contrato de siempre: a esa persona se le dice que
    falló y decide.
  · En el despacho diferido (`despachar`, sin nadie mirando): vuelve a `pendiente` sin
    contar el intento; es transitorio y la vigencia lo acota.
  · En la recuperación (`revisar_publicando`): sigue `publicando` y se reintenta.
- INCIERTO (salió y no hubo PUBACK: timeout o corte) → sigue `publicando`. NO se afirma que
  el broker la tenga NI que no la tenga (se comprobó en EMQX real que puede haber quedado
  retenida). Software y despliegue: `revisar_publicando` republica el MISMO payload firmado
  hasta MAX_ENTREGAS, y si no se confirma nunca, `no_entregable` + retiro. Scripts: nunca se
  republican; quedan marcados y, pasado su plazo, `no_entregable`.

- `publicando`: la ranura quedó RESERVADA en la base y se intentó publicar, pero todavía no
  hay respuesta del broker, o la respuesta fue incierta, o el proceso murió en el medio. No
  afirma que EMQX la tenga.
- `entregada`: EMQX la ACEPTÓ (PUBACK de MQTT 5 con código de éxito). No dice que la
  estación la recibió: eso es `recibida`, el primer reporte del agente.
- `completada` y `fallida` son definitivas. `interrumpida`, `no_entregable`, `caducada` y
  `cancelada` son finales por inferencia o por decisión del servidor: si después llega
  evidencia del agente (un reporte), manda la evidencia y queda en el historial.

Qué se puede volver a publicar (`repetible`):
- software y despliegues: SÍ, pero solo el MISMO payload firmado (mismo timestamp). El agente
  los descarta si ya los procesó (`_ya_procesado`, id + timestamp). Por eso una orden en
  `publicando` sin respuesta se recupera republicándola tal cual.
- scripts: NUNCA. El agente no deduplica scripts por id; solo los protege una ventana de
  120 s. Si su publicación es incierta, queda para revisión humana.

Ranura retenida: software y despliegue usan un tópico retenido por estación, que guarda solo
la última orden. Como máximo UNA orden `publicando` o `entregada` por estación y canal; lo
garantiza el índice único parcial `orden_agente_una_entregada_por_ranura` de PostgreSQL. Las
demás esperan en `pendiente`.

Procesamiento periódico (`procesar`): una sola pasada a la vez en todo el sistema, con un
advisory lock de PostgreSQL (no una bandera en memoria: tiene que valer entre procesos), y un
tope de filas por fase para que una pasada no se extienda indefinidamente.
"""
import json
import logging
from collections import Counter
from dataclasses import dataclass
from datetime import timedelta

from django.db import DatabaseError, IntegrityError, connection, transaction
from django.db.models import DateTimeField, DurationField, Exists, ExpressionWrapper, F, OuterRef, Q
from django.db.models.expressions import RawSQL
from django.db.models.functions import Coalesce
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from .models import OrdenAgente

logger = logging.getLogger(__name__)

Estado = OrdenAgente.Estado
Tipo = OrdenAgente.Tipo

ACEPTADO, RECHAZADO, INCIERTO = 'aceptado', 'rechazado', 'incierto'

TRANSICIONES = {
    Estado.PENDIENTE: {Estado.PUBLICANDO, Estado.CADUCADA, Estado.CANCELADA, Estado.NO_ENTREGABLE},
    Estado.PUBLICANDO: {
        Estado.ENTREGADA, Estado.PENDIENTE, Estado.NO_ENTREGABLE, Estado.CADUCADA, Estado.CANCELADA,
        # Evidencia: el broker la aceptó y el agente reportó antes de que se guardara el PUBACK.
        Estado.RECIBIDA, Estado.EN_EJECUCION, Estado.COMPLETADA, Estado.FALLIDA,
    },
    Estado.ENTREGADA: {
        Estado.RECIBIDA, Estado.EN_EJECUCION, Estado.COMPLETADA, Estado.FALLIDA,
        Estado.CADUCADA, Estado.CANCELADA, Estado.NO_ENTREGABLE,
    },
    Estado.RECIBIDA: {Estado.EN_EJECUCION, Estado.COMPLETADA, Estado.FALLIDA, Estado.INTERRUMPIDA},
    Estado.EN_EJECUCION: {Estado.COMPLETADA, Estado.FALLIDA, Estado.INTERRUMPIDA},
    # Evidencia tardía: el agente reportó algo de una orden que el servidor ya había cerrado
    # por inferencia. Lo que dice el agente es un hecho; la inferencia, no.
    Estado.INTERRUMPIDA: {Estado.EN_EJECUCION, Estado.COMPLETADA, Estado.FALLIDA},
    Estado.NO_ENTREGABLE: {Estado.RECIBIDA, Estado.EN_EJECUCION, Estado.COMPLETADA, Estado.FALLIDA},
    Estado.CADUCADA: {Estado.RECIBIDA, Estado.EN_EJECUCION, Estado.COMPLETADA, Estado.FALLIDA},
    Estado.CANCELADA: {Estado.RECIBIDA, Estado.EN_EJECUCION, Estado.COMPLETADA, Estado.FALLIDA},
    Estado.COMPLETADA: set(),
    Estado.FALLIDA: set(),
}

# Estados en que la orden todavía puede avanzar sin intervención humana.
VIVOS = {Estado.PENDIENTE, Estado.PUBLICANDO, Estado.ENTREGADA, Estado.RECIBIDA, Estado.EN_EJECUCION}
# Estados en que el agente ya tiene (o puede tener) la orden: no se crea otra igual.
EN_CURSO = {Estado.PUBLICANDO, Estado.ENTREGADA, Estado.RECIBIDA, Estado.EN_EJECUCION}
# Ocupan la ranura retenida: lo que puede estar en el broker esperando a la estación.
OCUPAN_RANURA = {Estado.PUBLICANDO, Estado.ENTREGADA}
# Cerradas por el servidor: si llega un reporte, es evidencia tardía.
CERRADAS_POR_EL_SERVIDOR = {Estado.INTERRUMPIDA, Estado.NO_ENTREGABLE, Estado.CADUCADA, Estado.CANCELADA}
# Inciertas: no se sabe si el agente la ejecutó, ni si todavía va a reportar. Quedan para
# revisión humana y bloquean crear otra orden del mismo origen a la misma estación (ver
# `encolar_y_publicar`): dos órdenes vivas del mismo origen no se pueden distinguir en los
# reportes, porque el agente no informa a qué orden corresponden.
INCIERTAS = {Estado.INTERRUMPIDA, Estado.NO_ENTREGABLE}
FINALES = {Estado.COMPLETADA, Estado.FALLIDA, *CERRADAS_POR_EL_SERVIDOR}

RANURA_RETENIDA = {Tipo.SOFTWARE, Tipo.DESPLIEGUE}

# Módulo de dominio de cada tipo. La cola no conoce los modelos de resultado: les avisa de
# los HECHOS que solo ella ve —el broker aceptó, el broker rechazó, una persona resolvió— y
# cada dominio los registra con sus propios estados y eventos. Un dominio que no define el
# aviso no recibe nada (los scripts registran la publicación en línea, al enviarlos).
DOMINIO = {
    Tipo.SOFTWARE: 'apps.software.services',
    Tipo.DESPLIEGUE: 'apps.despliegues.services',
    Tipo.SCRIPT: 'apps.scripts.services',
}
AVISO_ACEPTADA = 'orden_aceptada_por_el_broker'
AVISO_RECHAZADA = 'orden_rechazada_por_el_broker'
AVISO_RESUELTA = 'orden_resuelta_a_mano'

# Decisiones de una persona al resolver un caso incierto.
RESOLUCIONES = {
    'completada': 'Se verificó que se aplicó / se ejecutó',
    'fallida': 'Se verificó que NO se aplicó / NO terminó bien',
    'desconocido': 'No se pudo determinar: sigue incierta',
}

# Reportes del agente → estado de la orden. Mismos nombres de paso que ya usan
# `software.EventoInstalacion.Paso`, `despliegues.EventoDespliegue.Paso` y
# `scripts.ResultadoEjecucionScript.Estado`: el protocolo del agente no cambia.
PASOS = {
    Tipo.SOFTWARE: {
        'recibido': Estado.RECIBIDA,
        'descargado': Estado.EN_EJECUCION, 'hash_verificado': Estado.EN_EJECUCION,
        'instalando': Estado.EN_EJECUCION,
        'instalado': Estado.COMPLETADA, 'error': Estado.FALLIDA,
    },
    Tipo.DESPLIEGUE: {
        'recibido': Estado.RECIBIDA,
        'descargado': Estado.EN_EJECUCION, 'hash_verificado': Estado.EN_EJECUCION,
        'pos_cerrado': Estado.EN_EJECUCION, 'aplicado': Estado.EN_EJECUCION,
        'pos_relanzado': Estado.EN_EJECUCION,
        'ok': Estado.COMPLETADA, 'error': Estado.FALLIDA, 'rollback': Estado.FALLIDA,
    },
    Tipo.SCRIPT: {
        'ejecutando': Estado.EN_EJECUCION,
        'completado': Estado.COMPLETADA, 'error': Estado.FALLIDA, 'timeout': Estado.FALLIDA,
    },
}

# Plazos. Ninguno es un número mágico de escala: son el orden de magnitud de cada operación.
VIGENCIA = timedelta(days=14)                 # software y despliegue sin recibir
PLAZO_SIN_AVANCE = timedelta(hours=6)         # software y despliegue "inmediato"
PLAZO_SIN_AVANCE_CIERRE_POS = timedelta(hours=72)  # espera a que se cierre el POS
MARGEN_SCRIPT = timedelta(minutes=10)         # sobre el timeout del script
ESPERA_CONFIRMACION = timedelta(minutes=2)    # `publicando` sin resultado guardado
ESPERA_REENTREGA = timedelta(minutes=5)       # latido posterior a la entrega, sin acuse
ESTACION_VIVA = timedelta(minutes=10)         # latido reciente = la estación está encendida
MAX_ENTREGAS = 3                              # publicaciones del mismo payload (sin confirmar o sin acuse)
MAX_HISTORIAL = 50
LIMITE_POR_FASE = 1000                        # filas por fase en cada pasada periódica
# Clave del advisory lock de `procesar` (arbitraria, fija; "SAID" + 1).
CERROJO_PROCESAR = 0x53414901


class TransicionInvalida(ValueError):
    pass


@dataclass
class ResultadoPublicacion:
    """Qué pasó al publicar una operación (solicitud de software, despliegue, reintento).

    `exitoso` conserva su contrato de siempre: False SOLO si no quedó nada en curso (no se
    pudo conectar, o el broker rechazó todo), y entonces el llamador lo muestra como fallo y
    la operación no avanza. True NO significa "todo confirmado": significa que la operación
    quedó registrada y en curso. Lo que el broker confirmó está en los conteos:

    - `aceptadas`: EMQX las aceptó (PUBACK de éxito). Solo estas tienen el evento «publicado».
    - `sin_confirmar`: salieron y no hubo PUBACK (timeout o corte). Pueden estar en el broker
      o no; la cola las republica con el MISMO mensaje firmado, y el dominio registra
      «publicado» recién si el broker lo confirma.
    - `rechazadas`: EMQX las rechazó: no se entregaron.
    - `en_espera`: la estación tenía otra orden del mismo canal sin acuse; salen después.
    - `omitidas`: ya tenían esta orden en curso, o una incierta que espera resolución.
    """
    total_estaciones: int
    exitoso: bool
    aceptadas: int = 0
    sin_confirmar: int = 0
    rechazadas: int = 0
    en_espera: int = 0
    omitidas: int = 0

    @classmethod
    def de_la_cola(cls, total_estaciones, cola):
        return cls(
            total_estaciones=total_estaciones, exitoso=True, aceptadas=len(cola['aceptadas']),
            sin_confirmar=len(cola['sin_confirmar']), rechazadas=len(cola['rechazadas']),
            en_espera=len(cola['en_espera']), omitidas=len(cola['omitidas']) + len(cola['inciertas']),
        )

    @property
    def confirmada(self) -> bool:
        """El broker aceptó todo lo que salió: nada sin confirmar ni rechazado."""
        return self.exitoso and not (self.sin_confirmar or self.rechazadas)

    def conteos(self) -> dict:
        return {
            'aceptadas': self.aceptadas, 'sin_confirmar': self.sin_confirmar, 'rechazadas': self.rechazadas,
            'en_espera': self.en_espera, 'omitidas': self.omitidas,
        }

    def resumen(self) -> str:
        """Texto para el operador, sin presentar lo incierto como publicado."""
        partes = [f'{self.aceptadas} confirmada(s) por el broker']
        if self.sin_confirmar:
            partes.append(f'{self.sin_confirmar} SIN confirmación del broker (no se dan por publicadas; se '
                          'reintentan solas con el mismo mensaje)')
        if self.rechazadas:
            partes.append(f'{self.rechazadas} RECHAZADA(S) por el broker (no se entregaron: revisar credencial o ACL)')
        if self.en_espera:
            partes.append(f'{self.en_espera} en espera (la estación tiene otra orden sin acuse; salen después)')
        if self.omitidas:
            partes.append(f'{self.omitidas} omitida(s) (orden en curso, o incierta pendiente de resolver)')
        return '; '.join(partes) + '.'


def _avisar_dominio(tipo, aviso, *args, **kwargs):
    from importlib import import_module

    funcion = getattr(import_module(DOMINIO[tipo]), aviso, None)
    if funcion is not None:
        funcion(*args, **kwargs)


def _por_tipo(ordenes):
    grupos = {}
    for orden in ordenes:
        grupos.setdefault(orden.tipo, []).append(orden)
    return grupos.items()


# --- Creación y transiciones ---------------------------------------------------------

def plazos_de(tipo, payload: dict, *, timeout_segundos=None, ahora=None):
    """(plazo_sin_avance_s, no_interrumpir_antes_de, expira_en) para una orden nueva."""
    ahora = ahora or timezone.now()
    if tipo == Tipo.SCRIPT:
        plazo = timedelta(seconds=timeout_segundos or 300) + MARGEN_SCRIPT
        return int(plazo.total_seconds()), None, None
    if tipo == Tipo.DESPLIEGUE:
        modo = payload.get('modo_aplicacion')
        ventana = parse_datetime(payload['ventana_fecha_hora']) if payload.get('ventana_fecha_hora') else None
        if modo == 'cierre_pos':
            return int(PLAZO_SIN_AVANCE_CIERRE_POS.total_seconds()), None, ahora + VIGENCIA
        if modo == 'ventana' and ventana is not None:
            expira = max(ahora + VIGENCIA, ventana + timedelta(days=2))
            return int(PLAZO_SIN_AVANCE.total_seconds()), ventana, expira
    return int(PLAZO_SIN_AVANCE.total_seconds()), None, ahora + VIGENCIA


def _entrada(estado, detalle, ahora):
    return {'estado': estado, 'en': ahora.isoformat(), 'detalle': detalle}


def _anotar(orden, estado, detalle='', ahora=None):
    historial = list(orden.historial or [])
    historial.append(_entrada(estado, detalle, ahora or timezone.now()))
    orden.historial = historial[-MAX_HISTORIAL:]


def transicionar(orden, nuevo, *, detalle='', ahora=None, guardar=True, por_evidencia=True) -> bool:
    """Aplica `nuevo` si la tabla lo permite. Devuelve False (sin error) si es el mismo
    estado o una transición que no corresponde: así un reporte duplicado o fuera de orden no
    cambia nada. Quien llama debe tener la fila bloqueada (`select_for_update`)."""
    if nuevo == orden.estado or nuevo not in TRANSICIONES[orden.estado]:
        return False
    ahora = ahora or timezone.now()
    if orden.estado in CERRADAS_POR_EL_SERVIDOR and por_evidencia:
        logger.warning(
            'Orden %s (%s) estaba %s y el agente reportó evidencia: pasa a %s.',
            orden.uid, orden, orden.estado, nuevo,
        )
        detalle = (detalle + ' ' if detalle else '') + f'(evidencia tardía: estaba {orden.estado})'
    if nuevo in (Estado.RECIBIDA, Estado.EN_EJECUCION, Estado.COMPLETADA, Estado.FALLIDA) \
            and orden.estado in (Estado.PUBLICANDO, Estado.ENTREGADA, *CERRADAS_POR_EL_SERVIDOR):
        orden.recibida_en = orden.recibida_en or ahora
    if nuevo == Estado.EN_EJECUCION:
        orden.iniciada_en = orden.iniciada_en or ahora
    if nuevo in FINALES:
        orden.finalizada_en = ahora
    orden.estado = nuevo
    if detalle:
        orden.detalle = detalle
    if nuevo in INCIERTAS:
        orden.requiere_revision = True
        orden.motivo_revision = (detalle or nuevo)[:300]
    _anotar(orden, nuevo, detalle, ahora)
    if guardar:
        orden.save()
    return True


def _mover(pks, *, desde, hacia, detalle, ahora, **campos) -> int:
    """Transición en bloque, en UNA sentencia SQL, solo para las filas que siguen en un
    estado de `desde`: si un reporte concurrente ya movió una fila, esa no se toca. Agrega
    la entrada al historial en la misma sentencia (concatenación de jsonb)."""
    if not pks:
        return 0
    assert all(hacia in TRANSICIONES[d] for d in desde), (desde, hacia)
    cambios = {
        'estado': hacia,
        'historial': RawSQL('historial || %s::jsonb', (json.dumps([_entrada(hacia, detalle, ahora)]),)),
        **campos,
    }
    if detalle:
        cambios['detalle'] = detalle
    if hacia in FINALES:
        cambios['finalizada_en'] = ahora
    if hacia in INCIERTAS:
        cambios['requiere_revision'] = True
        cambios['motivo_revision'] = detalle[:300]
    return OrdenAgente.objects.filter(pk__in=list(pks), estado__in=list(desde)).update(**cambios)


def crear_orden(*, estacion, tipo, origen_id, topico, payload_str='', repetible, ronda,
                creada_por=None, timeout_segundos=None, ahora=None) -> OrdenAgente:
    """Crea la orden en `pendiente`. `ronda` distingue publicaciones sucesivas del mismo
    origen (la primera y cada reintento): con la estación forma la clave de idempotencia,
    así que llamar dos veces con la misma ronda devuelve la orden existente."""
    payload = json.loads(payload_str) if payload_str else {}
    plazo, no_antes, expira = plazos_de(tipo, payload, timeout_segundos=timeout_segundos, ahora=ahora)
    orden, _ = OrdenAgente.objects.get_or_create(
        estacion=estacion, clave_idempotencia=f'{tipo}:{origen_id}:{ronda}',
        defaults={
            'tipo': tipo, 'origen_id': origen_id, 'topico': topico, 'payload': payload_str,
            'repetible': repetible, 'plazo_sin_avance_s': plazo, 'no_interrumpir_antes_de': no_antes,
            'expira_en': expira, 'creada_por': creada_por,
            'historial': [_entrada(Estado.PENDIENTE, '', ahora or timezone.now())],
        },
    )
    return orden


def ultima_orden(estacion, tipo, origen_id):
    return (
        OrdenAgente.objects.filter(estacion=estacion, tipo=tipo, origen_id=origen_id)
        .order_by('-creada_en', '-pk').first()
    )


def ronda_siguiente(estacion, tipo, origen_id) -> int:
    return OrdenAgente.objects.filter(estacion=estacion, tipo=tipo, origen_id=origen_id).count() + 1


def reservar_ranura(orden, *, ahora=None) -> bool:
    """Pasa la orden a `publicando` ANTES de publicarla: la ranura queda reservada en la
    base. Si ya está ocupada, el índice único lo impide y la orden sigue `pendiente`.
    Reservar primero y publicar después es lo que evita que dos publicaciones simultáneas se
    pisen en el broker."""
    ahora = ahora or timezone.now()
    try:
        with transaction.atomic():
            if orden.tipo in RANURA_RETENIDA and OrdenAgente.objects.filter(
                estacion_id=orden.estacion_id, tipo=orden.tipo, estado__in=list(OCUPAN_RANURA),
            ).exclude(pk=orden.pk).exists():
                return False
            orden.entregas += 1
            orden.ultima_entrega_en = ahora
            transicionar(orden, Estado.PUBLICANDO, detalle=f'intento {orden.entregas}', ahora=ahora)
        return True
    except IntegrityError:
        orden.refresh_from_db()
        return False


def aplicar_resultados(ordenes, resultados, *, ahora=None, detalle_dominio=None) -> dict:
    """Lleva a la base lo que respondió el broker para cada orden en `publicando`, con las
    reglas del encabezado del módulo, y avisa al dominio de las aceptadas y de las
    rechazadas que es seguro que no salieron. Todo en una transacción: orden y resultado de
    dominio cambian juntos o no cambia nada.

    Solo se mueven las que SIGUEN en `publicando`: si el agente ya reportó (la orden
    avanzó), el resultado del broker no la mueve para atrás.

    Devuelve `aceptadas`, `rechazadas` e `inciertas`: las órdenes a las que efectivamente
    se les aplicó cada resultado."""
    ahora = ahora or timezone.now()
    por = {ACEPTADO: [], RECHAZADO: [], INCIERTO: []}
    for orden, resultado in zip(ordenes, resultados):
        por[resultado if resultado in por else INCIERTO].append(orden)
    with transaction.atomic():
        previo = dict(
            OrdenAgente.objects.select_for_update()
            .filter(pk__in=[o.pk for o in ordenes], estado=Estado.PUBLICANDO)
            .values_list('pk', 'pudo_llegar')
        )
        aceptadas = [o for o in por[ACEPTADO] if o.pk in previo]
        nunca_salieron = [o for o in por[RECHAZADO] if o.pk in previo and not previo[o.pk]]
        dudosas = [o for o in por[RECHAZADO] if o.pk in previo and previo[o.pk]]
        inciertas = [o for o in por[INCIERTO] if o.pk in previo]
        desde = [Estado.PUBLICANDO]

        _mover([o.pk for o in aceptadas], desde=desde, hacia=Estado.ENTREGADA, ahora=ahora,
               detalle='aceptada por el broker', pudo_llegar=True)
        _mover([o.pk for o in nunca_salieron], desde=desde, hacia=Estado.CANCELADA, ahora=ahora,
               detalle='el broker la rechazó (¿ACL o credencial?): no se entregó; no se reintenta sola')
        _mover([o.pk for o in dudosas], desde=desde, hacia=Estado.NO_ENTREGABLE, ahora=ahora,
               detalle='el broker rechazó este intento, pero uno anterior quedó sin confirmar: puede estar retenida')
        if inciertas:
            entrada = json.dumps([_entrada(Estado.PUBLICANDO, 'sin confirmación del broker', ahora)])
            OrdenAgente.objects.filter(
                pk__in=[o.pk for o in inciertas if o.tipo != Tipo.SCRIPT], estado=Estado.PUBLICANDO,
            ).update(historial=RawSQL('historial || %s::jsonb', (entrada,)), pudo_llegar=True)
            OrdenAgente.objects.filter(
                pk__in=[o.pk for o in inciertas if o.tipo == Tipo.SCRIPT], estado=Estado.PUBLICANDO,
            ).update(
                historial=RawSQL('historial || %s::jsonb', (entrada,)), pudo_llegar=True, requiere_revision=True,
                motivo_revision='publicación del script sin confirmar: puede haberse ejecutado; NO se reintenta',
            )
        for orden in aceptadas + inciertas:
            orden.pudo_llegar = True
        for tipo, grupo in _por_tipo(aceptadas):
            _avisar_dominio(tipo, AVISO_ACEPTADA, grupo, detalle=detalle_dominio)
        for tipo, grupo in _por_tipo(nunca_salieron):
            _avisar_dominio(tipo, AVISO_RECHAZADA, grupo,
                            detalle='No se publicó: EMQX rechazó la publicación (¿ACL o credencial?).')
    return {'aceptadas': aceptadas, 'rechazadas': nunca_salieron + dudosas, 'inciertas': inciertas}


def _cancelar_creadas(creadas, *, detalle, ahora):
    """La publicación inicial falló entera: conserva el contrato de antes (si el broker no
    acepta, la operación no queda en curso)."""
    pks = [o.pk for o in creadas]
    _mover(pks, desde=[Estado.PUBLICANDO, Estado.PENDIENTE], hacia=Estado.CANCELADA, detalle=detalle, ahora=ahora)


def encolar_y_publicar(*, tipo, origen_id, envios, publicar, repetible=True, creada_por=None, ahora=None,
                       detalle_publicado=None):
    """Crea una orden por estación y publica de una vez las que tienen la ranura libre.

    `envios`: lista de (estacion, topico, payload_str). `publicar(mensajes)` es el transporte
    de quien llama: devuelve un resultado por mensaje (`aceptado`/`rechazado`/`incierto`) o
    lanza si no pudo ni conectarse. Se le pasa SOLO lo que sale ahora, en una sola llamada.

    Por estación:
    - si la última orden de ese origen está `pendiente`, se reemplaza: se cancela y se crea
      otra (es la misma orden, con firma más nueva);
    - si está en curso (`publicando`, `entregada`, `recibida`, `en_ejecucion`), se OMITE: el
      agente ya la tiene o puede tenerla, y otra publicación con timestamp nuevo la
      ejecutaría de nuevo;
    - si está incierta (`interrumpida`, `no_entregable`), también se OMITE y va a
      `inciertas`: el agente puede seguir con ella y reportarla, y ese reporte sería
      indistinguible del de la orden nueva. Una persona la resuelve (`resolver_orden`)
      después de mirar la estación, y recién ahí se puede reintentar;
    - si no hay o terminó, se crea una nueva.

    Si la publicación falla entera —no se pudo conectar, o el broker rechazó TODO—, las
    órdenes creadas quedan `cancelada` y la excepción sube: el contrato de siempre (si el
    broker no acepta, la operación no queda en curso, y el dominio no registra nada). Si
    solo algunas fallan, cada una sigue su camino (`aplicar_resultados`), y el dominio
    registra «publicado» solo para las aceptadas, con `detalle_publicado` (por omisión,
    el tópico).

    Devuelve un dict con `aceptadas`, `sin_confirmar` (siguen `publicando`: salieron sin
    PUBACK), `rechazadas` (el broker las rechazó), `en_espera` (ranura ocupada: salen
    después), `omitidas` (orden en curso) e `inciertas` (orden anterior incierta: esperan
    una resolución humana).
    """
    ahora = ahora or timezone.now()
    omitidas, inciertas = [], []
    por_estacion = {estacion.pk: (estacion, topico, payload_str) for estacion, topico, payload_str in envios}

    # Una consulta para todas las estaciones: sus órdenes previas de este origen.
    ultima, rondas = {}, Counter()
    previas = OrdenAgente.objects.filter(
        estacion_id__in=list(por_estacion), tipo=tipo, origen_id=origen_id,
    ).order_by('-creada_en', '-pk')
    for previa in previas:
        rondas[previa.estacion_id] += 1
        ultima.setdefault(previa.estacion_id, previa)

    a_crear, reemplazar = [], []
    for pk, (estacion, topico, payload_str) in por_estacion.items():
        anterior = ultima.get(pk)
        if anterior is not None and anterior.estado in EN_CURSO:
            omitidas.append(estacion)
            continue
        if anterior is not None and anterior.estado in INCIERTAS:
            inciertas.append(estacion)
            continue
        if anterior is not None and anterior.estado == Estado.PENDIENTE:
            reemplazar.append(anterior.pk)
        a_crear.append((estacion, topico, payload_str, rondas[pk] + 1))

    _mover(reemplazar, desde=[Estado.PENDIENTE], hacia=Estado.CANCELADA, ahora=ahora,
           detalle='reemplazada por una publicación nueva')

    # Una consulta: ranuras ocupadas por otra orden del mismo canal todavía sin acuse.
    ocupadas = set()
    if tipo in RANURA_RETENIDA:
        ocupadas = set(OrdenAgente.objects.filter(
            estacion_id__in=[e.pk for e, *_ in a_crear], tipo=tipo, estado__in=list(OCUPAN_RANURA),
        ).values_list('estacion_id', flat=True))

    filas = []
    for estacion, topico, payload_str, ronda in a_crear:
        plazo, no_antes, expira = plazos_de(tipo, json.loads(payload_str) if payload_str else {}, ahora=ahora)
        libre = estacion.pk not in ocupadas
        historial = [_entrada(Estado.PENDIENTE, '', ahora)]
        if libre:
            historial.append(_entrada(Estado.PUBLICANDO, 'intento 1', ahora))
        filas.append(OrdenAgente(
            estacion=estacion, tipo=tipo, origen_id=origen_id, clave_idempotencia=f'{tipo}:{origen_id}:{ronda}',
            topico=topico, payload=payload_str, repetible=repetible, plazo_sin_avance_s=plazo,
            no_interrumpir_antes_de=no_antes, expira_en=expira, creada_por=creada_por,
            estado=Estado.PUBLICANDO if libre else Estado.PENDIENTE, entregas=1 if libre else 0,
            ultima_entrega_en=ahora if libre else None, historial=historial,
        ))
    try:
        # Todas de una vez, ya en `publicando` las de ranura libre. La consulta de arriba es
        # solo una estimación: lo que impide dos reservas en la misma ranura es el índice
        # único, que se evalúa en cada fila de esta inserción. Si otro proceso ocupó una
        # ranura (o creó la misma orden) en el medio, falla la inserción entera y se sigue
        # por el camino de a una, que resuelve cada choque por separado.
        with transaction.atomic():
            creadas = OrdenAgente.objects.bulk_create(filas)
    except IntegrityError:
        logger.warning('Choque al encolar %s #%s en lote: se encola de a una.', tipo, origen_id)
        creadas = []
        for estacion, topico, payload_str, _ in a_crear:
            orden = crear_orden(
                estacion=estacion, tipo=tipo, origen_id=origen_id, topico=topico, payload_str=payload_str,
                repetible=repetible, ronda=ronda_siguiente(estacion, tipo, origen_id), creada_por=creada_por,
                ahora=ahora,
            )
            reservar_ranura(orden, ahora=ahora)
            orden.refresh_from_db()
            creadas.append(orden)
    return _publicar_reservadas(creadas, publicar, ahora=ahora, omitidas=omitidas, inciertas=inciertas,
                                detalle_publicado=detalle_publicado)


def _publicar_reservadas(creadas, publicar, *, ahora, omitidas, inciertas, detalle_publicado):
    reservadas = [o for o in creadas if o.estado == Estado.PUBLICANDO]
    en_espera = [o for o in creadas if o.estado == Estado.PENDIENTE]
    resultados = []
    if reservadas:
        mensajes = [{'topic': o.topico, 'payload': o.payload, 'retain': True} for o in reservadas]
        try:
            resultados = list(publicar(mensajes))
        except Exception:
            _cancelar_creadas(creadas, detalle='no se pudo conectar al broker: nada se publicó', ahora=ahora)
            raise
        if len(resultados) != len(reservadas):
            raise ValueError('el transporte devolvió una cantidad de resultados distinta de los mensajes')
        if RECHAZADO in resultados and ACEPTADO not in resultados and INCIERTO not in resultados:
            from .services import PublicacionRechazada

            _cancelar_creadas(creadas, detalle='el broker rechazó todas las publicaciones', ahora=ahora)
            raise PublicacionRechazada('EMQX rechazó todas las publicaciones (¿ACL o credencial?)')
        aplicado = aplicar_resultados(reservadas, resultados, ahora=ahora, detalle_dominio=detalle_publicado)
    else:
        aplicado = {'aceptadas': [], 'rechazadas': [], 'inciertas': []}
    return {
        'aceptadas': aplicado['aceptadas'], 'sin_confirmar': aplicado['inciertas'],
        'rechazadas': aplicado['rechazadas'], 'en_espera': en_espera,
        'omitidas': omitidas, 'inciertas': inciertas,
    }


# --- Reportes del agente -------------------------------------------------------------

def registrar_reporte(*, estacion, tipo, origen_id, paso, detalle='', ahora=None):
    """Asocia un reporte del agente a su orden y aplica la transición. Devuelve
    (orden, ranura_liberada) — `ranura_liberada` si la orden dejó la ranura retenida y hay
    que despachar la siguiente de esa estación. (None, False) si no hay orden: publicaciones
    anteriores a esta fase, que siguen funcionando sin cola."""
    nuevo = PASOS.get(tipo, {}).get(paso)
    if nuevo is None:
        return None, False
    ahora = ahora or timezone.now()
    with transaction.atomic():
        candidatas = list(
            OrdenAgente.objects.select_for_update()
            .filter(estacion=estacion, tipo=tipo, origen_id=origen_id)
            .order_by('-creada_en', '-pk')
        )
        orden = next((o for o in candidatas if o.estado in VIVOS), None) or (candidatas[0] if candidatas else None)
        if orden is None:
            return None, False
        estaba = orden.estado
        # Correlación sin identificador de orden: el reporte trae solo el origen. Si hay una
        # orden ANTERIOR del mismo origen que el servidor cerró sin evidencia del agente
        # (interrumpida, no entregable, caducada o cancelada) y que PUDO llegar, el agente
        # pudo haberla tenido y este reporte puede ser de ella. No se aplica: la orden viva
        # queda para revisión. Una cerrada que es seguro que nunca salió (rechazo
        # confirmado, sin conexión, cancelada antes de publicarse) no genera ambigüedad.
        anteriores_sin_evidencia = [
            o for o in candidatas
            if o.pk != orden.pk and o.creada_en <= orden.creada_en and o.estado in CERRADAS_POR_EL_SERVIDOR
            and o.pudo_llegar
        ]
        if orden.estado in VIVOS and anteriores_sin_evidencia:
            orden.requiere_revision = True
            orden.motivo_revision = (
                f'reporte "{paso}" ambiguo: puede ser de la orden anterior '
                f'{anteriores_sin_evidencia[0].uid} ({anteriores_sin_evidencia[0].estado})'
            )[:300]
            _anotar(orden, orden.estado, f'NO aplicado: {orden.motivo_revision}', ahora)
            orden.ultimo_reporte_en = ahora
            orden.pudo_llegar = True
            orden.save()
            logger.warning('Orden %s: %s', orden.uid, orden.motivo_revision)
            return orden, False
        orden.ultimo_reporte_en = ahora
        orden.ultimo_paso = paso
        orden.pudo_llegar = True  # el agente la tiene
        cambio = transicionar(orden, nuevo, detalle=detalle, ahora=ahora, guardar=False)
        orden.save()
    if not cambio:
        logger.info('Reporte "%s" de la orden %s sin cambio de estado (%s).', paso, orden.uid, estaba)
    return orden, (cambio and estaba in OCUPAN_RANURA and tipo in RANURA_RETENIDA)


# --- Transporte ----------------------------------------------------------------------

def publicar_lote(mensajes):
    """Transporte de la cola: MQTT 5 en una conexión, un resultado por mensaje (ver
    `apps.catalogo.services._enviar_lote_v5`). Lanza si no se pudo conectar."""
    from .services import publicar_lote as publicar

    return publicar(mensajes)


def retirar_del_broker(orden) -> bool:
    """Borra el retenido de la ranura (payload vacío). MQTT 5 con PUBACK: si EMQX no lo
    acepta —o no hay certeza— devuelve False y la orden NO se da por cancelada ni caducada,
    porque seguiría pudiendo entregarse al reconectar."""
    from .services import _publicar_mqtt

    return _publicar_mqtt(orden.topico, '', retain=True)


def _retirar_lote(ordenes):
    """Retira del broker varias ranuras en una conexión. Devuelve las órdenes cuyo retiro
    EMQX aceptó; las demás siguen como estaban."""
    if not ordenes:
        return []
    try:
        resultados = publicar_lote([{'topic': o.topico, 'payload': '', 'retain': True} for o in ordenes])
    except Exception:
        logger.exception('No se pudieron retirar %d orden(es) del broker', len(ordenes))
        return []
    return [o for o, r in zip(ordenes, resultados) if r == ACEPTADO]


# --- Cancelación y resolución humana -------------------------------------------------

def cancelar_orden(orden, *, usuario=None, motivo='') -> OrdenAgente:
    """Cancela una orden que el agente todavía no recibió. Lanza `TransicionInvalida` si ya
    la recibió (no hay forma de detenerla desde el servidor en esta fase), si es un script
    que ya salió, o si no se pudo retirar del broker."""
    detalle = f'cancelada por {getattr(usuario, "username", "el sistema")}' + (f': {motivo}' if motivo else '')
    with transaction.atomic():
        orden = OrdenAgente.objects.select_for_update().get(pk=orden.pk)
        if orden.estado == Estado.PENDIENTE:
            transicionar(orden, Estado.CANCELADA, detalle=detalle)
        elif orden.estado in OCUPAN_RANURA and orden.tipo in RANURA_RETENIDA:
            if not retirar_del_broker(orden):
                raise TransicionInvalida('No se pudo retirar la orden del broker: sigue como estaba.')
            transicionar(orden, Estado.CANCELADA, detalle=detalle)
        elif orden.estado in OCUPAN_RANURA:
            raise TransicionInvalida('El script ya se envió (o pudo haberse enviado): no se puede retirar.')
        else:
            raise TransicionInvalida(f'La orden está {orden.get_estado_display().lower()}: ya no se puede cancelar.')
    despachar(estacion_ids=[orden.estacion_id])
    return orden


def puede_resolverse(orden) -> bool:
    """Una orden se resuelve a mano si quedó incierta o marcada para revisión, y si desde
    su estado actual se puede llegar a un final. Una pendiente o recién publicada (sin
    marca) no: todavía puede avanzar sola."""
    if not (orden.estado in INCIERTAS or orden.requiere_revision):
        return False
    return bool(TRANSICIONES[orden.estado] & {Estado.COMPLETADA, Estado.FALLIDA})


def resolver_orden(orden, *, resultado, usuario, nota) -> OrdenAgente:
    """Decisión humana sobre un caso incierto, después de mirar la estación (versión del
    POS, software instalado, salida del script). `resultado`:

    - 'completada': se verificó que se aplicó o se ejecutó. La orden pasa a `completada` y
      el resultado de dominio a su estado de éxito, con la nota como evidencia.
    - 'fallida': se verificó que NO se aplicó o no terminó bien. La orden pasa a `fallida`
      y el resultado de dominio a error.
    - 'desconocido': se miró y no se pudo determinar. NO se inventa un resultado: la orden
      sigue en su estado incierto y marcada, el resultado de dominio no se toca, y la
      revisión queda en el historial. Un reintento sigue bloqueado.

    Orden, resultado de dominio y (desde el admin) evento de auditoría van en una sola
    transacción: no quedan a medias.

    Si la orden todavía puede estar retenida en el broker (`publicando`/`entregada` de
    software o despliegue), primero se retira: cerrarla dejando el retenido haría que el
    agente la ejecute al reconectar. Si el retiro no se confirma, no se resuelve.

    Reintentos después: `completada` y `fallida` desbloquean el origen (`encolar_y_publicar`
    vuelve a crear órdenes); qué se reintenta lo decide cada dominio —un despliegue reintenta
    solo los resultados que no quedaron aplicados—. Los scripts nunca se reenvían. Si la
    persona se equivoca y el agente seguía trabajando, un reporte tardío puede caer en la
    orden nueva: sin identificador de orden en el protocolo, no se puede cerrar del lado del
    servidor."""
    if resultado not in RESOLUCIONES:
        raise TransicionInvalida(f"El resultado tiene que ser uno de: {', '.join(RESOLUCIONES)}.")
    if not nota or not nota.strip():
        raise TransicionInvalida('Hace falta una nota con lo que se verificó en la estación.')
    nota = nota.strip()
    quien = getattr(usuario, 'username', '?')
    liberar_ranura = False
    with transaction.atomic():
        orden = OrdenAgente.objects.select_for_update().get(pk=orden.pk)
        if not puede_resolverse(orden):
            raise TransicionInvalida('Solo se resuelven a mano las órdenes inciertas o marcadas para revisión.')
        if resultado == 'desconocido':
            orden.motivo_revision = f'revisada por {quien}, resultado desconocido: {nota}'[:300]
            _anotar(orden, orden.estado, f'revisada por {quien}: no se pudo determinar el resultado: {nota}')
            orden.save()
            return orden
        if orden.estado in OCUPAN_RANURA and orden.tipo in RANURA_RETENIDA:
            if not retirar_del_broker(orden):
                raise TransicionInvalida('No se pudo retirar la orden del broker: no se resuelve (seguiría pudiendo '
                                         'ejecutarse al reconectar).')
            liberar_ranura = True
        nuevo = Estado.COMPLETADA if resultado == 'completada' else Estado.FALLIDA
        detalle = f'resuelta por {quien}: {nota}'
        if not transicionar(orden, nuevo, detalle=detalle, guardar=False, por_evidencia=False):
            raise TransicionInvalida(f'No se puede pasar de {orden.estado} a {nuevo}.')
        orden.requiere_revision = False
        orden.save()
        _avisar_dominio(orden.tipo, AVISO_RESUELTA, orden, decision=resultado,
                        detalle=f'Resolución manual de {quien} (sin reporte del agente): {nota}')
    if liberar_ranura:
        despachar(estacion_ids=[orden.estacion_id])
    return orden


# --- Procesamiento periódico ---------------------------------------------------------

def _estacion_viva(estacion, ahora) -> bool:
    return bool(estacion.ultimo_heartbeat and estacion.ultimo_heartbeat >= ahora - ESTACION_VIVA)


def caducar(ahora=None, limite=LIMITE_POR_FASE) -> int:
    """Órdenes sin recibir cuya vigencia venció. Las pendientes, en bloque. Las que pueden
    estar en el broker (`publicando`/`entregada` de una ranura retenida) se retiran primero:
    si no, se ejecutarían al reconectar. Si el retiro no se confirma, se reintenta."""
    ahora = ahora or timezone.now()
    pendientes = list(OrdenAgente.objects.filter(
        estado=Estado.PENDIENTE, expira_en__lt=ahora,
    ).values_list('pk', flat=True)[:limite])
    total = _mover(pendientes, desde=[Estado.PENDIENTE], hacia=Estado.CADUCADA, ahora=ahora,
                   detalle='venció sin que la estación la recibiera')
    en_broker = list(OrdenAgente.objects.filter(
        estado__in=list(OCUPAN_RANURA), tipo__in=list(RANURA_RETENIDA), expira_en__lt=ahora,
    )[:limite])
    retiradas = _retirar_lote(en_broker)
    total += _mover([o.pk for o in retiradas], desde=list(OCUPAN_RANURA), hacia=Estado.CADUCADA, ahora=ahora,
                    detalle='venció sin que la estación la recibiera; retirada del broker')
    return total


def revisar_publicando(ahora=None, limite=LIMITE_POR_FASE) -> dict:
    """Órdenes en `publicando` sin resultado guardado hace más de ESPERA_CONFIRMACION: el
    PUBACK fue incierto, o el proceso murió entre la reserva y la publicación o entre la
    publicación y el guardado del resultado.

    - Software y despliegue: se REPUBLICA el mismo payload firmado. Si el broker ya lo tenía,
      no cambia nada; si el agente ya lo recibió, lo descarta por id + timestamp. No crea
      otra reserva: es la misma fila.
    - Scripts: nunca se republican. Pasado su plazo sin reportes, `no_entregable` (puede
      haberse ejecutado; queda para revisión).
    """
    ahora = ahora or timezone.now()
    resumen = Counter()
    base = OrdenAgente.objects.filter(estado=Estado.PUBLICANDO, ultima_entrega_en__lt=ahora - ESPERA_CONFIRMACION)

    plazo = ExpressionWrapper(F('plazo_sin_avance_s') * timedelta(seconds=1), output_field=DurationField())
    scripts = list(
        base.filter(tipo=Tipo.SCRIPT)
        .annotate(limite_script=ExpressionWrapper(F('ultima_entrega_en') + plazo, output_field=DateTimeField()))
        .filter(limite_script__lt=ahora).values_list('pk', flat=True)[:limite]
    )
    resumen['scripts_sin_confirmar'] = _mover(
        scripts, desde=[Estado.PUBLICANDO], hacia=Estado.NO_ENTREGABLE, ahora=ahora,
        detalle='publicación sin confirmar y sin reportes del agente: puede haberse ejecutado; NO se reintenta',
        pudo_llegar=True,
    )

    retenidas = list(base.filter(tipo__in=list(RANURA_RETENIDA)).order_by('ultima_entrega_en')[:limite])
    agotadas = [o for o in retenidas if o.entregas >= MAX_ENTREGAS]
    a_republicar = [o for o in retenidas if o.entregas < MAX_ENTREGAS]
    retiradas = _retirar_lote(agotadas)
    resumen['no_entregables'] = _mover(
        [o.pk for o in retiradas], desde=[Estado.PUBLICANDO], hacia=Estado.NO_ENTREGABLE, ahora=ahora,
        detalle=f'{MAX_ENTREGAS} publicaciones sin confirmación del broker; retirada del broker',
        pudo_llegar=True,
    )
    if a_republicar:
        pks = [o.pk for o in a_republicar]
        # Lo que se sabe de un `publicando` vencido es que PUDO salir (sin PUBACK, o el
        # proceso murió después de reservarla): queda anotado antes de republicar, para que
        # un rechazo de esta vuelta no la dé por "nunca salió".
        OrdenAgente.objects.filter(pk__in=pks, estado=Estado.PUBLICANDO).update(
            entregas=F('entregas') + 1, ultima_entrega_en=ahora, pudo_llegar=True,
        )
        for orden in a_republicar:
            orden.entregas += 1
            orden.pudo_llegar = True
        try:
            resultados = publicar_lote([{'topic': o.topico, 'payload': o.payload, 'retain': True} for o in a_republicar])
        except Exception:
            # Sin conexión: esta vuelta no salió y no cuenta. Siguen `publicando`.
            logger.exception('No se pudieron republicar %d orden(es) sin confirmar', len(a_republicar))
            OrdenAgente.objects.filter(pk__in=pks, estado=Estado.PUBLICANDO).update(entregas=F('entregas') - 1)
            return resumen
        aplicado = aplicar_resultados(
            a_republicar, resultados, ahora=ahora,
            detalle_dominio='Publicación confirmada por el broker al recuperarla (mismo mensaje firmado)',
        )
        resumen['republicadas'] = len(a_republicar)
        resumen['confirmadas'] = len(aplicado['aceptadas'])
    return resumen


def revisar_entregadas(ahora=None, limite=LIMITE_POR_FASE) -> dict:
    """Entregadas (aceptadas por el broker) sin acuse del agente.

    - Software y despliegue: si la estación latió después de la entrega y no acusó, se
      vuelve a publicar el MISMO payload (seguro: el agente lo deduplica). Después de
      MAX_ENTREGAS, `no_entregable` y se retira del broker.
    - Scripts: nunca se reenvían. Pasado el plazo, `no_entregable`, aclarando que no se sabe
      si se ejecutó.
    """
    ahora = ahora or timezone.now()
    resumen = {'reentregadas': 0, 'no_entregables': 0}
    sin_acuse = Q(ultimo_reporte_en__isnull=True) | Q(ultimo_reporte_en__lt=F('ultima_entrega_en'))
    plazo = ExpressionWrapper(F('plazo_sin_avance_s') * timedelta(seconds=1), output_field=DurationField())
    entregadas = OrdenAgente.objects.filter(sin_acuse, estado=Estado.ENTREGADA)

    scripts = list(
        entregadas.filter(tipo=Tipo.SCRIPT)
        .annotate(limite_script=ExpressionWrapper(F('ultima_entrega_en') + plazo, output_field=DateTimeField()))
        .filter(limite_script__lt=ahora).values_list('pk', flat=True)[:limite]
    )
    resumen['no_entregables'] += _mover(
        scripts, desde=[Estado.ENTREGADA], hacia=Estado.NO_ENTREGABLE, ahora=ahora,
        detalle='sin reporte del agente: no se sabe si se ejecutó; NO se reintenta sola',
    )

    # La estación latió después de la entrega: estuvo conectada y no acusó.
    retenidas = list(
        entregadas.filter(tipo__in=list(RANURA_RETENIDA),
                          estacion__ultimo_heartbeat__gte=F('ultima_entrega_en') + ESPERA_REENTREGA)
        .order_by('ultima_entrega_en')[:limite]
    )
    agotadas = [o for o in retenidas if o.entregas >= MAX_ENTREGAS]
    a_reentregar = [o for o in retenidas if o.entregas < MAX_ENTREGAS]
    retiradas = _retirar_lote(agotadas)
    resumen['no_entregables'] += _mover(
        [o.pk for o in retiradas], desde=[Estado.ENTREGADA], hacia=Estado.NO_ENTREGABLE, ahora=ahora,
        detalle=f'la estación latió {MAX_ENTREGAS} veces después de la entrega sin acusarla',
    )
    if a_reentregar:
        try:
            resultados = publicar_lote([{'topic': o.topico, 'payload': o.payload, 'retain': True} for o in a_reentregar])
        except Exception:
            logger.exception('No se pudieron reentregar %d orden(es)', len(a_reentregar))
            return resumen
        aceptadas = [o.pk for o, r in zip(a_reentregar, resultados) if r == ACEPTADO]
        entrada = json.dumps([_entrada(Estado.ENTREGADA, 'reentrega aceptada (mismo payload)', ahora)])
        resumen['reentregadas'] = OrdenAgente.objects.filter(pk__in=aceptadas, estado=Estado.ENTREGADA).update(
            entregas=F('entregas') + 1, ultima_entrega_en=ahora,
            historial=RawSQL('historial || %s::jsonb', (entrada,)),
        )
    return resumen


def detectar_interrumpidas(ahora=None, limite=LIMITE_POR_FASE) -> int:
    """Recibidas o en ejecución sin avance durante su plazo, con la estación encendida. El
    agente no informa cuándo se reinicia, así que es una inferencia: si después reporta, la
    evidencia corrige el estado. Nunca se reintentan solas. Un script no se retoma nunca, así
    que pasado su plazo queda incierto aunque la estación esté apagada."""
    ahora = ahora or timezone.now()
    plazo = ExpressionWrapper(F('plazo_sin_avance_s') * timedelta(seconds=1), output_field=DurationField())
    activas = (
        OrdenAgente.objects.filter(estado__in=[Estado.RECIBIDA, Estado.EN_EJECUCION])
        .annotate(limite_avance=ExpressionWrapper(
            Coalesce('ultimo_reporte_en', 'recibida_en', 'ultima_entrega_en') + plazo, output_field=DateTimeField(),
        ))
        .filter(limite_avance__lt=ahora)
        .filter(Q(no_interrumpir_antes_de__isnull=True) | Q(no_interrumpir_antes_de__lte=ahora))
        .filter(Q(tipo=Tipo.SCRIPT) | Q(estacion__ultimo_heartbeat__gte=ahora - ESTACION_VIVA))
        .values_list('pk', flat=True)[:limite]
    )
    return _mover(
        list(activas), desde=[Estado.RECIBIDA, Estado.EN_EJECUCION], hacia=Estado.INTERRUMPIDA, ahora=ahora,
        detalle='sin avance durante su plazo con la estación encendida; reintentar es una decisión humana',
    )


def despachar(estacion_ids=None, ahora=None, limite=LIMITE_POR_FASE) -> int:
    """Publica, por cada ranura retenida libre, la orden pendiente más vieja. Reserva en
    bloque (`publicando`) y publica en una conexión. Si no se pudo conectar, las reservas
    vuelven a `pendiente`: ya fueron autorizadas y se reintentan en la próxima pasada."""
    ahora = ahora or timezone.now()
    # Se descartan en la consulta las que tienen la ranura ocupada. Es solo un filtro: la
    # exclusión real la da el índice único.
    ocupada = OrdenAgente.objects.filter(
        estacion_id=OuterRef('estacion_id'), tipo=OuterRef('tipo'), estado__in=list(OCUPAN_RANURA),
    )
    pendientes = (
        OrdenAgente.objects.filter(estado=Estado.PENDIENTE, tipo__in=list(RANURA_RETENIDA))
        .exclude(expira_en__lt=ahora)
        .exclude(Exists(ocupada))
        .order_by('creada_en', 'pk')
    )
    if estacion_ids is not None:
        pendientes = pendientes.filter(estacion_id__in=estacion_ids)
    elegidas, vistas = [], set()
    for orden in pendientes.iterator(chunk_size=2000):
        ranura = (orden.estacion_id, orden.tipo)
        if ranura in vistas:
            continue
        vistas.add(ranura)
        elegidas.append(orden)
        if len(elegidas) >= limite:
            break
    if not elegidas:
        return 0
    try:
        with transaction.atomic():
            _mover([o.pk for o in elegidas], desde=[Estado.PENDIENTE], hacia=Estado.PUBLICANDO, ahora=ahora,
                   detalle='intento de publicación', entregas=F('entregas') + 1, ultima_entrega_en=ahora)
        reservadas = list(OrdenAgente.objects.filter(pk__in=[o.pk for o in elegidas], estado=Estado.PUBLICANDO,
                                                     ultima_entrega_en=ahora))
    except IntegrityError:
        # Otro proceso reservó alguna de estas ranuras en el medio: de a una, cada choque por
        # separado (el índice único decide).
        reservadas = [o for o in elegidas if reservar_ranura(o, ahora=ahora)]
    if not reservadas:
        return 0
    try:
        resultados = publicar_lote([{'topic': o.topico, 'payload': o.payload, 'retain': True} for o in reservadas])
    except Exception:
        logger.exception('No se pudieron despachar %d orden(es): vuelven a pendiente', len(reservadas))
        _mover([o.pk for o in reservadas], desde=[Estado.PUBLICANDO], hacia=Estado.PENDIENTE, ahora=ahora,
               detalle='no se pudo conectar al broker: nada se publicó', entregas=F('entregas') - 1)
        return 0
    aplicado = aplicar_resultados(
        reservadas, resultados, ahora=ahora, detalle_dominio='Despachada desde la cola: aceptada por el broker',
    )
    return len(aplicado['aceptadas'])


def procesar(ahora=None, limite=LIMITE_POR_FASE) -> dict:
    """Una pasada. La corre `procesar_ordenes_task` cada minuto.

    Una sola a la vez en todo el sistema: `pg_try_advisory_lock` sobre la conexión de esta
    pasada. Si otra pasada lo tiene —porque tarda más que el intervalo o porque hay dos
    celery_worker—, esta no hace nada. Si el proceso que lo tenía muere, PostgreSQL lo
    libera al cerrarse su conexión: no queda un cerrojo huérfano.

    Cada fase procesa como mucho `limite` filas: lo que no entra, va en la pasada siguiente.
    El orden importa: primero se cierra lo vencido, después se recupera lo sin confirmar, y
    al final se despacha lo que quedó libre."""
    ahora = ahora or timezone.now()
    with connection.cursor() as cursor:
        cursor.execute('SELECT pg_try_advisory_lock(%s)', [CERROJO_PROCESAR])
        if not cursor.fetchone()[0]:
            logger.info('Otra pasada de la cola de órdenes está en curso: esta se omite.')
            return {'omitida': True}
    try:
        resumen = {'caducadas': caducar(ahora, limite)}
        resumen.update({f'publicando_{k}': v for k, v in revisar_publicando(ahora, limite).items()})
        resumen.update(revisar_entregadas(ahora, limite))
        resumen['interrumpidas'] = detectar_interrumpidas(ahora, limite)
        resumen['despachadas'] = despachar(ahora=ahora, limite=limite)
        return resumen
    finally:
        try:
            with connection.cursor() as cursor:
                cursor.execute('SELECT pg_advisory_unlock(%s)', [CERROJO_PROCESAR])
        except DatabaseError:
            # La conexión se cayó: al cerrarse, PostgreSQL ya liberó el cerrojo.
            logger.exception('No se pudo liberar el cerrojo de la cola (la conexión se perdió)')
