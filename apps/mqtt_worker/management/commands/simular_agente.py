"""Simula el comportamiento del agente real (`C:\\Proyectos\\saidsoft-agente`, .NET).

El agente real ya existe y está operativo; este simulador se mantiene como
herramienta de desarrollo para probar el flujo completo del backend sin instalarlo:
1. Se enrola contra el worker (si aún no está registrado).
2. Se suscribe a su(s) tópico(s) de despliegue.
3. Al recibir uno, reporta cada paso del ciclo (descarga, hash, aplica, ok/error)
   igual que lo haría el agente real.

Uso:
    python manage.py simular_agente ML001-A
    python manage.py simular_agente MAM01-C --forzar-error
"""
import hashlib
import json
import time
import urllib.request

import paho.mqtt.client as mqtt
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from apps.catalogo.models import Estacion


class Command(BaseCommand):
    help = 'Simula un agente real: se enrola, escucha despliegues y reporta su avance paso a paso.'

    def add_arguments(self, parser):
        parser.add_argument('codigo_estacion', help='Ej. ML001-A')
        parser.add_argument('--forzar-error', action='store_true', help='Simula un fallo al aplicar el despliegue.')
        parser.add_argument('--timeout', type=int, default=30, help='Segundos a esperar un despliegue.')

    def handle(self, *args, **options):
        codigo = options['codigo_estacion']
        forzar_error = options['forzar_error']
        timeout = options['timeout']

        try:
            estacion = Estacion.objects.get(codigo=codigo)
        except Estacion.DoesNotExist:
            raise CommandError(f'La estación {codigo} no existe. Cárgala primero (ver seed_demo).')

        if estacion.estado_aprobacion != Estacion.EstadoAprobacion.APROBADA:
            raise CommandError(f'{codigo} no está aprobada todavía.')

        token = estacion.token_enrolamiento
        conf = settings.MQTT_CONFIG
        recibido = {}

        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f'simulador-{codigo}')
        if conf['USERNAME']:
            client.username_pw_set(conf['USERNAME'], conf['PASSWORD'])
        # SEC-6 (auditoría 22-ago-2026): a diferencia de run_mqtt_worker.py y de todos los
        # publishers (apps.catalogo/despliegues/software.services), este simulador nunca
        # chequeaba USE_TLS y se conectaba siempre en plano — inofensivo contra un broker
        # de desarrollo sin TLS, pero si alguien lo apunta a producción (MQTT_HOST/PUERTO
        # de un .env real) se conectaría sin cifrar sin ningún aviso.
        if conf['USE_TLS']:
            client.tls_set(ca_certs=conf['CA_CERT'] or None)

        def on_connect(c, userdata, flags, reason_code, properties=None):
            self.stdout.write(f'[{codigo}] Conectado. Suscribiendo a tópicos de despliegue...')
            c.subscribe(f'/saidsof/agente/{codigo}/despliegue/')
            c.subscribe(f'/saidsof/despliegue/farmacia/{estacion.farmacia.codigo}/')
            c.subscribe(f'/saidsof/despliegue/grupo/{estacion.farmacia.grupo.codigo}/')
            c.subscribe('/saidsof/despliegue/global/')
            # heartbeat inicial, como haría el agente al arrancar
            self._publicar_heartbeat(c, codigo, token, estacion)

        def on_message(c, userdata, msg):
            recibido['payload'] = json.loads(msg.payload.decode('utf-8'))
            recibido['topic'] = msg.topic

        client.on_connect = on_connect
        client.on_message = on_message
        client.connect(conf['HOST'], conf['PORT'], keepalive=30)
        client.loop_start()

        self.stdout.write(f'[{codigo}] Esperando un despliegue hasta {timeout}s...')
        esperado = time.time() + timeout
        while time.time() < esperado and 'payload' not in recibido:
            time.sleep(0.5)

        if 'payload' not in recibido:
            client.loop_stop()
            self.stdout.write(self.style.WARNING(f'[{codigo}] No llegó ningún despliegue en {timeout}s.'))
            return

        payload = recibido['payload']
        self.stdout.write(self.style.SUCCESS(f'[{codigo}] Despliegue recibido en {recibido["topic"]}: {payload}'))
        self._ejecutar_ciclo(client, codigo, token, payload, forzar_error, version_previa=estacion.version_pos)
        client.loop_stop()
        client.disconnect()

    def _publicar_heartbeat(self, client, codigo, token, estacion):
        payload = {
            'token': token,
            'version_agente': estacion.version_agente or '1.0.0',
            'version_pos': estacion.version_pos,
            'so_nombre': estacion.so_nombre,
            'so_build': estacion.so_build,
            'numero_serie': estacion.numero_serie,
        }
        client.publish(f'/saidsof/agente/{codigo}/heartbeat/', json.dumps(payload))

    def _reportar(self, client, codigo, token, despliegue_id, paso, detalle='', **extra):
        payload = {'token': token, 'despliegue_id': despliegue_id, 'paso': paso, 'detalle': detalle, **extra}
        client.publish(f'/saidsof/agente/{codigo}/despliegue_estado/', json.dumps(payload))
        self.stdout.write(f'[{codigo}] -> {paso}' + (f' ({detalle})' if detalle else ''))
        time.sleep(0.3)  # da tiempo al worker a procesar antes del siguiente paso

    def _ejecutar_ciclo(self, client, codigo, token, payload, forzar_error, version_previa=''):
        despliegue_id = payload['despliegue_id']

        self._reportar(client, codigo, token, despliegue_id, 'recibido')

        try:
            with urllib.request.urlopen(payload['url'], timeout=15) as resp:
                contenido = resp.read()
        except Exception as exc:
            self._reportar(client, codigo, token, despliegue_id, 'error', detalle=f'No se pudo descargar: {exc}')
            return

        self._reportar(client, codigo, token, despliegue_id, 'descargado')

        sha256_calculado = hashlib.sha256(contenido).hexdigest()
        if sha256_calculado != payload['sha256']:
            self._reportar(client, codigo, token, despliegue_id, 'error', detalle='Hash no coincide, descarte del paquete')
            return
        self._reportar(client, codigo, token, despliegue_id, 'hash_verificado')

        self._reportar(client, codigo, token, despliegue_id, 'pos_cerrado', version_previa=version_previa)
        self._reportar(client, codigo, token, despliegue_id, 'aplicado')

        if forzar_error:
            self._reportar(client, codigo, token, despliegue_id, 'error', detalle='Fallo simulado al relanzar el POS')
            self._reportar(client, codigo, token, despliegue_id, 'rollback', detalle='Restaurada versión previa')
            return

        self._reportar(client, codigo, token, despliegue_id, 'pos_relanzado')
        self._reportar(client, codigo, token, despliegue_id, 'ok', version_nueva=payload['version'])
        self.stdout.write(self.style.SUCCESS(f'[{codigo}] Despliegue {payload["version"]} aplicado correctamente.'))
