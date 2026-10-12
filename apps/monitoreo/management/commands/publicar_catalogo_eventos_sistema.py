"""Publica el catálogo de eventos de Windows a la flota, retenido en el tópico global.

El gemelo de `publicar_catalogo_servicios_pos`, que faltaba. En la operación normal no hace
falta correrlo —el admin publica al guardar un `EventoSistemaVigilado`—, pero sin comando el
único modo de republicar era entrar al admin y guardar cualquier fila para que el `save_model`
lo dispare. Eso dejaba al catálogo de eventos sin equivalente de los cuatro casos que el
comando de servicios ya cubría:

- después de un `migrate` que sembró o cambió eventos vigilados (y hay una migración de datos
  que los siembra, así que pasa);
- cuando el broker estaba caído al guardar en el admin;
- después de `reaplicar_acls_mqtt`, por si algún agente estuvo denegado mientras faltaba la
  regla del tópico;
- y el que lo hizo necesario (11-oct-2026, `RIESGO-06`): **los dos catálogos ahora viajan
  firmados, y el mensaje retenido que ya está en el broker se publicó sin firma.** Ahí se
  queda hasta que alguien publique uno nuevo, así que después de desplegar hay que
  republicar los dos. El runbook está en `deploy/README-produccion.md`.

    python manage.py publicar_catalogo_eventos_sistema             # simula, muestra el JSON
    python manage.py publicar_catalogo_eventos_sistema --aplicar
"""
import json

from django.core.management.base import BaseCommand

from apps.monitoreo.servicios_pos import (
    TOPICO_CATALOGO_EVENTOS, catalogo_eventos_para_agentes, publicar_catalogo_eventos_sistema,
)


class Command(BaseCommand):
    help = 'Publica el catálogo de eventos de Windows a los agentes (MQTT retenido).'

    def add_arguments(self, parser):
        parser.add_argument(
            '--aplicar', action='store_true',
            help='Publica de verdad. Sin esto solo muestra lo que se enviaría.',
        )

    def handle(self, *args, **options):
        from apps.monitoreo.models import EventoSistemaVigilado

        eventos = catalogo_eventos_para_agentes()

        self.stdout.write(f'Tópico: {TOPICO_CATALOGO_EVENTOS} (retenido)')
        self.stdout.write('')
        self.stdout.write('Eventos vigilados:')
        for vigilado in EventoSistemaVigilado.objects.filter(activo=True).order_by('log', 'identificador'):
            self.stdout.write(
                f'  {vigilado.log:<12} {vigilado.proveedor:<24} {vigilado.identificador:<6} '
                f'{vigilado.nombre}',
            )
        if not eventos:
            self.stdout.write(self.style.WARNING(
                '  (ninguno activo: se publicaría una lista vacía, y los agentes dejarían de '
                'mirar el visor de eventos)',
            ))

        self.stdout.write('')
        self.stdout.write('JSON que viaja (sin el sobre firmado, que se arma al publicar):')
        self.stdout.write(json.dumps({'eventos': eventos}, indent=2, ensure_ascii=False))
        self.stdout.write('')

        if not options['aplicar']:
            self.stdout.write(self.style.WARNING(
                'SIMULACRO: no se publicó nada. Repetí con --aplicar.',
            ))
            return

        cuantos, enviado = publicar_catalogo_eventos_sistema()
        if enviado:
            self.stdout.write(self.style.SUCCESS(
                f'Publicado y firmado. {cuantos} evento(s) vigilado(s); las estaciones '
                'apagadas lo reciben al encender (mensaje retenido).',
            ))
        else:
            self.stderr.write(self.style.ERROR(
                'NO se pudo publicar: el broker no respondió. Las estaciones siguen con el '
                'catálogo anterior. Revisá que este proceso tenga las variables MQTT_*.',
            ))
