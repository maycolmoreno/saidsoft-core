from datetime import timedelta

from django.contrib.auth.decorators import login_required
from django.db.models import Count, F, Max, Q
from django.shortcuts import render
from django.views.decorators.http import require_GET
from django.utils import timezone

from apps.catalogo.models import Estacion, Farmacia
from apps.catalogo.horarios import separar_calladas_por_horario
from apps.catalogo.services import calcular_matriz_cumplimiento, estado_flota
from apps.cuentas.services import unidades_negocio_en_foco
from apps.despliegues.models import Despliegue, ResultadoDespliegue
from apps.monitoreo.enlaces import VENTANA_LINEA_TIEMPO_HORAS, linea_tiempo_caidas_en_curso
from apps.monitoreo.incidencias import cola_incidencias, mosaico_farmacias
from apps.monitoreo.models import (
    Alerta, EstadoEnlaceFarmacia, EventoEnlaceFarmacia, MuestraRedFarmacia,
)
# La salud de la plataforma la calcula el dominio, no esta vista: la consumen también la
# barra superior (apps.panel.context_processors) y, el día que se unifiquen, el comando
# `verificar_salud`. Ver el docstring de apps.monitoreo.salud.
from apps.monitoreo.graficos import construir_grafico
from apps.monitoreo.salud import estado_latidos, resumen_salud
from apps.monitoreo.series import serie_disponibilidad
from apps.monitoreo.services import antiguedad_alertas_sin_resolver, fuente_desactualizada
from apps.monitoreo.umbrales import ONLINE_UMBRAL_MINUTOS
# Los dos umbrales de salud de la plataforma vivian ACA y el comando verificar_salud
# los importaba, con lo que el dominio terminaba dependiendo de una vista. Despues la
# fuente de verdad paso a apps.monitoreo.umbrales y esta vista los consumia; desde el
# 10-oct-2026 no los necesita ni siquiera para eso, porque la COMPARACION tambien vive en
# el dominio (apps.monitoreo.salud) y acá solo se lee el resultado. Es la misma direccion,
# un paso mas: el panel no decide que es "sano".
from apps.mqtt_worker.management.commands.registrar_latido import NOMBRE_RESPALDO
from apps.mqtt_worker.models import EnrolamientoRechazado, MensajeMqttFallido
from apps.mqtt_worker.services import NOMBRE_WORKER_MQTT

# `ONLINE_UMBRAL_MINUTOS = 5` vivía ACÁ, y era el primero de los tres criterios de
# "estación en línea" que se contradecían entre pantallas: 5 minutos en este tablero, 10
# en el Centro, y el campo `estado_conexion` en el listado (hallazgo D-01). Ahora el valor
# —y el motivo de que haya DOS, uno para mostrar y otro para alertar— vive en
# `apps.monitoreo.umbrales`, y esta vista solo lo consume.


# Cada cuanto se refresca sola la parte viva del tablero.
#
# Sesenta segundos, el mismo valor y el mismo motivo que `SEGUNDOS_REFRESCO_CENTRO`: es la
# cadencia real mas rapida del backend (`marcar-estaciones-offline` corre cada 60 s y el
# sondeo de enlaces cada 2 min), asi que pollear mas seguido mostraria varias veces el
# mismo dato, multiplicado por cada agente que deja la pantalla abierta.
#
# UN solo endpoint para todas las zonas vivas y no uno por zona: con cadencias distintas,
# dos zonas de la misma pantalla pueden mostrar estados de momentos distintos y
# contradecirse — que es el problema que este trabajo vino a cerrar, no a mover de lugar.
SEGUNDOS_REFRESCO_TABLERO = 60


def _contexto_vivo(request):
    """Todo lo que cambia solo. Lo comparten la carga inicial y el refresco de HTMX.

    Una funcion y no dos vistas con el mismo cuerpo: el marco la usa para renderizar la
    pagina completa de entrada —sin el parpadeo de un "Cargando..." en la pantalla que se
    abre al entrar— y el parcial la usa para devolver solo el interior cada minuto. Si
    cada uno armara su propio contexto, volverian a poder discrepar.
    """
    visibles = unidades_negocio_en_foco(request)

    total_pendientes = Estacion.objects.filter(
        estado_aprobacion=Estacion.EstadoAprobacion.PENDIENTE, farmacia__unidad_negocio__in=visibles,
    ).count()

    # La flota, de la FUENTE ÚNICA. Antes estos dos números se sumaban en Python sobre
    # `grupos`, que excluye el grupo `PENDIENTE` y no filtra por estado de aprobación: el
    # KPI más visible del panel contaba una población que no era la flota ni ninguna otra
    # cosa definida (hallazgo D-02). Ahora es el mismo número que da el Centro.
    flota = estado_flota(visibles)
    total_estaciones = flota['total']
    total_online = flota['en_linea']

    # Salud de la plataforma (Zona 0). Sale de `apps.monitoreo.salud`, el mismo lugar del
    # que la lee la barra superior: si cada uno la calculara por su lado, la barra podría
    # decir "verificada" mientras el tablero muestra un componente en rojo, que es
    # exactamente el problema que esta zona existe para cerrar.
    #
    # Son DOS filas de la misma tabla de tres, así que una consulta y no dos: hasta el
    # 10-oct-2026 esta vista hacía un `.filter(nombre=...)` por cada componente (hallazgo
    # R-2). No tiene tenant propio — es salud de infraestructura, no dato de cliente, así
    # que se muestra igual para todos.
    componentes_salud = estado_latidos()
    salud = resumen_salud(componentes_salud)
    por_clave = {c.clave: c for c in componentes_salud}
    latido_worker = por_clave.get(NOMBRE_WORKER_MQTT)
    # `not critico` y no `ok`: este flag dispara el aviso rojo que afirma que NO se están
    # procesando despliegues, latidos ni scripts, y eso solo es cierto si el worker está
    # muerto o desconectado del broker. Un worker que late pero todavía no reporta su
    # conexión queda en ámbar en la Zona 0 —se dice— y no dispara el aviso: afirmarlo
    # sería mentir al revés, y pasaría en la ventana de cualquier despliegue.
    worker_mqtt_activo = not (latido_worker is None or latido_worker.critico)

    # Alertas, en DOS números. Hasta hoy era uno solo rotulado "Alertas abiertas" que
    # sumaba abiertas + reconocidas, mientras el Centro contaba solo las abiertas: los dos
    # tableros mostraban cifras distintas para algo que se llamaba igual, y ninguno decía
    # qué contaba (hallazgo D-03). Una reconocida no está resuelta, pero tiene a alguien
    # encima, y eso cambia qué hace el operador con ella.
    #
    # Una consulta con dos conteos condicionales, no dos consultas.
    conteo_alertas = Alerta.objects.filter(
        estado__in=[Alerta.Estado.ABIERTA, Alerta.Estado.RECONOCIDA],
        estacion__farmacia__unidad_negocio__in=visibles,
    ).aggregate(
        sin_reconocer=Count('pk', filter=Q(estado=Alerta.Estado.ABIERTA)),
        en_atencion=Count('pk', filter=Q(estado=Alerta.Estado.RECONOCIDA)),
    )

    # Las MISMAS alertas de arriba, repartidas por antigüedad. Un total de alertas no
    # dice si hay que correr: cinco abiertas en la última hora son un incidente en curso
    # y cinco de hace dos semanas son deuda que nadie atendió, y hasta hoy el tablero
    # mostraba las dos cosas como el mismo número.
    #
    # Lo calcula el dominio (`apps.monitoreo.services`), no esta vista, y en UNA consulta
    # con conteos condicionales: la vista no decide qué es "viejo" ni cuántas cubetas hay.
    antiguedad_alertas = antiguedad_alertas_sin_resolver(visibles)

    # Zona 1: la cola unificada. Reemplaza a las dos listas por dominio que había acá
    # —"alertas abiertas más recientes" y "enlaces caídos"— porque una farmacia con el
    # enlace caído aparecía en las dos Y en "estaciones sin reportar", tres veces por un
    # único hecho. Ver apps.monitoreo.incidencias para el orden de prioridad.
    #
    # Se pide la lista COMPLETA y se recorta acá: el total es el número de farmacias
    # afectadas, que es lo que dimensiona la jornada, y sacarlo de la misma lista evita
    # una segunda consulta que podría contar algo ligeramente distinto.
    incidencias = cola_incidencias(visibles, limite=None)
    total_incidencias = len(incidencias)

    # Zona 2e — el mosaico. REUSA la cola que se acaba de calcular, así que cuesta una
    # sola consulta más (las farmacias con agente): el estado de cada celda sale de la
    # misma fuente que la Zona 1, y por construcción no pueden contradecirse.
    #
    # Va antes del recorte a 8: el mosaico necesita la cola COMPLETA para saber el estado
    # de cada farmacia, no solo las ocho que se listan arriba.
    mosaico = mosaico_farmacias(visibles, incidencias=incidencias)

    incidencias = incidencias[:8]

    # Zona 5: las estaciones calladas, con el MISMO criterio que el contador.
    #
    # Solo aprobadas: una pendiente de aprobación no reporta porque todavía no le toca, y
    # ya tiene su propio bloque de enrolamiento. `nulls_first` pone adelante a las que
    # nunca reportaron — una estación aprobada que jamás dio señal es una instalación
    # fallida, no una caída.
    #
    # **Se excluyen las administrativas**, igual que hace `estado_flota`: se apagan de
    # noche y el fin de semana, así que sumarlas haría saltar esta lista ~100 todas las
    # noches y dejaría de significar "hay un problema". Su estado se ve aparte, en la
    # tarjeta de la flota. Hasta hoy el contador las excluía (vía `resumen_operacion`) y
    # esta lista no, así que la tarjeta y la lista hablaban de conjuntos distintos.
    #
    # El total NO se cuenta otra vez: sale de `flota`, que ya lo calculó en la misma
    # agregación. Son dos cifras que tienen que cuadrar, y la única forma de garantizarlo
    # es que vengan del mismo cálculo.
    limite_online = timezone.now() - timedelta(minutes=ONLINE_UMBRAL_MINUTOS)
    sin_reportar = (
        Estacion.objects
        .filter(
            estado_aprobacion=Estacion.EstadoAprobacion.APROBADA,
            farmacia__unidad_negocio__in=visibles,
        )
        .exclude(farmacia__tipo=Farmacia.Tipo.ADMINISTRATIVO)
        .filter(Q(ultimo_heartbeat__isnull=True) | Q(ultimo_heartbeat__lt=limite_online))
        .select_related('farmacia')
        .order_by(F('ultimo_heartbeat').asc(nulls_first=True))
    )
    total_sin_reportar = flota['sin_reportar']

    # El reparto por el horario de CADA farmacia, que es la diferencia entre "hay 37
    # estaciones calladas" y "hay 7 que deberían estar vendiendo y no responden".
    #
    # Medido el 12-oct-2026 a las 23:29: 37 calladas de 66 y ninguna era una avería —eran
    # locales cerrados—. Una franja global no alcanza: de las 702 farmacias activas, 8
    # abren 24 horas y 42 cierran después de las 22:00, así que cualquier hora de corte
    # única miente para unas 50 (ver apps.catalogo.horarios).
    #
    # Se MATERIALIZA la lista en vez de dejarla perezosa: antes se pedía `[:6]` y esto
    # necesita todas para repartirlas. Es la MISMA consulta, sin el LIMIT — son decenas de
    # filas, no miles, porque ya viene acotada a aprobadas y no administrativas. El reparto
    # no consulta nada: el horario viaja en `farmacia`, que ya venía con select_related.
    calladas = list(sin_reportar)
    reparto_calladas = separar_calladas_por_horario(calladas)

    # Farmacias sin enlace, para el KPI. La LISTA ya no vive acá: sus filas entran en la
    # cola de incidencias de arriba, con prioridad 1, porque una farmacia sin enlace no
    # vende y es lo primero que hay que ver.
    #
    # Mismas tres exclusiones que `resumen_operacion`, el Centro y la cola, para que los
    # cuatro números no puedan discrepar:
    #   - `fin__isnull=True`  -> solo las caídas todavía abiertas.
    #   - `farmacia__activa`  -> una farmacia dada de baja deja de sondearse y su estado
    #                            queda congelado; no es una caída que alguien vaya a ir a
    #                            arreglar (reportado con GP063 el 2-oct-2026).
    #   - `respondio_alguna_vez=False` excluido -> nunca respondió es configuración
    #                            pendiente, no una caída.
    #
    # Se cuentan FARMACIAS distintas y no eventos: es lo que dice la etiqueta, y así el
    # número es inmune a un segundo evento abierto para el mismo sitio — que nada impide
    # hoy, porque no hay constraint (hallazgo D-04).
    total_enlaces_caidos = (
        EventoEnlaceFarmacia.objects
        .filter(fin__isnull=True, farmacia__unidad_negocio__in=visibles, farmacia__activa=True)
        .exclude(farmacia__estado_enlace__respondio_alguna_vez=False)
        .values('farmacia_id').distinct().count()
    )

    # Las MISMAS caídas, dibujadas. El KPI de arriba dice cuántas son; esto dice desde
    # cuándo, que es lo que decide a dónde va el técnico primero. Mismo queryset y mismas
    # tres exclusiones —las calcula el dominio, en `apps.monitoreo.enlaces`— así que el
    # número y las barras no pueden hablar de conjuntos distintos.
    #
    # El total de la lista NO se vuelve a contar: la plantilla muestra
    # `total_enlaces_caidos`, el del KPI.
    caidas_en_curso = linea_tiempo_caidas_en_curso(visibles)

    # Zona 2d — la serie de 24 h: lo mismo que el KPI de arriba, pero con memoria.
    #
    # Se reconstruye desde las transiciones (`apps.monitoreo.series`), no se guarda: son
    # ~103 filas para 24 horas de flota, contra los 24 x N snapshots que haría falta
    # persistir. El umbral NO da problema —medido: los cuatro criterios dan el mismo
    # número, ver el docstring de ese módulo— así que el último punto coincide con el KPI.
    #
    # La marca es el PROMEDIO del período, no un SLA: nadie definió un objetivo de
    # disponibilidad en este proyecto, y dibujar una línea de "90%" sería inventarlo.
    serie_24h = serie_disponibilidad(visibles)
    promedio_24h = round(sum(p.pct for p in serie_24h) / len(serie_24h), 1) if serie_24h else 0
    grafico_24h = construir_grafico(
        [p.pct for p in serie_24h], ancho=640, alto=120, escala_fija=100,
        marcas=[(promedio_24h, 'referencia', 'promedio %.0f%%' % promedio_24h)] if serie_24h else (),
    )

    # Misma consulta que arriba: `componentes_salud` ya trajo la tabla entera.
    latido_respaldo = por_clave.get(NOMBRE_RESPALDO)
    horas_sin_respaldo = None
    if latido_respaldo is not None and latido_respaldo.instante is not None:
        horas_sin_respaldo = (timezone.now() - latido_respaldo.instante).total_seconds() / 3600

    # --- Frescura de los datos (Zona 0, segunda mitad) --------------------------------
    #
    # Un worker vivo NO prueba que la cadena esté entregando: el latido lo escribe un hilo
    # aparte sin consultar la conexión al broker (hallazgo D-05). Estas cuatro señales son
    # lo más barato que sí lo prueba, y las cuatro salen de datos que ya se persisten y que
    # hasta ahora no se veían en ninguna pantalla.
    #
    # Sin tenant, por el mismo motivo que el latido del worker: es salud de la
    # infraestructura. Acotarlas al alcance del usuario haría que un cliente sin
    # estaciones leyera "sin datos" y concluyera que la plataforma está caída.
    ultimo_latido_flota = (
        Estacion.objects
        .filter(estado_aprobacion=Estacion.EstadoAprobacion.APROBADA)
        .aggregate(ultimo=Max('ultimo_heartbeat'))['ultimo']
    )
    # La cola de mensajes que el worker recibió y NO pudo procesar. Existe desde que existe
    # el worker y solo se podía mirar desde el admin de Django (hallazgo D-11): es la
    # prueba directa de "recibe pero no procesa", que es justo lo que el latido no dice.
    mqtt_fallidos = MensajeMqttFallido.objects.filter(revisado=False).count()
    # Agentes que intentaron enrolarse con un código cuyo sitio no existe. La pantalla que
    # los lista existe desde el 27-sep-2026 y NINGUNA plantilla la enlazaba (hallazgo
    # D-10): para llegar había que escribir la URL a mano. Es el caso de "se instaló el
    # agente, el técnico se fue, y la estación nunca apareció en el panel".
    enrolamientos_rechazados = EnrolamientoRechazado.objects.filter(revisado=False).count()
    # Frescura del barrido de enlaces. Importa porque el barrido ABORTA completo si falla
    # el 80% o más —protección correcta contra declarar 704 caídas por una ruta perdida—
    # y entonces no escribe nada: el panel sigue mostrando los últimos estados buenos como
    # si fueran de ahora. Pasó: 4,7 h sin un solo sondeo el 2-oct-2026.
    ultimo_sondeo_enlaces = (
        EstadoEnlaceFarmacia.objects
        .order_by('-ultima_verificacion')
        .values_list('ultima_verificacion', flat=True)
        .first()
    )
    # Celery Beat no deja latido propio; se infiere de que sus tareas escriban. Se mira la
    # muestra de red porque su tarea es de las más frecuentes (5 min) — mismo criterio que
    # `verificar_salud._revisar_beat`, que lee esta misma tabla.
    ultima_muestra_red = (
        MuestraRedFarmacia.objects
        .order_by('-timestamp')
        .values_list('timestamp', flat=True)
        .first()
    )

    return {
        # Zona 0 — salud de la plataforma y frescura de los datos.
        'salud': salud,
        'ultimo_latido_flota': ultimo_latido_flota,
        'latido_flota_vencido': fuente_desactualizada(ultimo_latido_flota, 'estaciones'),
        'mqtt_fallidos': mqtt_fallidos,
        'enrolamientos_rechazados': enrolamientos_rechazados,
        'ultimo_sondeo_enlaces': ultimo_sondeo_enlaces,
        'sondeo_enlaces_vencido': fuente_desactualizada(ultimo_sondeo_enlaces, 'enlaces'),
        'ultima_muestra_red': ultima_muestra_red,
        'beat_vencido': fuente_desactualizada(ultima_muestra_red, 'red_farmacias'),
        'total_pendientes': total_pendientes,
        # Zona 1 — la cola unificada.
        'incidencias': incidencias,
        'total_incidencias': total_incidencias,
        'mosaico': mosaico,
        # Zona 2 — indicadores. `flota` entero va al contexto porque la Zona 5 usa el
        # desglose (nunca reportó / dejaron de reportar / pausadas) y la tarjeta usa el
        # resumen: los dos tienen que salir del mismo cálculo o volverían a discrepar.
        'flota': flota,
        'total_estaciones': total_estaciones,
        'total_online': total_online,
        'antiguedad_alertas': antiguedad_alertas,
        'alertas_sin_reconocer': conteo_alertas['sin_reconocer'],
        'alertas_en_atencion': conteo_alertas['en_atencion'],
        'total_enlaces_caidos': total_enlaces_caidos,
        'caidas_en_curso': caidas_en_curso,
        'serie_24h': serie_24h,
        'grafico_24h': grafico_24h,
        'promedio_24h': promedio_24h,
        'minimo_24h': min(serie_24h, key=lambda p: p.pct) if serie_24h else None,
        'maximo_24h': max(serie_24h, key=lambda p: p.pct) if serie_24h else None,
        'ventana_linea_tiempo_horas': VENTANA_LINEA_TIEMPO_HORAS,
        # Se muestran primero las que deberían estar operando: son las accionables. Si no
        # hay ninguna, se muestran las otras igual, para no dejar el bloque vacío cuando
        # el total dice que hay calladas.
        'sin_reportar': (reparto_calladas['dentro'] or calladas)[:6],
        'calladas_dentro_de_horario': reparto_calladas['total_dentro'],
        'calladas_fuera_de_horario': reparto_calladas['total_fuera'],
        'total_sin_reportar': total_sin_reportar,
        'worker_mqtt_activo': worker_mqtt_activo,
        'worker_mqtt_ultimo_latido': latido_worker.instante if latido_worker else None,
        'respaldo_ultimo': latido_respaldo.instante if latido_respaldo else None,
        # None (nunca respaldó) cuenta como NO al día: es el estado de un servidor
        # recién montado y también el de uno donde el timer nunca se instaló. Lo decide
        # `apps.monitoreo.salud` comparando contra RESPALDO_UMBRAL_HORAS; acá solo se lee,
        # para que la franja de aviso y la Zona 0 no puedan discrepar sobre lo mismo.
        'respaldo_al_dia': bool(latido_respaldo and latido_respaldo.ok),
        'respaldo_horas': horas_sin_respaldo,
        # La hora del CALCULO, distinta de la hora de cada dato. Las dos tienen que estar
        # visibles y no confundirse: "calculado 14:32" dice cuando corrio la consulta, y
        # cada fila de la Zona 0 dice cuando llego SU dato. El tablero no tenia ninguna de
        # las dos, asi que una pantalla abierta desde las 8:00 mostraba las 8:00 y no lo
        # decia (hallazgo D-07).
        'calculado_en': timezone.now(),
        'segundos_refresco': SEGUNDOS_REFRESCO_TABLERO,
    }


@login_required
def dashboard(request):
    """El tablero completo, renderizado de entrada.

    Se sirve ya armado y no con un "Cargando..." que HTMX rellena —como si hace el Centro—
    porque esta es la pantalla que se abre al entrar al panel: un parpadeo en la portada se
    paga en cada navegacion, y aca el contenido inicial no cuesta nada extra (el parcial
    usaria las mismas consultas un segundo despues).

    La Zona 6 (cumplimiento y despliegues) NO entra: va plegada y se carga sola la primera
    vez que alguien la abre. Son 5 `Count(DISTINCT)` sobre dos JOIN que hasta hoy se
    pagaban en cada carga de la portada para una tabla que ni se estaba mirando.
    """
    return render(request, 'panel/dashboard.html', _contexto_vivo(request))


@login_required
@require_GET
def dashboard_vivo_partial(request):
    """Solo el interior que cambia. Lo reemplaza HTMX cada SEGUNDOS_REFRESCO_TABLERO.

    Mismo patron y mismo motivo que `centro_monitoreo_partial`: a un refresco por minuto
    por persona, mandar la navegacion, el CSS y la cabecera cada vez es trabajo puro.
    """
    return render(request, 'panel/dashboard_vivo_partial.html', _contexto_vivo(request))


@login_required
@require_GET
def dashboard_cumplimiento_partial(request):
    """Zona 6: cumplimiento por grupo y despliegues en curso. Se carga al ABRIRSE.

    Vive aparte porque es lo caro de la pantalla y lo menos urgente. `calcular_matriz_
    cumplimiento` hace 5 `Count(DISTINCT)` sobre `Grupo -> Farmacia -> Estacion`, y se
    pagaba en cada carga del tablero —y en cada F5— para una tabla que ocupaba la primera
    posicion y que casi nadie estaba leyendo (hallazgos U-2 y R-4).

    No se refresca sola: la version objetivo de un canal cambia cuando alguien publica un
    despliegue, no cada minuto.
    """
    visibles = unidades_negocio_en_foco(request)

    # Un Grupo (canal TRX) puede estar compartido por farmacias de varias unidades de
    # negocio (ver apps.cumplimiento) -- calcular_matriz_cumplimiento ya se encarga de
    # mostrar solo los grupos con al menos una farmacia visible para este usuario, y de
    # contar unicamente esas farmacias/estaciones, no las de un tenant al que no tiene
    # acceso.
    #
    # Sin `umbral_online_minutos`: desde el 10-oct-2026 el defecto de esa funcion ES
    # `ONLINE_UMBRAL_MINUTOS`, asi que la columna "En linea" de la tabla y la tarjeta de
    # arriba hablan con el mismo reloj sin que esta vista tenga que recordarselo.
    grupos = calcular_matriz_cumplimiento(visibles)

    # Progreso y errores de cada despliegue en curso, anotados en la MISMA consulta. El
    # tablero mostraba version y estado y nada mas, asi que no habia forma de ver desde
    # aca que un despliegue estuviera fallando (hallazgo D-13). El porcentaje no se
    # inventa: es el mismo criterio que `apps.panel.progreso.resumen_de_progreso` usa en
    # la pantalla de detalle -- `APLICADO` cuenta como terminado, `ERROR` y `ROLLBACK`
    # como falla, y el resto esta en camino.
    despliegues_activos = (
        Despliegue.objects
        .filter(estado__in=[Despliegue.Estado.PUBLICANDO, Despliegue.Estado.PAUSADO], unidad_negocio__in=visibles)
        .annotate(
            total_resultados=Count('resultados', distinct=True),
            aplicados=Count('resultados', filter=Q(resultados__estado=ResultadoDespliegue.Estado.APLICADO), distinct=True),
            con_error=Count(
                'resultados',
                filter=Q(resultados__estado__in=[
                    ResultadoDespliegue.Estado.ERROR, ResultadoDespliegue.Estado.ROLLBACK,
                ]),
                distinct=True,
            ),
        )
        # Lo que tiene errores primero: es lo unico de este bloque que puede exigir una
        # decision ahora. Despues, el mas reciente.
        .order_by('-con_error', '-fecha_publicacion')
    )

    return render(request, 'panel/dashboard_cumplimiento_partial.html', {
        'grupos': grupos,
        'despliegues_activos': despliegues_activos,
    })
