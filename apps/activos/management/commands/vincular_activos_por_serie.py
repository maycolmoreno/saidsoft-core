"""Wrapper delgado sobre apps.activos.services.vincular_activos_por_numero_serie — la
misma lógica la corre también la tarea periódica de Celery
(apps.activos.tasks.vincular_activos_por_serie_task, diaria, ver
CELERY_BEAT_SCHEDULE). Este comando queda para correrlo a mano si hace falta.
"""
from django.core.management.base import BaseCommand

from apps.activos.services import vincular_activos_por_numero_serie


class Command(BaseCommand):
    help = 'Cruza Estacion.numero_serie contra Activo.numero_serie y vincula los que matchean.'

    def handle(self, *args, **options):
        total = vincular_activos_por_numero_serie()
        self.stdout.write(self.style.SUCCESS(f'{total} activo(s) vinculado(s) por número de serie.'))
