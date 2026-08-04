"""Agente de prueba SAIDSOFT — implementación de referencia liviana del protocolo MQTT
que habla el agente real (saidsoft-agente, C#, repo aparte que no existe en este entorno).

Pensado para copiarse a una estación Windows real (como .exe standalone, ver build.ps1)
y validar el flujo servidor↔agente sin depender del simulador Django, que solo corre en
el propio servidor.

Cubre: enrolamiento, heartbeat, ejecución de scripts (RMM) y solicitudes de instalación
de software del catálogo. NO cubre despliegues de POS (fuera del alcance pedido) ni
sirve de caché de farmacia (es_cache_farmacia) — un solo agente de prueba no necesita
servir paquetes a nadie más.

Uso:
    agente_prueba.exe --codigo ML001-B --host 127.0.0.1 --puerto 1883 --hmac-secret <secreto>

Guarda su identidad (hardware_id fijado la primera vez, token recibido en el
enrolamiento, farmacia/grupo) en identidad.json junto al ejecutable — no vuelve a
enrolarse en cada arranque si ya tiene un token guardado, igual que se documenta que
hace el agente real.
"""
import argparse
import hashlib
import hmac
import json
import logging
import os
import platform
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request

import paho.mqtt.client as mqtt

ARCHIVO_IDENTIDAD = 'identidad.json'
ARCHIVO_LOG = 'agente_prueba.log'
VERSION_AGENTE_PRUEBA = 'agente-prueba-0.1'


def _configurar_logging():
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s [%(levelname)s] %(message)s',
        handlers=[logging.StreamHandler(sys.stdout), logging.FileHandler(ARCHIVO_LOG, encoding='utf-8')],
    )


def leer_machine_guid() -> str:
    """MachineGuid de Windows — mismo identificador estable que usa el agente real
    para hardware_id (ver README.md del proyecto principal, sección de enrolamiento)."""
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r'SOFTWARE\Microsoft\Cryptography') as clave:
            return winreg.QueryValueEx(clave, 'MachineGuid')[0]
    except Exception:
        logging.warning('No se pudo leer MachineGuid del registro, uso un valor fijo de prueba.')
        return 'SIN-MACHINEGUID-DE-PRUEBA'


def leer_numero_serie() -> str:
    try:
        salida = subprocess.check_output(
            ['powershell', '-NoProfile', '-Command', '(Get-CimInstance Win32_BIOS).SerialNumber'],
            timeout=10, text=True,
        )
        return salida.strip() or 'SN-DESCONOCIDO'
    except Exception:
        return 'SN-DESCONOCIDO'


def firmar(secreto: str, **campos) -> str:
    """Debe coincidir exactamente con apps.catalogo.services.firmar_payload del
    servidor: HMAC-SHA256 sobre los valores unidos con "|", en el orden en que se
    pasan los kwargs (Python 3.7+ preserva ese orden)."""
    mensaje = '|'.join(str(v) for v in campos.values())
    return hmac.new(secreto.encode(), mensaje.encode(), hashlib.sha256).hexdigest()


class AgentePrueba:
    def __init__(self, args):
        self.args = args
        self.identidad = self._cargar_identidad()
        self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f'agente-prueba-{args.codigo}')
        if args.usuario:
            self.client.username_pw_set(args.usuario, args.password or '')
        if args.tls:
            self.client.tls_set(ca_certs=args.ca_cert or None)
        self.client.on_connect = self._on_connect
        self.client.on_message = self._on_message
        self.client.on_disconnect = self._on_disconnect

    # --- identidad persistida ---
    def _cargar_identidad(self) -> dict:
        if os.path.exists(ARCHIVO_IDENTIDAD):
            with open(ARCHIVO_IDENTIDAD, encoding='utf-8') as f:
                return json.load(f)
        return {}

    def _guardar_identidad(self):
        with open(ARCHIVO_IDENTIDAD, 'w', encoding='utf-8') as f:
            json.dump(self.identidad, f, indent=2)

    def _token(self) -> str:
        return self.identidad.get('token', '')

    # --- ciclo de conexión ---
    def _on_connect(self, client, userdata, flags, reason_code, properties=None):
        if reason_code != 0:
            logging.error('Falló la conexión al broker: %s', reason_code)
            return
        logging.info('Conectado al broker %s:%s', self.args.host, self.args.puerto)
        client.subscribe(f'/saidsof/enrolamiento/respuesta/{self.args.codigo}/')
        client.subscribe(f'/saidsof/agente/{self.args.codigo}/comando/')
        client.subscribe(f'/saidsof/agente/{self.args.codigo}/software/')
        client.subscribe('/saidsof/software/global/')
        if self.identidad.get('farmacia'):
            client.subscribe(f"/saidsof/software/farmacia/{self.identidad['farmacia']}/")
        if self.identidad.get('grupo'):
            client.subscribe(f"/saidsof/software/grupo/{self.identidad['grupo']}/")

        if self._token():
            logging.info('Ya tengo identidad guardada (token existente) — no vuelvo a enrolarme.')
        else:
            self._enrolar()

    def _on_disconnect(self, client, userdata, flags, reason_code, properties=None):
        logging.warning('Desconectado del broker (%s), paho reintentará solo.', reason_code)

    def _enrolar(self):
        numero_serie = leer_numero_serie()
        self.identidad['numero_serie'] = numero_serie
        payload = {
            'codigo': self.args.codigo,
            'hardware_id': self.identidad.get('hardware_id') or leer_machine_guid(),
            'hostname': socket.gethostname(),
            'numero_serie': numero_serie,
            'so_nombre': f'Windows {platform.win32_ver()[0]}',
            'so_build': platform.win32_ver()[1],
            'version_agente': VERSION_AGENTE_PRUEBA,
        }
        self.identidad['hardware_id'] = payload['hardware_id']
        self._guardar_identidad()
        self.client.publish('/saidsof/enrolamiento/solicitar/', json.dumps(payload))
        logging.info('Enrolamiento solicitado: %s', payload)

    def _on_message(self, client, userdata, msg):
        try:
            payload = json.loads(msg.payload.decode('utf-8'))
        except (UnicodeDecodeError, json.JSONDecodeError):
            logging.warning('Mensaje no-JSON en %s, descartado', msg.topic)
            return
        try:
            if msg.topic == f'/saidsof/enrolamiento/respuesta/{self.args.codigo}/':
                self._manejar_respuesta_enrolamiento(payload)
            elif msg.topic == f'/saidsof/agente/{self.args.codigo}/comando/':
                self._manejar_comando(payload)
            elif msg.topic.startswith('/saidsof/software/') or msg.topic.endswith('/software/'):
                self._manejar_software(payload)
            else:
                logging.debug('Mensaje en tópico no manejado: %s', msg.topic)
        except Exception:
            logging.exception('Error procesando mensaje de %s', msg.topic)

    def _manejar_respuesta_enrolamiento(self, payload):
        if not payload.get('aceptado'):
            logging.error('Enrolamiento rechazado: %s', payload.get('motivo'))
            return
        self.identidad['token'] = payload['token']
        self.identidad['farmacia'] = payload.get('farmacia')
        self.identidad['grupo'] = payload.get('grupo')
        self._guardar_identidad()
        logging.info(
            'Enrolado. estado_aprobacion=%s farmacia=%s grupo=%s '
            '(si dice "pendiente", hay que aprobar la estación desde el panel: /estaciones/)',
            payload.get('estado_aprobacion'), payload.get('farmacia'), payload.get('grupo'),
        )
        if payload.get('farmacia'):
            self.client.subscribe(f"/saidsof/software/farmacia/{payload['farmacia']}/")
        if payload.get('grupo'):
            self.client.subscribe(f"/saidsof/software/grupo/{payload['grupo']}/")

    def _publicar(self, topico, payload):
        self.client.publish(topico, json.dumps(payload))

    # --- heartbeat ---
    def bucle_heartbeat(self):
        while True:
            time.sleep(self.args.intervalo_heartbeat)
            if not self._token():
                continue
            self._publicar(f'/saidsof/agente/{self.args.codigo}/heartbeat/', {
                'token': self._token(),
                'version_agente': VERSION_AGENTE_PRUEBA,
                'version_pos': 'N/A (agente de prueba)',
                'so_nombre': f'Windows {platform.win32_ver()[0]}',
                'so_build': platform.win32_ver()[1],
                'hostname': socket.gethostname(),
                'numero_serie': self.identidad.get('numero_serie', ''),
            })
            logging.info('Heartbeat enviado')

    # --- scripts (RMM) ---
    def _manejar_comando(self, payload):
        comando = payload.get('comando')
        if comando == 'ejecutar_script':
            self._verificar_y_ejecutar_script(payload)
        else:
            logging.info('Comando "%s" recibido — no implementado en este agente de prueba.', comando)

    def _verificar_y_ejecutar_script(self, payload):
        firma_esperada = firmar(
            self.args.hmac_secret,
            comando='ejecutar_script', ejecucion_id=payload['ejecucion_id'], resultado_id=payload['resultado_id'],
            tipo_script=payload['tipo_script'], timeout_segundos=payload['timeout_segundos'],
            contenido=payload['contenido'],
        )
        if not hmac.compare_digest(firma_esperada, payload.get('firma', '')):
            logging.error('Firma HMAC inválida en comando ejecutar_script — se ignora (posible suplantación).')
            return

        resultado_id = payload['resultado_id']
        self._reportar_script(resultado_id, 'ejecutando')
        ruta_script = None
        try:
            extension = '.ps1' if payload['tipo_script'] == 'powershell' else '.bat'
            with tempfile.NamedTemporaryFile('w', suffix=extension, delete=False, encoding='utf-8') as f:
                f.write(payload['contenido'])
                ruta_script = f.name
            comando_exec = (
                ['powershell', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', ruta_script]
                if payload['tipo_script'] == 'powershell' else [ruta_script]
            )
            resultado = subprocess.run(
                comando_exec, capture_output=True, text=True, timeout=payload['timeout_segundos'],
            )
            estado = 'completado' if resultado.returncode == 0 else 'error'
            self._reportar_script(
                resultado_id, estado, exit_code=resultado.returncode,
                stdout=resultado.stdout[-4000:], stderr=resultado.stderr[-4000:],
            )
        except subprocess.TimeoutExpired:
            self._reportar_script(resultado_id, 'timeout')
        except Exception as exc:
            self._reportar_script(resultado_id, 'error', stderr=str(exc))
        finally:
            if ruta_script:
                try:
                    os.unlink(ruta_script)
                except OSError:
                    pass

    def _reportar_script(self, resultado_id, estado, exit_code=None, stdout='', stderr=''):
        self._publicar(f'/saidsof/agente/{self.args.codigo}/script_estado/', {
            'token': self._token(), 'resultado_id': resultado_id, 'estado': estado,
            'exit_code': exit_code, 'stdout': stdout, 'stderr': stderr,
        })
        logging.info('Script #%s -> %s', resultado_id, estado)

    # --- software (catálogo) ---
    def _manejar_software(self, payload):
        solicitud_id = payload['solicitud_id']
        accion = payload.get('accion', 'instalar')

        if accion == 'desinstalar':
            self._reportar_instalacion(solicitud_id, 'instalando')
            self._correr_comando_software(solicitud_id, payload['comando_desinstalacion'], payload)
            return

        self._reportar_instalacion(solicitud_id, 'recibido')
        ruta = None
        try:
            ruta = self._descargar(payload['url'])
            self._reportar_instalacion(solicitud_id, 'descargado')

            hash_local = self._sha256_de(ruta)
            if hash_local != payload['sha256']:
                self._reportar_instalacion(
                    solicitud_id, 'error',
                    detalle=f"SHA-256 no coincide (esperado {payload['sha256']}, obtenido {hash_local})",
                )
                return
            self._reportar_instalacion(solicitud_id, 'hash_verificado')
            self._reportar_instalacion(solicitud_id, 'instalando')

            comando = payload['comando_instalacion_silenciosa'].replace('{archivo}', ruta)
            self._correr_comando_software(solicitud_id, comando, payload)
        except Exception as exc:
            self._reportar_instalacion(solicitud_id, 'error', detalle=str(exc))
        finally:
            if ruta:
                try:
                    os.unlink(ruta)
                except OSError:
                    pass

    def _correr_comando_software(self, solicitud_id, comando, payload):
        if payload.get('argumentos_adicionales'):
            comando = f"{comando} {payload['argumentos_adicionales']}"
        try:
            resultado = subprocess.run(comando, shell=True, capture_output=True, text=True, timeout=600)
        except subprocess.TimeoutExpired:
            self._reportar_instalacion(solicitud_id, 'error', detalle='Timeout (600s) ejecutando el instalador.')
            return
        if resultado.returncode == 0:
            self._reportar_instalacion(solicitud_id, 'instalado', version_instalada=payload.get('version', ''))
        else:
            self._reportar_instalacion(
                solicitud_id, 'error',
                detalle=f'El comando devolvió código {resultado.returncode}: {resultado.stderr[-2000:]}',
            )

    def _descargar(self, url: str) -> str:
        ruta = os.path.join(tempfile.gettempdir(), os.path.basename(url) or 'paquete.bin')
        urllib.request.urlretrieve(url, ruta)
        return ruta

    def _sha256_de(self, ruta: str) -> str:
        hasher = hashlib.sha256()
        with open(ruta, 'rb') as f:
            for bloque in iter(lambda: f.read(65536), b''):
                hasher.update(bloque)
        return hasher.hexdigest()

    def _reportar_instalacion(self, solicitud_id, paso, detalle='', version_instalada=''):
        self._publicar(f'/saidsof/agente/{self.args.codigo}/software_estado/', {
            'token': self._token(), 'solicitud_id': solicitud_id, 'paso': paso,
            'detalle': detalle, 'version_instalada': version_instalada,
        })
        logging.info('Instalación #%s -> %s%s', solicitud_id, paso, f' ({detalle})' if detalle else '')

    # --- arranque ---
    def correr(self):
        logging.info('Agente de prueba %s arrancando para la estación %s', VERSION_AGENTE_PRUEBA, self.args.codigo)
        self.client.connect(self.args.host, self.args.puerto, keepalive=60)
        threading.Thread(target=self.bucle_heartbeat, daemon=True).start()
        self.client.loop_forever()


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--codigo', required=True, help='Código de la estación, ej. ML001-B')
    parser.add_argument('--host', default='127.0.0.1', help='Host del broker MQTT (default 127.0.0.1)')
    parser.add_argument('--puerto', type=int, default=1883, help='Puerto del broker (default 1883)')
    parser.add_argument('--usuario', default='', help='Usuario MQTT (vacío = sin autenticación, como en dev)')
    parser.add_argument('--password', default='', help='Password MQTT')
    parser.add_argument('--tls', action='store_true', help='Usar MQTT sobre TLS (como en producción)')
    parser.add_argument('--ca-cert', default='', help='Ruta al CA cert para validar el broker si --tls')
    parser.add_argument(
        '--hmac-secret', default='',
        help='COMANDO_HMAC_SECRET del servidor — obligatorio para validar comandos de scripts (ejecutar_script).',
    )
    parser.add_argument('--intervalo-heartbeat', type=int, default=60, help='Segundos entre heartbeats (default 60)')
    args = parser.parse_args()

    _configurar_logging()
    AgentePrueba(args).correr()


if __name__ == '__main__':
    main()
