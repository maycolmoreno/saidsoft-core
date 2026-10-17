"""Webhook de WhatsApp: por acá Meta entrega los mensajes que mandan las farmacias.

Público a propósito (lo llama Meta, sin sesión). Lo que lo protege es la **firma**:
`X-Hub-Signature-256` es el HMAC-SHA256 del cuerpo con `WHATSAPP_APP_SECRET`, y un POST que
no la trae bien no se procesa. Sin el secreto configurado no se acepta nada.

Solo guarda y encola: Meta reintenta si no recibe 200 en pocos segundos, así que acá no se
manda nada ni se avisa a nadie. `MensajeWhatsApp.wa_id` es único, y un reintento del mismo
mensaje no entra dos veces.
"""
import hashlib
import hmac
import json
import logging

from django.conf import settings
from django.db import IntegrityError, transaction
from django.http import HttpResponse, HttpResponseForbidden
from django.views.decorators.csrf import csrf_exempt

from apps.whatsapp.models import MensajeWhatsApp

logger = logging.getLogger(__name__)


def _firma_valida(request) -> bool:
    secreto = getattr(settings, 'WHATSAPP_APP_SECRET', '') or ''
    recibida = request.headers.get('X-Hub-Signature-256', '')
    if not secreto or not recibida.startswith('sha256='):
        return False
    esperada = hmac.new(secreto.encode('utf-8'), request.body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(recibida[len('sha256='):], esperada)


@csrf_exempt
def webhook(request):
    if request.method == 'GET':
        # Alta del webhook en Meta: devuelve el challenge solo si el token coincide.
        token = getattr(settings, 'WHATSAPP_VERIFY_TOKEN', '') or ''
        if (token and request.GET.get('hub.mode') == 'subscribe'
                and hmac.compare_digest(request.GET.get('hub.verify_token', ''), token)):
            return HttpResponse(request.GET.get('hub.challenge', ''), content_type='text/plain')
        return HttpResponseForbidden()
    if request.method != 'POST':
        return HttpResponse(status=405)
    if not _firma_valida(request):
        logger.warning('WhatsApp: webhook con firma inválida o sin secreto configurado; se descarta.')
        return HttpResponseForbidden()
    try:
        cuerpo = json.loads(request.body.decode('utf-8'))
    except (ValueError, UnicodeDecodeError):
        return HttpResponse(status=400)

    from apps.whatsapp.tasks import procesar_mensaje_whatsapp_task

    for entrada in cuerpo.get('entry', []):
        for cambio in entrada.get('changes', []):
            for mensaje in (cambio.get('value') or {}).get('messages', []) or []:
                try:
                    with transaction.atomic():
                        registro = MensajeWhatsApp.objects.create(
                            direccion=MensajeWhatsApp.Direccion.ENTRADA, telefono=mensaje.get('from', '')[:20],
                            wa_id=mensaje.get('id') or None, contenido=mensaje,
                        )
                except IntegrityError:
                    continue  # reintento de Meta: ya está
                transaction.on_commit(lambda pk=registro.pk: procesar_mensaje_whatsapp_task.delay(pk))
    # Los `statuses` (entregado, leído) no se usan por ahora: se responde 200 igual.
    return HttpResponse('ok', content_type='text/plain')
