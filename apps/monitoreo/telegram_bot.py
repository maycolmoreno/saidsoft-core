"""Consultas por Telegram (/enlaces, /estado, /alertas, /farmacia, /hora) y dos
acciones: /reconocer y /sincronizar.

Complementa la notificación saliente (ver `notificar_alerta` y
`notificar_cambios_enlaces`): eso avisa cuando algo pasa, esto responde cuando alguien
pregunta.

**Por qué hay acciones, si esto nació de solo lectura.** La regla original era que
nada de acá modificara nada, porque el canal de entrada de un bot público no es el lugar
para accionar sobre 1.800 equipos. Sigue valiendo, y cada excepción se aprueba sola
contra cuatro criterios: una estación, idempotente, acción única y obvia, y que sea lo
que la alerta que llegó por este mismo chat está pidiendo.

- `/sincronizar` corrige el reloj de UNA estación. Sin esto el aviso llega al teléfono y
  obliga a abrir la computadora para un clic.
- `/reconocer` no arregla nada: cambia una fila. Pero corta el reenvío por escalamiento
  (`escalar_alertas_abiertas` solo mira las que siguen en ABIERTA), así que deja decir
  "ya la vi, estoy en eso" a las 3 de la mañana. Es la acción de menor riesgo del
  sistema y la que más cambia cómo se vive una guardia.

Lo que NO está y se decidió que no esté: reiniciar una estación (el radio de daño es un
cliente en el mostrador), ver la clave de BitLocker (un secreto por un canal que no
controlamos), aprobar enrolamientos, y **resolver** una alerta — que es distinto de
reconocerla: afirma "esto ya está arreglado" y eso no se verifica desde un teléfono.
Cualquier acción nueva se discute contra los mismos cuatro criterios; ninguna hereda
este permiso.

**Dos autorizaciones distintas, y conviene no confundirlas.**

*Consultar* se gobierna con `TELEGRAM_CHAT_IDS_AUTORIZADOS`, una lista en el .env. Un
bot de Telegram es público: cualquiera que adivine su nombre de usuario puede
escribirle. A un chat que no está en la lista no se le contesta nada, ni siquiera "no
autorizado" — confirmar que el bot existe y a qué responde ya es información. Lo que
devuelven estos comandos (códigos de farmacia, IPs de routers, qué está caído y desde
cuándo) es exactamente el mapa que alguien necesitaría para atacar la red.

*Accionar* exige además que el chat esté atado a un usuario del panel
(`PerfilUsuario.telegram_chat_id`), y se resuelve con el RBAC que ya existe: el permiso
que esa misma acción exige en el panel (`scripts.add_ejecucionscript` para sincronizar,
`monitoreo.change_alerta` para reconocer) más acceso a la unidad de negocio. Una lista
de chats no alcanza para escribir: no dice quién es cada uno, así que el historial no
podría nombrar a nadie y revocarle la acción a una persona le quitaría también la
consulta. Con el vínculo, `EjecucionScript.creado_por` queda con una persona real y dar
de baja el usuario en Django le apaga el Telegram.

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

# Ventana de /toperrores. Siete días cubre la semana operativa: lo bastante largo para
# que un error de los fines de semana no desaparezca, y lo bastante corto para que algo
# arreglado hace tres semanas deje de encabezar el ranking.
# Valor por defecto. La fuente real es ConfiguracionMonitoreo (editable en el admin).
_DIAS_TOPERRORES = 7


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
    """El "¿cómo está todo?" de un vistazo.

    Los números salen de `resumen_operacion`, el mismo servicio que alimenta el Centro de
    Monitoreo del panel. Hasta el 19-sep-2026 esta función tenía su propia copia de las
    agregaciones: dos lugares calculando "cuántos enlaces están caídos" es una garantía
    de que algún día digan cosas distintas, y el que se equivoca es siempre el que nadie
    mira.

    Sin filtrar por unidad de negocio: el chat autorizado es el equipo interno, que ve
    todo. El scope por cliente lo aplica el panel, que sí tiene un usuario detrás.
    """
    from .services import resumen_operacion

    r = resumen_operacion()
    frescura = _texto_duracion(r['ultimo_sondeo_red']) if r['ultimo_sondeo_red'] else 'sin datos'
    pos_caidos = r['pos_criticos'] + r['pos_no_criticos']

    return '\n'.join([
        '📊 Estado de SAIDSOFT',
        '',
        f"Estaciones: {r['estaciones_en_linea']}/{r['estaciones_total']} en línea",
        f"Alertas abiertas: {r['alertas_criticas']} crítica(s), {r['alertas_advertencias']} advertencia(s)",
        f"Enlaces caídos: {r['enlaces_caidos']}",
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
            f'{emoji} #{alerta.pk} {alerta.estacion.codigo} — {alerta.regla.nombre}{marca}\n'
            f'    hace {_texto_duracion(alerta.abierta_en)}'
        )
    if len(abiertas) > _MAX_FILAS:
        lineas.append(f'… y {len(abiertas) - _MAX_FILAS} más')
    if any(a.estado == Alerta.Estado.ABIERTA for a in abiertas):
        lineas += ['', 'Para avisar que la estás mirando: /reconocer <número>']
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

    **Solo lo visto en los últimos `_DIAS_TOPERRORES` días.** `PosErrorDetectado` es un
    contador de por vida, así que sin corte un problema ya resuelto sigue encabezando el
    ranking para siempre: el 18-sep-2026, 19 de los 42 mensajes acumulados eran viejos, y
    el segundo puesto lo ocupaba un `3D000: no existe la base de datos "TRX004"` de
    ML027-ADM del 27 de agosto, arreglado hacía tres semanas. Un ranking que muestra lo
    que pasó alguna vez, en vez de lo que está pasando, manda a revisar cosas que ya no
    existen.

    El número que se muestra sigue siendo el acumulado histórico de ese mensaje —el
    modelo no guarda el desglose por día, así que no se puede recortar— pero solo
    aparecen los que se vieron dentro de la ventana. El encabezado lo dice para que
    nadie lea "x500" como "500 veces esta semana".
    """
    from datetime import timedelta

    from django.db.models import Count, Max, Sum

    from .models import ConfiguracionMonitoreo, PosErrorDetectado

    dias = ConfiguracionMonitoreo.obtener().dias_ventana_top_errores
    desde = timezone.now() - timedelta(days=dias)
    ranking = list(
        PosErrorDetectado.objects
        .exclude(categoria=PosErrorDetectado.Categoria.NEGOCIO)
        .filter(ultima_vez__gte=desde)
        .values('mensaje')
        .annotate(
            total=Sum('cantidad_total'),
            estaciones=Count('estacion', distinct=True),
            visto=Max('ultima_vez'),
        )
        .order_by('-total')[:_MAX_FILAS]
    )
    if not ranking:
        return f'✅ Sin errores de sistema del POS en los últimos {dias} días.'

    lineas = [f'🐞 Errores del POS de los últimos {dias} días (toda la flota)', '']
    for fila in ranking:
        mensaje = fila['mensaje'][:110]
        lineas.append(
            f'  x{fila["total"]} · {fila["estaciones"]} estación(es) · '
            f'visto hace {_texto_duracion(fila["visto"])}\n    {mensaje}'
        )
    lineas += [
        '',
        'El total es histórico del mensaje; la ventana filtra qué sigue apareciendo.',
        'No se cuentan las validaciones de negocio (ej. VENTA SIN LOTE).',
    ]
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
    '/hora — estaciones con el reloj corrido',
    '',
    'Acciones:',
    '',
    '/reconocer 42 — avisá que estás mirando esa alerta y corta el escalamiento',
    '/sincronizar ML002-B — corrige el reloj de esa estación (pide confirmación)',
    '',
    'Todo lo demás es solo lectura. La acción se ejecuta con TUS permisos y queda a tu '
    'nombre en el historial, así que tu chat tiene que estar atado a tu usuario del panel.',
])


# --- Reconocer una alerta ---
    #
# Segunda accion de escritura del bot, y la que menos hace: cambia una fila y no toca
# ninguna caja. Justamente por eso es la de mejor relacion valor/riesgo.
    #
# Lo que gana es el escalamiento: `escalar_alertas_abiertas` solo reenvia las que
# siguen en ABIERTA, asi que reconocer corta la insistencia. A las 3 de la manana eso
# es la diferencia entre decir "ya la vi, estoy en eso" desde el telefono y tener que
# abrir la computadora — que es exactamente el momento en que nadie la abre.
    #
# Reconocer NO es resolver, y el bot deliberadamente no ofrece lo segundo: resolver
# afirma "esto ya esta arreglado", y eso no se puede verificar desde un telefono.

def _pedir_confirmacion_reconocer(alerta_id):
    """Paso intermedio, igual que /sincronizar: un toque no alcanza para accionar."""
    from .models import Alerta, ReglaAlerta

    try:
        alerta = (
            Alerta.objects.select_related('regla', 'estacion', 'estacion__farmacia')
            .get(pk=int(alerta_id))
        )
    except (Alerta.DoesNotExist, TypeError, ValueError):
        return f'No encuentro la alerta #{alerta_id}.', _TECLADO_PRINCIPAL

    if alerta.estado != Alerta.Estado.ABIERTA:
        # Ya la reconocio alguien, o se resolvio sola porque el servicio volvio. Decirlo
        # evita que dos personas salgan a atender lo mismo, que es justo lo que reconocer
        # viene a resolver.
        quien = alerta.reconocida_por.username if alerta.reconocida_por_id else 'el sistema'
        return (
            f'La alerta #{alerta.pk} ya no esta abierta ({alerta.get_estado_display()}).\n'
            f'La atendio {quien}.'
        ), _TECLADO_PRINCIPAL

    emoji = '🔴' if alerta.regla.severidad == ReglaAlerta.Severidad.CRITICAL else '🟡'
    texto = (
        f'{emoji} Reconocer la alerta #{alerta.pk}\n\n'
        f'{alerta.estacion.codigo} ({alerta.estacion.farmacia.codigo})\n'
        f'{alerta.regla.nombre}\n'
        f'Abierta hace {_texto_duracion(alerta.abierta_en)}.\n\n'
        'Reconocer NO la resuelve: deja constancia de que alguien la esta mirando y corta '
        'el reenvio por escalamiento. Queda a tu nombre.'
    )
    teclado = [[
        {'text': f'Reconocer #{alerta.pk}', 'callback_data': f'ack:{alerta.pk}'},
        {'text': 'Cancelar', 'callback_data': 'menu:principal'},
    ]]
    return texto, teclado


def _reconocer_alerta(alerta_id, chat_id) -> str:
    """Marca la alerta como RECONOCIDA a nombre de la persona atada a este chat.

    Mismos tres controles que `_ejecutar_sincronizar`, y el mismo permiso que exige el
    panel (`monitoreo.change_alerta`): no se inventa un criterio nuevo para la misma
    accion segun por donde entre.
    """
    from apps.auditoria.models import registrar_evento
    from apps.cuentas.services import usuario_de_chat_telegram, usuario_puede_ver

    from .models import Alerta

    usuario = usuario_de_chat_telegram(chat_id)
    if usuario is None:
        return (
            'Este chat puede consultar, pero no accionar.\n\n'
            'Para habilitarlo, un administrador tiene que poner tu chat_id '
            f'({chat_id}) en tu perfil de usuario del panel. Asi la alerta queda '
            'reconocida a tu nombre y no a nombre de nadie.'
        )
    if not usuario.has_perm('monitoreo.change_alerta'):
        return f'{usuario.username}: no tenes permiso para reconocer alertas.'

    try:
        alerta = (
            Alerta.objects.select_related('regla', 'estacion', 'estacion__farmacia__unidad_negocio')
            .get(pk=int(alerta_id))
        )
    except (Alerta.DoesNotExist, TypeError, ValueError):
        return f'No encuentro la alerta #{alerta_id}.'

    unidad = alerta.estacion.farmacia.unidad_negocio
    if not usuario_puede_ver(usuario, unidad):
        return f'{usuario.username}: no tenes acceso a {unidad.codigo}.'

    if alerta.estado != Alerta.Estado.ABIERTA:
        quien = alerta.reconocida_por.username if alerta.reconocida_por_id else 'el sistema'
        return f'La alerta #{alerta.pk} ya no estaba abierta: la atendio {quien}.'

    alerta.estado = Alerta.Estado.RECONOCIDA
    alerta.reconocida_en = timezone.now()
    alerta.reconocida_por = usuario
    alerta.save(update_fields=['estado', 'reconocida_en', 'reconocida_por'])
# Sin `request`: no hay uno. La IP queda vacia y el origen va en el detalle, que es
# el dato que de verdad importa para entender de donde salio la accion.
    registrar_evento(
        usuario=usuario, accion='alerta.reconocer', objeto=alerta,
        detalle={'origen': 'telegram'},
    )
    logger.info('Telegram: %s reconocio la alerta #%s.', usuario.username, alerta.pk)
    return (
        f'✔ Alerta #{alerta.pk} reconocida a nombre de {usuario.username}.\n\n'
        f'{alerta.estacion.codigo} — {alerta.regla.nombre}\n\n'
        'Deja de reenviarse por escalamiento. Sigue abierta hasta que se resuelva: '
        'la mayoria se cierra sola cuando el servicio vuelve.'
    )


# --- Reloj: consultar y corregir ---
#
# `/sincronizar` es la PRIMERA accion de escritura de este bot, que hasta aca era solo
# de lectura por decision explicita (ver el docstring del modulo). Se abre esta sola, y
# no "ejecutar cualquier script", porque el caso lo justifica y el radio de daño es
# minimo: corrige el reloj de UNA estacion, es idempotente, y es exactamente lo que la
# alerta que llega por este mismo chat te esta pidiendo que hagas. Cualquier otra accion
# tiene que volver a discutirse, no heredar este permiso.

def _comando_hora() -> str:
    """Estaciones con el reloj corrido, la peor primero. Solo lectura."""
    from apps.catalogo.models import Estacion

    estaciones = [
        e for e in Estacion.objects.filter(estado_aprobacion='aprobada').select_related('farmacia')
        if e.desfase_reloj_segundos is not None and e.reloj_desincronizado
    ]
    estaciones.sort(key=lambda e: abs(e.desfase_reloj_segundos), reverse=True)

    if not estaciones:
        return (
            'Ninguna estacion con el reloj corrido.\n'
            f'(Se lista a partir de {Estacion.UMBRAL_RELOJ_AVISO_SEGUNDOS} s de desfase.)'
        )

    lineas = ['Relojes corridos:', '']
    for e in estaciones[:_MAX_FILAS]:
        seg = e.desfase_reloj_segundos
        direccion = 'adelantada' if seg > 0 else 'atrasada'
        if e.reloj_incomunicado:
            estado = 'INCOMUNICADA: descarta comandos, hay que ir al local'
        else:
            margen = Estacion.UMBRAL_RELOJ_INCOMUNICADO_SEGUNDOS - abs(seg)
            estado = f'quedan {margen} s de margen, se arregla en remoto'
        lineas.append(f'{e.codigo} ({e.farmacia.codigo}) {abs(seg)} s {direccion}')
        lineas.append(f'   {estado}')
    if len(estaciones) > _MAX_FILAS:
        lineas.append(f'... y {len(estaciones) - _MAX_FILAS} mas.')
    lineas.append('')
    lineas.append('Para corregir una: /sincronizar CODIGO')
    return '\n'.join(lineas)


def _buscar_estacion(codigo: str):
    from apps.catalogo.models import Estacion

    return Estacion.objects.select_related('farmacia__unidad_negocio').filter(
        codigo__iexact=(codigo or '').strip(), estado_aprobacion='aprobada',
    ).first()


def _pedir_confirmacion_sincronizar(codigo: str):
    """Paso intermedio: que un toque no alcance para accionar sobre una estacion."""
    from apps.catalogo.models import Estacion

    estacion = _buscar_estacion(codigo)
    if estacion is None:
        # Teclado principal y no None: que un codigo mal escrito no deje al operador sin
        # botones, teniendo que escribir el comando siguiente a mano.
        return f'No encuentro una estacion aprobada con codigo "{codigo}".', _TECLADO_PRINCIPAL

    seg = estacion.desfase_reloj_segundos
    if seg is None:
        detalle = 'Todavia no reporto su reloj.'
    else:
        direccion = 'adelantada' if seg > 0 else 'atrasada'
        detalle = f'Hoy esta {abs(seg)} s {direccion}.'

    aviso = ''
    if estacion.reloj_incomunicado:
        # No se bloquea el intento, se avisa: el desfase se midio en el ultimo latido y
        # pudo haber cambiado. Pero que nadie crea que quedo resuelto si el agente lo
        # descarta en silencio, que es justo como se pierde una tarde.
        aviso = (
            f'\n\nOJO: con mas de {Estacion.UMBRAL_RELOJ_INCOMUNICADO_SEGUNDOS} s de desfase '
            'el agente DESCARTA todo mensaje firmado, incluido este script. Lo mas probable '
            'es que no llegue y haya que ir al local.'
        )

    texto = (
        f'Sincronizar el reloj de {estacion.codigo} ({estacion.farmacia.codigo}).\n'
        f'{detalle}\n\n'
        'Se le va a ejecutar el script "Sincronizar hora con el dominio": corrige la zona '
        f'horaria si hace falta y sincroniza contra el dominio.{aviso}'
    )
    teclado = [[
        {'text': f'Confirmar {estacion.codigo}', 'callback_data': f'sync:{estacion.codigo}'},
        {'text': 'Cancelar', 'callback_data': 'menu:principal'},
    ]]
    return texto, teclado


def _ejecutar_sincronizar(codigo: str, chat_id) -> str:
    """Dispara el script de hora sobre una estacion, con los permisos de la PERSONA
    atada a este chat — no con los del bot.

    Tres controles, los mismos que aplica el panel y por las mismas razones:
      1. el chat tiene que estar atado a un usuario activo (si no, el historial no
         podria decir quien lo hizo);
      2. ese usuario necesita el permiso de ejecutar scripts;
      3. y acceso a la unidad de negocio de esa estacion.
    """
    from apps.cuentas.services import usuario_de_chat_telegram, usuario_puede_ver
    from apps.scripts.management.commands.seed_scripts_hora import NOMBRE_SINCRONIZAR
    from apps.scripts.models import EjecucionScript, Script
    from apps.scripts.services import registrar_ejecucion_script

    usuario = usuario_de_chat_telegram(chat_id)
    if usuario is None:
        return (
            'Este chat puede consultar, pero no accionar.\n\n'
            'Para habilitarlo, un administrador tiene que poner tu chat_id '
            f'({chat_id}) en tu perfil de usuario del panel. Asi la ejecucion queda '
            'a tu nombre y con tus permisos.'
        )
    if not usuario.has_perm('scripts.add_ejecucionscript'):
        return f'{usuario.username}: no tenes permiso para ejecutar scripts.'

    estacion = _buscar_estacion(codigo)
    if estacion is None:
        return f'No encuentro una estacion aprobada con codigo "{codigo}".'

    unidad = estacion.farmacia.unidad_negocio
    if not usuario_puede_ver(usuario, unidad):
        return f'{usuario.username}: no tenes acceso a {unidad.codigo}.'

    script = Script.objects.filter(nombre=NOMBRE_SINCRONIZAR, unidad_negocio__isnull=True).first()
    if script is None:
        return (
            f'Falta el script "{NOMBRE_SINCRONIZAR}" en la biblioteca.\n'
            'Se crea con: manage.py seed_scripts_hora'
        )

    ejecucion = registrar_ejecucion_script(
        script=script, destino_tipo=EjecucionScript.DestinoTipo.ESTACIONES,
        usuario=usuario, unidad_negocio=unidad, estaciones=[estacion],
    )
    logger.info(
        'Telegram: %s disparo la sincronizacion de hora de %s (ejecucion #%s).',
        usuario.username, estacion.codigo, ejecucion.pk,
    )
    return (
        f'Enviado a {estacion.codigo}: ejecucion #{ejecucion.pk}, a nombre de {usuario.username}.\n\n'
        'El agente lo aplica y reporta el resultado. Volve a mirar con /hora en un par de minutos: '
        'el desfase se recalcula con el siguiente latido.'
    )


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
    [{'text': '🐞 Top errores POS', 'callback_data': 'cmd:toperrores'},
     {'text': '🕐 Relojes', 'callback_data': 'cmd:hora'}],
    [{'text': '◀ Volver', 'callback_data': 'menu:principal'}],
]

# Cuántas farmacias entran en el submenú. Telegram deja mandar más, pero una pared de
# botones en el teléfono se vuelve peor que escribir el código.
_MAX_BOTONES_FARMACIA = 8

# Cuáles viven detrás de "Más", para devolver al operador al submenú correcto.
_COMANDOS_DEL_SUBMENU = frozenset({'criticas', 'mantenimiento', 'toperrores', 'hora'})

# Qué comandos puede disparar un botón. Se declara aparte de los que acepta `responder_a`
# porque no son lo mismo: `/farmacia` necesita un argumento y por eso tiene su submenú.
#
# `sincronizar` NO está acá, y la omisión es deliberada: es el único comando que escribe.
# Su botón no sale de esta lista genérica sino de `pedirsync:`/`sync:`, que llevan el
# código de la estación adentro y pasan por una confirmación explícita.
_COMANDOS_CON_BOTON = frozenset({'enlaces', 'estado', 'alertas', 'criticas',
                                 'mantenimiento', 'toperrores', 'hora'})


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


def responder_a_callback(data: str, chat_id=None):
    """Traduce el botón tocado a (texto, teclado). None = no se hace nada.

    `chat_id` identifica a la persona para los botones que ACCIONAN (`sync:`); los de
    consulta lo ignoran.
    """
    data = (data or '').strip()
    if data == 'menu:principal':
        return _AYUDA, _TECLADO_PRINCIPAL
    if data.startswith('pedirack:'):
        # Viene con la notificacion de la alerta: pide confirmacion, no reconoce.
        return _pedir_confirmacion_reconocer(data.split(':', 1)[1])
    if data.startswith('ack:'):
        return _reconocer_alerta(data.split(':', 1)[1], chat_id), _TECLADO_PRINCIPAL
    if data.startswith('pedirsync:'):
        # Botón que viene con la alerta de reloj: no ejecuta, pide confirmación. Que
        # llegue un aviso al teléfono y un roce accidental accione sobre una caja no
        # puede ser el diseño.
        return _pedir_confirmacion_sincronizar(data.split(':', 1)[1])
    if data.startswith('sync:'):
        return _ejecutar_sincronizar(data.split(':', 1)[1], chat_id), _TECLADO_PRINCIPAL
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


def responder_a(texto: str, chat_id=None):
    """Qué contestar a un mensaje. None = no se contesta nada.

    Devuelve un str, o una tupla `(texto, teclado)` cuando la respuesta necesita su
    propio teclado en vez del principal — hoy solo la confirmación de `/sincronizar`,
    que tiene que ofrecer "Confirmar" y "Cancelar" y nada más.

    `chat_id` solo hace falta para los comandos que ACCIONAN: es con lo que se resuelve
    qué persona está del otro lado (ver `usuario_de_chat_telegram`). Las consultas no lo
    usan, y por eso sigue siendo opcional: se pueden probar sin inventar un chat.

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
    if comando == '/reconocer':
        if not argumento.strip():
            return 'Decime cuál: /reconocer 42 (el número sale de /alertas).'
        return _pedir_confirmacion_reconocer(argumento.strip().lstrip('#'))
    if comando == '/hora':
        return _comando_hora()
    if comando == '/sincronizar':
        if not argumento.strip():
            return 'Decime cuál: /sincronizar CODIGO (mirá /hora para ver cuáles están corridas).'
        # No ejecuta: devuelve la confirmación. Escribir el comando es el primer paso,
        # tocar "Confirmar" es el segundo — accionar sobre una estación no puede salir
        # de un solo tipeo.
        return _pedir_confirmacion_sincronizar(argumento)
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
        respuesta = responder_a(texto, chat_id)
        if respuesta is None:
            return False
        # Un comando puede pedir su propio teclado (la confirmación de /sincronizar);
        # el resto sigue con el principal.
        if isinstance(respuesta, tuple):
            respuesta, teclado = respuesta
        else:
            teclado = _TECLADO_PRINCIPAL
        return _enviar_telegram(chat_id, respuesta, teclado=teclado)
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

    resultado = responder_a_callback(callback.get('data'), chat_id)
    if resultado is None:
        return False
    texto, teclado = resultado
    # Mensaje nuevo en vez de editar el anterior: así queda el historial de lo que se
    # consultó y a qué hora, que es lo que después se copia a un ticket.
    return _enviar_telegram(chat_id, texto, teclado=teclado)
