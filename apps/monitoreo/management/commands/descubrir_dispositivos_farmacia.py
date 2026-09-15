"""Descubre por SNMP qué equipos están realmente conectados en cada farmacia.

Lee la tabla ARP del Mikrotik: IP y MAC de todo lo que habló por su LAN. Es la mitad
**verificada** del inventario — `Activo` guarda lo que alguien declaró que hay, esto
guarda lo que el router ve, sin que nadie visite el local.

El cruce entre las dos responde dos preguntas que hoy no se pueden contestar:

- **Qué hay enchufado que nadie inventarió.** Sale en el resumen de cada corrida.
- **Qué está inventariado pero no aparece.** Un `Activo` con MAC cargada que el router
  nunca ve puede estar apagado, robado, o cargado con la MAC equivocada.

También llena solo los campos que quedaron vacíos al cargar la topología a mano: el
router sabe la IP y la MAC de cada equipo de la farmacia.

    python manage.py descubrir_dispositivos_farmacia
    python manage.py descubrir_dispositivos_farmacia --farmacias MCAR3,MC001
"""
from django.core.management.base import BaseCommand, CommandError

from apps.catalogo.models import Farmacia
from apps.monitoreo.mikrotik import sincronizar_dispositivos_detectados


class Command(BaseCommand):
    help = 'Lee la tabla ARP de los Mikrotik y registra los equipos conectados en cada farmacia.'

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
            faltantes = set(codigos) - {f.codigo for f in farmacias}
            if faltantes:
                raise CommandError(
                    'No existen o no tienen ip_router cargada: %s.' % ', '.join(sorted(faltantes)),
                )

        resumen = sincronizar_dispositivos_detectados(farmacias)

        self.stdout.write(self.style.SUCCESS(
            'Farmacias leídas: %d. Equipos nuevos: %d, ya conocidos: %d.'
            % (resumen['farmacias_leidas'], resumen['nuevos'], resumen['actualizados']),
        ))
        if resumen['sin_responder']:
            self.stdout.write(self.style.WARNING(
                'Sin responder: %d (apagadas, o todavía sin SNMP habilitado).' % resumen['sin_responder'],
            ))

        if resumen['sin_declarar']:
            self.stdout.write('')
            self.stdout.write(self.style.WARNING(
                'Conectados pero SIN inventariar (%d). No es necesariamente un problema: '
                'puede ser un equipo legítimo que falta cargar, o uno que no debería estar ahí.'
                % len(resumen['sin_declarar']),
            ))
            for linea in resumen['sin_declarar'][:40]:
                self.stdout.write(self.style.WARNING('  %s' % linea))
            if len(resumen['sin_declarar']) > 40:
                self.stdout.write(self.style.WARNING(
                    '  … y %d más. Mirá la lista completa en el admin.' % (len(resumen['sin_declarar']) - 40),
                ))
