from .connectors import obtener_conector
from .models import DireccionSync, EstadoSync, SincronizacionExterna


def _resolver_unidad_negocio(objeto):
    """Misma heurística que apps.auditoria._resolver_unidad_negocio: FK/propiedad directa,
    farmacia, estación, equipo (Activo). Duplicada a propósito — cada app de la capa de
    eventos inmutables resuelve el tenant de forma autónoma, sin depender de otra app."""
    if objeto is None:
        return None
    directa = getattr(objeto, 'unidad_negocio', None)
    if directa is not None:
        return directa
    farmacia = getattr(objeto, 'farmacia', None)
    if farmacia is not None:
        return farmacia.unidad_negocio
    estacion = getattr(objeto, 'estacion', None)
    if estacion is not None:
        return estacion.farmacia.unidad_negocio
    equipo = getattr(objeto, 'equipo', None)
    if equipo is not None:
        return getattr(equipo, 'unidad_negocio', None)
    return None


def registrar_sync_pendiente(*, conector, objeto, direccion=DireccionSync.SALIENTE, payload=None) -> SincronizacionExterna:
    """Encola `objeto` para sincronizar con `conector`. Idempotente por (conector, objeto):
    si ya existe una SincronizacionExterna para ese par la reutiliza y la vuelve a poner en
    pendiente en vez de crear una fila duplicada — el historial de intentos previos queda
    intacto en EventoSyncExterno.
    """
    sincronizacion, _ = SincronizacionExterna.objects.update_or_create(
        conector=conector, modelo=objeto._meta.label, objeto_id=str(objeto.pk),
        defaults=dict(
            direccion=direccion,
            objeto_repr=str(objeto),
            unidad_negocio=_resolver_unidad_negocio(objeto),
            estado=EstadoSync.PENDIENTE,
            payload=payload or {},
        ),
    )
    sincronizacion.eventos.create(estado=EstadoSync.PENDIENTE, detalle='Encolado para sincronización')
    return sincronizacion


def ejecutar_sync(sincronizacion: SincronizacionExterna) -> SincronizacionExterna:
    """Invoca al conector registrado para `sincronizacion` y dice el resultado, tanto en el
    estado actual (SincronizacionExterna) como en la línea de tiempo (EventoSyncExterno).
    Es la única función que llama a conector.enviar() — tanto el uso directo (síncrono)
    como sincronizar_task (Celery) pasan por acá. Relanza la excepción del conector después
    de registrar el error, para que el llamador (ej. Celery) decida si reintentar.
    """
    sincronizacion.intentos += 1
    try:
        conector = obtener_conector(sincronizacion.conector)
        respuesta = conector.enviar(sincronizacion) or {}
    except Exception as exc:
        sincronizacion.estado = EstadoSync.ERROR
        sincronizacion.ultimo_error = str(exc)
        sincronizacion.save(update_fields=['estado', 'intentos', 'ultimo_error', 'actualizado_en'])
        sincronizacion.eventos.create(estado=EstadoSync.ERROR, detalle=str(exc))
        raise

    sincronizacion.estado = EstadoSync.ENVIADO
    sincronizacion.respuesta = respuesta
    sincronizacion.ultimo_error = ''
    sincronizacion.save(update_fields=['estado', 'intentos', 'respuesta', 'ultimo_error', 'actualizado_en'])
    sincronizacion.eventos.create(estado=EstadoSync.ENVIADO, respuesta=respuesta)
    return sincronizacion
