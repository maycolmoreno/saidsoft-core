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


def _texto_duracion(desde) -> str:
    """"3 h 12 min" en vez de "192 minutos": es lo que se lee de un vistazo."""
    if desde is None:
        return 'sin dato'
    minutos = int((timezone.now() - desde).total_seconds() // 60)
    if minutos < 60:
        return f'{minutos} min'
    horas, resto = divmod(minutos, 60)
    if horas < 24:
        return f'{horas} h {resto} min'
    dias, horas = divmod(horas, 24)
    return f'{dias} d {horas} h'


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


def _comando_alertas() -> str:
    from .models import Alerta, ReglaAlerta

    abiertas = list(
        Alerta.objects.filter(estado__in=[Alerta.Estado.ABIERTA, Alerta.Estado.RECONOCIDA])
        .select_related('regla', 'estacion').order_by('-abierta_en')
    )
    if not abiertas:
        return '✅ No hay alertas abiertas.'

    lineas = [f'🔔 {len(abiertas)} alerta(s) sin resolver', '']
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
    '/farmacia ML016 — detalle de una farmacia',
    '',
    'Solo lectura: nada de esto modifica el sistema.',
])


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
        return _enviar_telegram(chat_id, respuesta)
    except Exception:
        logger.exception('Telegram: error procesando una consulta.')
        return False
