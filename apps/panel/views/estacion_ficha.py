"""Ficha completa de una estación (/estaciones/<pk>/) y la salud que muestra la lista.

Aparte de `estaciones.py` por tamaño (ver DivisionDeVistasTests) y porque es una sola
idea: el índice de salud y lo que lo rodea. La lógica está en `apps.panel.ficha_estacion`.
"""
from datetime import timedelta

from django.contrib.auth.decorators import login_required, permission_required
from django.shortcuts import get_object_or_404, render
from django.utils import timezone

from apps.catalogo.models import Estacion, VersionAgente
from apps.cuentas.services import verificar_acceso


def salud_de_la_pagina(estaciones):
    """Pone en cada estación de la página `salud` (índice de la ficha) y `latido` (su
    antigüedad como tono). Tres consultas para toda la página, no una por fila: las
    alertas abiertas se cuentan agrupadas y las versiones vigentes se leen una vez. El
    disco no entra: pediría la última muestra de cada estación; la ficha sí lo cuenta."""
    from collections import Counter

    from django.db.models import Count

    from apps.monitoreo.models import Alerta
    from apps.monitoreo.servicios_pos import estado_catalogo, versiones_vigentes
    from apps.monitoreo.umbrales import ONLINE_UMBRAL_MINUTOS

    from .. import ficha_estacion as ficha

    estaciones = list(estaciones)
    conteo = Counter()
    for fila in (Alerta.objects.filter(estacion__in=estaciones, estado__in=[Alerta.Estado.ABIERTA, Alerta.Estado.RECONOCIDA])
                 .values('estacion_id', 'regla__severidad').annotate(n=Count('id'))):
        conteo[(fila['estacion_id'], fila['regla__severidad'] == 'critical')] += fila['n']
    ultima_version = VersionAgente.objects.order_by('-fecha_creacion').first()
    vigentes = versiones_vigentes() if estaciones else None
    ahora = timezone.now()
    for e in estaciones:
        e.salud = ficha.salud_desde_conteos(
            e, criticas=conteo[(e.pk, True)], advertencias=conteo[(e.pk, False)],
            ultima_version_agente=ultima_version, catalogos=estado_catalogo(e, vigentes),
        )
        e.latido = ficha.edad(e.ultimo_heartbeat, ahora=ahora, vieja_tras=timedelta(minutes=ONLINE_UMBRAL_MINUTOS))
        if e.ultimo_heartbeat and e.latido['tono'] != ficha.BIEN:
            e.latido['tono'] = ficha.CRITICO


@login_required
@permission_required('catalogo.view_estacion', raise_exception=True)
def estacion_ficha(request, pk):
    """Ficha completa de una estación: salud, recursos, equipo, escaneos y línea de vida.

    Las acciones (escanear, actualizar agente, freno, BitLocker…) siguen en la ventana
    emergente, que esta página abre con el mismo botón que la lista: así no se duplican
    ni sus permisos ni sus avisos. Ver apps.panel.ficha_estacion."""
    from apps.monitoreo.models import Alerta, MuestraMetrica
    from apps.monitoreo.servicios_pos import estado_catalogo

    from .. import ficha_estacion as ficha

    estacion = get_object_or_404(Estacion.objects.select_related('farmacia__unidad_negocio'), pk=pk)
    verificar_acceso(request.user, estacion.farmacia.unidad_negocio)
    ahora = timezone.now()

    muestra = MuestraMetrica.objects.filter(estacion=estacion).order_by('-timestamp').first()
    recursos = ficha.recursos(muestra)
    disco = next((r['pct'] for r in recursos if r['nombre'] == 'Disco C:'), None)
    alertas_abiertas = list(
        Alerta.objects.filter(estacion=estacion, estado__in=[Alerta.Estado.ABIERTA, Alerta.Estado.RECONOCIDA])
        .select_related('regla'),
    )
    ultima_version_agente = VersionAgente.objects.order_by('-fecha_creacion').first()
    catalogos = estado_catalogo(estacion)
    categoria = request.GET.get('ver', '')
    if categoria not in dict(ficha.CATEGORIAS):
        categoria = ''

    return render(request, 'panel/estacion_ficha.html', {
        'estacion': estacion,
        'salud': ficha.salud(
            estacion, alertas_abiertas=alertas_abiertas, ultima_version_agente=ultima_version_agente,
            catalogos=catalogos, disco_pct=disco,
        ),
        'recursos': recursos,
        'edad_recursos': ficha.edad(muestra.timestamp if muestra else None, ahora=ahora, vieja_tras=timedelta(minutes=30)),
        'edad_equipo': ficha.edad(estacion.info_equipo_fecha, ahora=ahora, vieja_tras=timedelta(days=30)),
        'escaneos': [
            ('Software instalado', ficha.edad(estacion.software_instalado_ultima_verificacion, ahora=ahora)),
            ('Windows Update', ficha.edad(estacion.windows_update_ultima_verificacion, ahora=ahora)),
            ('Periféricos USB', ficha.edad(estacion.perifericos_ultima_verificacion, ahora=ahora)),
        ],
        'alertas_abiertas': alertas_abiertas,
        'catalogos': catalogos,
        'agente_al_dia': bool(ultima_version_agente and estacion.version_agente == ultima_version_agente.version),
        'activo': getattr(estacion, 'activo_vinculado', None),
        'puede_acceso_remoto': request.user.has_perm('catalogo.acceso_remoto_estacion'),
        'eventos': ficha.linea_de_vida(estacion, categoria=categoria, ahora=ahora),
        'categorias': ficha.CATEGORIAS,
        'categoria': categoria,
    })
