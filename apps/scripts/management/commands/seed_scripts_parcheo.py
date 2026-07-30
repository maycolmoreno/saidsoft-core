"""Crea los scripts de arranque para parcheo de terceros vía winget.

A diferencia de sembrar catálogos (unidades de negocio, grupos), esto crea
contenido *ejecutable* que puede terminar corriendo en hasta 1.800 equipos —
por eso es un comando explícito, no una migración de datos automática.

    python manage.py seed_scripts_parcheo
"""
from django.contrib.auth.models import User
from django.core.management.base import BaseCommand
from django.db import transaction

from apps.scripts.models import Script, TipoScript

CONTENIDO_LISTAR = 'winget upgrade'
CONTENIDO_ACTUALIZAR = 'winget upgrade --all --silent --accept-source-agreements --accept-package-agreements'


class Command(BaseCommand):
    help = 'Crea los scripts compartidos de parcheo de terceros vía winget (biblioteca).'

    @transaction.atomic
    def handle(self, *args, **options):
        admin = User.objects.filter(is_superuser=True).order_by('id').first()
        if admin is None:
            self.stderr.write(self.style.ERROR('Necesitas al menos un superusuario antes de correr este seed.'))
            return

        creados = 0
        for nombre, descripcion, contenido, categoria in [
            (
                'Listar actualizaciones pendientes (winget)',
                'Solo diagnóstico: reporta qué apps de terceros tienen actualización disponible, sin aplicar nada.',
                CONTENIDO_LISTAR, 'Parcheo',
            ),
            (
                'Actualizar aplicaciones de terceros (winget)',
                'Aplica todas las actualizaciones de terceros disponibles vía winget, en silencio.',
                CONTENIDO_ACTUALIZAR, 'Parcheo',
            ),
        ]:
            _, creado = Script.objects.get_or_create(
                nombre=nombre, unidad_negocio=None,
                defaults={
                    'descripcion': descripcion, 'tipo': TipoScript.POWERSHELL, 'contenido': contenido,
                    'categoria': categoria, 'creado_por': admin,
                },
            )
            creados += int(creado)

        self.stdout.write(self.style.SUCCESS(f'{creados} script(s) de parcheo creado(s) (ya existentes se dejaron igual).'))
