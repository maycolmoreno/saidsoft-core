"""La mesa de ayuda: el aviso por Telegram y lo que hacen sus botones.

La mesa trabaja en Odoo Helpdesk y por ahora no hay integración (decisión del usuario,
§10-CM): el caso le llega acá, con todo lo necesario para llamar a la farmacia y abrir el
ticket a mano. Los botones registran lo que averiguó sin salir de Telegram, y la bandeja
del panel muestra lo mismo.
"""
import logging

from django.utils import timezone

from apps.whatsapp.models import Novedad
from apps.whatsapp.servicios import MOTIVOS_ESCALADA, aplicar_respuesta, cerrar, chat_mesa, tomar

logger = logging.getLogger(__name__)


def _texto(novedad, *, informativo=False) -> str:
    f = novedad.farmacia
    lineas = []
    if informativo:
        lineas.append(f'ℹ️ {f.codigo} ({f.ciudad or f.ubicacion}): {novedad.get_causa_display() or novedad.get_motivo_display()}')
    else:
        lineas.append(f'⚠️ {f.codigo} ({f.ciudad or f.ubicacion}): {MOTIVOS_ESCALADA.get(novedad.motivo_escalada, novedad.motivo_escalada)}')
    lineas.append(f'Caso N° {novedad.pk} · {novedad.get_motivo_display()}')
    if novedad.evento_enlace_id:
        e = novedad.evento_enlace
        estado = 'sigue caído' if e.fin is None else f'volvió a las {timezone.localtime(e.fin):%H:%M}'
        lineas.append(
            f'Enlace caído desde {timezone.localtime(e.inicio):%H:%M} ({estado}) · '
            f'{f.tipo_enlace or "proveedor sin cargar"}'
            + (f' · circuito {e.circuito_proveedor}' if e.circuito_proveedor else '')
            + ('' if f.tiene_backup else ' · SIN respaldo'),
        )
    if novedad.estacion_id:
        lineas.append(f'Caja: {novedad.estacion.codigo}')
    if novedad.equipo:
        lineas.append(f'Equipo: {novedad.equipo}')
    if novedad.descripcion:
        lineas.append(f'La farmacia escribió: «{novedad.descripcion[:300]}»')
    if novedad.foto_media_id:
        lineas.append('Mandó una foto (ver en WhatsApp Business).')
    lineas.append(f'📞 {f.telefono or "sin teléfono cargado"}')
    if f.tecnico_asignado_id:
        lineas.append(f'Técnico: {f.tecnico_asignado}')
    if not informativo:
        lineas.append('Ticket en Odoo: abrirlo a mano (todavía no hay integración).')
    return '\n'.join(lineas)


def _teclado(novedad) -> list:
    filas = [[{'text': '🙋 Tomo el caso', 'callback_data': f'mesa:tomar:{novedad.pk}'}]]
    if novedad.motivo in (Novedad.Motivo.ENLACE_CAIDO, Novedad.Motivo.REPORTE_INTERNET, Novedad.Motivo.MENSAJE):
        filas.append([
            {'text': '💡 Era luz', 'callback_data': f'mesa:luz:{novedad.pk}'},
            {'text': '🌐 Era internet', 'callback_data': f'mesa:internet:{novedad.pk}'},
        ])
    filas.append([
        {'text': '✅ Cerrar', 'callback_data': f'mesa:cerrar:{novedad.pk}'},
        {'text': novedad.farmacia.codigo, 'callback_data': f'farm:{novedad.farmacia.codigo}'},
    ])
    return filas


def avisar(novedad_pk, *, informativo=False) -> bool:
    """Manda el caso al chat de la mesa. Nunca lanza."""
    from apps.monitoreo.services import _enviar_telegram

    chat = chat_mesa()
    if not chat:
        logger.warning('Mesa de ayuda: no hay MESA_AYUDA_TELEGRAM_CHAT_ID ni ENLACES_TELEGRAM_CHAT_ID; '
                       'el caso %s queda solo en la bandeja del panel.', novedad_pk)
        return False
    novedad = Novedad.objects.select_related('farmacia', 'farmacia__tecnico_asignado', 'evento_enlace',
                                             'estacion').filter(pk=novedad_pk).first()
    if novedad is None:
        return False
    return _enviar_telegram(chat, _texto(novedad, informativo=informativo),
                            teclado=None if informativo else _teclado(novedad))


def responder_callback(data: str, chat_id):
    """`mesa:<accion>:<id>` desde el chat de Telegram. Devuelve `(texto, teclado)` o None.

    Quién lo toca sale de `usuario_de_chat_telegram` (el mismo control que ya decide quién
    puede accionar en el bot); si es un grupo sin persona vinculada, queda el chat_id.
    """
    from apps.cuentas.services import usuario_de_chat_telegram

    try:
        _, accion, pk = data.split(':', 2)
        novedad = Novedad.objects.select_related('farmacia').get(pk=int(pk))
    except (ValueError, Novedad.DoesNotExist):
        return None
    usuario = usuario_de_chat_telegram(chat_id)
    quien = usuario.get_username() if usuario else f'chat {chat_id}'
    if accion == 'tomar':
        tomar(novedad, usuario=usuario, quien=quien)
        texto = f'🙋 Caso N° {novedad.pk} ({novedad.farmacia.codigo}) tomado por {quien}.'
    elif accion in ('luz', 'internet'):
        causa = Novedad.Causa.LUZ if accion == 'luz' else Novedad.Causa.INTERNET
        aplicar_respuesta(novedad, causa, por_mesa=True, usuario=usuario, quien=quien)
        texto = f'Caso N° {novedad.pk} ({novedad.farmacia.codigo}): registrado «{novedad.get_causa_display()}» por {quien}.'
    elif accion == 'cerrar':
        cerrar(novedad, f'Cerrado desde Telegram por {quien}.')
        texto = f'✅ Caso N° {novedad.pk} ({novedad.farmacia.codigo}) cerrado por {quien}.'
    else:
        return None
    return texto, None
