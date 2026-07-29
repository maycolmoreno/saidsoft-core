from datetime import timedelta

from django.contrib.auth.decorators import login_required
from django.db.models import Count, F, Q
from django.shortcuts import render
from django.utils import timezone

from apps.catalogo.models import Estacion, Grupo
from apps.despliegues.models import Despliegue

ONLINE_UMBRAL_MINUTOS = 5


@login_required
def dashboard(request):
    umbral_online = timezone.now() - timedelta(minutes=ONLINE_UMBRAL_MINUTOS)

    grupos = Grupo.objects.annotate(
        total_estaciones=Count('farmacias__estaciones', distinct=True),
        total_farmacias=Count('farmacias', distinct=True),
        conformes=Count(
            'farmacias__estaciones',
            filter=Q(farmacias__estaciones__version_pos=F('version_objetivo')),
            distinct=True,
        ),
        online=Count(
            'farmacias__estaciones',
            filter=Q(farmacias__estaciones__ultimo_heartbeat__gte=umbral_online),
            distinct=True,
        ),
        pendientes=Count(
            'farmacias__estaciones',
            filter=Q(farmacias__estaciones__estado_aprobacion=Estacion.EstadoAprobacion.PENDIENTE),
            distinct=True,
        ),
    ).order_by('codigo')

    for g in grupos:
        g.pct_conforme = round(100 * g.conformes / g.total_estaciones) if g.total_estaciones else None

    despliegues_activos = (
        Despliegue.objects
        .filter(estado__in=[Despliegue.Estado.PUBLICANDO, Despliegue.Estado.PAUSADO])
        .order_by('-fecha_publicacion')
    )
    total_pendientes = Estacion.objects.filter(estado_aprobacion=Estacion.EstadoAprobacion.PENDIENTE).count()

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
    })
