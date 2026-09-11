"""Arma el `config.txt` de cada estación esperada de una apertura, con su token.

Reemplaza el copiar/pegar a mano del token desde el panel a cada equipo, que es
justo donde se equivoca uno cuando hay tres cajas y un servidor esperando en el local.

    python manage.py generar_paquete_apertura --farmacia ML099                 # simula
    python manage.py generar_paquete_apertura --farmacia ML099 --destino C:\\paquetes --aplicar

**Escribe secretos en disco.** Por eso simula por defecto, como todo comando que escribe
en masa en este proyecto, y además:

- **Se niega a escribir dentro de `MEDIA_ROOT`.** Ese fue exactamente el incidente §10-Z:
  el paquete de instalación quedó publicado bajo `/media/` y se descargaba sin
  autenticación, con las credenciales de toda la flota adentro. Hubo que borrarlo y rotar
  los dos secretos. Este comando no puede repetirlo por accidente.
- **Emitir consume el token del perfil.** Si el perfil ya tiene uno vigente o usado, no se
  emite otro: se avisa y se saltea, porque el valor en claro del anterior ya no existe
  (solo se guarda el hash). Para reemplazarlo hay que revocar primero, desde el panel.

Lo que este comando NO arregla, y conviene tener presente: el `config.txt` sigue llevando
`MqttPassword` y `ComandoHmacSecret`, que son **compartidos por toda la flota** — el token
de apertura es de una sola estación, pero esos dos no. Mientras el agente necesite la
credencial compartida para el primer enrolamiento, un paquete filtrado sigue exponiéndolos.
Resolverlo de verdad pide HMAC por estación, que es un cambio aparte.
"""
from pathlib import Path

from django.conf import settings
from django.contrib.auth.models import User
from django.core.management.base import BaseCommand, CommandError

from apps.aperturas.models import Apertura, TokenApertura
from apps.aperturas.services import emitir_tokens

PLANTILLA_CONFIG = """CentralHost={central_host}
MqttPuerto={mqtt_puerto}
MqttPassword={mqtt_password}
ComandoHmacSecret={hmac_secret}
ServidorHora={servidor_hora}
TokenApertura={token}
"""


class Command(BaseCommand):
    help = 'Genera el config.txt de cada estación esperada de una apertura, con su token de enrolamiento.'

    def add_arguments(self, parser):
        parser.add_argument('--farmacia', required=True, help='Código de la farmacia que abre (ej. ML099).')
        parser.add_argument(
            '--destino', default='',
            help='Carpeta donde escribir los config.txt (uno por subcarpeta de estación). '
                 'Obligatorio con --aplicar.',
        )
        parser.add_argument(
            '--servidor-hora', default='farmaciasmia.int',
            help='Servidor NTP al que sincronizar la estación. Vacío desactiva la sincronización — pero '
                 'ojo: el agente descarta todo comando con más de 2 min de desfase de reloj.',
        )
        parser.add_argument('--aplicar', action='store_true', help='Escribe de verdad. Sin esto, solo simula.')

    def handle(self, *args, **options):
        apertura = self._apertura(options['farmacia'])
        destino = self._destino(options['destino'], options['aplicar'])

        usuario = User.objects.filter(is_superuser=True).order_by('id').first()
        if usuario is None:
            raise CommandError('Necesitás al menos un superusuario para emitir tokens.')

        ya_tenian = {
            t.perfil_id: t for t in apertura.tokens.select_related('perfil')
            if t.vigente or t.usado_en is not None
        }
        for token in ya_tenian.values():
            estado = 'ya usado' if token.usado_en else 'vigente'
            self.stderr.write(self.style.WARNING(
                f'-{token.perfil.sufijo}: ya tiene un token {estado} ({token.prefijo}…) y no se puede '
                'volver a leer en claro. Se saltea; revocalo desde el panel si necesitás uno nuevo.',
            ))

        pendientes = [p for p in apertura.plantilla.perfiles_estacion.all() if p.pk not in ya_tenian]
        if not pendientes:
            self.stdout.write('No hay ningún perfil sin token. Nada que generar.')
            return

        if not options['aplicar']:
            self.stdout.write(f'Se emitiría un token y un config.txt para {len(pendientes)} estación(es):')
            for perfil in pendientes:
                self.stdout.write(f'  {apertura.farmacia.codigo}-{perfil.sufijo} ({perfil.get_rol_display()})')
            self.stdout.write(self.style.WARNING(
                '\nSimulación: no se emitió ni se escribió nada. Repetí con --destino y --aplicar.',
            ))
            return

        emitidos = emitir_tokens(apertura=apertura, usuario=usuario)
        escritos = []
        for perfil, token_plano in emitidos:
            codigo = f'{apertura.farmacia.codigo}-{perfil.sufijo}'
            carpeta = destino / codigo
            carpeta.mkdir(parents=True, exist_ok=True)
            archivo = carpeta / 'config.txt'
            archivo.write_text(
                PLANTILLA_CONFIG.format(
                    central_host=self._central_host(),
                    mqtt_puerto=settings.MQTT_CONFIG['PORT'],
                    mqtt_password=settings.MQTT_CONFIG['PASSWORD'],
                    hmac_secret=settings.COMANDO_HMAC_SECRET,
                    servidor_hora=options['servidor_hora'],
                    token=token_plano,
                ),
                encoding='utf-8',
            )
            escritos.append((codigo, archivo))
            self.stdout.write(f'  {codigo}: {archivo}')

        self.stdout.write(self.style.SUCCESS(f'\n{len(escritos)} config.txt escrito(s) en {destino}.'))
        self.stdout.write(self.style.WARNING(
            'Cada uno lleva la contraseña MQTT y el COMANDO_HMAC_SECRET compartidos de la flota, '
            'además del token de esa estación. Copialos al equipo junto a Instalar.bat, cert.pem y '
            'Saidsoft.Agente.exe, y borralos de acá cuando termines. NO los publiques en /media/ '
            'ni en ningún lado servido por HTTP (ver §10-Z).',
        ))

    def _apertura(self, codigo_farmacia):
        apertura = (
            Apertura.objects
            .select_related('farmacia', 'plantilla')
            .filter(farmacia__codigo=codigo_farmacia.upper(), estado__in=Apertura.ESTADOS_VIGENTES)
            .first()
        )
        if apertura is None:
            raise CommandError(
                f'{codigo_farmacia.upper()} no tiene una apertura vigente. Creala en /aperturas/nueva/.',
            )
        if apertura.estado not in (Apertura.Estado.APROBADA, Apertura.Estado.EN_CURSO):
            raise CommandError(
                f'La apertura de {apertura.farmacia.codigo} está en estado "{apertura.get_estado_display()}". '
                'Tiene que estar aprobada por un segundo usuario antes de emitir tokens.',
            )
        return apertura

    def _destino(self, destino, aplicar):
        if not aplicar:
            return None
        if not destino:
            raise CommandError('--destino es obligatorio con --aplicar: hay que decir dónde escribir los secretos.')
        ruta = Path(destino).resolve()

        # §10-Z: el paquete anterior quedó publicado bajo /media/ y se descargaba sin
        # autenticación. Que este comando no pueda hacerlo por accidente.
        media = Path(settings.MEDIA_ROOT).resolve()
        if ruta == media or media in ruta.parents:
            raise CommandError(
                f'{ruta} está dentro de MEDIA_ROOT, que se sirve por HTTP sin autenticación. '
                'Elegí una carpeta fuera de ahí (ver §10-Z del plan).',
            )
        return ruta

    def _central_host(self):
        """Host del central tal como lo espera `Instalar.bat` (sin esquema ni puerto)."""
        base = settings.ARCHIVOS_BASE_URL
        sin_esquema = base.split('://', 1)[-1]
        return sin_esquema.split('/', 1)[0].split(':', 1)[0]
