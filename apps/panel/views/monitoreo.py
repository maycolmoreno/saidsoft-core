from datetime import timedelta

from django.contrib import messages
from django.contrib.auth.decorators import login_required, permission_required
from django.db.models import Avg
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone

from apps.auditoria.models import registrar_evento
from apps.catalogo.models import Estacion
from apps.cuentas.services import scope_por_unidad_negocio_activa, verificar_acceso
from apps.monitoreo.forms import VentanaMantenimientoForm
from apps.monitoreo.models import Alerta, MuestraMetrica, ReglaAlerta, VentanaMantenimiento

from .alertas import _top_mensajes_pos_errores


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


def _semanas_recientes(n=12):
    """Últimas n semanas (lunes a domingo, más vieja primero), incluyendo la semana
    actual (parcial). Buckets calculados a mano en vez de TruncWeek de la ORM — evita
    depender de cómo trunca semanas cada backend (SQLite en dev, Postgres/TimescaleDB
    en producción) por un cálculo que de todas formas es sobre pocos puntos (12)."""
    hoy = timezone.localdate()
    lunes_actual = hoy - timedelta(days=hoy.weekday())
    return [
        (lunes_actual - timedelta(weeks=i), lunes_actual - timedelta(weeks=i - 1))
        for i in range(n - 1, -1, -1)
    ]


@login_required
def tendencia_flota(request):
    """M5 del roadmap de monitoreo: series semanales a nivel de flota (no por
    estación) — alertas abiertas/resueltas por severidad y promedio de CPU/RAM/disco
    de los servidores monitoreados. Los errores del POS se muestran como "top actual"
    (mismo dato que pos_errores_flota), no como tendencia: PosErrorDetectado solo
    guarda un contador acumulado de por vida, no hay con qué armar una serie semanal
    real sin agregar un modelo nuevo — decisión explícita del usuario, ver
    PLAN_MODERNIZACION.md."""
    from apps.monitoreo.graficos import construir_grafico

    semanas = _semanas_recientes(12)

    alertas = scope_por_unidad_negocio_activa(
        Alerta.objects.all(), request, 'estacion__farmacia__unidad_negocio',
    )
    servidores = scope_por_unidad_negocio_activa(
        Estacion.objects.filter(monitorear_recursos=True), request, 'farmacia__unidad_negocio',
    )
    metricas = MuestraMetrica.objects.filter(estacion__in=servidores)

    abiertas_warning, abiertas_critical, resueltas = [], [], []
    cpu_prom, ram_prom, disco_prom = [], [], []
    for inicio, fin in semanas:
        de_la_semana = alertas.filter(abierta_en__date__gte=inicio, abierta_en__date__lt=fin)
        abiertas_warning.append(de_la_semana.filter(regla__severidad=ReglaAlerta.Severidad.WARNING).count())
        abiertas_critical.append(de_la_semana.filter(regla__severidad=ReglaAlerta.Severidad.CRITICAL).count())
        resueltas.append(alertas.filter(resuelta_en__date__gte=inicio, resuelta_en__date__lt=fin).count())

        # Promedio de la flota = razón de promedios (avg(ram_usada)/avg(ram_total)), no
        # promedio de razones por muestra — evita traer cada MuestraMetrica a Python
        # para calcular su .ram_usada_pct/.disco_usado_pct fila por fila (inviable a
        # ~1.900 estaciones × muestras cada 5-10 min sobre 12 semanas).
        agregado = metricas.filter(timestamp__date__gte=inicio, timestamp__date__lt=fin).aggregate(
            cpu=Avg('cpu_carga_pct'), ram_total=Avg('ram_total'), ram_usada=Avg('ram_usada'),
            disco_total=Avg('disco_total_gb'), disco_libre=Avg('disco_libre_gb'),
        )
        cpu_prom.append(agregado['cpu'])
        ram_prom.append(100 * agregado['ram_usada'] / agregado['ram_total'] if agregado['ram_total'] else None)
        disco_prom.append(
            100 * (agregado['disco_total'] - agregado['disco_libre']) / agregado['disco_total']
            if agregado['disco_total'] else None
        )

    filas_pos, _detectados = _top_mensajes_pos_errores(request)

    return render(request, 'panel/tendencia_flota.html', {
        'g_abiertas_warning': construir_grafico(abiertas_warning),
        'g_abiertas_critical': construir_grafico(abiertas_critical),
        'g_resueltas': construir_grafico(resueltas),
        'g_cpu': construir_grafico(cpu_prom, escala_fija=100),
        'g_ram': construir_grafico(ram_prom, escala_fija=100),
        'g_disco': construir_grafico(disco_prom, escala_fija=100),
        # Explícito en vez de g_cpu.ultimo_valor: ese es el ÚLTIMO VALOR NO NULO de la
        # serie (puede venir de una semana vieja si la actual todavía no tiene
        # muestras), y el template lo etiqueta "Esta semana" — con .ultimo_valor
        # mostraría un dato de hace 3 semanas rotulado como si fuera de ahora.
        'cpu_semana_actual': cpu_prom[-1],
        'ram_semana_actual': ram_prom[-1],
        'disco_semana_actual': disco_prom[-1],
        'total_abiertas_periodo': sum(abiertas_warning) + sum(abiertas_critical),
        'total_resueltas_periodo': sum(resueltas),
        'semana_desde': semanas[0][0],
        'semana_hasta': semanas[-1][1] - timedelta(days=1),
        'top_pos_errores': filas_pos[:5],
    })
