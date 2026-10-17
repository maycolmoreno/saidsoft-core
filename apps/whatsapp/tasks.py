from celery import shared_task


@shared_task(name='apps.whatsapp.tasks.procesar_mensaje_whatsapp_task')
def procesar_mensaje_whatsapp_task(mensaje_pk):
    """Atiende un mensaje que entró por el webhook. Se reserva con un UPDATE condicional:
    si la tarea se reintenta o se encola dos veces, el mensaje se procesa una sola."""
    from apps.whatsapp import bot
    from apps.whatsapp.models import MensajeWhatsApp

    if not MensajeWhatsApp.objects.filter(pk=mensaje_pk, procesado=False).update(procesado=True):
        return 'ya procesado'
    bot.procesar(MensajeWhatsApp.objects.get(pk=mensaje_pk))
    return 'procesado'


@shared_task(name='apps.whatsapp.tasks.evaluar_whatsapp_task')
def evaluar_whatsapp_task():
    """Cada 5 min: preguntar, escalar, cerrar (ver apps.whatsapp.reglas)."""
    from apps.whatsapp.reglas import evaluar

    return evaluar()
