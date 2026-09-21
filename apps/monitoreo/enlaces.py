"""Sondeo ICMP del enlace de cada farmacia, y registro de sus caídas.

Por qué existe: todo el monitoreo de este proyecto depende de que la farmacia tenga un
**agente instalado**, y eso cubre 8 de ~1.800 estaciones. Un ping al equipo de borde no
necesita nada instalado del otro lado, así que cubre las ~704 sucursales desde el día
uno. Es la capacidad que tenía `Cresio_enlaces`, el sistema anterior, que estuvo vivo
hasta el 4-sep-2026 sondeando la flota completa (ver `docs/evaluacion-cresio-enlaces.md`).

**Dónde corre esto (verificado en el servidor real el 11-sep-2026):**

Este módulo nació asumiendo que el servidor central no tenía ruta hacia las farmacias —
así lo afirman `CLAUDE.md` y el docstring de
`apps.monitoreo.mikrotik.sincronizar_ancho_banda_farmacias` desde el 24-ago-2026 ("100%
de pérdida de ping, sin entrada en la tabla de rutas"). **Eso ya no es cierto.**
Comprobado sobre el NUC de producción:

- Desde el host: 19 de 25 gateways de San Gregorio responden al ping (las 6 que no son
  caídas reales o sitios de baja — si no hubiera ruta fallarían las 25).
- Desde adentro del contenedor de Celery: `connect()` TCP a esas mismas IP devuelve
  **ConnectionRefused**, que prueba que el paquete llegó al destino y volvió.

El ICMP fallaba en el contenedor por un motivo distinto y engañoso: la imagen no traía
el binario `ping` (corregido en `deploy/Dockerfile`, y `verificar_ping_disponible` abajo
ahora lo detecta y lo dice en vez de reportar la flota entera como caída).

Por eso **sí** se programa en `CELERY_BEAT_SCHEDULE`. Sigue existiendo el comando
(`python manage.py sondear_enlaces`) para correrlo desde otro host, y la API de ingesta
(`apps.monitoreo.api_views`) para una sonda externa: las tres vías comparten
`registrar_sondeo`, así que cuál se use es una decisión de operación, no de diseño.

La guarda de `sondear_enlaces_farmacias` es la red de seguridad de todo esto: si casi
todo el barrido falla, no registra nada. 704 farmacias no se caen a la vez; lo que se
cayó es la ruta desde donde se está sondeando. Con el sondeo ya programado, esa guarda
es lo que hace que un cambio de red no llene la base de caídas falsas.
"""
import logging
import platform
import re
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor

from django.utils import timezone

logger = logging.getLogger(__name__)

# Ping del sistema en vez de una librería ICMP: `pythonping`/`icmplib` arman paquetes
# ICMP crudos, que en Linux exigen root o CAP_NET_RAW. El binario del sistema ya viene
# con el bit setuid resuelto por el SO, así que esto corre sin privilegios especiales
# tanto en el contenedor como en una máquina Windows de la oficina.
TIMEOUT_SEGUNDOS = 2
MAX_SONDEOS_CONCURRENTES = 50

# Paquetes por sondeo. UNO SOLO NO ALCANZA: medido contra la flota real el 11-sep-2026,
# la misma IP responde en un sondeo y falla en el siguiente pocos segundos después
# (192.168.11.1 lo hizo dos veces seguidas). Estos son enlaces WAN de farmacia, no una
# LAN: un paquete perdido es normal y no significa que el sitio esté caído. Con 3, se
# cuenta alcanzable si contesta CUALQUIERA — que es lo que `ping` ya refleja en su
# código de salida. Las caídas reales siguen detectándose: las que se comprobaron caídas
# perdieron los 4 paquetes de 4, no 1 de 4.
PAQUETES_POR_SONDEO = 3

# Si este porcentaje del barrido o más falla, se asume que el problema es la ruta desde
# donde se sondea, no 704 caídas simultáneas. Ver el docstring del módulo.
UMBRAL_BARRIDO_SOSPECHOSO_PCT = 80

# La unidad se pide como `m` y no `ms` a propósito: Windows en español imprime
# "tiempo<1m" (sin la s) cuando la respuesta es submilisegundo, y exigir "ms" hacía que
# esos sondeos se registraran como vivos pero sin latencia. Encontrado corriéndolo de
# verdad, no leyendo la documentación de ping.
_RE_LATENCIA = re.compile(r'(?:time|tiempo)[=<]\s*([\d.,]+)\s*m', re.IGNORECASE)


class PingNoDisponible(RuntimeError):
    """No hay binario `ping` en este host.

    Tiene excepción propia porque el síntoma es engañoso: `subprocess.run` de un binario
    inexistente levanta FileNotFoundError, que el sondeo trataría como "no respondió", y
    el barrido entero se vería idéntico a una flota caída o a una ruta rota. Pasó de
    verdad — la imagen de producción no traía `iputils-ping` (ver deploy/Dockerfile).
    """


def verificar_ping_disponible():
    """Levanta PingNoDisponible si falta el binario. Se llama UNA vez por barrido, no por
    farmacia: es una propiedad del host, no de cada sondeo."""
    if shutil.which('ping') is None:
        raise PingNoDisponible(
            'No hay binario `ping` en este host. En el contenedor se instala con el paquete '
            'iputils-ping (ver deploy/Dockerfile); en Windows y en la mayoría de las distros '
            'viene de fábrica. Sin él, el sondeo reportaría toda la flota como caída.',
        )


def sondear_enlace(ip: str, timeout=TIMEOUT_SEGUNDOS) -> tuple[bool, float | None]:
    """Un ping. Devuelve `(alcanzable, latencia_ms)`.

    `latencia_ms` es None si no respondió, o si respondió pero no se pudo parsear el
    tiempo (la salida de `ping` cambia con el idioma del SO — por eso el regex acepta
    "time=" y "tiempo="; si igual no calza, el enlace se cuenta como vivo sin latencia,
    que es mejor que descartar un sondeo bueno por un problema de locale).
    """
    if platform.system() == 'Windows':
        comando = ['ping', '-n', str(PAQUETES_POR_SONDEO), '-w', str(int(timeout * 1000)), ip]
    else:
        comando = ['ping', '-c', str(PAQUETES_POR_SONDEO), '-W', str(int(timeout)), ip]

    try:
        proceso = subprocess.run(
            # El timeout del proceso cubre el peor caso: todos los paquetes agotando su
            # espera, más margen para que `ping` imprima el resumen.
            comando, capture_output=True, text=True, timeout=timeout * PAQUETES_POR_SONDEO + 5,
            # El texto de `ping` viene en la codificación de la consola del SO, que en
            # Windows en español no es UTF-8. Sin errors='replace', una tilde en
            # "Tiempo de espera agotado" revienta el decode y pierde el sondeo entero.
            errors='replace',
        )
    except (subprocess.TimeoutExpired, OSError):
        return False, None

    if proceso.returncode != 0:
        return False, None

    # returncode 0 no alcanza en Windows: `ping` devuelve 0 aunque la respuesta sea
    # "Host de destino inaccesible", que es un ICMP de otro equipo, no del destino.
    salida = proceso.stdout or ''
    coincidencia = _RE_LATENCIA.search(salida)
    if coincidencia is None:
        if 'inaccesible' in salida.lower() or 'unreachable' in salida.lower():
            return False, None
        return True, None
    return True, float(coincidencia.group(1).replace(',', '.'))


def registrar_sondeo(farmacia, alcanzable: bool, latencia_ms: float | None):
    """Aplica el resultado de un sondeo al estado del enlace, abriendo o cerrando la
    caída si corresponde. Es el punto de entrada para CUALQUIER origen del dato —
    el comando de acá, un agente, o un probe externo por API el día que exista.

    La caída no se declara al primer fallo: un paquete ICMP se pierde por mil motivos.
    Se cuentan `UMBRAL_FALLAS_CONSECUTIVAS` sondeos fallidos seguidos, igual que hacía
    `Cresio_enlaces`. La recuperación sí es inmediata — si respondió, está viva.
    """
    from .models import ConfiguracionMonitoreo, EstadoEnlaceFarmacia, EventoEnlaceFarmacia

    ahora = timezone.now()
    umbral_fallas = ConfiguracionMonitoreo.obtener().fallas_consecutivas_enlace
    estado, _ = EstadoEnlaceFarmacia.objects.get_or_create(farmacia=farmacia)
    estaba_caida = estado.alcanzable is False

    if alcanzable:
        estado.fallas_consecutivas = 0
        estado.latencia_ms = latencia_ms
        estado.respondio_alguna_vez = True
        estado.primer_fallo = None
        nuevo_alcanzable = True
    else:
        if estado.fallas_consecutivas == 0:
            # Arranca la racha: este es el instante que despues va como inicio de la
            # caida, no el tercer fallo (ver EstadoEnlaceFarmacia.primer_fallo).
            estado.primer_fallo = ahora
        estado.fallas_consecutivas += 1
        estado.latencia_ms = None
        # Mientras no supere el umbral se conserva el estado anterior: null sigue siendo
        # null (nunca se sondeó) y una farmacia viva sigue viva. No se inventa un "caído"
        # con una sola pérdida de paquete.
        if estado.fallas_consecutivas >= umbral_fallas:
            nuevo_alcanzable = False
        else:
            nuevo_alcanzable = estado.alcanzable

    if nuevo_alcanzable != estado.alcanzable:
        estado.ultimo_cambio_estado = ahora
    estado.alcanzable = nuevo_alcanzable
    estado.ultima_verificacion = ahora
    estado.save()

    if nuevo_alcanzable is False and not estaba_caida:
        EventoEnlaceFarmacia.objects.create(
            farmacia=farmacia,
            # El inicio real es el primer fallo, no el tercero: si no, toda caída
            # aparecería más corta de lo que fue y el dato no serviría para un SLA.
            # `ultimo_cambio_estado` marca justamente el tercero, así que no sirve acá.
            inicio=estado.primer_fallo or ahora,
            circuito_proveedor=farmacia.circuito_proveedor,
        )
        logger.warning('Enlace CAÍDO en %s (circuito %s)', farmacia.codigo, farmacia.circuito_proveedor or '?')
    elif nuevo_alcanzable and estaba_caida:
        abierta = EventoEnlaceFarmacia.objects.filter(farmacia=farmacia, fin__isnull=True).order_by('-inicio').first()
        if abierta is not None:
            abierta.fin = ahora
            abierta.save(update_fields=['fin'])
            logger.info('Enlace RECUPERADO en %s tras %s min', farmacia.codigo, abierta.duracion_minutos)
    return estado


def sondear_enlaces_farmacias(farmacias=None) -> dict:
    """Sondea el enlace de cada farmacia con `ip_router` cargada. Devuelve un resumen.

    **No se programa en Celery Beat**: desde el servidor central no hay ruta a esas IP
    (ver el docstring del módulo). Se corre con `python manage.py sondear_enlaces` desde
    un host que sí la tenga.

    Si el barrido falla casi entero, **no registra nada** y devuelve el resumen con
    `abortado=True`. Esa es la diferencia entre una herramienta útil y uno que llena la
    base de 704 caídas falsas la primera vez que alguien lo corre desde el lugar
    equivocado.
    """
    from apps.catalogo.models import Farmacia

    if farmacias is None:
        # __isnull=True alcanza solo: GenericIPAddressField normaliza '' a None, y un
        # .exclude(ip_router='') encadenado excluye TODAS las filas (bug real ya
        # documentado en mikrotik.py).
        farmacias = list(Farmacia.objects.filter(activa=True).exclude(ip_router__isnull=True))
    else:
        farmacias = list(farmacias)

    resumen = {'sondeadas': 0, 'activas': 0, 'caidas': 0, 'abortado': False}
    if not farmacias:
        return resumen

    # Antes de medir nada: si falta el binario, el barrido entero daría "todo caído" y el
    # operador leería "se cayó la ruta" cuando en realidad falta un paquete del sistema.
    verificar_ping_disponible()

    with ThreadPoolExecutor(max_workers=MAX_SONDEOS_CONCURRENTES) as pool:
        resultados = list(pool.map(lambda f: (f, *sondear_enlace(str(f.ip_router))), farmacias))

    fallidos = sum(1 for _, alcanzable, _lat in resultados if not alcanzable)
    pct_fallido = 100 * fallidos / len(resultados)
    if pct_fallido >= UMBRAL_BARRIDO_SOSPECHOSO_PCT:
        logger.error(
            'Barrido de enlaces abortado: %.0f%% de %d farmacias no respondió. Eso no son caídas '
            'simultáneas, es que este host no tiene ruta hacia las IP de las farmacias. No se '
            'registró nada.', pct_fallido, len(resultados),
        )
        resumen.update(abortado=True, sondeadas=len(resultados), caidas=fallidos)
        return resumen

    for farmacia, alcanzable, latencia_ms in resultados:
        registrar_sondeo(farmacia, alcanzable, latencia_ms)
        resumen['sondeadas'] += 1
        resumen['activas' if alcanzable else 'caidas'] += 1
    return resumen


def nombre_proveedor(farmacia) -> str:
    """El ISP de una farmacia: TELCONET, PUNTO NET, FIBROMARK...

    Punto único: lo consumen el correo de caídas, la tabla de enlaces y el Centro de
    Monitoreo. Tres lugares diciendo "el proveedor" con tres criterios distintos es cómo
    se llega a que el panel y el correo se contradigan.

    **Sale de `tipo_enlace` y NO de `circuito_proveedor`, y la diferencia importa.** Hasta
    el 20-sep-2026 esto cortaba el circuito en el primer guion creyendo que ahí estaba el
    proveedor. No está: medido sobre las 700 farmacias, el circuito tiene la forma
    `cliente-sitio-ciudad` (`sangregorio-7deagosto-buenafe`), así que el primer segmento
    es la CADENA. El agrupado "por proveedor" venía devolviendo 164 bloques llamados
    `sangregorio`, `sangregorio61`, `sg278` — que para reclamarle a un proveedor no sirven.

    `tipo_enlace` sí es el ISP y está cargado en el 100% de las farmacias monitoreadas:
    TELCONET 579, PUNTO NET 109, FIBROMARK 5, ETAPA 4, y tres sueltas.
    """
    return (getattr(farmacia, 'tipo_enlace', '') or '').strip() or '(proveedor sin cargar)'


def _agrupar_por_proveedor(eventos):
    """Agrupa las caídas por ISP, para que el correo se lea como se abre el ticket: un
    reclamo por proveedor.

    Requiere que los eventos vengan con `select_related('farmacia')` — si no, esto hace
    una consulta por evento, y una tanda de caídas puede traer cientos.
    """
    grupos: dict[str, list] = {}
    for evento in eventos:
        grupos.setdefault(nombre_proveedor(evento.farmacia), []).append(evento)
    return dict(sorted(grupos.items()))


# --- Caídas simultáneas: circuito individual, corte del proveedor, o ceguera propia ---
#
# ESTO NO DISTINGUE CORTE DE LUZ DE CAÍDA DEL ENLACE. Se investigó el 20-sep-2026 y hoy
# no es posible: el ping externo y el heartbeat MQTT viajan por la misma conexión que se
# corta en los dos casos, los Mikrotik instalados (RB941-2nD, RB951Ui-2nD/2HnD) NO
# implementan el árbol de salud de MikroTik —comprobado contra GAT01: `sysName` responde
# y los cinco OID `mtxrHl*` devuelven "No Such Object"— y hay CERO UPS en el inventario.
# Sin un sensor que sobreviva al corte, o un canal fuera de banda, no hay señal que
# separar. Ver README.md / PLAN_MODERNIZACION.md.
#
# Lo que SÍ se puede responder con los datos que ya existen es otra pregunta, y resulta
# ser la que la mesa de ayuda necesita antes: **¿esto es un sitio, o es el proveedor?**
# De eso depende si se abre un ticket por farmacia o uno solo por el circuito troncal.

# Umbrales, medidos sobre los 1.108 eventos de los últimos 30 días (20-sep-2026):
#
# - VENTANA de 10 min: los racimos reales se forman en 4-22 min. Con 5 min se parten en
#   dos y con 30 se pegan cosas que no tienen que ver.
# - MÍNIMO de 3: dos farmacias cayendo juntas pasa por casualidad; de las 166 tandas con
#   2+ caídas, 162 (98%) comparten un solo proveedor, así que el agrupado discrimina.
# - PROVEEDORES_PARA_CEGUERA en 3: una tanda que toca tres ISP distintos a la vez no es
#   una coincidencia, es que el que dejó de ver fue el servidor. Pasó de verdad: 194
#   caídas en 8 minutos abarcando 6 proveedores y 39 ciudades — el 17% de TODOS los
#   eventos del mes en un solo episodio. Sin esta regla, esos 194 se reportan como 194
#   farmacias caídas y el número del mes queda inflado.
VENTANA_SIMULTANEAS_MINUTOS = 10
MINIMO_PARA_CORTE_DE_PROVEEDOR = 3
PROVEEDORES_PARA_CEGUERA = 3


def clasificar_caidas_simultaneas(eventos):
    """`{farmacia_id: (clase, detalle)}` para un conjunto de caídas abiertas.

    `clase` es una de:
      - `'proveedor'`  — varias del MISMO ISP cayeron juntas: probable corte del proveedor.
      - `'ceguera'`    — cayeron de varios ISP a la vez: lo más probable es que el que
                         perdió visibilidad sea este servidor, no las farmacias.
      - `'individual'` — nada alrededor: es ese sitio.

    Es una INFERENCIA, no una lectura. Nada de esto mide el estado del enlace ni de la
    energía: mira cuándo empezaron las caídas y de qué proveedor es cada una. Por eso las
    etiquetas dicen "probable" y no afirman una causa.

    Los eventos tienen que venir con `select_related('farmacia')`.
    """
    from datetime import timedelta

    ordenados = sorted(eventos, key=lambda e: e.inicio)
    ventana = timedelta(minutes=VENTANA_SIMULTANEAS_MINUTOS)

    racimos, actual = [], []
    for evento in ordenados:
        if actual and (evento.inicio - actual[-1].inicio) > ventana:
            racimos.append(actual)
            actual = []
        actual.append(evento)
    if actual:
        racimos.append(actual)

    clasificacion = {}
    for racimo in racimos:
        proveedores = {nombre_proveedor(e.farmacia) for e in racimo}
        if len(racimo) >= MINIMO_PARA_CORTE_DE_PROVEEDOR and len(proveedores) >= PROVEEDORES_PARA_CEGUERA:
            detalle = (
                f'{len(racimo)} sitios de {len(proveedores)} proveedores distintos cayeron '
                'en minutos. Que varios ISP fallen a la vez es muy improbable: revisá '
                'primero la conexión del servidor de monitoreo.'
            )
            clase = 'ceguera'
        elif len(racimo) >= MINIMO_PARA_CORTE_DE_PROVEEDOR and len(proveedores) == 1:
            proveedor = next(iter(proveedores))
            detalle = (
                f'{len(racimo)} sitios de {proveedor} cayeron en minutos. Conviene un solo '
                'reclamo al proveedor en vez de un ticket por farmacia.'
            )
            clase = 'proveedor'
        else:
            detalle = ''
            clase = 'individual'
        for evento in racimo:
            clasificacion[evento.farmacia_id] = (clase, detalle)
    return clasificacion


def notificar_cambios_enlaces() -> dict:
    """Manda UN correo con los enlaces que se cayeron y los que volvieron desde el
    último aviso. Devuelve cuántos de cada uno.

    Un correo agrupado y no uno por evento: se midieron 196 caídas en 24 horas sobre
    700 sitios (17-sep-2026). Un correo por caída, con los 10 destinatarios que hoy
    reciben las alertas de estación, serían ~2.000 envíos diarios — Gmail los corta y
    nadie los lee. Agrupado por proveedor, que es como se abre el ticket.

    No decide nada sobre cuándo una caída es real: eso ya lo resolvió `registrar_sondeo`
    exigiendo UMBRAL_FALLAS_CONSECUTIVAS sondeos fallidos seguidos antes de abrir el
    EventoEnlaceFarmacia. Acá solo se avisa de lo que ya está confirmado, y cada evento
    se avisa una sola vez (`notificado_en` / `recuperacion_notificada_en`).

    Sin ENLACES_NOTIFICAR_A configurado no manda nada y lo dice en el log: el evento ya
    quedó guardado igual, y una instalación nueva no debe empezar a escribirle a nadie.
    """
    from datetime import timedelta

    from django.conf import settings
    from django.core.mail import send_mail

    from .models import ConfiguracionMonitoreo, EventoEnlaceFarmacia

    # Se excluye lo que nunca respondio: un sitio que jamas contesto no tiene una
    # caida que reportarle al proveedor -- el proveedor va a responder que su enlace
    # esta arriba, y va a tener razon. Eso se revisa por el panel (ruta, IP, si el
    # sitio sigue activo), no por un correo de incidente.
    # Solo lo que lleva caido el tiempo minimo. Un corte que se resuelve solo en pocos
    # minutos no amerita un aviso: de 85 caidas en 24 h medidas el 18-sep-2026, 16
    # duraron 5 minutos o menos. Las que sigan caidas en la proxima corrida se avisan
    # entonces — no se pierden, se esperan.
    ahora = timezone.now()
    minimos = ConfiguracionMonitoreo.obtener().minutos_minimos_aviso_enlace
    corte = ahora - timedelta(minutes=minimos)
    caidos = list(
        EventoEnlaceFarmacia.objects
        .filter(fin__isnull=True, notificado_en__isnull=True, inicio__lte=corte)
        .exclude(farmacia__estado_enlace__respondio_alguna_vez=False)
        .select_related('farmacia').order_by('inicio')
    )
    # Solo la recuperacion de lo que SI se aviso como caido. Sin este filtro llegaba
    # "GP092 estuvo caido 2 min" sin que antes se hubiera avisado nada: la caida nacia y
    # moria entre dos corridas, y salia el aviso de vuelta huerfano. Pasaba en 12 de las
    # 85 caidas de un dia. Anunciar que algo volvio, cuando nunca se dijo que se habia
    # ido, es puro ruido.
    recuperados = list(
        EventoEnlaceFarmacia.objects
        .filter(fin__isnull=False, recuperacion_notificada_en__isnull=True,
                notificado_en__isnull=False)
        .select_related('farmacia').order_by('fin')
    )
    resumen = {'caidos': len(caidos), 'recuperados': len(recuperados), 'enviado': False}
    if not caidos and not recuperados:
        return resumen

    destinatarios = list(getattr(settings, 'ENLACES_NOTIFICAR_A', []) or [])
    chat_telegram = getattr(settings, 'ENLACES_TELEGRAM_CHAT_ID', '')
    # Alcanza con UNO de los dos canales para que valga la pena armar el mensaje. Se sale
    # solo si no hay a quién avisarle por ningún lado: marcar los eventos como avisados
    # sin haberlos mandado a nadie perdería el aviso para siempre.
    if not destinatarios and not chat_telegram:
        logger.info(
            'Enlaces: %d caída(s) y %d recuperación(es) sin avisar — no hay ENLACES_NOTIFICAR_A '
            'ni ENLACES_TELEGRAM_CHAT_ID configurados.',
            len(caidos), len(recuperados),
        )
        return resumen

    lineas = []
    if caidos:
        lineas.append(f'ENLACES CAÍDOS ({len(caidos)})')
        for proveedor, eventos in _agrupar_por_proveedor(caidos).items():
            lineas.append(f'\n  {proveedor}')
            for e in eventos:
                lineas.append(
                    f'    {e.farmacia.codigo:<10} {e.farmacia.ip_router}  '
                    f'desde {timezone.localtime(e.inicio):%H:%M}  circuito: {e.circuito_proveedor or "-"}'
                )
    if recuperados:
        if lineas:
            lineas.append('')
        lineas.append(f'ENLACES RECUPERADOS ({len(recuperados)})')
        for proveedor, eventos in _agrupar_por_proveedor(recuperados).items():
            lineas.append(f'\n  {proveedor}')
            for e in eventos:
                lineas.append(
                    f'    {e.farmacia.codigo:<10} {e.farmacia.ip_router}  '
                    f'estuvo caído {e.duracion_minutos} min'
                )

    partes_asunto = []
    if caidos:
        partes_asunto.append(f'{len(caidos)} caído(s)')
    if recuperados:
        partes_asunto.append(f'{len(recuperados)} recuperado(s)')
    asunto = '[Enlaces] ' + ', '.join(partes_asunto)

    cuerpo = '\n'.join(lineas)
    if destinatarios:
        # fail_silently: un SMTP caído no debe tumbar el sondeo. El evento ya está en la
        # BD y el panel lo muestra igual -- mismo criterio que notificar_alerta.
        send_mail(asunto, cuerpo, None, destinatarios, fail_silently=True)

    if chat_telegram:
        # Import diferido: enlaces.py lo importan el comando de sondeo y la API de
        # ingesta, y services arrastra el motor de alertas entero.
        from .services import _enviar_telegram
        # Mismo texto que el correo, con el asunto arriba: es un resumen operativo, no
        # una alerta de una estación, así que no lleva emoji de severidad.
        _enviar_telegram(chat_telegram, f'{asunto}\n\n{cuerpo}')

    # Se marcan DESPUÉS del envío, y con fail_silently arriba eso significa que un SMTP
    # caído marca igual el evento como avisado: el correo se pierde. Es deliberado --
    # la alternativa (reintentar) acumularía el backlog entero y lo mandaría de golpe
    # cuando el SMTP vuelva, que es justo el correo ilegible que este diseño evita.
    EventoEnlaceFarmacia.objects.filter(pk__in=[e.pk for e in caidos]).update(notificado_en=ahora)
    EventoEnlaceFarmacia.objects.filter(pk__in=[e.pk for e in recuperados]).update(
        recuperacion_notificada_en=ahora,
    )
    resumen['enviado'] = True
    logger.info('Enlaces: avisadas %d caída(s) y %d recuperación(es).', len(caidos), len(recuperados))
    return resumen
