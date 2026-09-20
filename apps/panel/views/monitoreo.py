from datetime import timedelta

from django.contrib import messages
from django.contrib.auth.decorators import login_required, permission_required
from django.views.decorators.http import require_GET
from django.db.models import Max
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from apps.auditoria.models import registrar_evento
from apps.catalogo.models import Estacion
from apps.cuentas.services import (
    scope_por_unidad_negocio_activa, unidades_negocio_en_foco, verificar_acceso,
)
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

    # Servicios del POS caidos, contados de una sola consulta para toda la lista: hacerlo
    # por tarjeta serian N consultas por carga, el mismo N+1 que la auditoria saco de esta
    # misma pantalla.
    #
    # Solo cuentan los chequeos VIGENTES: una estacion apagada deja su ultimo resultado
    # congelado, y contarlo como caida seria inventar una incidencia sobre un equipo que
    # nadie esta midiendo.
    from apps.monitoreo.models import EstadoServicioPos

    vigente_desde = timezone.now() - timedelta(hours=EstadoServicioPos.HORAS_VERIFICACION_VIGENTE)
    caidos = {}
    for estado in EstadoServicioPos.objects.filter(
        estacion_id__in=ids, disponible=False, ultima_verificacion__gte=vigente_desde,
    ):
        marca = caidos.setdefault(estado.estacion_id, {'total': 0, 'criticos': 0})
        marca['total'] += 1
        marca['criticos'] += 1 if estado.critico else 0

    tarjetas = []
    for estacion in servidores:
        ultima = por_estacion.get(estacion.pk)
        tarjetas.append({
            'estacion': estacion,
            'ultima': ultima,
            'servicios_caidos': caidos.get(estacion.pk),
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
        # Fuera del `if ultima` del template: una estación sin muestras de recursos puede
        # igual estar reportando sus servicios, y viceversa.
        'servicios_pos': estacion.servicios_pos.all(),
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


# Cuántas filas de detalle entran en cada bloque del Centro de Monitoreo. Mismo criterio
# que `_MAX_FILAS` del bot: con 700 farmacias, una lista completa no se lee y además
# convierte cada refresco en una consulta cara que varios agentes disparan todo el día.
# El total sí va completo, porque es el número que decide si hay que actuar.
_MAX_FILAS_CENTRO = 15

# Cada cuánto se refresca la pantalla sola.
#
# Sesenta segundos y no diez, que es lo que usan las pantallas de detalle: éstas se abren
# un rato para seguir un despliegue puntual, el Centro lo dejan abierto TODO EL DÍA varios
# agentes a la vez, así que cada refresco se multiplica por gente y por horas.
#
# Sesenta es además la cadencia real más rápida del backend: `marcar-estaciones-offline`
# corre cada 60 s y el sondeo de enlaces cada 2 min (ver CELERY_BEAT_SCHEDULE). Pollear
# cada 10 s mostraría nueve veces el mismo dato. Lo único que llega en tiempo real son las
# alertas, que se abren al ingerir el mensaje MQTT — y un minuto de demora para que
# aparezcan en un tablero de triage es aceptable, porque la notificación por Telegram y
# correo ya salió en el momento.
SEGUNDOS_REFRESCO_CENTRO = 60


@login_required
@permission_required('monitoreo.view_alerta', raise_exception=True)
@require_GET
def centro_monitoreo(request):
    """El marco de la pantalla. El contenido lo trae el parcial y se refresca solo.

    Se separa en dos vistas —marco y parcial— por el mismo motivo que `monitoreo_detalle`:
    el polling de HTMX reemplaza el interior sin volver a pedir la navegación, el CSS ni
    la cabecera. A un refresco por minuto por agente, mandar la página entera cada vez es
    trabajo puro.

    `monitoreo.view_alerta` es la puerta y no un permiso nuevo: es el mismo que ya decide
    si se ve el menú de Alertas en `base.html`, y esta pantalla es exactamente esa
    información puesta junto al resto. Inventar `ver_centro_monitoreo` habría creado un
    esquema paralelo para responder la pregunta que ese permiso ya responde.
    """
    return render(request, 'panel/centro_monitoreo.html', {
        'segundos_refresco': SEGUNDOS_REFRESCO_CENTRO,
    })


@login_required
@permission_required('monitoreo.view_alerta', raise_exception=True)
@require_GET
def centro_monitoreo_partial(request):
    """Todo lo que la pantalla muestra, en una sola respuesta.

    De SOLO LECTURA, igual que el bot de Telegram y por el mismo motivo: es una pantalla
    de triage que se deja abierta, y una acción destructiva a un clic de distancia en algo
    que se mira de reojo es una mala idea. Cada fila enlaza al detalle donde esa acción ya
    existe.
    """
    from apps.monitoreo.enlaces import _agrupar_por_proveedor
    from apps.monitoreo.models import (
        Alerta, EstadoRedActivo, EstadoServicioPos, EventoEnlaceFarmacia,
        ReglaAlerta, VentanaMantenimiento,
    )
    from apps.monitoreo.services import (
        TOLERANCIA_FRESCURA_MINUTOS, fuente_desactualizada, resumen_operacion,
    )

    unidades = unidades_negocio_en_foco(request)
    resumen = resumen_operacion(unidades)

    alertas = (
        Alerta.objects.filter(
            estado=Alerta.Estado.ABIERTA, estacion__farmacia__unidad_negocio__in=unidades,
        )
        .select_related('regla', 'estacion', 'estacion__farmacia')
        .order_by('-abierta_en')
    )
    criticas = list(alertas.filter(regla__severidad=ReglaAlerta.Severidad.CRITICAL)[:_MAX_FILAS_CENTRO])
    advertencias = list(alertas.filter(regla__severidad=ReglaAlerta.Severidad.WARNING)[:_MAX_FILAS_CENTRO])

    # El evento abierto y no EstadoEnlaceFarmacia: el evento sabe CUÁNDO empezó la caída
    # (el primer fallo, no el sondeo que la confirmó) y trae el circuito denormalizado,
    # que es lo que `_agrupar_por_proveedor` necesita. Se reusa esa función en vez de
    # reimplementar el corte del circuito, que ya tiene su propia sutileza documentada.
    eventos = list(
        EventoEnlaceFarmacia.objects
        .filter(fin__isnull=True, farmacia__unidad_negocio__in=unidades)
        .exclude(farmacia__estado_enlace__respondio_alguna_vez=False)
        .select_related('farmacia')
        .order_by('inicio')[:_MAX_FILAS_CENTRO]
    )
    enlaces_por_proveedor = _agrupar_por_proveedor(eventos)

    servicios = (
        EstadoServicioPos.objects
        .filter(
            disponible=False, ultima_respuesta__isnull=False,
            estacion__farmacia__unidad_negocio__in=unidades,
        )
        .select_related('estacion', 'estacion__farmacia')
        .order_by('-critico', 'estacion__codigo')
    )
    pos_criticos = list(servicios.filter(critico=True)[:_MAX_FILAS_CENTRO])
    pos_no_criticos = list(servicios.filter(critico=False)[:_MAX_FILAS_CENTRO])

    # "Silencio" y no "caído": un equipo que dejó de ser sondeado no está reportando nada,
    # y eso el tablero lo tiene que distinguir de uno que responde "no".
    corte = timezone.now() - timedelta(hours=EstadoRedActivo.HORAS_VERIFICACION_VIGENTE)
    sin_sondeo = list(
        EstadoRedActivo.objects
        .filter(ultima_verificacion__lt=corte, activo__unidad_negocio__in=unidades)
        .select_related('activo', 'activo__farmacia')
        .order_by('ultima_verificacion')[:_MAX_FILAS_CENTRO]
    )

    # Aparte y NO mezcladas con lo crítico: algo caído dentro de una ventana es esperado.
    # Verlo en la misma lista que un incidente real es lo que entrena a la mesa de ayuda a
    # ignorar el tablero.
    ahora = timezone.now()
    ventanas = list(
        VentanaMantenimiento.objects
        .filter(activo=True, desde__lte=ahora, hasta__gte=ahora, unidad_negocio__in=unidades)
        .select_related('unidad_negocio')
        .order_by('hasta')
    )

    return render(request, 'panel/centro_monitoreo_partial.html', {
        'r': resumen,
        'criticas': criticas,
        'advertencias': advertencias,
        'enlaces_por_proveedor': enlaces_por_proveedor,
        'pos_criticos': pos_criticos,
        'pos_no_criticos': pos_no_criticos,
        'sin_sondeo': sin_sondeo,
        'ventanas': ventanas,
        'max_filas': _MAX_FILAS_CENTRO,
        # Por bloque, para poder marcar viejo solo lo que lo está en vez de teñir toda la
        # pantalla cuando una sola tarea de fondo se cae.
        'stale_enlaces': fuente_desactualizada(resumen['ultimo_sondeo_red'], 'red_farmacias'),
        'tolerancia': TOLERANCIA_FRESCURA_MINUTOS,
        'ahora': ahora,
    })
