"""Reescribe en EMQX la ACL de cada estación con credencial propia, sin rotar nada.

Hace falta cuando `_reglas_para` gana una regla: las ACLs se escriben una sola vez, al
enrolar, así que una regla nueva no alcanza a las estaciones ya aprovisionadas.

El caso que lo motivó (17-sep-2026): faltaba el permiso para publicar el propio
re-enrolamiento. Sin él, una estación con credencial propia no puede pedir su secreto
HMAC — el broker deniega el publish en silencio y el agente espera una respuesta que
nunca llegó a solicitar. La auto-reparación solo funcionaba en las estaciones que aún
usaban la credencial compartida, o sea justo en las que no la necesitaban.

NO rota la contraseña: `aprovisionar_credencial_estacion` sí lo hace, y rotarla para
corregir un permiso dejaría a la estación sin poder conectarse hasta re-enrolarse — que
es lo que no puede hacer si lo que falta es, precisamente, el permiso de enrolamiento.

    python manage.py reaplicar_acls_mqtt            # simula
    python manage.py reaplicar_acls_mqtt --aplicar
"""
from django.core.management.base import BaseCommand

from apps.catalogo.models import Estacion
from apps.mqtt_worker.emqx_admin import reaplicar_acl_estacion


class Command(BaseCommand):
    help = 'Reescribe la ACL de EMQX de cada estación aprobada, sin rotar credenciales.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--aplicar', action='store_true',
            help='Escribe de verdad. Sin esto, solo lista a quiénes tocaría.',
        )

    def handle(self, *args, **options):
        estaciones = (
            Estacion.objects
            .filter(estado_aprobacion=Estacion.EstadoAprobacion.APROBADA)
            .select_related('farmacia__grupo')
            .order_by('codigo')
        )
        if not options['aplicar']:
            for e in estaciones:
                self.stdout.write('  %s' % e.codigo)
            self.stdout.write(self.style.WARNING(
                'Simulación: se reaplicaría la ACL de %d estación(es). Repetí con --aplicar.'
                % estaciones.count(),
            ))
            return

        ok, fallidas = 0, []
        for estacion in estaciones:
            if reaplicar_acl_estacion(estacion):
                ok += 1
            else:
                fallidas.append(estacion.codigo)

        self.stdout.write(self.style.SUCCESS('%d ACL(s) reaplicadas.' % ok))
        if fallidas:
            # No es fatal: una estación sin credencial propia todavía usa la compartida y
            # sigue funcionando. Pero conviene saber cuáles quedaron afuera.
            self.stdout.write(self.style.WARNING(
                'Sin aplicar (¿sin credencial propia o EMQX no responde?): %s'
                % ', '.join(fallidas),
            ))
