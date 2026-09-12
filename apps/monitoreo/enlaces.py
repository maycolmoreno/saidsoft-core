"""Sondeo ICMP del enlace de cada farmacia, y registro de sus caídas.

Por qué existe: todo el monitoreo de este proyecto depende de que la farmacia tenga un
**agente instalado**, y eso cubre 8 de ~1.800 estaciones. Un ping al equipo de borde no
necesita nada instalado del otro lado, así que cubre las ~704 sucursales desde el día
uno. Es la capacidad que tenía `Cresio_enlaces`, el sistema anterior, que estuvo vivo
hasta el 4-sep-2026 sondeando la flota completa (ver `docs/evaluacion-cresio-enlaces.md`).

**Dónde puede correr esto — leer antes de programarlo:**

`apps.monitoreo.mikrotik.sincronizar_ancho_banda_farmacias` ya aprendió esta lección por
las malas: el 24-ago-2026 se confirmó que el servidor **no tiene ninguna ruta de red
hacia las IP privadas de las farmacias** (100% de pérdida de ping, sin entrada en la
tabla de rutas del host). Esa función quedó como código que nunca puede funcionar desde
ahí.

Por eso este módulo **no se agrega a `CELERY_BEAT_SCHEDULE`**: correrlo en el servidor
central reportaría 704 farmacias caídas que no lo están. Se expone como comando
(`python manage.py sondear_enlaces`) para correrlo **desde un host que sí tenga ruta** —
el mismo desde el que corría `Cresio_enlaces`. Cuando se sepa cuál es ese host (acción
pendiente #1 de la evaluación), se decide si se programa ahí o si ese host reporta al
central por API.

La guarda de `sondear_enlaces_farmacias` es la red de seguridad de todo esto: si casi
todo el barrido falla, no registra nada. 704 farmacias no se caen a la vez; lo que se
cayó es la ruta desde donde se está sondeando.
"""
import logging
import platform
import re
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

# Si este porcentaje del barrido o más falla, se asume que el problema es la ruta desde
# donde se sondea, no 704 caídas simultáneas. Ver el docstring del módulo.
UMBRAL_BARRIDO_SOSPECHOSO_PCT = 80

_RE_LATENCIA = re.compile(r'(?:time|tiempo)[=<]\s*([\d.,]+)\s*ms', re.IGNORECASE)


def sondear_enlace(ip: str, timeout=TIMEOUT_SEGUNDOS) -> tuple[bool, float | None]:
    """Un ping. Devuelve `(alcanzable, latencia_ms)`.

    `latencia_ms` es None si no respondió, o si respondió pero no se pudo parsear el
    tiempo (la salida de `ping` cambia con el idioma del SO — por eso el regex acepta
    "time=" y "tiempo="; si igual no calza, el enlace se cuenta como vivo sin latencia,
    que es mejor que descartar un sondeo bueno por un problema de locale).
    """
    if platform.system() == 'Windows':
        comando = ['ping', '-n', '1', '-w', str(int(timeout * 1000)), ip]
    else:
        comando = ['ping', '-c', '1', '-W', str(int(timeout)), ip]

    try:
        proceso = subprocess.run(
            comando, capture_output=True, text=True, timeout=timeout + 3,
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
    from .models import EstadoEnlaceFarmacia, EventoEnlaceFarmacia

    ahora = timezone.now()
    estado, _ = EstadoEnlaceFarmacia.objects.get_or_create(farmacia=farmacia)
    estaba_caida = estado.alcanzable is False

    if alcanzable:
        estado.fallas_consecutivas = 0
        estado.latencia_ms = latencia_ms
        nuevo_alcanzable = True
    else:
        estado.fallas_consecutivas += 1
        estado.latencia_ms = None
        # Mientras no supere el umbral se conserva el estado anterior: null sigue siendo
        # null (nunca se sondeó) y una farmacia viva sigue viva. No se inventa un "caído"
        # con una sola pérdida de paquete.
        if estado.fallas_consecutivas >= EstadoEnlaceFarmacia.UMBRAL_FALLAS_CONSECUTIVAS:
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
            inicio=estado.ultimo_cambio_estado or ahora,
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
