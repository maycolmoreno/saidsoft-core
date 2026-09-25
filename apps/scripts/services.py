"""Ejecución de scripts contra un destino de estaciones.

Sigue el mismo patrón que apps/despliegues/services.py: la lógica de negocio
vive aquí, separada de las vistas, y reusa apps.catalogo.services para la
resolución de destino y el envío por MQTT.
"""
from datetime import timedelta

from django.db.models import DateTimeField, ExpressionWrapper, F
from django.utils import timezone

from apps.catalogo.services import enviar_script, resolver_estaciones

from .models import EjecucionScript, ResultadoEjecucionScript, Script

# Correr código arbitrario contra toda la cadena/grupos/farmacias es la misma superficie
# de riesgo que un Despliegue a esos destinos: requiere aprobación de un segundo usuario
# (regla de cuatro ojos, permiso `aprobar_ejecucionscript`). ESTACIONES queda afuera
# porque ya es un destino angosto (p.ej. instalar el agente en una sola estación puntual).
DESTINOS_QUE_REQUIEREN_APROBACION = {
    EjecucionScript.DestinoTipo.CADENA, EjecucionScript.DestinoTipo.GRUPOS, EjecucionScript.DestinoTipo.FARMACIAS,
}


def crear_script_adhoc(*, nombre, tipo, contenido, unidad_negocio, usuario):
    return Script.objects.create(
        nombre=nombre, tipo=tipo, contenido=contenido, unidad_negocio=unidad_negocio,
        es_adhoc=True, creado_por=usuario,
    )


def registrar_ejecucion_script(*, script, destino_tipo, usuario, unidad_negocio, timeout_segundos=300,
                                grupos=None, farmacias=None, estaciones=None, parametros=None, programado=None,
                                omitir_aprobacion=False):
    # Las ejecuciones generadas por un ScriptProgramado ya pasaron su propio control de
    # cuatro ojos al crear la política (permiso scripts.add_scriptprogramado) — exigir
    # aprobación en cada disparo automático rompería la recurrencia sin agregar gobernanza real.
    # `omitir_aprobacion` es para comandos de gestión que ya corren con acceso de shell al
    # servidor de producción (p.ej. cambiar_nodo_pos) — ese acceso es un control más fuerte
    # que el permiso del panel que esta aprobación reemplaza, así que exigirla ahí también
    # sería gobernanza de cartón, no real.
    requiere_aprobacion = (
        not omitir_aprobacion and programado is None and destino_tipo in DESTINOS_QUE_REQUIEREN_APROBACION
    )
    ejecucion = EjecucionScript(
        script=script, contenido_snapshot=script.contenido, destino_tipo=destino_tipo,
        unidad_negocio=unidad_negocio, timeout_segundos=timeout_segundos, programado=programado,
        parametros=parametros or {}, creado_por=usuario,
        estado=(
            EjecucionScript.Estado.PENDIENTE_APROBACION if requiere_aprobacion else EjecucionScript.Estado.PENDIENTE
        ),
    )
    ejecucion.save()
    if destino_tipo == EjecucionScript.DestinoTipo.GRUPOS:
        ejecucion.grupos.set(grupos or [])
    elif destino_tipo == EjecucionScript.DestinoTipo.FARMACIAS:
        ejecucion.farmacias.set(farmacias or [])
    elif destino_tipo == EjecucionScript.DestinoTipo.ESTACIONES:
        ejecucion.estaciones.set(estaciones or [])

    if not requiere_aprobacion:
        _publicar_ejecucion(ejecucion)
    return ejecucion


def _publicar_ejecucion(ejecucion):
    """Resuelve el destino y envía el script por MQTT a cada estación. Se llama al crear
    una ejecución que no necesita aprobación, y desde `aprobar_ejecucion_script` una vez
    que un segundo usuario aprobó una que sí la necesitaba."""
    estaciones_destino = resolver_estaciones(
        ejecucion.destino_tipo, unidad_negocio=ejecucion.unidad_negocio, grupos=ejecucion.grupos.all(),
        farmacias=ejecucion.farmacias.all(), estaciones=ejecucion.estaciones.all(),
    )
    resultados = ResultadoEjecucionScript.objects.bulk_create([
        ResultadoEjecucionScript(ejecucion=ejecucion, estacion=estacion)
        for estacion in estaciones_destino
    ])

    for resultado in resultados:
        enviado = enviar_script(
            resultado.estacion, ejecucion_id=ejecucion.pk, resultado_id=resultado.pk,
            tipo_script=ejecucion.script.tipo, contenido=ejecucion.contenido_snapshot,
            timeout_segundos=ejecucion.timeout_segundos,
        )
        resultado.estado = (
            ResultadoEjecucionScript.Estado.ENVIADO if enviado else ResultadoEjecucionScript.Estado.ERROR
        )
        resultado.fecha_envio = timezone.now()
        resultado.save(update_fields=['estado', 'fecha_envio'])

    recalcular_estado_ejecucion(ejecucion)


def aprobar_ejecucion_script(*, ejecucion, usuario):
    if ejecucion.estado != EjecucionScript.Estado.PENDIENTE_APROBACION:
        raise ValueError('Esta ejecución ya no está pendiente de aprobación.')
    if ejecucion.creado_por_id == usuario.id:
        raise ValueError('Quien crea la ejecución no puede aprobarla (regla de cuatro ojos).')
    ejecucion.aprobado_por = usuario
    ejecucion.estado = EjecucionScript.Estado.PENDIENTE
    ejecucion.save(update_fields=['aprobado_por', 'estado'])
    _publicar_ejecucion(ejecucion)
    return ejecucion


def recalcular_estado_ejecucion(ejecucion):
    estados = list(ejecucion.resultados.values_list('estado', flat=True))
    if not estados:
        return
    terminales = {
        ResultadoEjecucionScript.Estado.COMPLETADO, ResultadoEjecucionScript.Estado.ERROR,
        ResultadoEjecucionScript.Estado.TIMEOUT,
    }
    if not all(e in terminales for e in estados):
        nuevo_estado = EjecucionScript.Estado.EN_PROGRESO
    elif any(e in (ResultadoEjecucionScript.Estado.ERROR, ResultadoEjecucionScript.Estado.TIMEOUT) for e in estados):
        nuevo_estado = EjecucionScript.Estado.CON_ERRORES
    else:
        nuevo_estado = EjecucionScript.Estado.COMPLETADO
    if ejecucion.estado != nuevo_estado:
        ejecucion.estado = nuevo_estado
        ejecucion.save(update_fields=['estado'])


def generar_ejecucion_programada(*, programado):
    """Crea la EjecucionScript de un ScriptProgramado vencido y avanza sus fechas.

    Pensado para ejecutarse desde el management command periódico
    `generar_ejecuciones_programadas` (cron/Programador de tareas) — mismo patrón que
    apps.mantenimiento.services.generar_proximo_mantenimiento_programado.
    """
    ejecucion = registrar_ejecucion_script(
        script=programado.script, destino_tipo=programado.destino_tipo,
        unidad_negocio=programado.unidad_negocio, timeout_segundos=programado.timeout_segundos,
        grupos=programado.grupos.all(), farmacias=programado.farmacias.all(),
        estaciones=programado.estaciones.all(), usuario=programado.creado_por, programado=programado,
    )
    # localdate(): con now().date() la fecha sale en UTC y, despues de las 19:00 hora
    # local, la proxima ejecucion quedaba agendada un dia mas tarde de lo pedido.
    hoy = timezone.localdate()
    programado.fecha_ultima_ejecucion = hoy
    programado.fecha_proxima_ejecucion = hoy + timedelta(days=programado.frecuencia_dias)
    programado.save(update_fields=['fecha_ultima_ejecucion', 'fecha_proxima_ejecucion'])
    return ejecucion


def generar_ejecuciones_vencidas() -> int:
    """Recorre los ScriptProgramado vencidos (fecha_proxima_ejecucion <= hoy) y genera la
    siguiente EjecucionScript de cada uno. La llaman tanto el comando manual
    (`generar_ejecuciones_programadas`) como la tarea periódica de Celery."""
    from django.db import transaction

    from .models import ScriptProgramado

    with transaction.atomic():
        # Mismo motivo: en UTC, los programados de la noche se disparan un dia antes.
        hoy = timezone.localdate()
        vencidos = ScriptProgramado.objects.filter(activo=True, fecha_proxima_ejecucion__lte=hoy)
        total = 0
        for programado in vencidos:
            generar_ejecucion_programada(programado=programado)
            total += 1
    return total


# Margen sobre el timeout pedido antes de dar un resultado por vencido.
#
# El `timeout_segundos` que viaja en el comando es para el PROCESO en la estación, no para
# el viaje: el agente puede tardar en recibirlo (reconexión), en arrancarlo (otro script
# corriendo) y en que el reporte de vuelta llegue al worker. Cerrar apenas vence el plazo
# convertiría en "vencido" algo que estaba por contestar.
#
# Cinco minutos es holgado para los tres tramos y sigue siendo insignificante frente al
# problema que esto resuelve: el resultado mas viejo sin cerrar llevaba 39 DIAS con un
# timeout de 300 s.
MARGEN_VENCIMIENTO_SEGUNDOS = 300

# Hasta cuando el estado ACTUAL de la estacion sirve para explicar por que no contesto.
#
# El diagnostico mira lo que la estacion tiene AHORA. Eso vale mientras la condicion siga
# siendo la misma —y lo es, porque el barrido corre cada 10 min y el plazo tipico son
# 10— pero deja de valer para algo que lleva dias colgado: la causa pudo resolverse en el
# medio y afirmar lo contrario es peor que no decir nada.
#
# Una hora: holgado frente al ciclo del barrido y corto frente a "esto quedo abandonado".
VENTANA_DIAGNOSTICO_CONFIABLE_SEGUNDOS = 3600


def _motivo_sin_respuesta(resultado) -> str:
    """Por qué esta estación no contestó, mirando su estado AHORA.

    Es lo que faltaba: hasta el 23-sep-2026 una ejecución se quedaba "en progreso" para
    siempre y la pantalla no daba una sola pista. El dato ya existía —el desfase del
    reloj, la pausa, el último latido— pero vivía en la ficha de la estación y nadie iba
    a cruzarlo a mano.

    Es un diagnóstico a posteriori, no una certeza: se mira el estado actual, que puede no
    ser el que tenía cuando se le mandó el comando. Por eso el texto dice qué se observa,
    no qué pasó.
    """
    from apps.catalogo.models import Estacion

    e = resultado.estacion

    # Si el comando se mandó hace mucho más que el plazo, el estado ACTUAL no dice nada
    # sobre lo que pasaba entonces, y afirmarlo es peor que no decir nada.
    #
    # Se vio al desplegar esto el 25-sep-2026: los 21 resultados viejos de la cola se
    # cerraron con "la estación está en línea y no contestó", cuando la causa real había
    # sido el reloj corrido de días antes — que para ese momento ya estaba corregido. El
    # mensaje era correcto sobre el presente y falso sobre el incidente.
    antiguedad = (timezone.now() - resultado.fecha_envio).total_seconds()
    if antiguedad > VENTANA_DIAGNOSTICO_CONFIABLE_SEGUNDOS:
        dias = int(antiguedad // 86400)
        cuanto = f'{dias} día(s)' if dias else f'{int(antiguedad // 3600)} hora(s)'
        return (
            f'Se envió hace {cuanto} y recién se cierra ahora: lo que la estación tenga '
            'hoy no dice qué pasaba entonces. No hay diagnóstico confiable para este caso.'
        )

    if e.estado_aprobacion != Estacion.EstadoAprobacion.APROBADA:
        return f'La estación no está aprobada ({e.get_estado_aprobacion_display()}).'
    if e.pausado:
        return 'La estación está pausada: no ejecuta comandos hasta que se la reanude.'

    desfase = e.desfase_reloj_segundos
    if desfase is not None and abs(desfase) > Estacion.UMBRAL_RELOJ_INCOMUNICADO_SEGUNDOS:
        return (
            f'El reloj de la estación está corrido {desfase:+d} s, más de los '
            f'{Estacion.UMBRAL_RELOJ_INCOMUNICADO_SEGUNDOS} s de la ventana de firma: '
            'descarta TODO comando, incluido este.'
        )

    if resultado.estado == resultado.Estado.EJECUTANDO:
        return (
            'La estación empezó a ejecutarlo y nunca reportó el final: el script se colgó '
            'o el agente se reinició a mitad de camino.'
        )

    if e.estado_conexion != Estacion.EstadoConexion.ONLINE:
        return (
            'La estación no está en línea. El tópico de comandos NO es retenido, así que '
            'lo publicado mientras estaba apagada se perdió: hay que volver a lanzarlo.'
        )

    return (
        'La estación está en línea y no contestó. Revisar su log local '
        '(C:\\ProgramData\\Saidsoft\\agente_prueba.log): puede ser una firma que no '
        'validó o un hilo del agente caído.'
    )


def caducar_resultados_vencidos() -> int:
    """Cierra como TIMEOUT los resultados que pasaron su plazo sin respuesta.

    Hacía falta porque **nadie lo hacía**. El `timeout_segundos` viaja al agente, que lo
    aplica al proceso que lanza; si el comando nunca llega —o llega y se descarta— no hay
    nada del lado servidor que lo cierre. El resultado queda en `enviado` y la ejecución
    en "En progreso" indefinidamente.

    Medido el 22-sep-2026: 12 resultados colgados, el más viejo de **948 horas (39 días)**
    con un timeout de 300 s.

    Se marca TIMEOUT y no ERROR a propósito: ERROR es lo que reporta el agente cuando el
    script corrió y salió mal. Esto es otra cosa —nunca supimos nada— y mezclarlas
    borraría la distinción justo en el estado que hay que investigar distinto.
    """
    from apps.scripts.models import EjecucionScript, ResultadoEjecucionScript

    ahora = timezone.now()
    # El vencimiento se calcula EN LA BASE, no en Python. Con 12 resultados abiertos da
    # lo mismo; un despliegue a ~1.800 estaciones deja 1.800 filas abiertas, y traerlas
    # todas cada 10 minutos para descartar casi todas es el mismo patron que la auditoria
    # ya marco en `_publicar_ejecucion`. Asi solo viajan las vencidas.
    #
    # El plazo sale de `ejecucion.timeout_segundos`, que vive en otra tabla: de ahi el F()
    # con aritmetica de intervalos, que Postgres resuelve sin problema (este proyecto es
    # Postgres-only, ver CLAUDE.md).
    vencidos = (
        ResultadoEjecucionScript.objects
        .filter(
            estado__in=[
                ResultadoEjecucionScript.Estado.PENDIENTE,
                ResultadoEjecucionScript.Estado.ENVIADO,
                ResultadoEjecucionScript.Estado.EJECUTANDO,
            ],
            fecha_envio__isnull=False,
        )
        .annotate(
            vence_en=ExpressionWrapper(
                F('fecha_envio')
                + F('ejecucion__timeout_segundos') * timedelta(seconds=1)
                + timedelta(seconds=MARGEN_VENCIMIENTO_SEGUNDOS),
                output_field=DateTimeField(),
            ),
        )
        .filter(vence_en__lt=ahora)
        .select_related('estacion', 'ejecucion')
    )

    cerrados = 0
    ejecuciones = set()
    for r in vencidos:
        # El motivo se calcula ANTES de pisar el estado: una de sus ramas distingue
        # "empezó y nunca reportó el final" de "nunca lo recibió", y eso se lee del
        # estado actual. Calcularlo después lo dejaba siempre en TIMEOUT y esa rama no
        # se alcanzaba nunca — lo encontró `test_si_empezo_y_no_termino_lo_distingue`.
        motivo = _motivo_sin_respuesta(r)[:400]
        r.estado = ResultadoEjecucionScript.Estado.TIMEOUT
        r.fecha_fin = ahora
        r.motivo_sin_respuesta = motivo
        r.save(update_fields=['estado', 'fecha_fin', 'motivo_sin_respuesta'])
        ejecuciones.add(r.ejecucion)
        cerrados += 1

    # Recién con todos los resultados cerrados se recalcula: hacerlo por resultado
    # dispararía un UPDATE por fila sobre la misma ejecución.
    for ejecucion in ejecuciones:
        recalcular_estado_ejecucion(ejecucion)

    return cerrados

