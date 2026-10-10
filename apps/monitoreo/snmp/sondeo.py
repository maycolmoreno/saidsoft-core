"""El motor de sondeo: elegir a quién le toca, leerlo y guardar. FASE 4.

**Solo visibilidad en v1: no crea `Alerta` ni notifica.** Es la misma decisión que tomó
`apps.monitoreo.mikrotik`, y por el mismo motivo: `Alerta.estacion` es una FK obligatoria a
`Estacion`, y medido el 7-oct-2026 **cero de las impresoras de producción tienen estación**.
Generalizar `Alerta` toca nueve funciones, el panel, el bot de Telegram y buena parte de las
pruebas — es un proyecto con su propia revisión de impacto (FASE 7), no un efecto colateral
de empezar a leer tóner.

El diseño, y por qué no es un solo task como el sondeo de ancho de banda:

    celery_beat  (una vez por clase de cadencia)
         │   elige los objetivos vencidos y los parte en lotes
         ▼
    Redis  ──>  N tareas `sondear_lote` en paralelo
         ▼
    celery_worker   cada lote: su propio asyncio loop + Semaphore acotado
         ▼
    LecturaSnmpActual (upsert)  +  estado del objetivo

`sincronizar_ancho_banda_farmacias` es **un** task que recorre ~700 farmacias con
`Semaphore(25)`. A 700 funciona. El problema es el peor caso, que además es el caso NORMAL
de noche con las farmacias cerradas: si todos los destinos están muertos, cada consulta
agota el timeout y el ciclo tarda `dispositivos × viajes × timeout / concurrencia`. Con
1.000 dispositivos y tres viajes eso son 360 s, y el ciclo es de 300: no cierra. Subir el
semáforo mueve el cuello al ancho de banda y a los descriptores del contenedor, no lo
elimina.

Con el abanico, la perilla de escalado pasa a ser **cuántos workers Celery hay**, que se
ajusta en el compose sin tocar código. El tiempo de un lote queda acotado y es medible —
ver `ProbarCargaSnmpTests`, que lo mide en vez de afirmarlo.
"""
import asyncio
import logging

from django.db import transaction
from django.utils import timezone

from apps.monitoreo.models import LecturaSnmpActual, ObjetivoSnmp

from . import leer
from .catalogo import Cadencia, catalogo_para

logger = logging.getLogger(__name__)

#: Objetivos por lote. 50 × 3 viajes × 3 s / 25 concurrentes ≈ 18 s en el peor caso, que
#: deja margen de sobra dentro de un ciclo de 5 minutos. Lotes más grandes concentran el
#: riesgo en una tarea; más chicos multiplican el costo fijo de encolar.
OBJETIVOS_POR_LOTE = 50

#: Concurrencia DENTRO de un lote. Mismo valor que `_MAX_SONDEOS_CONCURRENTES` de
#: mikrotik.py, que es el que ya está validado contra la red real.
MAX_SONDEOS_CONCURRENTES = 25

#: Si un lote falla casi entero, no se escribe nada. Es la guarda de
#: `sondear_enlaces_farmacias` (`UMBRAL_BARRIDO_SOSPECHOSO_PCT`), y existe por lo mismo:
#: 50 equipos no se caen a la vez — lo que se cayó es la ruta desde donde se sondea. Sin
#: esto, un cambio de red llena la base de fallas falsas y envejece todas las lecturas
#: parejo, que es un síntoma difícil de leer.
UMBRAL_LOTE_SOSPECHOSO_PCT = 80

#: Cada cuánto corre cada clase, en minutos. Lo que de verdad acota el volumen: con 10.000
#: dispositivos y 20 métricas cada 5 minutos son ~57 millones de filas por día; con estas
#: tres clases, ~4 millones.
MINUTOS_POR_CADENCIA = {
    Cadencia.RAPIDA: 5,
    Cadencia.LENTA: 60,
    Cadencia.IDENTIDAD: 60 * 24,
}


def objetivos_pendientes(cadencia, ahora=None):
    """Los `ObjetivoSnmp` a los que les toca esta cadencia, ya con perfil y activo.

    Un objetivo entra si nunca se leyó, o si pasó su intervalo **más** los ciclos de
    castigo que le corresponden por fallas consecutivas. El backoff se evalúa en Python y
    no en SQL a propósito: son un par de cientos de filas, y expresar `2 ** min(fallas, 5)`
    en el ORM lo vuelve ilegible sin ganar nada medible.

    `select_related` no es cosmético: sin él, leer `objetivo.perfil.comunidad()` dentro del
    bucle del lote es una consulta por dispositivo — el N+1 clásico, y justamente en el
    camino que tiene que escalar.
    """
    ahora = ahora or timezone.now()
    minutos = MINUTOS_POR_CADENCIA[cadencia]
    # El reloj de ESTA cadencia, no el compartido. Con uno solo, la cadencia rapida —que
    # corre cada 5 minutos— lo refresca permanentemente y la lenta nunca vence: el toner
    # no se leeria nunca. Lo descubrio la primera lectura real contra una Ricoh.
    campo = ObjetivoSnmp.campo_de_cadencia(cadencia)

    candidatos = (
        ObjetivoSnmp.objects
        .filter(habilitado=True)
        .select_related('perfil', 'activo')
        .order_by(campo)
    )
    pendientes = []
    for objetivo in candidatos:
        ultimo = getattr(objetivo, campo)
        if ultimo is None:
            pendientes.append(objetivo)
            continue
        espera = minutos * (1 + objetivo.ciclos_a_saltar)
        if (ahora - ultimo).total_seconds() >= espera * 60:
            pendientes.append(objetivo)
    return pendientes


def partir_en_lotes(objetivos, tamano=None):
    """Los ids en lotes. Ids y no objetos: lo que viaja por Redis tiene que ser chico y
    no quedar viejo — entre que Beat encola y el worker toma el lote, el objetivo puede
    haberse deshabilitado, y el worker lo relee.

    `tamano=None` y no `tamano=OBJETIVOS_POR_LOTE` como default: un default se evalúa al
    DEFINIR la función, así que quedaría congelado y no habría forma de cambiar el tamaño
    del lote sin editar este archivo — ni de probar el reparto con lotes chicos.
    """
    tamano = tamano or OBJETIVOS_POR_LOTE
    ids = [o.pk for o in objetivos]
    return [ids[i:i + tamano] for i in range(0, len(ids), tamano)]


def sondear_lote(ids, cadencia=None) -> dict:
    """Sondea un lote y guarda. Devuelve un resumen. **Nunca lanza.**

    Nunca lanza porque un lote es una tarea de Celery entre muchas: que una explote no
    puede dejar al resto del ciclo sin correr, y el reintento automático de Celery sobre
    un sondeo ya hecho solo duplica trabajo.
    """
    resumen = {'sondeados': 0, 'con_lectura': 0, 'fallados': 0, 'abortado': False, 'claves': 0}
    objetivos = list(
        ObjetivoSnmp.objects
        .filter(pk__in=ids, habilitado=True)
        .select_related('perfil', 'activo')
    )
    if not objetivos:
        return resumen

    cadencias = {cadencia} if cadencia else None
    try:
        resultados = asyncio.run(_leer_todos(objetivos, cadencias))
    except Exception:
        logger.exception('Lote SNMP abortado antes de leer nada (%d objetivos).', len(objetivos))
        resumen.update(abortado=True)
        return resumen

    resumen['sondeados'] = len(resultados)
    fallados = [o for o, lecturas in resultados if lecturas is None]
    pct_fallido = 100 * len(fallados) / len(resultados)

    # La guarda. Se evalúa ANTES de escribir: la diferencia entre una herramienta útil y
    # una que llena la base de fallas falsas la primera vez que alguien la corre desde el
    # lugar equivocado.
    if pct_fallido >= UMBRAL_LOTE_SOSPECHOSO_PCT and len(resultados) > 1:
        logger.error(
            'Lote SNMP abortado sin registrar nada: %d de %d fallaron (%.0f%%). '
            '50 equipos no se caen a la vez — revisar la ruta desde este host.',
            len(fallados), len(resultados), pct_fallido,
        )
        resumen.update(abortado=True, fallados=len(fallados))
        return resumen

    ahora = timezone.now()
    for objetivo, lecturas in resultados:
        if lecturas is None:
            _registrar_falla(objetivo, ahora, cadencia)
            resumen['fallados'] += 1
            continue
        # El guardado también va dentro de un try, y no es simetría decorativa: estaba
        # AFUERA y un `DataError` de PostgreSQL por un NUL en el texto de un equipo se
        # escapaba de `sondear_lote` —que promete no lanzar— y tumbaba el lote entero,
        # incluidos los equipos que se habían leído bien. Un firmware raro no puede dejar
        # sin lectura a los otros 49 del lote.
        try:
            resumen['claves'] += guardar_lecturas(objetivo, lecturas, ahora, cadencia)
            resumen['con_lectura'] += 1
        except Exception:
            logger.exception(
                'Objetivo SNMP %s: se leyó bien pero no se pudo guardar.', objetivo.pk,
            )
            resumen['fallados'] += 1
    return resumen


#: Campos que el upsert sobrescribe. `clave` y `objetivo` no están: son la identidad.
_CAMPOS_A_ACTUALIZAR = ('valor', 'valor_crudo', 'unidad', 'texto', 'actualizado_en')


def guardar_lecturas(objetivo, lecturas, ahora=None, cadencia=None) -> int:
    """Upsert de las lecturas y marca el éxito. Devuelve cuántas claves se escribieron.

    **Un solo INSERT con `ON CONFLICT DO UPDATE`, no un `update_or_create` por clave.**
    La diferencia la midió la prueba de carga: `update_or_create` cuesta ~7 consultas por
    clave entre el SELECT, el INSERT y los savepoints, y una impresora publica unas 15
    claves. Un lote de 50 equipos serían ~5.250 consultas — el guardado costaría más que
    el sondeo. Así es una por objetivo.

    Lo que lo hace posible es el `UniqueConstraint(objetivo, clave)`: sin él no hay sobre
    qué resolver el conflicto. Es el mismo motivo por el que ese constraint existe.

    Upsert y no borrar-e-insertar: una lectura que el equipo dejó de publicar se queda con
    su `actualizado_en` viejo, y eso **es información** —dice desde cuándo no se sabe de
    ella— mientras borrarla haría desaparecer el hecho sin dejar rastro.

    Todo dentro de una transacción: media lectura guardada dejaría el tóner negro nuevo
    junto al cian de hace una hora, y nadie podría saber cuál de los dos creer.
    """
    ahora = ahora or timezone.now()
    filas = [
        LecturaSnmpActual(
            objetivo=objetivo, clave=lectura.clave, valor=lectura.valor,
            valor_crudo=lectura.crudo, unidad=lectura.unidad,
            texto=lectura.texto[:200], actualizado_en=ahora,
        )
        for lectura in lecturas
    ]
    with transaction.atomic():
        if filas:
            LecturaSnmpActual.objects.bulk_create(
                filas,
                update_conflicts=True,
                unique_fields=('objetivo', 'clave'),
                update_fields=_CAMPOS_A_ACTUALIZAR,
            )
        campos = {
            'ultima_lectura': ahora, 'ultimo_exito': ahora,
            'fallas_consecutivas': 0, 'ultimo_error': '',
        }
        if cadencia:
            campos[ObjetivoSnmp.campo_de_cadencia(cadencia)] = ahora
        ObjetivoSnmp.objects.filter(pk=objetivo.pk).update(**campos)
    return len(filas)


# --- internos ---------------------------------------------------------------------

async def _leer_todos(objetivos, cadencias):
    """`[(objetivo, lecturas|None)]`, con la concurrencia acotada.

    `Semaphore` y no `ThreadPoolExecutor`: el cliente ya es asíncrono, y no hace falta un
    hilo por sondeo para no hacerlos en serie. Mismo criterio que `mikrotik.py`.
    """
    limite = asyncio.Semaphore(MAX_SONDEOS_CONCURRENTES)

    async def uno(objetivo):
        async with limite:
            return objetivo, await _leer_uno(objetivo, cadencias)

    return list(await asyncio.gather(*(uno(o) for o in objetivos)))


async def _leer_uno(objetivo, cadencias):
    """Las lecturas de un objetivo, o None. Nunca lanza."""
    try:
        comunidad = objetivo.perfil.comunidad()
    except ValueError:
        # La community está cifrada con otra clave. Es un problema de configuración, no
        # del equipo, y hay que poder distinguirlo de un timeout.
        logger.error(
            'Objetivo SNMP %s: la community del perfil "%s" no se pudo descifrar.',
            objetivo.pk, objetivo.perfil.nombre,
        )
        return None
    if not comunidad:
        logger.warning(
            'Objetivo SNMP %s: el perfil "%s" no tiene community cargada.',
            objetivo.pk, objetivo.perfil.nombre,
        )
        return None

    try:
        return await leer(
            str(objetivo.ip_sondeada), comunidad, catalogo_para(objetivo.catalogo),
            puerto=objetivo.perfil.puerto, cadencias=cadencias,
        )
    except Exception:
        # La IP nunca entra en el log: puede no ser sensible, pero la community sí viaja
        # cerca y el hábito de no loguear el contexto de una credencial se mantiene.
        logger.warning('Objetivo SNMP %s: excepción leyendo.', objetivo.pk, exc_info=True)
        return None


def _registrar_falla(objetivo, ahora, cadencia=None):
    """Suma una falla y clasifica el motivo. No borra las lecturas que ya estaban.

    No borrarlas es deliberado: lo último que se supo del equipo sigue siendo lo último
    que se supo, y su `actualizado_en` ya dice cuánto hace. Vaciarlas convertiría "no
    pudimos leer" en "no hay nada", que es una afirmación más fuerte y falsa.
    """
    codigo = (
        ObjetivoSnmp.Error.PERFIL_SIN_COMUNIDAD
        if not objetivo.perfil.comunidad_cifrada
        else ObjetivoSnmp.Error.TIMEOUT
    )
    campos = {
        'ultima_lectura': ahora,
        'fallas_consecutivas': objetivo.fallas_consecutivas + 1,
        'ultimo_error': codigo,
    }
    # El reloj de la cadencia se mueve TAMBIEN al fallar: si no, un equipo muerto queda
    # vencido para siempre y se reintenta en cada ciclo — el backoff no tendria efecto.
    if cadencia:
        campos[ObjetivoSnmp.campo_de_cadencia(cadencia)] = ahora
    ObjetivoSnmp.objects.filter(pk=objetivo.pk).update(**campos)
