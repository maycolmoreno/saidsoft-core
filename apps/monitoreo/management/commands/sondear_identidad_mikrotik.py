"""Lee por SNMP el modelo, número de serie, versión de RouterOS y uptime de los
equipos de borde de las farmacias.

Tres cosas que hoy no se pueden ver de otra forma:

**Inventario sin ir al sitio.** El router entrega su modelo y su número de serie. Es
justo lo que quedó vacío al cargar la topología de ML016 a mano, esperando una planilla.

**Brecha de parcheo.** El primer sondeo (15-sep-2026) encontró MC001 en RouterOS 6.47.7
y GNB01/MCAR3 en 6.49.17. Nadie tenía forma de saber que un equipo estaba atrasado.

**Routers que se reinician solos.** Con el uptime, un equipo que arranca cada noche deja
de verse igual que uno estable — entre reinicio y reinicio responde perfecto al ping, así
que el monitoreo de enlace no lo distingue.

No hace falta correrlo seguido: el número de serie no cambia nunca y la versión solo
cuando alguien actualiza. Una corrida diaria alcanza.

    python manage.py sondear_identidad_mikrotik
    python manage.py sondear_identidad_mikrotik --farmacias GNB01,MC001
"""
from django.core.management.base import BaseCommand, CommandError

from apps.catalogo.models import Farmacia
from apps.monitoreo.mikrotik import sincronizar_identidad_equipos


class Command(BaseCommand):
    help = 'Lee por SNMP la identidad (modelo, serie, RouterOS, uptime) de los equipos de borde.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--farmacias',
            help='Códigos separados por comas. Vacío = todas las que tengan ip_router cargada.',
        )

    def handle(self, *args, **options):
        farmacias = None
        if options['farmacias']:
            codigos = [c.strip().upper() for c in options['farmacias'].split(',') if c.strip()]
            farmacias = list(Farmacia.objects.filter(codigo__in=codigos).exclude(ip_router__isnull=True))
            encontrados = {f.codigo for f in farmacias}
            faltantes = set(codigos) - encontrados
            if faltantes:
                raise CommandError(
                    'No existen o no tienen ip_router cargada: %s.' % ', '.join(sorted(faltantes)),
                )

        resumen = sincronizar_identidad_equipos(farmacias)

        self.stdout.write(self.style.SUCCESS('Equipos leídos: %d.' % resumen['leidos']))
        if resumen['sin_responder']:
            # No es un error de la corrida: a 15-sep-2026, 696 de 700 no tienen SNMP
            # habilitado todavía. Se informa para que el número sea visible y baje.
            self.stdout.write(self.style.WARNING(
                'Sin responder: %d (apagados, o todavía sin SNMP habilitado).' % resumen['sin_responder'],
            ))

        if resumen['versiones']:
            self.stdout.write('')
            self.stdout.write('Versiones de RouterOS encontradas:')
            for version, cuantos in sorted(resumen['versiones'].items()):
                self.stdout.write('  %-12s %d equipo(s)' % (version, cuantos))
            if len(resumen['versiones']) > 1:
                self.stdout.write(self.style.WARNING(
                    'Hay más de una versión en la flota: revisá cuáles conviene actualizar.',
                ))

        if resumen['nombres_discrepantes']:
            self.stdout.write('')
            self.stdout.write(self.style.ERROR(
                'La IP cargada apunta a un equipo que dice llamarse de otra forma. Si el nombre '
                'del equipo es el de OTRA farmacia, todo lo que se monitorea de esta es de esa:',
            ))
            for linea in resumen['nombres_discrepantes']:
                self.stdout.write(self.style.ERROR('  %s' % linea))
