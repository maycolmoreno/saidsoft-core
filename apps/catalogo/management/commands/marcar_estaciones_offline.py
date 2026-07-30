"""Marca como OFFLINE las estaciones que dejaron de reportar heartbeat.

El agente pone la estación ONLINE en cada heartbeat, pero nada la pasa a OFFLINE
cuando se apaga o pierde la red. Este comando cierra ese hueco: se corre
periódicamente (cron / Programador de tareas, ej. cada minuto).

    python manage.py marcar_estaciones_offline

Umbral: una estación se considera caída si su último heartbeat es más viejo que
`--minutos` (por defecto 5, unas 5 veces el intervalo de heartbeat del agente).
"""
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from apps.catalogo.models import Estacion


class Command(BaseCommand):
    help = 'Pasa a OFFLINE las estaciones sin heartbeat reciente.'

    def add_arguments(self, parser):
        parser.add_argument('--minutos', type=int, default=5)

    def handle(self, *args, **options):
        umbral = timezone.now() - timedelta(minutes=options['minutos'])
        # Se traen los objetos (no un .update() directo) porque evaluar_reglas_sin_heartbeat
        # necesita la unidad_negocio de cada estación afectada, no solo el conteo.
        afectadas = list(
            Estacion.objects.select_related('farmacia__unidad_negocio').filter(
                estado_conexion=Estacion.EstadoConexion.ONLINE, ultimo_heartbeat__lt=umbral,
            )
        )
        if afectadas:
            Estacion.objects.filter(pk__in=[e.pk for e in afectadas]).update(
                estado_conexion=Estacion.EstadoConexion.OFFLINE,
            )
            from apps.monitoreo.services import evaluar_reglas_sin_heartbeat
            evaluar_reglas_sin_heartbeat(afectadas)

        # Caso borde: estaciones que reportaron heartbeat alguna vez pero el campo quedó
        # en "nunca conectada" — no aplica aquí; solo movemos ONLINE -> OFFLINE.
        self.stdout.write(self.style.SUCCESS(f'{len(afectadas)} estación(es) marcada(s) como offline.'))
