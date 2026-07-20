"""Borra muestras de métricas más viejas que un umbral de retención.

Reemplaza el `vaciar_logs` del sistema viejo (que borraba TODO cada domingo). Aquí la
retención es por antigüedad, para conservar historia reciente. Se corre por cron
(ej. diario).

    python manage.py purgar_metricas --dias 30

En producción con TimescaleDB esto lo haría una política de retención nativa; el comando
queda como respaldo y para el entorno SQLite de desarrollo.
"""
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from apps.monitoreo.models import MuestraMetrica


class Command(BaseCommand):
    help = 'Elimina muestras de métricas más viejas que --dias.'

    def add_arguments(self, parser):
        parser.add_argument('--dias', type=int, default=30)

    def handle(self, *args, **options):
        umbral = timezone.now() - timedelta(days=options['dias'])
        borradas, _ = MuestraMetrica.objects.filter(timestamp__lt=umbral).delete()
        self.stdout.write(self.style.SUCCESS(f'{borradas} muestra(s) de métricas eliminada(s).'))
