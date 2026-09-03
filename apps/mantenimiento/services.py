"""Transiciones de estado del ciclo de vida de un Mantenimiento.

Mismo patrón que apps/activos/services.py: cada función muta el modelo y
crea el EventoMantenimiento inmutable correspondiente.
"""
from datetime import datetime, time as dt_time, timedelta

from django.db import transaction
from django.db.models import Avg, DurationField, ExpressionWrapper, F, Q, Sum
from django.utils import timezone

from apps.activos import services as activos_services
from apps.activos.models import Activo, MovimientoInventario

from .models import (
    AcuerdoNivelServicio, ActividadChecklist, ActividadPlanificada, ActividadRealizada, EstadoGeneralEquipo,
    EventoMantenimiento,
    FirmaMantenimiento, ImagenMantenimiento, Mantenimiento, MantenimientoEquipo, MantenimientoProgramado,
    Notificacion, PrioridadActividad, PrioridadMantenimiento, RepuestoUtilizado, ResultadoTecnico,
    TipoMantenimiento, TipoOrigenMantenimiento, VisitaTecnica,
)


def _tipo_mantenimiento(codigo):
    """Busca un TipoMantenimiento del catálogo por código -- nunca lanza si no existe
    (un administrador puede haber renombrado/desactivado el catálogo semilla), deja el
    Mantenimiento sin clasificar en vez de reventar el flujo que lo llama."""
    return TipoMantenimiento.objects.filter(codigo=codigo).first()

# Resultados que indican que el equipo vuelve a estar disponible: solo estos devuelven
# automáticamente un Activo de "En reparación" a "En bodega" al cerrar el mantenimiento.
# Los que quedan afuera (requiere_repuesto, escalado_a_proveedor, garantia_rechazada)
# significan que el equipo sigue roto/en curso en otro lado -- se deja "En reparación"
# a propósito, no hay todavía un flujo v1 para reabrir/encadenar un mantenimiento
# siguiente. requiere_baja/irreparable ya tienen su propio manejo (baja_recomendada,
# ver más abajo) y tampoco vuelven a bodega.
RESULTADOS_RETORNAN_A_BODEGA = frozenset({
    ResultadoTecnico.REPARADO, ResultadoTecnico.SIN_FALLA, ResultadoTecnico.SIN_INTERVENCION,
    ResultadoTecnico.PARCIALMENTE_REPARADO, ResultadoTecnico.GARANTIA_APLICADA,
    ResultadoTecnico.ACTUALIZADO, ResultadoTecnico.INSTALADO,
})

_ESTADO_FISICO_DESDE_GENERAL = {
    EstadoGeneralEquipo.OPERATIVO: Activo.EstadoFisico.BUENO,
    EstadoGeneralEquipo.REQUIERE_REVISION: Activo.EstadoFisico.REGULAR,
    EstadoGeneralEquipo.NO_OPERATIVO: Activo.EstadoFisico.MALO,
}


def _snapshot_equipo(equipo):
    return {'codigo': equipo.codigo, 'serie': equipo.numero_serie, 'modelo': equipo.modelo}


def crear_mantenimiento_manual(*, equipos, tecnico, descripcion, fecha_programada, usuario,
                                cliente=None, tipo_mantenimiento=None, equipo_principal=None,
                                estado_general='', mantenimiento_programado=None, prioridad=None):
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
        prioridad=prioridad or PrioridadMantenimiento.NORMAL,
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
    # Avisar al técnico que le asignaron trabajo. Hasta ahora la bandeja solo se
    # poblaba desde la tarea diaria de vencimientos, así que una asignación nueva no
    # generaba ningún aviso: el técnico se enteraba recién si entraba a mirar.
    #
    # No se avisa cuando alguien se asigna a sí mismo (autoservicio desde la app): ya
    # sabe que lo creó.
    if tecnico is not None and tecnico != usuario:
        codigos = ', '.join(e.codigo for e in equipos)
        notificar(
            usuario=tecnico,
            mensaje=f'Te asignaron un mantenimiento de {codigos}.',
            mantenimiento=mantenimiento,
        )
    return mantenimiento


@transaction.atomic
def iniciar_reparacion_desde_activo(*, activo, motivo, detalle_motivo, usuario):
    """Envía un Activo a reparación Y abre de una vez el Mantenimiento que lo cubre.

    Antes, "enviar a reparación" era solo un cambio de estado en Activo (motivo +
    nota de texto, nada más) sin ningún vínculo con apps.mantenimiento -- el módulo
    completo (checklist, firma, repuestos, informe PDF) quedaba desconectado de este
    flujo, aunque ya existía para todo lo demás. Se captura `cliente` ANTES de llamar
    a `registrar_envio_reparacion` porque esa función limpia `colaborador_actual`.
    `tecnico` queda sin asignar (asignable después desde el propio Mantenimiento) --
    quien envía a reparación desde Activos no es necesariamente quien la va a hacer.
    """
    cliente = activo.colaborador_actual
    activos_services.registrar_envio_reparacion(
        activo=activo, motivo=motivo, detalle_motivo=detalle_motivo, usuario=usuario,
    )
    return crear_mantenimiento_manual(
        equipos=[activo], tecnico=None, descripcion=detalle_motivo,
        tipo_mantenimiento=_tipo_mantenimiento('correctivo'),
        # Un equipo que se manda a reparar está fuera de servicio: no es un preventivo
        # de rutina, arranca en ALTA (ajustable después desde el propio mantenimiento).
        prioridad=PrioridadMantenimiento.ALTA,
        fecha_programada=timezone.now(), usuario=usuario, cliente=cliente,
    )


def _metros_entre(lat1, lon1, lat2, lon2) -> float:
    """Distancia en metros entre dos coordenadas (haversine).

    Trigonometría a mano en vez de PostGIS/GeoDjango: es una sola distancia
    punto-a-punto, no hace falta arrastrar una extensión de PostgreSQL ni las
    dependencias de GDAL para esto.
    """
    import math

    radio_tierra_m = 6_371_000
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (
        math.sin(dlat / 2) ** 2
        + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon / 2) ** 2
    )
    return radio_tierra_m * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def distancia_minima_a_farmacia(*, tecnico_id, farmacia, desde, hasta):
    """Distancia MÍNIMA en metros entre las posiciones GPS del técnico en la ventana
    [desde, hasta] y la farmacia, o None si no se puede determinar.

    Se toma la mínima de toda la ventana y no la posición del momento exacto del
    cierre: el técnico puede cerrar desde el auto ya saliendo, y eso no significa que
    no haya estado en el local.

    Nunca lanza: ninguna operación de cierre puede fallar porque falte GPS. Devuelve
    None ante cualquier dato ausente (sin técnico, farmacia sin coordenadas, sin
    posiciones registradas) -- 'sin datos' NO es lo mismo que "no fue".
    """
    from .models import UbicacionTecnico

    try:
        if tecnico_id is None or farmacia is None:
            return None
        if farmacia.latitud is None or farmacia.longitud is None:
            return None
        posiciones = UbicacionTecnico.objects.filter(
            usuario_id=tecnico_id, timestamp_captura__gte=desde, timestamp_captura__lte=hasta,
        ).values_list('latitud', 'longitud')
        distancias = [
            _metros_entre(float(lat), float(lon), farmacia.latitud, farmacia.longitud)
            for lat, lon in posiciones
        ]
        return round(min(distancias), 1) if distancias else None
    except Exception:
        import logging
        logging.getLogger(__name__).exception('No se pudo calcular la distancia del técnico a %s', farmacia)
        return None


def _verificar_presencia_en_sitio(mantenimiento):
    """Verificación GPS de un Mantenimiento: usa la farmacia del equipo principal."""
    principal = mantenimiento.equipos.select_related('equipo__farmacia').filter(es_principal=True).first()
    farmacia = principal.equipo.farmacia if principal and principal.equipo else None
    return distancia_minima_a_farmacia(
        tecnico_id=mantenimiento.tecnico_id, farmacia=farmacia,
        desde=mantenimiento.inicio_real or mantenimiento.fecha_programada,
        hasta=mantenimiento.fecha_cierre or timezone.now(),
    )


def abrir_mantenimiento_desde_alerta(alerta):
    """Abre un Mantenimiento para el equipo de la estación que disparó `alerta`.

    Cierra el círculo del RMM: hasta ahora una alerta avisaba, pero alguien tenía que
    cargar la orden de trabajo a mano. Solo se llama para reglas marcadas con
    `abre_mantenimiento` (ver ReglaAlerta) -- no toda alerta amerita un técnico.

    NUNCA lanza: lo llama apps.monitoreo.services.abrir_o_mantener_alerta, que corre
    sobre toda la flota; un problema acá no puede tumbar la evaluación de alertas ni
    impedir que la alerta se abra. Devuelve el Mantenimiento creado, o None con el
    motivo logueado.

    Casos en que no crea nada (ninguno es un error):
    - La estación no tiene un Activo vinculado todavía (el cruce por número de serie
      corre a diario, ver apps.activos.services.vincular_activos_por_numero_serie):
      sin equipo no hay a qué asociar el mantenimiento.
    - Ese equipo ya tiene un mantenimiento abierto: crear_mantenimiento_manual lo
      rechaza a propósito, y acá es justamente lo que se quiere (una falla que
      persiste no debe generar una orden nueva cada vez que se re-evalúa la regla).
    """
    import logging

    from .models import PrioridadMantenimiento

    logger = logging.getLogger(__name__)
    estacion = alerta.estacion
    activo = getattr(estacion, 'activo_vinculado', None)
    if activo is None:
        logger.info(
            'Alerta #%s en %s: la estación no tiene un activo vinculado, no se abre mantenimiento.',
            alerta.pk, estacion.codigo,
        )
        return None

    # La severidad de la regla define la prioridad: una regla crítica (POS caído) no
    # puede entrar con el mismo plazo que una advertencia (disco al 80%).
    prioridad = (
        PrioridadMantenimiento.CRITICA
        if alerta.regla.severidad == alerta.regla.Severidad.CRITICAL
        else PrioridadMantenimiento.ALTA
    )
    try:
        mantenimiento = crear_mantenimiento_manual(
            equipos=[activo], tecnico=None, cliente=activo.colaborador_actual,
            descripcion=(
                f'Abierto automáticamente por la alerta #{alerta.pk} '
                f'"{alerta.regla.nombre}" en {estacion.codigo} (valor: {alerta.valor_disparador}).'
            ),
            tipo_mantenimiento=_tipo_mantenimiento('correctivo'),
            fecha_programada=timezone.now(), usuario=None, prioridad=prioridad,
        )
    except ValueError as exc:
        # Típicamente "ya hay un mantenimiento abierto para este equipo".
        logger.info('Alerta #%s en %s: no se abrió mantenimiento (%s).', alerta.pk, estacion.codigo, exc)
        return None
    except Exception:
        logger.exception('Alerta #%s en %s: error abriendo el mantenimiento automático.', alerta.pk, estacion.codigo)
        return None

    mantenimiento.tipo_origen = TipoOrigenMantenimiento.MONITOREO
    mantenimiento.save(update_fields=['tipo_origen'])
    alerta.mantenimiento = mantenimiento
    alerta.save(update_fields=['mantenimiento'])
    logger.info('Alerta #%s en %s abrió el mantenimiento #%s.', alerta.pk, estacion.codigo, mantenimiento.pk)
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


@transaction.atomic
def cerrar_mantenimiento(*, mantenimiento, resultado_tecnico, usuario, tiempo_real_minutos=None, estado_general=''):
    if mantenimiento.estado_interno == Mantenimiento.EstadoInterno.CERRADO:
        raise ValueError('El mantenimiento ya está cerrado.')
    if mantenimiento.estado_interno == Mantenimiento.EstadoInterno.CANCELADO:
        raise ValueError('Un mantenimiento cancelado no puede cerrarse.')
    mantenimiento.estado_interno = Mantenimiento.EstadoInterno.CERRADO
    mantenimiento.resultado_tecnico = resultado_tecnico
    mantenimiento.fecha_cierre = timezone.now()
    mantenimiento.cerrado_por = usuario
    mantenimiento.tiempo_real_minutos = tiempo_real_minutos
    campos = ['estado_interno', 'resultado_tecnico', 'fecha_cierre', 'cerrado_por', 'tiempo_real_minutos']
    if estado_general:
        mantenimiento.estado_general = estado_general
        campos.append('estado_general')
    # Se calcula ACÁ y se persiste: es un hecho del momento del cierre, y recalcularlo
    # después daría otro resultado (las posiciones se purgan, el umbral puede cambiar).
    mantenimiento.distancia_verificacion_metros = _verificar_presencia_en_sitio(mantenimiento)
    campos.append('distancia_verificacion_metros')
    mantenimiento.save(update_fields=campos)
    EventoMantenimiento.objects.create(
        mantenimiento=mantenimiento, tipo_evento=EventoMantenimiento.TipoEvento.CERRADO, usuario=usuario,
        detalle={'resultado_tecnico': resultado_tecnico},
    )
    if resultado_tecnico in (ResultadoTecnico.REQUIERE_BAJA, ResultadoTecnico.IRREPARABLE):
        for me in mantenimiento.equipos.select_related('equipo'):
            activos_services.registrar_baja_recomendada(
                activo=me.equipo, motivo=f'Mantenimiento #{mantenimiento.pk}: {resultado_tecnico}', usuario=usuario,
            )
    elif resultado_tecnico in RESULTADOS_RETORNAN_A_BODEGA:
        # Solo los equipos que este mantenimiento puso "En reparación" -- si el
        # mantenimiento es preventivo sobre un activo ya asignado/en bodega, cerrarlo
        # no le cambia el estado de ciclo de vida.
        estado_fisico = _ESTADO_FISICO_DESDE_GENERAL.get(
            mantenimiento.estado_general, Activo.EstadoFisico.BUENO,
        )
        for me in mantenimiento.equipos.select_related('equipo').filter(equipo__estado=Activo.Estado.EN_REPARACION):
            activos_services.registrar_retorno_reparacion(
                activo=me.equipo, estado_fisico=estado_fisico, usuario=usuario,
            )

    if mantenimiento.mantenimiento_programado_id:
        programado = mantenimiento.mantenimiento_programado
        hoy = timezone.localtime(mantenimiento.fecha_cierre).date()
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
        tipo_origen=TipoOrigenMantenimiento.PROGRAMADO, tipo_mantenimiento=_tipo_mantenimiento('preventivo'),
        fecha_programada=timezone.now(), snapshot_equipo=_snapshot_equipo(equipo),
    )
    MantenimientoEquipo.objects.create(mantenimiento=mantenimiento, equipo=equipo, es_principal=True)
    EventoMantenimiento.objects.create(
        mantenimiento=mantenimiento, tipo_evento=EventoMantenimiento.TipoEvento.PROGRAMADO, usuario=usuario,
        detalle={'origen': 'programado', 'mantenimiento_programado_id': programado.pk},
    )
    hoy = timezone.localdate()
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
        hoy = timezone.localdate()
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


def notificar(*, usuario, mensaje, url='', mantenimiento=None, actividad_planificada=None,
               mantenimiento_programado=None):
    """Bandeja de notificaciones in-app; no genera EventoMantenimiento (no es un hecho de negocio a auditar)."""
    return Notificacion.objects.create(
        usuario=usuario, mensaje=mensaje, url=url, mantenimiento=mantenimiento,
        actividad_planificada=actividad_planificada, mantenimiento_programado=mantenimiento_programado,
    )


# --- Alertas de vencimiento (reutilizan Notificacion, no un modelo de alerta propio) ---
#
# apps.mantenimiento.Notificacion existía desde antes pero nada la poblaba todavía --
# generar_mantenimientos_vencidos() ya resuelve el vencimiento de un plan apenas se
# cumple (crea el siguiente Mantenimiento el mismo día), así que un plan casi nunca
# queda "vencido" visible más de un día si la tarea diaria corre bien. El valor real de
# esto es doble: avisar CON ANTICIPACIÓN (para que el técnico planifique su ruta, no se
# entere el mismo día) y detectar cuando un Mantenimiento ya generado (programado o no)
# lleva demasiado tiempo sin cerrarse -- eso sí es un problema operativo real y no se
# autorresuelve solo.

DIAS_PROXIMO_A_VENCER = 7
DIAS_GRACIA_ATRASADO = 3


def mantenimientos_programados_por_vencer(dias=DIAS_PROXIMO_A_VENCER):
    """Planes activos cuyo `fecha_proximo` cae dentro de los próximos `dias` días."""
    hoy = timezone.localdate()
    return MantenimientoProgramado.objects.filter(
        activo=True, fecha_proximo__gte=hoy, fecha_proximo__lte=hoy + timedelta(days=dias),
    ).select_related('equipo', 'tecnico')


def mantenimientos_atrasados(dias_gracia=DIAS_GRACIA_ATRASADO):
    """Mantenimientos abiertos que ya pasaron su límite de RESOLUCIÓN según el SLA de
    su prioridad (AcuerdoNivelServicio).

    Antes se usaba un único umbral en días para todo: un POS caído en una farmacia
    vendiendo "vencía" igual que un preventivo de rutina. Ahora cada prioridad tiene
    su propio plazo y el atraso se mide contra ese.

    Se resuelve en SQL (un Q por prioridad) y no filtrando en Python, porque esto lo
    llaman el dashboard y la tarea de notificaciones sobre toda la flota.

    `dias_gracia` sigue siendo el respaldo para prioridades sin SLA cargado (o si
    alguien borró los acuerdos): sin acuerdo no se puede afirmar un incumplimiento,
    pero tampoco conviene dejar de avisar de algo abierto hace días.
    """
    ahora = timezone.now()
    abiertos = Mantenimiento.objects.filter(
        estado_interno__in=[Mantenimiento.EstadoInterno.PENDIENTE, Mantenimiento.EstadoInterno.EN_PROCESO],
    )

    acuerdos = {a.prioridad: a for a in AcuerdoNivelServicio.objects.filter(activo=True)}
    condicion = Q()
    for prioridad, _ in PrioridadMantenimiento.choices:
        acuerdo = acuerdos.get(prioridad)
        horas = acuerdo.horas_resolucion if acuerdo else dias_gracia * 24
        condicion |= Q(prioridad=prioridad, fecha_programada__lt=ahora - timedelta(hours=horas))

    return abiertos.filter(condicion).select_related('tecnico', 'cliente')


def notificar_mantenimientos_proximos_y_atrasados() -> dict:
    """Diaria (ver CELERY_BEAT_SCHEDULE). Idempotente por día calendario: si ya se avisó
    hoy sobre un plan/mantenimiento puntual, no lo repite -- pero si sigue sin
    resolverse, vuelve a avisar mañana (recordatorio diario a propósito, no una sola vez)."""
    from django.urls import reverse

    hoy = timezone.localdate()
    creadas_proximos = 0
    for programado in mantenimientos_programados_por_vencer():
        if not programado.tecnico_id:
            continue
        ya_avisado_hoy = Notificacion.objects.filter(
            mantenimiento_programado=programado, creado_en__date=hoy,
        ).exists()
        if ya_avisado_hoy:
            continue
        dias_restantes = (programado.fecha_proximo - hoy).days
        notificar(
            usuario=programado.tecnico,
            mensaje=f'Mantenimiento preventivo de {programado.equipo.codigo} vence en '
                    f'{dias_restantes} día(s) ({programado.fecha_proximo:%d/%m/%Y}).',
            url=reverse('panel:mantenimientos_programados_lista'),
            mantenimiento_programado=programado,
        )
        creadas_proximos += 1

    creadas_atrasados = 0
    for mantenimiento in mantenimientos_atrasados():
        if not mantenimiento.tecnico_id:
            continue
        ya_avisado_hoy = Notificacion.objects.filter(
            mantenimiento=mantenimiento, creado_en__date=hoy,
        ).exists()
        if ya_avisado_hoy:
            continue
        dias_atraso = (hoy - timezone.localtime(mantenimiento.fecha_programada).date()).days
        notificar(
            usuario=mantenimiento.tecnico,
            mensaje=f'Mantenimiento #{mantenimiento.pk} lleva {dias_atraso} día(s) sin cerrarse.',
            url=reverse('panel:mantenimiento_detalle', args=[mantenimiento.pk]),
            mantenimiento=mantenimiento,
        )
        creadas_atrasados += 1

    return {'proximos': creadas_proximos, 'atrasados': creadas_atrasados}


# --- KPIs (para el resumen por cliente y el reporte CSV) ---
#
# apps.mantenimiento nunca tuvo un dashboard propio -- todo lo de acá se calcula al
# vuelo sobre datos que ya se venían capturando (Mantenimiento/RepuestoUtilizado), no
# hace falta ningún campo ni tabla nueva. Fórmulas tal como quedaron documentadas en
# docs/proceso-mantenimiento-ti.md (22-ago-2026).

def resumen_mantenimiento_periodo(unidad_negocio, desde, hasta):
    """KPIs de mantenimiento de una unidad de negocio en un rango de fechas
    [desde, hasta) -- mismo contrato que apps.facturacion.services.resumen_facturacion."""
    del_periodo = Mantenimiento.objects.filter(
        cliente__unidad_negocio=unidad_negocio, fecha_creacion__gte=desde, fecha_creacion__lt=hasta,
    )
    cerrados_periodo = del_periodo.filter(estado_interno=Mantenimiento.EstadoInterno.CERRADO)

    mttr = cerrados_periodo.annotate(
        duracion=ExpressionWrapper(F('fecha_cierre') - F('fecha_creacion'), output_field=DurationField()),
    ).aggregate(promedio=Avg('duracion'))['promedio']

    costo_repuestos = RepuestoUtilizado.objects.filter(
        mantenimiento__cliente__unidad_negocio=unidad_negocio,
        mantenimiento__fecha_creacion__gte=desde, mantenimiento__fecha_creacion__lt=hasta,
    ).aggregate(total=Sum(F('cantidad') * F('costo_unitario')))['total'] or 0

    atrasados_ahora = mantenimientos_atrasados().filter(cliente__unidad_negocio=unidad_negocio).count()
    requieren_reemplazo = Activo.objects.filter(
        unidad_negocio=unidad_negocio, baja_recomendada=True,
    ).exclude(estado=Activo.Estado.DADO_DE_BAJA).count()

    # % de cumplimiento sobre los CERRADOS del período: se calcula en Python porque
    # el límite depende del SLA de cada prioridad (propiedad del modelo, no una columna).
    cerrados = list(cerrados_periodo.only('prioridad', 'fecha_programada', 'fecha_cierre', 'estado_interno'))
    con_sla = [m for m in cerrados if m.limite_resolucion is not None]
    cumplidos = [m for m in con_sla if not m.sla_resolucion_incumplido]
    pct_sla = round(100 * len(cumplidos) / len(con_sla)) if con_sla else None

    return {
        'total_periodo': del_periodo.count(),
        'cerrados_periodo': cerrados_periodo.count(),
        'sla_cumplido_pct': pct_sla,
        'sla_incumplidos_periodo': len(con_sla) - len(cumplidos),
        'atrasados_ahora': atrasados_ahora,
        'mttr_horas': round(mttr.total_seconds() / 3600, 1) if mttr else None,
        'costo_repuestos_periodo': costo_repuestos,
        'equipos_requieren_reemplazo': requieren_reemplazo,
    }


# --- Visitas técnicas ------------------------------------------------------------
# Ciclo de vida: planificada -> en curso -> realizada (o cancelada). Mismo criterio que
# el resto del módulo: cada transición valida el estado de origen y nunca deja el
# registro a medias.

def crear_visita_tecnica(*, farmacia, tecnico, fecha_planificada, motivo='', usuario=None):
    return VisitaTecnica.objects.create(
        farmacia=farmacia, tecnico=tecnico, fecha_planificada=fecha_planificada,
        motivo=motivo, creado_por=usuario,
    )


def iniciar_visita_tecnica(*, visita, usuario=None):
    """Marca la llegada del técnico. A partir de acá corre la ventana contra la que se
    verifica el GPS al cerrar."""
    if visita.estado != VisitaTecnica.Estado.PLANIFICADA:
        raise ValueError('Solo una visita planificada puede iniciarse.')
    visita.estado = VisitaTecnica.Estado.EN_CURSO
    visita.fecha_inicio = timezone.now()
    visita.save(update_fields=['estado', 'fecha_inicio'])
    return visita


def cerrar_visita_tecnica(*, visita, usuario=None, observaciones=''):
    """Cierra la visita y verifica por GPS que el técnico haya estado en la farmacia.

    La ventana arranca en fecha_inicio si la visita se inició; si el técnico cerró sin
    marcar la llegada, se usa el día planificado completo, que es lo más justo que se
    puede hacer sin ese dato.
    """
    if visita.estado == VisitaTecnica.Estado.REALIZADA:
        raise ValueError('La visita ya está cerrada.')
    if visita.estado == VisitaTecnica.Estado.CANCELADA:
        raise ValueError('Una visita cancelada no puede cerrarse.')

    ahora = timezone.now()
    if visita.fecha_inicio:
        desde = visita.fecha_inicio
    else:
        inicio_dia = datetime.combine(visita.fecha_planificada, dt_time.min)
        desde = timezone.make_aware(inicio_dia, timezone.get_current_timezone())

    visita.distancia_verificacion_metros = distancia_minima_a_farmacia(
        tecnico_id=visita.tecnico_id, farmacia=visita.farmacia, desde=desde, hasta=ahora,
    )
    visita.estado = VisitaTecnica.Estado.REALIZADA
    visita.fecha_cierre = ahora
    if observaciones:
        visita.observaciones = observaciones
    visita.save(update_fields=[
        'estado', 'fecha_cierre', 'observaciones', 'distancia_verificacion_metros',
    ])
    return visita


def cancelar_visita_tecnica(*, visita, motivo='', usuario=None):
    if visita.estado in (VisitaTecnica.Estado.REALIZADA, VisitaTecnica.Estado.CANCELADA):
        raise ValueError('Una visita realizada o ya cancelada no puede cancelarse.')
    visita.estado = VisitaTecnica.Estado.CANCELADA
    if motivo:
        visita.observaciones = motivo
    visita.save(update_fields=['estado', 'observaciones'])
    return visita
