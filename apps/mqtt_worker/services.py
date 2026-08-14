"""Manejadores de los mensajes MQTT que llegan de los agentes.

Reemplaza la lógica que antes vivía en projectNodeJS/index.js. Se invoca
desde management/commands/run_mqtt_worker.py, que es quien mantiene la
conexión MQTT viva.
"""
import logging
from datetime import timedelta

from django.db import close_old_connections
from django.utils import timezone

from apps.catalogo import crypto
from apps.catalogo.models import ClaveRecuperacionBitLocker, Estacion, Farmacia
from apps.despliegues.models import EventoDespliegue, ResultadoDespliegue
from apps.despliegues.services import evaluar_freno_automatico, verificar_completado
from apps.mqtt_worker.emqx_admin import aprovisionar_credencial_estacion
from apps.mqtt_worker.models import MensajeMqttFallido, WorkerHeartbeat

logger = logging.getLogger(__name__)

# Una caché se considera utilizable solo si dio señales de vida recientemente.
CACHE_FRESCO_MINUTOS = 5

# Nombre de fila en WorkerHeartbeat para run_mqtt_worker — compartido con el
# dashboard, que lo usa para mostrar si el worker sigue activo.
NOMBRE_WORKER_MQTT = 'mqtt_worker'

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


def _cache_url_base_para(estacion) -> str | None:
    """URL LAN del caché de la farmacia de `estacion`, si hay uno online y no es ella misma."""
    fresco = timezone.now() - timedelta(minutes=CACHE_FRESCO_MINUTOS)
    cache = (
        Estacion.objects
        .filter(
            farmacia=estacion.farmacia_id,
            es_cache_farmacia=True,
            estado_aprobacion=Estacion.EstadoAprobacion.APROBADA,
            ultimo_heartbeat__gte=fresco,
        )
        .exclude(pk=estacion.pk)
        .exclude(ip_lan__isnull=True)
        .exclude(puerto_cache__isnull=True)
        .first()
    )
    if cache is None:
        return None
    return f'http://{cache.ip_lan}:{cache.puerto_cache}/'


def _respuesta_aceptado(estacion) -> dict:
    # Credencial MQTT propia de la estación (aislamiento a nivel de broker, no solo de
    # aplicación) — None si EMQX_ADMIN_CONFIG no está configurado o la llamada a EMQX
    # falla; el agente sigue funcionando con la credencial compartida en ese caso (ver
    # docstring de apps.mqtt_worker.emqx_admin).
    credencial_mqtt = aprovisionar_credencial_estacion(estacion)
    mqtt_username, mqtt_password = credencial_mqtt if credencial_mqtt else (None, None)
    return {
        'aceptado': True,
        'token': estacion.token_enrolamiento,
        'estado_aprobacion': estacion.estado_aprobacion,
        'farmacia': estacion.farmacia.codigo,
        'grupo': estacion.farmacia.grupo.codigo,
        'monitorear_recursos': estacion.monitorear_recursos,
        'soy_cache': estacion.es_cache_farmacia,
        'cache_url_base': _cache_url_base_para(estacion),
        'mqtt_username': mqtt_username,
        'mqtt_password': mqtt_password,
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
        campos = []
        if not estacion.hardware_id and hardware_id:
            estacion.hardware_id = hardware_id
            campos.append('hardware_id')
        if payload.get('hostname') and payload['hostname'] != estacion.hostname:
            estacion.hostname = payload['hostname']
            campos.append('hostname')
        if campos:
            estacion.save(update_fields=campos)
        return _respuesta_aceptado(estacion)

    farmacia = _farmacia_desde_codigo_estacion(codigo)
    if farmacia is None:
        logger.warning('Enrolamiento rechazado: farmacia no encontrada para %s', codigo)
        return {'aceptado': False, 'motivo': 'farmacia no encontrada'}

    estacion = Estacion.objects.create(
        codigo=codigo,
        farmacia=farmacia,
        hardware_id=hardware_id,
        hostname=payload.get('hostname', ''),
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

    if estacion.estado_conexion != Estacion.EstadoConexion.ONLINE:
        from apps.monitoreo.services import resolver_alertas_agente_caido_red_viva, resolver_alertas_sin_heartbeat
        resolver_alertas_sin_heartbeat(estacion)
        resolver_alertas_agente_caido_red_viva(estacion)

    estacion.version_agente = payload.get('version_agente', estacion.version_agente)
    estacion.version_pos = payload.get('version_pos', estacion.version_pos)
    estacion.so_nombre = payload.get('so_nombre', estacion.so_nombre)
    estacion.so_build = payload.get('so_build', estacion.so_build)
    estacion.hostname = payload.get('hostname', estacion.hostname)
    estacion.numero_serie = payload.get('numero_serie', estacion.numero_serie)
    if payload.get('ip_lan'):
        estacion.ip_lan = payload['ip_lan']
    if payload.get('puerto_cache'):
        estacion.puerto_cache = payload['puerto_cache']
    estacion.estado_conexion = Estacion.EstadoConexion.ONLINE
    estacion.ultimo_heartbeat = timezone.now()
    estacion.save()

    from apps.facturacion.services import registrar_actividad_mensual
    registrar_actividad_mensual(estacion)

    from apps.monitoreo.models import EstadoDispositivo
    from apps.monitoreo.services import registrar_estado_dispositivo
    registrar_estado_dispositivo(estacion, fuente=EstadoDispositivo.Fuente.MQTT, en_linea=True)


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

            from apps.facturacion.services import registrar_actividad_mensual
            registrar_actividad_mensual(estacion)

            from apps.monitoreo.models import EstadoDispositivo
            from apps.monitoreo.services import registrar_estado_dispositivo
            registrar_estado_dispositivo(estacion, fuente=EstadoDispositivo.Fuente.MQTT, en_linea=True)

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


def manejar_estado_instalacion(codigo_estacion: str, payload: dict) -> None:
    """Progreso/resultado de una SolicitudInstalacion (catálogo de software).

    Mismo patrón que manejar_estado_despliegue, sin los pasos de POS (pos_cerrado/
    pos_relanzado) que no aplican a instalar software genérico, y sin freno automático
    (instalar software es de menor radio que actualizar el POS de toda la cadena — ver
    docstring de apps.software.models).
    """
    close_old_connections()
    try:
        estacion = Estacion.objects.get(codigo=codigo_estacion, token_enrolamiento=payload.get('token'))
    except Estacion.DoesNotExist:
        logger.warning('Reporte de instalación con token inválido: %s', codigo_estacion)
        return
    if estacion.estado_aprobacion != Estacion.EstadoAprobacion.APROBADA:
        logger.warning('Reporte de instalación de estación no aprobada: %s', codigo_estacion)
        return

    from apps.software.models import EventoInstalacion, ResultadoInstalacion
    from apps.software.services import verificar_completado as verificar_instalacion_completada

    # Traduce cada paso fino al estado agregado — dict local (no a nivel de módulo, como
    # _PASO_A_ESTADO) porque apps.software se importa diferido: viene después de
    # mqtt_worker en INSTALLED_APPS, importar sus modelos arriba del todo del archivo
    # rompería la carga de apps de Django (mismo motivo que scripts/monitoreo abajo).
    paso_a_estado = {
        EventoInstalacion.Paso.RECIBIDO: ResultadoInstalacion.Estado.DESCARGANDO,
        EventoInstalacion.Paso.DESCARGADO: ResultadoInstalacion.Estado.DESCARGADO,
        EventoInstalacion.Paso.HASH_VERIFICADO: ResultadoInstalacion.Estado.VERIFICADO,
        EventoInstalacion.Paso.INSTALANDO: ResultadoInstalacion.Estado.INSTALANDO,
        EventoInstalacion.Paso.INSTALADO: ResultadoInstalacion.Estado.INSTALADO,
        EventoInstalacion.Paso.ERROR: ResultadoInstalacion.Estado.ERROR,
    }

    solicitud_id = payload.get('solicitud_id')
    paso = payload.get('paso')
    if paso not in EventoInstalacion.Paso.values:
        logger.warning('Paso desconocido "%s" reportado por %s (instalación)', paso, codigo_estacion)
        return

    resultado, _ = ResultadoInstalacion.objects.get_or_create(solicitud_id=solicitud_id, estacion=estacion)

    if paso == EventoInstalacion.Paso.RECIBIDO:
        resultado.version_previa_detectada = payload.get(
            'version_previa_detectada', resultado.version_previa_detectada,
        )
    if paso == EventoInstalacion.Paso.INSTALADO:
        resultado.version_instalada = payload.get('version_instalada', resultado.version_instalada)

    nuevo_estado = paso_a_estado.get(paso)
    if nuevo_estado:
        resultado.estado = nuevo_estado
    if paso == EventoInstalacion.Paso.ERROR:
        resultado.detalle_error = payload.get('detalle', '')
    resultado.save()

    EventoInstalacion.objects.create(resultado=resultado, paso=paso, detalle=payload.get('detalle', ''))

    verificar_instalacion_completada(resultado.solicitud)


def manejar_info_equipo(codigo_estacion: str, payload: dict) -> None:
    """Guarda la respuesta a una consulta puntual de hardware (comando "consultar_info")."""
    close_old_connections()
    try:
        estacion = Estacion.objects.get(codigo=codigo_estacion, token_enrolamiento=payload.get('token'))
    except Estacion.DoesNotExist:
        logger.warning('Info de equipo con token inválido: %s', codigo_estacion)
        return
    if estacion.estado_aprobacion != Estacion.EstadoAprobacion.APROBADA:
        return

    def _entero(clave):
        valor = payload.get(clave)
        return int(valor) if isinstance(valor, (int, float)) else None

    estacion.hostname = payload.get('hostname', estacion.hostname)
    estacion.numero_serie = payload.get('numero_serie', estacion.numero_serie)
    estacion.so_nombre = payload.get('so_nombre', estacion.so_nombre)
    estacion.so_build = payload.get('so_build', estacion.so_build)
    estacion.procesador = payload.get('procesador', estacion.procesador)
    estacion.ram_total_mb = _entero('ram_total_mb') or estacion.ram_total_mb
    estacion.almacenamiento_total_gb = _entero('almacenamiento_total_gb') or estacion.almacenamiento_total_gb
    if 'bitlocker_habilitado' in payload:
        estacion.bitlocker_habilitado = bool(payload['bitlocker_habilitado'])
    estacion.bitlocker_metodo_proteccion = payload.get(
        'bitlocker_metodo_proteccion', estacion.bitlocker_metodo_proteccion,
    )
    estacion.info_equipo_fecha = timezone.now()
    estacion.save()

    if 'bitlocker_habilitado' in payload:
        from apps.monitoreo.services import evaluar_regla_bitlocker, resolver_alertas_bitlocker
        if estacion.bitlocker_habilitado:
            resolver_alertas_bitlocker(estacion)
        else:
            evaluar_regla_bitlocker(estacion)

    # La clave de recuperación solo viaja si BitLocker está habilitado y el agente la
    # incluyó en esta respuesta puntual (no en cada heartbeat). Se cifra antes de
    # guardarse: en ningún momento queda en texto plano en la base de datos.
    clave_plana = payload.get('bitlocker_clave_recuperacion')
    if clave_plana:
        ClaveRecuperacionBitLocker.objects.update_or_create(
            estacion=estacion,
            defaults={
                'clave_cifrada': crypto.cifrar(clave_plana),
                'id_protector': payload.get('bitlocker_id_protector', ''),
            },
        )


def manejar_windows_update(codigo_estacion: str, payload: dict) -> None:
    """Guarda el resultado de un escaneo puntual de Windows Update (comando
    "escanear_actualizaciones") — v1 es solo escaneo/reporte, no instala nada.

    Si el agente reportó `error` (el escaneo falló de su lado — el caso más común es que
    la estación no tiene salida a internet habilitada, ver
    `agente_prueba._hay_conexion_a_internet`), se guarda el motivo en
    `windows_update_ultimo_error` para que el panel le avise al operador qué hacer, pero
    se deja el último resultado conocido (pendientes/requiere_reinicio/detalle) como
    estaba, en vez de borrarlo con datos vacíos que se verían como "sin pendientes" sin serlo.
    """
    close_old_connections()
    try:
        estacion = Estacion.objects.get(codigo=codigo_estacion, token_enrolamiento=payload.get('token'))
    except Estacion.DoesNotExist:
        logger.warning('Reporte de Windows Update con token inválido: %s', codigo_estacion)
        return
    if estacion.estado_aprobacion != Estacion.EstadoAprobacion.APROBADA:
        return

    estacion.windows_update_ultima_verificacion = timezone.now()
    error = payload.get('error')
    if error:
        logger.warning('Escaneo de Windows Update falló en %s: %s', codigo_estacion, error)
        estacion.windows_update_ultimo_error = error
        estacion.save(update_fields=['windows_update_ultima_verificacion', 'windows_update_ultimo_error'])
        return

    pendientes = payload.get('pendientes') or []
    estacion.windows_update_pendientes = len(pendientes)
    estacion.windows_update_requiere_reinicio = bool(payload.get('requiere_reinicio'))
    estacion.windows_update_detalle = pendientes
    estacion.windows_update_ultimo_error = ''
    estacion.save(update_fields=[
        'windows_update_ultima_verificacion', 'windows_update_pendientes',
        'windows_update_requiere_reinicio', 'windows_update_detalle', 'windows_update_ultimo_error',
    ])


def manejar_estado_script(codigo_estacion: str, payload: dict) -> None:
    """Guarda el progreso/resultado de una ejecución de script (comando "ejecutar_script")."""
    close_old_connections()
    try:
        estacion = Estacion.objects.get(codigo=codigo_estacion, token_enrolamiento=payload.get('token'))
    except Estacion.DoesNotExist:
        logger.warning('Estado de script con token inválido: %s', codigo_estacion)
        return
    if estacion.estado_aprobacion != Estacion.EstadoAprobacion.APROBADA:
        return

    from apps.scripts.models import ResultadoEjecucionScript
    from apps.scripts.services import recalcular_estado_ejecucion

    resultado_id = payload.get('resultado_id')
    estado = payload.get('estado')
    if estado not in ResultadoEjecucionScript.Estado.values:
        logger.warning('Estado de script desconocido "%s" reportado por %s', estado, codigo_estacion)
        return

    try:
        resultado = ResultadoEjecucionScript.objects.get(pk=resultado_id, estacion=estacion)
    except ResultadoEjecucionScript.DoesNotExist:
        logger.warning('ResultadoEjecucionScript %s no encontrado para %s', resultado_id, codigo_estacion)
        return

    resultado.estado = estado
    if estado == ResultadoEjecucionScript.Estado.EJECUTANDO and not resultado.fecha_inicio:
        resultado.fecha_inicio = timezone.now()
    if estado in (
        ResultadoEjecucionScript.Estado.COMPLETADO, ResultadoEjecucionScript.Estado.ERROR,
        ResultadoEjecucionScript.Estado.TIMEOUT,
    ):
        resultado.fecha_fin = timezone.now()
        resultado.exit_code = payload.get('exit_code')
        resultado.stdout = payload.get('stdout', '') or resultado.stdout
        resultado.stderr = payload.get('stderr', '') or resultado.stderr
    resultado.save()

    recalcular_estado_ejecucion(resultado.ejecucion)


def manejar_metricas(codigo_estacion: str, payload: dict) -> None:
    """Guarda una muestra de recursos reportada por el agente de un servidor."""
    close_old_connections()
    try:
        estacion = Estacion.objects.get(codigo=codigo_estacion, token_enrolamiento=payload.get('token'))
    except Estacion.DoesNotExist:
        logger.warning('Métricas con token inválido: %s', codigo_estacion)
        return
    if estacion.estado_aprobacion != Estacion.EstadoAprobacion.APROBADA:
        return

    from apps.monitoreo.models import MuestraMetrica

    def _num(clave):
        valor = payload.get(clave)
        return valor if isinstance(valor, (int, float)) else None

    muestra = MuestraMetrica.objects.create(
        estacion=estacion,
        ram_total=_num('ram_total'),
        ram_usada=_num('ram_usada'),
        ram_libre=_num('ram_libre'),
        cache=_num('cache'),
        swap_total=_num('swap_total'),
        swap_usada=_num('swap_usada'),
        cpu_carga_pct=_num('cpu_carga_pct'),
        temperatura_c=_num('temperatura_c'),
        latencia_ms=_num('latencia_ms'),
    )

    from apps.monitoreo.services import evaluar_reglas_metricas
    evaluar_reglas_metricas(estacion, muestra)


def registrar_latido_worker(nombre: str) -> None:
    """Marca que el worker `nombre` sigue vivo y procesando. Ver WorkerHeartbeat."""
    close_old_connections()
    WorkerHeartbeat.objects.update_or_create(nombre=nombre, defaults={'ultimo_latido': timezone.now()})


def registrar_mensaje_fallido(*, topico: str, payload_crudo: str, error: str) -> None:
    """Cola de revisión manual para un mensaje MQTT que no se pudo procesar.

    Antes esto solo quedaba en el log del proceso (fácil de perder de vista); ahora
    queda en una tabla que el panel/admin puede revisar y marcar como resuelta.
    """
    close_old_connections()
    MensajeMqttFallido.objects.create(topico=topico, payload_crudo=payload_crudo, error=error)
