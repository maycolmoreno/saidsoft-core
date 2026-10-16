"""Genera el script de PowerShell que corren los técnicos para instalar una estación.

    # en el servidor, desde deploy/
    docker compose exec -T -e MQTT_PASSWORD_AGENTE="$(grep ^MQTT_PASSWORD_AGENTE= .env | cut -d= -f2-)" \
        web python manage.py generar_script_instalacion > Instalar-Saidsoft.ps1

El script resultante hace, en una sola corrida y sin preguntar nada:

  1. baja el paquete del agente de /media/ y lo descomprime
  2. escribe config.txt **con la MqttPassword ya puesta**
  3. instala el servicio SaidsoftAgente
  4. instala el agente de MeshCentral con `--agentName=<código>`, que es lo que hace que
     el nodo se vincule solo a la estación sin copiar node_id a mano

**Por qué esto y no meter la clave en el zip publicado.** `armar_paquete_agente` deja
`MqttPassword` afuera a propósito: `/media/` se sirve por HTTP **sin autenticación**
—tiene que serlo, es de donde descargan los agentes— así que todo lo que entre al zip
queda legible para cualquiera que alcance el servidor.

Y no alcanza con angostar la ACL del usuario compartido: aun después de correr
`deploy/emqx-narrow-acl-agente.sh`, esa credencial conserva `subscribe` sobre
`/saidsof/enrolamiento/respuesta/+/` **con comodín**, así que quien la tenga puede
escuchar la respuesta de enrolamiento de CUALQUIER estación y quedarse con su
credencial propia y su secreto HMAC. Publicarla sería regalar esa capacidad.

Este script, en cambio, se le entrega a cada técnico por un canal privado. Es el mismo
secreto, pero deja de estar publicado: quien lo tiene es alguien a quien ya se lo
confiaste.

**Nunca lo escribas dentro del repo ni lo subas a /media/.**
"""
import os

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from apps.catalogo.instalador import generar_script
from apps.catalogo.models import VersionAgente


class Command(BaseCommand):
    help = 'Genera el script de PowerShell de instalación (incluye la MqttPassword — canal privado).'

    def add_arguments(self, parser):
        parser.add_argument(
            '--mqtt-password', default='',
            help='MQTT_PASSWORD_AGENTE. Si se omite, se lee de la variable de entorno del mismo nombre.',
        )
        parser.add_argument(
            '--central-host', default='',
            help='Host del central. Por defecto se deduce de ARCHIVOS_BASE_URL.',
        )
        parser.add_argument('--mqtt-puerto', type=int, default=8081)

    def handle(self, *args, **opciones):
        password = opciones['mqtt_password'] or os.environ.get('MQTT_PASSWORD_AGENTE', '')
        if not password:
            raise CommandError(
                'Falta la clave. Pasala con --mqtt-password o exportá MQTT_PASSWORD_AGENTE.\n'
                'Nunca la escribas en el repo: vive solo en deploy/.env del servidor.',
            )

        version = VersionAgente.objects.order_by('-fecha_creacion').first()
        if version is None:
            raise CommandError('No hay ninguna VersionAgente cargada; no hay paquete que instalar.')

        if not settings.MESHCENTRAL_CONFIG.get('MESH_ID'):
            self.stderr.write(
                'AVISO: MESHCENTRAL_MESH_ID no está configurado — el script va a quedar '
                'sin la parte de MeshCentral utilizable.',
            )
        self.stdout.write(generar_script(
            password, central_host=opciones['central_host'], mqtt_puerto=opciones['mqtt_puerto'],
        ))
        self.stderr.write(
            f'\nGenerado para {version.version}. CONTIENE LA CLAVE MQTT: entregalo por un '
            'canal privado y no lo subas al repo ni a /media/.',
        )
