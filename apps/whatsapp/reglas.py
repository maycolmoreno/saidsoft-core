"""Cuándo pregunta el sistema, cuándo escala y cuándo cierra (PLAN_MODERNIZACION.md §10-CM).

Corre cada 5 minutos (`evaluar_whatsapp_task`). Cada paso es idempotente: correrlo dos
veces seguidas no pregunta dos veces ni avisa dos veces a la mesa.
"""
import logging
from datetime import timedelta

from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.whatsapp import cliente
from apps.whatsapp.models import Novedad
from apps.whatsapp.servicios import cerrar, enviar_a_farmacia, escalar, preguntar, preguntas_de_hoy

logger = logging.getLogger(__name__)

Estado, Causa, Motivo = Novedad.Estado, Novedad.Causa, Novedad.Motivo

MINUTOS_CAIDA_PARA_PREGUNTAR = 15
MINUTOS_CAJA_PARA_PREGUNTAR = 30
MINUTOS_SIN_RESPUESTA = 15
MAX_PREGUNTAS_POR_DIA = 2
HORAS_LUZ_PROLONGADA = 2
# Horario de respaldo para el bot cuando el de la farmacia no se puede leer. El tablero
# cuenta un horario ilegible como "abierta" para no esconder problemas; para escribirle a
# una persona eso no sirve: le llegaría un mensaje a las 3 de la mañana.
HORA_DESDE_SIN_HORARIO, HORA_HASTA_SIN_HORARIO = 8, 21


def en_horario(farmacia, instante=None) -> bool:
    from apps.catalogo.horarios import leer_horario

    local = timezone.localtime(instante or timezone.now())
    horario = leer_horario(farmacia.horario or '')
    if horario.es_24h:
        return True
    hora = local.hour + local.minute / 60
    if not horario.determinado:
        return HORA_DESDE_SIN_HORARIO <= hora < HORA_HASTA_SIN_HORARIO
    return horario.abierta_a(hora)


def _puede_preguntar(farmacia) -> bool:
    return (cliente.farmacia_habilitada(farmacia) and en_horario(farmacia)
            and preguntas_de_hoy(farmacia) < MAX_PREGUNTAS_POR_DIA)


def preguntar_caidas() -> int:
    """Caídas SOLAS (no cortes masivos ni del monitoreo), de más de 15 min, en horario."""
    from apps.monitoreo.enlaces import incidentes_de_enlace

    ahora = timezone.now()
    preguntadas = 0
    for sitio in incidentes_de_enlace()['aisladas']:
        if sitio['cronica'] or ahora - sitio['inicio'] < timedelta(minutes=MINUTOS_CAIDA_PARA_PREGUNTAR):
            continue
        farmacia = sitio['farmacia']
        evento = farmacia.eventos_enlace.filter(fin__isnull=True).order_by('-inicio').first()
        if evento is None or evento.novedades.exists() or not _puede_preguntar(farmacia):
            continue
        try:
            with transaction.atomic():
                novedad = Novedad.objects.create(
                    farmacia=farmacia, origen=Novedad.Origen.SISTEMA, motivo=Motivo.ENLACE_CAIDO,
                    estado=Estado.ESPERANDO, evento_enlace=evento,
                )
        except IntegrityError:
            continue  # otra corrida ya preguntó por esta caída
        preguntar(novedad, settings.WHATSAPP_PLANTILLA_CAIDA,
                  [farmacia.codigo, timezone.localtime(evento.inicio).strftime('%H:%M')],
                  [Causa.LUZ, Causa.INTERNET, Causa.TODO_FUNCIONA])
        preguntadas += 1
    return preguntadas


def preguntar_cajas() -> int:
    """Cajas sin reportar más de 30 min, en horario, con su enlace arriba (si la farmacia
    entera no tiene enlace, la pregunta es la de la caída, no esta)."""
    from apps.monitoreo.models import Alerta, Metrica

    limite = timezone.now() - timedelta(minutes=MINUTOS_CAJA_PARA_PREGUNTAR)
    preguntadas = 0
    alertas = (Alerta.objects.filter(estado=Alerta.Estado.ABIERTA, regla__metrica=Metrica.SIN_HEARTBEAT,
                                     estacion__ultimo_heartbeat__lt=limite)
               .select_related('estacion__farmacia'))
    for alerta in alertas:
        estacion, farmacia = alerta.estacion, alerta.estacion.farmacia
        if farmacia.eventos_enlace.filter(fin__isnull=True).exists():
            continue
        if estacion.novedades.filter(estado__in=Novedad.ABIERTAS).exists() or not _puede_preguntar(farmacia):
            continue
        novedad = Novedad.objects.create(
            farmacia=farmacia, origen=Novedad.Origen.SISTEMA, motivo=Motivo.ESTACION_SIN_REPORTAR,
            estado=Estado.ESPERANDO, estacion=estacion,
        )
        preguntar(novedad, settings.WHATSAPP_PLANTILLA_ESTACION, [farmacia.codigo, estacion.codigo],
                  [Causa.CAJA_ENCENDIDA, Causa.CAJA_APAGADA, Causa.NO_SABE])
        preguntadas += 1
    return preguntadas


def escalar_sin_respuesta() -> int:
    limite = timezone.now() - timedelta(minutes=MINUTOS_SIN_RESPUESTA)
    pendientes = Novedad.objects.filter(estado=Estado.ESPERANDO, preguntado_en__lt=limite)
    for novedad in pendientes:
        escalar(novedad, 'sin_respuesta')
    return len(pendientes)


def escalar_luz_prolongada() -> int:
    """"Se fue la luz" no escala, salvo que el enlace siga caído 2 h después: o la luz no
    volvió y hay que saberlo, o volvió y el equipo no levantó."""
    limite = timezone.now() - timedelta(hours=HORAS_LUZ_PROLONGADA)
    casos = Novedad.objects.filter(estado=Estado.REGISTRADA, causa=Causa.LUZ, respondido_en__lt=limite,
                                   evento_enlace__fin__isnull=True)
    for novedad in casos:
        escalar(novedad, 'luz_prolongada')
    return len(casos)


def cerrar_resueltas() -> int:
    """Lo que se resolvió solo: la caída volvió, o la caja volvió a reportar. Si la farmacia
    había contestado, se le avisa (nivel 2 de §10-CM); si no, se cierra en silencio."""
    cerradas = 0
    caidas = Novedad.objects.filter(estado__in=Novedad.ABIERTAS, evento_enlace__fin__isnull=False,
                                    motivo__in=[Motivo.ENLACE_CAIDO, Motivo.REPORTE_LUZ, Motivo.REPORTE_INTERNET])
    for novedad in caidas.select_related('farmacia', 'evento_enlace'):
        contesto = novedad.respondido_en is not None and not novedad.respondido_por_mesa
        cerrar(novedad, 'El enlace volvió.')
        if contesto:
            enviar_a_farmacia(novedad, f'Su conexión volvió a las '
                                       f'{timezone.localtime(novedad.evento_enlace.fin):%H:%M}. Gracias por avisar.')
        cerradas += 1
    from apps.monitoreo.models import Alerta

    cajas = Novedad.objects.filter(estado__in=Novedad.ABIERTAS, motivo=Motivo.ESTACION_SIN_REPORTAR)
    for novedad in cajas.select_related('estacion'):
        if novedad.estacion and not Alerta.objects.filter(estacion=novedad.estacion, estado=Alerta.Estado.ABIERTA,
                                                          regla__metrica='sin_heartbeat').exists():
            cerrar(novedad, 'La caja volvió a reportar.')
            cerradas += 1
    return cerradas


def reescalar_sin_tomar() -> int:
    """Lo escalado que nadie tomó en `minutos_escalamiento_alerta` se vuelve a anunciar una
    sola vez, igual que las alertas del panel."""
    from apps.monitoreo.models import ConfiguracionMonitoreo
    from apps.whatsapp import mesa

    minutos = ConfiguracionMonitoreo.obtener().minutos_escalamiento_alerta or 30
    limite = timezone.now() - timedelta(minutes=minutos)
    casos = list(Novedad.objects.filter(estado=Estado.ESCALADA, escalado_en__lt=limite, reescalado_en__isnull=True))
    for novedad in casos:
        Novedad.objects.filter(pk=novedad.pk).update(reescalado_en=timezone.now())
        mesa.avisar(novedad.pk)
    return len(casos)


def evaluar() -> dict:
    resumen = {'cerradas': cerrar_resueltas(), 'escaladas': escalar_sin_respuesta(),
               'luz_prolongada': escalar_luz_prolongada(), 'reescaladas': reescalar_sin_tomar()}
    if cliente.modo() != 'apagado':
        resumen['preguntas_caida'] = preguntar_caidas()
        resumen['preguntas_caja'] = preguntar_cajas()
    return resumen
