"""Motor de alertas: evalúa ReglaAlerta contra MuestraMetrica (o ausencia de
heartbeat), abre/mantiene/resuelve Alerta, y notifica por correo al abrir.

Los handlers de MQTT (apps.mqtt_worker.services) llaman a estas funciones justo
después de guardar cada muestra/heartbeat; el comando `marcar_estaciones_offline`
llama a la parte de "sin_heartbeat".
"""
import logging
from datetime import timedelta

from django.core.mail import send_mail
from django.db.models import Q
from django.utils import timezone

from .models import Alerta, Metrica, ReglaAlerta

logger = logging.getLogger(__name__)


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
    cuerpo = (
        f'{alerta.regla.nombre} en {alerta.estacion.codigo} ({unidad.codigo}).\n'
        f'Valor: {alerta.valor_disparador} (umbral: {alerta.regla.get_operador_display()} {alerta.regla.umbral}).\n'
        f'Abierta: {alerta.abierta_en:%Y-%m-%d %H:%M:%S}.'
    )
    # fail_silently: un SMTP caído no debe tumbar la ingesta de métricas del worker MQTT
    # ni el cron de marcar_estaciones_offline — la alerta ya quedó guardada en la BD.
    send_mail(asunto, cuerpo, None, destinatarios, fail_silently=True)
