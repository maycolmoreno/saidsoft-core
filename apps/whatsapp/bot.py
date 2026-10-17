"""Lo que el bot contesta a cada mensaje que entra (PLAN_MODERNIZACION.md §10-CM).

Dos caminos:

- **Respuesta a una pregunta del sistema.** El botón trae `r:<novedad>:<causa>`: se aplica
  la causa a ESA novedad, sin adivinar a qué pregunta contesta.
- **La farmacia escribe por su cuenta.** Un menú: *Se fue la luz / Sin internet / Un equipo
  falla*, y para el equipo, cuál y qué le pasa (texto o foto).

La farmacia se identifica por su número: el celular cargado en `Farmacia.telefono`.
"""
import logging
from datetime import timedelta

from django.utils import timezone

from apps.catalogo.models import Farmacia
from apps.whatsapp import cliente
from apps.whatsapp.models import ConversacionWhatsApp, MensajeWhatsApp, Novedad
from apps.whatsapp.servicios import aplicar_respuesta, escalar, registrar_salida

logger = logging.getLogger(__name__)

Causa, Motivo, Estado = Novedad.Causa, Novedad.Motivo, Novedad.Estado

# Una conversación a medias (eligió "un equipo falla" y no dijo cuál) se olvida a la
# media hora: si vuelve a escribir después, empieza de nuevo con el menú.
VENCIMIENTO_CONVERSACION = timedelta(minutes=30)

MENU = [('rep:luz', 'Se fue la luz'), ('rep:internet', 'Sin internet'), ('rep:equipo', 'Un equipo falla')]
EQUIPOS = [('eq:caja', 'Caja / PC'), ('eq:impresora', 'Impresora'), ('eq:lector', 'Lector / balanza'),
           ('eq:otro', 'Otro equipo')]

RESPUESTAS = {
    Causa.LUZ: 'Gracias. Registramos el corte de luz. Si vuelve la luz y el sistema no, escríbanos.',
    Causa.INTERNET: 'Gracias. La mesa de ayuda ya está avisada y va a reclamar al proveedor.',
    Causa.TODO_FUNCIONA: 'Gracias por confirmar.',
    Causa.CAJA_APAGADA: 'Gracias. Por favor enciéndala; en unos minutos debería volver a reportar.',
    Causa.CAJA_ENCENDIDA: 'Gracias. La mesa de ayuda lo va a revisar.',
    Causa.NO_SABE: 'Gracias. La mesa de ayuda lo va a revisar.',
}


def leer(mensaje: dict) -> dict:
    """Lo que importa de un mensaje del webhook: quién, el id del botón y el texto."""
    tipo = mensaje.get('type')
    boton, texto, foto = '', '', ''
    if tipo == 'text':
        texto = (mensaje.get('text') or {}).get('body', '')
    elif tipo == 'interactive':
        inter = mensaje.get('interactive') or {}
        boton = (inter.get('button_reply') or inter.get('list_reply') or {}).get('id', '')
    elif tipo == 'button':  # botón de una plantilla
        boton = (mensaje.get('button') or {}).get('payload', '')
    elif tipo == 'image':
        foto = (mensaje.get('image') or {}).get('id', '')
        texto = (mensaje.get('image') or {}).get('caption', '')
    return {'wa': mensaje.get('from', ''), 'boton': boton, 'texto': texto.strip()[:1000], 'foto': foto}


def farmacia_de(wa: str):
    """La farmacia de un número. Con `WHATSAPP_REDIRIGIR_A` (modo prueba), el número de
    prueba habla por la última farmacia a la que se le escribió."""
    from django.conf import settings
    import re

    prueba = re.sub(r'\D', '', getattr(settings, 'WHATSAPP_REDIRIGIR_A', '') or '')
    if prueba and wa == prueba:
        ultimo = MensajeWhatsApp.objects.filter(direccion='salida', farmacia__isnull=False).first()
        return ultimo.farmacia if ultimo else None
    candidatas = [f for f in Farmacia.objects.filter(activa=True).exclude(telefono='')
                  if cliente.numero_wa(f.telefono) == wa]
    if len(candidatas) > 1:
        # Un número corporativo compartido: se prefiere la que tiene una pregunta pendiente.
        con_pregunta = [f for f in candidatas if f.novedades.filter(estado=Estado.ESPERANDO).exists()]
        candidatas = con_pregunta or sorted(candidatas, key=lambda f: f.codigo)
    return candidatas[0] if candidatas else None


def _responder(wa, farmacia, texto, botones=None, *, lista=None, novedad=None):
    if lista:
        resultado = cliente.enviar_lista(wa, texto, 'Elegir', lista)
    elif botones:
        resultado = cliente.enviar_botones(wa, texto, botones)
    else:
        resultado = cliente.enviar_texto(wa, texto)
    registrar_salida(wa, farmacia=farmacia, novedad=novedad,
                     contenido={'texto': texto, 'botones': botones or lista or []}, resultado=resultado)


def _caida_abierta(farmacia):
    from apps.monitoreo.models import EventoEnlaceFarmacia

    return EventoEnlaceFarmacia.objects.filter(farmacia=farmacia, fin__isnull=True).order_by('-inicio').first()


def procesar(mensaje_entrada: MensajeWhatsApp):
    datos = leer(mensaje_entrada.contenido)
    wa = datos['wa']
    farmacia = farmacia_de(wa)
    if farmacia is None:
        _responder(wa, None, 'Este número no está registrado como farmacia. Si es un error, avise a la mesa de ayuda.')
        _avisar_numero_desconocido(wa, datos)
        return
    mensaje_entrada.farmacia = farmacia
    mensaje_entrada.save(update_fields=['farmacia'])

    if datos['boton'].startswith('r:'):
        return _respuesta_a_pregunta(wa, farmacia, datos['boton'], mensaje_entrada)

    conv, _ = ConversacionWhatsApp.objects.get_or_create(telefono=wa, defaults={'farmacia': farmacia})
    if conv.farmacia_id != farmacia.pk or timezone.now() - conv.actualizado_en > VENCIMIENTO_CONVERSACION:
        conv.farmacia, conv.paso, conv.datos = farmacia, '', {}
    boton = datos['boton']

    if boton in ('rep:luz', 'rep:internet'):
        _reporte_luz_o_internet(wa, farmacia, Causa.LUZ if boton == 'rep:luz' else Causa.INTERNET)
        conv.paso, conv.datos = '', {}
    elif boton == 'rep:equipo':
        _responder(wa, farmacia, '¿Qué equipo no funciona?', lista=EQUIPOS)
        conv.paso = 'equipo'
    elif boton.startswith('eq:') and conv.paso == 'equipo':
        conv.paso, conv.datos = 'equipo_desc', {'equipo': dict(EQUIPOS).get(boton, 'Otro equipo')}
        _responder(wa, farmacia, 'Cuéntenos en una frase qué le pasa, o envíe una foto.')
    elif conv.paso == 'equipo_desc' and (datos['texto'] or datos['foto']):
        novedad = Novedad.objects.create(
            farmacia=farmacia, origen=Novedad.Origen.FARMACIA, motivo=Motivo.REPORTE_EQUIPO,
            causa=Causa.EQUIPO, estado=Estado.REGISTRADA, equipo=conv.datos.get('equipo', ''),
            descripcion=datos['texto'], foto_media_id=datos['foto'], respondido_en=timezone.now(),
        )
        escalar(novedad, 'reporte_equipo')
        _responder(wa, farmacia, f'Listo, quedó registrado como caso N° {novedad.pk}. La mesa de ayuda lo va '
                                 'a revisar y le escribiremos cuando haya novedad.', novedad=novedad)
        conv.paso, conv.datos = '', {}
    else:
        pendiente = farmacia.novedades.filter(estado=Estado.ESPERANDO).first()
        if pendiente and datos['texto']:
            # Contestó la pregunta con texto en vez de un botón: lo lee una persona.
            pendiente.descripcion = datos['texto']
            pendiente.save(update_fields=['descripcion'])
            escalar(pendiente, 'respuesta_no_entendida')
            _responder(wa, farmacia, 'Gracias. La mesa de ayuda lo va a revisar.', novedad=pendiente)
        else:
            _responder(wa, farmacia, f'Hola, {farmacia.codigo}. ¿Qué está pasando?', botones=MENU)
    conv.save()


def _respuesta_a_pregunta(wa, farmacia, boton, mensaje_entrada):
    try:
        _, pk, causa = boton.split(':', 2)
        novedad = farmacia.novedades.get(pk=int(pk))
    except (ValueError, Novedad.DoesNotExist):
        # Un botón viejo o que no es de esta farmacia: se ofrece el menú, no se aplica nada.
        return _responder(wa, farmacia, f'Hola, {farmacia.codigo}. ¿Qué está pasando?', botones=MENU)
    mensaje_entrada.novedad = novedad
    mensaje_entrada.save(update_fields=['novedad'])
    if not novedad.abierta:
        return _responder(wa, farmacia, 'Gracias, ese caso ya está cerrado.', novedad=novedad)
    if causa not in Causa.values:
        return None
    aplicar_respuesta(novedad, causa)
    _responder(wa, farmacia, RESPUESTAS.get(causa, 'Gracias.'), novedad=novedad)


def _reporte_luz_o_internet(wa, farmacia, causa):
    caida = _caida_abierta(farmacia)
    # Si el sistema ya le preguntó por esta caída, el reporte ES la respuesta.
    pendiente = (farmacia.novedades.filter(evento_enlace=caida, estado=Estado.ESPERANDO).first()
                 if caida else None)
    if pendiente:
        aplicar_respuesta(pendiente, causa)
        return _responder(wa, farmacia, RESPUESTAS[causa], novedad=pendiente)
    novedad = Novedad.objects.create(
        farmacia=farmacia, origen=Novedad.Origen.FARMACIA,
        motivo=Motivo.REPORTE_LUZ if causa == Causa.LUZ else Motivo.REPORTE_INTERNET,
        causa=causa, estado=Estado.REGISTRADA, evento_enlace=caida, respondido_en=timezone.now(),
    )
    if causa == Causa.INTERNET:
        escalar(novedad, 'reporte_internet')
    else:
        from django.db import transaction

        from apps.whatsapp import mesa

        transaction.on_commit(lambda: mesa.avisar(novedad.pk, informativo=True))
    _responder(wa, farmacia, RESPUESTAS[causa], novedad=novedad)


def _avisar_numero_desconocido(wa, datos):
    from apps.monitoreo.services import _enviar_telegram
    from apps.whatsapp.servicios import chat_mesa

    chat = chat_mesa()
    if chat:
        _enviar_telegram(chat, f'❓ Escribió por WhatsApp un número que no es de ninguna farmacia: {wa}\n'
                               f'«{(datos["texto"] or datos["boton"] or "(sin texto)")[:200]}»')
