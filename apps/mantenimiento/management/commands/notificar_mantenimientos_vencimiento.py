"""Wrapper delgado sobre apps.mantenimiento.services.notificar_mantenimientos_proximos_y_atrasados
-- la misma lógica la corre también la tarea periódica de Celery
(apps.mantenimiento.tasks.notificar_mantenimientos_vencimiento_task, diaria, ver
CELERY_BEAT_SCHEDULE). Este comando queda para correrlo a mano si hace falta.
"""
from django.core.management.base import BaseCommand

from apps.mantenimiento.services import notificar_mantenimientos_proximos_y_atrasados


class Command(BaseCommand):
    help = 'Avisa (Notificacion in-app) de planes próximos a vencer y mantenimientos atrasados.'

    def handle(self, *args, **options):
        resultado = notificar_mantenimientos_proximos_y_atrasados()
        self.stdout.write(self.style.SUCCESS(
            f'{resultado["proximos"]} aviso(s) de vencimiento próximo, '
            f'{resultado["atrasados"]} de atraso.',
        ))
