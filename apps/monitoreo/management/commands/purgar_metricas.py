"""Borra muestras de métricas más viejas que un umbral de retención.

Wrapper delgado sobre apps.monitoreo.services.purgar_metricas_antiguas — la misma
lógica la corre también la tarea periódica de Celery
(apps.monitoreo.tasks.purgar_metricas_task, diaria, ver CELERY_BEAT_SCHEDULE). Este
comando queda para correrlo a mano si hace falta.

    python manage.py purgar_metricas --dias 30
"""
from django.core.management.base import BaseCommand

from apps.monitoreo.services import purgar_metricas_antiguas


class Command(BaseCommand):
    help = 'Elimina muestras de métricas más viejas que --dias.'

    def add_arguments(self, parser):
        parser.add_argument('--dias', type=int, default=30)

    def handle(self, *args, **options):
        borradas = purgar_metricas_antiguas(dias=options['dias'])
        self.stdout.write(self.style.SUCCESS(f'{borradas} muestra(s) de métricas eliminada(s).'))
