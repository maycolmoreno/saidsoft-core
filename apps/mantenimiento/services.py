"""Transiciones de estado del ciclo de vida de un Mantenimiento.

Mismo patrón que apps/activos/services.py: cada función muta el modelo y
crea el EventoMantenimiento inmutable correspondiente.
"""
from datetime import timedelta

from django.utils import timezone

from apps.activos import services as activos_services
from apps.activos.models import MovimientoInventario

from .models import (
    ActividadChecklist, ActividadPlanificada, ActividadRealizada, EventoMantenimiento, FirmaMantenimiento,
    ImagenMantenimiento, Mantenimiento, MantenimientoEquipo, MantenimientoProgramado, Notificacion,
    PrioridadActividad, RepuestoUtilizado, ResultadoTecnico, TipoOrigenMantenimiento,
)


def _snapshot_equipo(equipo):
    return {'codigo': equipo.codigo, 'serie': equipo.numero_serie, 'modelo': equipo.modelo}


def crear_mantenimiento_manual(*, equipos, tecnico, descripcion, fecha_programada, usuario,
                                cliente=None, tipo_mantenimiento='', equipo_principal=None,
                                estado_general='', mantenimiento_programado=None):
    if not equipos:
        raise ValueError('Un mantenimiento debe tener al menos un equipo.')

    conflictos = MantenimientoEquipo.objects.filter(
        equipo__in=equipos,
        mantenimiento__estado_interno__in=[
            Mantenimiento.EstadoInterno.PENDIENTE, Mantenimiento.EstadoInterno.EN_PROCESO,
        ],
    ).select_related('equipo').distinct()
    if conflictos.exists():
        codigos = sorted({me.equipo.codigo for me in conflictos})
        raise ValueError(
            f'Ya hay un mantenimiento abierto para: {", ".join(codigos)}. Debe finalizarlo antes de crear uno nuevo.',
        )

    principal = equipo_principal or equipos[0]
    mantenimiento = Mantenimiento.objects.create(
        cliente=cliente, tecnico=tecnico, descripcion=descripcion,
        tipo_mantenimiento=tipo_mantenimiento, tipo_origen=TipoOrigenMantenimiento.MANUAL,
        estado_general=estado_general, mantenimiento_programado=mantenimiento_programado,
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


def cerrar_mantenimiento(*, mantenimiento, resultado_tecnico, usuario, tiempo_real_minutos=None):
    if mantenimiento.estado_interno == Mantenimiento.EstadoInterno.CERRADO:
        raise ValueError('El mantenimiento ya está cerrado.')
    if mantenimiento.estado_interno == Mantenimiento.EstadoInterno.CANCELADO:
        raise ValueError('Un mantenimiento cancelado no puede cerrarse.')
    mantenimiento.estado_interno = Mantenimiento.EstadoInterno.CERRADO
    mantenimiento.resultado_tecnico = resultado_tecnico
    mantenimiento.fecha_cierre = timezone.now()
    mantenimiento.cerrado_por = usuario
    mantenimiento.tiempo_real_minutos = tiempo_real_minutos
    mantenimiento.save(update_fields=[
        'estado_interno', 'resultado_tecnico', 'fecha_cierre', 'cerrado_por', 'tiempo_real_minutos',
    ])
    EventoMantenimiento.objects.create(
        mantenimiento=mantenimiento, tipo_evento=EventoMantenimiento.TipoEvento.CERRADO, usuario=usuario,
        detalle={'resultado_tecnico': resultado_tecnico},
    )
    if resultado_tecnico in (ResultadoTecnico.REQUIERE_BAJA, ResultadoTecnico.IRREPARABLE):
        for me in mantenimiento.equipos.select_related('equipo'):
            activos_services.registrar_baja_recomendada(
                activo=me.equipo, motivo=f'Mantenimiento #{mantenimiento.pk}: {resultado_tecnico}', usuario=usuario,
            )

    if mantenimiento.mantenimiento_programado_id:
        programado = mantenimiento.mantenimiento_programado
        hoy = mantenimiento.fecha_cierre.date()
        programado.fecha_ultimo = hoy
        programado.fecha_proximo = hoy + timedelta(days=programado.frecuencia_dias)
        programado.save(update_fields=['fecha_ultimo', 'fecha_proximo'])


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


def generar_mantenimientos_vencidos() -> int:
    """Recorre los MantenimientoProgramado vencidos (fecha_proximo <= hoy) y genera el
    siguiente Mantenimiento de cada uno. La llaman tanto el comando manual
    (`generar_mantenimientos_programados`) como la tarea periódica de Celery."""
    from django.db import transaction

    with transaction.atomic():
        hoy = timezone.now().date()
        vencidos = MantenimientoProgramado.objects.filter(activo=True, fecha_proximo__lte=hoy)
        total = 0
        for programado in vencidos:
            generar_proximo_mantenimiento_programado(programado=programado)
            total += 1
    return total


def firmar_mantenimiento(*, mantenimiento, tipo_firma, firma_base64, usuario, ip_origen=None):
    firma = FirmaMantenimiento.objects.create(
        mantenimiento=mantenimiento, tipo_firma=tipo_firma, firma_base64=firma_base64,
        ip_origen=ip_origen, firmado_por=usuario,
    )
    EventoMantenimiento.objects.create(
        mantenimiento=mantenimiento, tipo_evento=EventoMantenimiento.TipoEvento.FIRMADO, usuario=usuario,
        detalle={'tipo_firma': tipo_firma},
    )
    return firma


def adjuntar_imagen_mantenimiento(*, mantenimiento, archivo, usuario):
    imagen = ImagenMantenimiento.objects.create(
        mantenimiento=mantenimiento, imagen=archivo, nombre_archivo=archivo.name, tamanio_bytes=archivo.size,
    )
    EventoMantenimiento.objects.create(
        mantenimiento=mantenimiento, tipo_evento=EventoMantenimiento.TipoEvento.IMAGEN_ADJUNTADA, usuario=usuario,
        detalle={'nombre_archivo': imagen.nombre_archivo},
    )
    return imagen


def registrar_repuesto_utilizado(*, mantenimiento, tipo_consumible, cantidad, usuario, bodega=None, costo_unitario=None):
    """Registra un repuesto/consumible usado en la intervención. Si se indica `bodega`,
    descuenta el stock real (apps.activos.services.registrar_salida_stock, que lanza
    ValueError si no alcanza) y deja el kardex (MovimientoInventario) — si no se indica,
    es un repuesto fuera del flujo de bodega, solo se registra el costo."""
    if cantidad <= 0:
        raise ValueError('La cantidad debe ser mayor a cero.')

    if bodega is not None:
        activos_services.registrar_salida_stock(bodega=bodega, tipo_consumible=tipo_consumible, cantidad=cantidad)
        MovimientoInventario.objects.create(
            tipo_movimiento=MovimientoInventario.TipoMovimiento.SALIDA_CONSUMO,
            tipo_consumible=tipo_consumible, cantidad=-cantidad, bodega_origen=bodega,
            realizado_por=usuario, motivo=f'Mantenimiento #{mantenimiento.pk}',
        )

    repuesto = RepuestoUtilizado.objects.create(
        mantenimiento=mantenimiento, tipo_consumible=tipo_consumible, bodega=bodega, cantidad=cantidad,
        costo_unitario=costo_unitario, registrado_por=usuario,
    )
    EventoMantenimiento.objects.create(
        mantenimiento=mantenimiento, tipo_evento=EventoMantenimiento.TipoEvento.REPUESTO_REGISTRADO, usuario=usuario,
        detalle={'tipo_consumible': tipo_consumible.nombre, 'cantidad': cantidad},
    )
    return repuesto


def crear_actividad_planificada(*, tecnico, creado_por, titulo, descripcion, tipo_actividad, fecha_inicio, fecha_fin,
                                 prioridad=None, mantenimiento=None, mantenimiento_programado=None, equipo=None,
                                 ubicacion=None, tiempo_estimado_minutos=None):
    return ActividadPlanificada.objects.create(
        tecnico=tecnico, creado_por=creado_por, titulo=titulo, descripcion=descripcion,
        tipo_actividad=tipo_actividad, prioridad=prioridad or PrioridadActividad.NORMAL,
        fecha_inicio=fecha_inicio, fecha_fin=fecha_fin, mantenimiento=mantenimiento,
        mantenimiento_programado=mantenimiento_programado, equipo=equipo, ubicacion=ubicacion,
        tiempo_estimado_minutos=tiempo_estimado_minutos,
    )


def completar_actividad_planificada(*, actividad, tiempo_real_minutos=None):
    if actividad.estado == ActividadPlanificada.Estado.COMPLETADA:
        raise ValueError('La actividad ya está completada.')
    actividad.estado = ActividadPlanificada.Estado.COMPLETADA
    actividad.fecha_completada = timezone.now()
    actividad.tiempo_real_minutos = tiempo_real_minutos
    actividad.save(update_fields=['estado', 'fecha_completada', 'tiempo_real_minutos'])
    return actividad


def _resolver_ruta_local(uri, rel):
    """link_callback de xhtml2pdf: traduce una URL de /media/ o /static/ a una ruta de
    archivo local, porque el renderizador de PDF no puede resolver URLs relativas como
    hace un navegador. Receta estándar de xhtml2pdf+Django."""
    from django.conf import settings

    media_url = settings.MEDIA_URL.lstrip('/')
    static_url = settings.STATIC_URL.lstrip('/')
    uri_limpia = uri.lstrip('/')
    if uri_limpia.startswith(media_url):
        return str(settings.MEDIA_ROOT / uri_limpia[len(media_url):])
    if settings.STATIC_ROOT and uri_limpia.startswith(static_url):
        return str(settings.STATIC_ROOT / uri_limpia[len(static_url):])
    return uri


def generar_informe_pdf(*, mantenimiento) -> Mantenimiento:
    """Renderiza la orden de trabajo a PDF y la guarda en informe_pdf.

    Pensada para correr como tarea Celery (apps.mantenimiento.tasks.generar_informe_pdf_task):
    es el primer caso real de generación de PDF/reporte pesado movido a la cola async (ver
    config/celery.py) en vez de bloquear el request que la dispara.
    """
    from io import BytesIO

    from django.core.files.base import ContentFile
    from django.template.loader import render_to_string
    from xhtml2pdf import pisa

    checklist_items = ActividadChecklist.objects.filter(activo=True).order_by('orden', 'nombre')
    realizadas = {ar.actividad_id: ar.realizada for ar in mantenimiento.actividades_realizadas.all()}
    generado_en = timezone.now()

    html = render_to_string('panel/mantenimiento_informe_pdf.html', {
        'mantenimiento': mantenimiento,
        'equipos': mantenimiento.equipos.select_related('equipo'),
        'checklist': [{'item': item, 'realizada': realizadas.get(item.pk, False)} for item in checklist_items],
        'firmas': mantenimiento.firmas.select_related('firmado_por').order_by('tipo_firma'),
        'imagenes': mantenimiento.imagenes.all(),
        'repuestos': mantenimiento.repuestos_utilizados.select_related('tipo_consumible', 'bodega'),
        'costo_total_repuestos': mantenimiento.costo_total_repuestos,
        'generado_en': generado_en,
    })

    buffer = BytesIO()
    resultado = pisa.CreatePDF(html, dest=buffer, link_callback=_resolver_ruta_local)
    if resultado.err:
        raise RuntimeError(f'No se pudo generar el PDF del mantenimiento #{mantenimiento.pk} ({resultado.err} error(es)).')

    mantenimiento.informe_pdf.save(
        f'mantenimiento_{mantenimiento.pk}.pdf', ContentFile(buffer.getvalue()), save=False,
    )
    mantenimiento.informe_pdf_generado_en = generado_en
    mantenimiento.save(update_fields=['informe_pdf', 'informe_pdf_generado_en'])
    EventoMantenimiento.objects.create(
        mantenimiento=mantenimiento, tipo_evento=EventoMantenimiento.TipoEvento.INFORME_GENERADO,
    )
    return mantenimiento


def notificar(*, usuario, mensaje, url='', mantenimiento=None, actividad_planificada=None):
    """Bandeja de notificaciones in-app; no genera EventoMantenimiento (no es un hecho de negocio a auditar)."""
    return Notificacion.objects.create(
        usuario=usuario, mensaje=mensaje, url=url, mantenimiento=mantenimiento,
        actividad_planificada=actividad_planificada,
    )
