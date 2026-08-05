from celery import shared_task

from .models import Mantenimiento
from .services import generar_informe_pdf, generar_mantenimientos_vencidos


@shared_task(name='apps.mantenimiento.tasks.generar_mantenimientos_programados_task')
def generar_mantenimientos_programados_task():
    """Diaria (ver CELERY_BEAT_SCHEDULE). Segura de repetir en el mismo día: el propio
    filtro por fecha_proximo avanza tras cada generación."""
    total = generar_mantenimientos_vencidos()
    return f'{total} mantenimiento(s) programado(s) generado(s).'


@shared_task(name='apps.mantenimiento.tasks.generar_informe_pdf_task')
def generar_informe_pdf_task(mantenimiento_id):
    mantenimiento = Mantenimiento.objects.get(pk=mantenimiento_id)
    generar_informe_pdf(mantenimiento=mantenimiento)
    return f'Informe PDF generado para mantenimiento #{mantenimiento_id}.'
