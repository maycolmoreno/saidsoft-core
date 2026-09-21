"""Motor de alertas: evalúa ReglaAlerta contra MuestraMetrica (o ausencia de
heartbeat), abre/mantiene/resuelve Alerta, y notifica por correo al abrir.

Los handlers de MQTT (apps.mqtt_worker.services) llaman a estas funciones justo
después de guardar cada muestra/heartbeat; el comando `marcar_estaciones_offline`
llama a la parte de "sin_heartbeat".
"""
import json
import logging
import urllib.error
import urllib.request
from datetime import timedelta

from django.conf import settings
from django.core.mail import send_mail
from django.db.models import Q
from django.utils import timezone

from apps.catalogo.db import cerrar_conexiones_viejas

from .models import (
    Alerta, CanalNotificacion, EstadoDispositivo, EventoMonitoreo, Metrica, MuestraMetrica, MuestraRedFarmacia,
    ConfiguracionMonitoreo, PosErrorDetectado, ReglaAlerta, VentanaMantenimiento,
)

logger = logging.getLogger(__name__)

# EstadoDispositivo(fuente=MESHCENTRAL) más viejo que esto se considera stale (ej. el
# worker de MeshCentral está caído) y NO se usa para abrir agente_caido_red_viva — evita
# falsos positivos por datos viejos en vez de por una discrepancia real entre fuentes.
FRESCURA_MESHCENTRAL_MINUTOS = 30

# Minutos que una Alerta puede quedar ABIERTA sin que nadie la reconozca antes de
# reenviar la notificación (ver escalar_alertas_abiertas). Global y no por ReglaAlerta
# a propósito (decisión del usuario): un solo valor es más simple de operar mientras
# no haya evidencia real de que algunas reglas necesitan escalar antes/después que
# otras — mismo criterio que FRESCURA_MESHCENTRAL_MINUTOS arriba.
# Valor por defecto historico. La fuente real es ConfiguracionMonitoreo, editable
# desde el admin: cuanto esperar antes de insistir depende de quien este de guardia,
# y eso no se decide una vez en el codigo.
UMBRAL_ESCALAMIENTO_MINUTOS = 30

# Prefijos de mensaje que el POS loguea en nivel ERROR pero que son una validación de
# negocio funcionando bien (ej. bloquear una venta sin lote), no una falla real del
# sistema — confirmado con el usuario que "VENTA SIN LOTE" es rutinario en la operación
# real, no esporádico. Contarlos igual que un timeout de conexión inundaría la alerta
# de falsos positivos apenas se activara en una farmacia con volumen normal de ventas.
# Agregar acá nuevos patrones a medida que se encuentren en producción (mismo criterio
# que PERMISOS_LITERALES en apps/activos/management/commands/seed_permisos.py: una
# lista chica que se edita a mano, no vale la pena un modelo/admin para esto todavía).
PREFIJOS_ERROR_DE_NEGOCIO = (
    'VENTA SIN LOTE',
)


def clasificar_error_pos(mensaje: str) -> str:
    """PosErrorDetectado.Categoria.NEGOCIO si `mensaje` matchea un prefijo conocido de
    validación de negocio, si no PosErrorDetectado.Categoria.SISTEMA (default: ante la
    duda, un mensaje nuevo que no se reconoce se trata como señal real, no se descarta
    en silencio)."""
    if mensaje.startswith(PREFIJOS_ERROR_DE_NEGOCIO):
        return PosErrorDetectado.Categoria.NEGOCIO
    return PosErrorDetectado.Categoria.SISTEMA


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


def ventana_mantenimiento_activa(estacion):
    """La VentanaMantenimiento activa que cubre a `estacion` ahora mismo, si alguna,
    si no None. Único punto que consulta VentanaMantenimiento — tanto
    abrir_o_mantener_alerta (silenciar) como el panel (aviso "en mantenimiento
    hasta") lo reusan, en vez de resolver el destino cada uno por su cuenta."""
    from apps.catalogo.services import resolver_estaciones

    ahora = timezone.now()
    candidatas = VentanaMantenimiento.objects.filter(
        activo=True, desde__lte=ahora, hasta__gte=ahora,
        unidad_negocio_id=estacion.farmacia.unidad_negocio_id,
    )
    for ventana in candidatas:
        estaciones = resolver_estaciones(
            ventana.destino_tipo, unidad_negocio=ventana.unidad_negocio,
            grupos=ventana.grupos.all(), farmacias=ventana.farmacias.all(), estaciones=ventana.estaciones.all(),
        )
        if estaciones.filter(pk=estacion.pk).exists():
            return ventana
    return None


def abrir_o_mantener_alerta(regla, estacion, valor):
    """Abre una Alerta nueva si no hay ya una activa para (regla, estacion); si ya
    existe, no hace nada (no se duplica ni se vuelve a notificar). Si `estacion` está
    cubierta por una VentanaMantenimiento activa, no abre nada — un solo chequeo acá
    cierra el silenciamiento para las cuatro rutas de evaluación (métricas, sin
    heartbeat, bitlocker, pos_errores), presente y futura, sin tocar cada una."""
    if ventana_mantenimiento_activa(estacion):
        return None
    if _alerta_activa(regla, estacion):
        return None
    alerta = Alerta.objects.create(regla=regla, estacion=estacion, valor_disparador=valor)
    notificar_alerta(alerta)
    if regla.severidad == ReglaAlerta.Severidad.CRITICAL:
        _pedir_diagnostico_ia(alerta)
    if regla.abre_mantenimiento:
        # Import diferido: mantener la dependencia monitoreo -> mantenimiento fuera del
        # nivel de módulo (mismo criterio que el resto de los cruces entre apps acá).
        # La función nunca lanza -- ver su docstring: un problema abriendo la orden de
        # trabajo no debe impedir que la alerta quede abierta y notificada.
        from apps.mantenimiento.services import abrir_mantenimiento_desde_alerta
        abrir_mantenimiento_desde_alerta(alerta)
    return alerta


def _pedir_diagnostico_ia(alerta) -> None:
    """Encola el diagnóstico automático de una alerta CRÍTICA. Nunca lanza.

    Asíncrono y no en línea: una llamada lenta o caída a la API retrasaría la apertura
    de la alerta y su notificación, que es lo único que de verdad no puede fallar acá.
    Mismo espíritu fail-silently que el resto de las notificaciones de este módulo.

    Se dispara UNA VEZ por incidente, y eso lo garantiza `abrir_o_mantener_alerta`: si
    la alerta sigue activa no se crea otra, así que no se vuelve a llegar hasta acá
    mientras el servicio siga caído. Es control de costo, no solo de ruido.

    Import diferido para no arrastrar apps.monitoreo.tasks (y con él Celery) al importar
    services, que es lo que hacen el worker MQTT y los comandos de gestión.
    """
    if not getattr(settings, 'ANTHROPIC_API_KEY', ''):
        return
    try:
        from .tasks import diagnosticar_alerta_task
        diagnosticar_alerta_task.delay(alerta.pk)
    except Exception:
        # Un broker caído no debe impedir que la alerta quede abierta y notificada: el
        # diagnóstico es un extra, la alerta es el producto.
        logger.warning('No se pudo encolar el diagnóstico IA de la alerta #%s.', alerta.pk, exc_info=True)


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

    `cerrar_conexiones_viejas()` porque, a diferencia de resto de este módulo, esto lo
    llaman también workers de larga duración fuera del ciclo request/response de Django
    (run_meshcentral_worker) — mismo motivo que ya usan los handlers de mqtt_worker.
    """
    cerrar_conexiones_viejas()
    anterior = EstadoDispositivo.objects.filter(estacion=estacion, fuente=fuente).first()
    cambio = anterior is None or anterior.en_linea != en_linea

    EstadoDispositivo.objects.update_or_create(
        estacion=estacion, fuente=fuente,
        defaults={'en_linea': en_linea, 'detalle': detalle or {}},
    )
    if cambio:
        EventoMonitoreo.objects.create(estacion=estacion, fuente=fuente, en_linea=en_linea, detalle=detalle or {})
        logger.info(
            '%s: %s -> %s (%s)', estacion.codigo, fuente, 'en línea' if en_linea else 'fuera de línea', detalle or {},
        )


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


def registrar_eventos_sistema(*, estacion, eventos: list) -> int:
    """Guarda los eventos de Windows que reportó el agente y evalúa la alerta.

    AGREGA por (estación, log, id): una fila que se va sumando, no una por ocurrencia.
    Medido el 20-sep-2026 sobre una máquina real, 260 `Ntfs 55` en 30 días — a 1.800
    estaciones eso es medio millón de filas mensuales que nadie lee. Lo que sirve es
    "esto viene pasando 260 veces desde el martes".

    Solo se aceptan los eventos que están en el catálogo y ACTIVOS: el agente ya filtra,
    pero un agente viejo con un catálogo desactualizado seguiría mandando lo que se dio
    de baja, y `identificador` sin validar deja entrar cualquier cosa.
    """
    from .models import EventoSistemaDetectado, EventoSistemaVigilado

    # La clave incluye el PROVEEDOR: el mismo ID significa cosas distintas segun quien
    # lo emita. `System 55` de Ntfs es corrupcion del sistema de archivos; `System 55` de
    # Kernel-Processor-Power es un aviso de energia del procesador, y en una maquina real
    # habia 260 de esos. Sin el proveedor en la clave, los 260 entran como corrupcion.
    vigilados = {
        (v.log, v.proveedor, v.identificador): v
        for v in EventoSistemaVigilado.objects.filter(activo=True)
    }
    guardados = 0

    for fila in eventos:
        log = (fila.get('log') or '').strip()
        try:
            identificador = int(fila.get('id'))
        except (TypeError, ValueError):
            continue
        origen = (fila.get('origen') or '').strip()
        vigilado = vigilados.get((log, origen, identificador))
        if vigilado is None:
            logger.info(
                '%s reportó %s/%s/%s, que no está en el catálogo activo — se ignora.',
                estacion.codigo, log, origen or '(sin origen)', identificador,
            )
            continue

        try:
            cantidad = max(1, int(fila.get('cantidad', 1)))
        except (TypeError, ValueError):
            cantidad = 1

        detectado, creado = EventoSistemaDetectado.objects.get_or_create(
            estacion=estacion, log=log, origen=origen[:120], identificador=identificador,
            defaults={
                'ultimo_mensaje': (fila.get('mensaje') or '')[:500],
                'cantidad_total': cantidad,
            },
        )
        if not creado:
            # F() y no leer-sumar-guardar: el worker MQTT puede estar ingiriendo el
            # reporte de otra estación al mismo tiempo, y la cuenta se pisa.
            from django.db.models import F

            EventoSistemaDetectado.objects.filter(pk=detectado.pk).update(
                cantidad_total=F('cantidad_total') + cantidad,
                ultimo_mensaje=(fila.get('mensaje') or '')[:500] or detectado.ultimo_mensaje,
                ultima_vez=timezone.now(),
            )
            detectado.refresh_from_db()
        guardados += 1

        if vigilado.abre_alerta:
            evaluar_regla_evento_sistema(estacion, detectado, vigilado)

    return guardados


def evaluar_regla_evento_sistema(estacion, detectado, vigilado) -> None:
    """Abre la alerta de un evento de Windows marcado como accionable.

    De la familia de `evaluar_regla_bitlocker`: el evento ya pasó, no hay condición
    sostenida que esperar.

    **Qué dispara y qué no lo decide el CATÁLOGO, no esta función** — `abre_alerta` por
    fila. Es lo que permite que un disco muriendo despierte a alguien y que los 260
    `Ntfs 55` de la misma máquina solo se guarden. Sin esa separación, la única forma de
    bajar el ruido sería dejar de recolectar, y entonces se pierde el contexto que
    explica el problema cuando llega.

    La severidad sale del catálogo y se aplica sobre las reglas de esa severidad: mismo
    mecanismo que `evaluar_regla_servicio_pos` usa con `critico`, sin inventar un
    concepto nuevo.
    """
    from .models import Metrica, ReglaAlerta

    unidad = estacion.farmacia.unidad_negocio
    for regla in reglas_aplicables_a(unidad, metrica=Metrica.EVENTO_SISTEMA):
        if regla.severidad != vigilado.severidad:
            continue
        abrir_o_mantener_alerta(regla, estacion, detectado.cantidad_total)


def evaluar_regla_reloj(estacion) -> None:
    """Llamada desde `manejar_heartbeat` justo despues de recalcular
    `Estacion.desfase_reloj_segundos`. De la familia de `evaluar_regla_bitlocker`: el
    heartbeat ya trae el estado medido, no hay condicion sostenida que esperar.

    Por que existe esta alerta y no alcanzaba con verlo en el panel: pasados los
    `UMBRAL_RELOJ_INCOMUNICADO_SEGUNDOS` (120 s) el agente descarta TODO mensaje
    firmado, incluido el script que le arreglaria el reloj, y la estacion solo se
    recupera yendo al local. Entre los 30 s y los 120 s hay una ventana en la que
    todavia obedece y el arreglo es remoto — avisar dentro de esa ventana es la
    diferencia entre un clic y un viaje. Le paso a MAM06-A el 26-ago-2026 y nadie se
    entero hasta que la estacion ya estaba muda.

    Se compara el valor ABSOLUTO contra el umbral: un reloj 40 s atrasado rompe la firma
    igual que uno 40 s adelantado. En la Alerta se guarda el desfase CON SIGNO, porque
    la direccion dice la causa probable (adelantada = reloj corriendo rapido; atrasada
    = tipicamente equipo apagado con la pila de CMOS agotada).
    """
    desfase = estacion.desfase_reloj_segundos
    if desfase is None:
        return
    unidad = estacion.farmacia.unidad_negocio
    for regla in reglas_aplicables_a(unidad, metrica=Metrica.DESFASE_RELOJ):
        if _cumple(regla, abs(desfase)):
            abrir_o_mantener_alerta(regla, estacion, desfase)
        else:
            resolver_condicion(regla, estacion)


def evaluar_regla_pos_errores(estacion, total_nuevos: int) -> None:
    """Llamada tras ingerir un reporte del log del POS (ver
    apps.mqtt_worker.services.manejar_pos_errores). Cada reporte ya es una ventana
    cerrada (los ERROR/FATAL nuevos desde el último chequeo del agente) — no hay
    condición sostenida en el tiempo que esperar, mismo criterio que
    agente_caido_red_viva/sin_heartbeat. A diferencia de esas dos (que ignoran
    operador/umbral salvo como minutos), acá sí se reusa el umbral/operador de
    ReglaAlerta tal cual: "abrir si la cantidad de errores nuevos en la ventana
    cumple la condición" — mismo mecanismo de comparación que evaluar_reglas_metricas
    (_cumple), solo que el valor no sale de una MuestraMetrica."""
    unidad = estacion.farmacia.unidad_negocio
    for regla in reglas_aplicables_a(unidad, metrica=Metrica.POS_ERRORES):
        if _cumple(regla, total_nuevos):
            abrir_o_mantener_alerta(regla, estacion, total_nuevos)
        else:
            resolver_condicion(regla, estacion)


def _enviar_webhook_teams(url, texto):
    """POST {"text": ...} al webhook entrante de Teams (formato clásico del conector
    O365) — nunca lanza: un webhook caído no debe tumbar la ingesta de métricas ni el
    envío de correo, mismo criterio que fail_silently=True de send_mail. Mismo patrón
    stdlib (urllib, sin agregar `requests` como dependencia) que
    apps.mqtt_worker.emqx_admin."""
    datos = json.dumps({'text': texto}).encode('utf-8')
    req = urllib.request.Request(url, data=datos, method='POST', headers={'Content-Type': 'application/json'})
    try:
        urllib.request.urlopen(req, timeout=5)
    except (urllib.error.URLError, urllib.error.HTTPError):
        logger.warning('No se pudo notificar por webhook de Teams (%s).', url, exc_info=True)


_EMOJI_SEVERIDAD = {
    ReglaAlerta.Severidad.CRITICAL: '🔴',
    ReglaAlerta.Severidad.WARNING: '🟡',
}

# Telegram rechaza con "message is too long" cualquier sendMessage de más de 4096
# caracteres. Se deja margen porque el límite es sobre el texto ya codificado y los
# emojis y acentos de estos mensajes ocupan más de un byte.
#
# No es teórico: la primera corrida real tras configurar el chat (17-sep-2026 21:27)
# juntó 29 caídas y 128 recuperaciones en un mensaje, Telegram lo rechazó entero, y como
# `notificar_cambios_enlaces` marca los eventos como avisados aunque el envío falle —a
# propósito, para no acumular backlog— esos 157 avisos se perdieron sin dejar más rastro
# que un WARNING. Trocear es lo que hace que ese diseño siga siendo seguro.
_LIMITE_TELEGRAM = 3800


def _trozos_telegram(texto):
    """Parte `texto` en fragmentos que Telegram acepte, cortando por líneas.

    Cortar por líneas y no a ciegas cada N caracteres importa porque estos mensajes son
    listados: partir "GP024  192.168.61.1  desde 14:20" por la mitad deja dos fragmentos
    que no se entienden. Una línea que por sí sola supere el límite (no debería pasar con
    el formato actual) se corta duro, que es mejor que no mandar nada.
    """
    if len(texto) <= _LIMITE_TELEGRAM:
        return [texto]
    trozos, actual = [], ''
    for linea in texto.split('\n'):
        while len(linea) > _LIMITE_TELEGRAM:
            if actual:
                trozos.append(actual)
                actual = ''
            trozos.append(linea[:_LIMITE_TELEGRAM])
            linea = linea[_LIMITE_TELEGRAM:]
        if len(actual) + len(linea) + 1 > _LIMITE_TELEGRAM:
            trozos.append(actual)
            actual = linea
        else:
            actual = f'{actual}\n{linea}' if actual else linea
    if actual:
        trozos.append(actual)
    return trozos


def llamar_telegram(metodo: str, cuerpo: dict) -> bool:
    """POST a cualquier método de la API de Telegram. Nunca lanza.

    Existe además de `_enviar_telegram` porque el bot necesita métodos que no son
    sendMessage: `answerCallbackQuery` para sacarle el reloj al botón que el operador
    acaba de tocar. El token se arma acá y no se devuelve a nadie.
    """
    token = getattr(settings, 'TELEGRAM_BOT_TOKEN', '')
    if not token:
        return False
    req = urllib.request.Request(
        f'https://api.telegram.org/bot{token}/{metodo}',
        data=json.dumps(cuerpo).encode('utf-8'),
        method='POST', headers={'Content-Type': 'application/json'},
    )
    try:
        urllib.request.urlopen(req, timeout=5)
        return True
    except (urllib.error.URLError, urllib.error.HTTPError):
        # Sin la URL en el log: lleva el token adentro.
        logger.warning('Telegram: falló %s.', metodo, exc_info=True)
        return False


def _enviar_telegram(chat_id, texto, *, teclado=None) -> bool:
    """POST a sendMessage de la API de Telegram — nunca lanza, mismo criterio que
    `_enviar_webhook_teams`: un bot caído o un chat_id mal cargado no debe tumbar la
    ingesta de métricas ni impedir que salga el correo. Mismo patrón stdlib (urllib, sin
    sumar `requests`) que el webhook de Teams y que apps.mqtt_worker.emqx_admin.

    Devuelve si se envió, porque a diferencia del webhook acá sí hay quien necesita
    saberlo: el diagnóstico de IA no debe darse por entregado si el mensaje no salió.

    `teclado` es el `inline_keyboard` que acompaña la respuesta. Va solo en el ÚLTIMO
    fragmento cuando el mensaje se trocea: repetirlo en cada trozo dejaría tres filas de
    botones idénticas en el chat, y la de arriba operaría sobre un mensaje viejo.

    **El token nunca entra en un log.** Va dentro de la URL, así que los mensajes de
    error informan el chat_id —que no es secreto— y jamás la URL armada. Mismo criterio
    que las contraseñas del POS y del broker en el resto del proyecto.
    """
    token = getattr(settings, 'TELEGRAM_BOT_TOKEN', '')
    if not token or not chat_id:
        return False
    url = f'https://api.telegram.org/bot{token}/sendMessage'
    fragmentos = _trozos_telegram(texto)
    for indice, fragmento in enumerate(fragmentos):
        cuerpo = {
            'chat_id': str(chat_id),
            'text': fragmento,
            # Sin parse_mode a propósito: el texto lo arma el sistema con códigos de
            # estación, circuitos y mensajes del POS, que traen guiones bajos y asteriscos.
            # Con Markdown activo Telegram rechaza el mensaje entero por un carácter suelto.
            'disable_web_page_preview': True,
        }
        if teclado and indice == len(fragmentos) - 1:
            cuerpo['reply_markup'] = {'inline_keyboard': teclado}
        datos = json.dumps(cuerpo).encode('utf-8')
        req = urllib.request.Request(
            url, data=datos, method='POST', headers={'Content-Type': 'application/json'},
        )
        try:
            urllib.request.urlopen(req, timeout=5)
        except (urllib.error.URLError, urllib.error.HTTPError):
            logger.warning('No se pudo notificar por Telegram (chat %s).', chat_id, exc_info=True)
            return False
    return True


def canales_telegram_para(unidad):
    """Los CanalNotificacion de Telegram activos que aplican a `unidad`: el suyo propio
    más el global. Misma resolución "global o del cliente" que los de Teams."""
    return CanalNotificacion.objects.filter(
        activo=True, tipo=CanalNotificacion.Tipo.TELEGRAM,
    ).filter(Q(unidad_negocio__isnull=True) | Q(unidad_negocio=unidad))


def notificar_alerta(alerta, *, escalamiento=False):
    """Correo a quienes tengan acceso a la unidad de negocio de la estación (equipo
    interno + usuarios de ese cliente) con email configurado, más un webhook de Teams
    si hay un CanalNotificacion activo para esa unidad de negocio (o uno global). Se
    llama al abrir una alerta nueva y, con escalamiento=True, desde
    escalar_alertas_abiertas cuando sigue sin reconocerse — mismos destinatarios y
    canales, solo cambia el texto (decisión del usuario: sin lista de escalamiento
    separada)."""
    from django.contrib.auth import get_user_model
    User = get_user_model()

    unidad = alerta.estacion.farmacia.unidad_negocio
    destinatarios = list(
        User.objects.filter(
            Q(is_superuser=True) | Q(perfil__acceso_todas_unidades=True) | Q(perfil__unidades_negocio=unidad),
        ).exclude(email='').values_list('email', flat=True).distinct()
    )

    if alerta.regla.metrica == Metrica.BITLOCKER_DESHABILITADO:
        # Condición binaria: "valor (umbral: >= X)" no significa nada aquí, a diferencia
        # de las métricas numéricas (CPU/RAM/latencia/sin_heartbeat).
        detalle_condicion = 'BitLocker deshabilitado.'
    elif alerta.regla.metrica == Metrica.DESFASE_RELOJ:
        # "Valor: -95 (umbral: >= 90.0)" es ilegible: el valor guardado tiene signo y el
        # umbral se compara contra el absoluto, así que el número no parece cumplirlo.
        # Lo que hay que leer de un vistazo es cuánto se corrió, para qué lado, y cuánto
        # margen queda antes de que la estación se quede muda.
        from apps.catalogo.models import Estacion

        segundos = int(alerta.valor_disparador)
        direccion = 'adelantada' if segundos > 0 else 'atrasada'
        margen = Estacion.UMBRAL_RELOJ_INCOMUNICADO_SEGUNDOS - abs(segundos)
        detalle_condicion = (
            f'Reloj {direccion} {abs(segundos)} s (umbral: {int(alerta.regla.umbral)} s).\n'
            f'Quedan {margen} s de margen: pasados los '
            f'{Estacion.UMBRAL_RELOJ_INCOMUNICADO_SEGUNDOS} s la estación descarta todo '
            f'comando y hay que ir al local.'
        )
    else:
        detalle_condicion = (
            f'Valor: {alerta.valor_disparador} (umbral: {alerta.regla.get_operador_display()} {alerta.regla.umbral}).'
        )
    prefijo_asunto = 'SIN ATENDER — ' if escalamiento else ''
    asunto = f'[{alerta.regla.get_severidad_display()}] {prefijo_asunto}{alerta.regla.nombre} — {alerta.estacion.codigo}'
    aviso_escalamiento = ''
    if escalamiento:
        minutos = ConfiguracionMonitoreo.obtener().minutos_escalamiento_alerta
        aviso_escalamiento = f'Sigue ABIERTA sin reconocer hace más de {minutos} minutos.\n'
    cuerpo = (
        f'{aviso_escalamiento}'
        f'{alerta.regla.nombre} en {alerta.estacion.codigo} ({unidad.codigo}).\n'
        f'{detalle_condicion}\n'
        f'Abierta: {alerta.abierta_en:%Y-%m-%d %H:%M:%S}.'
    )

    if destinatarios:
        # fail_silently: un SMTP caído no debe tumbar la ingesta de métricas del worker
        # MQTT ni el cron de marcar_estaciones_offline — la alerta ya quedó guardada en la BD.
        send_mail(asunto, cuerpo, None, destinatarios, fail_silently=True)
    else:
        logger.info('Alerta #%s sin destinatarios de correo (unidad %s).', alerta.pk, unidad.codigo)

    canales_teams = CanalNotificacion.objects.filter(
        activo=True, tipo=CanalNotificacion.Tipo.WEBHOOK_TEAMS,
    ).filter(Q(unidad_negocio__isnull=True) | Q(unidad_negocio=unidad))
    for canal in canales_teams:
        _enviar_webhook_teams(canal.destino, f'{asunto}\n\n{cuerpo}')

    # El emoji va delante del asunto para que en la lista de chats del teléfono se vea
    # la severidad antes que el texto: con varias alertas encima, es lo que decide cuál
    # abrir primero.
    emoji = _EMOJI_SEVERIDAD.get(alerta.regla.severidad, '')
    texto_telegram = f'{emoji} {asunto}\n\n{cuerpo}'.strip()
    # El aviso de reloj corrido viene con el botón para corregirlo. Es el único caso en
    # que la notificación ofrece accionar, y se justifica porque la acción es única,
    # obvia y contra el reloj: pasados los 120 s de desfase la estación deja de obedecer
    # y el arreglo ya no se puede hacer en remoto. Sin el botón, el aviso llega al
    # teléfono y obliga a abrir la computadora para dar un clic.
    #
    # `pedirsync:`/`pedirack:` y no `sync:`/`ack:`: los botones piden confirmación, no
    # ejecutan. Un roce sobre una notificación no puede accionar sobre una caja.
    #
    # "Reconocer" va en TODA alerta, no solo en las de reloj: no arregla nada —cambia una
    # fila— pero corta el reenvío por escalamiento, y el momento en que eso importa es
    # justo cuando nadie va a abrir la computadora. Sin el botón, la única forma de decir
    # "ya la vi" a las 3 de la mañana era entrar al panel.
    botones = [{
        'text': '✔ Reconocer',
        'callback_data': f'pedirack:{alerta.pk}',
    }]
    if alerta.regla.metrica == Metrica.DESFASE_RELOJ:
        botones.append({
            'text': f'🕐 Sincronizar {alerta.estacion.codigo}',
            'callback_data': f'pedirsync:{alerta.estacion.codigo}',
        })
    teclado_alerta = [botones]
    for canal in canales_telegram_para(unidad):
        _enviar_telegram(canal.destino, texto_telegram, teclado=teclado_alerta)


def escalar_alertas_abiertas() -> int:
    """Celery Beat periódico (mismo mecanismo que marcar_estaciones_offline): reenvía
    la notificación de cualquier Alerta en estado ABIERTA (nunca reconocida —
    RECONOCIDA ya significa que alguien la vio, aunque no la haya resuelto todavía) más
    vieja que UMBRAL_ESCALAMIENTO_MINUTOS, por los mismos canales que la notificación
    original. Marca escalada_en para no repetir el aviso en cada corrida siguiente."""
    minutos = ConfiguracionMonitoreo.obtener().minutos_escalamiento_alerta
    umbral = timezone.now() - timedelta(minutes=minutos)
    candidatas = Alerta.objects.filter(
        estado=Alerta.Estado.ABIERTA, escalada_en__isnull=True, abierta_en__lte=umbral,
    ).select_related('regla', 'estacion__farmacia__unidad_negocio')
    escaladas = 0
    for alerta in candidatas:
        notificar_alerta(alerta, escalamiento=True)
        alerta.escalada_en = timezone.now()
        alerta.save(update_fields=['escalada_en'])
        escaladas += 1
    return escaladas


def purgar_metricas_antiguas(*, dias: int = 30) -> int:
    """Borra MuestraMetrica más viejas que `dias`. Reemplaza el `vaciar_logs` del sistema
    viejo (que borraba TODO cada domingo) — retención por antigüedad, no total.

    En producción NO hay ninguna política de retención nativa: verificado el 16-sep-2026,
    `timescaledb_information.hypertables` devuelve cero filas. Las tres migraciones que
    intentan `create_hypertable` (monitoreo 0002, 0006 y 0021) fallan siempre con
    "cannot create a unique index without the column timestamp" — el PK `id` que Django
    crea solo no incluye la columna de particionado. Fallan sin abortar, así que el
    despliegue pasa y nadie se entera.

    O sea: esta función es lo ÚNICO que controla el crecimiento de la tabla. Desactivarla
    creyendo que TimescaleDB se hace cargo dejaría la base creciendo sin límite.

    La llaman tanto el comando manual (`purgar_metricas`) como la tarea periódica de
    Celery.
    """
    umbral = timezone.now() - timedelta(days=dias)
    borradas, _ = MuestraMetrica.objects.filter(timestamp__lt=umbral).delete()
    return borradas


def purgar_muestras_red_antiguas(*, dias: int = 30) -> int:
    """Borra MuestraRedFarmacia más viejas que `dias`.

    Faltaba: `muestra_metrica` y `evento_monitoreo` tenían purga e hypertable desde el
    principio, y esta tabla quedó sin ninguna de las dos. Hoy no se nota porque solo 4
    Mikrotiks responden SNMP, pero se escribe una fila por farmacia cada 5 minutos —
    con las 700 respondiendo son ~6 millones de filas por mes, y sería la tabla más
    grande del sistema. El momento barato de arreglarlo es antes de que crezca.

    Mismo criterio de retención que las otras dos: 30 días. Y la misma advertencia — ver
    `purgar_metricas_antiguas`: no hay política nativa de TimescaleDB detrás, esto es lo
    único que acota el crecimiento.

    Dimensión, con los números medidos el 16-sep-2026 (~200 bytes por fila): con las 700
    farmacias reportando cada 5 minutos son ~200.000 filas por día, y con 30 días de
    retención el régimen estable queda en ~1,2 GB. PostgreSQL sin hypertable lo sostiene
    de sobra con el índice (farmacia, -timestamp) que ya existe.
    """
    umbral = timezone.now() - timedelta(days=dias)
    borradas, _ = MuestraRedFarmacia.objects.filter(timestamp__lt=umbral).delete()
    return borradas


def purgar_eventos_monitoreo_antiguos(*, dias: int = 30) -> int:
    """Borra EventoMonitoreo más viejos que `dias` — mismo criterio de retención que
    purgar_metricas_antiguas (EstadoDispositivo no se purga: es un snapshot, no un
    histórico)."""
    umbral = timezone.now() - timedelta(days=dias)
    borrados, _ = EventoMonitoreo.objects.filter(timestamp__lt=umbral).delete()
    return borrados


# --- Sondeo por ping de los activos sin agente (ver models.EstadoRedActivo) ---

# Tope de equipos por pedido. No es una limitación técnica sino de tiempo: el agente
# pingea en serie con 1 s de espera, así que 40 destinos son ~40 s en el peor caso. Una
# farmacia estándar tiene menos de 10 activos de red; el tope existe para que una carga
# masiva mal hecha no deje a una estación pingeando durante minutos.
MAX_OBJETIVOS_POR_PEDIDO = 40


def objetivos_de_ping(farmacia) -> str:
    """"id:ip,id:ip,…" con los activos de `farmacia` que hay que pingear.

    Quedan afuera, a propósito:

    - Los que tienen estación RMM vinculada. Esos ya reportan su propio estado por
      heartbeat; pingearlos sería una segunda fuente de verdad sobre lo mismo, y cuando
      dos fuentes discrepan nadie sabe cuál creer.
    - Los dados de baja. Un equipo que se retiró no responde, y eso es correcto, no una
      incidencia.
    - Los que no tienen IP cargada. Sin IP no hay a qué pingear — y son la mayoría hoy,
      hasta que se cargue la planilla.
    """
    from apps.activos.models import Activo

    candidatos = (
        Activo.objects
        .filter(farmacia=farmacia, estacion__isnull=True)
        .exclude(ip__isnull=True)
        .exclude(estado=Activo.Estado.DADO_DE_BAJA)
        .order_by('codigo')
        .values_list('pk', 'ip')[:MAX_OBJETIVOS_POR_PEDIDO]
    )
    return ','.join(f'{pk}:{ip}' for pk, ip in candidatos)


def solicitar_sondeo_activos_via_agente() -> int:
    """Por cada farmacia con activos pingeables y una estación en línea, le pide a esa
    estación que los pingee. Devuelve cuántos pedidos se enviaron.

    Que el pedido salga no significa que el sondeo funcione: eso se ve en
    `EstadoRedActivo.ultima_verificacion`. Mismo criterio que
    `solicitar_sondeo_red_farmacias_via_agente`.
    """
    from apps.catalogo.models import Estacion
    from apps.catalogo.services import enviar_consultar_activos_farmacia

    candidatas = (
        Estacion.objects
        .filter(
            estado_aprobacion=Estacion.EstadoAprobacion.APROBADA,
            estado_conexion=Estacion.EstadoConexion.ONLINE,
            farmacia__isnull=False,
        )
        .select_related('farmacia')
        .order_by('farmacia_id', 'codigo')
    )

    vistas = set()
    enviados = 0
    for estacion in candidatas:
        if estacion.farmacia_id in vistas:
            continue
        vistas.add(estacion.farmacia_id)
        objetivos = objetivos_de_ping(estacion.farmacia)
        if not objetivos:
            continue
        if enviar_consultar_activos_farmacia(estacion, objetivos):
            enviados += 1
    return enviados


def registrar_estado_red_activos(*, estacion, resultados: list) -> int:
    """Guarda lo que reportó el agente. `resultados` = [{activo_id, ip, responde, latencia_ms}].

    `ultima_respuesta` solo avanza cuando el equipo contestó: es el "último visto", y
    pisarlo en cada verificación borraría justamente el dato que sirve para saber hace
    cuánto está caído.
    """
    from apps.activos.models import Activo
    from .models import EstadoRedActivo

    ahora = timezone.now()
    permitidos = set(
        Activo.objects.filter(farmacia=estacion.farmacia_id).values_list('pk', flat=True),
    )
    guardados = 0
    for fila in resultados:
        try:
            activo_id = int(fila['activo_id'])
        except (KeyError, TypeError, ValueError):
            continue
        # Una estación solo puede reportar sobre activos de SU farmacia: el payload lo
        # arma el agente y no puede ser autoridad sobre el inventario de otra.
        if activo_id not in permitidos:
            logger.warning(
                'La estación %s reportó el activo %s, que no es de su farmacia — se ignora.',
                estacion.codigo, activo_id,
            )
            continue
        responde = bool(fila.get('responde'))
        estado, _ = EstadoRedActivo.objects.get_or_create(
            activo_id=activo_id,
            defaults={'ip_sondeada': fila.get('ip') or '0.0.0.0', 'ultima_verificacion': ahora},
        )
        estado.ip_sondeada = fila.get('ip') or estado.ip_sondeada
        estado.responde = responde
        estado.latencia_ms = fila.get('latencia_ms') if responde else None
        estado.ultima_verificacion = ahora
        estado.estacion_que_sondeo = estacion
        if responde:
            estado.ultima_respuesta = ahora
        estado.save()
        guardados += 1
    return guardados


# --- servicios externos que consume el POS (ver models.EstadoServicioPos) ---


def registrar_servicios_pos(*, estacion, resultados: list) -> int:
    """Guarda lo que reportó el agente y evalúa la regla de alerta por cada servicio.

    `ultima_respuesta` solo avanza cuando el servicio contestó: es el "último visto", y
    pisarlo en cada chequeo borraría el dato que dice hace cuánto está caído.

    La muestra de latencia se guarda solo si respondió. Una latencia nula no es un punto
    en la curva, y graficarla como cero diría que contestó instantáneamente.
    """
    from .models import EstadoServicioPos, MuestraServicioPos, ServicioPosMonitoreado

    # Las claves validas salen del catalogo ACTIVO, no de un choices fijo en codigo: ese
    # es el cambio que permite agregar una plataforma desde el admin sin un deploy.
    #
    # Se sigue validando —no se acepta cualquier cosa que mande un agente— por dos
    # motivos: una clave inventada crearia filas de EstadoServicioPos que nadie
    # administra, y `servicio` tiene 20 caracteres, asi que un valor largo reventaria el
    # insert en PostgreSQL (en SQLite entraria igual, que es como este tipo de bug se
    # escapa hasta produccion).
    #
    # Se filtra por `activo`: desactivar una entrada tiene que dejar de aceptar reportes
    # de inmediato, sin esperar a que el agente se entere del catalogo nuevo. El historial
    # ya guardado no se toca.
    validos = set(
        ServicioPosMonitoreado.objects.filter(activo=True).values_list('clave', flat=True)
    )
    ahora = timezone.now()
    guardados = 0

    for fila in resultados:
        servicio = fila.get('servicio')
        if servicio not in validos:
            logger.warning(
                '%s reportó el servicio desconocido %r — se ignora.', estacion.codigo, servicio,
            )
            continue

        disponible = bool(fila.get('disponible'))
        latencia = fila.get('latencia_ms') if disponible else None

        estado, _ = EstadoServicioPos.objects.get_or_create(
            estacion=estacion, servicio=servicio,
            defaults={'ultima_verificacion': ahora},
        )
        estado.disponible = disponible
        estado.latencia_ms = latencia
        estado.mensaje = (fila.get('mensaje') or '')[:300]
        estado.endpoint = (fila.get('endpoint') or '')[:200]
        estado.critico = bool(fila.get('critico', True))
        estado.ultima_verificacion = ahora
        if disponible:
            estado.ultima_respuesta = ahora
        estado.save()
        guardados += 1

        if disponible and isinstance(latencia, int):
            MuestraServicioPos.objects.create(
                estacion=estacion, servicio=servicio, latencia_ms=latencia,
            )

        evaluar_regla_servicio_pos(estacion, estado)

    return guardados


def evaluar_regla_servicio_pos(estacion, estado) -> None:
    """Abre o resuelve la alerta de un servicio del POS.

    De la familia de `evaluar_regla_bitlocker`/`evaluar_regla_pos_errores`: es un estado
    binario reportado puntualmente, no una serie de tiempo, así que no hay condición
    sostenida que esperar.

    La severidad NO la decide esta función: la toma de la `ReglaAlerta` que el operador
    configuró. Lo que sí hace es respetar el campo `critico` del servicio — sin la base
    local la caja no vende, sin Odoo sí — aplicando solo las reglas cuya severidad
    corresponde. Así "si cae un servicio crítico, alerta crítica" se expresa con las dos
    reglas que ya existen en el modelo, sin inventar un concepto nuevo de severidad.
    """
    from .models import EstadoServicioPos, Metrica, ReglaAlerta

    unidad = estacion.farmacia.unidad_negocio
    severidad = ReglaAlerta.Severidad.CRITICAL if estado.critico else ReglaAlerta.Severidad.WARNING

    # Se mira el conjunto de servicios de esa criticidad, no solo el que acaba de
    # reportar. La alerta es por (regla, estación) y las tres no críticas comparten
    # regla: resolver por el servicio que acaba de volver dejaría la alerta cerrada con
    # otro todavía caído. Visto en producción el 17-sep-2026 — Odoo sin responder en
    # ML017-B y la alerta figurando resuelta porque pg_central habia contestado despues.
    #
    # Se excluye lo que NUNCA respondió (`ultima_respuesta` vacía): eso no es una caída
    # sino configuración pendiente o un servicio que ya no existe, y tratarlo como
    # incidente convierte un estado permanente en una alerta que se reabre para siempre.
    # Mismo criterio que `EstadoEnlaceFarmacia.nunca_respondio` para los enlaces.
    #
    # Encontrado en producción el 18-sep-2026: Odoo (192.168.112.125:8069) figuraba en el
    # .exe.Config del POS de las 9 estaciones con agente y no había respondido ni una vez
    # desde que existe el monitor — el servicio está de baja. Cada estación abría su
    # alerta de "servicio del POS sin responder" por algo que nadie iba a arreglar.
    hay_caido = EstadoServicioPos.objects.filter(
        estacion=estacion, critico=estado.critico, disponible=False,
        ultima_respuesta__isnull=False,
    ).exists()

    for regla in reglas_aplicables_a(unidad, metrica=Metrica.SERVICIO_POS_CAIDO):
        if regla.severidad != severidad:
            continue
        if hay_caido:
            # valor_disparador es obligatorio y no hay número natural para "no responde";
            # 0 es el marcador, mismo criterio que evaluar_regla_bitlocker.
            abrir_o_mantener_alerta(regla, estacion, valor=0)
        else:
            resolver_condicion(regla, estacion)


# --- Resumen de operación (Centro de Monitoreo y /estado del bot) ---
#
# Vive acá y no en la vista ni en el bot porque los dos responden la misma pregunta
# —"¿cómo está todo ahora?"— y tenerla escrita dos veces garantiza que un día difieran.
# `_comando_estado` de telegram_bot.py consumía su propia copia hasta el 19-sep-2026.

# Cuánto puede tardar cada fuente en refrescarse antes de que su dato se considere viejo.
# Salen de CELERY_BEAT_SCHEDULE, con margen: el sondeo de enlaces corre cada 2 min, así
# que a los 6 ya se saltearon dos vueltas y eso no es demora, es que algo dejó de correr.
#
# Existen porque un tablero en verde con datos viejos es peor que no tener tablero: el
# agente de mesa de ayuda concluye "no hay nada que atender" de una pantalla que en
# realidad dejó de mirar.
TOLERANCIA_FRESCURA_MINUTOS = {
    'enlaces': 6,           # sondear-enlaces-farmacias, cada 2 min
    'red_farmacias': 15,    # sondear-red-farmacias-via-agente, cada 5 min
    'estaciones': 10,       # heartbeat del agente (~30 s) + marcar-estaciones-offline (60 s)
    'servicios_pos': 30,    # lo reporta el agente en su propio bucle, no una tarea de Beat
}


def _unidades_a_filtrar(unidades):
    """`None` = sin filtrar (todas las que el llamador ya decidió mostrar)."""
    return None if unidades is None else list(unidades)


def _por_unidad(queryset, unidades, lookup):
    unidades = _unidades_a_filtrar(unidades)
    if unidades is None:
        return queryset
    return queryset.filter(**{f'{lookup}__in': unidades})


def resumen_operacion(unidades=None) -> dict:
    """Los números de cabecera, con consultas AGREGADAS.

    `unidades` = iterable de UnidadNegocio, o None para todas. El llamador decide el
    alcance (la vista con el scope del usuario, el bot con todo), esta función no lee
    la sesión ni el request.

    Todo sale de `count()` y no de traer objetos: esta pantalla la dejan abierta varios
    agentes todo el día, así que cada refresco es carga real. Las listas de detalle, que
    sí traen filas, están acotadas aparte por `_MAX_FILAS_CENTRO`.
    """
    from datetime import timedelta

    from apps.catalogo.models import Estacion

    from .models import (
        Alerta, EstadoEnlaceFarmacia, EstadoRedActivo, EstadoServicioPos,
        MuestraRedFarmacia, ReglaAlerta,
    )

    ahora = timezone.now()

    estaciones = _por_unidad(
        Estacion.objects.filter(estado_aprobacion=Estacion.EstadoAprobacion.APROBADA),
        unidades, 'farmacia__unidad_negocio',
    )
    total_estaciones = estaciones.count()
    en_linea = estaciones.filter(
        ultimo_heartbeat__gte=ahora - timedelta(minutes=TOLERANCIA_FRESCURA_MINUTOS['estaciones']),
    ).count()

    abiertas = _por_unidad(
        Alerta.objects.filter(estado=Alerta.Estado.ABIERTA),
        unidades, 'estacion__farmacia__unidad_negocio',
    )
    criticas = abiertas.filter(regla__severidad=ReglaAlerta.Severidad.CRITICAL).count()
    advertencias = abiertas.filter(regla__severidad=ReglaAlerta.Severidad.WARNING).count()

    # Se excluye lo que NUNCA respondió: no es una caída sino configuración pendiente, y
    # contarlo como incidente infla el tablero con algo que nadie va a resolver hoy.
    # Mismo criterio que ya aplican notificar_cambios_enlaces y evaluar_regla_servicio_pos.
    enlaces_caidos = _por_unidad(
        EstadoEnlaceFarmacia.objects.filter(alcanzable=False, respondio_alguna_vez=True),
        unidades, 'farmacia__unidad_negocio',
    ).count()

    servicios = _por_unidad(
        EstadoServicioPos.objects.filter(disponible=False, ultima_respuesta__isnull=False),
        unidades, 'estacion__farmacia__unidad_negocio',
    )
    pos_criticos = servicios.filter(critico=True).count()
    pos_no_criticos = servicios.filter(critico=False).count()

    corte_red = ahora - timedelta(hours=EstadoRedActivo.HORAS_VERIFICACION_VIGENTE)
    sin_sondeo = _por_unidad(
        EstadoRedActivo.objects.filter(ultima_verificacion__lt=corte_red),
        unidades, 'activo__unidad_negocio',
    ).count()

    ultima_muestra = _por_unidad(
        MuestraRedFarmacia.objects.all(), unidades, 'farmacia__unidad_negocio',
    ).order_by('-timestamp').values_list('timestamp', flat=True).first()

    return {
        'estaciones_total': total_estaciones,
        'estaciones_en_linea': en_linea,
        'estaciones_fuera': total_estaciones - en_linea,
        'alertas_criticas': criticas,
        'alertas_advertencias': advertencias,
        'enlaces_caidos': enlaces_caidos,
        'pos_criticos': pos_criticos,
        'pos_no_criticos': pos_no_criticos,
        'activos_sin_sondeo': sin_sondeo,
        'ultimo_sondeo_red': ultima_muestra,
        'calculado_en': ahora,
    }


def fuente_desactualizada(instante, clave: str) -> bool:
    """True si `instante` quedó fuera de la tolerancia de esa fuente.

    `None` cuenta como desactualizado a propósito: "nunca se midió" y "se midió hace
    mucho" llevan a la misma conclusión operativa —no te fíes de este bloque— y
    distinguirlos en el tablero solo agrega ruido.
    """
    from datetime import timedelta

    if instante is None:
        return True
    minutos = TOLERANCIA_FRESCURA_MINUTOS.get(clave)
    if minutos is None:
        return False
    return (timezone.now() - instante) > timedelta(minutes=minutos)
