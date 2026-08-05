"""Ejecución de scripts contra un destino de estaciones.

Sigue el mismo patrón que apps/despliegues/services.py: la lógica de negocio
vive aquí, separada de las vistas, y reusa apps.catalogo.services para la
resolución de destino y el envío por MQTT.
"""
from datetime import timedelta

from django.utils import timezone

from apps.catalogo.services import enviar_script, resolver_estaciones

from .models import EjecucionScript, ResultadoEjecucionScript, Script


def crear_script_adhoc(*, nombre, tipo, contenido, unidad_negocio, usuario):
    return Script.objects.create(
        nombre=nombre, tipo=tipo, contenido=contenido, unidad_negocio=unidad_negocio,
        es_adhoc=True, creado_por=usuario,
    )


def registrar_ejecucion_script(*, script, destino_tipo, usuario, unidad_negocio, timeout_segundos=300,
                                grupos=None, farmacias=None, estaciones=None, parametros=None, programado=None):
    ejecucion = EjecucionScript(
        script=script, contenido_snapshot=script.contenido, destino_tipo=destino_tipo,
        unidad_negocio=unidad_negocio, timeout_segundos=timeout_segundos, programado=programado,
        parametros=parametros or {}, creado_por=usuario,
    )
    ejecucion.save()
    if destino_tipo == EjecucionScript.DestinoTipo.GRUPOS:
        ejecucion.grupos.set(grupos or [])
    elif destino_tipo == EjecucionScript.DestinoTipo.FARMACIAS:
        ejecucion.farmacias.set(farmacias or [])
    elif destino_tipo == EjecucionScript.DestinoTipo.ESTACIONES:
        ejecucion.estaciones.set(estaciones or [])

    estaciones_destino = resolver_estaciones(
        destino_tipo, unidad_negocio=unidad_negocio, grupos=ejecucion.grupos.all(),
        farmacias=ejecucion.farmacias.all(), estaciones=ejecucion.estaciones.all(),
    )
    resultados = ResultadoEjecucionScript.objects.bulk_create([
        ResultadoEjecucionScript(ejecucion=ejecucion, estacion=estacion)
        for estacion in estaciones_destino
    ])

    for resultado in resultados:
        enviado = enviar_script(
            resultado.estacion, ejecucion_id=ejecucion.pk, resultado_id=resultado.pk,
            tipo_script=script.tipo, contenido=ejecucion.contenido_snapshot,
            timeout_segundos=ejecucion.timeout_segundos,
        )
        resultado.estado = (
            ResultadoEjecucionScript.Estado.ENVIADO if enviado else ResultadoEjecucionScript.Estado.ERROR
        )
        resultado.fecha_envio = timezone.now()
        resultado.save(update_fields=['estado', 'fecha_envio'])

    recalcular_estado_ejecucion(ejecucion)
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
    hoy = timezone.now().date()
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
        hoy = timezone.now().date()
        vencidos = ScriptProgramado.objects.filter(activo=True, fecha_proxima_ejecucion__lte=hoy)
        total = 0
        for programado in vencidos:
            generar_ejecucion_programada(programado=programado)
            total += 1
    return total
