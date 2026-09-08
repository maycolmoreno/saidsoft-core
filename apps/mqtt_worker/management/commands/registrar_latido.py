"""Registra un latido en WorkerHeartbeat desde afuera del stack.

Los workers de larga duración escriben su propio latido en el loop. Las tareas de
infraestructura que corren en el HOST (el respaldo, orquestado por systemd fuera de
los contenedores — ver `deploy/backup.sh`) no pueden: no tienen acceso al ORM. Este
comando es su puerta de entrada.

    docker compose exec -T web python manage.py registrar_latido respaldo

Se registra DESPUÉS de que la tarea terminó bien, nunca antes: el punto de la fila es
responder "¿cuándo fue la última vez que esto funcionó?", y un latido escrito al
empezar mentiría justo en el caso que importa (la tarea arrancó y falló).
"""

from django.core.management.base import BaseCommand
from django.utils import timezone

from apps.mqtt_worker.models import WorkerHeartbeat

# Nombre de fila del respaldo, compartido con el dashboard (apps.panel.views.dashboard).
NOMBRE_RESPALDO = 'respaldo'


class Command(BaseCommand):
    help = 'Registra (o actualiza) el latido de una tarea en WorkerHeartbeat.'

    def add_arguments(self, parser):
        parser.add_argument(
            'nombre',
            help='Nombre de la fila en WorkerHeartbeat, ej. "respaldo".',
        )

    def handle(self, *args, **options):
        nombre = options['nombre'].strip()
        if not nombre:
            self.stderr.write('El nombre no puede estar vacío.')
            return
        latido, creado = WorkerHeartbeat.objects.update_or_create(
            nombre=nombre, defaults={'ultimo_latido': timezone.now()},
        )
        verbo = 'Creado' if creado else 'Actualizado'
        self.stdout.write(f'{verbo} el latido "{nombre}": {latido.ultimo_latido:%Y-%m-%d %H:%M:%S}')
