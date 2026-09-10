from datetime import timedelta

from django.contrib.auth.decorators import login_required
from django.db.models import F, Q
from django.shortcuts import render
from django.utils import timezone

from apps.catalogo.models import Estacion
from apps.catalogo.services import calcular_matriz_cumplimiento
from apps.cuentas.services import unidades_negocio_en_foco
from apps.despliegues.models import Despliegue
from apps.monitoreo.models import Alerta
from apps.mqtt_worker.models import WorkerHeartbeat
from apps.mqtt_worker.management.commands.registrar_latido import NOMBRE_RESPALDO
from apps.mqtt_worker.services import NOMBRE_WORKER_MQTT

ONLINE_UMBRAL_MINUTOS = 5

# 3x el intervalo de latido del worker (30s) con margen para jitter/latencia de red,
# antes de considerar que dejó de reportarse.
WORKER_MQTT_UMBRAL_SEGUNDOS = 90

# El respaldo corre a las 02:00 (deploy/saidsoft-respaldo.timer). 26h = un día más dos
# horas de gracia: alcanza para que un arranque tardío tras un corte de energía corra su
# ejecución atrasada sin que el panel grite, y no tanto como para tapar un día perdido.
RESPALDO_UMBRAL_HORAS = 26


@login_required
def dashboard(request):
    visibles = unidades_negocio_en_foco(request)

    # Un Grupo (canal TRX) puede estar compartido por farmacias de varias unidades de
    # negocio (ver apps.cumplimiento) — calcular_matriz_cumplimiento ya se encarga de
    # mostrar solo los grupos con al menos una farmacia visible para este usuario, y de
    # contar únicamente esas farmacias/estaciones, no las de un tenant al que no tiene acceso.
    grupos = calcular_matriz_cumplimiento(visibles, umbral_online_minutos=ONLINE_UMBRAL_MINUTOS)

    despliegues_activos = (
        Despliegue.objects
        .filter(estado__in=[Despliegue.Estado.PUBLICANDO, Despliegue.Estado.PAUSADO], unidad_negocio__in=visibles)
        .order_by('-fecha_publicacion')
    )
    total_pendientes = Estacion.objects.filter(
        estado_aprobacion=Estacion.EstadoAprobacion.PENDIENTE, farmacia__unidad_negocio__in=visibles,
    ).count()
    total_alertas_abiertas = Alerta.objects.filter(
        estado__in=[Alerta.Estado.ABIERTA, Alerta.Estado.RECONOCIDA],
        estacion__farmacia__unidad_negocio__in=visibles,
    ).count()

    # Agregados en Python sobre `grupos` (ya evaluado arriba) para la franja de KPI
    # del dashboard — no son consultas nuevas, solo sumar lo que ya se trajo.
    total_estaciones = sum(g.total_estaciones for g in grupos)
    total_online = sum(g.online for g in grupos)

    # Estado del worker MQTT: antes solo se podía inferir indirectamente (estaciones
    # empezando a reportarse offline, tardío). No tiene tenant propio — es salud de
    # infraestructura, no dato de cliente, así que se muestra igual para todos.
    latido_worker = WorkerHeartbeat.objects.filter(nombre=NOMBRE_WORKER_MQTT).first()
    worker_mqtt_activo = bool(
        latido_worker
        and (timezone.now() - latido_worker.ultimo_latido).total_seconds() < WORKER_MQTT_UMBRAL_SEGUNDOS
    )

    # Salud del respaldo. El panel vigilaba 8 estaciones y no la máquina que lo
    # hospeda: el 6 y el 7-sep-2026 no hubo respaldo (el NUC estuvo apagado el fin de
    # semana) y nadie se enteró en dos días. La fila la escribe deploy/backup.sh al
    # terminar bien, vía `manage.py registrar_latido respaldo`.
    # Las dos listas de abajo existen porque el dashboard terminaba a media pantalla y
    # dejaba el trabajo a medias: decía "8 alertas abiertas" y "8/8 en línea" sin decir
    # cuáles ni desde cuándo, así que había que salir a Alertas y a Estaciones para
    # empezar a entender. Van acotadas a 6 filas: es un tablero, no un listado.
    alertas_recientes = (
        Alerta.objects
        .filter(
            estado__in=[Alerta.Estado.ABIERTA, Alerta.Estado.RECONOCIDA],
            estacion__farmacia__unidad_negocio__in=visibles,
        )
        .select_related('regla', 'estacion', 'estacion__farmacia')
        .order_by('-abierta_en')[:6]
    )

    # Solo aprobadas: una pendiente de aprobación no reporta porque todavía no le toca,
    # y ya tiene su propio aviso arriba. `nulls_first` pone adelante a las que nunca
    # reportaron — una estación aprobada que jamás dio señal es una instalación fallida,
    # no una caída.
    limite_online = timezone.now() - timedelta(minutes=ONLINE_UMBRAL_MINUTOS)
    sin_reportar = (
        Estacion.objects
        .filter(
            estado_aprobacion=Estacion.EstadoAprobacion.APROBADA,
            farmacia__unidad_negocio__in=visibles,
        )
        .filter(Q(ultimo_heartbeat__isnull=True) | Q(ultimo_heartbeat__lt=limite_online))
        .select_related('farmacia')
        .order_by(F('ultimo_heartbeat').asc(nulls_first=True))
    )
    total_sin_reportar = sin_reportar.count()

    latido_respaldo = WorkerHeartbeat.objects.filter(nombre=NOMBRE_RESPALDO).first()
    horas_sin_respaldo = None
    if latido_respaldo:
        horas_sin_respaldo = (timezone.now() - latido_respaldo.ultimo_latido).total_seconds() / 3600

    return render(request, 'panel/dashboard.html', {
        'grupos': grupos,
        'despliegues_activos': despliegues_activos,
        'total_pendientes': total_pendientes,
        'total_estaciones': total_estaciones,
        'total_online': total_online,
        'total_alertas_abiertas': total_alertas_abiertas,
        'alertas_recientes': alertas_recientes,
        'sin_reportar': sin_reportar[:6],
        'total_sin_reportar': total_sin_reportar,
        'worker_mqtt_activo': worker_mqtt_activo,
        'worker_mqtt_ultimo_latido': latido_worker.ultimo_latido if latido_worker else None,
        'respaldo_ultimo': latido_respaldo.ultimo_latido if latido_respaldo else None,
        # None (nunca respaldó) cuenta como NO al día: es el estado de un servidor
        # recién montado y también el de uno donde el timer nunca se instaló.
        'respaldo_al_dia': horas_sin_respaldo is not None and horas_sin_respaldo < RESPALDO_UMBRAL_HORAS,
        'respaldo_horas': horas_sin_respaldo,
    })
