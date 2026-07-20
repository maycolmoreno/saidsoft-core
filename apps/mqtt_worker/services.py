"""Manejadores de los mensajes MQTT que llegan de los agentes.

Reemplaza la lógica que antes vivía en projectNodeJS/index.js. Se invoca
desde management/commands/run_mqtt_worker.py, que es quien mantiene la
conexión MQTT viva.
"""
import logging

from django.db import close_old_connections
from django.utils import timezone

from apps.catalogo.models import Estacion, Farmacia
from apps.despliegues.models import EventoDespliegue, ResultadoDespliegue
from apps.despliegues.services import evaluar_freno_automatico, verificar_completado

logger = logging.getLogger(__name__)

# Traduce cada paso fino de la línea de tiempo al estado agregado de ResultadoDespliegue
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


def _farmacia_desde_codigo_estacion(codigo_estacion: str) -> Farmacia | None:
    codigo_farmacia = codigo_estacion.split('-')[0]
    return Farmacia.objects.filter(codigo=codigo_farmacia).first()


def _respuesta_aceptado(estacion) -> dict:
    return {
        'aceptado': True,
        'token': estacion.token_enrolamiento,
        'estado_aprobacion': estacion.estado_aprobacion,
        'farmacia': estacion.farmacia.codigo,
        'grupo': estacion.farmacia.grupo.codigo,
    }


def manejar_enrolamiento(payload: dict) -> dict:
    """Un agente nuevo se presenta. Lo crea en estado pendiente si su farmacia existe."""
    close_old_connections()
    codigo = payload.get('codigo', '')
    hardware_id = payload.get('hardware_id', '')

    estacion = Estacion.objects.select_related('farmacia__grupo').filter(codigo=codigo).first()
    if estacion is not None:
        # Re-enrolamiento (el agente perdió su identidad.json). No entregamos el token
        # solo porque alguien diga el código: exigimos que el hardware_id coincida con el
        # que se fijó la primera vez, para que un equipo ajeno en la VPN no pueda pedir el
        # token de una estación existente y suplantarla.
        if estacion.hardware_id and estacion.hardware_id != hardware_id:
            logger.warning(
                'Re-enrolamiento rechazado por hardware_id distinto para %s (posible suplantación)', codigo,
            )
            return {'aceptado': False, 'motivo': 'hardware no coincide, requiere reaprobación manual en el panel'}
        # Trust-on-first-use: si nunca se guardó un hardware_id (estación creada antes de
        # este mecanismo), se fija el primero que llegue.
        if not estacion.hardware_id and hardware_id:
            estacion.hardware_id = hardware_id
            estacion.save(update_fields=['hardware_id'])
        return _respuesta_aceptado(estacion)

    farmacia = _farmacia_desde_codigo_estacion(codigo)
    if farmacia is None:
        logger.warning('Enrolamiento rechazado: farmacia no encontrada para %s', codigo)
        return {'aceptado': False, 'motivo': 'farmacia no encontrada'}

    estacion = Estacion.objects.create(
        codigo=codigo,
        farmacia=farmacia,
        hardware_id=hardware_id,
        numero_serie=payload.get('numero_serie', ''),
        so_nombre=payload.get('so_nombre', ''),
        so_build=payload.get('so_build', ''),
        version_agente=payload.get('version_agente', ''),
    )
    logger.info('Nueva estación enrolada (pendiente de aprobación): %s', codigo)
    return _respuesta_aceptado(estacion)


def manejar_heartbeat(codigo_estacion: str, payload: dict) -> None:
    close_old_connections()
    try:
        estacion = Estacion.objects.get(codigo=codigo_estacion, token_enrolamiento=payload.get('token'))
    except Estacion.DoesNotExist:
        logger.warning('Heartbeat con token inválido o estación desconocida: %s', codigo_estacion)
        return

    if estacion.estado_aprobacion != Estacion.EstadoAprobacion.APROBADA:
        return

    estacion.version_agente = payload.get('version_agente', estacion.version_agente)
    estacion.version_pos = payload.get('version_pos', estacion.version_pos)
    estacion.so_nombre = payload.get('so_nombre', estacion.so_nombre)
    estacion.so_build = payload.get('so_build', estacion.so_build)
    estacion.numero_serie = payload.get('numero_serie', estacion.numero_serie)
    estacion.estado_conexion = Estacion.EstadoConexion.ONLINE
    estacion.ultimo_heartbeat = timezone.now()
    estacion.save()


def manejar_estado_despliegue(codigo_estacion: str, payload: dict) -> None:
    close_old_connections()
    try:
        estacion = Estacion.objects.get(codigo=codigo_estacion, token_enrolamiento=payload.get('token'))
    except Estacion.DoesNotExist:
        logger.warning('Reporte de despliegue con token inválido: %s', codigo_estacion)
        return
    if estacion.estado_aprobacion != Estacion.EstadoAprobacion.APROBADA:
        logger.warning('Reporte de despliegue de estación no aprobada: %s', codigo_estacion)
        return

    despliegue_id = payload.get('despliegue_id')
    paso = payload.get('paso')
    if paso not in EventoDespliegue.Paso.values:
        logger.warning('Paso desconocido "%s" reportado por %s', paso, codigo_estacion)
        return

    resultado, _ = ResultadoDespliegue.objects.get_or_create(
        despliegue_id=despliegue_id, estacion=estacion,
    )

    if paso == EventoDespliegue.Paso.POS_CERRADO:
        resultado.version_previa = payload.get('version_previa', resultado.version_previa)
    if paso == EventoDespliegue.Paso.OK:
        resultado.version_nueva = payload.get('version_nueva', resultado.version_nueva)
        # No esperamos al próximo heartbeat para reflejar la versión real: el propio
        # reporte "ok" ya es una confirmación directa del agente de que quedó instalada.
        if resultado.version_nueva:
            estacion.version_pos = resultado.version_nueva
            estacion.estado_conexion = Estacion.EstadoConexion.ONLINE
            estacion.ultimo_heartbeat = timezone.now()
            estacion.save(update_fields=['version_pos', 'estado_conexion', 'ultimo_heartbeat'])

    nuevo_estado = _PASO_A_ESTADO.get(paso)
    if nuevo_estado:
        resultado.estado = nuevo_estado
    if paso == EventoDespliegue.Paso.ERROR:
        resultado.detalle_error = payload.get('detalle', '')
    resultado.save()

    EventoDespliegue.objects.create(resultado=resultado, paso=paso, detalle=payload.get('detalle', ''))

    despliegue = resultado.despliegue
    evaluar_freno_automatico(despliegue)
    verificar_completado(despliegue)
