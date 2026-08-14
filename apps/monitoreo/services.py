"""Motor de alertas: evalúa ReglaAlerta contra MuestraMetrica (o ausencia de
heartbeat), abre/mantiene/resuelve Alerta, y notifica por correo al abrir.

Los handlers de MQTT (apps.mqtt_worker.services) llaman a estas funciones justo
después de guardar cada muestra/heartbeat; el comando `marcar_estaciones_offline`
llama a la parte de "sin_heartbeat".
"""
import logging
from datetime import timedelta

from django.core.mail import send_mail
from django.db import close_old_connections
from django.db.models import Q
from django.utils import timezone

from .models import Alerta, EstadoDispositivo, EventoMonitoreo, Metrica, MuestraMetrica, ReglaAlerta

logger = logging.getLogger(__name__)

# EstadoDispositivo(fuente=MESHCENTRAL) más viejo que esto se considera stale (ej. el
# worker de MeshCentral está caído) y NO se usa para abrir agente_caido_red_viva — evita
# falsos positivos por datos viejos en vez de por una discrepancia real entre fuentes.
FRESCURA_MESHCENTRAL_MINUTOS = 30


def reglas_aplicables_a(unidad_negocio, *, metrica=None):
    """Reglas activas que aplican a `unidad_negocio`: globales (unidad_negocio=None)
    o privadas de esa unidad — mismo criterio "global o del cliente" que
    apps.cuentas.services.scope_scripts_visibles usa para Script."""
    qs = ReglaAlerta.objects.filter(activo=True).filter(
        Q(unidad_negocio__isnull=True) | Q(unidad_negocio=unidad_negocio),
    )
    if metrica:
        qs = qs.filter(metrica=metrica)
    return qs


def _cumple(regla, valor):
    if regla.operador == ReglaAlerta.Operador.LTE:
        return valor <= regla.umbral
    return valor >= regla.umbral


def _condicion_sostenida(regla, estacion):
    """True si todas las muestras no nulas de `regla.metrica` en los últimos
    `duracion_minutos` incumplen el umbral, y hay historial suficiente para afirmarlo
    (la estación ya reportaba desde antes de que empezara la ventana) — así un pico
    aislado de una sola muestra no abre alerta, y una estación recién enrolada tampoco
    dispara una por falta de datos."""
    desde = timezone.now() - timedelta(minutes=regla.duracion_minutos)
    primera = estacion.metricas.order_by('timestamp').first()
    if primera is None or primera.timestamp > desde:
        return False

    valores = [
        valor for m in estacion.metricas.filter(timestamp__gte=desde)
        if (valor := getattr(m, regla.metrica, None)) is not None
    ]
    if not valores:
        return False
    return all(_cumple(regla, valor) for valor in valores)


def _alerta_activa(regla, estacion):
    return Alerta.objects.filter(
        regla=regla, estacion=estacion, estado__in=[Alerta.Estado.ABIERTA, Alerta.Estado.RECONOCIDA],
    ).first()


def abrir_o_mantener_alerta(regla, estacion, valor):
    """Abre una Alerta nueva si no hay ya una activa para (regla, estacion); si ya
    existe, no hace nada (no se duplica ni se vuelve a notificar)."""
    if _alerta_activa(regla, estacion):
        return None
    alerta = Alerta.objects.create(regla=regla, estacion=estacion, valor_disparador=valor)
    notificar_alerta(alerta)
    return alerta


def resolver_condicion(regla, estacion):
    """Si hay una alerta activa para (regla, estacion), la marca resuelta."""
    alerta = _alerta_activa(regla, estacion)
    if alerta:
        alerta.estado = Alerta.Estado.RESUELTA
        alerta.resuelta_en = timezone.now()
        alerta.save(update_fields=['estado', 'resuelta_en'])


def registrar_estado_dispositivo(estacion, *, fuente: str, en_linea: bool, detalle: dict | None = None) -> None:
    """Puerto de entrada único para cualquier fuente de monitoreo (MQTT, MeshCentral, y a
    futuro ESET): actualiza el snapshot en EstadoDispositivo y, solo si `en_linea` cambió
    respecto al valor anterior, agrega una fila a EventoMonitoreo (histórico de
    transiciones, no de cada señal).

    No evalúa reglas de alerta acá — evaluar_cruce_monitoreo lo hace por lotes, no en
    cada llamada, porque a la frecuencia de heartbeat/eventos de 1.800+ estaciones
    evaluar el cruce en cada señal sería redundante (la comparación no cambia entre una
    señal MQTT y la siguiente si MeshCentral no se movió).

    `close_old_connections()` porque, a diferencia de resto de este módulo, esto lo
    llaman también workers de larga duración fuera del ciclo request/response de Django
    (run_meshcentral_worker) — mismo motivo que ya usan los handlers de mqtt_worker.
    """
    close_old_connections()
    anterior = EstadoDispositivo.objects.filter(estacion=estacion, fuente=fuente).first()
    cambio = anterior is None or anterior.en_linea != en_linea

    EstadoDispositivo.objects.update_or_create(
        estacion=estacion, fuente=fuente,
        defaults={'en_linea': en_linea, 'detalle': detalle or {}},
    )
    if cambio:
        EventoMonitoreo.objects.create(estacion=estacion, fuente=fuente, en_linea=en_linea, detalle=detalle or {})


def evaluar_cruce_monitoreo() -> int:
    """Cruce MQTT × MeshCentral: abre `agente_caido_red_viva` para estaciones sin
    heartbeat MQTT que MeshCentral todavía ve en línea — señal de que el servicio del
    agente se cayó, no la red (si fuera la red, MeshCentral tampoco la vería).

    Reusa el mismo umbral en minutos que `sin_heartbeat` (Estacion.ultimo_heartbeat), no
    `duracion_minutos` — ver docstring de ReglaAlerta. Requiere que el EstadoDispositivo
    de MeshCentral esté fresco (FRESCURA_MESHCENTRAL_MINUTOS): si el propio worker de
    MeshCentral está caído, su último dato puede ser viejo y no debe usarse para inferir
    nada.

    Una sola pasada por lotes (dos querysets, cruzados por estacion_id en Python) en vez
    de una consulta por estación — a 1.800 estaciones, N consultas individuales no
    escala igual que el resto de las tareas periódicas de esta escala (ver
    marcar_estaciones_offline).
    """
    from apps.catalogo.models import Estacion

    reglas = list(ReglaAlerta.objects.filter(activo=True, metrica=Metrica.AGENTE_CAIDO_RED_VIVA))
    if not reglas:
        return 0
    reglas_por_unidad: dict = {}
    for regla in reglas:
        reglas_por_unidad.setdefault(regla.unidad_negocio_id, []).append(regla)

    fresco_desde = timezone.now() - timedelta(minutes=FRESCURA_MESHCENTRAL_MINUTOS)
    estaciones_meshcentral_online = set(
        EstadoDispositivo.objects.filter(
            fuente=EstadoDispositivo.Fuente.MESHCENTRAL, en_linea=True, actualizado_en__gte=fresco_desde,
        ).values_list('estacion_id', flat=True)
    )
    if not estaciones_meshcentral_online:
        return 0

    candidatas = (
        Estacion.objects
        .filter(pk__in=estaciones_meshcentral_online, estado_conexion=Estacion.EstadoConexion.OFFLINE)
        .exclude(ultimo_heartbeat__isnull=True)
        .select_related('farmacia__unidad_negocio')
    )

    abiertas = 0
    ahora = timezone.now()
    for estacion in candidatas:
        unidad_id = estacion.farmacia.unidad_negocio_id
        aplicables = reglas_por_unidad.get(None, []) + reglas_por_unidad.get(unidad_id, [])
        if not aplicables:
            continue
        minutos_sin_heartbeat = (ahora - estacion.ultimo_heartbeat).total_seconds() / 60
        for regla in aplicables:
            if minutos_sin_heartbeat >= regla.umbral:
                if abrir_o_mantener_alerta(regla, estacion, minutos_sin_heartbeat):
                    abiertas += 1
    return abiertas


def resolver_alertas_agente_caido_red_viva(estacion) -> None:
    """Al recibir heartbeat MQTT de nuevo, resuelve cualquier alerta
    'agente_caido_red_viva' activa de esta estación — mismo criterio que
    resolver_alertas_sin_heartbeat."""
    Alerta.objects.filter(
        estacion=estacion, regla__metrica=Metrica.AGENTE_CAIDO_RED_VIVA,
        estado__in=[Alerta.Estado.ABIERTA, Alerta.Estado.RECONOCIDA],
    ).update(estado=Alerta.Estado.RESUELTA, resuelta_en=timezone.now())


def evaluar_reglas_metricas(estacion, muestra):
    """Evalúa contra `muestra` (recién guardada) todas las reglas de métrica (todo
    salvo sin_heartbeat) aplicables a la unidad de negocio de `estacion`."""
    unidad = estacion.farmacia.unidad_negocio
    for regla in reglas_aplicables_a(unidad).exclude(metrica=Metrica.SIN_HEARTBEAT):
        valor = getattr(muestra, regla.metrica, None)
        if valor is None:
            continue
        if _cumple(regla, valor):
            if _condicion_sostenida(regla, estacion):
                abrir_o_mantener_alerta(regla, estacion, valor)
            # Si incumple pero todavía no se sostiene lo suficiente: no se abre nada
            # todavía, pero tampoco se resuelve una que ya estuviera abierta.
        else:
            resolver_condicion(regla, estacion)


def evaluar_reglas_sin_heartbeat(estaciones_offline):
    """Para cada estación recién marcada OFFLINE (ver marcar_estaciones_offline),
    abre alerta 'sin_heartbeat' si hay una regla aplicable cuyo umbral ya se cumplió."""
    for estacion in estaciones_offline:
        if not estacion.ultimo_heartbeat:
            continue
        minutos_sin_heartbeat = (timezone.now() - estacion.ultimo_heartbeat).total_seconds() / 60
        unidad = estacion.farmacia.unidad_negocio
        for regla in reglas_aplicables_a(unidad, metrica=Metrica.SIN_HEARTBEAT):
            if minutos_sin_heartbeat >= regla.umbral:
                abrir_o_mantener_alerta(regla, estacion, minutos_sin_heartbeat)


def resolver_alertas_sin_heartbeat(estacion):
    """Al recibir heartbeat de nuevo, resuelve cualquier alerta 'sin_heartbeat'
    activa de esta estación."""
    Alerta.objects.filter(
        estacion=estacion, regla__metrica=Metrica.SIN_HEARTBEAT,
        estado__in=[Alerta.Estado.ABIERTA, Alerta.Estado.RECONOCIDA],
    ).update(estado=Alerta.Estado.RESUELTA, resuelta_en=timezone.now())


def evaluar_regla_bitlocker(estacion):
    """Llamada justo después de guardar `Estacion.bitlocker_habilitado=False` (ver
    apps.mqtt_worker.services.manejar_info_equipo). Es un estado binario reportado
    puntualmente, no una serie de tiempo — a diferencia de evaluar_reglas_metricas, no
    hay "condición sostenida" que esperar: se abre directo si hay una regla aplicable."""
    unidad = estacion.farmacia.unidad_negocio
    for regla in reglas_aplicables_a(unidad, metrica=Metrica.BITLOCKER_DESHABILITADO):
        # valor_disparador es un FloatField requerido pero no tiene un valor numérico
        # natural para una condición binaria; 0 es solo un marcador ("no cifrado").
        abrir_o_mantener_alerta(regla, estacion, valor=0)


def resolver_alertas_bitlocker(estacion):
    """Al reportarse BitLocker habilitado de nuevo, resuelve cualquier alerta
    'bitlocker_deshabilitado' activa de esta estación."""
    Alerta.objects.filter(
        estacion=estacion, regla__metrica=Metrica.BITLOCKER_DESHABILITADO,
        estado__in=[Alerta.Estado.ABIERTA, Alerta.Estado.RECONOCIDA],
    ).update(estado=Alerta.Estado.RESUELTA, resuelta_en=timezone.now())


def notificar_alerta(alerta):
    """Correo a quienes tengan acceso a la unidad de negocio de la estación (equipo
    interno + usuarios de ese cliente) con email configurado. Solo se llama al abrir
    una alerta nueva, no en cada muestra que la sostiene."""
    from django.contrib.auth import get_user_model
    User = get_user_model()

    unidad = alerta.estacion.farmacia.unidad_negocio
    destinatarios = list(
        User.objects.filter(
            Q(is_superuser=True) | Q(perfil__acceso_todas_unidades=True) | Q(perfil__unidades_negocio=unidad),
        ).exclude(email='').values_list('email', flat=True).distinct()
    )
    if not destinatarios:
        logger.info('Alerta #%s sin destinatarios de correo (unidad %s).', alerta.pk, unidad.codigo)
        return

    asunto = f'[{alerta.regla.get_severidad_display()}] {alerta.regla.nombre} — {alerta.estacion.codigo}'
    if alerta.regla.metrica == Metrica.BITLOCKER_DESHABILITADO:
        # Condición binaria: "valor (umbral: >= X)" no significa nada aquí, a diferencia
        # de las métricas numéricas (CPU/RAM/latencia/sin_heartbeat).
        detalle_condicion = 'BitLocker deshabilitado.'
    else:
        detalle_condicion = (
            f'Valor: {alerta.valor_disparador} (umbral: {alerta.regla.get_operador_display()} {alerta.regla.umbral}).'
        )
    cuerpo = (
        f'{alerta.regla.nombre} en {alerta.estacion.codigo} ({unidad.codigo}).\n'
        f'{detalle_condicion}\n'
        f'Abierta: {alerta.abierta_en:%Y-%m-%d %H:%M:%S}.'
    )
    # fail_silently: un SMTP caído no debe tumbar la ingesta de métricas del worker MQTT
    # ni el cron de marcar_estaciones_offline — la alerta ya quedó guardada en la BD.
    send_mail(asunto, cuerpo, None, destinatarios, fail_silently=True)


def purgar_metricas_antiguas(*, dias: int = 30) -> int:
    """Borra MuestraMetrica más viejas que `dias`. Reemplaza el `vaciar_logs` del sistema
    viejo (que borraba TODO cada domingo) — retención por antigüedad, no total.

    En producción con TimescaleDB esto lo haría una política de retención nativa; esta
    función queda como respaldo y para el entorno SQLite de desarrollo. La llaman tanto
    el comando manual (`purgar_metricas`) como la tarea periódica de Celery.
    """
    umbral = timezone.now() - timedelta(days=dias)
    borradas, _ = MuestraMetrica.objects.filter(timestamp__lt=umbral).delete()
    return borradas


def purgar_eventos_monitoreo_antiguos(*, dias: int = 30) -> int:
    """Borra EventoMonitoreo más viejos que `dias` — mismo criterio de retención que
    purgar_metricas_antiguas (EstadoDispositivo no se purga: es un snapshot, no un
    histórico)."""
    umbral = timezone.now() - timedelta(days=dias)
    borrados, _ = EventoMonitoreo.objects.filter(timestamp__lt=umbral).delete()
    return borrados
