"""Lógica de publicación de solicitudes de instalación de software.

Mismo patrón que apps.despliegues.services.publicar_despliegue — incluyendo las dos
correcciones que le aplicamos ahí: una sola conexión MQTT (publish.multiple, no
publish.single en loop) y no registrar el evento "publicado" ni avanzar el estado si
la publicación falla por completo (antes eso generaba auditoría falsa en despliegues).
"""
import json
import logging
import time
from dataclasses import dataclass
from datetime import timedelta

import paho.mqtt.publish as mqtt_publish
from django.conf import settings
from django.utils import timezone

from apps.catalogo.services import firmar_payload, secreto_de

from .models import EstadoSolicitud, EventoInstalacion, ResultadoInstalacion, SolicitudInstalacion

logger = logging.getLogger(__name__)


@dataclass
class ResultadoPublicacion:
    total_estaciones: int
    exitoso: bool


def _topico_de(estacion) -> str:
    """Tópico propio de la estación, siempre. Mismo fan-out y mismos motivos que
    `apps.despliegues.services._topico_de`: publicar por estación es lo que permite
    firmar cada copia con el secreto de su destinataria y dejar de depender del
    `COMANDO_HMAC_SECRET` compartido de la flota.
    """
    return f'/saidsof/agente/{estacion.codigo}/software/'


def _payload(solicitud: SolicitudInstalacion, estacion) -> dict:
    # SEC-1 (auditoría 22-ago-2026): mismo hueco y mismo fix que apps.despliegues.services
    # ._payload — este mensaje también le dice al agente qué instalador correr, y no
    # llevaba ninguna firma. Ver ese docstring para el razonamiento completo.
    va = solicitud.version_aplicacion
    timestamp = int(time.time())
    campos = {
        'solicitud_id': solicitud.id,
        'aplicacion': va.aplicacion.nombre,
        'version': va.version,
        'accion': solicitud.accion,
        # rstrip('/') por el mismo motivo que en apps/despliegues/services.py: con
        # barra final quedaba '//media/...' y el agente recibía un 404.
        'url': settings.ARCHIVOS_BASE_URL.rstrip('/') + va.instalador.url,
        'sha256': va.sha256,
        'comando_instalacion_silenciosa': va.comando_instalacion_silenciosa,
        'comando_desinstalacion': va.comando_desinstalacion,
        'argumentos_adicionales': va.argumentos_adicionales,
        'comando_deteccion': va.aplicacion.comando_deteccion,
    }
    # Campos firmados sin cambios (el agente 0.20 reconstruye esta lista exacta);
    # cambia solo el secreto. Ver apps.despliegues.services._payload.
    firma = firmar_payload(secreto_de(estacion), comando='instalar_software', **campos, timestamp=timestamp)
    return {
        **campos,
        'timestamp': timestamp,
        # Mismo mecanismo de caché por farmacia que ya usan los despliegues: clave para
        # no saturar el enlace de datos con instaladores pesados hacia 600 farmacias.
        'usar_cache': settings.DESPLIEGUE_USAR_CACHE,
        'firma': firma,
    }


def publicar_solicitud(solicitud: SolicitudInstalacion) -> ResultadoPublicacion:
    """Resuelve el destino, crea ResultadoInstalacion por estación y publica por MQTT.

    Si la publicación falla (ej. broker caído), la solicitud se queda en su estado
    actual — no avanza a PUBLICANDO ni se registra ningún EventoInstalacion "publicado".
    """
    estaciones = list(solicitud.resolver_estaciones_destino())

    resultados = [
        ResultadoInstalacion(solicitud=solicitud, estacion=estacion, estado=ResultadoInstalacion.Estado.PENDIENTE)
        for estacion in estaciones
    ]
    ResultadoInstalacion.objects.bulk_create(resultados, ignore_conflicts=True)


    mqtt_conf = settings.MQTT_CONFIG
    auth = None
    if mqtt_conf['USERNAME']:
        auth = {'username': mqtt_conf['USERNAME'], 'password': mqtt_conf['PASSWORD']}
    tls = None
    if mqtt_conf['USE_TLS']:
        tls = {'ca_certs': mqtt_conf['CA_CERT'] or None}

    # Un payload por estación, firmado con el secreto de cada una.
    mensajes = [
        {'topic': _topico_de(e), 'payload': json.dumps(_payload(solicitud, e)), 'retain': True}
        for e in estaciones
    ]

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
            'No se pudo publicar la solicitud de instalación %s por MQTT (%d estación(es) destino)',
            solicitud.id, len(estaciones),
        )
        return ResultadoPublicacion(total_estaciones=len(estaciones), exitoso=False)

    EventoInstalacion.objects.bulk_create([
        EventoInstalacion(
            resultado=r, paso=EventoInstalacion.Paso.PUBLICADO, detalle=f'Tópico: {_topico_de(r.estacion)}',
        )
        for r in ResultadoInstalacion.objects.filter(solicitud=solicitud).select_related('estacion')
    ])

    solicitud.estado = EstadoSolicitud.PUBLICANDO
    solicitud.fecha_publicacion = timezone.now()
    solicitud.save(update_fields=['estado', 'fecha_publicacion'])

    return ResultadoPublicacion(total_estaciones=len(estaciones), exitoso=True)


def verificar_completado(solicitud: SolicitudInstalacion) -> bool:
    """Marca la solicitud como completada si ya no quedan estaciones pendientes/en curso."""
    en_curso = solicitud.resultados.exclude(
        estado__in=[ResultadoInstalacion.Estado.INSTALADO, ResultadoInstalacion.Estado.ERROR],
    ).exists()
    if not en_curso and solicitud.estado == EstadoSolicitud.PUBLICANDO:
        solicitud.estado = EstadoSolicitud.COMPLETADO
        solicitud.save(update_fields=['estado'])
        return True
    return False


def generar_escaneo_programado(*, programado) -> int:
    """Dispara "consultar_software_instalado" a cada estación resuelta de un
    InventarioProgramado vencido, y avanza sus fechas. Mismo patrón que
    apps.scripts.services.generar_ejecucion_programada, pero sin crear un registro de
    "ejecución" intermedio: no hay Script de por medio, solo el comando fijo — el
    resultado de cada escaneo llega solo (o no) por el canal ya existente
    (manejar_software_instalado) cuando cada agente responda.

    Devuelve la cantidad de estaciones a las que se les envió el comando (no confirma
    que lo hayan recibido — igual que enviar_comando en general).
    """
    from apps.catalogo.services import enviar_comando, resolver_estaciones

    estaciones = resolver_estaciones(
        programado.destino_tipo, unidad_negocio=programado.unidad_negocio,
        grupos=programado.grupos.all(), farmacias=programado.farmacias.all(),
        estaciones=programado.estaciones.all(),
    )
    enviados = 0
    for estacion in estaciones:
        if enviar_comando(estacion, 'consultar_software_instalado'):
            enviados += 1

    # localdate(), igual que en apps.scripts.services: en UTC la proxima corrida
    # quedaba un dia corrida despues de las 19:00 hora local.
    hoy = timezone.localdate()
    programado.fecha_ultima_ejecucion = hoy
    programado.fecha_proxima_ejecucion = hoy + timedelta(days=programado.frecuencia_dias)
    programado.save(update_fields=['fecha_ultima_ejecucion', 'fecha_proxima_ejecucion'])
    return enviados


def generar_escaneos_vencidos() -> int:
    """Recorre los InventarioProgramado vencidos (fecha_proxima_ejecucion <= hoy) y
    dispara el escaneo de cada uno. La llaman tanto el comando manual
    (`generar_escaneos_programados`) como la tarea periódica de Celery."""
    from django.db import transaction

    from apps.auditoria.models import registrar_evento

    from .models import InventarioProgramado

    with transaction.atomic():
        hoy = timezone.localdate()
        vencidos = InventarioProgramado.objects.filter(activo=True, fecha_proxima_ejecucion__lte=hoy)
        total = 0
        for programado in vencidos:
            enviados = generar_escaneo_programado(programado=programado)
            registrar_evento(
                usuario=programado.creado_por, accion='inventario_programado.disparar', objeto=programado,
                detalle={'estaciones_notificadas': enviados},
            )
            total += 1
    return total


def estaciones_desactualizadas(aplicacion):
    """QuerySet de SoftwareInstaladoDetectado donde el inventario (R7) detectó
    `aplicacion` instalada con una versión que no coincide con
    `aplicacion.version_mas_reciente_conocida`. Vacío si esa aplicación no tiene
    versión cargada (no se vigila).

    Match por nombre con `icontains` (no exacto): el nombre real del programa en el
    registro de Windows no siempre coincide letra por letra con el nombre del
    catálogo (ej. "Google Chrome" vs "Google Chrome (64-bit)") — limitación de v1,
    aceptada explícitamente, mismo criterio que la deduplicación por mensaje exacto
    de PosErrorDetectado.

    No es comparación semántica de versiones (mayor/menor): solo "no coincide con la
    última conocida" — comparar versiones de forma genérica y confiable (¿"9.5.1" es
    mayor o menor que "9.10"?) es un problema mayor, fuera de alcance de v1."""
    from .models import SoftwareInstaladoDetectado

    if not aplicacion.version_mas_reciente_conocida:
        return SoftwareInstaladoDetectado.objects.none()
    return SoftwareInstaladoDetectado.objects.filter(
        nombre__icontains=aplicacion.nombre,
    ).exclude(version=aplicacion.version_mas_reciente_conocida)


# --- Ingesta: lo que reporta el agente por MQTT ---
#
# Hermano de `apps.despliegues.services.registrar_estado_de_estacion`, y por el mismo
# motivo: hasta el 2-oct-2026 el mapa paso->estado se armaba dentro de
# `apps.mqtt_worker.services.manejar_estado_instalacion`, como un dict local. El ciclo
# de vida de ResultadoInstalacion no es del transporte.

_PASO_A_ESTADO = {
    EventoInstalacion.Paso.RECIBIDO: ResultadoInstalacion.Estado.DESCARGANDO,
    EventoInstalacion.Paso.DESCARGADO: ResultadoInstalacion.Estado.DESCARGADO,
    EventoInstalacion.Paso.HASH_VERIFICADO: ResultadoInstalacion.Estado.VERIFICADO,
    EventoInstalacion.Paso.INSTALANDO: ResultadoInstalacion.Estado.INSTALANDO,
    EventoInstalacion.Paso.INSTALADO: ResultadoInstalacion.Estado.INSTALADO,
    EventoInstalacion.Paso.ERROR: ResultadoInstalacion.Estado.ERROR,
}


def paso_valido(paso) -> bool:
    """Si `paso` es uno de los que entiende la linea de tiempo de una instalacion."""
    return paso in EventoInstalacion.Paso.values


def registrar_estado_de_estacion(*, solicitud_id, estacion, paso, datos: dict | None = None):
    """Aplica a `ResultadoInstalacion` el paso que reporto una estacion y deja su evento.

    Devuelve el resultado, o None si el paso no es valido.

    Sin freno automatico, a diferencia de despliegues: instalar software del catalogo es
    de menor radio que actualizar el POS de toda la cadena (ver docstring de
    apps.software.models).
    """
    datos = datos or {}
    if not paso_valido(paso):
        return None

    resultado, _ = ResultadoInstalacion.objects.get_or_create(
        solicitud_id=solicitud_id, estacion=estacion,
    )
    if paso == EventoInstalacion.Paso.RECIBIDO:
        resultado.version_previa_detectada = datos.get(
            'version_previa_detectada', resultado.version_previa_detectada,
        )
    if paso == EventoInstalacion.Paso.INSTALADO:
        resultado.version_instalada = datos.get('version_instalada', resultado.version_instalada)

    nuevo_estado = _PASO_A_ESTADO.get(paso)
    if nuevo_estado:
        resultado.estado = nuevo_estado
    if paso == EventoInstalacion.Paso.ERROR:
        resultado.detalle_error = datos.get('detalle', '')
    resultado.save()

    EventoInstalacion.objects.create(resultado=resultado, paso=paso, detalle=datos.get('detalle', ''))

    verificar_completado(resultado.solicitud)
    return resultado


def registrar_software_instalado(*, estacion, programas: list) -> int:
    """Reemplaza el inventario de software de `estacion` por el que reporto el agente.

    Es un SNAPSHOT, no un delta: se borra lo anterior y se escribe lo nuevo, porque lo
    que importa es que esta instalado AHORA. Devuelve cuantos quedaron.

    Descarta duplicados dentro del mismo lote: el registro de Windows puede traer la
    misma entrada dos veces (32/64 bits) y `unique_together=('estacion', 'nombre')` no
    los tolera en un `bulk_create`. Esa regla depende del `unique_together` de ESTA
    tabla, y hasta el 2-oct-2026 vivia en el worker — que para escribir aca tenia que
    conocer una restriccion de un modelo ajeno.

    NO marca `Estacion.software_instalado_ultima_verificacion`: ese sello queda a cargo
    de quien llama (hoy `apps.mqtt_worker.services.manejar_software_instalado`). Es a
    proposito y es la unica diferencia con su hermano `catalogo.registrar_perifericos`,
    que si sella su fecha adentro: `Estacion` es de `catalogo`, asi que alla el sello es
    una escritura propia, y aca seria `software` escribiendo un modelo ajeno — justo el
    acoplamiento que esta extraccion vino a quitar. Encapsular una fecha no vale
    estrenar una dependencia de `software` hacia `catalogo`.

    La contra, que conviene saber: un segundo llamador que se olvide del sello deja la
    fecha vieja sin que nada falle. Si algun dia aparece, el lugar correcto para el sello
    es un servicio de `catalogo` que lo haga explicito, no un import desde aca.
    """
    from .models import SoftwareInstaladoDetectado

    def _limpiar(valor) -> str:
        # Postgres rechaza bytes NUL (0x00) en columnas text — algunos instaladores mal
        # comportados dejan uno en el registro de Windows (encontrado escaneando una
        # estación real, 20-ago-2026: tumbaba el bulk_create COMPLETO, no solo esa fila).
        # SQLite los tolera, por eso no se veía antes de probar contra Postgres real.
        return (valor or '').replace('\x00', '').strip()

    detectados = []
    nombres_vistos = set()
    for p in programas or []:
        nombre = _limpiar(p.get('nombre'))
        if not nombre or nombre in nombres_vistos:
            continue
        nombres_vistos.add(nombre)
        detectados.append(SoftwareInstaladoDetectado(
            estacion=estacion, nombre=nombre,
            version=_limpiar(p.get('version')), fabricante=_limpiar(p.get('fabricante')),
        ))

    SoftwareInstaladoDetectado.objects.filter(estacion=estacion).delete()
    SoftwareInstaladoDetectado.objects.bulk_create(detectados)
    return len(detectados)
