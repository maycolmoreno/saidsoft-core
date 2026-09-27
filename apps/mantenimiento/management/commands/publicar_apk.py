"""Publica una versión de la app de campo en `media/movil/` y deja constancia de cuál es.

POR QUÉ EXISTE

La distribución del APK es manual: se copia a `media/movil/`, que nginx sirve público, y
alguien lo instala teléfono por teléfono. El problema no era copiar el archivo — era que
**nadie se enteraba de que había una versión nueva**. Un técnico podía pasar semanas con
un APK viejo sin ninguna señal de que existía otro, y con la distribución a mano eso es
lo normal, no la excepción.

Este comando hace dos cosas que a mano se olvidan:

1. **Nombra el archivo de forma canónica** (`saidsoft-campo-<version>.apk`), para que la
   carpeta no termine con `app-release.apk`, `app-release(1).apk` y `nuevo_final.apk`.
2. **Escribe `media/movil/version.json`**, que es lo que lee `/api/v1/version-app/` para
   avisarle a la app. Sin ese archivo el aviso no existe: el endpoint no adivina cuál de
   los APK de la carpeta es el bueno.

La versión NO se escribe a mano: sale de `movil-campo/pubspec.yaml`, que es de donde la
tomó el build. Escribirla aparte es garantizar que algún día no coincidan.

Simula por defecto y exige `--aplicar`, como el resto de los comandos que escriben
(ver CLAUDE.md).
"""
import json
import re
import shutil
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

#: Subcarpeta de MEDIA_ROOT donde nginx ya sirve lo que bajan los teléfonos.
SUBCARPETA = 'movil'

#: Lo que lee `/api/v1/version-app/`.
MANIFIESTO = 'version.json'


def leer_version_de_pubspec(raiz: Path) -> tuple[str, int]:
    """`version: 1.9.0+12` -> ('1.9.0', 12).

    El número de build (`+12`) es lo que compara la app: es el `versionCode` de Android,
    un entero que solo sube. Comparar '1.10.0' contra '1.9.0' como texto daría al revés.
    """
    pubspec = raiz / 'movil-campo' / 'pubspec.yaml'
    if not pubspec.is_file():
        raise CommandError(f'no se encontró {pubspec} — ¿se movió la app?')
    encontrado = re.search(r'^version:\s*([0-9.]+)\+(\d+)\s*$', pubspec.read_text(encoding='utf-8'), re.M)
    if not encontrado:
        raise CommandError(
            f'{pubspec} no tiene una línea `version: X.Y.Z+N` reconocible. '
            'Sin eso no se puede saber qué versión se está publicando.',
        )
    return encontrado.group(1), int(encontrado.group(2))


class Command(BaseCommand):
    help = (
        'Copia un APK compilado a media/movil/ con nombre canónico y actualiza el '
        'manifiesto que la app consulta para avisar que hay versión nueva. '
        'Simula por defecto; exige --aplicar para escribir.'
    )

    def add_arguments(self, parser):
        parser.add_argument('apk', help='Ruta al APK ya compilado y FIRMADO.')
        parser.add_argument(
            '--notas', default='',
            help='Qué trae esta versión, en una línea. Se le muestra al técnico.',
        )
        parser.add_argument(
            '--aplicar', action='store_true',
            help='Escribe de verdad. Sin esto solo muestra lo que haría.',
        )

    def handle(self, *args, **opciones):
        aplicar = opciones['aplicar']
        origen = Path(opciones['apk'])
        if not origen.is_file():
            raise CommandError(f'no existe {origen}.')
        if origen.suffix.lower() != '.apk':
            raise CommandError(f'{origen} no parece un APK.')

        raiz = Path(settings.BASE_DIR)
        version, build = leer_version_de_pubspec(raiz)

        destino_dir = Path(settings.MEDIA_ROOT) / SUBCARPETA
        nombre = f'saidsoft-campo-{version}+{build}.apk'
        destino = destino_dir / nombre
        manifiesto = destino_dir / MANIFIESTO

        # Aviso y no error: republicar el mismo build es legítimo (se corrigió la firma,
        # se perdió el archivo). Lo que no puede pasar es hacerlo sin darse cuenta.
        if destino.exists():
            self.stdout.write(self.style.WARNING(
                f'Ojo: ya existe {nombre} y se va a sobrescribir. Si esto es una versión '
                f'nueva de verdad, subí el `+{build}` en pubspec.yaml antes de compilar — '
                'la app compara ese número y no se va a enterar del cambio.',
            ))

        tamanio_mb = origen.stat().st_size / (1024 * 1024)
        self.stdout.write(f'APK:      {origen}  ({tamanio_mb:.1f} MB)')
        self.stdout.write(f'Version:  {version}+{build}  (de movil-campo/pubspec.yaml)')
        self.stdout.write(f'Destino:  {destino}')
        self.stdout.write(f'Manifiesto: {manifiesto}')

        if not aplicar:
            self.stdout.write(self.style.WARNING('\nSIMULACRO — no se escribió nada. Repetí con --aplicar.'))
            return

        destino_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(origen, destino)

        datos = {
            'version': version,
            'build': build,
            'archivo': nombre,
            'notas': opciones['notas'],
            'publicado_en': timezone.now().isoformat(),
        }
        manifiesto.write_text(json.dumps(datos, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')

        self.stdout.write(self.style.SUCCESS(f'\nPublicado {nombre}.'))
        self.stdout.write(
            'Las apps con un build anterior van a avisar que hay una versión nueva la '
            'próxima vez que tengan señal. Seguí distribuyéndolo como siempre: el aviso '
            'no instala nada, solo evita que un técnico pase semanas sin enterarse.',
        )
