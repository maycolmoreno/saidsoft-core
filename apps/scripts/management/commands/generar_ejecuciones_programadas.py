from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from apps.scripts import services
from apps.scripts.models import ScriptProgramado


class Command(BaseCommand):
    help = (
        'Recorre los ScriptProgramado vencidos (fecha_proxima_ejecucion <= hoy) y genera la '
        'siguiente EjecucionScript de cada uno. Pensado para correr periódicamente vía cron/'
        'Programador de tareas, igual que apps/mantenimiento/management/commands/'
        'generar_mantenimientos_programados.py.'
    )

    @transaction.atomic
    def handle(self, *args, **options):
        hoy = timezone.now().date()
        vencidos = ScriptProgramado.objects.filter(activo=True, fecha_proxima_ejecucion__lte=hoy)
        total = 0
        for programado in vencidos:
            services.generar_ejecucion_programada(programado=programado)
            total += 1
        self.stdout.write(self.style.SUCCESS(f'{total} ejecución(es) programada(s) generada(s).'))
