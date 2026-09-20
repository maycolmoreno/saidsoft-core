"""Enlaces de internet de las farmacias y su equipo de borde.

Tercer dominio que vivia dentro de monitoreo.py. No habla de estaciones sino del
enlace del sitio: ancho de banda, caidas del proveedor y reinicios del Mikrotik.
"""
from django.contrib import messages
from django.contrib.auth.decorators import login_required, permission_required
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST
from apps.auditoria.models import registrar_evento
from apps.catalogo.models import Estacion, Farmacia
from apps.cuentas.services import scope_por_unidad_negocio_activa, verificar_acceso
from ..indicadores import Metrica, indicador
from ..paginacion import paginar
from ..umbrales import (
    BW_CONTRATADO_UMBRAL_CRITICAL_PCT, BW_CONTRATADO_UMBRAL_WARNING_PCT, clasificar,
    limites_de_enlace,
)


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
    # "Caidas" son solo las que alguna vez respondieron: lo demas no es una caida sino
    # un sitio que el monitoreo no alcanza, y mezclarlos hacia que el KPI dijera 162
    # cuando lo accionable eran 28 (medido el 17-sep-2026).
    nunca = Q(estado_enlace__alcanzable=False, estado_enlace__respondio_alguna_vez=False)
    total = base.count()
    activas = base.filter(estado_enlace__alcanzable=True).count()
    caidas = base.filter(estado_enlace__alcanzable=False).exclude(nunca).count()
    nunca_respondieron = base.filter(nunca).count()
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
        listado = listado.filter(estado_enlace__alcanzable=False).exclude(nunca)
    elif filtros['estado'] == 'nunca':
        listado = listado.filter(nunca)
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
            # Las caidas reales primero; las que nunca respondieron despues de las
            # activas, porque no son trabajo de hoy sino de revisar la carga de datos.
            When(nunca, then=3),
            When(estado_enlace__alcanzable=False, then=0),
            When(estado_enlace__alcanzable=True, then=1),
            default=2,
            output_field=IntegerField(),
        ),
    ).order_by('orden_estado', 'codigo')

    pagina, query_filtros = paginar(listado, request)

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
            # La fila tiene que decir lo mismo que el KPI: marcar "Caído" un sitio que
            # nunca respondió manda a alguien a abrir un ticket con el proveedor por un
            # enlace que nunca estuvo arriba.
            'nunca_respondio': bool(estado and estado.nunca_respondio),
            'total_kbps': total_kbps,
            # Mismo criterio que el modal de esta farmacia: % de SU ancho contratado
            # cuando se conoce, y recién si no, el umbral absoluto en kbps. Antes la
            # columna usaba siempre el absoluto y el modal el porcentaje, así que la
            # misma farmacia salía verde en la tabla y roja al abrirla.
            'estado_bw': clasificar(total_kbps, *limites_de_enlace(farmacia.ancho_contratado_mbps)),
            # Qué parte del enlace contratado es ese consumo, para el medidor de la
            # columna. None cuando no se sabe el contratado: sin eso no hay medidor,
            # solo el número.
            'pct_contratado': (
                min(round(100 * total_kbps / (farmacia.ancho_contratado_mbps * 1000)), 100)
                if total_kbps is not None and farmacia.ancho_contratado_mbps else None
            ),
        })

    # Las caídas en curso se listan completas pero la plantilla las trae colapsadas: 154
    # ítems abiertos empujan la tabla fuera de la pantalla, que es el problema que tenía
    # esta vista. El conteo va aparte para poder mostrarlo sin recorrer la lista.
    en_curso = EventoEnlaceFarmacia.objects.filter(
        fin__isnull=True, farmacia__in=base.values('pk'),
    ).select_related('farmacia').order_by('inicio')

    return render(request, 'panel/enlaces_farmacias_lista.html', {
        'filas': filas,
        'activas': activas,
        'caidas': caidas,
        'nunca_respondieron': nunca_respondieron,
        'sin_sondear': sin_sondear,
        'total': total,
        'con_ancho_banda': con_ancho_banda,
        'en_curso': en_curso,
        'en_curso_total': en_curso.count(),
        'pagina': pagina,
        'filtros': filtros,
        'query_filtros': query_filtros,
        'grupos': Grupo.objects.filter(farmacias__in=base.values('pk')).distinct().order_by('codigo'),
        'umbral_fallas': EstadoEnlaceFarmacia.UMBRAL_FALLAS_CONSECUTIVAS,
    })


def _render_enlace_modal(request, farmacia):
    """Contenido del modal de una farmacia: estado del enlace + su consumo.

    El consumo de ancho de banda vive acá y no en la tabla porque es un dato de
    profundidad: en el listado de 700 filas solo interesa "responde o no", y mirar el
    tráfico es algo que se hace de a una farmacia, cuando ya sospechás de esa.
    """
    from apps.monitoreo.models import EventoEnlaceFarmacia, MuestraRedFarmacia, ReinicioEquipoBorde

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

    # La serie se escala al ancho CONTRATADO, no al pico de la ventana. Con autoescala,
    # un pico de 3.481 kbps llenaba todo el alto del gráfico y se leía como saturación,
    # cuando sobre un enlace de 10 Mbps es un 35%. El eje tiene que ser la capacidad del
    # enlace para que la altura signifique algo.
    #
    # Sin ancho contratado se vuelve a la autoescala, y la plantilla lo dice: un gráfico
    # sin eje declarado se puede mirar para ver la FORMA (picos, mesetas), pero no para
    # juzgar cuánto se está usando.
    escala_kbps = farmacia.ancho_contratado_mbps * 1000 if farmacia.ancho_contratado_mbps else None

    # Los límites en kbps de ESTA farmacia. Se pasan como `limites_fijos` porque no
    # salen de una ReglaAlerta global sino del contrato de cada sitio: 8 Mbps de consumo
    # son el 80% de un enlace de 10 y el 200% de uno de 4.
    limites_kbps = limites_de_enlace(farmacia.ancho_contratado_mbps)
    consumo = indicador(
        Metrica('red_total_kbps', 'Consumo actual', ' kbps', escala_fija=escala_kbps),
        [m.red_total_kbps for m in muestras],
        valor=ultima.red_total_kbps if ultima else None,
        limites_fijos=limites_kbps,
        # Sin ancho contratado el eje es el pico de la ventana —una escala distinta en
        # cada farmacia—, así que la línea de umbral no se dibuja: significaría algo
        # distinto en cada modal. El color del número sí sale del umbral absoluto, que es
        # lo único que queda, y el pie de la tarjeta lo aclara.
        dibujar_umbrales=bool(escala_kbps),
        # Solo cuando hay algo que colorear: con la tarjeta en "sin dato", explicar de
        # dónde sale un color que no se está mostrando confunde más de lo que aclara.
        nota=(
            'Sin ancho contratado cargado: el color sale del umbral general en kbps, no '
            'de qué parte de ESTE enlace se está usando.'
            if ultima and not escala_kbps else ''
        ),
    )

    return render(request, 'panel/enlace_farmacia_modal.html', {
        'farmacia': farmacia,
        'estado': getattr(farmacia, 'estado_enlace', None),
        'ultima': ultima,
        'total_muestras': len(muestras),
        'escala_kbps': escala_kbps,
        # El pico REAL de la ventana, aparte del tope del eje: si supera lo contratado, el
        # gráfico lo recorta arriba y hace falta decirlo con un número.
        'pico_kbps': max((m.red_total_kbps for m in muestras if m.red_total_kbps is not None), default=None),
        'consumo': consumo,
        # Mismo objeto Grafico que usa la tarjeta: el modal lo sigue exponiendo con este
        # nombre porque la escala del eje es parte del contrato de esta pantalla.
        'g_red': consumo.grafico,
        'estado_bw': clasificar(ultima.red_total_kbps if ultima else None, *limites_kbps),
        'bw_umbral_warning_pct': BW_CONTRATADO_UMBRAL_WARNING_PCT,
        'bw_umbral_critical_pct': BW_CONTRATADO_UMBRAL_CRITICAL_PCT,
        'estacion_sondeadora': estacion_sondeadora,
        'caidas_recientes': EventoEnlaceFarmacia.objects.filter(farmacia=farmacia)[:5],
        'reinicios_recientes': ReinicioEquipoBorde.objects.filter(farmacia=farmacia)[:5],
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
