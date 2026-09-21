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
tópico de acá está en `apps.mqtt_worker.emqx_admin.reglas_acl_estacion`; sin esa regla el
agente se suscribe, no recibe nada, y nada en ningún log lo dice.
"""
import json
import logging

logger = logging.getLogger(__name__)

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
        enviado = _publicar_mqtt(TOPICO_CATALOGO, json.dumps(payload), retain=True)
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
            TOPICO_CATALOGO_EVENTOS, json.dumps({'eventos': eventos}), retain=True,
        )
    except Exception:
        logger.exception('No se pudo publicar el catalogo de eventos de Windows.')
        return len(eventos), False
    if enviado:
        logger.info('Catalogo de eventos de Windows publicado: %d vigilado(s).', len(eventos))
    else:
        logger.warning('El catalogo de eventos no se pudo publicar (broker sin responder).')
    return len(eventos), bool(enviado)
