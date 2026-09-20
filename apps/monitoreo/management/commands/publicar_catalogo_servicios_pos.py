"""Publica el catálogo de servicios del POS a la flota, retenido en el tópico global.

En la operación normal no hace falta correrlo: el admin publica solo al guardar (ver
`ServicioPosMonitoreadoAdmin.save_model`). Existe para los casos en que eso no alcanza:

- después de un `migrate` que sembró o cambió entradas del catálogo;
- cuando el broker estaba caído al guardar en el admin y quedó el aviso amarillo;
- después de correr `reaplicar_acls_mqtt`, para reenviar por si algún agente estuvo
  denegado mientras faltaba la regla;
- para MIRAR qué se va a publicar, sin publicar, que es lo que hace sin `--aplicar`.

    python manage.py publicar_catalogo_servicios_pos             # simula, muestra el JSON
    python manage.py publicar_catalogo_servicios_pos --aplicar
"""
import json

from django.core.management.base import BaseCommand

from apps.monitoreo.servicios_pos import TOPICO_CATALOGO, payload_catalogo, publicar_catalogo_servicios_pos


class Command(BaseCommand):
    help = 'Publica el catálogo de servicios del POS a los agentes (MQTT retenido).'

    def add_arguments(self, parser):
        parser.add_argument(
            '--aplicar', action='store_true',
            help='Publica de verdad. Sin esto solo muestra lo que se enviaría.',
        )

    def handle(self, *args, **options):
        from apps.monitoreo.models import ServicioPosMonitoreado

        payload = payload_catalogo()

        self.stdout.write(f'Tópico: {TOPICO_CATALOGO} (retenido)')
        self.stdout.write('')
        self.stdout.write('Catálogo activo:')
        for servicio in ServicioPosMonitoreado.objects.filter(activo=True):
            if servicio.origen == ServicioPosMonitoreado.Origen.CONFIG_POS:
                donde = 'lo descubre el agente del .exe.Config'
            else:
                donde = servicio.destino
            marca = self.style.WARNING('CRITICO') if servicio.critico else '       '
            self.stdout.write(f'  {marca} {servicio.clave:<16} {servicio.tipo:<9} {donde}')

        desactivados = payload['desactivados']
        if desactivados:
            self.stdout.write('')
            self.stdout.write(
                'Se les pide DEJAR de chequear: %s' % ', '.join(desactivados),
            )

        self.stdout.write('')
        self.stdout.write('JSON que viaja:')
        self.stdout.write(json.dumps(payload, indent=2, ensure_ascii=False))
        self.stdout.write('')

        if not options['aplicar']:
            self.stdout.write(self.style.WARNING(
                'SIMULACRO: no se publicó nada. Repetí con --aplicar.',
            ))
            return

        cuantos, enviado = publicar_catalogo_servicios_pos()
        if enviado:
            self.stdout.write(self.style.SUCCESS(
                f'Publicado. {cuantos} servicio(s) con destino fijo; las estaciones '
                'apagadas lo reciben al encender (mensaje retenido).',
            ))
        else:
            self.stderr.write(self.style.ERROR(
                'NO se pudo publicar: el broker no respondió. Las estaciones siguen con '
                'el catálogo anterior. Revisá que este proceso tenga las variables MQTT_*.',
            ))
