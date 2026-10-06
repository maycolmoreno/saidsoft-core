"""Manejadores de los mensajes MQTT que llegan de los agentes.

Reemplaza la lógica que antes vivía en projectNodeJS/index.js. Se invoca
desde management/commands/run_mqtt_worker.py, que es quien mantiene la
conexión MQTT viva.
"""
import logging
from datetime import timedelta

from django.utils import timezone

from apps.catalogo.db import cerrar_conexiones_viejas
from apps.catalogo.models import Estacion, Farmacia
from apps.mqtt_worker.emqx_admin import aprovisionar_credencial_estacion
from apps.mqtt_worker.models import MensajeMqttFallido, WorkerHeartbeat

logger = logging.getLogger(__name__)

# Una caché se considera utilizable solo si dio señales de vida recientemente.
CACHE_FRESCO_MINUTOS = 5

# Nombre de fila en WorkerHeartbeat para run_mqtt_worker — compartido con el
# dashboard, que lo usa para mostrar si el worker sigue activo.
NOMBRE_WORKER_MQTT = 'mqtt_worker'

def _farmacia_desde_codigo_estacion(codigo_estacion: str) -> Farmacia | None:
    codigo_farmacia = codigo_estacion.split('-')[0]
    return Farmacia.objects.filter(codigo=codigo_farmacia).first()


def _registrar_enrolamiento_rechazado(codigo: str, payload: dict) -> None:
    """Deja el intento fallido en la bandeja de triage del panel.

    NUNCA lanza: esto corre dentro del worker MQTT, que es monohilo y es el unico oido
    de la plataforma. Que falle registrar un rechazo no puede tumbar la ingesta de todo
    lo demas.

    Se cuenta el intento en vez de crear una fila por vez: el agente reintenta solo, asi
    que una instalacion mal configurada generaria miles de filas y la bandeja dejaria de
    servir para lo unico que sirve, que es mirarla.
    """
    from django.db.models import F
    from django.utils import timezone

    from apps.mqtt_worker.models import EnrolamientoRechazado

    try:
        hostname = (payload.get('hostname') or '')[:120]
        fila, creada = EnrolamientoRechazado.objects.get_or_create(
            codigo_recibido=codigo[:100], hostname=hostname,
        )
        if not creada:
            EnrolamientoRechazado.objects.filter(pk=fila.pk).update(
                intentos=F('intentos') + 1, ultimo_intento=timezone.now(),
                # Vuelve a la bandeja: si alguien lo marco revisado y el equipo sigue
                # insistiendo, es que no se resolvio.
                revisado=False,
            )
    except Exception:
        logger.exception('No se pudo registrar el enrolamiento rechazado de %s', codigo)


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


def _respuesta_aceptado(estacion, payload_version: str = '') -> dict:
    # Credencial MQTT propia de la estación (aislamiento a nivel de broker, no solo de
    # aplicación) — None si EMQX_ADMIN_CONFIG no está configurado o la llamada a EMQX
    # falla; el agente sigue funcionando con la credencial compartida en ese caso (ver
    # docstring de apps.mqtt_worker.emqx_admin).
    from apps.catalogo.services import VERSION_AGENTE_CON_HMAC_PROPIO, _version_agente

    # El servidor SABE a quien le entrega el secreto, y en el enrolamiento la version del
    # agente si determina si lo va a guardar: es el mismo evento, el agente que responde
    # este mensaje es el que esta corriendo ahora.
    #
    # Esa es exactamente la diferencia con el bug del 16-sep-2026: alli la version se uso
    # para afirmar algo sobre un enrolamiento ocurrido MESES antes, con otro ejecutable.
    # Aca la inferencia es sobre el instante presente y es solida.
    #
    # Sin esto, una estacion instalada de cero con ComandoHmacSecret vacio —que es lo que
    # el instalador recomienda desde 0.21— queda sin poder recibir comandos: no tiene el
    # compartido y el servidor no sabe que si tiene el propio. Le paso a ML017-B.
    if _version_agente(payload_version) >= VERSION_AGENTE_CON_HMAC_PROPIO:
        if not estacion.hmac_propio_confirmado:
            estacion.hmac_propio_confirmado = True
            estacion.save(update_fields=['hmac_propio_confirmado'])

    credencial_mqtt = aprovisionar_credencial_estacion(estacion)
    mqtt_username, mqtt_password = credencial_mqtt if credencial_mqtt else (None, None)
    # Secreto HMAC propio de la estación, por el mismo canal que la credencial MQTT. Un
    # agente que no lo entienda lo ignora y sigue con el compartido de su config.txt; el
    # servidor tampoco lo usa para firmar hasta que la estación reporte una versión que
    # lo soporte, así que entregarlo acá no cambia nada por sí solo.
    #
    # Lo que este canal NO resuelve todavía: mientras el usuario MQTT compartido siga con
    # ACL sobre /saidsof/#, cualquiera con esa credencial puede suscribirse al tópico de
    # respuesta de otra estación y leer este secreto — igual que ya puede leer
    # `mqtt_password`. El aislamiento real llega con deploy/emqx-narrow-acl-agente.sh,
    # que espera a que las 3 estaciones apagadas migren a credencial propia.
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
        'hmac_secret': estacion.hmac_secret,
    }


def manejar_enrolamiento(payload: dict) -> dict:
    """Un agente nuevo se presenta. Lo crea en estado pendiente si su farmacia existe."""
    cerrar_conexiones_viejas()
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
        return _respuesta_aceptado(estacion, payload.get('version_agente', ''))

    # Interruptor global, la otra mitad del freno de emergencia. Va DESPUES del
    # re-enrolamiento y ANTES del token de apertura, y las dos cosas son deliberadas:
    #
    #   - Una estacion que ya existe se sigue re-enrolando siempre. Negarselo la dejaria
    #     muda —perdio su identidad.json y este es el unico camino de vuelta— y eso no es
    #     lo que alguien pide cuando frena las altas.
    #   - El camino cero-touch (token de apertura) SI queda bloqueado. Es tentador
    #     dejarlo pasar porque un token lo emite una persona a proposito, pero durante
    #     una emergencia lo que hay que cortar es que sigan apareciendo equipos nuevos,
    #     y el cero-touch es justamente el que los hace aparecer solos y en tanda.
    from apps.monitoreo.models import ConfiguracionMonitoreo
    if not ConfiguracionMonitoreo.obtener().enrolamiento_habilitado:
        logger.warning('Enrolamiento de %s rechazado: las altas nuevas estan deshabilitadas.', codigo)
        return {'aceptado': False, 'motivo': 'el alta de estaciones nuevas esta deshabilitada'}

    # Estación nueva. Si el agente trae un token de apertura válido, entra ya aprobada y
    # con la configuración de su perfil, y arranca sola los pasos de la plantilla — es el
    # camino "cero-touch" (ver apps.aperturas). Un token inválido/vencido no rechaza el
    # enrolamiento: cae al camino de siempre (pendiente de aprobación manual), para que un
    # token mal copiado en el config.txt no deje al técnico sin poder enrolar nada.
    token_apertura = payload.get('token_apertura', '')
    if token_apertura:
        from apps.aperturas.services import consumir_token, enrolar_estacion_de_apertura
        token = consumir_token(
            token_plano=token_apertura, codigo_estacion=codigo, hardware_id=hardware_id,
        )
        if token is not None:
            estacion = enrolar_estacion_de_apertura(token=token, codigo=codigo, payload=payload)
            return _respuesta_aceptado(estacion, payload.get('version_agente', ''))

    farmacia = _farmacia_desde_codigo_estacion(codigo)
    if farmacia is None:
        logger.warning('Enrolamiento rechazado: farmacia no encontrada para %s', codigo)
        # Y ademas queda en una bandeja que alguien puede mirar. Solo en el log,
        # el sintoma era: se instala el agente, el tecnico se va, y la estacion
        # nunca aparece en el panel sin que nadie sepa por que. Con codigos
        # escritos a mano eso no es una posibilidad, es cuestion de tiempo.
        _registrar_enrolamiento_rechazado(codigo, payload)
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
    return _respuesta_aceptado(estacion, payload.get('version_agente', ''))


def manejar_heartbeat(codigo_estacion: str, payload: dict) -> None:
    # `select_related` porque este latido termina en `evaluar_regla_reloj` y
    # `evaluar_regla_autocorrecciones_reloj`, y las dos arrancan con
    # `estacion.farmacia.unidad_negocio` para buscar las reglas aplicables. Sin esto son
    # dos cargas diferidas por latido -- medido el 5-oct-2026: 11 consultas por latido, de
    # las cuales estas dos. Es la ruta de escritura más caliente del sistema (un latido por
    # minuto por estación), así que a 1.300 farmacias son ~111 consultas/s solo de acá.
    # Mismo patrón que ya usan `manejar_servicios_pos` y `manejar_eventos_sistema`.
    cerrar_conexiones_viejas()
    try:
        estacion = Estacion.objects.select_related('farmacia__unidad_negocio').get(
            codigo=codigo_estacion, token_enrolamiento=payload.get('token'),
        )
    except Estacion.DoesNotExist:
        logger.warning('Heartbeat con token inválido o estación desconocida: %s', codigo_estacion)
        return

    if estacion.estado_aprobacion != Estacion.EstadoAprobacion.APROBADA:
        return

    if estacion.estado_conexion != Estacion.EstadoConexion.ONLINE:
        from apps.monitoreo.services import resolver_alertas_agente_caido_red_viva, resolver_alertas_sin_heartbeat
        resolver_alertas_sin_heartbeat(estacion)
        resolver_alertas_agente_caido_red_viva(estacion)

    version_agente_anterior = estacion.version_agente
    estacion.version_agente = payload.get('version_agente', estacion.version_agente)
    # Lo declara el agente, no se deduce de la versión: un agente actualizado a 0.21 que
    # se enroló ANTES conserva su identidad.json y nunca recibió el secreto. Ver
    # apps.catalogo.services.secreto_de. Un agente viejo no manda la clave y el
    # `.get(..., actual)` deja el valor como estaba en vez de apagarlo por omisión.
    estacion.hmac_propio_confirmado = bool(
        payload.get('hmac_propio', estacion.hmac_propio_confirmado),
    )
    estacion.version_pos = payload.get('version_pos', estacion.version_pos)
    estacion.so_nombre = payload.get('so_nombre', estacion.so_nombre)
    estacion.so_build = payload.get('so_build', estacion.so_build)
    estacion.hostname = payload.get('hostname', estacion.hostname)
    estacion.numero_serie = payload.get('numero_serie', estacion.numero_serie)
    # Config del POS (ver Estacion.pos_bdd): el agente solo la manda si pudo leer el
    # .Config, así que se usa .get con el valor actual como default -- un heartbeat de
    # una estación sin POS instalado no debe borrar lo que ya se sabía.
    estacion.pos_servidor = payload.get('pos_servidor', estacion.pos_servidor)
    estacion.pos_bdd = payload.get('pos_bdd', estacion.pos_bdd)
    estacion.pos_puerto = payload.get('pos_puerto', estacion.pos_puerto)
    # Reloj y zona horaria (ver los campos en catalogo.Estacion). El desfase se calcula
    # acá y no en el agente a propósito: el agente no tiene con qué compararse — si su
    # reloj está mal, su idea de "ahora" también lo está. El servidor sí es la referencia.
    reloj_epoch = payload.get('reloj_epoch')
    if reloj_epoch is not None:
        try:
            estacion.desfase_reloj_segundos = round(float(reloj_epoch) - timezone.now().timestamp())
        except (TypeError, ValueError):
            logger.warning('Heartbeat de %s con reloj_epoch ilegible: %r', codigo_estacion, reloj_epoch)
    offset = payload.get('offset_utc_minutos')
    if offset is not None:
        try:
            estacion.offset_utc_minutos = int(offset)
        except (TypeError, ValueError):
            logger.warning('Heartbeat de %s con offset_utc_minutos ilegible: %r', codigo_estacion, offset)
    estacion.zona_horaria = payload.get('zona_horaria', estacion.zona_horaria)

    if payload.get('ip_lan'):
        estacion.ip_lan = payload['ip_lan']
    if payload.get('puerto_cache'):
        estacion.puerto_cache = payload['puerto_cache']
    # Cuántas veces la estación se corrigió el reloj sola (agente 0.30+). Se guarda el
    # contador acumulado tal cual lo reporta ella: es su historia, no la nuestra, y
    # sobrevive a que el servidor pierda datos.
    #
    # Un agente anterior no manda la clave, y entonces no se toca nada: poner 0 borraría
    # el historial de una estación que sí se venía corrigiendo, y justo esa es la señal
    # que este contador existe para conservar.
    autocorrecciones = payload.get('autocorrecciones_reloj')
    if isinstance(autocorrecciones, int) and autocorrecciones != estacion.autocorrecciones_reloj:
        estacion.autocorrecciones_reloj = autocorrecciones
        ultima = payload.get('ultima_autocorreccion_reloj') or ''
        if ultima:
            from django.utils.dateparse import parse_datetime
            fecha = parse_datetime(ultima)
            if fecha is not None:
                # El agente la manda en hora LOCAL de la estación y sin zona (isoformat
                # de datetime.now()). Se interpreta en la zona del proyecto en vez de
                # asumir UTC, que la correría 5 horas.
                estacion.ultima_autocorreccion_reloj = (
                    timezone.make_aware(fecha) if timezone.is_naive(fecha) else fecha
                )
        logger.warning(
            '%s se corrigió el reloj sola (van %s veces).', codigo_estacion, autocorrecciones,
        )

    # La estación declara si está frenada. Es lo único que convierte "publiqué la orden"
    # en "la orden se aplicó": el mensaje de pausa va retenido, así que salir del
    # servidor no prueba nada sobre una estación apagada. Se confía en lo que reporta y
    # no en lo que el panel pidió, porque son dos cosas distintas y la diferencia entre
    # ellas es justo lo que hay que poder ver durante una emergencia.
    #
    # Un agente anterior a 0.29 no manda la clave: `payload.get(...)` sin default la deja
    # en None y entonces no se toca nada. Marcarlo como "no pausado" seria peor —
    # afirmaria algo que ese agente no puede decir.
    pausado_reportado = payload.get('pausado')
    if isinstance(pausado_reportado, bool):
        estacion.pausado = pausado_reportado
        estacion.pausa_confirmada_en = timezone.now()

    estacion.estado_conexion = Estacion.EstadoConexion.ONLINE
    estacion.ultimo_heartbeat = timezone.now()
    estacion.save()

    if estacion.version_agente and estacion.version_agente != version_agente_anterior:
        # La versión reportada cambió -- si había una actualización retenida esperando
        # a que esta estación se conectara (ver enviar_actualizacion_agente), ya se
        # aplicó: borrarla para que una desconexión/reconexión de red cualquiera (no
        # solo apagar/prender el equipo) no la vuelva a disparar de nuevo.
        from apps.catalogo.services import limpiar_actualizacion_pendiente
        limpiar_actualizacion_pendiente(estacion)

    from apps.facturacion.services import registrar_actividad_mensual
    registrar_actividad_mensual(estacion)

    from apps.monitoreo.models import EstadoDispositivo
    from apps.monitoreo.services import (
        evaluar_regla_autocorrecciones_reloj, evaluar_regla_reloj, registrar_estado_dispositivo,
    )
    registrar_estado_dispositivo(estacion, fuente=EstadoDispositivo.Fuente.MQTT, en_linea=True)
    # Despues del save: el desfase recien recalculado ya esta persistido, y la alerta se
    # abre o se resuelve con el valor de ESTE latido (ver evaluar_regla_reloj).
    evaluar_regla_reloj(estacion)
    evaluar_regla_autocorrecciones_reloj(estacion)


def manejar_estado_despliegue(codigo_estacion: str, payload: dict) -> None:
    cerrar_conexiones_viejas()
    try:
        estacion = Estacion.objects.get(codigo=codigo_estacion, token_enrolamiento=payload.get('token'))
    except Estacion.DoesNotExist:
        logger.warning('Reporte de despliegue con token inválido: %s', codigo_estacion)
        return
    if estacion.estado_aprobacion != Estacion.EstadoAprobacion.APROBADA:
        logger.warning('Reporte de despliegue de estación no aprobada: %s', codigo_estacion)
        return

    from apps.despliegues.services import paso_valido, registrar_estado_de_estacion

    paso = payload.get('paso')
    if not paso_valido(paso):
        logger.warning('Paso desconocido "%s" reportado por %s', paso, codigo_estacion)
        return

    resultado = registrar_estado_de_estacion(
        despliegue_id=payload.get('despliegue_id'), estacion=estacion, paso=paso, datos=payload,
    )

    # Que un despliegue termine OK tambien significa "esta caja esta viva y corre tal
    # version del POS". Esa conclusion cruza tres dominios —catalogo, facturacion,
    # monitoreo— y por eso la coordina el worker y no `despliegues`: traerla alla lo
    # obligaria a importar las otras dos, acoplamiento que hoy no existe.
    if paso == 'ok' and resultado is not None and resultado.version_nueva:
        estacion.version_pos = resultado.version_nueva
        estacion.estado_conexion = Estacion.EstadoConexion.ONLINE
        estacion.ultimo_heartbeat = timezone.now()
        estacion.save(update_fields=['version_pos', 'estado_conexion', 'ultimo_heartbeat'])

        from apps.facturacion.services import registrar_actividad_mensual
        registrar_actividad_mensual(estacion)

        from apps.monitoreo.models import EstadoDispositivo
        from apps.monitoreo.services import registrar_estado_dispositivo
        registrar_estado_dispositivo(estacion, fuente=EstadoDispositivo.Fuente.MQTT, en_linea=True)


def manejar_estado_instalacion(codigo_estacion: str, payload: dict) -> None:
    """Progreso/resultado de una SolicitudInstalacion (catálogo de software).

    Mismo patrón que manejar_estado_despliegue, sin los pasos de POS (pos_cerrado/
    pos_relanzado) que no aplican a instalar software genérico, y sin freno automático
    (instalar software es de menor radio que actualizar el POS de toda la cadena — ver
    docstring de apps.software.models).
    """
    cerrar_conexiones_viejas()
    try:
        estacion = Estacion.objects.get(codigo=codigo_estacion, token_enrolamiento=payload.get('token'))
    except Estacion.DoesNotExist:
        logger.warning('Reporte de instalación con token inválido: %s', codigo_estacion)
        return
    if estacion.estado_aprobacion != Estacion.EstadoAprobacion.APROBADA:
        logger.warning('Reporte de instalación de estación no aprobada: %s', codigo_estacion)
        return

    from apps.software.services import paso_valido, registrar_estado_de_estacion

    paso = payload.get('paso')
    if not paso_valido(paso):
        logger.warning('Paso desconocido "%s" reportado por %s (instalación)', paso, codigo_estacion)
        return

    registrar_estado_de_estacion(
        solicitud_id=payload.get('solicitud_id'), estacion=estacion, paso=paso, datos=payload,
    )


def manejar_info_equipo(codigo_estacion: str, payload: dict) -> None:
    """Guarda la respuesta a una consulta puntual de hardware (comando "consultar_info")."""
    cerrar_conexiones_viejas()
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
    if payload.get('power_plan'):
        estacion.power_plan_actual = payload['power_plan']
        estacion.power_plan_ultima_verificacion = timezone.now()
    estacion.info_equipo_fecha = timezone.now()
    estacion.save()

    if 'bitlocker_habilitado' in payload:
        from apps.monitoreo.services import evaluar_regla_bitlocker, resolver_alertas_bitlocker
        if estacion.bitlocker_habilitado:
            resolver_alertas_bitlocker(estacion)
        else:
            evaluar_regla_bitlocker(estacion)

    # La clave de recuperación solo viaja si BitLocker está habilitado y el agente la
    # incluyó en esta respuesta puntual (no en cada heartbeat). El cifrado y la escritura
    # son de catalogo, que es el dueño del secreto: acá solo se entrega lo que llegó.
    from apps.catalogo.services import guardar_clave_bitlocker
    guardar_clave_bitlocker(
        estacion=estacion,
        clave_plana=payload.get('bitlocker_clave_recuperacion') or '',
        id_protector=payload.get('bitlocker_id_protector', ''),
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
    cerrar_conexiones_viejas()
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


def manejar_software_instalado(codigo_estacion: str, payload: dict) -> None:
    """Guarda el resultado de un escaneo puntual de software instalado (comando
    "consultar_software_instalado"). Semántica de snapshot: reemplaza por completo lo
    que había antes para esta estación — ver docstring de
    apps.software.models.SoftwareInstaladoDetectado sobre por qué (evita lógica de
    diff; instalar/desinstalar algo entre escaneos se refleja solo en el próximo)."""
    cerrar_conexiones_viejas()
    try:
        estacion = Estacion.objects.get(codigo=codigo_estacion, token_enrolamiento=payload.get('token'))
    except Estacion.DoesNotExist:
        logger.warning('Reporte de software instalado con token inválido: %s', codigo_estacion)
        return
    if estacion.estado_aprobacion != Estacion.EstadoAprobacion.APROBADA:
        return

    from apps.software.services import registrar_software_instalado

    registrar_software_instalado(estacion=estacion, programas=payload.get('programas') or [])

    estacion.software_instalado_ultima_verificacion = timezone.now()
    estacion.save(update_fields=['software_instalado_ultima_verificacion'])


def manejar_perifericos(codigo_estacion: str, payload: dict) -> None:
    """Guarda el resultado de un escaneo puntual de periféricos USB (comando
    "consultar_perifericos"). Semántica de snapshot, mismo criterio que
    manejar_software_instalado: reemplaza por completo lo que había antes para esta
    estación — ver docstring de apps.catalogo.models.PerifericoDetectado."""
    cerrar_conexiones_viejas()
    try:
        estacion = Estacion.objects.get(codigo=codigo_estacion, token_enrolamiento=payload.get('token'))
    except Estacion.DoesNotExist:
        logger.warning('Reporte de periféricos con token inválido: %s', codigo_estacion)
        return
    if estacion.estado_aprobacion != Estacion.EstadoAprobacion.APROBADA:
        return

    from apps.catalogo.services import registrar_perifericos

    registrar_perifericos(estacion=estacion, dispositivos=payload.get('dispositivos') or [])


def manejar_eventos_sistema(codigo_estacion: str, payload: dict) -> None:
    """Guarda los eventos del visor de Windows que reporto esta estacion.

    Un reporte vacio no se guarda ni se trata como "todo bien": que el agente no haya
    encontrado eventos y que no haya podido leer el visor se ven igual desde aca, y
    marcar una estacion como sana por no recibir nada es el modo de falla que este
    proyecto ya conoce.
    """
    cerrar_conexiones_viejas()
    try:
        estacion = Estacion.objects.select_related('farmacia__unidad_negocio').get(
            codigo=codigo_estacion, token_enrolamiento=payload.get('token'),
        )
    except Estacion.DoesNotExist:
        logger.warning('Reporte de eventos de Windows con token invalido: %s', codigo_estacion)
        return
    if estacion.estado_aprobacion != Estacion.EstadoAprobacion.APROBADA:
        return

    eventos = payload.get('eventos')
    if not isinstance(eventos, list) or not eventos:
        return

    from apps.monitoreo.services import registrar_eventos_sistema

    guardados = registrar_eventos_sistema(estacion=estacion, eventos=eventos)
    logger.info('%s: %d tipo(s) de evento de Windows registrado(s).', codigo_estacion, guardados)


def manejar_servicios_pos(codigo_estacion: str, payload: dict) -> None:
    """Guarda el chequeo de los servicios externos que consume el POS de esta estación.

    El agente reporta datos crudos —alcanzable, latencia, el mensaje real— y no decide si
    eso es bueno o malo. Esa decisión vive en `ReglaAlerta`, configurable desde el panel:
    un umbral en el agente obligaría a redistribuir el ejecutable a ~1.800 estaciones para
    cambiarlo.

    Un reporte sin resultados no se guarda: significa que el agente no pudo leer el
    `.exe.Config` del POS, y marcar los cuatro servicios como caídos por eso convertiría
    un problema de lectura en una falla aparente de toda la infraestructura.
    """
    cerrar_conexiones_viejas()
    try:
        estacion = Estacion.objects.select_related('farmacia__unidad_negocio').get(
            codigo=codigo_estacion, token_enrolamiento=payload.get('token'),
        )
    except Estacion.DoesNotExist:
        logger.warning('Reporte de servicios del POS con token inválido: %s', codigo_estacion)
        return
    if estacion.estado_aprobacion != Estacion.EstadoAprobacion.APROBADA:
        return

    resultados = payload.get('resultados')
    if not isinstance(resultados, list) or not resultados:
        logger.info('%s: reporte de servicios del POS sin resultados', codigo_estacion)
        return

    from apps.monitoreo.services import registrar_servicios_pos

    guardados = registrar_servicios_pos(estacion=estacion, resultados=resultados)
    logger.info('%s: %d servicio(s) del POS actualizados', codigo_estacion, guardados)


def manejar_activos_farmacia(codigo_estacion: str, payload: dict) -> None:
    """Guarda el resultado del ping a los activos sin agente de la farmacia (ver
    apps.monitoreo.models.EstadoRedActivo).

    A diferencia de `manejar_red_farmacia`, acá un "no responde" SÍ es un dato que hay
    que guardar: la ausencia de respuesta es justamente lo que se quiere saber. Lo que no
    se guarda es un reporte sin lista de resultados — eso significa que el agente no pudo
    sondear nada, y marcar todo como caído por eso convertiría un problema de la estación
    en una falla aparente de diez equipos.
    """
    cerrar_conexiones_viejas()
    try:
        estacion = Estacion.objects.select_related('farmacia').get(
            codigo=codigo_estacion, token_enrolamiento=payload.get('token'),
        )
    except Estacion.DoesNotExist:
        logger.warning('Reporte de activos de farmacia con token inválido: %s', codigo_estacion)
        return
    if estacion.estado_aprobacion != Estacion.EstadoAprobacion.APROBADA:
        return

    resultados = payload.get('resultados')
    if not isinstance(resultados, list) or not resultados:
        logger.info('%s: reporte de activos sin resultados', codigo_estacion)
        return

    from apps.monitoreo.services import registrar_estado_red_activos

    guardados = registrar_estado_red_activos(estacion=estacion, resultados=resultados)
    logger.info('%s: estado de red actualizado para %d activo(s)', codigo_estacion, guardados)


def manejar_red_farmacia(codigo_estacion: str, payload: dict) -> None:
    """Guarda el resultado de un sondeo de ancho de banda del Mikrotik LOCAL de la
    farmacia, hecho por una estación de esa misma LAN (ver
    apps.catalogo.services.enviar_consultar_red_farmacia y el docstring de
    agente_prueba._leer_contadores_mikrotik sobre por qué esto reemplaza el sondeo
    directo desde el servidor -- sin ruta de red hacia esas IPs privadas).

    Sin bytes_recibidos/bytes_enviados en el payload (el agente no pudo sondear su
    router local -- caído, community equivocada, etc.) no se crea ninguna
    MuestraRedFarmacia: no hay nada real que guardar, y una fila con contadores en
    cero rompería el cálculo de tasa de la siguiente muestra (se leería como una
    caída real de tráfico, no como "no se pudo medir")."""
    cerrar_conexiones_viejas()
    try:
        estacion = Estacion.objects.select_related('farmacia').get(
            codigo=codigo_estacion, token_enrolamiento=payload.get('token'),
        )
    except Estacion.DoesNotExist:
        logger.warning('Reporte de red de farmacia con token inválido: %s', codigo_estacion)
        return
    if estacion.estado_aprobacion != Estacion.EstadoAprobacion.APROBADA:
        return

    from apps.monitoreo.services import registrar_muestra_red_farmacia

    muestra = registrar_muestra_red_farmacia(
        farmacia=estacion.farmacia,
        bytes_recibidos=payload.get('bytes_recibidos'),
        bytes_enviados=payload.get('bytes_enviados'),
    )
    if muestra is None:
        logger.info('%s: no se pudo sondear el Mikrotik local de %s', codigo_estacion, estacion.farmacia.codigo)


def manejar_pos_errores(codigo_estacion: str, payload: dict) -> None:
    """Guarda lo nuevo de un reporte periódico del log del POS (ver
    agente-prueba/agente_prueba.py::bucle_log_pos) — el agente ya agrupó por mensaje
    exacto dentro de la ventana leída, acá solo se acumula por estación (a diferencia
    de manejar_software_instalado, esto no es un snapshot: cada reporte es un delta que
    se suma al contador de por vida de PosErrorDetectado). Termina evaluando la regla
    de alerta correspondiente con el total de errores de categoría "sistema" nuevos de
    esta ventana — los de categoría "negocio" (ej. "VENTA SIN LOTE", una validación del
    POS funcionando bien, no una falla) se guardan igual pero no cuentan para la
    alerta, ver apps.monitoreo.services.clasificar_error_pos."""
    cerrar_conexiones_viejas()
    try:
        estacion = Estacion.objects.get(codigo=codigo_estacion, token_enrolamiento=payload.get('token'))
    except Estacion.DoesNotExist:
        logger.warning('Reporte de errores del POS con token inválido: %s', codigo_estacion)
        return
    if estacion.estado_aprobacion != Estacion.EstadoAprobacion.APROBADA:
        return

    from apps.monitoreo.services import registrar_errores_pos

    registrar_errores_pos(estacion=estacion, errores=payload.get('errores') or [])


def manejar_estado_script(codigo_estacion: str, payload: dict) -> None:
    """Guarda el progreso/resultado de una ejecución de script (comando "ejecutar_script")."""
    cerrar_conexiones_viejas()
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
    # `select_related` por lo mismo que en `manejar_heartbeat`: `registrar_muestra_metricas`
    # termina en `evaluar_reglas_metricas`, que arranca con
    # `estacion.farmacia.unidad_negocio`.
    cerrar_conexiones_viejas()
    try:
        estacion = Estacion.objects.select_related('farmacia__unidad_negocio').get(
            codigo=codigo_estacion, token_enrolamiento=payload.get('token'),
        )
    except Estacion.DoesNotExist:
        logger.warning('Métricas con token inválido: %s', codigo_estacion)
        return
    if estacion.estado_aprobacion != Estacion.EstadoAprobacion.APROBADA:
        return

    from apps.monitoreo.services import registrar_muestra_metricas

    registrar_muestra_metricas(estacion=estacion, payload=payload)


def registrar_latido_worker(nombre: str) -> None:
    """Marca que el worker `nombre` sigue vivo y procesando. Ver WorkerHeartbeat."""
    cerrar_conexiones_viejas()
    WorkerHeartbeat.objects.update_or_create(nombre=nombre, defaults={'ultimo_latido': timezone.now()})


def registrar_mensaje_fallido(*, topico: str, payload_crudo: str, error: str) -> None:
    """Cola de revisión manual para un mensaje MQTT que no se pudo procesar.

    Antes esto solo quedaba en el log del proceso (fácil de perder de vista); ahora
    queda en una tabla que el panel/admin puede revisar y marcar como resuelta.
    """
    cerrar_conexiones_viejas()
    MensajeMqttFallido.objects.create(topico=topico, payload_crudo=payload_crudo, error=error)
