"""Reglas de negocio del control de viáticos (política GFI-GTC-PR002).

Mismo patrón que apps/activos/services.py: la lógica vive acá, separada de las
vistas, para que el panel, el admin y cualquier carga futura corran exactamente las
mismas reglas. Un reporte cargado por el admin tiene que levantar las mismas alertas
que uno cargado por el técnico; si la validación viviera en el formulario, no.

Dos clases de regla, deliberadamente distintas:

- Las que BLOQUEAN viven en `ReporteViatico.clean()` (movilización sin origen/destino,
  reembolso parcial). Son datos que no deberían poder existir.
- Las que AVISAN viven acá y producen `AlertaViatico`. El gasto se guarda igual: quien
  decide es el coordinador, y esconder el reporte le sacaría justamente lo que tiene
  que revisar.
"""
import datetime
import logging

from django.db import transaction
from django.db.models import Count, Q, Sum
from django.utils import timezone

from apps.mantenimiento.models import Mantenimiento, VisitaTecnica

from .models import (
    ALERTAS_QUE_EXIGEN_JUSTIFICACION, UNIDAD_DEL_TOPE, AlertaViatico, ColaboradorZona, EstadoReporteViatico,
    ReporteViatico, RubroViatico, TipoAlertaViatico,
)

logger = logging.getLogger(__name__)


class TransicionInvalida(Exception):
    """El reporte no está en un estado desde el que se pueda hacer esa acción."""


class JustificacionRequerida(Exception):
    """Aprobar un reporte con alertas de zona/tope exige que el coordinador escriba por qué."""


def colaborador_de(user):
    """El Colaborador detrás de un usuario del panel, o None.

    Hay dos caminos a propósito en el proyecto: `Colaborador.usuario` (OneToOne, el
    login propio del técnico) y `PerfilUsuario.colaborador`. Se prueban los dos para
    que quien cargue su reporte no dependa de cuál de los dos le configuraron.
    """
    if user is None or not getattr(user, 'is_authenticated', False):
        return None
    colaborador = getattr(user, 'colaborador', None)
    if colaborador is not None:
        return colaborador
    perfil = getattr(user, 'perfil', None)
    return getattr(perfil, 'colaborador', None) if perfil is not None else None


def rango_del_mes(anio: int, mes: int):
    """(primer día, primer día del mes siguiente) — el segundo es EXCLUSIVO.

    Se usa `fecha__gte`/`fecha__lt` en vez de `__month`/`__year` para que los índices
    de (colaborador, fecha) sirvan.
    """
    desde = datetime.date(anio, mes, 1)
    hasta = datetime.date(anio + 1, 1, 1) if mes == 12 else datetime.date(anio, mes + 1, 1)
    return desde, hasta


# --- Las reglas, una función por regla ---------------------------------------
#
# Cada una devuelve el texto del hallazgo, o None si no hay nada que reportar. Así se
# prueban sueltas y `evaluar_alertas` queda como la lista legible de qué se controla.


def _revisar_tope(reporte):
    tope = reporte.tope
    if tope is None or reporte.monto is None or reporte.monto <= tope:
        return None
    unidad = UNIDAD_DEL_TOPE.get(reporte.rubro, '')
    exceso = reporte.monto - tope
    return (
        f'${reporte.monto} excede el tope de ${tope} {unidad} para '
        f'{reporte.get_rubro_display().lower()} (${exceso} por encima).'
    )


def _revisar_zona(reporte):
    """El caso real que originó el módulo: un técnico reportando un punto que le
    correspondía a otra zona.

    Sin zona asignada, o con la zona sin farmacias cargadas, NO se inventa un
    hallazgo: no hay contra qué comparar, y una alerta falsa en la bandeja del
    coordinador es peor que ninguna -- deja de mirarlas.
    """
    zona = ColaboradorZona.objects.filter(colaborador=reporte.colaborador, activa=True).first()
    if zona is None:
        return None
    asignadas = zona.farmacias_asignadas.all()
    if not asignadas.exists():
        return None
    if asignadas.filter(pk=reporte.farmacia_visitada_id).exists():
        return None

    # Quién sí la tiene asignada: es la primera pregunta del coordinador, y tenerla
    # acá le evita ir a buscarla farmacia por farmacia.
    responsables = ColaboradorZona.objects.filter(
        activa=True, farmacias_asignadas=reporte.farmacia_visitada_id,
    ).exclude(pk=zona.pk).select_related('colaborador')
    detalle = (
        f'{reporte.farmacia_visitada.codigo} no está asignada a la zona '
        f'"{zona.zona_cobertura}" de {reporte.colaborador.nombre}.'
    )
    if responsables:
        quienes = ', '.join(f'{z.colaborador.nombre} ({z.zona_cobertura})' for z in responsables)
        detalle += f' Corresponde a: {quienes}.'
    else:
        detalle += ' Ningún técnico la tiene asignada.'
    return detalle


def _revisar_origen_destino(reporte):
    """Red de seguridad para datos que entraron sin pasar por `clean()` (una carga
    masiva, un `save()` directo en una migración). El camino normal ya los bloquea."""
    if reporte.rubro != RubroViatico.MOVILIZACION:
        return None
    if (reporte.origen or '').strip() and (reporte.destino or '').strip():
        return None
    return 'Movilización sin origen y/o destino: el tramo no se puede auditar.'


def _revisar_monto_repetido(reporte):
    """Mismo colaborador, mismo rubro, mismo monto, varias veces en el mes.

    No es prueba de nada por sí solo -- almorzar tres veces por $4.00 es lo esperable
    -- así que el umbral es 3 o más y la alerta dice "revisar", no "irregular".
    """
    if reporte.monto is None:
        return None
    desde, hasta = rango_del_mes(reporte.fecha.year, reporte.fecha.month)
    gemelos = ReporteViatico.objects.filter(
        colaborador_id=reporte.colaborador_id, rubro=reporte.rubro, monto=reporte.monto,
        fecha__gte=desde, fecha__lt=hasta,
    ).exclude(estado=EstadoReporteViatico.RECHAZADO)
    if reporte.pk:
        gemelos = gemelos.exclude(pk=reporte.pk)
    repeticiones = gemelos.count() + 1  # + este
    if repeticiones < 3:
        return None
    return (
        f'{repeticiones} reportes de {reporte.get_rubro_display().lower()} por ${reporte.monto} '
        f'en {reporte.fecha.strftime("%m/%Y")}. Revisar que no sea el mismo gasto cargado varias veces.'
    )


def _revisar_planificacion(reporte):
    """Un gasto en un punto donde ese día no había nada planificado ni ejecutado.

    Se cruza contra VisitaTecnica (la entidad de planificación) y contra los
    mantenimientos nacidos de una visita a esa farmacia. `Mantenimiento` no tiene FK
    a Farmacia -- llega por `visita` o por sus equipos -- así que ese es el cruce
    posible sin adivinar.

    Requiere que el colaborador tenga usuario del panel: la planificación se guarda
    contra el User, no contra el Colaborador. Sin usuario no se controla.
    """
    usuario_id = reporte.colaborador.usuario_id
    if usuario_id is None:
        return None

    hay_visita = VisitaTecnica.objects.filter(
        tecnico_id=usuario_id, farmacia_id=reporte.farmacia_visitada_id, fecha_planificada=reporte.fecha,
    ).exclude(estado=VisitaTecnica.Estado.CANCELADA).exists()
    if hay_visita:
        return None

    hay_mantenimiento = Mantenimiento.objects.filter(
        tecnico_id=usuario_id, visita__farmacia_id=reporte.farmacia_visitada_id,
        fecha_programada__date=reporte.fecha,
    ).exists()
    if hay_mantenimiento:
        return None

    return (
        f'No hay visita planificada ni mantenimiento de {reporte.colaborador.nombre} en '
        f'{reporte.farmacia_visitada.codigo} el {reporte.fecha}.'
    )


REGLAS = (
    (TipoAlertaViatico.EXCEDE_TOPE, _revisar_tope),
    (TipoAlertaViatico.FUERA_DE_ZONA, _revisar_zona),
    (TipoAlertaViatico.SIN_ORIGEN_DESTINO, _revisar_origen_destino),
    (TipoAlertaViatico.MONTO_REPETIDO, _revisar_monto_repetido),
    (TipoAlertaViatico.SIN_PLANIFICACION, _revisar_planificacion),
)


@transaction.atomic
def evaluar_alertas(reporte) -> list:
    """Corre todas las reglas sobre `reporte` y sincroniza sus AlertaViatico.

    Idempotente y sincronizante, no acumulativo: si el técnico corrige el monto y el
    reporte se revalida, la alerta de tope se marca `resuelta` en vez de quedar
    colgada para siempre. Se marca en vez de borrarse para no perder el rastro de que
    el reporte pasó por ahí.

    Devuelve las alertas ABIERTAS que quedaron.
    """
    abiertas = []
    for tipo, regla in REGLAS:
        detalle = regla(reporte)
        if detalle:
            alerta, _ = AlertaViatico.objects.update_or_create(
                reporte=reporte, tipo_alerta=tipo, defaults={'detalle': detalle, 'resuelta': False},
            )
            abiertas.append(alerta)
        else:
            AlertaViatico.objects.filter(
                reporte=reporte, tipo_alerta=tipo, resuelta=False,
            ).update(resuelta=True)
    return abiertas


@transaction.atomic
def registrar_reporte(*, colaborador, fecha, farmacia_visitada, rubro, monto, origen='', destino='',
                      descripcion='', factura_adjunta=None, total_factura=None):
    """Alta de un reporte: valida lo que bloquea, guarda, y levanta las alertas.

    Lanza ValidationError (desde `clean()`) si el dato no debe existir. El llamador
    -- formulario o admin -- la deja subir: es lo que se le muestra al técnico.
    """
    reporte = ReporteViatico(
        colaborador=colaborador, fecha=fecha, farmacia_visitada=farmacia_visitada, rubro=rubro,
        monto=monto, origen=origen or '', destino=destino or '', descripcion=descripcion or '',
        total_factura=total_factura,
    )
    if factura_adjunta is not None:
        reporte.factura_adjunta = factura_adjunta
    reporte.full_clean(exclude=['estado'])
    reporte.save()
    evaluar_alertas(reporte)
    return reporte


@transaction.atomic
def aprobar_reporte(*, reporte, coordinador, comentario=''):
    """Aprueba. Con alertas de zona o tope abiertas exige justificación escrita.

    Ese es el control que la política pide y el que hace auditable la excepción: si se
    puede aprobar en un clic, la alerta no cambia nada.
    """
    if reporte.estado == EstadoReporteViatico.APROBADO:
        raise TransicionInvalida('El reporte ya está aprobado.')
    comentario = (comentario or '').strip()
    if reporte.requiere_justificacion and not comentario:
        pendientes = ', '.join(
            a.get_tipo_alerta_display()
            for a in reporte.alertas_abiertas.filter(tipo_alerta__in=ALERTAS_QUE_EXIGEN_JUSTIFICACION)
        )
        raise JustificacionRequerida(
            f'Este reporte tiene alertas que exigen justificación ({pendientes}). '
            f'Escribí por qué se aprueba igual.'
        )
    reporte.estado = EstadoReporteViatico.APROBADO
    reporte.comentario_coordinador = comentario
    reporte.revisado_por = coordinador
    reporte.revisado_en = timezone.now()
    reporte.save(update_fields=['estado', 'comentario_coordinador', 'revisado_por', 'revisado_en'])
    return reporte


@transaction.atomic
def observar_reporte(*, reporte, coordinador, comentario):
    """Devuelve el reporte al técnico para que lo corrija. El comentario es el pedido
    de corrección, así que sin él la acción no tiene sentido."""
    comentario = (comentario or '').strip()
    if not comentario:
        raise JustificacionRequerida('Indicá qué tiene que corregir el técnico.')
    reporte.estado = EstadoReporteViatico.OBSERVADO
    reporte.comentario_coordinador = comentario
    reporte.revisado_por = coordinador
    reporte.revisado_en = timezone.now()
    reporte.save(update_fields=['estado', 'comentario_coordinador', 'revisado_por', 'revisado_en'])
    return reporte


@transaction.atomic
def rechazar_reporte(*, reporte, coordinador, comentario):
    """Rechaza definitivamente. Exige motivo: es la decisión que el técnico va a
    discutir, y sin motivo escrito no hay nada que discutir."""
    comentario = (comentario or '').strip()
    if not comentario:
        raise JustificacionRequerida('Indicá el motivo del rechazo.')
    reporte.estado = EstadoReporteViatico.RECHAZADO
    reporte.comentario_coordinador = comentario
    reporte.revisado_por = coordinador
    reporte.revisado_en = timezone.now()
    reporte.save(update_fields=['estado', 'comentario_coordinador', 'revisado_por', 'revisado_en'])
    return reporte


# --- Consolidado mensual ------------------------------------------------------


def consolidado_mensual(queryset, anio: int, mes: int) -> list:
    """Total por rubro y cantidad de alertas, por colaborador, para un mes.

    Recibe un queryset YA escopado por tenant: el aislamiento se decide en la vista,
    no acá, para no tener dos criterios de scoping que se puedan desincronizar.

    Los rechazados no suman: no se van a pagar, y mezclarlos infla el total que el
    coordinador usa para decidir.
    """
    desde, hasta = rango_del_mes(anio, mes)
    del_mes = queryset.filter(fecha__gte=desde, fecha__lt=hasta).exclude(
        estado=EstadoReporteViatico.RECHAZADO,
    )

    # Los montos se suman SIN tocar `alertas`. Pedir Sum('monto') y Count('alertas')
    # en el mismo annotate hace que el JOIN a alertas multiplique las filas y el total
    # salga inflado tantas veces como alertas tenga cada reporte -- un reporte de $40
    # con dos alertas sumaba $80.
    filas = list(
        del_mes.values('colaborador_id', 'colaborador__nombre', 'colaborador__cedula')
        .annotate(
            hospedaje=Sum('monto', filter=Q(rubro=RubroViatico.HOSPEDAJE), default=0),
            alimentacion=Sum('monto', filter=Q(rubro=RubroViatico.ALIMENTACION), default=0),
            movilizacion=Sum('monto', filter=Q(rubro=RubroViatico.MOVILIZACION), default=0),
            total=Sum('monto', default=0),
            reportes=Count('id'),
        )
        .order_by('colaborador__nombre')
    )

    alertas_por_colaborador = dict(
        AlertaViatico.objects.filter(resuelta=False, reporte__in=del_mes)
        .values_list('reporte__colaborador_id')
        .annotate(total=Count('id'))
        .values_list('reporte__colaborador_id', 'total')
    )
    for fila in filas:
        fila['alertas'] = alertas_por_colaborador.get(fila['colaborador_id'], 0)
    return filas


def tendencia_ultimos_meses(queryset, anio: int, mes: int, meses: int = 3) -> list:
    """Total gastado y alertas abiertas por mes, del más viejo al más nuevo.

    `meses=3` incluye el mes pedido: es "el mes y los dos anteriores", que es como se
    lee una tendencia en la bandeja.
    """
    resultado = []
    for atras in range(meses - 1, -1, -1):
        y, m = anio, mes - atras
        while m < 1:
            m += 12
            y -= 1
        desde, hasta = rango_del_mes(y, m)
        del_mes = queryset.filter(fecha__gte=desde, fecha__lt=hasta).exclude(
            estado=EstadoReporteViatico.RECHAZADO,
        )
        # Misma separación que en `consolidado_mensual` y por el mismo motivo: sumar
        # montos en el mismo aggregate que cuenta alertas infla el total.
        resultado.append({
            'anio': y, 'mes': m, 'etiqueta': f'{m:02d}/{y}',
            'total': del_mes.aggregate(total=Sum('monto', default=0))['total'],
            'alertas': AlertaViatico.objects.filter(resuelta=False, reporte__in=del_mes).count(),
            'reportes': del_mes.count(),
        })
    return resultado
