"""Marca como OFFLINE las estaciones que dejaron de reportar heartbeat.

Wrapper delgado sobre apps.catalogo.services.marcar_estaciones_offline — la misma
lógica la corre también la tarea periódica de Celery
(apps.catalogo.tasks.marcar_estaciones_offline_task, cada minuto, ver
CELERY_BEAT_SCHEDULE). Este comando queda para correrlo a mano si hace falta.

    python manage.py marcar_estaciones_offline
"""
from django.core.management.base import BaseCommand

from apps.catalogo.services import marcar_estaciones_offline


class Command(BaseCommand):
    help = 'Pasa a OFFLINE las estaciones sin heartbeat reciente.'

    def add_arguments(self, parser):
        # Sin default acá: lo pone `marcar_estaciones_offline` desde
        # `apps.monitoreo.umbrales.ALERTA_SIN_LATIDO_UMBRAL_MINUTOS`. Repetirlo en este
        # wrapper era el cuarto lugar donde vivía el número.
        parser.add_argument('--minutos', type=int, default=None)

    def handle(self, *args, **options):
        total = marcar_estaciones_offline(minutos=options['minutos'])
        self.stdout.write(self.style.SUCCESS(f'{total} estación(es) marcada(s) como offline.'))
