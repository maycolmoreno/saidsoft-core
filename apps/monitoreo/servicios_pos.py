"""Publicación del catálogo de servicios del POS hacia la flota.

## Por qué MQTT retenido en un tópico global, y no un endpoint HTTP

Se evaluaron los dos caminos que el proyecto ya usa y ganó éste, por tres razones
concretas y no por preferencia:

1. **El catálogo es global, y ya existe el patrón exacto.** Los agentes se suscriben a
   `/saidsof/software/global/` y `/saidsof/despliegue/global/` desde que existen. Un
   catálogo que es uno solo para las ~1.800 estaciones encaja en un tópico global sin
   inventar nada: un publish y lo tienen todas, en vez de un fan-out por estación.

2. **Retenido resuelve el problema de las estaciones apagadas sin código extra.** Es el
   mismo mecanismo que `enviar_actualizacion_agente` (ver `apps.catalogo.services`): EMQX
   guarda el último mensaje del tópico y se lo entrega a cada agente apenas se conecta.
   Las farmacias apagan los equipos al cerrar, así que "todas conectadas al mismo tiempo"
   no pasa nunca. Con HTTP habría que escribir un bucle de polling en el agente, elegir
   un intervalo, y aceptar que el catálogo tarda ese intervalo en llegar.

3. **El agente ya tiene el transporte abierto.** Está conectado a MQTT permanentemente;
   agregar una suscripción son dos líneas. Un endpoint HTTP sumaría un camino de red
   nuevo hacia el servidor —con su autenticación, su manejo de errores y su firma HMAC—
   para distribuir un dato que no es secreto.

El contra honesto: un mensaje retenido no confirma entrega. No se sabe qué estación tiene
qué versión del catálogo. Para esto alcanza —el peor caso es que una caja chequee la
lista vieja unos minutos— y si algún día hace falta saberlo, el agente ya reporta su
`version_agente` en cada latido y se podría sumar la versión del catálogo ahí.

**Ojo con la ACL**: EMQX autoriza por lista explícita y **deniega en silencio**. El
tópico de acá está en `apps.mqtt_worker.emqx_admin._reglas_para`; sin esa regla el
agente se suscribe, no recibe nada, y nada en ningún log lo dice.

## Los dos catálogos van firmados (desde el 11-oct-2026)

Hasta hoy eran los **dos únicos** mensajes dirigidos al agente que viajaban sin firma HMAC
—`RIESGO-06` de la auditoría por agentes, §10-BK—, y el agente los aplicaba directo. El
docstring de arriba justificaba el tópico global y advertía sobre la ACL, pero no sobre
esto: quien alcanzara el tópico dirigía la actividad de red de toda la flota (`urlopen`,
`psycopg2.connect`, `ping`) contra destinos que vienen en el propio mensaje, y leía el
resultado en `/saidsof/agente/{codigo}/servicios_pos/`. Retenido y persistido en el
`identidad.json` del agente, así que sobrevivía al reinicio.

El secreto es el **compartido** de la flota, no el propio de cada estación: es la misma
razón que en los tópicos de difusión de despliegue y software, un solo payload que
verifican muchas estaciones (ver `firmar_payload`).

**Lo que viaja firmado es un string, no un objeto.** El contenido va serializado en la
clave `catalogo` y la firma se calcula sobre ese string exacto. Es deliberado: firmar un
objeto obligaría al agente a re-serializarlo igual que Python para verificar, y
`firmar_payload` existe justamente para no depender de que `json.dumps` y
`System.Text.Json` canonicalicen igual.

Las claves de primer nivel (`servicios`, `criticidad`, `desactivados`, `eventos`) se siguen
mandando **duplicadas** para que los agentes anteriores a **0.33** sigan entendiendo el
mensaje: firmar primero y exigir después, el mismo orden que se usó para el HMAC propio.
El agente 0.33 lee solo de `catalogo`, que es lo firmado; cuando no quede ninguno anterior
en la flota, las claves duplicadas se quitan y queda solo el sobre.

Mientras conviven, un agente viejo sigue aceptando un catálogo forjado: el arreglo completo
es la actualización del agente, y el freno inmediato es
`deploy/emqx-narrow-acl-agente.sh` —con la ACL angosta ninguna estación puede publicar en
estos dos tópicos—.
"""
import hashlib
import json
import logging
import time

logger = logging.getLogger(__name__)


def serializar(contenido: dict) -> str:
    """El texto exacto que viaja en `catalogo` y que se firma."""
    return json.dumps(contenido, separators=(',', ':'), sort_keys=True)


def version_de(cuerpo: str) -> str:
    """Versión de un catálogo: los primeros 12 del SHA-256 de su texto firmado. El agente
    0.38 calcula lo mismo sobre el `catalogo` que aplicó y lo manda en el latido."""
    return hashlib.sha256(cuerpo.encode('utf-8')).hexdigest()[:12]


def envolver_firmado(comando: str, contenido: dict) -> dict:
    """Agrega al catálogo el sobre firmado: `catalogo` (el contenido serializado),
    `timestamp` y `firma`.

    `comando` entra a la firma como en todos los demás mensajes, y acá cumple una función
    extra: impide que un catálogo de servicios válido se reenvíe por el tópico de eventos
    y viceversa. Los dos tópicos llevan la misma ACL, así que sin eso la firma de uno
    valdría para el otro.

    El `timestamp` se firma pero el agente **no** lo valida contra una ventana: estos
    mensajes son retenidos y pueden quedar guardados en el broker días hasta que una
    estación se prenda, igual que `actualizar_agente` (ver `_firma_valida` en el agente,
    `verificar_ventana=False`). Va firmado igual para que no se pueda alterar.
    """
    from apps.catalogo.services import firmar_payload

    from . import firma_catalogo

    cuerpo = serializar(contenido)
    timestamp = int(time.time())
    sobre = {
        # Duplicado a propósito, para los agentes anteriores a 0.22 — ver el docstring
        # del módulo. El agente nuevo lee SOLO de `catalogo`, que es lo que está firmado.
        **contenido,
        'catalogo': cuerpo,
        'timestamp': timestamp,
        'firma': firmar_payload(None, comando=comando, catalogo=cuerpo, timestamp=timestamp),
    }
    # Agente 0.38: firma asimétrica del mismo mensaje (ver apps.monitoreo.firma_catalogo).
    # Los agentes anteriores no la miran y siguen con la HMAC.
    firma_ed25519 = firma_catalogo.firmar(comando, cuerpo, timestamp)
    if firma_ed25519:
        sobre['firma_ed25519'] = firma_ed25519
    return sobre

# Global y no por estación: el catálogo es uno solo para toda la flota (ver el docstring
# del modelo sobre por qué no hay catálogo por farmacia). Mismo nivel que
# /saidsof/software/global/ y /saidsof/despliegue/global/, que ya existen.
TOPICO_CATALOGO = '/saidsof/catalogo/servicios_pos/'


def catalogo_para_agentes() -> list:
    """El catálogo activo, tal como lo consume el agente.

    Solo las entradas con destino fijo: las de origen `config_pos` las descubre el propio
    agente leyendo el `.exe.Config` de su POS, y mandárselas sería decirle algo que ya
    sabe —peor, sin el host, que es justamente lo que varía entre farmacias.

    Lo que sí viaja de TODAS las entradas activas es la criticidad, en `criticidad`: el
    agente la usa para marcar bien los servicios que descubre solo, sin tener que
    redistribuir el ejecutable cada vez que alguien decide que Odoo dejó de ser crítico.
    """
    from .models import ServicioPosMonitoreado

    activos = ServicioPosMonitoreado.objects.filter(activo=True)
    return [
        s.como_lo_lee_el_agente()
        for s in activos.filter(origen=ServicioPosMonitoreado.Origen.CATALOGO)
    ]


def criticidad_para_agentes() -> dict:
    """`{clave: critico}` de todo el catálogo activo, incluidos los descubiertos.

    Separado de la lista de arriba porque responde otra pregunta: no "qué chequear" sino
    "qué tan grave es que esto falle". Cambiar la criticidad de un servicio descubierto
    no requiere que el agente sepa a dónde apuntar.
    """
    from .models import ServicioPosMonitoreado

    return dict(
        ServicioPosMonitoreado.objects.filter(activo=True).values_list('clave', 'critico')
    )


def _claves_desactivadas() -> list:
    """Las claves que el agente debe DEJAR de chequear.

    Van explícitas y no se deducen por ausencia: el agente descubre servicios del
    `.exe.Config` por su cuenta, así que "no está en la lista" significa "no tengo nada
    que decirte sobre esto", no "dejá de mirarlo". Sin esto, desactivar Odoo en el admin
    no apagaría el chequeo en ninguna estación.
    """
    from .models import ServicioPosMonitoreado

    return list(
        ServicioPosMonitoreado.objects.filter(activo=False).values_list('clave', flat=True)
    )


def payload_catalogo() -> dict:
    return {
        'servicios': catalogo_para_agentes(),
        'criticidad': criticidad_para_agentes(),
        'desactivados': _claves_desactivadas(),
    }


def publicar_catalogo_servicios_pos() -> tuple:
    """Publica el catálogo RETENIDO. Devuelve `(cuántos servicios con destino, se envió)`.

    Nunca lanza: que el broker esté caído no puede hacer fallar el guardado en el admin
    ni una migración. Pero SÍ devuelve si se envió, para que quien llama lo pueda decir —
    un guardado que parece exitoso y no llegó a la flota es exactamente el modo de falla
    que este proyecto ya sufrió tres veces este mes.
    """
    from apps.catalogo.services import _publicar_mqtt

    payload = payload_catalogo()
    cuantos = len(payload['servicios'])
    try:
        enviado = _publicar_mqtt(
            TOPICO_CATALOGO,
            json.dumps(envolver_firmado('catalogo_servicios_pos', payload)),
            retain=True,
        )
    except Exception:
        logger.exception('No se pudo publicar el catálogo de servicios del POS.')
        return cuantos, False
    if enviado:
        logger.info(
            'Catálogo de servicios del POS publicado: %d con destino fijo, %d criticidades, '
            '%d desactivado(s).',
            cuantos, len(payload['criticidad']), len(payload['desactivados']),
        )
    else:
        logger.warning('El catálogo de servicios del POS no se pudo publicar (broker sin responder).')
    return cuantos, bool(enviado)


# Topico propio y no una seccion del de servicios: son dos catalogos distintos y meter
# eventos de Windows en un topico llamado `servicios_pos` deja un nombre que miente. El
# costo de separarlos es una regla de ACL y una suscripcion; el de mezclarlos lo paga el
# que lo lea en seis meses.
TOPICO_CATALOGO_EVENTOS = '/saidsof/catalogo/eventos_sistema/'


def catalogo_eventos_para_agentes() -> list:
    """Que eventos del visor de Windows tiene que mirar el agente.

    Viaja lo minimo que el agente necesita para FILTRAR: log, proveedor e id. El nombre,
    la nota y la severidad son para el panel y no le sirven a la estacion.

    **El proveedor no es opcional y omitirlo no falla, falla en silencio.** Paso el
    21-sep-2026: esta funcion mandaba solo `log` e `id`, el agente armaba el
    `Get-WinEvent` sin `ProviderName`, traia los eventos equivocados (`System 55` de
    Kernel-Processor-Power en vez de Ntfs) y `registrar_eventos_sistema` los rechazaba
    todos porque la clave `(log, proveedor, id)` no coincidia. Resultado: cero eventos
    recolectados, cero errores, y nada en ningun log que lo explicara.
    """
    from .models import EventoSistemaVigilado

    return [
        {'log': v.log, 'proveedor': v.proveedor, 'id': v.identificador}
        for v in EventoSistemaVigilado.objects.filter(activo=True).order_by('log', 'identificador')
    ]


def publicar_catalogo_eventos_sistema() -> tuple:
    """Publica RETENIDO el catalogo de eventos. `(cuantos, se_envio)`. Nunca lanza."""
    from apps.catalogo.services import _publicar_mqtt

    eventos = catalogo_eventos_para_agentes()
    try:
        enviado = _publicar_mqtt(
            TOPICO_CATALOGO_EVENTOS,
            json.dumps(envolver_firmado('catalogo_eventos_sistema', {'eventos': eventos})),
            retain=True,
        )
    except Exception:
        logger.exception('No se pudo publicar el catalogo de eventos de Windows.')
        return len(eventos), False
    if enviado:
        logger.info('Catalogo de eventos de Windows publicado: %d vigilado(s).', len(eventos))
    else:
        logger.warning('El catalogo de eventos no se pudo publicar (broker sin responder).')
    return len(eventos), bool(enviado)


def versiones_vigentes() -> dict:
    """La versión que deberían tener aplicada las estaciones de cada catálogo: la de lo
    que se publicaría HOY. Si alguien cambió el catálogo y la publicación falló, todas
    figuran desactualizadas, y es correcto: les falta lo último."""
    return {
        'servicios': version_de(serializar(payload_catalogo())),
        'eventos': version_de(serializar({'eventos': catalogo_eventos_para_agentes()})),
    }


def estado_catalogo(estacion, vigentes=None) -> list:
    """Por catálogo: `(nombre, version_de_la_estacion, al_dia)`; `al_dia` es None si la
    estación no informa versión (agente anterior a 0.38)."""
    vigentes = vigentes or versiones_vigentes()
    filas = []
    for nombre, clave, version in (
        ('Servicios del POS', 'servicios', estacion.catalogo_servicios_version),
        ('Eventos de Windows', 'eventos', estacion.catalogo_eventos_version),
    ):
        filas.append((nombre, version, (version == vigentes[clave]) if version else None))
    return filas
