from datetime import timedelta

from django.contrib import messages
from django.contrib.auth.decorators import login_required, permission_required
from django.db.models import Avg
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST
from django.urls import reverse
from django.utils import timezone

from apps.auditoria.models import registrar_evento
from apps.catalogo.models import Estacion, Farmacia
from apps.cuentas.services import scope_por_unidad_negocio_activa, verificar_acceso
from apps.monitoreo.forms import VentanaMantenimientoForm
from apps.monitoreo.models import Alerta, MuestraMetrica, ReglaAlerta, VentanaMantenimiento

from .alertas import _top_mensajes_pos_errores

# Umbral fijo en v1 (no hay Farmacia.capacidad_mbps todavía para mostrar % de la
# capacidad contratada) — mismo estilo que FRESCURA_MESHCENTRAL_MINUTOS/
# UMBRAL_ESCALAMIENTO_MINUTOS en apps.monitoreo.services: constante a nivel de
# módulo, se ajusta a mano si hace falta.
RED_FARMACIA_UMBRAL_WARNING_KBPS = 8000
RED_FARMACIA_UMBRAL_CRITICAL_KBPS = 15000

# Umbrales de color de CPU/RAM/disco. Estaban escritos como literales sueltos en las DOS
# vistas que los usan (la lista de servidores y el detalle), con los mismos seis números
# repetidos. Ajustar uno y olvidar el otro pintaba la MISMA estación de un color en la
# lista y de otro en su ficha, sin que nada fallara — el operador ve dos verdades y no
# sabe cuál creer. Ahora hay un solo lugar donde cambiarlos.
UMBRAL_CPU_WARNING_PCT, UMBRAL_CPU_CRITICAL_PCT = 75, 90
UMBRAL_RAM_WARNING_PCT, UMBRAL_RAM_CRITICAL_PCT = 80, 92
UMBRAL_DISCO_WARNING_PCT, UMBRAL_DISCO_CRITICAL_PCT = 85, 95


def _clasificar(valor, umbral_warning, umbral_critico):
    """Devuelve un estado (ok/warning/critical) para colorear un stat tile."""
    if valor is None:
        return 'sin_dato'
    if valor >= umbral_critico:
        return 'critical'
    if valor >= umbral_warning:
        return 'warning'
    return 'ok'


def estados_de_recursos(muestra):
    """Los tres colores de una muestra de recursos, calculados en un solo lugar.

    Se devuelven juntos y no de a uno a propósito: el problema no era el valor de cada
    umbral sino que hubiera dos caminos para llegar al color. Con esto, la lista y el
    detalle no pueden divergir ni aunque alguien toque uno solo.

    `muestra` puede ser None (estación sin métricas todavía): los tres salen 'sin_dato'.
    """
    return {
        'estado_cpu': _clasificar(
            muestra.cpu_carga_pct if muestra else None,
            UMBRAL_CPU_WARNING_PCT, UMBRAL_CPU_CRITICAL_PCT,
        ),
        'estado_ram': _clasificar(
            muestra.ram_usada_pct if muestra else None,
            UMBRAL_RAM_WARNING_PCT, UMBRAL_RAM_CRITICAL_PCT,
        ),
        'estado_disco': _clasificar(
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
    tarjetas = []
    for estacion in servidores:
        ultima = estacion.metricas.first()  # ordering = -timestamp
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
@permission_required('monitoreo.view_alerta', raise_exception=True)
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
    cpu_prom, ram_prom, disco_prom, red_prom = [], [], [], []
    for inicio, fin in semanas:
        de_la_semana = alertas.filter(abierta_en__date__gte=inicio, abierta_en__date__lt=fin)
        abiertas_warning.append(de_la_semana.filter(regla__severidad=ReglaAlerta.Severidad.WARNING).count())
        abiertas_critical.append(de_la_semana.filter(regla__severidad=ReglaAlerta.Severidad.CRITICAL).count())
        resueltas.append(alertas.filter(resuelta_en__date__gte=inicio, resuelta_en__date__lt=fin).count())

        # Promedio de la flota = razón de promedios (avg(ram_usada)/avg(ram_total)), no
        # promedio de razones por muestra — evita traer cada MuestraMetrica a Python
        # para calcular su .ram_usada_pct/.disco_usado_pct fila por fila (inviable a
        # ~1.900 estaciones × muestras cada 5-10 min sobre 12 semanas). Red no necesita
        # esa razón (ya es una tasa, no un % derivado de dos cantidades) — promedio
        # directo de los kbps ya calculados por el agente.
        agregado = metricas.filter(timestamp__date__gte=inicio, timestamp__date__lt=fin).aggregate(
            cpu=Avg('cpu_carga_pct'), ram_total=Avg('ram_total'), ram_usada=Avg('ram_usada'),
            disco_total=Avg('disco_total_gb'), disco_libre=Avg('disco_libre_gb'),
            red_recibido=Avg('red_recibido_kbps'), red_enviado=Avg('red_enviado_kbps'),
        )
        cpu_prom.append(agregado['cpu'])
        ram_prom.append(100 * agregado['ram_usada'] / agregado['ram_total'] if agregado['ram_total'] else None)
        disco_prom.append(
            100 * (agregado['disco_total'] - agregado['disco_libre']) / agregado['disco_total']
            if agregado['disco_total'] else None
        )
        if agregado['red_recibido'] is None and agregado['red_enviado'] is None:
            red_prom.append(None)
        else:
            red_prom.append(round((agregado['red_recibido'] or 0) + (agregado['red_enviado'] or 0), 1))

    filas_pos, _detectados = _top_mensajes_pos_errores(request)

    return render(request, 'panel/tendencia_flota.html', {
        'g_abiertas_warning': construir_grafico(abiertas_warning),
        'g_abiertas_critical': construir_grafico(abiertas_critical),
        'g_resueltas': construir_grafico(resueltas),
        'g_cpu': construir_grafico(cpu_prom, escala_fija=100),
        'g_ram': construir_grafico(ram_prom, escala_fija=100),
        'g_disco': construir_grafico(disco_prom, escala_fija=100),
        'g_red': construir_grafico(red_prom),
        # Explícito en vez de g_cpu.ultimo_valor: ese es el ÚLTIMO VALOR NO NULO de la
        # serie (puede venir de una semana vieja si la actual todavía no tiene
        # muestras), y el template lo etiqueta "Esta semana" — con .ultimo_valor
        # mostraría un dato de hace 3 semanas rotulado como si fuera de ahora.
        'cpu_semana_actual': cpu_prom[-1],
        'ram_semana_actual': ram_prom[-1],
        'disco_semana_actual': disco_prom[-1],
        'red_semana_actual': red_prom[-1],
        'total_abiertas_periodo': sum(abiertas_warning) + sum(abiertas_critical),
        'total_resueltas_periodo': sum(resueltas),
        'semana_desde': semanas[0][0],
        'semana_hasta': semanas[-1][1] - timedelta(days=1),
        'top_pos_errores': filas_pos[:5],
    })


@login_required
def red_farmacias_lista(request):
    """Redirección permanente a /monitoreo/enlaces/, que absorbió esta pantalla.

    Eran dos listados de las MISMAS 700 farmacias: uno con el estado del enlace (ICMP)
    y otro con su ancho de banda (SNMP). El usuario reportó la duplicación el
    11-sep-2026 y tenía razón — y el desbalance la hacía peor: el ancho de banda
    depende de que la farmacia tenga una estación con agente que sondee su Mikrotik,
    y eso cubría **2 de 700**, así que esta pantalla mostraba 700 filas casi todas
    vacías. El ancho de banda pasó a ser una columna más de la pantalla de enlaces.

    Se conserva la URL y el nombre en vez de borrarlos: hay enlaces guardados y
    `{% url %}` en plantillas que seguirían apuntando acá.

    Solo `@login_required`: el permiso lo aplica la vista destino. Exigir acá el
    permiso viejo daría 403 a alguien que sí puede ver la pantalla nueva.
    """
    return redirect('panel:enlaces_farmacias_lista')


@login_required
@permission_required('monitoreo.view_estadoenlacefarmacia', raise_exception=True)
def enlaces_farmacias_lista(request):
    """Estado del enlace de cada farmacia, sondeado por ICMP (apps.monitoreo.enlaces),
    con su consumo por SNMP donde el Mikrotik lo expone.

    Buscador, filtros y paginación son **server-side**. A 700 farmacias —y el parque
    apunta a más— traer todo y filtrar en el navegador significa mandar la tabla entera
    en cada carga y dejar al operador esperando para ver las 10 filas que le importan.
    Es la primera vista paginada del panel: si aparece una segunda, vale la pena mover
    `_pagina_actual` a un helper compartido.

    Los KPI se calculan sobre el conjunto COMPLETO, no sobre la página: "154 caídos"
    tiene que seguir diciendo 154 aunque estés mirando la página 3 con 25 filas.
    """
    from django.core.paginator import Paginator
    from django.db.models import Case, IntegerField, OuterRef, Q, Subquery, TextField, When
    from django.db.models.functions import Cast

    from apps.catalogo.models import Grupo
    from apps.monitoreo.models import EstadoEnlaceFarmacia, EventoEnlaceFarmacia, MuestraRedFarmacia

    # Última muestra de ancho de banda por farmacia, en la misma consulta (ver el
    # comentario de la versión anterior: el bucle con `.first()` eran 700 consultas).
    ultima_muestra = MuestraRedFarmacia.objects.filter(farmacia=OuterRef('pk')).order_by('-timestamp')

    base = scope_por_unidad_negocio_activa(
        Farmacia.objects.exclude(ip_router__isnull=True),
        request, 'unidad_negocio',
    ).select_related('estado_enlace', 'grupo').annotate(
        bw_rx=Subquery(ultima_muestra.values('red_recibido_kbps')[:1]),
        bw_tx=Subquery(ultima_muestra.values('red_enviado_kbps')[:1]),
        # `ip_router` es `inet` en PostgreSQL y `__icontains` no aplica sobre ese tipo:
        # buscar por IP reventaría la consulta en producción aunque en SQLite (donde es
        # texto) funcione. El cast explícito lo hace portable — misma familia de bug que
        # ya documentó §10 del plan con `ip_lan='localhost'`.
        ip_router_texto=Cast('ip_router', TextField()),
    )

    con_trafico = Q(bw_rx__isnull=False) | Q(bw_tx__isnull=False)
    sin_dato = Q(estado_enlace__isnull=True) | Q(estado_enlace__alcanzable__isnull=True)

    # KPI sobre el conjunto completo. Cuentas en la base, no recorriendo 700 objetos.
    total = base.count()
    activas = base.filter(estado_enlace__alcanzable=True).count()
    caidas = base.filter(estado_enlace__alcanzable=False).count()
    sin_sondear = base.filter(sin_dato).count()
    con_ancho_banda = base.filter(con_trafico).count()

    filtros = {
        'q': request.GET.get('q', '').strip(),
        'estado': request.GET.get('estado', ''),
        'grupo': request.GET.get('grupo', ''),
        'trafico': request.GET.get('trafico', ''),
    }

    listado = base
    if filtros['q']:
        termino = filtros['q']
        listado = listado.filter(
            Q(codigo__icontains=termino)
            | Q(nombre__icontains=termino)
            | Q(grupo__codigo__icontains=termino)
            | Q(circuito_proveedor__icontains=termino)
            | Q(ip_router_texto__icontains=termino)
        )
    if filtros['estado'] == 'activos':
        listado = listado.filter(estado_enlace__alcanzable=True)
    elif filtros['estado'] == 'caidos':
        listado = listado.filter(estado_enlace__alcanzable=False)
    elif filtros['estado'] == 'sin_sondear':
        listado = listado.filter(sin_dato)
    if filtros['grupo']:
        listado = listado.filter(grupo__codigo=filtros['grupo'])
    if filtros['trafico'] == 'con':
        listado = listado.filter(con_trafico)
    elif filtros['trafico'] == 'sin':
        listado = listado.exclude(con_trafico)

    # Caídos primero: es lo que el operador vino a ver. Después activos, y al final los
    # que nunca se sondearon. El orden se hace en la base porque ahora se pagina: un
    # `sorted()` en Python solo ordenaría las 25 filas de la página.
    listado = listado.annotate(
        orden_estado=Case(
            When(estado_enlace__alcanzable=False, then=0),
            When(estado_enlace__alcanzable=True, then=1),
            default=2,
            output_field=IntegerField(),
        ),
    ).order_by('orden_estado', 'codigo')

    paginador = Paginator(listado, 25)
    pagina = paginador.get_page(request.GET.get('pagina'))

    filas = []
    for farmacia in pagina.object_list:
        estado = getattr(farmacia, 'estado_enlace', None)
        total_kbps = None
        if farmacia.bw_rx is not None or farmacia.bw_tx is not None:
            total_kbps = round((farmacia.bw_rx or 0) + (farmacia.bw_tx or 0), 1)
        filas.append({
            'farmacia': farmacia,
            'estado': estado,
            'alcanzable': estado.alcanzable if estado else None,
            'total_kbps': total_kbps,
            'estado_bw': _clasificar(total_kbps, RED_FARMACIA_UMBRAL_WARNING_KBPS,
                                     RED_FARMACIA_UMBRAL_CRITICAL_KBPS),
        })

    # Las caídas en curso se listan completas pero la plantilla las trae colapsadas: 154
    # ítems abiertos empujan la tabla fuera de la pantalla, que es el problema que tenía
    # esta vista. El conteo va aparte para poder mostrarlo sin recorrer la lista.
    en_curso = EventoEnlaceFarmacia.objects.filter(
        fin__isnull=True, farmacia__in=base.values('pk'),
    ).select_related('farmacia').order_by('inicio')

    # Parámetros de filtro sin `pagina`, para que los enlaces del paginador no arrastren
    # la página vieja ni pierdan el filtro activo.
    query = request.GET.copy()
    query.pop('pagina', None)

    return render(request, 'panel/enlaces_farmacias_lista.html', {
        'filas': filas,
        'activas': activas,
        'caidas': caidas,
        'sin_sondear': sin_sondear,
        'total': total,
        'con_ancho_banda': con_ancho_banda,
        'en_curso': en_curso,
        'en_curso_total': en_curso.count(),
        'pagina': pagina,
        'filtros': filtros,
        'query_filtros': query.urlencode(),
        'grupos': Grupo.objects.filter(farmacias__in=base.values('pk')).distinct().order_by('codigo'),
        'umbral_fallas': EstadoEnlaceFarmacia.UMBRAL_FALLAS_CONSECUTIVAS,
    })


def _render_enlace_modal(request, farmacia):
    """Contenido del modal de una farmacia: estado del enlace + su consumo.

    El consumo de ancho de banda vive acá y no en la tabla porque es un dato de
    profundidad: en el listado de 700 filas solo interesa "responde o no", y mirar el
    tráfico es algo que se hace de a una farmacia, cuando ya sospechás de esa.
    """
    from apps.monitoreo.graficos import construir_grafico
    from apps.monitoreo.models import EventoEnlaceFarmacia, MuestraRedFarmacia

    muestras = list(MuestraRedFarmacia.objects.filter(farmacia=farmacia)[:40])[::-1]
    ultima = muestras[-1] if muestras else None

    # Una estación aprobada y en línea de esta farmacia es lo que hace posible pedir la
    # lectura: el SNMP al Mikrotik lo hace el agente desde la LAN del sitio, no este
    # servidor. Sin ella el botón no se ofrece, en vez de ofrecerlo y fallar en silencio.
    estacion_sondeadora = (
        farmacia.estaciones.filter(
            estado_aprobacion=Estacion.EstadoAprobacion.APROBADA,
            estado_conexion=Estacion.EstadoConexion.ONLINE,
        ).order_by('codigo').first()
    )

    return render(request, 'panel/enlace_farmacia_modal.html', {
        'farmacia': farmacia,
        'estado': getattr(farmacia, 'estado_enlace', None),
        'ultima': ultima,
        'total_muestras': len(muestras),
        'g_red': construir_grafico([m.red_total_kbps for m in muestras]),
        'estado_bw': _clasificar(
            ultima.red_total_kbps if ultima else None,
            RED_FARMACIA_UMBRAL_WARNING_KBPS, RED_FARMACIA_UMBRAL_CRITICAL_KBPS,
        ),
        'estacion_sondeadora': estacion_sondeadora,
        'caidas_recientes': EventoEnlaceFarmacia.objects.filter(farmacia=farmacia)[:5],
        'puede_solicitar': request.user.has_perm('catalogo.consultar_info_estacion'),
    })


@login_required
@permission_required('monitoreo.view_estadoenlacefarmacia', raise_exception=True)
def enlace_farmacia_modal(request, pk):
    farmacia = get_object_or_404(
        Farmacia.objects.select_related('grupo', 'unidad_negocio', 'estado_enlace'), pk=pk,
    )
    verificar_acceso(request.user, farmacia.unidad_negocio)
    return _render_enlace_modal(request, farmacia)


@login_required
@permission_required('catalogo.consultar_info_estacion', raise_exception=True)
@require_POST
def enlace_farmacia_solicitar(request, pk):
    """Pide AHORA la lectura de consumo del enlace de esta farmacia.

    Intenta primero el SNMP **directo desde este servidor**, y solo si eso falla le pide
    a una estación de la propia farmacia que lo haga por MQTT.

    Ese orden se invirtió el 12-sep-2026. La versión original solo sabía pedirle al
    agente, porque el repo daba por sentado que el servidor no tenía ruta hacia las IP
    de las farmacias. Ya no es así (ver el docstring de apps.monitoreo.enlaces), y se
    comprobó leyendo los contadores reales de GNB01 desde el contenedor. El camino
    directo es mejor por dos motivos: funciona en las ~700 farmacias y no solo en las
    que tienen agente, y el dato queda guardado antes de responder, así que el operador
    lo ve en el mismo repintado en vez de esperar a que llegue por MQTT.

    La vía del agente se conserva como respaldo: sirve donde el Mikrotik todavía no
    tiene SNMP habilitado pero sí hay un agente que lo alcanza desde la LAN.
    """
    from apps.catalogo.services import enviar_consultar_red_farmacia
    from apps.monitoreo.mikrotik import sondear_y_guardar_farmacia

    farmacia = get_object_or_404(
        Farmacia.objects.select_related('grupo', 'unidad_negocio', 'estado_enlace'), pk=pk,
    )
    verificar_acceso(request.user, farmacia.unidad_negocio)

    if farmacia.ip_router is not None and sondear_y_guardar_farmacia(farmacia):
        registrar_evento(
            usuario=request.user, accion='farmacia.consultar_red', objeto=farmacia,
            detalle={'via': 'snmp_directo'}, request=request,
        )
        ultima = farmacia.muestras_red.first()
        if ultima and ultima.red_total_kbps is None:
            # Primera muestra de esta farmacia: no hay contra qué diferenciar todavía.
            # Decirlo explícitamente evita que se lea como "el router no reporta nada".
            messages.success(
                request,
                'Primera lectura guardada. El consumo se calcula comparando dos lecturas, '
                'así que el valor aparece en la próxima (unos 5 minutos).',
            )
        else:
            messages.success(request, 'Lectura tomada por SNMP directo.')
        farmacia.refresh_from_db()
        return _render_enlace_modal(request, farmacia)

    estacion = farmacia.estaciones.filter(
        estado_aprobacion=Estacion.EstadoAprobacion.APROBADA,
        estado_conexion=Estacion.EstadoConexion.ONLINE,
    ).order_by('codigo').first()

    if estacion is None:
        messages.error(
            request,
            f'El Mikrotik de {farmacia.codigo} no respondió por SNMP y no hay ninguna estación en '
            'línea que pueda leerlo desde la LAN. Habilitá SNMP en el router con la comunidad '
            f'"{farmacia.codigo.lower()}" — probá con: probar_snmp_farmacia {farmacia.codigo}',
        )
    elif enviar_consultar_red_farmacia(estacion, farmacia.codigo.lower()):
        registrar_evento(
            usuario=request.user, accion='farmacia.consultar_red', objeto=farmacia,
            detalle={'via': 'agente', 'estacion': estacion.codigo}, request=request,
        )
        messages.success(
            request,
            f'El SNMP directo no respondió; se le pidió a {estacion.codigo}. El dato llega '
            'en unos segundos.',
        )
    else:
        messages.error(request, 'No respondió el SNMP ni se pudo pedir por MQTT (¿broker caído?).')

    farmacia.refresh_from_db()
    return _render_enlace_modal(request, farmacia)
