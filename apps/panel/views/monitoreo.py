from datetime import timedelta

from django.contrib import messages
from django.contrib.auth.decorators import login_required, permission_required
from django.views.decorators.http import require_GET
from django.db.models import Max
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.formats import number_format
from apps.auditoria.models import registrar_evento
from apps.catalogo.models import Estacion
from apps.cuentas.services import (
    scope_por_unidad_negocio_activa, unidades_negocio_en_foco, verificar_acceso,
)
from apps.monitoreo.forms import VentanaMantenimientoForm
from apps.monitoreo.models import MuestraMetrica, VentanaMantenimiento
from ..indicadores import (
    METRICAS_RECURSOS, estado_de, indicadores_de_recursos, series_por_estacion,
)
from ..umbrales import umbrales_de_reglas


def estados_de_recursos(muestra, reglas=None):
    """Los tres colores de una muestra de recursos, calculados en un solo lugar.

    Se devuelven juntos y no de a uno a propósito: el problema no era el valor de cada
    umbral sino que hubiera dos caminos para llegar al color. Con esto, la lista y el
    detalle no pueden divergir ni aunque alguien toque uno solo.

    Desde el rediseño de gráficas (19-sep-2026) ese "único lugar" es
    `apps.panel.indicadores`: el color de este número y el color del punto en su
    sparkline salen de la misma función, así que tampoco pueden divergir entre el valor
    y su propia curva. Los umbrales de defecto siguen viviendo en
    `apps/panel/umbrales.py`, y los pisa la ReglaAlerta activa que llegue en `reglas`.

    `muestra` puede ser None (estación sin métricas todavía): los tres salen 'sin_dato'.
    """
    return {
        'estado_cpu': estado_de('cpu_carga_pct', muestra.cpu_carga_pct if muestra else None, reglas),
        'estado_ram': estado_de('ram_usada_pct', muestra.ram_usada_pct if muestra else None, reglas),
        'estado_disco': estado_de('disco_usado_pct', muestra.disco_usado_pct if muestra else None, reglas),
    }


def valores_actuales(muestra):
    """El valor "de ahora" de cada métrica, tomado de la última muestra recibida.

    No se usa el último punto del sparkline: esa ventana son 20 minutos y la última
    muestra puede ser más vieja si la estación se apagó. El número grande tiene que ser
    el mismo que muestra el resto de la pantalla.
    """
    return {m.clave: getattr(muestra, m.clave, None) if muestra else None for m in METRICAS_RECURSOS}


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

    # Umbrales configurados y series recientes: DOS consultas fijas para toda la
    # pantalla, no una por tarjeta. Sin ellas la tarjeta mostraba cinco números crudos y
    # había que abrir el detalle de la estación para saber si alguno estaba mal.
    reglas = umbrales_de_reglas(unidades_negocio_en_foco(request))
    series = series_por_estacion(ids)

    tarjetas = []
    for estacion in servidores:
        ultima = por_estacion.get(estacion.pk)
        tarjetas.append({
            'estacion': estacion,
            'ultima': ultima,
            'servicios_caidos': caidos.get(estacion.pk),
            'indicadores': indicadores_de_recursos(
                series.get(estacion.pk, {}), reglas, ancho=120, alto=26,
                valores=valores_actuales(ultima),
            ),
            **estados_de_recursos(ultima, reglas),
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
    estacion = get_object_or_404(Estacion, pk=pk, monitorear_recursos=True)
    verificar_acceso(request.user, estacion.farmacia.unidad_negocio)
    # Últimas 60 muestras en orden cronológico (más viejo → más nuevo) para graficar.
    muestras = list(estacion.metricas.all()[:60])[::-1]
    ultima = muestras[-1] if muestras else None

    series = {
        m.clave: [getattr(muestra, m.clave) for muestra in muestras]
        for m in METRICAS_RECURSOS
    }
    # Los umbrales de ESTA estación: los de su cliente más los globales. Acá se puede
    # ser preciso porque hay una sola unidad de negocio a la vista, a diferencia del
    # listado, que muestra varias juntas y tiene que quedarse con el límite más estricto.
    reglas = umbrales_de_reglas([estacion.farmacia.unidad_negocio])

    # El pie de cada tarjeta: el dato absoluto que el porcentaje esconde. "RAM 88%" no
    # dice si son 7 GB de 8 o 900 MB de 1 GB, y la acción no es la misma.
    #
    # `number_format` y no interpolación directa: el texto lo arma Python, así que sin
    # esto saldría con punto decimal ("20.0 GB") mientras el resto del panel —que pasa
    # por los filtros de Django— usa la coma de es-EC. Y a un decimal, porque el agente
    # manda `disco_libre_gb` como 16.043524498578883 y quince decimales en un pie de
    # 10 px son ruido que además parte el texto en dos renglones.
    notas = {}
    if ultima and ultima.ram_total:
        notas['ram_usada_pct'] = '%s de %s MB' % (ultima.ram_usada, ultima.ram_total)
    if ultima and ultima.disco_total_gb:
        notas['disco_usado_pct'] = '%s GB libres de %s GB' % (
            number_format(ultima.disco_libre_gb or 0, decimal_pos=1),
            number_format(ultima.disco_total_gb, decimal_pos=1),
        )
    if ultima and ultima.red_total_kbps is not None:
        notas['red_total_kbps'] = '%s kbps de bajada / %s de subida' % (
            number_format(ultima.red_recibido_kbps or 0, decimal_pos=1),
            number_format(ultima.red_enviado_kbps or 0, decimal_pos=1),
        )

    return render(request, 'panel/monitoreo_detalle_partial.html', {
        'estacion': estacion,
        # Fuera del `if ultima` del template: una estación sin muestras de recursos puede
        # igual estar reportando sus servicios, y viceversa.
        'servicios_pos': estacion.servicios_pos.all(),
        'ultima': ultima,
        'total_muestras': len(muestras),
        'indicadores': indicadores_de_recursos(
            series, reglas, valores=valores_actuales(ultima), notas=notas,
        ),
        **estados_de_recursos(ultima, reglas),
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
