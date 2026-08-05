"""Wrapper delgado sobre apps.mantenimiento.services.generar_mantenimientos_vencidos —
la misma lógica la corre también la tarea periódica de Celery
(apps.mantenimiento.tasks.generar_mantenimientos_programados_task, diaria, ver
CELERY_BEAT_SCHEDULE). Este comando queda para correrlo a mano si hace falta.
"""
from django.core.management.base import BaseCommand

from apps.mantenimiento.services import generar_mantenimientos_vencidos


class Command(BaseCommand):
    help = 'Genera el siguiente Mantenimiento de cada MantenimientoProgramado vencido.'

    def handle(self, *args, **options):
        total = generar_mantenimientos_vencidos()
        self.stdout.write(self.style.SUCCESS(f'{total} mantenimiento(s) programado(s) generado(s).'))
