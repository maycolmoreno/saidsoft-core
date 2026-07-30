from datetime import timedelta

from django.contrib.auth.decorators import login_required
from django.db.models import Count, F, Q
from django.shortcuts import render
from django.utils import timezone

from apps.catalogo.models import Estacion, Grupo
from apps.cuentas.services import unidades_negocio_en_foco
from apps.despliegues.models import Despliegue
from apps.monitoreo.models import Alerta

ONLINE_UMBRAL_MINUTOS = 5


@login_required
def dashboard(request):
    umbral_online = timezone.now() - timedelta(minutes=ONLINE_UMBRAL_MINUTOS)
    visibles = unidades_negocio_en_foco(request)
    en_alcance = Q(farmacias__unidad_negocio__in=visibles)

    # Un Grupo (canal TRX) puede estar compartido por farmacias de varias unidades de
    # negocio (ver apps.cumplimiento) — el dashboard solo debe mostrar los grupos con
    # al menos una farmacia visible para este usuario, y contar únicamente esas
    # farmacias/estaciones, no las de un tenant al que no tiene acceso.
    grupos = Grupo.objects.filter(en_alcance).distinct().annotate(
        total_estaciones=Count('farmacias__estaciones', filter=en_alcance, distinct=True),
        total_farmacias=Count('farmacias', filter=en_alcance, distinct=True),
        conformes=Count(
            'farmacias__estaciones',
            filter=Q(farmacias__estaciones__version_pos=F('version_objetivo')) & en_alcance,
            distinct=True,
        ),
        online=Count(
            'farmacias__estaciones',
            filter=Q(farmacias__estaciones__ultimo_heartbeat__gte=umbral_online) & en_alcance,
            distinct=True,
        ),
        pendientes=Count(
            'farmacias__estaciones',
            filter=Q(farmacias__estaciones__estado_aprobacion=Estacion.EstadoAprobacion.PENDIENTE) & en_alcance,
            distinct=True,
        ),
    ).order_by('codigo')

    for g in grupos:
        g.pct_conforme = round(100 * g.conformes / g.total_estaciones) if g.total_estaciones else None

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

    return render(request, 'panel/dashboard.html', {
        'grupos': grupos,
        'despliegues_activos': despliegues_activos,
        'total_pendientes': total_pendientes,
        'total_estaciones': total_estaciones,
        'total_online': total_online,
        'total_alertas_abiertas': total_alertas_abiertas,
    })
