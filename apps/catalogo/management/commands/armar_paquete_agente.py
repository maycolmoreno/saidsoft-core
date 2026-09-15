"""Arma el zip de instalación del agente y lo publica en /media/ para bajarlo desde
cualquier estación.

Reemplaza el copiado manual de una carpeta a cada equipo por:

    mkdir C:\\Instalador-Saidsoft
    cd C:\\Instalador-Saidsoft
    curl.exe -o agente-instalador.zip http://10.111.6.20:8080/media/agente-instalador/agente-instalador.zip
    tar -xf agente-instalador.zip
    .\\Instalar.bat

(`curl.exe` y `tar` vienen con Windows 10 1803+; no hace falta PowerShell.)

**El paquete NO lleva `config.txt`.** Ese archivo tiene `MqttPassword` y
`ComandoHmacSecret` en texto plano, y `/media/` se sirve por HTTP **sin autenticación**
a propósito para que los agentes descarguen. Publicarlo ahí entregaría a cualquiera que
alcance el servidor el secreto con el que se FIRMAN los comandos a las estaciones: con
eso se puede fabricar un `ejecutar_script` válido para cualquier equipo de la cadena.

Por eso el zip incluye `config.ejemplo.txt` y quien instala completa los dos valores en
la estación. Es un paso manual más, y es el que evita repetir la fuga que documenta
PLAN_MODERNIZACION §10-N.

El `cert.pem` sí va: es el certificado **público** de EMQX, lo que los agentes usan para
validar TLS. El privado (`key.pem`) no se toca.

    python manage.py armar_paquete_agente
    python manage.py armar_paquete_agente --agente agente-prueba-0.19
"""
import zipfile
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from apps.catalogo.models import VersionAgente

# Lo que va adentro: (ruta en el repo, nombre dentro del zip).
ARCHIVOS_DEL_REPO = [
    ('agente-prueba/instalar-servicio.ps1', 'instalar-servicio.ps1'),
    ('agente-prueba/Instalar.bat', 'Instalar.bat'),
    ('agente-prueba/config.ejemplo.txt', 'config.ejemplo.txt'),
    ('deploy/certs/cert.pem', 'cert.pem'),
]

CARPETA_PUBLICADA = 'agente-instalador'
NOMBRE_ZIP = 'agente-instalador.zip'

LEEME = """INSTALADOR DEL AGENTE SAIDSOFT — version {version}

1. Copia config.ejemplo.txt a config.txt y completa los dos valores vacios:
      MqttPassword        -> MQTT_PASSWORD_AGENTE del deploy/.env del servidor
      ComandoHmacSecret   -> COMANDO_HMAC_SECRET del deploy/.env del servidor

   Esos dos NO vienen en el paquete a proposito: este zip se descarga por HTTP sin
   autenticacion, y el secreto HMAC es con el que se firman los comandos a las
   estaciones. Publicarlo seria entregar el control de la flota.

2. Verifica que el nombre del equipo siga la convencion FARMACIA-SUFIJO (ej. ML016-C):

      hostname

   Si no la cumple, renombra el equipo o pasa -Codigo al instalador. Este paquete NO
   renombra nada.

3. Doble clic en Instalar.bat (se autoeleva a Administrador).

4. Verifica:

      sc query SaidsoftAgente
      type C:\\ProgramData\\Saidsoft\\agente_prueba.log

   El log NO queda en la carpeta de instalacion: va a C:\\ProgramData\\Saidsoft\\.

5. La estacion queda PENDIENTE DE APROBACION en el panel (/estaciones/). Hasta que
   alguien la apruebe, no le llegan scripts ni software ni despliegues.

   Para una farmacia que abre, completa TokenApertura en config.txt con el token que
   emite el panel: con el, la estacion se enrola YA APROBADA y con la configuracion de
   su perfil aplicada.
"""


class Command(BaseCommand):
    help = 'Arma el zip de instalación del agente y lo publica en /media/agente-instalador/.'

    def add_arguments(self, parser):
        # `--version` no se puede: BaseCommand lo reserva para imprimir la versión de
        # Django y argparse rechaza el duplicado.
        parser.add_argument(
            '--agente',
            help='Versión de agente a empaquetar, ej. agente-prueba-0.20. '
                 'Vacío = la última cargada.',
        )

    def handle(self, *args, **options):
        if options['agente']:
            version = VersionAgente.objects.filter(version=options['agente']).first()
            if version is None:
                raise CommandError(
                    'No existe la versión "%s". Cargadas: %s.'
                    % (options['agente'], ', '.join(
                        VersionAgente.objects.order_by('-fecha_creacion').values_list('version', flat=True)[:5],
                    )),
                )
        else:
            version = VersionAgente.objects.order_by('-fecha_creacion').first()
            if version is None:
                raise CommandError(
                    'No hay ninguna VersionAgente cargada. Subí el ejecutable primero '
                    '(Admin → Versiones de agente).',
                )

        base = Path(settings.BASE_DIR)
        faltantes = [ruta for ruta, _ in ARCHIVOS_DEL_REPO if not (base / ruta).is_file()]
        if faltantes:
            raise CommandError('Faltan archivos del paquete: %s.' % ', '.join(faltantes))

        exe = Path(version.ejecutable.path)
        if not exe.is_file():
            raise CommandError('El ejecutable de %s no está en disco (%s).' % (version.version, exe))

        destino = Path(settings.MEDIA_ROOT) / CARPETA_PUBLICADA
        destino.mkdir(parents=True, exist_ok=True)
        ruta_zip = destino / NOMBRE_ZIP

        with zipfile.ZipFile(ruta_zip, 'w', zipfile.ZIP_DEFLATED) as paquete:
            paquete.write(exe, 'Saidsoft.Agente.exe')
            for ruta, nombre in ARCHIVOS_DEL_REPO:
                paquete.write(base / ruta, nombre)
            paquete.writestr('LEEME.txt', LEEME.format(version=version.version))

        # Comprobación explícita: si alguna vez alguien agrega config.txt a la lista, este
        # chequeo falla antes de publicar en vez de exponer los secretos en silencio.
        with zipfile.ZipFile(ruta_zip) as paquete:
            nombres = paquete.namelist()
        if 'config.txt' in nombres:
            ruta_zip.unlink()
            raise CommandError(
                'El paquete incluía config.txt, que lleva secretos en texto plano y esto se '
                'publica sin autenticación. No se publicó nada.',
            )

        self.stdout.write(self.style.SUCCESS(
            'Paquete armado con %s (%.1f MB).' % (version.version, ruta_zip.stat().st_size / 1048576),
        ))
        self.stdout.write('Contiene: %s' % ', '.join(sorted(nombres)))
        self.stdout.write('')
        self.stdout.write('Desde una estación, en cmd como Administrador:')
        self.stdout.write('  mkdir C:\\Instalador-Saidsoft && cd C:\\Instalador-Saidsoft')
        self.stdout.write(
            '  curl.exe -o agente-instalador.zip '
            'http://%s:8080/media/%s/%s' % (
                (settings.ALLOWED_HOSTS or ['SERVIDOR'])[0], CARPETA_PUBLICADA, NOMBRE_ZIP,
            ),
        )
        self.stdout.write('  tar -xf agente-instalador.zip')
        self.stdout.write('  (copiar config.ejemplo.txt a config.txt y completar los dos valores)')
        self.stdout.write('  Instalar.bat')
        self.stdout.write('')
        self.stdout.write(self.style.WARNING(
            'El paquete NO lleva config.txt: se descarga sin autenticación y ese archivo tiene '
            'el secreto con el que se firman los comandos a las estaciones.',
        ))
