"""Wrapper delgado sobre apps.software.services.generar_escaneos_vencidos — la misma
lógica la corre también la tarea periódica de Celery
(apps.software.tasks.generar_escaneos_programados_task, diaria, ver
CELERY_BEAT_SCHEDULE). Este comando queda para correrlo a mano si hace falta.
"""
from django.core.management.base import BaseCommand

from apps.software.services import generar_escaneos_vencidos


class Command(BaseCommand):
    help = 'Dispara el escaneo de software instalado de cada InventarioProgramado vencido.'

    def handle(self, *args, **options):
        total = generar_escaneos_vencidos()
        self.stdout.write(self.style.SUCCESS(f'{total} escaneo(s) programado(s) disparado(s).'))
