from django.contrib import messages
from django.contrib.auth.decorators import login_required, permission_required
from django.db.models import Max
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from apps.auditoria.models import registrar_evento
from apps.catalogo.models import Estacion
from apps.cuentas.services import scope_por_unidad_negocio_activa, verificar_acceso
from apps.monitoreo.forms import VentanaMantenimientoForm
from apps.monitoreo.models import MuestraMetrica, VentanaMantenimiento
from ..umbrales import (
    RED_FARMACIA_UMBRAL_CRITICAL_KBPS, RED_FARMACIA_UMBRAL_WARNING_KBPS,
    UMBRAL_CPU_CRITICAL_PCT, UMBRAL_CPU_WARNING_PCT, UMBRAL_DISCO_CRITICAL_PCT,
    UMBRAL_DISCO_WARNING_PCT, UMBRAL_RAM_CRITICAL_PCT, UMBRAL_RAM_WARNING_PCT, clasificar,
)


def estados_de_recursos(muestra):
    """Los tres colores de una muestra de recursos, calculados en un solo lugar.

    Se devuelven juntos y no de a uno a propósito: el problema no era el valor de cada
    umbral sino que hubiera dos caminos para llegar al color. Con esto, la lista y el
    detalle no pueden divergir ni aunque alguien toque uno solo.

    `muestra` puede ser None (estación sin métricas todavía): los tres salen 'sin_dato'.
    """
    return {
        'estado_cpu': clasificar(
            muestra.cpu_carga_pct if muestra else None,
            UMBRAL_CPU_WARNING_PCT, UMBRAL_CPU_CRITICAL_PCT,
        ),
        'estado_ram': clasificar(
            muestra.ram_usada_pct if muestra else None,
            UMBRAL_RAM_WARNING_PCT, UMBRAL_RAM_CRITICAL_PCT,
        ),
        'estado_disco': clasificar(
            muestra.disco_usado_pct if muestra else None,
            UMBRAL_DISCO_WARNING_PCT, UMBRAL_DISCO_CRITICAL_PCT,
        ),
    }


@login_required
@permission_required('monitoreo.view_muestrametrica', raise_exception=True)
def monitoreo_lista(request):
    servidores = scope_por_unidad_negocio_activa(
        Estacion.objects
        .filter(monitorear_recursos=True)
        .select_related('farmacia', 'farmacia__grupo'),
        request, 'farmacia__unidad_negocio',
    ).order_by('codigo')
    # La última muestra de cada servidor en DOS consultas, no una por servidor. Antes
    # era `estacion.metricas.first()` dentro del bucle: con 4 servidores monitoreados no
    # se notaba, pero `monitorear_recursos` se activa en el servidor de cada farmacia, así
    # que a escala de rollout son ~700 consultas por carga de pantalla.
    #
    # Se resuelve con Max(timestamp) por estación y después un `IN` sobre esos instantes,
    # en vez de `DISTINCT ON (estacion)`: eso último es exclusivo de PostgreSQL y las
    # pruebas corren sobre SQLite.
    servidores = list(servidores)
    ids = [e.pk for e in servidores]
    ultimas = MuestraMetrica.objects.filter(estacion_id__in=ids).values('estacion_id').annotate(
        ts=Max('timestamp'),
    )
    pares = {(u['estacion_id'], u['ts']) for u in ultimas}
    por_estacion = {
        m.estacion_id: m
        for m in MuestraMetrica.objects.filter(
            estacion_id__in=ids, timestamp__in=[ts for _e, ts in pares],
        )
        # El filtro por timestamp solo puede traer de más (dos estaciones que midieron en
        # el mismo instante), nunca de menos: el par exacto es el que decide.
        if (m.estacion_id, m.timestamp) in pares
    }

    tarjetas = []
    for estacion in servidores:
        ultima = por_estacion.get(estacion.pk)
        tarjetas.append({
            'estacion': estacion,
            'ultima': ultima,
            **estados_de_recursos(ultima),
        })
    return render(request, 'panel/monitoreo_lista.html', {'tarjetas': tarjetas})


@login_required
@permission_required('monitoreo.view_muestrametrica', raise_exception=True)
def monitoreo_detalle(request, pk):
    estacion = get_object_or_404(
        Estacion.objects.select_related('farmacia', 'farmacia__grupo'),
        pk=pk, monitorear_recursos=True,
    )
    verificar_acceso(request.user, estacion.farmacia.unidad_negocio)
    return render(request, 'panel/monitoreo_detalle.html', {'estacion': estacion})


@login_required
@permission_required('monitoreo.view_muestrametrica', raise_exception=True)
def monitoreo_detalle_partial(request, pk):
    from apps.monitoreo.graficos import construir_grafico

    estacion = get_object_or_404(Estacion, pk=pk, monitorear_recursos=True)
    verificar_acceso(request.user, estacion.farmacia.unidad_negocio)
    # Últimas 60 muestras en orden cronológico (más viejo → más nuevo) para graficar.
    muestras = list(estacion.metricas.all()[:60])[::-1]

    ram_pct = [m.ram_usada_pct for m in muestras]
    cpu = [m.cpu_carga_pct for m in muestras]
    disco_pct = [m.disco_usado_pct for m in muestras]
    latencia = [m.latencia_ms for m in muestras]
    red = [m.red_total_kbps for m in muestras]

    ultima = muestras[-1] if muestras else None
    return render(request, 'panel/monitoreo_detalle_partial.html', {
        'estacion': estacion,
        'ultima': ultima,
        'total_muestras': len(muestras),
        'g_cpu': construir_grafico(cpu, escala_fija=100),
        'g_ram': construir_grafico(ram_pct, escala_fija=100),
        'g_disco': construir_grafico(disco_pct, escala_fija=100),
        'g_latencia': construir_grafico(latencia),
        'g_red': construir_grafico(red),
        **estados_de_recursos(ultima),
    })


@login_required
@permission_required('monitoreo.view_ventanamantenimiento', raise_exception=True)
def ventanas_mantenimiento_lista(request):
    ventanas = scope_por_unidad_negocio_activa(
        VentanaMantenimiento.objects.select_related('unidad_negocio').order_by('-desde'),
        request, 'unidad_negocio',
    )
    return render(request, 'panel/ventanas_mantenimiento_lista.html', {'ventanas': ventanas})


@login_required
@permission_required('monitoreo.add_ventanamantenimiento', raise_exception=True)
def ventana_mantenimiento_crear(request):
    if request.method == 'POST':
        form = VentanaMantenimientoForm(request.POST, user=request.user)
        if form.is_valid():
            ventana = form.save(commit=False)
            ventana.creado_por = request.user
            ventana.save()
            form.save_m2m()
            registrar_evento(usuario=request.user, accion='ventana_mantenimiento.crear', objeto=ventana, request=request)
            messages.success(request, f'Ventana de mantenimiento "{ventana.motivo}" creada.')
            return redirect('panel:ventanas_mantenimiento_lista')
    else:
        form = VentanaMantenimientoForm(user=request.user)
    return render(request, 'panel/ventana_mantenimiento_form.html', {
        'form': form, 'titulo': 'Nueva ventana de mantenimiento',
        'volver_url': reverse('panel:ventanas_mantenimiento_lista'),
    })
