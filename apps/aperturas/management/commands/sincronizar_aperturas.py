"""Relee el estado de los módulos que ejecutan el trabajo de cada apertura en curso.

Por qué hace falta: el resultado de un script o de una instalación llega por MQTT a
`apps.mqtt_worker`, que actualiza `ResultadoEjecucionScript`/`ResultadoInstalacion` sin
saber que esa ejecución pertenece a una apertura. En vez de acoplar el worker a
`apps.aperturas`, la apertura va y lee — y eso lo dispara la vista de detalle del panel
(polling, mismo patrón que despliegues) o este comando, para que una apertura se cierre
sola aunque nadie tenga la pantalla abierta.

A diferencia de los comandos que escriben en masa sobre el inventario, este NO simula por
defecto: solo adelanta el estado de pasos a lo que los módulos de origen ya reportaron, no
crea ni borra nada del catálogo. Corre en el mismo cron que
`generar_ejecuciones_programadas`.

    python manage.py sincronizar_aperturas
    python manage.py sincronizar_aperturas --apertura 12
"""
from django.core.management.base import BaseCommand

from apps.aperturas.models import Apertura
from apps.aperturas.services import sincronizar_apertura


class Command(BaseCommand):
    help = 'Actualiza los pasos de las aperturas en curso con lo que reportaron sus módulos.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--apertura', type=int, default=None,
            help='Sincroniza solo esta apertura (por id). Sin esto, todas las que estén en curso.',
        )

    def handle(self, *args, **options):
        aperturas = Apertura.objects.filter(
            estado__in=[Apertura.Estado.APROBADA, Apertura.Estado.EN_CURSO],
        ).select_related('farmacia', 'plantilla')
        if options['apertura'] is not None:
            aperturas = aperturas.filter(pk=options['apertura'])

        completadas = 0
        for apertura in aperturas:
            sincronizar_apertura(apertura)
            apertura.refresh_from_db()
            estado = apertura.get_estado_display()
            if apertura.estado == Apertura.Estado.COMPLETADA:
                completadas += 1
                self.stdout.write(self.style.SUCCESS(f'{apertura.farmacia.codigo}: {estado}'))
            else:
                pendientes = apertura.pasos.exclude(
                    estado__in=['completado', 'omitido'],
                ).count()
                self.stdout.write(f'{apertura.farmacia.codigo}: {estado} ({pendientes} paso(s) abiertos)')

        self.stdout.write(
            self.style.SUCCESS(f'{aperturas.count()} apertura(s) revisadas, {completadas} completada(s).'),
        )
