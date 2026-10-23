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


class FalloLocalDeSondeo(RuntimeError):
    """Base de los fallos que son de ESTE host y no del enlace de la farmacia.

    Existe porque los dos se ven idénticos desde afuera —"no respondió"— y confundirlos
    hace diagnosticar al revés: se sale a buscar el problema a 700 farmacias cuando está
    en el servidor que pregunta. Todo lo que herede de acá tiene que ABORTAR el barrido
    con el motivo real, nunca contarse como una caída.
    """


class PingNoDisponible(FalloLocalDeSondeo):
    """No hay binario `ping` en este host.

    Tiene excepción propia porque el síntoma es engañoso: `subprocess.run` de un binario
    inexistente levanta FileNotFoundError, que el sondeo trataría como "no respondió", y
    el barrido entero se vería idéntico a una flota caída o a una ruta rota. Pasó de
    verdad — la imagen de producción no traía `iputils-ping` (ver deploy/Dockerfile).
    """


class RecursosDelHostAgotados(FalloLocalDeSondeo):
    """Este host no pudo ni LANZAR el ping: se quedó sin descriptores, memoria o procesos.

    El 3-oct-2026 esto tumbó el monitoreo de enlaces durante horas y nadie lo supo, porque
    `sondear_enlace` atrapaba el `OSError` junto con el timeout y devolvía "no responde".
    El panel mostraba 700 farmacias caídas; lo que pasaba era que el proceso de Celery
    había agotado sus descriptores por una fuga de sockets SNMP (ver `_motor_snmp` en
    apps/monitoreo/mikrotik.py) y no conseguía ni abrir un pipe para `ping`.

    Un `OSError` de `subprocess.run` SIEMPRE es local: habla de arrancar el proceso, no de
    la red. Cuando el ping sale y el destino no contesta, eso llega por `returncode`, no
    por excepción. Por eso se puede separar sin ambigüedad del timeout, que sí es del
    enlace.
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


class ResultadoSondeo(tuple):
    """`(alcanzable, latencia_ms)` —lo que siempre devolvió `sondear_enlace`— más la
    calidad del sondeo en atributos: `perdida_pct` y `latencia_max_ms`.

    Es una tupla de DOS elementos a propósito: todo lo que ya desempaquetaba
    `alcanzable, latencia = sondear_enlace(ip)` (el barrido, la API, las pruebas que lo
    sustituyen) sigue igual. Lo nuevo se lee con `getattr(r, 'perdida_pct', None)`, que
    también funciona cuando alguien devuelve una tupla común.
    """

    def __new__(cls, alcanzable, latencia_ms, perdida_pct=None, latencia_max_ms=None):
        resultado = super().__new__(cls, (alcanzable, latencia_ms))
        resultado.perdida_pct = perdida_pct
        resultado.latencia_max_ms = latencia_max_ms
        return resultado


def _calidad(salida: str):
    """`(promedio, máximo, pérdida %)` a partir de las líneas de respuesta de `ping`.

    Se cuentan las respuestas con tiempo ("time=" / "tiempo=") en vez de leer el resumen
    final, porque el resumen cambia de formato e idioma entre Linux y Windows y estas
    líneas no. Si no calza ninguna, devuelve `(None, None, None)`: mejor sin dato que una
    pérdida inventada por un problema de locale.
    """
    tiempos = [float(t.replace(',', '.')) for t in _RE_LATENCIA.findall(salida)]
    if not tiempos:
        return None, None, None
    recibidos = min(len(tiempos), PAQUETES_POR_SONDEO)
    perdida = round(100 * (PAQUETES_POR_SONDEO - recibidos) / PAQUETES_POR_SONDEO, 1)
    return round(sum(tiempos) / len(tiempos), 1), max(tiempos), perdida


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
    except subprocess.TimeoutExpired:
        # Esto sí es del enlace: el ping salió y nadie contestó a tiempo.
        return ResultadoSondeo(False, None, 100.0)
    except OSError as error:
        # Esto NO es del enlace: no se pudo arrancar el proceso. Se propaga para que el
        # barrido aborte diciendo el motivo real, en vez de anotar una caída falsa por
        # farmacia. Ver RecursosDelHostAgotados.
        raise RecursosDelHostAgotados(
            f'Este host no pudo lanzar `ping` hacia {ip}: {error}. No es una caída del '
            'enlace — revisá los recursos del proceso (descriptores, memoria) antes de '
            'sospechar de la farmacia.',
        ) from error

    if proceso.returncode != 0:
        return ResultadoSondeo(False, None, 100.0)

    # returncode 0 no alcanza en Windows: `ping` devuelve 0 aunque la respuesta sea
    # "Host de destino inaccesible", que es un ICMP de otro equipo, no del destino.
    salida = proceso.stdout or ''
    coincidencia = _RE_LATENCIA.search(salida)
    if coincidencia is None:
        if 'inaccesible' in salida.lower() or 'unreachable' in salida.lower():
            return ResultadoSondeo(False, None, 100.0)
        return ResultadoSondeo(True, None)
    # La latencia del estado sigue siendo la del PRIMER paquete, como siempre; el promedio,
    # el máximo y la pérdida van aparte, para el historial.
    promedio, maximo, perdida = _calidad(salida)
    resultado = ResultadoSondeo(True, float(coincidencia.group(1).replace(',', '.')), perdida, maximo)
    resultado.latencia_promedio_ms = promedio
    return resultado


def registrar_sondeo(farmacia, alcanzable: bool, latencia_ms: float | None):
    """Aplica el resultado de un sondeo al estado del enlace, abriendo o cerrando la
    caída si corresponde. Es el punto de entrada para CUALQUIER origen del dato —
    el comando de acá, un agente, o un probe externo por API el día que exista.

    La caída no se declara al primer fallo: un paquete ICMP se pierde por mil motivos.
    Se cuentan `UMBRAL_FALLAS_CONSECUTIVAS` sondeos fallidos seguidos, igual que hacía
    `Cresio_enlaces`. La recuperación sí es inmediata — si respondió, está viva.

    **Una farmacia con `activa=False` no se registra y devuelve None.** La guarda vive
    acá y no solo en el barrido porque `activa=False` es la única palanca que tiene el
    operador para dar de baja un sitio, y hasta el 2-oct-2026 solo la respetaba
    `sondear_enlaces_farmacias`: la API de ingesta (`apps.monitoreo.api_views`) y el
    `--farmacia CODIGO` del comando seguían escribiendo, así que un sitio dado de baja
    volvía a abrir caídas por la puerta de al lado. Siendo este el punto único por donde
    pasa todo sondeo, la baja se respeta venga el dato de donde venga.
    """
    from .models import ConfiguracionMonitoreo, EstadoEnlaceFarmacia, EventoEnlaceFarmacia

    if not farmacia.activa:
        logger.debug('Sondeo de %s ignorado: la farmacia está dada de baja (activa=False).', farmacia.codigo)
        return None

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
        # Se cierran TODAS las caídas abiertas de la farmacia, no solo la más reciente.
        #
        # Hasta el 10-oct-2026 esto hacía `.order_by('-inicio').first()`, y la diferencia
        # importa porque nada impide que haya dos abiertas a la vez: no hay constraint, y
        # `estaba_caida` es la única cosa que lo evita en la práctica. Si por cualquier
        # camino quedaba una segunda, la recuperación cerraba una y la otra se volvía un
        # fantasma: una caída abierta para siempre, en un sitio que responde, que ninguna
        # pantalla podía hacer desaparecer (hallazgo D-04).
        #
        # No se añade el constraint de unicidad que la auditoría proponía: una migración
        # que lo imponga falla si ya hay duplicados en producción, y resolverlos
        # automáticamente significaría inventar un `fin` para un evento cuya duración
        # real nadie sabe. Esto quita la CAUSA de que un fantasma sobreviva —se cierra
        # con el resto— sin tocar el historial, que es la evidencia de SLA. Para medir si
        # hay duplicados hoy: `manage.py revisar_caidas_duplicadas`.
        abiertas = list(
            EventoEnlaceFarmacia.objects.filter(farmacia=farmacia, fin__isnull=True).order_by('inicio')
        )
        if abiertas:
            EventoEnlaceFarmacia.objects.filter(
                pk__in=[e.pk for e in abiertas],
            ).update(fin=ahora)
            if len(abiertas) > 1:
                logger.warning(
                    'Enlace RECUPERADO en %s con %d caídas abiertas a la vez: se cerraron '
                    'todas. Revisar con revisar_caidas_duplicadas.',
                    farmacia.codigo, len(abiertas),
                )
            abiertas[0].fin = ahora
            logger.info('Enlace RECUPERADO en %s tras %s min', farmacia.codigo, abiertas[0].duracion_minutos)
    return estado


def dar_de_baja_enlace(farmacia) -> dict:
    """Limpia el rastro de monitoreo de enlace de una farmacia dada de baja.

    Por qué hace falta y no alcanza con `activa=False`: el flag frena el SONDEO, pero no
    toca lo que el último sondeo ya dejó escrito. Si la farmacia estaba caída cuando se
    la dio de baja, quedan dos cosas vivas:

    - un `EventoEnlaceFarmacia` con `fin=None`, que todas las pantallas de caídas en
      curso leen (Centro de Monitoreo, `/enlaces` del bot, el detalle de la farmacia), y
    - un `EstadoEnlaceFarmacia` con `alcanzable=False`, que es lo que cuenta el KPI
      "enlaces caídos" de `resumen_operacion`.

    Como ya nadie la sondea, nada la va a recuperar nunca: la caída se congela y el sitio
    aparece caído para siempre. Es el síntoma que se reportó con GP063 el 2-oct-2026 —
    "la deshabilité en el admin y me sigue saliendo la alerta".

    La caída se CIERRA (no se borra) con `fin=ahora`: el historial de lo que de verdad
    pasó mientras el sitio estaba en operación no se toca, que es lo que sostiene
    cualquier reclamo de SLA hacia atrás. Y se marca como notificada para que
    `notificar_cambios_enlaces` no mande después un "GP063 se recuperó" por algo que no
    se recuperó: se dio de baja.

    Idempotente: correrlo dos veces no cambia nada la segunda.
    """
    from .models import EstadoEnlaceFarmacia, EventoEnlaceFarmacia

    ahora = timezone.now()
    cerrados = EventoEnlaceFarmacia.objects.filter(farmacia=farmacia, fin__isnull=True).update(
        fin=ahora, recuperacion_notificada_en=ahora,
    )
    # El estado vuelve a `alcanzable=None` ("nunca se sondeó"), que es exactamente la
    # verdad de una farmacia que ya no se sondea — y el único valor que ninguna pantalla
    # cuenta como caída. Dejarlo en True mentiría al revés: diría que responde.
    estados = EstadoEnlaceFarmacia.objects.filter(farmacia=farmacia).update(
        alcanzable=None, latencia_ms=None, fallas_consecutivas=0, primer_fallo=None,
        ultimo_cambio_estado=ahora,
    )
    if cerrados or estados:
        logger.info(
            'Enlace de %s dado de baja: %d caída(s) cerrada(s), %d estado(s) limpiado(s).',
            farmacia.codigo, cerrados, estados,
        )
    return {'caidas_cerradas': cerrados, 'estados_limpiados': estados}


def sondear_enlaces_farmacias(farmacias=None, *, flota_entera=None) -> dict:
    """Sondea el enlace de cada farmacia con `ip_router` cargada. Devuelve un resumen.

    **Corre en Celery Beat cada 2 minutos** (`sondear-enlaces-farmacias`), y también a
    mano con `python manage.py sondear_enlaces`.

    Hasta el 2-oct-2026 este docstring decía lo contrario —"no se programa en Beat,
    desde el servidor central no hay ruta a esas IP"—, que era cierto cuando se escribió
    (24-ago-2026) y dejó de serlo el 11-sep, cuando se verificó sobre el NUC real que
    host y contenedor sí alcanzan las IP de las farmacias y la tarea se agregó al
    schedule. El docstring se quedó con la frase vieja, y eso hace diagnosticar al revés:
    frente a estados de enlace congelados se concluye "nadie lo está corriendo" cuando en
    realidad corre cada 2 minutos y está ABORTANDO.

    Si el barrido falla casi entero (`UMBRAL_BARRIDO_SOSPECHOSO_PCT`), **no registra
    nada** y devuelve el resumen con `abortado=True`. Esa es la diferencia entre una
    herramienta útil y una que llena la base de 704 caídas falsas la primera vez que
    alguien la corre desde el lugar equivocado.

    **Cómo se ve un barrido que aborta, desde afuera:** los estados dejan de
    actualizarse y `EstadoEnlaceFarmacia.ultima_verificacion` envejece parejo en TODA la
    flota. No hay error en el panel ni alerta — solo el `logger.error` de más abajo. Si
    ves todos los enlaces con la misma antigüedad de horas, buscá el abortado en el log
    del worker antes de sospechar de las farmacias: lo más probable es que el que perdió
    la ruta sea este host. Ver también `clasificar_caidas_simultaneas`, que distingue
    'ceguera' (varios ISP a la vez = somos nosotros) de 'proveedor'.
    """
    from apps.catalogo.models import Farmacia

    # `flota_entera`: el comando manual pasa la flota como lista y también es un barrido
    # completo. Por defecto, solo cuando no se pasó una selección.
    es_flota_entera = (farmacias is None) if flota_entera is None else flota_entera
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

    try:
        with ThreadPoolExecutor(max_workers=MAX_SONDEOS_CONCURRENTES) as pool:
            crudos = list(pool.map(lambda f: (f, sondear_enlace(str(f.ip_router))), farmacias))
    except FalloLocalDeSondeo as error:
        # El problema es de este host, así que no hay NINGÚN resultado que valga: anotar
        # las farmacias que alcanzamos a sondear antes del fallo mezclaría medidas buenas
        # con una base que ya no se sostiene. Se aborta entero y se dice por qué.
        logger.error('Barrido de enlaces abortado sin registrar nada: %s', error)
        resumen.update(abortado=True)
        return resumen

    resultados = [(f, r[0], r[1]) for f, r in crudos]
    calidades = crudos
    fallidos = sum(1 for _, alcanzable, _lat in resultados if not alcanzable)
    pct_fallido = 100 * fallidos / len(resultados)
    if pct_fallido >= UMBRAL_BARRIDO_SOSPECHOSO_PCT:
        logger.error(
            'Barrido de enlaces abortado: %.0f%% de %d farmacias no respondió. Eso no son caídas '
            'simultáneas: el problema está en este host o en su ruta hacia las farmacias. No se '
            'registró nada. Buscá antes en este mismo log un fallo local (ver '
            'FalloLocalDeSondeo) y recién después sospechá de la ruta — el 3-oct-2026 este '
            'mensaje decía "no hay ruta" como un hecho y la causa era una fuga de descriptores '
            'en el propio proceso, con la red intacta.',
            pct_fallido, len(resultados),
        )
        resumen.update(abortado=True, sondeadas=len(resultados), caidas=fallidos)
        return resumen

    # Antes de registrar: los estados de ahora son todavía los de antes del hueco, que es
    # contra lo que se mide quién no volvió. Solo en el barrido de la flota entera; uno
    # manual de unas pocas no dice nada del monitoreo.
    if es_flota_entera:
        _registrar_reanudacion_si_hubo_hueco()

    for farmacia, alcanzable, latencia_ms in resultados:
        registrar_sondeo(farmacia, alcanzable, latencia_ms)
        resumen['sondeadas'] += 1
        resumen['activas' if alcanzable else 'caidas'] += 1
    guardar_muestras_de_enlace(calidades)
    if es_flota_entera:
        _marcar_barrido_completo()
    return resumen


def guardar_muestras_de_enlace(calidades) -> int:
    """Guarda el historial de latencia y pérdida de un barrido, en UN insert.

    `calidades`: `[(farmacia, resultado_de_sondear_enlace)]`. Solo se llama con un barrido
    que NO abortó: si falló casi entero, el problema era la ruta desde acá, y guardar 700
    "pérdida 100 %" ensuciaría la evidencia de cada farmacia con un fallo nuestro.

    Nunca lanza: el historial es un agregado, y un problema escribiéndolo no puede
    impedir que el barrido registre caídas y recuperaciones, que es lo que avisa.
    """
    from .models import MuestraEnlaceFarmacia

    ahora = timezone.now()
    filas = []
    for farmacia, resultado in calidades:
        alcanzable, latencia_ms = resultado[0], resultado[1]
        perdida = getattr(resultado, 'perdida_pct', None)
        if perdida is None and not alcanzable:
            perdida = 100.0
        filas.append(MuestraEnlaceFarmacia(
            farmacia=farmacia, timestamp=ahora,
            latencia_ms=getattr(resultado, 'latencia_promedio_ms', None) or latencia_ms,
            latencia_max_ms=getattr(resultado, 'latencia_max_ms', None),
            perdida_pct=perdida,
        ))
    try:
        MuestraEnlaceFarmacia.objects.bulk_create(filas)
    except Exception:
        logger.exception('No se pudo guardar el historial de %d sondeos de enlace.', len(filas))
        return 0
    return len(filas)


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


def lineas_de_contacto(farmacia, sangria='      ') -> list[str]:
    """Quién atiende en el local y qué técnico va, para los avisos de enlace caído.

    Lo pidió la operación (23-oct-2026): con el aviso en la mano, lo siguiente siempre era
    buscar en otra planilla a quién llamar. El dato ya estaba en la ficha de la farmacia
    (`administrador`, `telefono`, `extension_telefonica`, `tecnico_asignado`).

    Si falta, se dice que falta en vez de omitir la línea: así el aviso mismo muestra qué
    hay que completar. Sin la cédula del técnico: no hace falta para llamarlo y es un dato
    personal que no tiene por qué viajar en un chat.
    """
    partes = [farmacia.administrador.strip()] if farmacia.administrador.strip() else []
    if farmacia.telefono.strip():
        partes.append(farmacia.telefono.strip())
    if farmacia.extension_telefonica:
        partes.append(f'ext {farmacia.extension_telefonica}')
    sitio = ' · '.join(partes) if partes else 'sin contacto cargado'
    tecnico = farmacia.tecnico_asignado
    if tecnico is None:
        texto_tecnico = 'sin asignar'
    else:
        texto_tecnico = tecnico.nombre + (f' · {tecnico.telefono}' if tecnico.telefono else ' · sin teléfono')
    return [f'{sangria}En sitio: {sitio}', f'{sangria}Técnico:  {texto_tecnico}']


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


# --- Línea de tiempo de las caídas en curso (Zona 2c del tablero) ------------------

# El ancho del dibujo, en horas. Es la ESCALA de la línea de tiempo, no un filtro: ver
# el docstring de `linea_tiempo_caidas_en_curso` sobre por qué esa distinción es lo
# único que mantiene el bloque coherente con el KPI de al lado.
VENTANA_LINEA_TIEMPO_HORAS = 6

# Cuántas barras entran. Mismo número que la cola de incidencias del tablero
# (`cola_incidencias` recortada a 8): es una pantalla de decisión, no un listado, y
# para el listado completo ya está /enlaces/.
MAX_BARRAS_LINEA_TIEMPO = 8


def linea_tiempo_caidas_en_curso(unidades=None, *, horas=VENTANA_LINEA_TIEMPO_HORAS,
                                 limite=MAX_BARRAS_LINEA_TIEMPO) -> list[dict]:
    """Las caídas de enlace TODAVÍA ABIERTAS, con cuánto llevan, para dibujarlas.

    **Mismo conjunto que el KPI "farmacias sin enlace", por construcción.** Las tres
    exclusiones son las canónicas, idénticas a las que ya usan el tablero, el Centro y
    `cola_incidencias`, y no una cuarta definición de "caída":

      - `fin__isnull=True`  -> solo las que siguen abiertas; una recuperada no es una
                               caída en curso y su historia está en /enlaces/.
      - `farmacia__activa`  -> una farmacia dada de baja deja de sondearse y su estado
                               queda congelado; no es algo que alguien vaya a ir a
                               arreglar (reportado con GP063 el 2-oct-2026).
      - `respondio_alguna_vez=False` excluido -> nunca respondió es configuración
                               pendiente, no una caída.

    **Las seis horas son la escala del dibujo, NO un filtro temporal**, y esto es la
    decisión de fondo del bloque. Filtrar por `inicio >= ahora - 6 h` habría dejado
    afuera justamente las peores: las caídas crónicas, que son mayoría —cuando se montó
    el aviso por correo había 162 caídas abiertas y 134 de ellas pasaban las 24 horas
    (ver el backfill de `notificado_en`)—. Y además habría hecho que este bloque y el KPI
    contaran poblaciones distintas bajo el mismo nombre, que es el defecto que el tablero
    ya corrigió dos veces (hallazgos D-01 y D-03). Así que entran todas las abiertas, y
    la que empezó antes del borde se dibuja llena hasta el borde con `recortada=True`:
    la barra se agota, el texto sigue diciendo la duración real.

    **Una farmacia, una barra.** Nada impide hoy dos eventos abiertos para el mismo
    sitio —no hay constraint, hallazgo D-04— y el KPI cuenta farmacias distintas, no
    eventos. Si pasara, se queda la más antigua: es la que dice desde cuándo el sitio no
    vende.

    `unidades` = iterable de UnidadNegocio, o None para todas. El llamador decide el
    alcance; esta función no lee la sesión ni el request.

    El TOTAL no sale de acá a propósito: la vista ya lo calcula para el KPI
    (`total_enlaces_caidos`) y el bloque muestra ese mismo número, así que las dos cifras
    no pueden discrepar ni hace falta una segunda consulta para contarlas.

    Una consulta, acotada a `limite` filas y con `select_related('farmacia')` porque
    `nombre_proveedor` lee `farmacia.tipo_enlace`.
    """
    from .models import EventoEnlaceFarmacia

    ahora = timezone.now()
    ventana_minutos = horas * 60

    eventos = (
        EventoEnlaceFarmacia.objects
        .filter(fin__isnull=True, farmacia__activa=True)
        .exclude(farmacia__estado_enlace__respondio_alguna_vez=False)
    )
    if unidades is not None:
        eventos = eventos.filter(farmacia__unidad_negocio__in=list(unidades))

    # `-inicio`: lo más reciente arriba. En una ventana de seis horas la pregunta es qué
    # se acaba de caer; lo crónico ya está contado en el total y priorizado en la Zona 1.
    # Se piden `limite + 1` por si las dos primeras filas son del mismo sitio: así la
    # deduplicación no deja la lista corta sin motivo.
    eventos = eventos.select_related('farmacia').order_by('-inicio')[:limite + 1]

    def barra(evento):
        minutos = max(0, int((ahora - evento.inicio).total_seconds() // 60))
        recortada = minutos > ventana_minutos
        return {
            'farmacia': evento.farmacia,
            'proveedor': nombre_proveedor(evento.farmacia),
            'inicio': evento.inicio,
            'minutos': minutos,
            # El ancho en % de la ventana. Se calcula acá y no en la plantilla porque las
            # plantillas de Django no dividen, y con un mínimo visible: una caída de un
            # minuto sobre una escala de 360 da 0,3% y no se vería, pero existe.
            'pct': 100 if recortada else max(1.5, round(minutos * 100 / ventana_minutos, 1)),
            'recortada': recortada,
        }

    barras = []
    fila_de_farmacia = {}
    for evento in eventos:
        fila = fila_de_farmacia.get(evento.farmacia_id)
        if fila is not None:
            # Segundo evento abierto del mismo sitio. Viene detrás porque el orden es
            # `-inicio`, o sea que es el MÁS ANTIGUO, y es ese el que dice desde cuándo la
            # farmacia no vende: se reemplaza el dato de la fila y no se agrega otra. Con
            # el más reciente, la barra diría que lleva menos tiempo caída de lo que lleva.
            barras[fila] = barra(evento)
            continue
        if len(barras) >= limite:
            break
        fila_de_farmacia[evento.farmacia_id] = len(barras)
        barras.append(barra(evento))
    return barras


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
# - PROVEEDORES_PARA_CEGUERA en 2, contando solo los proveedores con al menos MÍNIMO
#   caídas en el racimo. Hasta el 17-oct-2026 alcanzaba con que el racimo TOCARA tres ISP,
#   y eso descontaba del SLA cortes que eran del proveedor: un corte de Telconet de 31
#   sitios en el que coincidían una caída de Punto Net y una de Fibromark se clasificaba
#   "el monitoreo no vio". Cuando el que deja de ver es el servidor, caen varios
#   proveedores EN CANTIDAD (17-oct: Telconet 80, Punto Net 31). Medido sobre 30 días: 228
#   caídas volvieron al reclamo y 106 que eran del monitoreo dejaron de cargarse al ISP.
# - TOPE de 30 min por racimo: sin tope, un día con muchas caídas encadena eventos sin
#   relación de diez en diez minutos.
VENTANA_SIMULTANEAS_MINUTOS = 10
TOPE_RACIMO_MINUTOS = 30
MINIMO_PARA_CORTE_DE_PROVEEDOR = 3
PROVEEDORES_PARA_CEGUERA = 2


def armar_racimos(eventos) -> list[list]:
    """Agrupa caídas que empezaron juntas: cada una a no más de VENTANA de la anterior y
    a no más de TOPE de la primera del racimo."""
    from datetime import timedelta

    ventana = timedelta(minutes=VENTANA_SIMULTANEAS_MINUTOS)
    tope = timedelta(minutes=TOPE_RACIMO_MINUTOS)
    racimos, actual = [], []
    for evento in sorted(eventos, key=lambda e: e.inicio):
        if actual and (evento.inicio - actual[-1].inicio > ventana or evento.inicio - actual[0].inicio > tope):
            racimos.append(actual)
            actual = []
        actual.append(evento)
    if actual:
        racimos.append(actual)
    return racimos


def clase_en_racimo(racimo) -> dict:
    """`{evento.pk: clase}` dentro de un racimo. LA regla de clasificación del proyecto:
    la usan el tablero, la tabla de enlaces y el historial, para que el mismo corte diga
    lo mismo en las tres pantallas (antes eran tres reglas y no coincidían).

      - `ceguera`    — dos o más proveedores con MÍNIMO caídas cada uno: lo más probable
                       es que el que perdió visibilidad sea este servidor.
      - `proveedor`  — el proveedor de ESTA caída tiene MÍNIMO caídas en el racimo.
      - `individual` — lo demás: es ese sitio (incluida la caída suelta de otro ISP que
                       coincidió con un corte ajeno).
    """
    from collections import Counter

    cuenta = Counter(nombre_proveedor(e.farmacia) for e in racimo)
    fuertes = {p for p, n in cuenta.items() if n >= MINIMO_PARA_CORTE_DE_PROVEEDOR}
    if len(fuertes) >= PROVEEDORES_PARA_CEGUERA:
        return {e.pk: 'ceguera' for e in racimo}
    return {e.pk: ('proveedor' if nombre_proveedor(e.farmacia) in fuertes else 'individual') for e in racimo}


def clasificar_caidas_simultaneas(eventos):
    """`{farmacia_id: (clase, detalle)}` para un conjunto de caídas.

    Es una INFERENCIA, no una lectura: mira cuándo empezaron las caídas y de qué proveedor
    es cada una. Por eso las etiquetas dicen "probable" y no afirman una causa. Los eventos
    tienen que venir con `select_related('farmacia')`, y conviene pasar la FLOTA ENTERA
    (abiertos y cerrados recientes): un corte se reconoce por lo que pasó alrededor.
    """
    from collections import Counter

    clasificacion = {}
    for racimo in armar_racimos(eventos):
        clases = clase_en_racimo(racimo)
        cuenta = Counter(nombre_proveedor(e.farmacia) for e in racimo)
        for evento in racimo:
            clase = clases[evento.pk]
            if clase == 'ceguera':
                detalle = (
                    f'{len(racimo)} sitios de {len(cuenta)} proveedores cayeron en minutos, varios '
                    'en cantidad. Que varios ISP fallen a la vez es muy improbable: revisá '
                    'primero la conexión del servidor de monitoreo.'
                )
            elif clase == 'proveedor':
                proveedor = nombre_proveedor(evento.farmacia)
                detalle = (
                    f'{cuenta[proveedor]} sitios de {proveedor} cayeron en minutos. Conviene un '
                    'solo reclamo al proveedor en vez de un ticket por farmacia.'
                )
            else:
                detalle = ''
            clasificacion[evento.farmacia_id] = (clase, detalle)
    return clasificacion


# --- Incidentes: el corte visto como UN evento, y lo que quedó suelto ----------------

# Hasta dónde se mira hacia atrás para armar los racimos. 48 h y no 24: un corte de la
# madrugada del día anterior con sitios que todavía no volvieron tiene que seguir
# apareciendo como corte, no deshacerse en caídas sueltas al pasar la medianoche.
VENTANA_INCIDENTES_HORAS = 48

# Una caída aislada que lleva más que esto ya no es una urgencia del día: es un sitio
# para revisar si sigue operando (GP063 llevaba 23 días el 17-oct-2026).
DIAS_CAIDA_CRONICA = 7


def incidentes_de_enlace(farmacias=None, *, horas=VENTANA_INCIDENTES_HORAS) -> dict:
    """Las caídas en curso, ordenadas para decidir qué atender primero.

    `clasificar_caidas_simultaneas` mira solo las caídas ABIERTAS, y eso esconde lo que más
    ayuda a decidir: el 17-oct-2026 a las 06:05 cayeron 36 farmacias de Telconet a la vez,
    20 volvieron solas en las 3 horas siguientes y 16 no. Viendo solo las 16 abiertas no se
    sabe que el proveedor ya resolvió para las demás — que es justo lo que dice que en esas
    16 hay algo más (el equipo de la farmacia no levantó solo, o un circuito pendiente).

    Por eso los racimos se arman con TODOS los eventos que empezaron en la ventana,
    abiertos o cerrados, con los mismos umbrales que `clasificar_caidas_simultaneas`.

    Devuelve:
      - `masivos`: un dict por corte con algún sitio todavía caído — cuándo empezó, de qué
        proveedor(es), cuántos cayeron, cuántos volvieron y la lista de los que siguen.
        Es la lista para UN reclamo al proveedor.
      - `aisladas`: las caídas abiertas que no son parte de ningún corte. Primero las que
        no tienen enlace de respaldo (probablemente sin sistema), después las más
        recientes; las que llevan más de `DIAS_CAIDA_CRONICA` van al final.

    Los sitios que nunca respondieron no entran: no son caídas (ver
    `EstadoEnlaceFarmacia.respondio_alguna_vez`).
    """
    from collections import Counter
    from datetime import timedelta

    from django.db.models import Count

    from apps.monitoreo.models import EventoEnlaceFarmacia

    ahora = timezone.now()
    # Los racimos se arman con la FLOTA ENTERA y el alcance se aplica recién al mostrar:
    # un corte se reconoce por lo que pasó alrededor, y armarlo solo con las farmacias de un
    # cliente convertía en "corte masivo de su proveedor" algo que fue del monitoreo, o lo
    # hacía desaparecer si en su alcance cayeron menos de tres (17-oct-2026).
    recientes = list(EventoEnlaceFarmacia.objects.filter(
        inicio__gte=ahora - timedelta(hours=horas), farmacia__estado_enlace__respondio_alguna_vez=True,
    ).select_related('farmacia'))
    abiertas = EventoEnlaceFarmacia.objects.filter(
        fin__isnull=True, farmacia__estado_enlace__respondio_alguna_vez=True,
    ).select_related('farmacia')
    permitidas = None if farmacias is None else set(farmacias)
    if permitidas is not None:
        abiertas = abiertas.filter(farmacia__in=permitidas)
    abiertas = list(abiertas)

    def _en_alcance(evento):
        return permitidas is None or evento.farmacia_id in permitidas

    caidas_30d = dict(
        EventoEnlaceFarmacia.objects.filter(
            farmacia__in=[e.farmacia_id for e in abiertas], inicio__gte=ahora - timedelta(days=30),
        ).values_list('farmacia').annotate(n=Count('id')),
    )

    def _sitio(evento):
        return {
            'farmacia': evento.farmacia, 'inicio': evento.inicio,
            'proveedor': nombre_proveedor(evento.farmacia),
            'sin_backup': not evento.farmacia.tiene_backup,
            'caidas_30d': caidas_30d.get(evento.farmacia_id, 0),
        }

    abiertas_por_id = {e.pk: e for e in abiertas}
    en_masivo = set()
    masivos = []
    for racimo in armar_racimos(recientes):
        clases = clase_en_racimo(racimo)
        if set(clases.values()) == {'ceguera'}:
            grupos = [('ceguera', racimo)]
        else:
            # Un corte por proveedor fuerte: la caída suelta de otro ISP que coincidió no
            # es parte de ese reclamo y queda como aislada.
            por_isp = {}
            for e in racimo:
                if clases[e.pk] == 'proveedor':
                    por_isp.setdefault(nombre_proveedor(e.farmacia), []).append(e)
            grupos = [('proveedor', es) for es in por_isp.values()]
        for clase, eventos in grupos:
            visibles = [e for e in eventos if _en_alcance(e)]
            siguen = [abiertas_por_id[e.pk] for e in visibles if e.pk in abiertas_por_id]
            if not siguen:
                continue
            en_masivo.update(e.pk for e in siguen)
            masivos.append({
                'inicio': eventos[0].inicio,
                'clase': clase,
                'proveedores': Counter(nombre_proveedor(e.farmacia) for e in visibles).most_common(),
                'cayeron': len(visibles),
                'volvieron': len(visibles) - len(siguen),
                'siguen': sorted((_sitio(e) for e in siguen), key=lambda s: s['farmacia'].codigo),
            })
    masivos.sort(key=lambda m: (-len(m['siguen']), m['inicio']))

    cronica = ahora - timedelta(days=DIAS_CAIDA_CRONICA)
    aisladas = [_sitio(e) for e in abiertas if e.pk not in en_masivo]
    for sitio in aisladas:
        sitio['cronica'] = sitio['inicio'] < cronica
    aisladas.sort(key=lambda s: (s['cronica'], not s['sin_backup'], -s['inicio'].timestamp()))

    # La etiqueta por fila de la tabla de enlaces, con la misma regla y la misma población
    # que los cortes de arriba: antes la tabla clasificaba aparte, y podían contradecirse.
    por_farmacia = clasificar_caidas_simultaneas(
        recientes + [e for e in abiertas if e.pk not in {r.pk for r in recientes}],
    )
    return {'masivos': masivos, 'aisladas': aisladas, 'clase_por_farmacia': por_farmacia}


# --- El monitoreo estuvo ciego y volvió: quién no se recuperó ------------------------

# Con barridos cada 2 min, 15 sin ninguno válido es que el monitoreo no estuvo mirando:
# el servidor apagado, o barridos abortados porque perdió la ruta (ver
# `sondear_enlaces_farmacias`). Holgado a propósito para no saltar por un worker lento.
HUECO_REANUDACION_MINUTOS = 15

# Cuánto se espera antes de avisar. Las que se recuperan solas lo hacen en los primeros
# minutos (el 17-oct-2026, 89 de 112 en el primer barrido) y una caída se declara recién
# al tercer sondeo fallido: avisar antes mandaría a reiniciar equipos que iban a volver.
ESPERA_AVISO_REANUDACION_MINUTOS = 15

# Las que cayeron en la hora anterior al hueco cuentan como "respondían antes". El
# 17-oct la red del data center se degradó de 02:54 a 03:09 antes de apagarse del todo,
# y 112 farmacias cayeron en ese tramo: por el corte, no por ellas.
MARGEN_PREVIO_AL_HUECO_MINUTOS = 60

# Pasado esto, un aviso que nunca salió (canales sin configurar, worker caído) ya no
# sirve: mandar "reiniciá estas" de un apagón de ayer confunde más de lo que ayuda.
VIGENCIA_REANUDACION_HORAS = 24


def _registrar_reanudacion_si_hubo_hueco():
    from datetime import timedelta

    from django.db.models import Max

    from .models import EstadoEnlaceFarmacia, EventoEnlaceFarmacia, ReanudacionMonitoreo

    from django.db import IntegrityError

    from .models import BarridoEnlaces

    ahora = timezone.now()
    # Contra el último barrido COMPLETO, no contra cualquier lectura: una farmacia probada a
    # mano durante el apagón, o la sonda externa, movían el Max y tapaban el hueco. El Max
    # queda solo como respaldo para la primera vez, antes de que exista la marca.
    marca = BarridoEnlaces.objects.filter(pk=1).values_list('ultimo_completo', flat=True).first()
    ultimo = marca or EstadoEnlaceFarmacia.objects.aggregate(m=Max('ultima_verificacion'))['m']
    if ultimo is None or ahora - ultimo < timedelta(minutes=HUECO_REANUDACION_MINUTOS):
        return None
    respondian = set(
        EstadoEnlaceFarmacia.objects.filter(alcanzable=True).values_list('farmacia_id', flat=True),
    )
    degradadas = set(
        EventoEnlaceFarmacia.objects.filter(
            fin__isnull=True, inicio__gte=ultimo - timedelta(minutes=MARGEN_PREVIO_AL_HUECO_MINUTOS),
        ).values_list('farmacia_id', flat=True),
    )
    logger.warning(
        'Enlaces: el monitoreo no barrió desde %s (%d min). Se registra la reanudación para '
        'avisar qué farmacias no vuelven.', ultimo, (ahora - ultimo).total_seconds() // 60,
    )
    # Un apagón, una fila (UniqueConstraint): si dos barridos arrancan juntos al volver el
    # worker, el segundo encuentra la del primero en vez de duplicar el aviso.
    try:
        reanudacion, _creada = ReanudacionMonitoreo.objects.get_or_create(
            apagado_desde=ultimo,
            defaults={'reanudado_en': ahora, 'farmacias_previas': sorted(respondian | degradadas)},
        )
    except IntegrityError:
        reanudacion = ReanudacionMonitoreo.objects.filter(apagado_desde=ultimo).first()
    return reanudacion


def _marcar_barrido_completo():
    from .models import BarridoEnlaces

    BarridoEnlaces.objects.update_or_create(pk=1, defaults={'ultimo_completo': timezone.now()})


def farmacias_sin_volver(reanudacion):
    """Las que respondían antes del hueco y todavía no contestan. Cuenta también las que
    fallan pero aún no cruzaron el umbral de caída: para esto da igual."""
    from django.db.models import Q

    from .models import EstadoEnlaceFarmacia

    estados = EstadoEnlaceFarmacia.objects.filter(
        farmacia_id__in=reanudacion.farmacias_previas, farmacia__activa=True,
    ).filter(Q(alcanzable=False) | Q(fallas_consecutivas__gt=0)).select_related('farmacia')
    return sorted((e.farmacia for e in estados), key=lambda f: (nombre_proveedor(f), f.codigo))


def reanudacion_vigente(farmacias=None):
    """La última reanudación de las últimas 24 h con farmacias que no volvieron, para el
    panel; None si no hay. `farmacias` limita a las del alcance del usuario."""
    from datetime import timedelta

    from .models import ReanudacionMonitoreo

    r = ReanudacionMonitoreo.objects.filter(
        reanudado_en__gte=timezone.now() - timedelta(hours=VIGENCIA_REANUDACION_HORAS),
    ).first()
    if r is None:
        return None
    permitidas = None if farmacias is None else set(farmacias)
    pendientes = farmacias_sin_volver(r)
    if permitidas is not None:
        pendientes = [f for f in pendientes if f.pk in permitidas]
    if not pendientes:
        return None
    return {'reanudacion': r, 'pendientes': pendientes, 'previas': _previas_vigentes(r, permitidas)}


def _previas_vigentes(reanudacion, permitidas=None) -> int:
    """Cuántas de las que respondían antes siguen activas (y están en el alcance). El total
    crudo contaba farmacias de otros clientes y dadas de baja: el usuario veía "de 650"
    con un número que no era el suyo, y el aviso contaba como "volvió sola" a una dada de
    baja."""
    from apps.catalogo.models import Farmacia

    ids = set(reanudacion.farmacias_previas)
    if permitidas is not None:
        ids &= permitidas
    return Farmacia.objects.filter(pk__in=ids, activa=True).count()


def avisar_reanudacion_monitoreo() -> dict:
    """Un solo aviso, a los mismos canales que las caídas, con las farmacias que no
    volvieron después de que el monitoreo estuvo ciego. Corre con
    `notificar_cambios_enlaces_task`."""
    from datetime import timedelta

    from django.conf import settings
    from django.core.mail import send_mail

    from .models import ReanudacionMonitoreo

    resumen = {'pendientes': 0, 'enviado': False}
    ahora = timezone.now()
    listas = ReanudacionMonitoreo.objects.filter(
        avisado_en__isnull=True,
        reanudado_en__lte=ahora - timedelta(minutes=ESPERA_AVISO_REANUDACION_MINUTOS),
    )
    r = listas.first()  # la más reciente
    if r is None:
        return resumen
    # Las anteriores sin avisar ya no sirven: avisar después de la vigente un apagón más
    # viejo es un mensaje fuera de orden. Se descartan.
    listas.exclude(pk=r.pk).update(avisado_en=ahora)
    if ahora - r.reanudado_en > timedelta(hours=VIGENCIA_REANUDACION_HORAS):
        ReanudacionMonitoreo.objects.filter(pk=r.pk).update(avisado_en=ahora)
        return resumen

    pendientes = farmacias_sin_volver(r)
    resumen['pendientes'] = len(pendientes)
    if not pendientes:
        # Volvieron todas: no hay nada que pedirle a nadie.
        ReanudacionMonitoreo.objects.filter(pk=r.pk).update(avisado_en=ahora)
        return resumen

    destinatarios = list(getattr(settings, 'ENLACES_NOTIFICAR_A', []) or [])
    chat_telegram = getattr(settings, 'ENLACES_TELEGRAM_CHAT_ID', '')
    if not destinatarios and not chat_telegram:
        return resumen
    # Se RESERVA antes de enviar, con un UPDATE condicional (mismo patrón que
    # `consumir_enlace`): si un envío se cuelga más de 5 min en el SMTP, la corrida
    # siguiente no encuentra la fila libre y no manda el aviso de nuevo.
    if not ReanudacionMonitoreo.objects.filter(pk=r.pk, avisado_en__isnull=True).update(avisado_en=ahora):
        return resumen

    previas = _previas_vigentes(r)
    horas, minutos = divmod(r.duracion_minutos, 60)
    asunto = f'[Enlaces] {len(pendientes)} farmacia(s) no volvieron tras el apagón del monitoreo'
    lineas = [
        f'El monitoreo de enlaces no estuvo mirando de {timezone.localtime(r.apagado_desde):%d/%m %H:%M} '
        f'a {timezone.localtime(r.reanudado_en):%H:%M} ({horas} h {minutos} min).',
        f'De {previas} farmacias que respondían antes, {previas - len(pendientes)} volvieron solas '
        f'y {len(pendientes)} NO:',
        '',
        'Lo más probable es que necesiten reiniciar el equipo de borde (router): el 17-oct-2026, '
        'tras un corte de luz en el data center, las que no volvieron solas se recuperaron recién '
        'con un reinicio manual.',
    ]
    por_proveedor = {}
    for f in pendientes:
        por_proveedor.setdefault(nombre_proveedor(f), []).append(f)
    for proveedor, fs in por_proveedor.items():
        lineas.append(f'\n  {proveedor} ({len(fs)})')
        for f in fs:
            lineas.append(f'    {f.codigo:<10} {f.ip_router}  {f.ciudad or f.ubicacion}'
                          f'{"  SIN respaldo" if not f.tiene_backup else ""}')
    cuerpo = '\n'.join(lineas)

    if destinatarios:
        send_mail(asunto, cuerpo, None, destinatarios, fail_silently=True)
    if chat_telegram:
        from .services import _enviar_telegram
        from .telegram_bot import teclado_para_farmacias

        _enviar_telegram(chat_telegram, f'{asunto}\n\n{cuerpo}',
                         teclado=teclado_para_farmacias([f.codigo for f in pendientes]) or None)
    resumen['enviado'] = True
    return resumen


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
    # `farmacia__activa=True` en las dos consultas: una farmacia dada de baja no debe
    # generar un solo aviso más, ni de caída ni de recuperación. El sondeo ya la
    # respetaba, pero el aviso no, así que una caída abierta justo antes de la baja
    # seguía saliendo por correo y Telegram (reportado con GP063 el 2-oct-2026). El
    # filtro no reemplaza a `dar_de_baja_enlace` sino que lo complementa: el filtro tapa
    # el aviso, la limpieza cierra el evento para que tampoco quede colgado en el panel.
    caidos = list(
        EventoEnlaceFarmacia.objects
        .filter(fin__isnull=True, notificado_en__isnull=True, inicio__lte=corte,
                farmacia__activa=True)
        .exclude(farmacia__estado_enlace__respondio_alguna_vez=False)
        .select_related('farmacia', 'farmacia__tecnico_asignado').order_by('inicio')
    )
    # Solo la recuperacion de lo que SI se aviso como caido. Sin este filtro llegaba
    # "GP092 estuvo caido 2 min" sin que antes se hubiera avisado nada: la caida nacia y
    # moria entre dos corridas, y salia el aviso de vuelta huerfano. Pasaba en 12 de las
    # 85 caidas de un dia. Anunciar que algo volvio, cuando nunca se dijo que se habia
    # ido, es puro ruido.
    recuperados = list(
        EventoEnlaceFarmacia.objects
        .filter(fin__isnull=False, recuperacion_notificada_en__isnull=True,
                notificado_en__isnull=False, farmacia__activa=True)
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
                lineas.extend(lineas_de_contacto(e.farmacia))
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
        from .telegram_bot import teclado_para_farmacias
        # Mismo texto que el correo, con el asunto arriba: es un resumen operativo, no
        # una alerta de una estación, así que no lleva emoji de severidad.
        #
        # Con teclado desde el 2-oct-2026: es la notificación más frecuente del sistema y
        # era la única que llegaba sin un solo botón. Avisaba que un sitio se cayó y para
        # ver qué le pasaba había que volver al menú y escribir `/farmacia CODIGO` —
        # justo lo que nadie hace a las 3 de la mañana. Se ofrecen las CAÍDAS y no las
        # recuperadas: una que volvió ya no necesita que nadie la mire.
        _enviar_telegram(
            chat_telegram, f'{asunto}\n\n{cuerpo}',
            teclado=teclado_para_farmacias([e.farmacia.codigo for e in caidos]) or None,
        )

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
