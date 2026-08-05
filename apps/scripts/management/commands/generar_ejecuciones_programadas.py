"""Wrapper delgado sobre apps.scripts.services.generar_ejecuciones_vencidas — la misma
lógica la corre también la tarea periódica de Celery
(apps.scripts.tasks.generar_ejecuciones_programadas_task, diaria, ver
CELERY_BEAT_SCHEDULE). Este comando queda para correrlo a mano si hace falta.
"""
from django.core.management.base import BaseCommand

from apps.scripts.services import generar_ejecuciones_vencidas


class Command(BaseCommand):
    help = 'Genera la EjecucionScript de cada ScriptProgramado vencido.'

    def handle(self, *args, **options):
        total = generar_ejecuciones_vencidas()
        self.stdout.write(self.style.SUCCESS(f'{total} ejecución(es) programada(s) generada(s).'))
