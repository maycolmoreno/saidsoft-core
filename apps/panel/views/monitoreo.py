from django.contrib import messages
from django.contrib.auth.decorators import login_required, permission_required
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse

from apps.auditoria.models import registrar_evento
from apps.catalogo.models import Estacion
from apps.cuentas.services import scope_por_unidad_negocio_activa, verificar_acceso
from apps.monitoreo.forms import VentanaMantenimientoForm
from apps.monitoreo.models import VentanaMantenimiento


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
    servidores = scope_por_unidad_negocio_activa(
        Estacion.objects
        .filter(monitorear_recursos=True)
        .select_related('farmacia', 'farmacia__grupo'),
        request, 'farmacia__unidad_negocio',
    ).order_by('codigo')
    tarjetas = []
    for estacion in servidores:
        ultima = estacion.metricas.first()  # ordering = -timestamp
        tarjetas.append({
            'estacion': estacion,
            'ultima': ultima,
            'estado_cpu': _clasificar(ultima.cpu_carga_pct if ultima else None, 75, 90),
            'estado_ram': _clasificar(ultima.ram_usada_pct if ultima else None, 80, 92),
            'estado_disco': _clasificar(ultima.disco_usado_pct if ultima else None, 85, 95),
        })
    return render(request, 'panel/monitoreo_lista.html', {'tarjetas': tarjetas})


@login_required
def monitoreo_detalle(request, pk):
    estacion = get_object_or_404(
        Estacion.objects.select_related('farmacia', 'farmacia__grupo'),
        pk=pk, monitorear_recursos=True,
    )
    verificar_acceso(request.user, estacion.farmacia.unidad_negocio)
    return render(request, 'panel/monitoreo_detalle.html', {'estacion': estacion})


@login_required
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

    ultima = muestras[-1] if muestras else None
    return render(request, 'panel/monitoreo_detalle_partial.html', {
        'estacion': estacion,
        'ultima': ultima,
        'total_muestras': len(muestras),
        'g_cpu': construir_grafico(cpu, escala_fija=100),
        'g_ram': construir_grafico(ram_pct, escala_fija=100),
        'g_disco': construir_grafico(disco_pct, escala_fija=100),
        'g_latencia': construir_grafico(latencia),
        'estado_cpu': _clasificar(ultima.cpu_carga_pct if ultima else None, 75, 90),
        'estado_ram': _clasificar(ultima.ram_usada_pct if ultima else None, 80, 92),
        'estado_disco': _clasificar(ultima.disco_usado_pct if ultima else None, 85, 95),
    })


@login_required
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
    return render(request, 'panel/accion_form.html', {
        'form': form, 'titulo': 'Nueva ventana de mantenimiento',
        'volver_url': reverse('panel:ventanas_mantenimiento_lista'),
    })
