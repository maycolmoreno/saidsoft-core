"""Borra muestras de latencia de los servicios del POS más viejas que un umbral.

Wrapper delgado sobre apps.monitoreo.services.purgar_muestras_servicio_pos_antiguas —
la misma lógica la corre la tarea periódica de Celery
(apps.monitoreo.tasks.purgar_muestras_servicio_pos_task, diaria a las 3:30, ver
CELERY_BEAT_SCHEDULE). Este comando queda para correrlo a mano si hace falta, igual
que `purgar_metricas`.

    python manage.py purgar_muestras_servicio_pos --dias 30
"""
from django.core.management.base import BaseCommand

from apps.monitoreo.services import purgar_muestras_servicio_pos_antiguas


class Command(BaseCommand):
    help = 'Elimina muestras de servicios del POS más viejas que --dias.'

    def add_arguments(self, parser):
        parser.add_argument('--dias', type=int, default=30)

    def handle(self, *args, **options):
        borradas = purgar_muestras_servicio_pos_antiguas(dias=options['dias'])
        self.stdout.write(self.style.SUCCESS(f'{borradas} muestra(s) de servicios del POS eliminada(s).'))
