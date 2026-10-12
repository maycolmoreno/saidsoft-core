"""Carga un build del agente como `VersionAgente`, sin pasar por el admin web.

    python manage.py cargar_version_agente ruta/Saidsoft.Agente.exe --usuario <username>
    python manage.py cargar_version_agente ruta/Saidsoft.Agente.exe --usuario <username> --aplicar

POR QUÉ EXISTE

El rollout del agente tenía un solo paso que no se podía hacer por línea de comandos:
`VersionAgente.ejecutable` es un `FileField` y la única vía era **Admin → Versiones de
agente**. Con eso, desplegar una versión nueva en un servidor sin interfaz gráfica —o desde
un runbook, o desde esta sesión— quedaba partido en dos: todo por SSH menos un formulario web.
`actualizar_agente_flota` cerró el otro lado (publicar la orden a la flota); esto cierra este.

**La versión NO se escribe a mano: sale de `agente-prueba/agente_prueba.py`**, que es de donde
la tomó el build. Es el mismo criterio que `publicar_apk` con `pubspec.yaml`, y por el mismo
motivo: escribirla aparte es garantizar que algún día no coincidan, y una `VersionAgente`
etiquetada con una versión que no es la del binario manda a la flota un ejecutable que
reporta otra cosa — el panel diría que el rollout no avanzó mientras avanzó, o al revés.

**Valida que el archivo sea un ejecutable de Windows** (los dos bytes `MZ` del encabezado PE).
No es paranoia de formato: el agente se actualiza **reemplazando su propio ejecutable**, así
que cargar acá un `.zip`, el `.py` o un `.exe` a medio escribir no da un error visible — deja
a la estación sin agente, y recuperarla es ir al equipo.

Simula por defecto y exige `--aplicar`, como el resto de los comandos que escriben
(ver CLAUDE.md).
"""
import hashlib
import re
from pathlib import Path

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.files import File
from django.core.management.base import BaseCommand, CommandError

from apps.catalogo.models import VersionAgente

#: Lo que PyInstaller produce y lo que el servicio de Windows ejecuta.
NOMBRE_CANONICO = 'Saidsoft.Agente.exe'


def leer_version_del_fuente(raiz: Path) -> str:
    """`VERSION_AGENTE_PRUEBA = 'agente-prueba-0.33'` -> 'agente-prueba-0.33'."""
    fuente = raiz / 'agente-prueba' / 'agente_prueba.py'
    if not fuente.is_file():
        raise CommandError(f'no se encontró {fuente} — ¿se movió el agente?')
    encontrado = re.search(
        r"^VERSION_AGENTE_PRUEBA\s*=\s*['\"]([^'\"]+)['\"]",
        fuente.read_text(encoding='utf-8'), re.M,
    )
    if not encontrado:
        raise CommandError(
            f'{fuente} no tiene una línea `VERSION_AGENTE_PRUEBA = "..."` reconocible. '
            'Sin eso no se puede saber qué versión se está cargando.',
        )
    return encontrado.group(1)


class Command(BaseCommand):
    help = 'Carga un .exe del agente como VersionAgente. Simula salvo --aplicar.'

    def add_arguments(self, parser):
        parser.add_argument('archivo', help=f'Ruta al {NOMBRE_CANONICO} compilado con build.ps1.')
        parser.add_argument(
            '--usuario', required=True,
            help='Username de quien carga la versión. Obligatorio: `VersionAgente.creado_por` '
                 'queda en el historial y atribuirlo a quien no lo hizo lo vuelve inútil.',
        )
        parser.add_argument(
            '--aplicar', action='store_true',
            help='Carga de verdad. Sin esto solo muestra qué se cargaría.',
        )

    def handle(self, *args, **opciones):
        ruta = Path(opciones['archivo']).expanduser()
        if not ruta.is_file():
            raise CommandError(f'no existe el archivo {ruta}')

        cabecera = ruta.open('rb').read(2)
        if cabecera != b'MZ':
            raise CommandError(
                f'{ruta} no es un ejecutable de Windows (no empieza con "MZ"). El agente '
                'reemplaza su propio ejecutable con esto: cargar otra cosa deja a la estación '
                'sin agente. ¿Es el dist/Saidsoft.Agente.exe que produjo build.ps1?',
            )

        Usuario = get_user_model()
        autor = Usuario.objects.filter(username=opciones['usuario']).first()
        if autor is None:
            raise CommandError(f'no existe el usuario {opciones["usuario"]!r}.')

        version = leer_version_del_fuente(Path(settings.BASE_DIR))
        ya_cargada = VersionAgente.objects.filter(version=version).first()

        digest = hashlib.sha256()
        with ruta.open('rb') as fh:
            for bloque in iter(lambda: fh.read(1024 * 1024), b''):
                digest.update(bloque)
        sha256 = digest.hexdigest()

        self.stdout.write(f'Versión (de agente_prueba.py): {version}')
        self.stdout.write(f'Archivo:  {ruta}')
        self.stdout.write(f'Tamaño:   {ruta.stat().st_size / (1024 * 1024):.1f} MB')
        self.stdout.write(f'sha256:   {sha256}')
        self.stdout.write(f'Autor:    {autor.username}')

        if ya_cargada is not None:
            # `version` es unique, así que esto fallaría igual; se corta antes para decir
            # algo útil. Y se compara el hash: si el binario es otro, el aviso importa —
            # significa que se recompiló sin subir el número de versión, y la flota no
            # tiene forma de distinguir los dos builds.
            mismo = ya_cargada.sha256 == sha256
            self.stderr.write(
                f'{version} ya está cargada (desde {ya_cargada.fecha_creacion:%Y-%m-%d %H:%M}) y '
                + ('es EL MISMO binario: no hay nada que cargar.' if mismo else
                   'es OTRO binario. Se recompiló sin cambiar VERSION_AGENTE_PRUEBA, así que '
                   'la flota no puede distinguir los dos builds: subí el número de versión en '
                   'agente_prueba.py y recompilá.'),
            )
            return

        if not opciones['aplicar']:
            self.stdout.write(self.style.WARNING('\nSIMULACIÓN — no se cargó nada. Repetí con --aplicar.'))
            return

        with ruta.open('rb') as fh:
            creada = VersionAgente(version=version, creado_por=autor)
            creada.ejecutable.save(NOMBRE_CANONICO, File(fh), save=True)

        self.stdout.write(self.style.SUCCESS(f'\nCargada: {creada.version}'))
        if creada.sha256 != sha256:
            # El modelo recalcula el hash al guardar (el agente nunca confía en uno que le
            # llegue por otro lado, SEC-1). Si no coincide con el del archivo local, algo se
            # corrompió en el camino y hay que borrarla antes de publicarla a la flota.
            self.stderr.write(
                f'ATENCIÓN: el sha256 guardado ({creada.sha256[:12]}…) no coincide con el del '
                f'archivo ({sha256[:12]}…). NO la publiques: borrala y volvé a cargarla.',
            )
            return
        self.stdout.write(
            'El sha256 guardado coincide con el del archivo.\n'
            'Siguiente paso: `actualizar_agente_flota --limite 3 --aplicar` para el canario.',
        )
