r"""Agente SAIDSOFT — reemplazo en Python del agente real perdido (saidsoft-agente, C#,
repo aparte cuyo código fuente no se pudo recuperar; ver PLAN_MODERNIZACION.md §10-K).

Empezó como "agente de prueba" (implementación de referencia liviana del protocolo
MQTT) pensado para copiarse a una estación Windows real (como .exe standalone, ver
build.ps1) y validar el flujo servidor↔agente sin depender del simulador Django. El
10-ago-2026, al no poder ubicarse la máquina de build del agente C# original para
corregirle un bug (comparación de SHA-256 sensible a mayúsculas/minúsculas que hacía
fallar todo despliegue de POS), se decidió promoverlo a agente de producción del
piloto, agregándole lo que le faltaba: despliegues de POS y la posibilidad de correr
como servicio de Windows (ver servicio_windows.py).

Cubre: enrolamiento, heartbeat, ejecución de scripts (RMM), instalación de software del
catálogo y despliegues de POS (descargar/verificar/aplicar/rollback, los tres modos de
aplicación). NO sirve de caché de farmacia (es_cache_farmacia) — no expone un servidor
HTTP local para que otras estaciones descarguen de él; si `usar_cache` viene activo en
un despliegue, sí intenta descargar primero del caché de su propia farmacia (best
effort, cae al central si falla).

Uso (modo consola, para pruebas):
    agente_prueba.exe --codigo ML001-B --host 127.0.0.1 --puerto 1883 --hmac-secret <secreto> \
        --pos-carpeta-instalacion "C:\Program Files (x86)\Farmamia Cia Ltda - Elipsys\Cliente" \
        --pos-nombre-proceso Zabyca.Pos.Desktop \
        --pos-comando-iniciar "C:\Program Files (x86)\Farmamia Cia Ltda - Elipsys\Cliente\Zabyca.Pos.Desktop.exe"

Para producción, ver servicio_windows.py e instalar-servicio.ps1 (corre como servicio
de Windows con auto-reinicio, en vez de consola manual).

Guarda su identidad (hardware_id fijado la primera vez, token recibido en el
enrolamiento, farmacia/grupo, cache_url_base) en identidad.json junto al ejecutable —
no vuelve a enrolarse en cada arranque si ya tiene un token guardado, igual que se
documenta que hace el agente real.
"""
import argparse
import hashlib
import hmac
import json
import logging
import os
import platform
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.request
import zipfile
from datetime import datetime

import paho.mqtt.client as mqtt

ARCHIVO_IDENTIDAD = 'identidad.json'
ARCHIVO_LOG = 'agente_prueba.log'
VERSION_AGENTE_PRUEBA = 'agente-prueba-0.1'


def _configurar_logging_archivo(ruta_log: str = ARCHIVO_LOG):
    """Solo archivo, sin StreamHandler(stdout) — usado por servicio_windows.py, que no
    tiene consola a la que escribir (un servicio de Windows lanzado por el SCM no
    hereda una)."""
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s [%(levelname)s] %(message)s',
        handlers=[logging.FileHandler(ruta_log, encoding='utf-8')],
    )


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
        # Si esta estación ya tiene una credencial MQTT propia guardada de un
        # enrolamiento anterior (ver _manejar_respuesta_enrolamiento), se usa esa en vez
        # de la compartida de args/config.json — nunca vuelve a tocar la compartida una
        # vez migrada.
        usuario = self.identidad.get('mqtt_username') or args.usuario
        password = self.identidad.get('mqtt_password') if self.identidad.get('mqtt_username') else args.password
        if usuario:
            self.client.username_pw_set(usuario, password or '')
        if args.tls:
            self.client.tls_set(ca_certs=args.ca_cert or None)
        self.client.on_connect = self._on_connect
        self.client.on_message = self._on_message
        self.client.on_disconnect = self._on_disconnect
        # (bytes_recibidos, bytes_enviados, momento) de la última muestra de red — en
        # memoria de proceso, no en identidad.json: el agente corre como servicio de
        # larga duración, así que persiste entre ciclos de bucle_metricas sin
        # necesidad de guardarlo en disco (a diferencia del sondeo SNMP de Mikrotik
        # del lado servidor, que si necesita persistir en BD por si el proceso
        # Celery reinicia entre corridas). None hasta la primera muestra.
        self._ultima_muestra_red = None

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
        client.subscribe(f'/saidsof/agente/{self.args.codigo}/despliegue/')
        client.subscribe('/saidsof/despliegue/global/')
        if self.identidad.get('farmacia'):
            client.subscribe(f"/saidsof/software/farmacia/{self.identidad['farmacia']}/")
            client.subscribe(f"/saidsof/despliegue/farmacia/{self.identidad['farmacia']}/")
        if self.identidad.get('grupo'):
            client.subscribe(f"/saidsof/software/grupo/{self.identidad['grupo']}/")
            client.subscribe(f"/saidsof/despliegue/grupo/{self.identidad['grupo']}/")

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
            elif msg.topic.startswith('/saidsof/despliegue/') or msg.topic.endswith('/despliegue/'):
                self._manejar_despliegue(payload)
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
        self.identidad['cache_url_base'] = payload.get('cache_url_base')
        # Viaja en la respuesta de enrolamiento (ver apps.mqtt_worker.services, línea
        # que arma la respuesta) — controla el volumen de métricas: solo las estaciones
        # marcadas (típicamente servidores/matriz) las reportan, igual que en el sistema
        # viejo. Si cambia desde el admin, no se aplica hasta el próximo enrolamiento
        # (no hay un mecanismo de "config push" separado todavía).
        self.identidad['monitorear_recursos'] = bool(payload.get('monitorear_recursos'))

        # Credencial MQTT propia de la estación (aislamiento a nivel de broker — ver
        # apps.mqtt_worker.emqx_admin del lado servidor). None si el servidor no tiene
        # EMQX_ADMIN_CONFIG configurado o falló: en ese caso se sigue usando la
        # compartida, igual que antes de que existiera este mecanismo.
        mqtt_username = payload.get('mqtt_username')
        mqtt_password = payload.get('mqtt_password')
        credencial_nueva = bool(mqtt_username) and mqtt_username != self.identidad.get('mqtt_username')
        if mqtt_username:
            self.identidad['mqtt_username'] = mqtt_username
            self.identidad['mqtt_password'] = mqtt_password

        self._guardar_identidad()
        logging.info(
            'Enrolado. estado_aprobacion=%s farmacia=%s grupo=%s '
            '(si dice "pendiente", hay que aprobar la estación desde el panel: /estaciones/)',
            payload.get('estado_aprobacion'), payload.get('farmacia'), payload.get('grupo'),
        )
        if payload.get('farmacia'):
            self.client.subscribe(f"/saidsof/software/farmacia/{payload['farmacia']}/")
            self.client.subscribe(f"/saidsof/despliegue/farmacia/{payload['farmacia']}/")
        if payload.get('grupo'):
            self.client.subscribe(f"/saidsof/software/grupo/{payload['grupo']}/")
            self.client.subscribe(f"/saidsof/despliegue/grupo/{payload['grupo']}/")

        if credencial_nueva:
            logging.info('Credencial MQTT propia recibida, reconectando con ella...')
            self.client.username_pw_set(mqtt_username, mqtt_password or '')
            # reconnect() en un hilo aparte: llamarlo directo desde este callback (que
            # corre en el hilo del loop de red de paho) puede pisarse con la propia
            # conexión que se está cerrando/reabriendo — mismo motivo por el que
            # _manejar_despliegue despacha a un hilo en vez de bloquear el loop de MQTT.
            threading.Thread(target=self.client.reconnect, daemon=True).start()

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

    # --- métricas periódicas (CPU/RAM/disco) ---
    def bucle_metricas(self):
        """Calco de bucle_heartbeat, con su propio intervalo. Solo reporta si esta
        estación está marcada `monitorear_recursos=True` (viaja en la respuesta de
        enrolamiento) — mismo criterio de volumen que ya usaba el sistema viejo (solo
        servidores/matriz), aplicado ahora también a las cajas si se decide activarlo."""
        while True:
            time.sleep(self.args.intervalo_metricas)
            if not self._token() or not self.identidad.get('monitorear_recursos'):
                continue
            recursos = self._medir_recursos()
            if not recursos:
                continue
            self._tasa_red_kbps(recursos)
            self._publicar(f'/saidsof/agente/{self.args.codigo}/metricas/', {
                'token': self._token(), **recursos,
            })
            logging.info('Métricas enviadas: %s', recursos)

    def _medir_recursos(self) -> dict:
        """CPU/RAM/disco/red vía CIM, mismo estilo de un solo script PowerShell que
        _consultar_info_equipo (evita varias llamadas sueltas). No mide latencia
        (`latencia_ms` queda ausente del payload — el servidor lo trata como no
        medido, igual que temperatura_c) ni temperatura, por las mismas razones que ya
        documenta _consultar_info_equipo para BitLocker: sin sensor confiable
        disponible de forma genérica.

        Red: contadores ACUMULADOS (bytes desde que arrancó el adaptador), no una
        tasa — bucle_metricas los convierte a kbps comparando contra la muestra
        anterior (ver _tasa_red_kbps). Se toma el adaptador de la ruta por defecto
        (Get-NetRoute a 0.0.0.0/0 con menor métrica), no se suman todas las
        interfaces — evita contar adaptadores virtuales/deshabilitados. Sin
        validar todavía contra una estación real con múltiples adaptadores."""
        script = r"""
$ErrorActionPreference = 'SilentlyContinue'
$cpu = (Get-CimInstance Win32_Processor | Measure-Object -Property LoadPercentage -Average).Average
$os = Get-CimInstance Win32_OperatingSystem
$ramTotalMb = if ($os.TotalVisibleMemorySize) { [math]::Round($os.TotalVisibleMemorySize / 1KB) } else { $null }
$ramLibreMb = if ($os.FreePhysicalMemory) { [math]::Round($os.FreePhysicalMemory / 1KB) } else { $null }
$ramUsadaMb = if ($ramTotalMb -and $ramLibreMb) { $ramTotalMb - $ramLibreMb } else { $null }
$disco = Get-CimInstance Win32_LogicalDisk -Filter "DeviceID='C:'"
$discoTotalGb = if ($disco) { [math]::Round($disco.Size / 1GB, 1) } else { $null }
$discoLibreGb = if ($disco) { [math]::Round($disco.FreeSpace / 1GB, 1) } else { $null }
$ruta = Get-NetRoute -DestinationPrefix '0.0.0.0/0' -ErrorAction SilentlyContinue | Sort-Object -Property RouteMetric | Select-Object -First 1
$redStats = if ($ruta) { Get-NetAdapterStatistics -InterfaceIndex $ruta.InterfaceIndex -ErrorAction SilentlyContinue } else { $null }
$redRecibidoBytes = if ($redStats) { $redStats.ReceivedBytes } else { $null }
$redEnviadoBytes = if ($redStats) { $redStats.SentBytes } else { $null }
[PSCustomObject]@{
    cpu_carga_pct = $cpu
    ram_total = $ramTotalMb
    ram_usada = $ramUsadaMb
    ram_libre = $ramLibreMb
    disco_total_gb = $discoTotalGb
    disco_libre_gb = $discoLibreGb
    red_recibido_bytes = $redRecibidoBytes
    red_enviado_bytes = $redEnviadoBytes
} | ConvertTo-Json -Compress
"""
        try:
            salida = subprocess.check_output(
                ['powershell', '-NoProfile', '-Command', script], timeout=20, text=True,
            )
            return json.loads(salida)
        except Exception:
            logging.exception('No se pudo medir CPU/RAM/disco')
            return {}

    def _tasa_red_kbps(self, recursos: dict) -> None:
        """Convierte los contadores acumulados de red de `recursos` (ver
        _medir_recursos) en una tasa kbps, comparando contra la muestra anterior
        guardada en memoria de proceso. Modifica `recursos` in-place: saca los bytes
        crudos (no viajan al servidor, solo la tasa) y agrega red_recibido_kbps/
        red_enviado_kbps si hay con qué calcularla — la primera muestra tras
        arrancar el servicio, o un contador que bajó (reinicio del adaptador/equipo),
        no calculan nada esta vez, se retoma normal en el próximo ciclo."""
        recibido = recursos.pop('red_recibido_bytes', None)
        enviado = recursos.pop('red_enviado_bytes', None)
        ahora = time.monotonic()
        anterior = self._ultima_muestra_red
        self._ultima_muestra_red = (
            (recibido, enviado, ahora) if recibido is not None and enviado is not None else None
        )
        if anterior is None or recibido is None or enviado is None:
            return
        recibido_prev, enviado_prev, momento_prev = anterior
        elapsed = ahora - momento_prev
        if elapsed <= 0 or recibido < recibido_prev or enviado < enviado_prev:
            return
        recursos['red_recibido_kbps'] = round((recibido - recibido_prev) * 8 / 1000 / elapsed, 1)
        recursos['red_enviado_kbps'] = round((enviado - enviado_prev) * 8 / 1000 / elapsed, 1)

    # --- log del POS (errores, "reportar a tiempo") ---
    # Cada línea de entrada real de log4net matchea este patrón (ver log4net.config del
    # POS: PatternLayout "%date [%thread] %-5level %logger - %message%newline"); las
    # líneas de un stack trace no lo matchean y se descartan sin reenviarlas al
    # servidor (evita payloads gigantes — si hace falta más detalle, queda en el
    # archivo local).
    _RE_LINEA_LOG_POS = re.compile(
        r'^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3} \[\d+\]\s+(?P<nivel>\w+)\s+\S+ - (?P<mensaje>.*)$',
    )
    _NIVELES_A_REPORTAR = {'ERROR', 'FATAL'}

    def bucle_log_pos(self):
        """Calco de bucle_metricas, con su propio intervalo. Solo corre si el agente
        tiene un POS configurado (--pos-carpeta-instalacion) — sin eso no hay log que
        leer. No depende de monitorear_recursos: la salud del POS importa para
        cualquier estación con POS, no es un concepto de "servidor"."""
        while True:
            time.sleep(self.args.intervalo_log_pos)
            if not self._token() or not self.args.pos_carpeta_instalacion:
                continue
            try:
                errores = self._leer_errores_nuevos_pos()
            except Exception:
                logging.exception('No se pudo leer el log del POS')
                continue
            if errores is None:
                continue  # no se pudo leer (archivo ausente todavía, etc.) — no reportar nada
            self._publicar(f'/saidsof/agente/{self.args.codigo}/pos_errores/', {
                'token': self._token(), 'errores': errores,
            })
            if errores:
                logging.info('Errores del POS reportados: %d tipo(s) distinto(s)', len(errores))

    def _ruta_log_pos(self) -> str:
        return os.path.join(self.args.pos_carpeta_instalacion, self.args.pos_log_relativo)

    def _leer_errores_nuevos_pos(self):
        """Lee desde la última posición guardada (identidad['pos_log_posicion']),
        agrupa por mensaje exacto los niveles ERROR/FATAL, y devuelve
        [{mensaje, nivel, cantidad}, ...]. None si el archivo no existe todavía (POS
        recién instalado, o nunca generó el log) — distinto de [] (se leyó, sin
        errores nuevos), para no pisar en falso la posición guardada."""
        ruta = self._ruta_log_pos()
        if not os.path.exists(ruta):
            return None

        tamanio_actual = os.path.getsize(ruta)
        posicion = self.identidad.get('pos_log_posicion', 0)
        if tamanio_actual < posicion:
            # El POS truncó el archivo (appendToFile=false en su log4net.config, se
            # reinicia en cada arranque) o log4net lo rotó por fecha — se relee desde
            # el principio en vez de perder lo nuevo asumiendo una posición inválida.
            posicion = 0

        conteos = {}  # mensaje -> {'nivel': ..., 'cantidad': int}
        with open(ruta, 'r', encoding='utf-8', errors='replace') as f:
            f.seek(posicion)
            entrada_actual = None
            for linea in f:
                coincidencia = self._RE_LINEA_LOG_POS.match(linea)
                if coincidencia:
                    entrada_actual = coincidencia
                    nivel = coincidencia.group('nivel').upper()
                    if nivel in self._NIVELES_A_REPORTAR:
                        mensaje = coincidencia.group('mensaje').strip()
                        item = conteos.setdefault(mensaje, {'nivel': nivel, 'cantidad': 0})
                        item['cantidad'] += 1
                # Líneas que no matchean (continuación de stack trace) se ignoran —
                # ya se contó la entrada por su primera línea.
            self.identidad['pos_log_posicion'] = f.tell()
        self._guardar_identidad()

        return [{'mensaje': m, 'nivel': d['nivel'], 'cantidad': d['cantidad']} for m, d in conteos.items()]

    # --- scripts (RMM) ---
    def _manejar_comando(self, payload):
        comando = payload.get('comando')
        if comando == 'ejecutar_script':
            self._verificar_y_ejecutar_script(payload)
        elif comando == 'consultar_info':
            self._verificar_y_consultar_info(payload)
        elif comando == 'reiniciar':
            self._verificar_y_reiniciar(payload)
        elif comando == 'escanear_actualizaciones':
            self._verificar_y_escanear_actualizaciones(payload)
        elif comando == 'consultar_software_instalado':
            self._verificar_y_consultar_software_instalado(payload)
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

    # --- consultar_info (hardware/BitLocker bajo demanda) ---
    def _verificar_y_consultar_info(self, payload):
        # Mismo esquema de firma que ejecutar_script — aunque este comando solo lee
        # info (no ejecuta nada), igual conviene no responder a un "consultar_info"
        # forjado por cualquiera que pueda publicar en el tópico.
        firma_esperada = firmar(self.args.hmac_secret, comando='consultar_info')
        if not hmac.compare_digest(firma_esperada, payload.get('firma', '')):
            logging.error('Firma HMAC inválida en comando consultar_info — se ignora (posible suplantación).')
            return

        info = self._consultar_info_equipo()
        self._publicar(f'/saidsof/agente/{self.args.codigo}/info_equipo/', {
            'token': self._token(),
            'hostname': socket.gethostname(),
            'numero_serie': leer_numero_serie(),
            'so_nombre': f'Windows {platform.win32_ver()[0]}',
            'so_build': platform.win32_ver()[1],
            **info,
        })
        logging.info('Info del equipo reportada.')

    def _consultar_info_equipo(self) -> dict:
        """Procesador/RAM/almacenamiento vía CIM, BitLocker del volumen C: y plan de
        energía activo — un solo script de PowerShell que arma todo en JSON, en vez de
        varias llamadas sueltas parseando texto (más frágil). BitLocker puede no estar
        disponible (Windows Home, o el cmdlet ausente) — con -ErrorAction Stop +
        try/catch, esa sección simplemente queda vacía en vez de tirar abajo el resto
        de la consulta. El plan de energía es solo lectura (v1, ver
        PLAN_MODERNIZACION.md §9) — no se aplica/fuerza nada desde acá."""
        script = r"""
$ErrorActionPreference = 'SilentlyContinue'
$proc = (Get-CimInstance Win32_Processor | Select-Object -First 1).Name
$ramBytes = (Get-CimInstance Win32_ComputerSystem).TotalPhysicalMemory
$disco = Get-CimInstance Win32_LogicalDisk -Filter "DeviceID='C:'"
$discoGB = if ($disco) { [math]::Round($disco.Size / 1GB) } else { $null }
$planEnergia = (Get-CimInstance -Namespace root\cimv2\power -ClassName Win32_PowerPlan | Where-Object IsActive).ElementName

$habilitado = $null
$metodo = ''
$claveRecuperacion = ''
$idProtector = ''
try {
    $bitlocker = Get-BitLockerVolume -MountPoint C: -ErrorAction Stop
    $habilitado = ($bitlocker.ProtectionStatus -eq 'On')
    $protectores = $bitlocker.KeyProtector
    $noRecovery = $protectores | Where-Object { $_.KeyProtectorType -ne 'RecoveryPassword' } | Select-Object -First 1
    if ($noRecovery) { $metodo = $noRecovery.KeyProtectorType.ToLower() }
    $recovery = $protectores | Where-Object { $_.KeyProtectorType -eq 'RecoveryPassword' } | Select-Object -First 1
    if ($recovery) {
        $claveRecuperacion = $recovery.RecoveryPassword
        $idProtector = $recovery.KeyProtectorId
    }
} catch {}

[PSCustomObject]@{
    procesador = $proc
    ram_total_mb = if ($ramBytes) { [math]::Round($ramBytes / 1MB) } else { $null }
    almacenamiento_total_gb = $discoGB
    bitlocker_habilitado = $habilitado
    bitlocker_metodo_proteccion = $metodo
    bitlocker_clave_recuperacion = $claveRecuperacion
    bitlocker_id_protector = $idProtector
    power_plan = $planEnergia
} | ConvertTo-Json -Compress
"""
        try:
            salida = subprocess.check_output(
                ['powershell', '-NoProfile', '-Command', script], timeout=20, text=True,
            )
            return json.loads(salida)
        except Exception:
            logging.exception('No se pudo consultar la info del equipo (CIM/BitLocker)')
            return {}

    # --- reiniciar (equipo completo, no solo el servicio) ---
    def _verificar_y_reiniciar(self, payload):
        # Mismo esquema de firma que consultar_info/ejecutar_script. Este comando
        # reinicia el equipo Windows completo (no el servicio del agente) — el botón
        # del panel avisa "interrumpe cualquier venta en curso en esa caja", y es
        # fire-and-forget: el servidor no espera ninguna confirmación de vuelta.
        firma_esperada = firmar(self.args.hmac_secret, comando='reiniciar')
        if not hmac.compare_digest(firma_esperada, payload.get('firma', '')):
            logging.error('Firma HMAC inválida en comando reiniciar — se ignora (posible suplantación).')
            return

        logging.warning('Reinicio del equipo solicitado desde el panel — reiniciando en 10s.')
        subprocess.run(
            ['shutdown', '/r', '/t', '10', '/c', 'Reinicio solicitado desde el panel SAIDSOFT'],
            capture_output=True,
        )

    # --- Windows Update nativo (v1: solo escaneo/reporte, nunca instala ni reinicia) ---
    def _verificar_y_escanear_actualizaciones(self, payload):
        # Mismo esquema de firma que consultar_info/reiniciar: solo el nombre del
        # comando, sin campos extra.
        firma_esperada = firmar(self.args.hmac_secret, comando='escanear_actualizaciones')
        if not hmac.compare_digest(firma_esperada, payload.get('firma', '')):
            logging.error('Firma HMAC inválida en comando escanear_actualizaciones — se ignora (posible suplantación).')
            return
        # En un hilo aparte: Windows Update puede tardar varios minutos en responder, y
        # eso no debe bloquear el heartbeat ni la recepción de otros mensajes MQTT —
        # mismo motivo que _manejar_despliegue.
        threading.Thread(target=self._escanear_y_reportar_actualizaciones, daemon=True).start()

    def _hay_conexion_a_internet(self, timeout: int = 5) -> bool:
        """Chequeo rápido de conectividad — mismo endpoint (NCSI) que usa el propio
        Windows para su indicador de estado de red: liviano, sin TLS (evita falsos
        negativos de portales cautivos). Muchas estaciones de este piloto no tienen
        salida a internet por defecto — este chequeo es lo que permite fallar en
        segundos en vez de dejar que `Search()` de Windows Update se cuelgue varios
        minutos intentando conectar sin poder."""
        try:
            with urllib.request.urlopen('http://www.msftconnecttest.com/connecttest.txt', timeout=timeout) as resp:
                return resp.read() == b'Microsoft Connect Test'
        except Exception:
            return False

    def _escanear_y_reportar_actualizaciones(self):
        if not self._hay_conexion_a_internet():
            logging.warning('Escaneo de Windows Update omitido: sin salida a internet.')
            self._reportar_windows_update(
                error='Sin acceso a internet — habilita la salida a internet en esta '
                      'estación para poder escanear actualizaciones.',
            )
            return
        try:
            resultado = self._escanear_actualizaciones_windows()
        except Exception as exc:
            logging.exception('Falló el escaneo de Windows Update')
            self._reportar_windows_update(error=str(exc))
            return
        self._reportar_windows_update(
            pendientes=resultado['pendientes'], requiere_reinicio=resultado['requiere_reinicio'],
        )
        logging.info(
            'Escaneo de Windows Update: %d pendiente(s), requiere_reinicio=%s',
            len(resultado['pendientes']), resultado['requiere_reinicio'],
        )

    def _escanear_actualizaciones_windows(self) -> dict:
        """Escanea actualizaciones de Windows pendientes vía la API COM de Windows
        Update Agent. SOLO `Search` — nunca `Download`/`Install`: v1 es puramente
        informativo, ver docstring del módulo y PLAN_MODERNIZACION.md."""
        import win32com.client

        sesion = win32com.client.Dispatch('Microsoft.Update.Session')
        buscador = sesion.CreateUpdateSearcher()
        resultado = buscador.Search('IsInstalled=0 and IsHidden=0')

        pendientes = []
        for i in range(resultado.Updates.Count):
            actualizacion = resultado.Updates.Item(i)
            kb = ''
            if actualizacion.KBArticleIDs.Count > 0:
                kb = f'KB{actualizacion.KBArticleIDs.Item(0)}'
            pendientes.append({'titulo': actualizacion.Title, 'kb': kb})

        # Reinicio pendiente AHORA (de una instalación previa) — señal más útil para
        # visibilidad de cumplimiento que solo "esta actualización lo pediría al
        # instalarse", que es lo único que expondría iterar RebootRequired por update.
        info_sistema = win32com.client.Dispatch('Microsoft.Update.SystemInfo')
        requiere_reinicio = bool(info_sistema.RebootRequired)

        return {'pendientes': pendientes, 'requiere_reinicio': requiere_reinicio}

    def _reportar_windows_update(self, pendientes=None, requiere_reinicio=False, error=''):
        cuerpo = {'token': self._token()}
        if error:
            cuerpo['error'] = error
        else:
            cuerpo['pendientes'] = pendientes or []
            cuerpo['requiere_reinicio'] = requiere_reinicio
        self._publicar(f'/saidsof/agente/{self.args.codigo}/windows_update/', cuerpo)

    # --- inventario de software instalado (bajo demanda) ---
    def _verificar_y_consultar_software_instalado(self, payload):
        # Mismo esquema de firma que consultar_info/escanear_actualizaciones.
        firma_esperada = firmar(self.args.hmac_secret, comando='consultar_software_instalado')
        if not hmac.compare_digest(firma_esperada, payload.get('firma', '')):
            logging.error(
                'Firma HMAC inválida en comando consultar_software_instalado — se ignora (posible suplantación).',
            )
            return
        # En un hilo aparte, igual que escanear_actualizaciones: en equipos con mucho
        # software instalado, leer el registro completo puede tardar más de lo que
        # conviene bloquear el loop de red de paho.
        threading.Thread(target=self._escanear_y_reportar_software_instalado, daemon=True).start()

    def _escanear_y_reportar_software_instalado(self):
        try:
            programas = self._listar_software_instalado()
        except Exception:
            logging.exception('No se pudo listar el software instalado')
            programas = []
        self._publicar(f'/saidsof/agente/{self.args.codigo}/software_instalado/', {
            'token': self._token(), 'programas': programas,
        })
        logging.info('Software instalado reportado: %d programa(s)', len(programas))

    def _listar_software_instalado(self) -> list:
        """Lee las claves de registro Uninstall (64 y 32 bits, más HKCU para lo instalado
        solo para el usuario actual) — mismo mecanismo que usa el propio Panel de control
        de Windows para armar "Aplicaciones y características". No se usa la clase WMI
        Win32_Product a propósito: es conocida por ser lenta y por reparar/reinstalar
        paquetes MSI como efecto secundario de solo consultarla."""
        script = r"""
$ErrorActionPreference = 'SilentlyContinue'
$rutas = @(
    'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\*',
    'HKLM:\SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\*',
    'HKCU:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\*'
)
$programas = Get-ItemProperty -Path $rutas -ErrorAction SilentlyContinue |
    Where-Object { $_.DisplayName -and -not $_.SystemComponent } |
    Select-Object @{N='nombre';E={$_.DisplayName}}, @{N='version';E={$_.DisplayVersion}}, @{N='fabricante';E={$_.Publisher}}
ConvertTo-Json -Compress -InputObject @($programas)
"""
        # -InputObject @(...) en vez de pipeline: ConvertTo-Json desenvuelve un array de
        # un solo elemento a un objeto suelto si llega por pipeline, lo que rompe el
        # parseo del lado servidor (espera siempre una lista). Pasarlo como -InputObject
        # lo serializa como array sin importar cuántos elementos tenga (0, 1 o muchos).
        salida = subprocess.check_output(
            ['powershell', '-NoProfile', '-Command', script], timeout=30, text=True,
        )
        datos = json.loads(salida) if salida.strip() else []
        return datos if isinstance(datos, list) else [datos]

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
            ruta = self._descargar(payload['url'], usar_cache=payload.get('usar_cache', False))
            self._reportar_instalacion(solicitud_id, 'descargado')

            hash_local = self._sha256_de(ruta)
            # Comparación insensible a mayúsculas/minúsculas: hashlib.hexdigest() de
            # Python es siempre lowercase, pero no todo emisor del hash lo garantiza
            # (ver PLAN_MODERNIZACION.md §10-J, mismo bug encontrado en el agente C#).
            if hash_local.lower() != payload['sha256'].lower():
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

    def _descargar(self, url: str, usar_cache: bool = False) -> str:
        """Descarga a un archivo temporal. Si `usar_cache` y hay un caché de farmacia
        conocido (`cache_url_base`, recibido en el enrolamiento), lo intenta primero —
        best effort: cualquier falla (caché apagado, no tiene el paquete, red LAN caída)
        cae al central sin propagar el error. El caché se asume que replica la misma
        ruta relativa que el central (`/media/despliegues/...`); no hay todavía una
        estación real actuando de caché contra la que confirmar este contrato."""
        ruta = os.path.join(tempfile.gettempdir(), os.path.basename(url) or 'paquete.bin')
        if usar_cache and self.identidad.get('cache_url_base'):
            ruta_relativa = urllib.parse.urlparse(url).path
            url_cache = self.identidad['cache_url_base'].rstrip('/') + ruta_relativa
            try:
                urllib.request.urlretrieve(url_cache, ruta)
                logging.info('Descargado del caché de farmacia: %s', url_cache)
                return ruta
            except Exception as exc:
                logging.warning('Caché de farmacia no disponible (%s: %s), cae al central.', url_cache, exc)
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

    # --- despliegues de POS ---
    def _manejar_despliegue(self, payload):
        despliegue_id = payload['despliegue_id']
        if not (self.args.pos_carpeta_instalacion and self.args.pos_nombre_proceso
                and self.args.pos_comando_iniciar):
            self._reportar_despliegue(
                despliegue_id, 'error',
                detalle='Este agente no tiene configurado el POS '
                        '(--pos-carpeta-instalacion/--pos-nombre-proceso/--pos-comando-iniciar).',
            )
            return
        self._reportar_despliegue(despliegue_id, 'recibido')
        # En un hilo aparte: puede quedar esperando horas (modo "ventana" o "cierre_pos")
        # y no puede bloquear el loop de MQTT (heartbeat, otros comandos) mientras tanto.
        threading.Thread(target=self._procesar_despliegue, args=(payload,), daemon=True).start()

    def _procesar_despliegue(self, payload):
        despliegue_id = payload['despliegue_id']
        ruta_paquete = None
        try:
            ruta_paquete = self._descargar(payload['url'], usar_cache=payload.get('usar_cache', False))
            self._reportar_despliegue(despliegue_id, 'descargado')

            hash_local = self._sha256_de(ruta_paquete)
            hash_esperado = payload['sha256']
            # Comparación insensible a mayúsculas/minúsculas — ver PLAN_MODERNIZACION.md
            # §10-J: el bug que dejó el agente C# original varado en este mismo paso.
            if hash_local.lower() != hash_esperado.lower():
                self._reportar_despliegue(
                    despliegue_id, 'error',
                    detalle=f'SHA-256 no coincide (esperado {hash_esperado}, obtenido {hash_local})',
                )
                return
            self._reportar_despliegue(despliegue_id, 'hash_verificado')

            modo = payload.get('modo_aplicacion', 'inmediato')
            if modo == 'ventana' and payload.get('ventana_fecha_hora'):
                self._esperar_ventana(payload['ventana_fecha_hora'])
            elif modo == 'cierre_pos':
                self._esperar_cierre_pos()
            # 'inmediato' (o ventana sin fecha, defensivo): aplica ya.

            self._aplicar_despliegue(despliegue_id, ruta_paquete, payload)
        except Exception as exc:
            logging.exception('Error procesando despliegue #%s', despliegue_id)
            self._reportar_despliegue(despliegue_id, 'error', detalle=str(exc))
        finally:
            if ruta_paquete:
                try:
                    os.unlink(ruta_paquete)
                except OSError:
                    pass

    def _esperar_ventana(self, iso_fecha_hora: str) -> None:
        try:
            objetivo = datetime.fromisoformat(iso_fecha_hora)
        except ValueError:
            logging.warning('ventana_fecha_hora inválida (%s), aplico ya.', iso_fecha_hora)
            return
        ahora = datetime.now(objetivo.tzinfo) if objetivo.tzinfo else datetime.now()
        espera = (objetivo - ahora).total_seconds()
        if espera > 0:
            logging.info('Esperando %.0fs hasta la ventana programada (%s)', espera, iso_fecha_hora)
            time.sleep(espera)

    def _esperar_cierre_pos(self) -> None:
        logging.info('Esperando a que el POS se cierre por su cuenta para aplicar...')
        while self._pos_corriendo():
            time.sleep(10)

    def _pos_corriendo(self) -> bool:
        try:
            salida = subprocess.check_output(
                ['tasklist', '/FI', f'IMAGENAME eq {self.args.pos_nombre_proceso}.exe', '/NH'],
                text=True, timeout=10,
            )
        except Exception:
            return False
        return self.args.pos_nombre_proceso.lower() in salida.lower()

    def _detener_pos(self, timeout_segundos: int = 30) -> None:
        if not self._pos_corriendo():
            return
        subprocess.run(
            ['taskkill', '/IM', f'{self.args.pos_nombre_proceso}.exe', '/F'],
            capture_output=True, timeout=15,
        )
        limite = time.time() + timeout_segundos
        while self._pos_corriendo() and time.time() < limite:
            time.sleep(1)

    def _iniciar_pos(self) -> None:
        subprocess.Popen(
            [self.args.pos_comando_iniciar],
            cwd=os.path.dirname(self.args.pos_comando_iniciar) or None,
        )

    def _version_pos_actual(self) -> str:
        """Best-effort: versión de archivo del ejecutable del POS antes de tocarlo.
        Si falla (no existe, sin permisos) devuelve '' — no es crítico para aplicar."""
        try:
            salida = subprocess.check_output(
                ['powershell', '-NoProfile', '-Command',
                 f"(Get-Item -LiteralPath '{self.args.pos_comando_iniciar}').VersionInfo.ProductVersion"],
                timeout=10, text=True,
            )
            return salida.strip()
        except Exception:
            return ''

    def _respaldar_carpeta(self, origen: str, destino: str) -> None:
        if os.path.isdir(origen):
            shutil.copytree(origen, destino, dirs_exist_ok=True)

    def _restaurar_carpeta(self, backup_dir: str, destino: str) -> None:
        if os.path.isdir(backup_dir):
            shutil.copytree(backup_dir, destino, dirs_exist_ok=True)

    def _extraer_paquete(self, ruta_zip: str, destino: str) -> None:
        os.makedirs(destino, exist_ok=True)
        with zipfile.ZipFile(ruta_zip) as zf:
            nombres = [n for n in zf.namelist() if n]
            primeros = {n.split('/', 1)[0] for n in nombres}
            # Si TODO el contenido cuelga de una única carpeta raíz (ej.
            # "Cliente/Zabyca.Pos.Desktop.exe" en vez del ejecutable suelto en la raíz
            # del zip), extraer tal cual crea esa carpeta como una subcarpeta NUEVA
            # dentro de pos_carpeta_instalacion, sin tocar los archivos reales del POS
            # — encontrado en el primer despliegue real contra el POS instalado: el
            # panel confirmó "OK" pero la carpeta del POS quedó intacta, con una
            # subcarpeta de más al lado. Si el zip viene así, se extrae saltando ese
            # primer nivel para que el contenido caiga directo en destino.
            prefijo = None
            if len(primeros) == 1:
                candidato = next(iter(primeros))
                if all(n == candidato or n.startswith(candidato + '/') for n in nombres):
                    prefijo = candidato + '/'
            if prefijo is None:
                zf.extractall(destino)
                return
            for info in zf.infolist():
                if info.filename in (prefijo, prefijo.rstrip('/')):
                    continue
                nombre_relativo = info.filename[len(prefijo):]
                if not nombre_relativo:
                    continue
                destino_final = os.path.join(destino, nombre_relativo)
                if info.is_dir():
                    os.makedirs(destino_final, exist_ok=True)
                    continue
                os.makedirs(os.path.dirname(destino_final), exist_ok=True)
                with zf.open(info) as origen, open(destino_final, 'wb') as f:
                    shutil.copyfileobj(origen, f)

    def _aplicar_despliegue(self, despliegue_id, ruta_paquete, payload):
        carpeta_pos = self.args.pos_carpeta_instalacion
        version_previa = self._version_pos_actual()

        self._detener_pos()
        self._reportar_despliegue(despliegue_id, 'pos_cerrado', version_previa=version_previa)

        # Respaldo completo de la carpeta del POS antes de tocar nada — es lo único que
        # permite un rollback real si el POS no vuelve a levantar. Puede pesar bastante
        # en disco (carpeta del POS completa, no solo lo que cambia el paquete); para el
        # piloto (una estación, un despliegue a la vez) alcanza sin necesidad de un
        # esquema más fino de backup incremental.
        backup_dir = tempfile.mkdtemp(prefix='saidsoft_backup_')
        try:
            self._respaldar_carpeta(carpeta_pos, backup_dir)
            self._extraer_paquete(ruta_paquete, carpeta_pos)
            self._reportar_despliegue(despliegue_id, 'aplicado')

            self._iniciar_pos()
            self._reportar_despliegue(despliegue_id, 'pos_relanzado')

            time.sleep(self.args.espera_liveness_segundos)
            if self._pos_corriendo():
                self._reportar_despliegue(despliegue_id, 'ok', version_nueva=payload.get('version', ''))
                shutil.rmtree(backup_dir, ignore_errors=True)
            else:
                self._rollback(
                    despliegue_id, carpeta_pos, backup_dir,
                    'El POS no quedó corriendo tras aplicar el despliegue',
                )
        except Exception as exc:
            logging.exception('Error aplicando despliegue #%s, se intenta rollback', despliegue_id)
            self._rollback(despliegue_id, carpeta_pos, backup_dir, str(exc))

    def _rollback(self, despliegue_id, carpeta_pos, backup_dir, motivo):
        try:
            self._detener_pos()
            self._restaurar_carpeta(backup_dir, carpeta_pos)
            try:
                self._iniciar_pos()
            except Exception:
                # Que no se pueda relanzar el POS (ej. ruta mal configurada) no debe
                # tapar que el rollback de archivos sí se completó — sin este except,
                # la excepción se escapaba de _rollback() entero, nunca se reportaba
                # el paso 'rollback', y el despliegue terminaba con un 'error' genérico
                # que no distinguía "no se pudo aplicar" de "se aplicó mal, pero ya se
                # restauró" (encontrado en el primer despliegue de POS real del piloto,
                # ML016-A, con el POS todavía sin instalar en la estación).
                logging.exception(
                    'Rollback del despliegue #%s: archivos restaurados, pero no se pudo relanzar el POS',
                    despliegue_id,
                )
        finally:
            shutil.rmtree(backup_dir, ignore_errors=True)
        self._reportar_despliegue(despliegue_id, 'rollback', detalle=motivo)

    def _reportar_despliegue(self, despliegue_id, paso, detalle='', version_previa=None, version_nueva=None):
        cuerpo = {'token': self._token(), 'despliegue_id': despliegue_id, 'paso': paso, 'detalle': detalle}
        if version_previa is not None:
            cuerpo['version_previa'] = version_previa
        if version_nueva is not None:
            cuerpo['version_nueva'] = version_nueva
        self._publicar(f'/saidsof/agente/{self.args.codigo}/despliegue_estado/', cuerpo)
        logging.info('Despliegue #%s -> %s%s', despliegue_id, paso, f' ({detalle})' if detalle else '')

    # --- arranque ---
    def correr(self):
        logging.info('Agente %s arrancando para la estación %s', VERSION_AGENTE_PRUEBA, self.args.codigo)
        # connect_async() (no bloquea, no lanza si el broker todavía no responde) +
        # loop_forever(retry_first_connection=True): con connect() simple, si el primer
        # intento fallaba (broker caído o red no lista al bootear como servicio de
        # Windows), la excepción mataba este hilo en silencio y el servicio quedaba
        # "Running" sin hacer nada — encontrado corriendo el .exe de servicio compilado
        # en modo debug sin broker disponible. Así, loop_forever reintenta solo desde el
        # primer intento, igual que hace con reconexiones posteriores.
        self.client.connect_async(self.args.host, self.args.puerto, keepalive=60)
        threading.Thread(target=self.bucle_heartbeat, daemon=True).start()
        threading.Thread(target=self.bucle_metricas, daemon=True).start()
        threading.Thread(target=self.bucle_log_pos, daemon=True).start()
        self.client.loop_forever(retry_first_connection=True)

    def detener(self):
        """Corta `loop_forever()` de forma prolija — lo usa servicio_windows.py al parar
        el servicio (SvcStop). Un disconnect() intencional hace que loop_forever()
        retorne en vez de seguir reintentando conectar."""
        logging.info('Deteniendo el agente...')
        self.client.disconnect()


# (campo, default) — comparte la lista de opciones entre la CLI (argparse, abajo) y
# servicio_windows.py, que arma el mismo objeto de args a partir de config.json en vez
# de parsear argv (un servicio de Windows no recibe argumentos de línea de comandos
# cómodamente).
CAMPOS_CONFIG = [
    ('codigo', None),
    ('host', '127.0.0.1'),
    ('puerto', 1883),
    ('usuario', ''),
    ('password', ''),
    ('tls', False),
    ('ca_cert', ''),
    ('hmac_secret', ''),
    ('intervalo_heartbeat', 60),
    ('intervalo_metricas', 300),
    ('intervalo_log_pos', 300),
    ('pos_log_relativo', os.path.join('Logs', 'GeneraXML.txt')),
    ('pos_carpeta_instalacion', ''),
    ('pos_nombre_proceso', ''),
    ('pos_comando_iniciar', ''),
    ('espera_liveness_segundos', 15),
]


def args_desde_config(config: dict) -> argparse.Namespace:
    valores = {campo: config.get(campo, default) for campo, default in CAMPOS_CONFIG}
    if not valores['codigo']:
        raise ValueError('config.json debe tener "codigo"')
    return argparse.Namespace(**valores)


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
    parser.add_argument(
        '--intervalo-metricas', type=int, default=300,
        help='Segundos entre reportes de CPU/RAM/disco (default 300 = 5min). Solo se envían si la '
             'estación está marcada "monitorear_recursos" en el panel.',
    )
    parser.add_argument(
        '--intervalo-log-pos', type=int, default=300,
        help='Segundos entre revisiones del log de errores del POS (default 300 = 5min). '
             'Solo corre si --pos-carpeta-instalacion está configurado.',
    )
    parser.add_argument(
        '--pos-log-relativo', default=os.path.join('Logs', 'GeneraXML.txt'),
        help='Ruta del log de errores del POS, relativa a --pos-carpeta-instalacion '
             '(default "Logs\\GeneraXML.txt" — pese al nombre, log4net lo usa como log general, '
             'no solo de generación de XML).',
    )
    parser.add_argument(
        '--pos-carpeta-instalacion', default='',
        help='Carpeta donde vive el POS real, ej. "C:\\Program Files (x86)\\Farmamia Cia Ltda - Elipsys\\Cliente". '
             'Vacío = los despliegues de POS que lleguen se reportan como error (agente sin POS configurado).',
    )
    parser.add_argument(
        '--pos-nombre-proceso', default='',
        help='Nombre del proceso del POS SIN ".exe" (para detectar si sigue vivo tras aplicar/relanzar).',
    )
    parser.add_argument(
        '--pos-comando-iniciar', default='',
        help='Ruta completa al .exe del POS que se relanza tras aplicar un despliegue.',
    )
    parser.add_argument(
        '--espera-liveness-segundos', type=int, default=15,
        help='Segundos a esperar tras relanzar el POS antes de chequear si sigue vivo (default 15).',
    )
    args = parser.parse_args()

    _configurar_logging()
    AgentePrueba(args).correr()


if __name__ == '__main__':
    main()
