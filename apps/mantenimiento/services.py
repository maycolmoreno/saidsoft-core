"""Transiciones de estado del ciclo de vida de un Mantenimiento.

Mismo patrón que apps/activos/services.py: cada función muta el modelo y
crea el EventoMantenimiento inmutable correspondiente.
"""
from datetime import timedelta

from django.utils import timezone

from apps.activos import services as activos_services

from .models import (
    ActividadRealizada, EventoMantenimiento, Mantenimiento, MantenimientoEquipo, ResultadoTecnico,
    TipoOrigenMantenimiento,
)


def _snapshot_equipo(equipo):
    return {'codigo': equipo.codigo, 'serie': equipo.numero_serie, 'modelo': equipo.modelo}


def crear_mantenimiento_manual(*, equipos, tecnico, descripcion, fecha_programada, usuario,
                                cliente=None, empresa=None, tipo_mantenimiento='', equipo_principal=None):
    if not equipos:
        raise ValueError('Un mantenimiento debe tener al menos un equipo.')
    principal = equipo_principal or equipos[0]
    mantenimiento = Mantenimiento.objects.create(
        cliente=cliente, tecnico=tecnico, empresa=empresa, descripcion=descripcion,
        tipo_mantenimiento=tipo_mantenimiento, tipo_origen=TipoOrigenMantenimiento.MANUAL,
        fecha_programada=fecha_programada, snapshot_equipo=_snapshot_equipo(principal),
    )
    MantenimientoEquipo.objects.bulk_create([
        MantenimientoEquipo(mantenimiento=mantenimiento, equipo=equipo, es_principal=(equipo.pk == principal.pk))
        for equipo in equipos
    ])
    EventoMantenimiento.objects.create(
        mantenimiento=mantenimiento, tipo_evento=EventoMantenimiento.TipoEvento.PROGRAMADO, usuario=usuario,
        detalle={'equipos': [e.codigo for e in equipos], 'tecnico': tecnico.username if tecnico else None},
    )
    return mantenimiento


def iniciar_mantenimiento(*, mantenimiento, usuario):
    if mantenimiento.estado_interno != Mantenimiento.EstadoInterno.PENDIENTE:
        raise ValueError('Solo un mantenimiento pendiente puede iniciarse.')
    mantenimiento.estado_interno = Mantenimiento.EstadoInterno.EN_PROCESO
    mantenimiento.save(update_fields=['estado_interno'])
    EventoMantenimiento.objects.create(
        mantenimiento=mantenimiento, tipo_evento=EventoMantenimiento.TipoEvento.INICIADO, usuario=usuario,
    )


def registrar_actividad_checklist(*, mantenimiento, actividad, realizada, usuario):
    actividad_realizada, _ = ActividadRealizada.objects.update_or_create(
        mantenimiento=mantenimiento, actividad=actividad, defaults={'realizada': realizada},
    )
    EventoMantenimiento.objects.create(
        mantenimiento=mantenimiento, tipo_evento=EventoMantenimiento.TipoEvento.CHECKLIST_ACTUALIZADO, usuario=usuario,
        detalle={'actividad': actividad.nombre, 'realizada': realizada},
    )
    return actividad_realizada


def cerrar_mantenimiento(*, mantenimiento, resultado_tecnico, usuario):
    if mantenimiento.estado_interno == Mantenimiento.EstadoInterno.CERRADO:
        raise ValueError('El mantenimiento ya está cerrado.')
    if mantenimiento.estado_interno == Mantenimiento.EstadoInterno.CANCELADO:
        raise ValueError('Un mantenimiento cancelado no puede cerrarse.')
    mantenimiento.estado_interno = Mantenimiento.EstadoInterno.CERRADO
    mantenimiento.resultado_tecnico = resultado_tecnico
    mantenimiento.fecha_cierre = timezone.now()
    mantenimiento.cerrado_por = usuario
    mantenimiento.save(update_fields=['estado_interno', 'resultado_tecnico', 'fecha_cierre', 'cerrado_por'])
    EventoMantenimiento.objects.create(
        mantenimiento=mantenimiento, tipo_evento=EventoMantenimiento.TipoEvento.CERRADO, usuario=usuario,
        detalle={'resultado_tecnico': resultado_tecnico},
    )
    if resultado_tecnico == ResultadoTecnico.REQUIERE_BAJA:
        for me in mantenimiento.equipos.select_related('equipo'):
            activos_services.registrar_baja_recomendada(
                activo=me.equipo, motivo=f'Mantenimiento #{mantenimiento.pk}: requiere baja', usuario=usuario,
            )


def cancelar_mantenimiento(*, mantenimiento, motivo, usuario):
    if mantenimiento.estado_interno in (Mantenimiento.EstadoInterno.CERRADO, Mantenimiento.EstadoInterno.CANCELADO):
        raise ValueError('Un mantenimiento cerrado o ya cancelado no puede cancelarse de nuevo.')
    mantenimiento.estado_interno = Mantenimiento.EstadoInterno.CANCELADO
    mantenimiento.save(update_fields=['estado_interno'])
    EventoMantenimiento.objects.create(
        mantenimiento=mantenimiento, tipo_evento=EventoMantenimiento.TipoEvento.CANCELADO, usuario=usuario,
        detalle={'motivo': motivo},
    )


def generar_proximo_mantenimiento_programado(*, programado, usuario=None):
    """Crea el siguiente Mantenimiento de una plantilla recurrente y avanza sus fechas.

    Pensado para ejecutarse desde el management command periódico
    `generar_mantenimientos_programados` (cron/Celery beat externo).
    """
    equipo = programado.equipo
    mantenimiento = Mantenimiento.objects.create(
        cliente=equipo.colaborador_actual, tecnico=programado.tecnico, mantenimiento_programado=programado,
        descripcion=f'Mantenimiento programado cada {programado.frecuencia_dias} días.',
        tipo_origen=TipoOrigenMantenimiento.PROGRAMADO,
        fecha_programada=timezone.now(), snapshot_equipo=_snapshot_equipo(equipo),
    )
    MantenimientoEquipo.objects.create(mantenimiento=mantenimiento, equipo=equipo, es_principal=True)
    EventoMantenimiento.objects.create(
        mantenimiento=mantenimiento, tipo_evento=EventoMantenimiento.TipoEvento.PROGRAMADO, usuario=usuario,
        detalle={'origen': 'programado', 'mantenimiento_programado_id': programado.pk},
    )
    hoy = timezone.now().date()
    programado.fecha_ultimo = hoy
    programado.fecha_proximo = hoy + timedelta(days=programado.frecuencia_dias)
    programado.save(update_fields=['fecha_ultimo', 'fecha_proximo'])
    return mantenimiento
