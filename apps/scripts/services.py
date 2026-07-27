"""Ejecución de scripts contra un destino de estaciones.

Sigue el mismo patrón que apps/despliegues/services.py: la lógica de negocio
vive aquí, separada de las vistas, y reusa apps.catalogo.services para la
resolución de destino y el envío por MQTT.
"""
from django.utils import timezone

from apps.catalogo.services import enviar_script, resolver_estaciones

from .models import EjecucionScript, ResultadoEjecucionScript, Script


def crear_script_adhoc(*, nombre, tipo, contenido, usuario):
    return Script.objects.create(
        nombre=nombre, tipo=tipo, contenido=contenido, es_adhoc=True, creado_por=usuario,
    )


def registrar_ejecucion_script(*, script, destino_tipo, usuario, timeout_segundos=300,
                                grupos=None, farmacias=None, estaciones=None, parametros=None):
    ejecucion = EjecucionScript(
        script=script, contenido_snapshot=script.contenido, destino_tipo=destino_tipo,
        timeout_segundos=timeout_segundos, parametros=parametros or {}, creado_por=usuario,
    )
    ejecucion.save()
    if destino_tipo == EjecucionScript.DestinoTipo.GRUPOS:
        ejecucion.grupos.set(grupos or [])
    elif destino_tipo == EjecucionScript.DestinoTipo.FARMACIAS:
        ejecucion.farmacias.set(farmacias or [])
    elif destino_tipo == EjecucionScript.DestinoTipo.ESTACIONES:
        ejecucion.estaciones.set(estaciones or [])

    estaciones_destino = resolver_estaciones(
        destino_tipo, grupos=ejecucion.grupos.all(), farmacias=ejecucion.farmacias.all(),
        estaciones=ejecucion.estaciones.all(),
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
