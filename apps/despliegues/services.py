"""Lógica de publicación de despliegues.

Se invoca desde la vista HTMX del panel (`apps/panel/views/despliegues.py::despliegue_publicar`,
también disponible como acción del admin) cuando un despliegue ya aprobado pasa a
distribuirse. No corre en el worker MQTT de larga duración: esto es una acción puntual
disparada por un humano.
"""
import json
import logging
import time
from dataclasses import dataclass

import paho.mqtt.publish as mqtt_publish
from django.conf import settings
from django.utils import timezone

from apps.catalogo.services import firmar_payload, secreto_de

from .models import Despliegue, EventoDespliegue, ResultadoDespliegue

logger = logging.getLogger(__name__)


@dataclass
class ResultadoPublicacion:
    total_estaciones: int
    exitoso: bool


def _topico_de(estacion) -> str:
    """Tópico propio de la estación, siempre — también para un despliegue a grupo o cadena.

    Antes se publicaba un único mensaje en `/saidsof/despliegue/global/` (o el del grupo o
    el de la farmacia) y lo recibían todas las estaciones suscritas. Eso obligaba a firmar
    con el `COMANDO_HMAC_SECRET` compartido de la flota: un mismo payload lo tiene que
    poder verificar cualquiera de las 700. Y ese secreto compartido es el que hay que
    tipear a mano en el config.txt de cada equipo, y el que impide publicar el instalador
    completo (§10-Z).

    Publicando por estación, cada copia se firma con el secreto de su destinataria y el
    compartido deja de ser necesario. Cuesta un mensaje por estación en vez de uno por
    grupo — a escala de la cadena, ~2100 publicaciones en una sola conexión MQTT.

    Efecto lateral que se gana: los mensajes van retenidos, y un retenido en el tópico de
    un grupo se le entrega a CUALQUIER estación que se suscriba después — una caja nueva
    recibía el despliegue viejo de su grupo al conectarse por primera vez. Con un tópico
    por estación, el retenido de cada una es solo el último que le tocó a ella.
    """
    return f'/saidsof/agente/{estacion.codigo}/despliegue/'


def _payload(despliegue: Despliegue, estacion) -> dict:
    # SEC-1 (auditoría 22-ago-2026): este mensaje le dice al agente qué paquete
    # descargar e instalar sobre el POS — antes no llevaba ninguna firma, así que
    # cualquiera con permiso de publish en el tópico (o que capturara/reenviara un
    # mensaje viejo) podía forzar la instalación de un paquete arbitrario, con el
    # `sha256` puesto por el propio emisor del mensaje (no sirve como prueba de
    # autenticidad, solo detecta corrupción de transporte). No lleva `estacion` como
    # campo: se firmaba un payload único para varias estaciones, y agregarlo ahora
    # rompería la validación de todo agente 0.20 (reconstruye esta lista exacta). Desde
    # el fan-out el vínculo con la estación lo da la firma misma, hecha con su secreto
    # propio. El `timestamp` sí entra para acotar el reenvío de un despliegue viejo
    # (p.ej. un downgrade a una versión vulnerable).
    ventana_fecha_hora = (
        despliegue.ventana_fecha_hora.isoformat() if despliegue.ventana_fecha_hora else None
    )
    timestamp = int(time.time())
    campos = {
        'despliegue_id': despliegue.id,
        'version': despliegue.version,
        # rstrip('/'): archivo.url ya empieza con '/', así que un ARCHIVOS_BASE_URL
        # con barra final daba '//media/...' — que no matchea el patrón de URL y el
        # agente recibía un 404 con el mismo mensaje opaco que cualquier otro fallo
        # de descarga ("no se pudo descargar de ninguna fuente").
        'url': settings.ARCHIVOS_BASE_URL.rstrip('/') + despliegue.archivo.url,
        'sha256': despliegue.sha256,
        'modo_aplicacion': despliegue.modo_aplicacion,
        'ventana_fecha_hora': ventana_fecha_hora,
    }
    # Los campos firmados NO cambian: el agente 0.20 reconstruye exactamente esta lista y
    # tiene que seguir validando. Lo único que cambia es CON QUÉ se firma — el secreto de
    # `estacion` si su agente lo entiende, el compartido si no. Por eso el fan-out no
    # necesita una versión nueva del agente ni un orden de despliegue obligatorio.
    firma = firmar_payload(secreto_de(estacion), comando='desplegar', **campos, timestamp=timestamp)
    return {
        **campos,
        'timestamp': timestamp,
        # El agente intentará descargar del caché de su farmacia antes que del central
        # (best-effort: si el caché no tiene el paquete o falla, cae al central).
        'usar_cache': settings.DESPLIEGUE_USAR_CACHE,
        'firma': firma,
    }


def aprobar_despliegue(*, despliegue: Despliegue, usuario) -> Despliegue:
    """Aprueba `despliegue` aplicando la regla de cuatro ojos. Lanza `ValueError` si no.

    `D-5` de la auditoría por agentes: esta transición vivía en un `elif` de
    `apps/panel/views/despliegues.py`, y con ella la **regla de gobernanza** de que quien
    crea un despliegue no puede aprobarlo (AC-2 de la auditoría del 22-ago-2026). Una regla
    de ese tipo en la vista la saltea cualquier otra vía: un `shell`, un comando, una tarea.

    Mismo contrato que `aperturas.services.aprobar_apertura`, que es el equivalente del otro
    módulo: valida estado, valida cuatro ojos, escribe con `update_fields` y lanza
    `ValueError` con el motivo para que quien llama lo muestre. Los mensajes son
    literalmente los que la vista ya mostraba, para no cambiar lo que ve el operador.

    **Lo que queda afuera a propósito:**

    - El permiso `despliegues.aprobar_despliegue`, que sigue en el decorador de la vista: es
      autorización HTTP, no una regla de dominio. La diferencia importa — sin permiso la
      respuesta es un 403, y esto en cambio es un mensaje de error en la pantalla.
    - `verificar_acceso` por unidad de negocio, por el mismo motivo.
    - La auditoría. Ningún servicio de este módulo registra eventos y la vista ya lo hace con
      su `request` (y por tanto con la IP); moverla acá cambiaría el patrón de la app entera
      y perdería ese dato.
    """
    if despliegue.estado != Despliegue.Estado.PENDIENTE_APROBACION:
        raise ValueError('Este despliegue ya no está pendiente de aprobación.')
    if despliegue.creado_por_id == usuario.id:
        raise ValueError('Quien crea un despliegue no puede aprobarlo (regla de cuatro ojos).')

    despliegue.estado = Despliegue.Estado.APROBADO
    despliegue.aprobado_por = usuario
    despliegue.save(update_fields=['estado', 'aprobado_por'])
    return despliegue


def pausar_despliegue(*, despliegue: Despliegue) -> Despliegue:
    """Pausa un despliegue que está publicando. Lanza `ValueError` si no lo está.

    `D-5`, y además **un solo escritor del estado `PAUSADO`**. Había dos: esta transición en
    `apps/panel/views/despliegues.py` y `evaluar_freno_automatico`. El freno sigue decidiendo
    *cuándo* frenar —umbral de error, `freno_omitido`, su propio log— y delega la escritura
    acá; su comportamiento no cambia porque su condición ya exigía `PUBLICANDO`, que es
    exactamente lo que este servicio valida.

    El `ValueError` lleva el estado actual a propósito: en la vista, "no se pudo pausar" sin
    decir por qué obliga al operador a adivinar si el despliegue ya estaba pausado,
    completado o cancelado.

    No registra auditoría —ningún servicio de este módulo lo hace— ni toca MQTT: pausar es
    solo el estado. Lo que deja de publicarse lo decide `publicar_despliegue` al consultar el
    estado, y los reintentos siguen en `reintentar_despliegue`.
    """
    if despliegue.estado != Despliegue.Estado.PUBLICANDO:
        raise ValueError(
            f'Solo se puede pausar un despliegue que está publicando, y este está '
            f'{despliegue.get_estado_display().lower()}.',
        )
    despliegue.estado = Despliegue.Estado.PAUSADO
    despliegue.save(update_fields=['estado'])
    return despliegue


def marcar_reanudado(*, despliegue: Despliegue) -> Despliegue:
    """Pasa un despliegue pausado a `PUBLICANDO` y marca `freno_omitido`.

    `D-5`. **Solo la transición**: el reenvío por MQTT sigue siendo de
    `reintentar_despliegue`, y quien llama debe invocar esto **después** de que ese reenvío
    tuvo éxito. Esa separación es deliberada — el bug del piloto (6-ago-2026) fue
    exactamente un estado que pasaba a `PUBLICANDO` sin que se reintentara nada, así que un
    servicio que hiciera las dos cosas juntas volvería a poder dejar el estado mintiendo si
    el broker falla a mitad.

    `freno_omitido = True` es la semántica que se conserva: reanudar es una decisión
    consciente del operador sobre errores que ya vio, así que el próximo error no debe
    volver a frenar el despliegue en un bucle (ver `evaluar_freno_automatico`).

    Valida `PAUSADO` como defensa en profundidad: la vista ya lo comprueba antes de
    reenviar, y revalidar acá no cuesta nada.
    """
    if despliegue.estado != Despliegue.Estado.PAUSADO:
        raise ValueError(
            f'Solo se puede reanudar un despliegue pausado, y este está '
            f'{despliegue.get_estado_display().lower()}.',
        )
    despliegue.estado = Despliegue.Estado.PUBLICANDO
    despliegue.freno_omitido = True
    despliegue.save(update_fields=['estado', 'freno_omitido'])
    return despliegue


def publicar_despliegue(despliegue: Despliegue) -> ResultadoPublicacion:
    """Resuelve el destino, crea ResultadoDespliegue por estación y publica por MQTT.

    Si la publicación falla (ej. broker caído), el despliegue se queda en su estado
    actual — no avanza a PUBLICANDO ni se registra ningún EventoDespliegue "publicado".
    Antes esto se registraba igual aunque la publicación hubiera fallado por completo,
    dejando al operador viendo "publicado con éxito" sin que ninguna estación recibiera
    nada. El caller decide qué mostrar/reintentar según `ResultadoPublicacion.exitoso`.
    """
    estaciones = list(despliegue.resolver_estaciones_destino())

    resultados = [
        ResultadoDespliegue(despliegue=despliegue, estacion=estacion, estado=ResultadoDespliegue.Estado.PENDIENTE)
        for estacion in estaciones
    ]
    ResultadoDespliegue.objects.bulk_create(resultados, ignore_conflicts=True)


    mqtt_conf = settings.MQTT_CONFIG
    auth = None
    if mqtt_conf['USERNAME']:
        auth = {'username': mqtt_conf['USERNAME'], 'password': mqtt_conf['PASSWORD']}
    tls = None
    if mqtt_conf['USE_TLS']:
        # EMQX en producción solo expone el listener TLS (8883); sin esto, publish.multiple
        # se conecta en plano y el broker cierra la conexión.
        tls = {'ca_certs': mqtt_conf['CA_CERT'] or None}

    # Un payload por estación: cada uno firmado con el secreto de su destinataria.
    mensajes = [
        {'topic': _topico_de(e), 'payload': json.dumps(_payload(despliegue, e)), 'retain': True}
        for e in estaciones
    ]

    try:
        # Una sola conexión para todos los tópicos del destino (antes: connect+disconnect
        # por tópico vía publish.single() en loop — con un destino de muchas estaciones
        # puntuales, eso bloqueaba la request HTTP con decenas de handshakes secuenciales).
        mqtt_publish.multiple(
            mensajes,
            hostname=mqtt_conf['HOST'],
            port=mqtt_conf['PORT'],
            auth=auth,
            tls=tls,
            client_id=mqtt_conf['CLIENT_ID_PANEL'],
        )
    except Exception:
        logger.exception(
            'No se pudo publicar el despliegue %s por MQTT (%d estación(es) destino)', despliegue.id, len(estaciones),
        )
        return ResultadoPublicacion(total_estaciones=len(estaciones), exitoso=False)

    EventoDespliegue.objects.bulk_create([
        EventoDespliegue(resultado=r, paso=EventoDespliegue.Paso.PUBLICADO, detalle=f'Tópico: {_topico_de(r.estacion)}')
        for r in ResultadoDespliegue.objects.filter(despliegue=despliegue).select_related('estacion')
    ])

    despliegue.estado = Despliegue.Estado.PUBLICANDO
    despliegue.fecha_publicacion = timezone.now()
    despliegue.save(update_fields=['estado', 'fecha_publicacion'])

    return ResultadoPublicacion(total_estaciones=len(estaciones), exitoso=True)


def reintentar_despliegue(despliegue: Despliegue) -> ResultadoPublicacion:
    """Re-publica el paquete solo a las estaciones que todavía no lo aplicaron.

    Se invoca desde `despliegue_reanudar`, que antes solo cambiaba el estado a
    PUBLICANDO sin reenviar nada por MQTT — el operador "reanudaba" un despliegue
    que en realidad nunca reintentaba (encontrado en el primer despliegue real del
    piloto, ML016-A, 6-ago-2026).

    A propósito NO reutiliza `publicar_despliegue`: ese resuelve el destino entero y
    reenviaría el paquete también a las estaciones que ya lo aplicaron con éxito,
    disparando ahí un cierre y reinstalación del POS innecesarios. Acá el destino son
    solo las pendientes. El tópico es el mismo en los dos casos desde el fan-out
    (`_topico_de`), así que la diferencia quedó únicamente en a quiénes se les manda.
    """
    pendientes = list(
        despliegue.resultados.exclude(estado=ResultadoDespliegue.Estado.APLICADO).select_related('estacion'),
    )
    if not pendientes:
        return ResultadoPublicacion(total_estaciones=0, exitoso=True)

    mensajes = [
        {'topic': _topico_de(r.estacion), 'payload': json.dumps(_payload(despliegue, r.estacion)), 'retain': True}
        for r in pendientes
    ]

    mqtt_conf = settings.MQTT_CONFIG
    auth = None
    if mqtt_conf['USERNAME']:
        auth = {'username': mqtt_conf['USERNAME'], 'password': mqtt_conf['PASSWORD']}
    tls = None
    if mqtt_conf['USE_TLS']:
        tls = {'ca_certs': mqtt_conf['CA_CERT'] or None}

    try:
        mqtt_publish.multiple(
            mensajes,
            hostname=mqtt_conf['HOST'],
            port=mqtt_conf['PORT'],
            auth=auth,
            tls=tls,
            client_id=mqtt_conf['CLIENT_ID_PANEL'],
        )
    except Exception:
        logger.exception(
            'No se pudo republicar el despliegue %s (%d estación(es) pendientes)',
            despliegue.id, len(pendientes),
        )
        return ResultadoPublicacion(total_estaciones=len(pendientes), exitoso=False)

    # Vuelven a PENDIENTE para que el próximo freno automático (evaluar_freno_automatico)
    # no las cuente contra el reintento con el % de error de la ronda anterior.
    ResultadoDespliegue.objects.filter(pk__in=[r.pk for r in pendientes]).update(
        estado=ResultadoDespliegue.Estado.PENDIENTE,
    )
    EventoDespliegue.objects.bulk_create([
        EventoDespliegue(resultado=r, paso=EventoDespliegue.Paso.PUBLICADO, detalle='Republicado (reintento manual)')
        for r in pendientes
    ])
    return ResultadoPublicacion(total_estaciones=len(pendientes), exitoso=True)


def publicar_despliegue_a_estacion(despliegue: Despliegue, estacion) -> bool:
    """Envía un despliegue ya existente a UNA estación, por su tópico individual.

    Lo usa `apps.aperturas`: una estación que se enrola en una farmacia que está abriendo
    tiene que recibir el POS, pero el despliegue que lo contiene se publicó antes de que
    esa estación existiera. Publicar de nuevo al tópico de la farmacia reenviaría el
    paquete a las cajas que ya lo aplicaron, disparando ahí un cierre y reinstalación del
    POS innecesarios — el mismo motivo por el que `reintentar_despliegue` publica por
    estación y no por destino agregado.

    No aprueba nada ni cambia el estado del despliegue: solo suma un destinatario a uno
    que ya pasó por su propio control de cuatro ojos.
    """
    resultado, _ = ResultadoDespliegue.objects.get_or_create(
        despliegue=despliegue, estacion=estacion,
        defaults={'estado': ResultadoDespliegue.Estado.PENDIENTE},
    )

    mqtt_conf = settings.MQTT_CONFIG
    auth = None
    if mqtt_conf['USERNAME']:
        auth = {'username': mqtt_conf['USERNAME'], 'password': mqtt_conf['PASSWORD']}
    tls = None
    if mqtt_conf['USE_TLS']:
        tls = {'ca_certs': mqtt_conf['CA_CERT'] or None}

    try:
        mqtt_publish.single(
            f'/saidsof/agente/{estacion.codigo}/despliegue/',
            payload=json.dumps(_payload(despliegue, estacion)),
            retain=True,
            hostname=mqtt_conf['HOST'],
            port=mqtt_conf['PORT'],
            auth=auth,
            tls=tls,
            client_id=mqtt_conf['CLIENT_ID_PANEL'],
        )
    except Exception:
        logger.exception(
            'No se pudo publicar el despliegue %s a la estación %s', despliegue.id, estacion.codigo,
        )
        return False

    EventoDespliegue.objects.create(
        resultado=resultado, paso=EventoDespliegue.Paso.PUBLICADO,
        detalle='Publicado por una apertura (estación recién enrolada)',
    )
    return True


def evaluar_freno_automatico(despliegue: Despliegue) -> bool:
    """Si el % de estaciones en error supera el umbral configurado, pausa el despliegue.

    Devuelve True si se pausó.
    """
    if despliegue.freno_omitido:
        # El operador ya reanudó a pesar de los errores: no volver a frenar.
        return False
    # Solo cuentan las estaciones que ya salieron de PENDIENTE (recibieron el mensaje y
    # empezaron a reportar). Antes el denominador era TODOS los destinatarios: en una
    # publicación amplia (ej. toda la cadena sin pasar por anillos), miles de estaciones
    # que ni siquiera habían descargado el paquete todavía diluían el % de error de las
    # pocas que ya habían fallado, y el freno no llegaba a activarse a tiempo.
    ya_reportaron = despliegue.resultados.exclude(estado=ResultadoDespliegue.Estado.PENDIENTE)
    total = ya_reportaron.count()
    if not total:
        return False
    errores = ya_reportaron.filter(estado=ResultadoDespliegue.Estado.ERROR).count()
    porcentaje = (errores / total) * 100
    if porcentaje >= float(despliegue.umbral_error_pct) and despliegue.estado == Despliegue.Estado.PUBLICANDO:
        # La escritura del estado pasa por `pausar_despliegue` para que haya un solo
        # escritor (`D-5`). No puede lanzar desde acá: la condición de arriba ya exige
        # PUBLICANDO, que es lo único que ese servicio valida. Lo que NO se mueve es la
        # decisión —umbral, `freno_omitido`— ni este log, que es del freno y no de la
        # transición.
        pausar_despliegue(despliegue=despliegue)
        logger.warning(
            'Despliegue %s pausado automáticamente: %.1f%% de error (umbral %.1f%%)',
            despliegue.id, porcentaje, despliegue.umbral_error_pct,
        )
        return True
    return False


def verificar_completado(despliegue: Despliegue) -> bool:
    """Marca el despliegue como completado si ya no quedan estaciones pendientes/en curso."""
    en_curso = despliegue.resultados.exclude(
        estado__in=[ResultadoDespliegue.Estado.APLICADO, ResultadoDespliegue.Estado.ERROR,
                    ResultadoDespliegue.Estado.ROLLBACK],
    ).exists()
    if not en_curso and despliegue.estado == Despliegue.Estado.PUBLICANDO:
        despliegue.estado = Despliegue.Estado.COMPLETADO
        despliegue.save(update_fields=['estado'])
        return True
    return False


# --- Ingesta: lo que reporta el agente por MQTT ---
#
# `apps.mqtt_worker` entrega el paso y los datos crudos; la traduccion a estado, la
# escritura de las dos tablas y el seguimiento posterior son de ACA. Hasta el
# 2-oct-2026 todo eso vivia en `manejar_estado_despliegue`, que para hacerlo tenia una
# constante `_PASO_A_ESTADO` a nivel de modulo: el ciclo de vida de ResultadoDespliegue
# escrito en el worker de transporte.

# Traduce cada paso fino de la linea de tiempo al estado agregado del resultado.
_PASO_A_ESTADO = {
    EventoDespliegue.Paso.RECIBIDO: ResultadoDespliegue.Estado.DESCARGANDO,
    EventoDespliegue.Paso.DESCARGADO: ResultadoDespliegue.Estado.DESCARGADO,
    EventoDespliegue.Paso.HASH_VERIFICADO: ResultadoDespliegue.Estado.VERIFICADO,
    EventoDespliegue.Paso.POS_CERRADO: ResultadoDespliegue.Estado.APLICANDO,
    EventoDespliegue.Paso.APLICADO: ResultadoDespliegue.Estado.APLICANDO,
    EventoDespliegue.Paso.POS_RELANZADO: ResultadoDespliegue.Estado.APLICANDO,
    EventoDespliegue.Paso.OK: ResultadoDespliegue.Estado.APLICADO,
    EventoDespliegue.Paso.ERROR: ResultadoDespliegue.Estado.ERROR,
    EventoDespliegue.Paso.ROLLBACK: ResultadoDespliegue.Estado.ROLLBACK,
}


def paso_valido(paso) -> bool:
    """Si `paso` es uno de los que entiende la linea de tiempo.

    Lo expone este modulo y no el worker: que pasos existen es del dominio, y el
    transporte solo tiene que poder descartar un mensaje mal formado sin conocerlos.
    """
    return paso in EventoDespliegue.Paso.values


#: Pasos con los que la estación demuestra que ya tiene la orden. Con cualquiera de
#: ellos el worker encola `retirar_retenido_despliegue_task`. `recibido` alcanza: el
#: agente marca el despliegue como procesado al recibirlo, antes de aplicarlo
#: (`agente_prueba._manejar_despliegue`), así que volver a entregárselo no sirve de
#: nada. Los finales están por si el `recibido` se perdió en el camino.
PASOS_QUE_CONFIRMAN_RECEPCION = frozenset({
    EventoDespliegue.Paso.RECIBIDO, EventoDespliegue.Paso.OK,
    EventoDespliegue.Paso.ERROR, EventoDespliegue.Paso.ROLLBACK,
})


def retenido_ya_entregado(estacion) -> bool:
    """True si la estación ya acusó recibo de todos sus despliegues.

    El tópico retenido es uno por estación y guarda solo la ÚLTIMA orden publicada, pero
    el servidor no registra cuál fue. Por eso no basta con que el despliegue que acaba de
    reportar esté terminado: si después se publicó otro a la misma estación, el retenido
    es ese otro. Lo que sí se puede afirmar es esto: si ningún resultado de la estación
    sigue en PENDIENTE o PUBLICADO, todo lo que se le publicó ya lo recibió, sea cual sea
    el que quedó retenido.

    Es conservador a propósito: un resultado que quedó pendiente para siempre (una
    publicación que falló, una orden que otra más nueva pisó antes de que la estación se
    conectara) impide retirar el retenido. Eso deja las cosas como estaban antes de esta
    limpieza; lo contrario podría borrar una orden que la estación todavía no recibió.
    """
    return not ResultadoDespliegue.objects.filter(
        estacion=estacion,
        estado__in=[ResultadoDespliegue.Estado.PENDIENTE, ResultadoDespliegue.Estado.PUBLICADO],
    ).exists()


def retirar_retenido(estacion) -> bool:
    """Borra el despliegue retenido en el tópico de `estacion` (payload vacío con
    retain=True). Publica con MQTT 5 y lee el PUBACK (`_publicar_mqtt`): un rechazo de
    EMQX devuelve False en vez de pasar por éxito. Lo llama la tarea, que corre con la
    credencial del panel; la del worker no puede publicar en este tópico."""
    from apps.catalogo.services import _publicar_mqtt

    return _publicar_mqtt(_topico_de(estacion), '', retain=True)


def registrar_estado_de_estacion(*, despliegue_id, estacion, paso, datos: dict | None = None):
    """Aplica a `ResultadoDespliegue` el paso que reporto una estacion y deja su evento.

    Devuelve el resultado, o None si el paso no es valido. Dispara ademas el freno
    automatico y la verificacion de completado, que son seguimiento de ESTE dominio.

    Lo que NO hace, a proposito: tocar `Estacion`, facturacion ni el estado de
    dispositivo. Que un despliegue termine OK tambien significa "esta caja esta viva y
    corre tal version del POS", pero eso es una conclusion que cruza tres dominios y la
    sigue coordinando el worker. Traerla aca haria que `despliegues` importara
    `facturacion` y `monitoreo`, acoplamiento que hoy no existe — se gana encapsular una
    tabla y se pierde mas de lo que se gana.

    `datos` son los campos crudos del payload (version_previa, version_nueva, detalle).
    """
    datos = datos or {}
    if not paso_valido(paso):
        return None

    resultado, _ = ResultadoDespliegue.objects.get_or_create(
        despliegue_id=despliegue_id, estacion=estacion,
    )
    if paso == EventoDespliegue.Paso.POS_CERRADO:
        resultado.version_previa = datos.get('version_previa', resultado.version_previa)
    if paso == EventoDespliegue.Paso.OK:
        resultado.version_nueva = datos.get('version_nueva', resultado.version_nueva)

    nuevo_estado = _PASO_A_ESTADO.get(paso)
    if nuevo_estado:
        resultado.estado = nuevo_estado
    if paso == EventoDespliegue.Paso.ERROR:
        resultado.detalle_error = datos.get('detalle', '')
    resultado.save()

    EventoDespliegue.objects.create(resultado=resultado, paso=paso, detalle=datos.get('detalle', ''))

    evaluar_freno_automatico(resultado.despliegue)
    verificar_completado(resultado.despliegue)
    return resultado
