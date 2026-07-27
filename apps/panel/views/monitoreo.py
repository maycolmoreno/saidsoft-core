from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, render

from apps.catalogo.models import Estacion


def _clasificar(valor, umbral_warning, umbral_critico):
    """Devuelve un estado (ok/warning/critical) para colorear un stat tile."""
    if valor is None:
        return 'sin_dato'
    if valor >= umbral_critico:
        return 'critical'
    if valor >= umbral_warning:
        return 'warning'
    return 'ok'


@login_required
def monitoreo_lista(request):
    servidores = (
        Estacion.objects
        .filter(monitorear_recursos=True)
        .select_related('farmacia', 'farmacia__grupo')
        .order_by('codigo')
    )
    tarjetas = []
    for estacion in servidores:
        ultima = estacion.metricas.first()  # ordering = -timestamp
        tarjetas.append({
            'estacion': estacion,
            'ultima': ultima,
            'estado_cpu': _clasificar(ultima.cpu_carga_pct if ultima else None, 75, 90),
            'estado_ram': _clasificar(ultima.ram_usada_pct if ultima else None, 80, 92),
        })
    return render(request, 'panel/monitoreo_lista.html', {'tarjetas': tarjetas})


@login_required
def monitoreo_detalle(request, pk):
    estacion = get_object_or_404(
        Estacion.objects.select_related('farmacia', 'farmacia__grupo'),
        pk=pk, monitorear_recursos=True,
    )
    return render(request, 'panel/monitoreo_detalle.html', {'estacion': estacion})


@login_required
def monitoreo_detalle_partial(request, pk):
    from apps.monitoreo.graficos import construir_grafico

    estacion = get_object_or_404(Estacion, pk=pk, monitorear_recursos=True)
    # Últimas 60 muestras en orden cronológico (más viejo → más nuevo) para graficar.
    muestras = list(estacion.metricas.all()[:60])[::-1]

    ram_pct = [m.ram_usada_pct for m in muestras]
    cpu = [m.cpu_carga_pct for m in muestras]
    latencia = [m.latencia_ms for m in muestras]

    ultima = muestras[-1] if muestras else None
    return render(request, 'panel/monitoreo_detalle_partial.html', {
        'estacion': estacion,
        'ultima': ultima,
        'total_muestras': len(muestras),
        'g_cpu': construir_grafico(cpu, escala_fija=100),
        'g_ram': construir_grafico(ram_pct, escala_fija=100),
        'g_latencia': construir_grafico(latencia),
        'estado_cpu': _clasificar(ultima.cpu_carga_pct if ultima else None, 75, 90),
        'estado_ram': _clasificar(ultima.ram_usada_pct if ultima else None, 80, 92),
    })
