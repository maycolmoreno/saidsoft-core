from django.contrib.auth.decorators import login_required
from django.shortcuts import render

from apps.catalogo.models import Estacion
from apps.catalogo.services import calcular_matriz_cumplimiento
from apps.cuentas.services import unidades_negocio_en_foco
from apps.despliegues.models import Despliegue
from apps.monitoreo.models import Alerta
from apps.mqtt_worker.models import WorkerHeartbeat
from apps.mqtt_worker.services import NOMBRE_WORKER_MQTT

ONLINE_UMBRAL_MINUTOS = 5

# 3x el intervalo de latido del worker (30s) con margen para jitter/latencia de red,
# antes de considerar que dejó de reportarse.
WORKER_MQTT_UMBRAL_SEGUNDOS = 90


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

    return render(request, 'panel/dashboard.html', {
        'grupos': grupos,
        'despliegues_activos': despliegues_activos,
        'total_pendientes': total_pendientes,
        'total_estaciones': total_estaciones,
        'total_online': total_online,
        'total_alertas_abiertas': total_alertas_abiertas,
        'worker_mqtt_activo': worker_mqtt_activo,
        'worker_mqtt_ultimo_latido': latido_worker.ultimo_latido if latido_worker else None,
    })
