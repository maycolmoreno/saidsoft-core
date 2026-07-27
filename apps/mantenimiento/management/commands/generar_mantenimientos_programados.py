from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from apps.mantenimiento import services
from apps.mantenimiento.models import MantenimientoProgramado


class Command(BaseCommand):
    help = (
        'Recorre los MantenimientoProgramado vencidos (fecha_proximo <= hoy) y genera el '
        'siguiente Mantenimiento de cada uno. Pensado para correr periódicamente vía cron/Celery '
        'beat, igual que apps/monitoreo/management/commands/purgar_metricas.py.'
    )

    @transaction.atomic
    def handle(self, *args, **options):
        hoy = timezone.now().date()
        vencidos = MantenimientoProgramado.objects.filter(activo=True, fecha_proximo__lte=hoy)
        total = 0
        for programado in vencidos:
            services.generar_proximo_mantenimiento_programado(programado=programado)
            total += 1
        self.stdout.write(self.style.SUCCESS(f'{total} mantenimiento(s) programado(s) generado(s).'))
