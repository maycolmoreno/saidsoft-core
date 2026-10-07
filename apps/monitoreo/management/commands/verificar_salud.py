"""Verifica que el propio SAIDSOFT esté sano. Sale con código 1 si algo no lo está.

Por qué hace falta: el sistema vigila 700 farmacias y no había nada vigilándolo a él.
Si el worker MQTT se cuelga sin morir, Docker lo reporta "corriendo"; si Celery Beat
deja de disparar, las tareas simplemente no ocurren. En los dos casos el síntoma es
ausencia de datos, que se descubre tarde y por casualidad — alguien nota que hace rato
no llega nada.

Está pensado para correr DESDE AFUERA del stack, con un temporizador de systemd igual
que el respaldo. Esa es la diferencia que importa: una alerta generada por Celery no
sirve para avisar que Celery se cayó. Un proceso externo sí.

El código de salida es el contrato: 0 = todo bien, 1 = algo mal. Así un temporizador o
un monitor externo puede avisar sin interpretar el texto.

    python manage.py verificar_salud
    python manage.py verificar_salud --silencioso    # solo los problemas

Los umbrales salen de `apps.monitoreo.umbrales`, que es el mismo lugar del que los lee
el panel para pintar de rojo. Duplicarlos acá haría que la consola y la pantalla
discreparan sobre qué es "sano", y hasta el 2-oct-2026 se evitaba al revés —
importándolos de `apps.panel.views.dashboard`, o sea haciendo que este comando dependiera
de una vista.
"""
from django.core.management.base import BaseCommand, CommandError
from django.db import connection
from django.utils import timezone

from apps.monitoreo.umbrales import RESPALDO_UMBRAL_HORAS, WORKER_MQTT_UMBRAL_SEGUNDOS
from apps.mqtt_worker.management.commands.registrar_latido import NOMBRE_RESPALDO
from apps.mqtt_worker.models import WorkerHeartbeat
from apps.mqtt_worker.services import NOMBRE_WORKER_MQTT

NOMBRE_WORKER_MESHCENTRAL = 'meshcentral_worker'

# Celery Beat no registra latido propio; se infiere de que las tareas periódicas estén
# escribiendo. La más frecuente corre cada 5 minutos, así que 20 sin una sola muestra
# nueva significa que el planificador no está disparando.
BEAT_UMBRAL_MINUTOS = 20

# Por debajo de esto, una purga o un respaldo pueden fallar a mitad de camino y dejar la
# base en un estado peor que el que tenían.
DISCO_LIBRE_MINIMO_PCT = 10


class Command(BaseCommand):
    help = 'Verifica la salud del propio SAIDSOFT. Código de salida 1 si algo falla.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--silencioso', action='store_true',
            help='Imprime solo los problemas. Útil para un temporizador que manda correo '
                 'solo cuando hay salida.',
        )
        parser.add_argument(
            '--solo',
            help='Verifica un único componente (ej. mqtt_worker). Para el healthcheck de '
                 'un contenedor, que debe medirse a sí mismo y no al sistema entero.',
        )

    def handle(self, *args, **options):
        silencioso = options['silencioso']
        solo = options['solo']
        problemas = []
        revisados = []

        def revisar(nombre, ok, detalle):
            revisados.append(nombre)
            # Acotar acá y no en cada _revisar_* mantiene un solo lugar donde se decide
            # qué se mira. Importa porque el healthcheck de un contenedor tiene que
            # medirse a SÍ MISMO: si el worker MQTT se marcara enfermo por un respaldo
            # atrasado, Docker lo reiniciaría en loop sin arreglar nada.
            if solo and nombre != solo:
                return
            if ok:
                if not silencioso:
                    self.stdout.write(self.style.SUCCESS('  OK      %-22s %s' % (nombre, detalle)))
            else:
                problemas.append(nombre)
                self.stdout.write(self.style.ERROR('  PROBLEMA %-21s %s' % (nombre, detalle)))

        if not silencioso:
            self.stdout.write('Salud de SAIDSOFT — %s' % timezone.localtime().strftime('%Y-%m-%d %H:%M:%S'))

        self._revisar_base(revisar)
        self._revisar_latidos(revisar)
        self._revisar_beat(revisar)
        self._revisar_sondeo_enlaces(revisar)
        self._revisar_telegram(revisar)
        self._revisar_disco(revisar)

        if solo and solo not in revisados:
            raise CommandError(
                'No existe el componente "%s". Verificables: %s.'
                % (solo, ', '.join(revisados)),
            )

        if problemas:
            self.stdout.write('')
            self.stdout.write(self.style.ERROR(
                '%d problema(s): %s' % (len(problemas), ', '.join(problemas)),
            ))
            # SystemExit y no CommandError: CommandError imprime "CommandError:" y sale
            # con 1 igual, pero acá el texto ya se explicó arriba y repetirlo confunde.
            raise SystemExit(1)

        if not silencioso:
            self.stdout.write('')
            self.stdout.write(self.style.SUCCESS('Todo sano.'))

    def _revisar_base(self, revisar):
        try:
            with connection.cursor() as cursor:
                cursor.execute('SELECT 1')
                cursor.fetchone()
            revisar('base de datos', True, 'responde')
        except Exception as exc:
            revisar('base de datos', False, 'no responde: %s' % exc)

    def _revisar_latidos(self, revisar):
        ahora = timezone.now()
        latidos = {h.nombre: h.ultimo_latido for h in WorkerHeartbeat.objects.all()}

        for nombre, umbral_segundos in (
            (NOMBRE_WORKER_MQTT, WORKER_MQTT_UMBRAL_SEGUNDOS),
            (NOMBRE_WORKER_MESHCENTRAL, WORKER_MQTT_UMBRAL_SEGUNDOS),
        ):
            ultimo = latidos.get(nombre)
            if ultimo is None:
                revisar(nombre, False, 'nunca registró un latido')
                continue
            atraso = (ahora - ultimo).total_seconds()
            revisar(
                nombre, atraso <= umbral_segundos,
                'último latido hace %.0f s (umbral %d s)' % (atraso, umbral_segundos),
            )

        ultimo = latidos.get(NOMBRE_RESPALDO)
        if ultimo is None:
            # Distinto de "atrasado": nunca corrió. Puede ser una instalación nueva, así
            # que se avisa sin marcarlo como problema — lo contrario llenaría de ruido el
            # primer día de cualquier despliegue.
            revisar('respaldo', True, 'sin registro todavía (¿instalación nueva?)')
        else:
            horas = (ahora - ultimo).total_seconds() / 3600
            revisar(
                'respaldo', horas <= RESPALDO_UMBRAL_HORAS,
                'último hace %.1f h (umbral %d h)' % (horas, RESPALDO_UMBRAL_HORAS),
            )

    def _revisar_beat(self, revisar):
        """Celery Beat no deja latido propio: se infiere de que sus tareas escriban.

        Se mira la muestra de red más reciente porque su tarea es la más frecuente (cada
        5 minutos). Si Beat dejara de disparar, esta tabla es la primera en quedarse
        quieta.
        """
        from apps.monitoreo.models import MuestraRedFarmacia

        ultima = MuestraRedFarmacia.objects.order_by('-timestamp').values_list('timestamp', flat=True).first()
        if ultima is None:
            revisar('celery beat', True, 'sin muestras todavía (¿ninguna farmacia sondeable?)')
            return
        minutos = (timezone.now() - ultima).total_seconds() / 60
        revisar(
            'celery beat', minutos <= BEAT_UMBRAL_MINUTOS,
            'última muestra hace %.0f min (umbral %d min)' % (minutos, BEAT_UMBRAL_MINUTOS),
        )

    def _revisar_sondeo_enlaces(self, revisar):
        """El barrido de enlaces corre cada 2 min, pero ABORTA si falla el 80% o más.

        Ese abortado es la protección funcionando —asume que el que perdió la ruta es
        este host, no que se cayeron 704 farmacias a la vez— y no registra nada. El
        problema es que hasta el 2-oct-2026 era INVISIBLE: solo dejaba un `logger.error`
        que nadie mira, mientras el panel seguía mostrando los últimos estados buenos
        como si fueran de ahora. El síntoma que llegó fue "hay enlaces marcados caídos
        que ya tienen conexión", y la causa real era que hacía 4,7 h que no se registraba
        un solo sondeo.

        Se mide por la frescura del estado y no leyendo el log: si `ultima_verificacion`
        envejece parejo en toda la flota, el barrido no está escribiendo, sin importar
        por qué. Misma idea que `_revisar_beat`, que infiere Beat de que sus tareas
        escriban.
        """
        from apps.monitoreo.models import EstadoEnlaceFarmacia
        from apps.monitoreo.services import TOLERANCIA_FRESCURA_MINUTOS

        umbral = TOLERANCIA_FRESCURA_MINUTOS['enlaces']
        ultima = (
            EstadoEnlaceFarmacia.objects.order_by('-ultima_verificacion')
            .values_list('ultima_verificacion', flat=True).first()
        )
        if ultima is None:
            # Sin ninguna farmacia con `ip_router` cargada no hay nada que sondear, y eso
            # es una instalación sin configurar, no un problema de salud.
            revisar('sondeo de enlaces', True, 'ninguna farmacia sondeada todavía')
            return
        minutos = (timezone.now() - ultima).total_seconds() / 60
        revisar(
            'sondeo de enlaces', minutos <= umbral,
            'último sondeo hace %.0f min (umbral %d min). Si está vencido, lo más probable '
            'es que el barrido esté abortando por falta de ruta: buscar "Barrido de enlaces '
            'abortado" en el log del worker.' % (minutos, umbral),
        )

    def _revisar_telegram(self, revisar):
        """Avisa de los chats del .env que le sobreviven a una baja.

        Desde el 7-oct-2026 un chat se autoriza de dos formas: por `PerfilUsuario` activo
        —el camino normal, desde el admin— o por `TELEGRAM_CHAT_IDS_AUTORIZADOS`, la lista
        del .env, pensada para chats que no son una persona (un grupo, un canal).

        La diferencia entre las dos es lo que esta revision vigila. El perfil se revoca
        solo: desactivar al usuario en Django le corta el Telegram en el mismo acto. **La
        lista del .env no.** Un chat que esta ahi sigue entrando aunque la persona ya no
        trabaje, y nadie se entera: dar de baja a alguien se siente completo y no lo esta.

        Por eso se revisa ESE sentido y no el inverso. Un perfil con chat_id y usuario
        inactivo no es un problema —el bot ya no le contesta, que es lo correcto— y un chat
        del .env sin perfil tampoco: es el caso legitimo de un grupo.
        """
        from django.conf import settings

        from apps.cuentas.models import PerfilUsuario

        autorizados = {str(c).strip() for c in getattr(settings, 'TELEGRAM_CHAT_IDS_AUTORIZADOS', [])}
        if not autorizados:
            revisar('telegram', True, 'sin chats fijos en el .env: todo se autoriza por perfil')
            return

        # Solo los del .env que SI son una persona y esa persona esta dada de baja.
        de_baja = list(
            PerfilUsuario.objects
            .filter(telegram_chat_id__in=autorizados, usuario__is_active=False)
            .select_related('usuario')
            .values_list('usuario__username', 'telegram_chat_id'),
        )
        if de_baja:
            revisar(
                'telegram', False,
                '%d chat(s) del .env pertenecen a usuarios DADOS DE BAJA y siguen '
                'entrando: %s. La baja en Django no alcanza para esos: hay que sacarlos de '
                'TELEGRAM_CHAT_IDS_AUTORIZADOS y recrear telegram_bot y celery_worker con '
                '`up -d` (un `restart` conserva las variables viejas).'
                % (len(de_baja), ', '.join('%s (%s)' % (u, c) for u, c in sorted(de_baja))),
            )
            return
        revisar(
            'telegram', True,
            '%d chat(s) fijo(s) en el .env, ninguno de un usuario dado de baja' % len(autorizados),
        )

    def _revisar_disco(self, revisar):
        import shutil

        from django.conf import settings

        try:
            uso = shutil.disk_usage(settings.MEDIA_ROOT)
        except OSError as exc:
            revisar('disco', False, 'no se pudo medir: %s' % exc)
            return
        libre_pct = uso.free / uso.total * 100
        revisar(
            'disco', libre_pct >= DISCO_LIBRE_MINIMO_PCT,
            '%.1f%% libre de %.1f GB (mínimo %d%%)' % (
                libre_pct, uso.total / 1024 ** 3, DISCO_LIBRE_MINIMO_PCT,
            ),
        )
