from celery import shared_task

from .models import Mantenimiento
from .services import (
    escalar_cierres_en_conflicto, generar_informe_pdf, generar_mantenimientos_vencidos,
    notificar_mantenimientos_proximos_y_atrasados,
)


@shared_task(name='apps.mantenimiento.tasks.generar_mantenimientos_programados_task')
def generar_mantenimientos_programados_task():
    """Diaria (ver CELERY_BEAT_SCHEDULE). Segura de repetir en el mismo día: el propio
    filtro por fecha_proximo avanza tras cada generación."""
    total = generar_mantenimientos_vencidos()
    return f'{total} mantenimiento(s) programado(s) generado(s).'


@shared_task(name='apps.mantenimiento.tasks.notificar_mantenimientos_vencimiento_task')
def notificar_mantenimientos_vencimiento_task():
    """Diaria (ver CELERY_BEAT_SCHEDULE). Avisa al técnico asignado de planes próximos a
    vencer y de mantenimientos abiertos hace demasiado tiempo -- idempotente por día."""
    resultado = notificar_mantenimientos_proximos_y_atrasados()
    return f'{resultado["proximos"]} aviso(s) de vencimiento próximo, {resultado["atrasados"]} de atraso.'


@shared_task(name='apps.mantenimiento.tasks.generar_informe_pdf_task')
def generar_informe_pdf_task(mantenimiento_id):
    mantenimiento = Mantenimiento.objects.get(pk=mantenimiento_id)
    generar_informe_pdf(mantenimiento=mantenimiento)
    return f'Informe PDF generado para mantenimiento #{mantenimiento_id}.'


@shared_task(name='apps.mantenimiento.tasks.escalar_cierres_en_conflicto_task')
def escalar_cierres_en_conflicto_task():
    """Periódica (ver CELERY_BEAT_SCHEDULE). Reenvía el aviso de los cierres de campo en
    conflicto que nadie revisó -- una bandeja que depende de que alguien se acuerde de
    mirarla es una bandeja que no se mira. Segura de repetir: `escalado_en` corta."""
    escalados = escalar_cierres_en_conflicto()
    return f'{escalados} cierre(s) en conflicto escalado(s).'
