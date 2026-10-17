"""Las transiciones de una novedad: preguntar, responder, escalar, tomar y cerrar.

Las reglas de cuándo preguntar están en `reglas.py`; lo que contesta el bot a cada mensaje,
en `bot.py`. Acá vive lo que cambia el estado de una `Novedad`, para que el webhook, la
tarea periódica, los botones de Telegram y la bandeja del panel pasen por el mismo camino.
"""
import logging

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from apps.whatsapp import cliente
from apps.whatsapp.models import MensajeWhatsApp, Novedad

logger = logging.getLogger(__name__)

Estado, Causa, Motivo = Novedad.Estado, Novedad.Causa, Novedad.Motivo

# Por qué escala cada caso: la mesa de ayuda lee esto en el aviso y en la bandeja.
MOTIVOS_ESCALADA = {
    'sin_respuesta': 'La farmacia no respondió el WhatsApp',
    'sin_telefono': 'La farmacia no tiene celular cargado',
    'reclamo_proveedor': 'Hay luz y no hay internet: reclamar al proveedor',
    'farmacia_no_ve_problema': 'La farmacia dice que todo funciona, pero el enlace sigue caído',
    'revisar_estacion': 'La caja está encendida pero no reporta',
    'no_sabe': 'La farmacia no sabe qué pasa',
    'respuesta_no_entendida': 'La farmacia respondió con texto: leer',
    'reporte_equipo': 'La farmacia reportó un equipo que no funciona',
    'reporte_internet': 'La farmacia reportó que no tiene internet',
    'luz_prolongada': 'Corte de luz reportado hace más de 2 horas y el enlace sigue caído',
    'numero_desconocido': 'Escribió un número que no es de ninguna farmacia',
}


def registrar_salida(wa, *, farmacia=None, novedad=None, contenido=None, resultado=(False, '')):
    enviado, detalle = resultado
    return MensajeWhatsApp.objects.create(
        direccion=MensajeWhatsApp.Direccion.SALIDA, telefono=wa, farmacia=farmacia, novedad=novedad,
        wa_id=(detalle or None) if enviado else None, contenido=contenido or {}, procesado=True,
        error='' if enviado else (detalle or '')[:300],
    )


def enviar_a_farmacia(novedad, texto, botones=None):
    """Texto libre (o con botones) a la farmacia de la novedad. Solo vale dentro de la
    ventana de 24 h que abre la farmacia al escribir, que es cuando se usa."""
    wa = cliente.numero_wa(novedad.farmacia.telefono)
    resultado = (cliente.enviar_botones(wa, texto, botones) if botones else cliente.enviar_texto(wa, texto))
    registrar_salida(wa, farmacia=novedad.farmacia, novedad=novedad,
                     contenido={'texto': texto, 'botones': botones or []}, resultado=resultado)
    return resultado[0]


def _avisar_mesa(novedad, *, informativo=False):
    from apps.whatsapp import mesa

    transaction.on_commit(lambda: mesa.avisar(novedad.pk, informativo=informativo))


def escalar(novedad, motivo):
    """Pasa a la bandeja de la mesa de ayuda y le avisa. Idempotente: una novedad ya
    escalada o tomada no se vuelve a anunciar (un mensaje por incidente)."""
    if novedad.estado in (Estado.ESCALADA, Estado.EN_ATENCION, Estado.CERRADA):
        return novedad
    novedad.estado = Estado.ESCALADA
    novedad.escalado_en = timezone.now()
    novedad.motivo_escalada = motivo
    novedad.save(update_fields=['estado', 'escalado_en', 'motivo_escalada'])
    _avisar_mesa(novedad)
    return novedad


def aplicar_respuesta(novedad, causa, *, por_mesa=False, usuario=None, quien=''):
    """Lo que contestó la farmacia (o lo que averiguó la mesa llamando) y qué sigue.

    | caso | causa | qué pasa |
    |---|---|---|
    | caída | luz | se registra; no escala (sale del SLA del proveedor) |
    | caída | internet | escala: reclamo al proveedor |
    | caída | todo funciona | escala si el enlace sigue caído; si volvió, se cierra |
    | caja | apagada | se registra y se le pide que la encienda |
    | caja | encendida / no sabe | escala |
    """
    if not novedad.abierta:
        return novedad
    ahora = timezone.now()
    novedad.causa = causa
    novedad.respondido_en = novedad.respondido_en or ahora
    novedad.respondido_por_mesa = por_mesa
    campos = ['causa', 'respondido_en', 'respondido_por_mesa']

    if por_mesa:
        # La mesa ya está en el caso: lo que registra es la causa, no un motivo para
        # volver a avisarse a sí misma.
        if novedad.estado != Estado.EN_ATENCION:
            novedad.estado = Estado.EN_ATENCION
            novedad.tomado_por, novedad.tomado_por_texto = usuario, quien[:100]
            novedad.tomado_en = ahora
            campos += ['estado', 'tomado_por', 'tomado_por_texto', 'tomado_en']
        novedad.save(update_fields=campos)
        return novedad

    novedad.save(update_fields=campos)
    if novedad.motivo == Motivo.ENLACE_CAIDO:
        sigue_caida = novedad.evento_enlace is not None and novedad.evento_enlace.fin is None
        if causa == Causa.LUZ:
            _registrar(novedad)
            _avisar_mesa(novedad, informativo=True)
        elif causa == Causa.INTERNET:
            escalar(novedad, 'reclamo_proveedor')
        elif causa == Causa.TODO_FUNCIONA:
            if sigue_caida:
                escalar(novedad, 'farmacia_no_ve_problema')
            else:
                cerrar(novedad, 'La farmacia confirmó que funciona y el enlace volvió.')
    elif novedad.motivo == Motivo.ESTACION_SIN_REPORTAR:
        if causa == Causa.CAJA_APAGADA:
            _registrar(novedad)
        else:
            escalar(novedad, 'revisar_estacion' if causa == Causa.CAJA_ENCENDIDA else 'no_sabe')
    return novedad


def _registrar(novedad):
    novedad.estado = Estado.REGISTRADA
    novedad.save(update_fields=['estado'])


def tomar(novedad, *, usuario=None, quien=''):
    if novedad.estado in (Estado.EN_ATENCION, Estado.CERRADA):
        return novedad
    novedad.estado = Estado.EN_ATENCION
    novedad.tomado_por, novedad.tomado_por_texto = usuario, quien[:100]
    novedad.tomado_en = timezone.now()
    novedad.save(update_fields=['estado', 'tomado_por', 'tomado_por_texto', 'tomado_en'])
    return novedad


def cerrar(novedad, nota=''):
    if novedad.estado == Estado.CERRADA:
        return novedad
    novedad.estado = Estado.CERRADA
    novedad.cerrado_en = timezone.now()
    novedad.nota_cierre = nota[:200]
    novedad.save(update_fields=['estado', 'cerrado_en', 'nota_cierre'])
    return novedad


def preguntas_de_hoy(farmacia) -> int:
    inicio = timezone.localtime().replace(hour=0, minute=0, second=0, microsecond=0)
    return Novedad.objects.filter(farmacia=farmacia, origen=Novedad.Origen.SISTEMA,
                                  preguntado_en__gte=inicio).count()


def preguntar(novedad, plantilla, parametros, opciones):
    """Manda la pregunta del sistema (una plantilla aprobada, porque la inicia el sistema)
    y deja la novedad esperando. `opciones` son las causas de los botones, en el orden de
    la plantilla; el payload lleva el id de la novedad para que la respuesta no dependa de
    adivinar a qué pregunta contesta."""
    wa = cliente.numero_wa(novedad.farmacia.telefono)
    if not wa:
        return escalar(novedad, 'sin_telefono')
    payloads = [f'r:{novedad.pk}:{causa}' for causa in opciones]
    resultado = cliente.enviar_plantilla(wa, plantilla, parametros, payloads)
    registrar_salida(wa, farmacia=novedad.farmacia, novedad=novedad,
                     contenido={'plantilla': plantilla, 'parametros': parametros, 'payloads': payloads},
                     resultado=resultado)
    if not resultado[0]:
        # Si no salió (API caída, plantilla rechazada), esperar una respuesta que no va a
        # llegar sería perder el incidente: va directo a la mesa.
        return escalar(novedad, 'sin_respuesta')
    novedad.preguntado_en = timezone.now()
    novedad.save(update_fields=['preguntado_en'])
    return novedad


def chat_mesa() -> str:
    return (getattr(settings, 'MESA_AYUDA_TELEGRAM_CHAT_ID', '') or getattr(settings, 'ENLACES_TELEGRAM_CHAT_ID', '')).strip()
