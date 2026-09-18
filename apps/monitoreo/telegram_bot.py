"""Consultas de solo lectura por Telegram: /enlaces, /estado, /alertas, /farmacia.

Complementa la notificación saliente (ver `notificar_alerta` y
`notificar_cambios_enlaces`): eso avisa cuando algo pasa, esto responde cuando alguien
pregunta. Nada de acá modifica nada — ni aprueba estaciones, ni reconoce alertas, ni
manda comandos a un agente. Es deliberado: el canal de entrada de un bot público no es
el lugar para accionar sobre 1.800 equipos.

**Autorización.** Un bot de Telegram es público: cualquiera que adivine su nombre de
usuario puede escribirle. Solo se responde a los chat_id de
`TELEGRAM_CHAT_IDS_AUTORIZADOS`; a cualquier otro no se le contesta nada, ni siquiera
"no autorizado" — confirmar que el bot existe y a qué responde ya es información. Lo
que devuelven estos comandos (códigos de farmacia, IPs de routers, qué está caído y
desde cuándo) es exactamente el mapa que alguien necesitaría para atacar la red.

El transporte lo pone `run_telegram_bot` (long polling). Acá vive solo qué se responde,
para que se pueda probar sin red.
"""
import logging

from django.conf import settings
from django.utils import timezone

logger = logging.getLogger(__name__)

# Cuántas filas entran en una respuesta antes de resumir. Telegram parte los mensajes
# largos (ver `_trozos_telegram`), pero una lista de 143 sitios en el teléfono no se lee:
# lo que sirve es el total y los más recientes.
_MAX_FILAS = 15


def _texto_intervalo(delta) -> str:
    """"3 h 12 min" en vez de "192 minutos": es lo que se lee de un vistazo."""
    minutos = max(0, int(delta.total_seconds() // 60))
    if minutos < 60:
        return f'{minutos} min'
    horas, resto = divmod(minutos, 60)
    if horas < 24:
        return f'{horas} h {resto} min'
    dias, horas = divmod(horas, 24)
    return f'{dias} d {horas} h'


def _texto_duracion(desde) -> str:
    """Cuánto pasó desde `desde`. Para lo que FALTA, usar `_texto_intervalo`."""
    if desde is None:
        return 'sin dato'
    return _texto_intervalo(timezone.now() - desde)


def _comando_enlaces() -> str:
    """Cuántos enlaces están caídos y desde cuándo.

    Separa las caídas reales de los sitios que NUNCA respondieron: son dos problemas
    distintos y mezclarlos hacía que el panel dijera 162 cuando lo accionable eran 13
    (ver `EstadoEnlaceFarmacia.nunca_respondio`). Un total inflado no sirve para
    reportarle nada a un proveedor.
    """
    from .models import EstadoEnlaceFarmacia, EventoEnlaceFarmacia

    estados = EstadoEnlaceFarmacia.objects.select_related('farmacia')
    caidas = [e for e in estados.filter(alcanzable=False) if not e.nunca_respondio]
    nunca = estados.filter(alcanzable=False, respondio_alguna_vez=False).count()
    activos = estados.filter(alcanzable=True).count()

    if not caidas:
        return (
            f'✅ Ningún enlace caído.\n\n'
            f'{activos} responden · {nunca} nunca respondieron (revisar ruta o IP cargada).'
        )

    # El inicio real de la caída sale del evento abierto, no de `ultimo_cambio_estado`:
    # el evento marca el PRIMER fallo, no el sondeo que confirmó la caída.
    abiertos = {
        e.farmacia_id: e
        for e in EventoEnlaceFarmacia.objects.filter(
            fin__isnull=True, farmacia__in=[c.farmacia_id for c in caidas],
        ).order_by('inicio')
    }

    def desde_de(estado):
        evento = abiertos.get(estado.farmacia_id)
        return evento.inicio if evento else estado.ultimo_cambio_estado

    caidas.sort(key=lambda e: desde_de(e) or timezone.now())

    lineas = [f'🔴 {len(caidas)} enlace(s) caído(s)', '']
    por_proveedor: dict = {}
    for estado in caidas:
        circuito = estado.farmacia.circuito_proveedor or ''
        proveedor = circuito.split('-')[0].strip() or '(sin circuito)'
        por_proveedor.setdefault(proveedor, []).append(estado)

    mostradas = 0
    for proveedor, items in sorted(por_proveedor.items()):
        lineas.append(f'  {proveedor} ({len(items)})')
        for estado in items:
            if mostradas >= _MAX_FILAS:
                break
            lineas.append(
                f'    {estado.farmacia.codigo}  {estado.farmacia.ip_router}  '
                f'hace {_texto_duracion(desde_de(estado))}'
            )
            mostradas += 1
        if mostradas >= _MAX_FILAS:
            break
    if len(caidas) > mostradas:
        lineas.append(f'    … y {len(caidas) - mostradas} más')

    lineas += [
        '',
        f'{activos} responden · {nunca} nunca respondieron (no son caídas: ruta o IP).',
    ]
    return '\n'.join(lineas)


def _comando_estado() -> str:
    """El "¿cómo está todo?" de un vistazo."""
    from datetime import timedelta

    from apps.catalogo.models import Estacion

    from .models import Alerta, EstadoEnlaceFarmacia, EstadoServicioPos, MuestraRedFarmacia, ReglaAlerta

    ahora = timezone.now()
    aprobadas = Estacion.objects.filter(estado_aprobacion='aprobada')
    total_est = aprobadas.count()
    en_linea = aprobadas.filter(ultimo_heartbeat__gte=ahora - timedelta(minutes=10)).count()

    abiertas = Alerta.objects.filter(estado=Alerta.Estado.ABIERTA).select_related('regla')
    criticas = sum(1 for a in abiertas if a.regla.severidad == ReglaAlerta.Severidad.CRITICAL)
    avisos = abiertas.count() - criticas

    estados = EstadoEnlaceFarmacia.objects.all()
    caidos = sum(1 for e in estados.filter(alcanzable=False) if not e.nunca_respondio)

    pos_caidos = EstadoServicioPos.objects.filter(disponible=False).count()

    # Si el sondeo dejó de correr, todo lo de arriba queda congelado sin avisar: el dato
    # más viejo delata que las tareas de fondo se cayeron.
    ultima = MuestraRedFarmacia.objects.order_by('-timestamp').first()
    frescura = _texto_duracion(ultima.timestamp) if ultima else 'sin datos'

    return '\n'.join([
        '📊 Estado de SAIDSOFT',
        '',
        f'Estaciones: {en_linea}/{total_est} en línea',
        f'Alertas abiertas: {criticas} crítica(s), {avisos} advertencia(s)',
        f'Enlaces caídos: {caidos}',
        f'Servicios del POS sin responder: {pos_caidos}',
        '',
        f'Último sondeo SNMP: hace {frescura}',
    ])


def _comando_alertas(*, solo_criticas: bool = False) -> str:
    """Alertas sin resolver. Con `solo_criticas`, únicamente las de severidad crítica.

    Un parámetro y no una función aparte: el query, el orden, el recorte por _MAX_FILAS
    y la marca de "reconocida" son idénticos, y duplicarlos garantizaba que el día que
    alguien cambie uno se olvide del otro. Lo único que cambia es el filtro y el
    encabezado.
    """
    from .models import Alerta, ReglaAlerta

    abiertas = Alerta.objects.filter(
        estado__in=[Alerta.Estado.ABIERTA, Alerta.Estado.RECONOCIDA],
    ).select_related('regla', 'estacion').order_by('-abierta_en')
    if solo_criticas:
        abiertas = abiertas.filter(regla__severidad=ReglaAlerta.Severidad.CRITICAL)
    abiertas = list(abiertas)

    if not abiertas:
        return ('✅ No hay alertas críticas abiertas.' if solo_criticas
                else '✅ No hay alertas abiertas.')

    titulo = ('alerta(s) CRÍTICA(s) sin resolver' if solo_criticas
              else 'alerta(s) sin resolver')
    lineas = [f'🔔 {len(abiertas)} {titulo}', '']
    for alerta in abiertas[:_MAX_FILAS]:
        emoji = '🔴' if alerta.regla.severidad == ReglaAlerta.Severidad.CRITICAL else '🟡'
        # Reconocida = alguien ya la vio, aunque no la haya resuelto. Distinguirlo evita
        # que dos personas salgan a atender lo mismo.
        marca = ' (reconocida)' if alerta.estado == Alerta.Estado.RECONOCIDA else ''
        lineas.append(
            f'{emoji} {alerta.estacion.codigo} — {alerta.regla.nombre}{marca}\n'
            f'    hace {_texto_duracion(alerta.abierta_en)}'
        )
    if len(abiertas) > _MAX_FILAS:
        lineas.append(f'… y {len(abiertas) - _MAX_FILAS} más')
    return '\n'.join(lineas)


def _comando_mantenimiento() -> str:
    """Ventanas de mantenimiento en curso: qué alertas están silenciadas y hasta cuándo.

    Importa poder consultarlo desde el teléfono porque una ventana activa explica el
    silencio: sin esto, "no llegó ninguna alerta" se lee igual si todo está bien que si
    alguien dejó abierta una ventana de la semana pasada. El `activo=True` junto con el
    rango es lo que mira `ventana_mantenimiento_activa` para silenciar, así que acá se
    aplica el MISMO criterio — mostrar algo distinto de lo que el motor usa sería peor
    que no mostrar nada.

    El destino se resuelve con `resolver_estaciones`, el mismo punto que usa el motor:
    contar las estaciones a mano según destino_tipo duplicaría esa lógica y se
    desincronizaría en cuanto aparezca un tipo de destino nuevo.
    """
    from apps.catalogo.services import resolver_estaciones

    from .models import VentanaMantenimiento

    ahora = timezone.now()
    activas = list(
        VentanaMantenimiento.objects
        .filter(activo=True, desde__lte=ahora, hasta__gte=ahora)
        .select_related('unidad_negocio').order_by('desde')
    )
    if not activas:
        return 'Sin ventanas de mantenimiento activas.'

    lineas = [f'🔧 {len(activas)} ventana(s) de mantenimiento en curso', '']
    for ventana in activas[:_MAX_FILAS]:
        estaciones = resolver_estaciones(
            ventana.destino_tipo, unidad_negocio=ventana.unidad_negocio,
            grupos=ventana.grupos.all(), farmacias=ventana.farmacias.all(),
            estaciones=ventana.estaciones.all(),
        )
        cantidad = estaciones.count()
        # Con pocas, los códigos dicen más que el número: se ve enseguida si la ventana
        # cubre lo que se quería. Con muchas, la lista tapa el resto del mensaje.
        if cantidad and cantidad <= 5:
            detalle = ', '.join(estaciones.values_list('codigo', flat=True))
        else:
            detalle = f'{cantidad} estación(es)'
        lineas.append(
            f'  {ventana.motivo}\n'
            f'    {ventana.unidad_negocio.codigo} · {detalle}\n'
            f'    hasta {timezone.localtime(ventana.hasta):%d/%m %H:%M} '
            f'(faltan {_texto_intervalo(ventana.hasta - ahora)})'
        )
    if len(activas) > _MAX_FILAS:
        lineas.append(f'… y {len(activas) - _MAX_FILAS} más')

    lineas += ['', 'Las alertas de esas estaciones están silenciadas a propósito.']
    return '\n'.join(lineas)


def _comando_toperrores() -> str:
    """Ranking de errores del POS en TODA la flota, agrupado por mensaje.

    Es una vista de flota y no de una estación (para eso está /farmacia): sirve para
    encontrar el mismo bug repetido en muchas farmacias, que es el que conviene arreglar
    una vez en vez de atenderlo sucursal por sucursal. Por eso lo que ordena es la
    cantidad total, y al lado va en cuántas estaciones distintas aparece: un error con
    5.000 repeticiones en una sola caja es un problema de esa caja; uno con 300 en 40
    farmacias es un problema del sistema.

    **Excluye `Categoria.NEGOCIO`**, el mismo criterio que ya aplica
    `manejar_pos_errores` al contar para la alerta. Esos son validaciones del POS
    haciendo su trabajo (ej. "VENTA SIN LOTE" bloqueando una venta), rutinarias y de
    altísima frecuencia: incluirlas haría que coparan las primeras posiciones y que este
    ranking nunca mostrara un bug real. Se siguen guardando y se ven en la ficha de la
    estación, solo no compiten acá.
    """
    from django.db.models import Count, Sum

    from .models import PosErrorDetectado

    ranking = list(
        PosErrorDetectado.objects
        .exclude(categoria=PosErrorDetectado.Categoria.NEGOCIO)
        .values('mensaje')
        .annotate(total=Sum('cantidad_total'), estaciones=Count('estacion', distinct=True))
        .order_by('-total')[:_MAX_FILAS]
    )
    if not ranking:
        return '✅ Sin errores de sistema en el log del POS.'

    lineas = ['🐞 Errores del POS más repetidos (toda la flota)', '']
    for fila in ranking:
        mensaje = fila['mensaje'][:110]
        lineas.append(
            f'  x{fila["total"]} · {fila["estaciones"]} estación(es)\n    {mensaje}'
        )
    lineas += ['', 'No se cuentan las validaciones de negocio (ej. VENTA SIN LOTE).']
    return '\n'.join(lineas)


def _comando_farmacia(codigo: str) -> str:
    from apps.catalogo.models import Farmacia

    from .models import EstadoEnlaceFarmacia, EstadoServicioPos, MuestraRedFarmacia

    codigo = (codigo or '').strip().upper()
    if not codigo:
        return 'Falta el código. Ejemplo: /farmacia ML016'

    farmacia = Farmacia.objects.filter(codigo=codigo).select_related('unidad_negocio').first()
    if farmacia is None:
        # Sugerir por prefijo ahorra el viaje de ir al panel a buscar el código exacto.
        parecidas = list(
            Farmacia.objects.filter(codigo__startswith=codigo[:3]).values_list('codigo', flat=True)[:8]
        )
        sugerencia = f'\n\n¿Quisiste decir? {", ".join(parecidas)}' if parecidas else ''
        return f'No encontré la farmacia {codigo}.{sugerencia}'

    lineas = [f'🏥 {farmacia.codigo} ({farmacia.unidad_negocio.codigo})', '']

    enlace = EstadoEnlaceFarmacia.objects.filter(farmacia=farmacia).first()
    if enlace is None or enlace.alcanzable is None:
        lineas.append('Enlace: sin sondear')
    elif enlace.alcanzable:
        lineas.append(f'Enlace: ✅ responde ({enlace.latencia_ms} ms)')
    elif enlace.nunca_respondio:
        lineas.append('Enlace: ⚠️ nunca respondió (revisar ruta o IP cargada)')
    else:
        lineas.append(f'Enlace: 🔴 caído hace {_texto_duracion(enlace.ultimo_cambio_estado)}')
    lineas.append(f'Router: {farmacia.ip_router} · circuito: {farmacia.circuito_proveedor or "sin dato"}')

    muestra = MuestraRedFarmacia.objects.filter(farmacia=farmacia).order_by('-timestamp').first()
    if muestra and muestra.red_total_kbps is not None:
        pct = muestra.porcentaje_del_contratado
        detalle = f' ({pct}% de {farmacia.ancho_contratado_mbps} Mbps)' if pct is not None else ''
        lineas.append(f'Tráfico: {muestra.red_total_kbps} kbps{detalle}')
    else:
        lineas.append('Tráfico: sin SNMP')

    estaciones = list(farmacia.estaciones.filter(estado_aprobacion='aprobada').order_by('codigo'))
    if estaciones:
        lineas += ['', f'Estaciones ({len(estaciones)}):']
        for estacion in estaciones:
            al_dia = (
                estacion.ultimo_heartbeat
                and (timezone.now() - estacion.ultimo_heartbeat).total_seconds() < 600
            )
            marca = '✅' if al_dia else '🔴'
            visto = _texto_duracion(estacion.ultimo_heartbeat) if estacion.ultimo_heartbeat else 'nunca'
            lineas.append(f'  {marca} {estacion.codigo} · visto hace {visto}')
            caidos = EstadoServicioPos.objects.filter(estacion=estacion, disponible=False)
            for servicio in caidos:
                lineas.append(f'      🔴 {servicio.get_servicio_display()}: {servicio.mensaje[:60]}')
    else:
        lineas += ['', 'Sin estaciones con agente instalado.']

    return '\n'.join(lineas)


_AYUDA = '\n'.join([
    'Consultas disponibles:',
    '',
    '/enlaces — enlaces caídos y desde cuándo',
    '/estado — resumen general',
    '/alertas — alertas sin resolver',
    '/criticas — solo las críticas',
    '/mantenimiento — ventanas activas (alertas silenciadas)',
    '/toperrores — errores del POS más repetidos en la flota',
    '/farmacia ML016 — detalle de una farmacia',
    '',
    'Solo lectura: nada de esto modifica el sistema.',
])


# Teclado que acompaña cada respuesta. Se repite siempre a propósito: sin él, después de
# la primera consulta habría que volver a escribir para hacer la siguiente, que es
# justamente lo que el teclado viene a evitar.
#
# `callback_data` tiene un tope duro de 64 bytes en Telegram, así que se manda un código
# corto ("cmd:enlaces", "farm:ML016") y no el texto del comando.
_TECLADO_PRINCIPAL = [
    [{'text': '🔴 Enlaces', 'callback_data': 'cmd:enlaces'},
     {'text': '📊 Estado', 'callback_data': 'cmd:estado'}],
    [{'text': '🔔 Alertas', 'callback_data': 'cmd:alertas'},
     {'text': '🏥 Farmacias', 'callback_data': 'menu:farmacias'}],
    # Los tres comandos nuevos van detrás de "Más" y no sueltos acá: siete botones en la
    # pantalla principal se leen peor que cuatro en un teléfono, y estos se consultan
    # menos seguido que el estado o los enlaces. El que los use a diario los tiene igual
    # como comando escrito.
    [{'text': '⋯ Más', 'callback_data': 'menu:mas'}],
]

_TECLADO_MAS = [
    [{'text': '🔴 Críticas', 'callback_data': 'cmd:criticas'},
     {'text': '🔧 Mantenimiento', 'callback_data': 'cmd:mantenimiento'}],
    [{'text': '🐞 Top errores POS', 'callback_data': 'cmd:toperrores'}],
    [{'text': '◀ Volver', 'callback_data': 'menu:principal'}],
]

# Cuántas farmacias entran en el submenú. Telegram deja mandar más, pero una pared de
# botones en el teléfono se vuelve peor que escribir el código.
_MAX_BOTONES_FARMACIA = 8

# Cuáles viven detrás de "Más", para devolver al operador al submenú correcto.
_COMANDOS_DEL_SUBMENU = frozenset({'criticas', 'mantenimiento', 'toperrores'})

# Qué comandos puede disparar un botón. Se declara aparte de los que acepta `responder_a`
# porque no son lo mismo: `/farmacia` necesita un argumento y por eso tiene su submenú.
_COMANDOS_CON_BOTON = frozenset({'enlaces', 'estado', 'alertas', 'criticas',
                                 'mantenimiento', 'toperrores'})


def _teclado_farmacias():
    """Submenú con las farmacias que hoy tienen algo para mirar.

    Se listan las que tienen un problema —enlace caído, o un servicio del POS sin
    responder— y no las 700: el teclado es un atajo para el caso frecuente, no un
    navegador del padrón. Para cualquier otra sigue estando `/farmacia CODIGO`.
    """
    from apps.catalogo.models import Farmacia

    from .models import EstadoEnlaceFarmacia, EstadoServicioPos

    codigos = []
    for estado in EstadoEnlaceFarmacia.objects.filter(alcanzable=False).select_related('farmacia'):
        if not estado.nunca_respondio:
            codigos.append(estado.farmacia.codigo)
    con_pos_caido = (
        EstadoServicioPos.objects.filter(disponible=False, ultima_respuesta__isnull=False)
        .values_list('estacion__farmacia__codigo', flat=True).distinct()
    )
    codigos.extend(con_pos_caido)

    # Sin nada roto, se ofrecen las que tienen agente: son las únicas con detalle rico.
    if not codigos:
        codigos = list(
            Farmacia.objects.filter(estaciones__estado_aprobacion='aprobada')
            .values_list('codigo', flat=True).distinct()
        )

    unicos = sorted(set(codigos))[:_MAX_BOTONES_FARMACIA]
    filas = [
        [{'text': c, 'callback_data': f'farm:{c}'} for c in unicos[i:i + 2]]
        for i in range(0, len(unicos), 2)
    ]
    filas.append([{'text': '◀ Volver', 'callback_data': 'menu:principal'}])
    return filas


def responder_a_callback(data: str):
    """Traduce el botón tocado a (texto, teclado). None = no se hace nada."""
    data = (data or '').strip()
    if data == 'menu:principal':
        return _AYUDA, _TECLADO_PRINCIPAL
    if data == 'menu:mas':
        return 'Otras consultas:', _TECLADO_MAS
    if data == 'menu:farmacias':
        return 'Elegí una farmacia (o escribí /farmacia CODIGO):', _teclado_farmacias()
    if data.startswith('farm:'):
        return _comando_farmacia(data.split(':', 1)[1]), _teclado_farmacias()
    if data.startswith('cmd:'):
        # Lista explícita y no "lo que sea que venga después de cmd:": un callback_data
        # solo puede venir de un botón que armó este mismo bot, así que cualquier otra
        # cosa es un botón viejo o un valor manipulado. A eso no se le contesta con la
        # ayuda —sería regalarle la lista de comandos a quien está probando—, se ignora.
        comando = data.split(':', 1)[1]
        if comando in _COMANDOS_CON_BOTON:
            # Se vuelve al mismo submenú desde el que se llegó: mandar al principal
            # obligaría a entrar a "Más" de nuevo para la consulta siguiente.
            teclado = _TECLADO_MAS if comando in _COMANDOS_DEL_SUBMENU else _TECLADO_PRINCIPAL
            return responder_a('/' + comando), teclado
    return None


def chat_autorizado(chat_id) -> bool:
    autorizados = [str(c).strip() for c in getattr(settings, 'TELEGRAM_CHAT_IDS_AUTORIZADOS', [])]
    return str(chat_id) in autorizados


def responder_a(texto: str) -> str | None:
    """Qué contestar a un mensaje. None = no se contesta nada.

    Separado del transporte para poder probar cada comando sin red ni bot.
    """
    if not texto:
        return None
    partes = texto.strip().split(maxsplit=1)
    # "/enlaces@saidsoftbot" es lo que llega cuando el bot está en un grupo.
    comando = partes[0].lower().split('@')[0]
    argumento = partes[1] if len(partes) > 1 else ''

    if comando in ('/start', '/ayuda', '/help'):
        return _AYUDA
    if comando == '/enlaces':
        return _comando_enlaces()
    if comando == '/estado':
        return _comando_estado()
    if comando == '/alertas':
        return _comando_alertas()
    if comando == '/criticas':
        return _comando_alertas(solo_criticas=True)
    if comando == '/mantenimiento':
        return _comando_mantenimiento()
    if comando == '/toperrores':
        return _comando_toperrores()
    if comando == '/farmacia':
        return _comando_farmacia(argumento)
    if comando.startswith('/'):
        return f'No conozco {comando}.\n\n{_AYUDA}'
    return None  # texto suelto: el bot no conversa, solo responde comandos


def procesar_actualizacion(update: dict) -> bool:
    """Atiende un update de Telegram. Devuelve si se respondió algo.

    Nunca lanza: un mensaje raro (una foto, un sticker, un comando con un código que no
    existe) no puede tumbar el bucle del bot y dejar el bot mudo hasta que alguien mire.
    """
    from .services import _enviar_telegram

    try:
        callback = update.get('callback_query')
        if callback:
            return _procesar_boton(callback)

        mensaje = update.get('message') or update.get('edited_message') or {}
        chat_id = (mensaje.get('chat') or {}).get('id')
        texto = mensaje.get('text') or ''
        if chat_id is None:
            return False
        if not chat_autorizado(chat_id):
            # Sin respuesta: contestar "no autorizado" ya confirma que el bot es de algo
            # real y que responde. Queda en el log para poder detectar el sondeo.
            logger.warning('Telegram: consulta de un chat no autorizado (%s).', chat_id)
            return False
        respuesta = responder_a(texto)
        if respuesta is None:
            return False
        return _enviar_telegram(chat_id, respuesta, teclado=_TECLADO_PRINCIPAL)
    except Exception:
        logger.exception('Telegram: error procesando una consulta.')
        return False


def _procesar_boton(callback: dict) -> bool:
    """Atiende un botón del teclado inline.

    `answerCallbackQuery` va SIEMPRE y primero, incluso si no se va a responder nada:
    hasta que Telegram lo recibe, el botón le queda al operador con un reloj girando y
    parece que el bot se colgó. Es la diferencia entre "no tenés permiso" y "esto no
    anda".
    """
    from .services import _enviar_telegram, llamar_telegram

    chat_id = ((callback.get('message') or {}).get('chat') or {}).get('id')
    if callback.get('id'):
        llamar_telegram('answerCallbackQuery', {'callback_query_id': callback['id']})
    if chat_id is None or not chat_autorizado(chat_id):
        logger.warning('Telegram: botón pulsado desde un chat no autorizado (%s).', chat_id)
        return False

    resultado = responder_a_callback(callback.get('data'))
    if resultado is None:
        return False
    texto, teclado = resultado
    # Mensaje nuevo en vez de editar el anterior: así queda el historial de lo que se
    # consultó y a qué hora, que es lo que después se copia a un ticket.
    return _enviar_telegram(chat_id, texto, teclado=teclado)
