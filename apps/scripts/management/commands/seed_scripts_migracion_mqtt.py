"""Crea el script de migración a credencial MQTT propia por estación.

Ver PLAN_MODERNIZACION.md §9 ("ACLs MQTT/EMQX por tenant") y
apps.mqtt_worker.emqx_admin: desde que el agente sabe pedir/usar una credencial MQTT
propia (requiere la versión del agente con ese soporte — hoy agente-prueba/
agente_prueba.py en Python, ver PLAN_MODERNIZACION.md §10-K sobre por qué reemplazó al
agente C# original), una estación solo la obtiene si vuelve a pasar por el enrolamiento
— y el agente no vuelve a enrolarse solo si ya tiene un token guardado en
identidad.json (`_on_connect` en agente_prueba.py). Este script fuerza ese
re-enrolamiento: borra identidad.json y reinicia el servicio. Como la Estacion ya existe
y ya está aprobada en la BD, el round-trip de re-enrolamiento es instantáneo (sin
esperar aprobación manual) y esta vez la respuesta trae la credencial MQTT propia.

A diferencia de sembrar catálogos, esto crea contenido *ejecutable* que puede terminar
corriendo en hasta 1.800 equipos — por eso es un comando explícito, no una migración de
datos automática. Correrlo NO reprovisiona nada por sí solo: solo crea el Script en la
biblioteca; el usuario decide cuándo y sobre qué estaciones ejecutarlo (ejecutar_script /
ScriptProgramado), típicamente una vez confirmado que ya tienen el agente nuevo
instalado (ver Estacion.version_agente).

    python manage.py seed_scripts_migracion_mqtt
"""
from django.contrib.auth.models import User
from django.core.management.base import BaseCommand

from apps.scripts.models import Script, TipoScript

NOMBRE = 'Migrar a credencial MQTT propia'
DESCRIPCION = (
    'Fuerza un re-enrolamiento borrando identidad.json y reiniciando el servicio del '
    'agente, para que reciba su propia credencial MQTT (en vez de la compartida) en el '
    'siguiente enrolamiento. Requiere que la estación ya tenga instalado el agente que '
    'sabe usar credenciales por estación — correrlo contra un agente viejo es inofensivo '
    'pero no hace nada (el agente viejo ignora los campos mqtt_username/mqtt_password '
    'de la respuesta).'
)
CONTENIDO = r"""
$identidad = Join-Path $env:ProgramData 'Saidsoft\identidad.json'
if (Test-Path $identidad) {
    Remove-Item $identidad -Force
    Write-Output "identidad.json eliminado, forzando re-enrolamiento."
} else {
    Write-Output "identidad.json no existe, nada que borrar."
}
Restart-Service -Name 'SaidsoftAgente' -Force
"""


class Command(BaseCommand):
    help = 'Crea el script (biblioteca) que fuerza la migración de una estación a credencial MQTT propia.'

    def handle(self, *args, **options):
        admin = User.objects.filter(is_superuser=True).order_by('id').first()
        if admin is None:
            self.stderr.write(self.style.ERROR('Necesitas al menos un superusuario antes de correr este seed.'))
            return

        _, creado = Script.objects.get_or_create(
            nombre=NOMBRE, unidad_negocio=None,
            defaults={
                'descripcion': DESCRIPCION, 'tipo': TipoScript.POWERSHELL, 'contenido': CONTENIDO,
                'categoria': 'Migración MQTT', 'creado_por': admin,
            },
        )
        if creado:
            self.stdout.write(self.style.SUCCESS(f'Script "{NOMBRE}" creado.'))
        else:
            self.stdout.write(f'Script "{NOMBRE}" ya existía, se dejó igual.')
