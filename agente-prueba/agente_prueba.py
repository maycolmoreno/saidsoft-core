r"""Agente SAIDSOFT — reemplazo en Python del agente real perdido (saidsoft-agente, C#,
repo aparte cuyo código fuente no se pudo recuperar; ver PLAN_MODERNIZACION.md §10-K).

Empezó como "agente de prueba" (implementación de referencia liviana del protocolo
MQTT) pensado para copiarse a una estación Windows real (como .exe standalone, ver
build.ps1) y validar el flujo servidor<->agente sin depender del simulador Django. El
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
import base64
import hashlib
import hmac
import ipaddress
import json
import logging
import os
import platform
import random
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
from datetime import datetime, timedelta

import paho.mqtt.client as mqtt

ARCHIVO_IDENTIDAD = 'identidad.json'
ARCHIVO_LOG = 'agente_prueba.log'
VERSION_AGENTE_PRUEBA = 'agente-prueba-0.38'

# Claves PÚBLICAS Ed25519 (base64) con las que el servidor firma los catálogos (0.38, H-03 y
# S-11; ver apps/monitoreo/firma_catalogo.py). Se acepta cualquiera de la lista: para rotar,
# primero se distribuye un agente con la vieja y la nueva, y recién después el servidor
# cambia de clave. Vacía = este agente valida los catálogos solo con HMAC, como 0.37.
# La pública sale de `python manage.py clave_firma_catalogos` en el servidor.
CLAVES_PUBLICAS_CATALOGO = ()

# SEC-1 (auditoría 22-ago-2026): ventana de tolerancia para el `timestamp` firmado en
# cada mensaje del servidor — sin esto, capturar un mensaje MQTT válido (comando,
# script, despliegue o instalación de software) alcanzaba para reproducirlo indefinidamente
# más tarde. No es a prueba de replay dentro de la ventana (MQTT QoS0 fire-and-forget no
# da pie a un nonce de un solo uso sin estado compartido), pero acota el daño a mensajes
# capturados hace menos de 2 minutos, y en ejecutar_script/despliegue/software el par de
# ids (resultado_id/despliegue_id/solicitud_id) ya actúa como nonce natural: reenviar el
# mismo mensaje dentro de la ventana produce un reporte de estado duplicado sobre un
# resultado que el servidor probablemente ya cerró.
VENTANA_TIMESTAMP_SEGUNDOS = 120

# Cuanto espera el agente antes de volver a intentar corregir su reloj (ver
# `_corregir_reloj`). 15 minutos, por tres razones:
#
#   - Si `net time` funciona, el reloj queda bien y no hay mas rechazos: el cooldown
#     solo entra en juego cuando el intento FALLA, y ahi lo que importa es no quedarse
#     ejecutando `net time` en bucle contra un DC inalcanzable.
#   - Es el mismo orden de magnitud que el resto de los ciclos del agente
#     (`intervalo_eventos_sistema` son 900 s), asi que no introduce un numero nuevo.
#   - Aun en el peor caso son 4 intentos por hora, despreciable, y sigue siendo
#     muchisimo mas rapido que la alternativa actual: que alguien entre por MeshCentral.
COOLDOWN_CORRECCION_RELOJ_SEGUNDOS = 900

# Cada cuanto el agente compara su reloj contra el servidor de hora POR SU CUENTA, sin
# esperar a que llegue un comando.
#
# El disparador por rechazo no alcanza y eso se midio: el 22-sep-2026 habia 6 estaciones
# de 39 (15%) con el reloj fuera de ventana, y ninguna se iba a curar sola porque nadie
# les estaba mandando comandos. Aparecieron porque se las fue a buscar. A 1.800 equipos
# eso serian ~270 sordas esperando que alguien las note.
#
# Una hora acota la deriva muy por debajo de los 120 s de la ventana (un reloj que se
# corre mas de 2 minutos por hora tiene un problema de hardware, no de sincronizacion) y
# cuesta una llamada SMB por estacion por hora: a 1.800, media llamada por segundo
# repartida.
INTERVALO_CHEQUEO_RELOJ_SEGUNDOS = 3600

# Cuanto tiene que saltar el reloj para contarlo como una autocorreccion de verdad.
#
# El bucle corre `net time /set` siempre —es idempotente y no hace falta parsear nada—
# pero contar cada corrida arruinaria la senal: el contador existe para delatar al equipo
# con la pila agotada, y si sube una vez por hora en las ~1.800 estaciones deja de
# distinguir nada. 30 s es el mismo umbral con el que el panel ya avisa "reloj corrido".
SALTO_MINIMO_PARA_CONTAR_SEGUNDOS = 30


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
    para hardware_id (ver README.md del proyecto principal, sección de enrolamiento).

    Devuelve `''` si no se pudo leer, y eso es deliberado (`A-6` de la auditoría por
    agentes). Antes caía a una constante de reserva escrita en este mismo archivo, que es
    lo peor posible para este campo: `hardware_id` existe para que un equipo ajeno no pueda
    pedir el token de una estación y suplantarla, y la única defensa del servidor es
    comparar el que llega contra el que se fijó la primera vez. Dos equipos donde la
    lectura fallara compartían el mismo valor y esa comparación pasaba sola — con un valor
    que, además, está en el repo, así que no es ningún secreto.

    No se reemplaza por otro valor inventado (un UUID generado y guardado no serviría:
    `identidad.json` es justo lo que se pierde en el caso que hace falta cubrir). Vacío es
    la respuesta honesta —este equipo no sabe identificarse— y el servidor ya lo trata
    bien: no lo fija como identidad y niega el re-enrolamiento de una estación que sí tenía
    uno, que es el lado seguro del error. Para una máquina de desarrollo sin registro de
    Windows, el escape es `--hardware-id`, explícito y distinto por equipo.
    """
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r'SOFTWARE\Microsoft\Cryptography') as clave:
            return winreg.QueryValueEx(clave, 'MachineGuid')[0]
    except Exception:
        logging.error(
            'No se pudo leer MachineGuid del registro: me enrolo SIN identidad de hardware. '
            'El servidor va a exigir reaprobación manual si esta estación tiene que '
            're-enrolarse. Para fijarla a mano, usar --hardware-id.',
        )
        return ''


# Codificacion de la salida de los subprocesos. Un solo lugar, porque equivocarlo no
# produce un error sino texto corrupto que sigue viaje: el agente reporta, el servidor
# guarda y el panel muestra "ConfiguraciÃ³n" sin que nada falle en el camino.
#
# Encontrado el 21-sep-2026: `text=True` sin `encoding=` hace que Python decodifique con
# la pagina de codigos local (cp1252 en un Windows en espanol) mientras el proceso
# escribe en otra. Estaba bien resuelto en la lectura del visor de eventos y mal en
# TODO el resto -- incluido el inventario de software, donde medio catalogo de un
# Windows en espanol lleva acentos.
#
# Son dos familias y no se arreglan igual:
#   PowerShell   -> se le fuerza la salida a UTF-8 y se decodifica UTF-8.
#   .exe nativos -> `ping`, `tasklist` y los instaladores escriben en la pagina OEM del
#                   sistema (cp850 en espanol) y no hay como pedirles otra cosa; se
#                   decodifica con 'oem', que es justo esa.
PREFIJO_SALIDA_UTF8 = '[Console]::OutputEncoding = [System.Text.Encoding]::UTF8\n'


def correr_powershell(script: str, timeout: int) -> str:
    """Corre `script` en PowerShell y devuelve su salida ya decodificada."""
    return subprocess.check_output(
        ['powershell', '-NoProfile', '-Command', PREFIJO_SALIDA_UTF8 + script],
        timeout=timeout, text=True, encoding='utf-8', errors='replace',
        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
    )


# --- Instaladores: sin tuberías y con el árbol de procesos acotado (0.37) ---
#
# Hasta 0.36 el instalador corría con `subprocess.run(shell=True, capture_output=True,
# timeout=600)`. `capture_output` crea una tubería, y `run` espera a que se CIERRE, no a
# que termine el instalador: si el instalador deja vivo un proceso hijo que heredó la
# tubería, el agente espera para siempre. Y el `timeout` no lo salva: al vencer mata solo
# el `cmd.exe` y vuelve a esperar la tubería, esta vez sin límite.
#
# Pasó con LibreOffice en ML008-A y ML008-B (19-oct-2026): el programa quedó instalado,
# el agente nunca reportó "instalado" y las dos cajas quedaron mudas dos días. Medido en
# Windows con un instalador que termina en <1 s y deja un hijo vivo 12 s: `run` devolvía
# a los 12,3 s, y con timeout=3 levantaba TimeoutExpired también a los 12,3 s. Con la
# salida a un archivo y `Popen.wait`, 0,2 s.
#
# Ahora la salida va a un archivo temporal (un hijo que lo herede no bloquea a nadie), se
# espera al proceso y no a la tubería, y al vencer el tiempo se mata el ÁRBOL completo.
TIMEOUT_INSTALADOR_SEGUNDOS = 600


def matar_arbol(proceso) -> None:
    """Termina `proceso` y todos sus descendientes. En Windows `taskkill /T`; matar solo
    el `cmd.exe` deja vivos al instalador y a sus hijos."""
    try:
        if os.name == 'nt':
            subprocess.run(
                ['taskkill', '/F', '/T', '/PID', str(proceso.pid)],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30,
            )
        else:
            proceso.kill()
        proceso.wait(timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        logging.exception('No se pudo terminar el proceso %s y sus hijos', proceso.pid)


def ejecutar_sin_tuberias(comando: str, timeout: int) -> tuple:
    """Corre `comando` (con `cmd`, como siempre) y devuelve (código de salida, últimas
    líneas de su salida). El código es None si se venció `timeout`: en ese caso el árbol
    de procesos ya fue terminado."""
    descriptor, ruta = tempfile.mkstemp(prefix='saidsoft_comando_', suffix='.log')
    try:
        with os.fdopen(descriptor, 'wb') as salida:
            proceso = subprocess.Popen(
                comando, shell=True, stdin=subprocess.DEVNULL, stdout=salida, stderr=subprocess.STDOUT,
            )
            try:
                codigo = proceso.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                matar_arbol(proceso)
                codigo = None
        with open(ruta, 'rb') as leido:
            leido.seek(0, os.SEEK_END)
            leido.seek(max(0, leido.tell() - 4000))
            texto = leido.read().decode('oem' if os.name == 'nt' else 'utf-8', errors='replace')
        return codigo, texto[-2000:]
    finally:
        try:
            os.unlink(ruta)
        except OSError:
            # Un hijo del instalador que siga vivo puede tener el archivo abierto: queda en
            # TEMP (unos KB) y Windows lo limpia después.
            pass


def _leer_final(ruta: str, encoding: str, caracteres: int) -> str:
    with open(ruta, 'rb') as leido:
        leido.seek(0, os.SEEK_END)
        leido.seek(max(0, leido.tell() - caracteres * 4))
        return leido.read().decode(encoding, errors='replace')[-caracteres:]


def ejecutar_capturando(comando: list, timeout: int, *, encoding: str | None = None,
                        caracteres: int = 4000) -> tuple:
    """Como `ejecutar_sin_tuberias`, pero con la salida estándar y la de error por
    separado, que es lo que necesita el reporte de un script (0.38). Devuelve (código,
    stdout, stderr), con el código en None si se venció `timeout` y el árbol de procesos ya
    terminado. `encoding` None = la página de códigos del sistema, como `text=True` sin
    `encoding=`, que es lo que usaba antes para los .bat."""
    import locale

    codificacion = encoding or locale.getpreferredencoding(False)
    descriptor_out, ruta_out = tempfile.mkstemp(prefix='saidsoft_stdout_', suffix='.log')
    descriptor_err, ruta_err = tempfile.mkstemp(prefix='saidsoft_stderr_', suffix='.log')
    try:
        with os.fdopen(descriptor_out, 'wb') as salida, os.fdopen(descriptor_err, 'wb') as errores:
            proceso = subprocess.Popen(comando, stdin=subprocess.DEVNULL, stdout=salida, stderr=errores)
            try:
                codigo = proceso.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                matar_arbol(proceso)
                codigo = None
        return (codigo, _leer_final(ruta_out, codificacion, caracteres),
                _leer_final(ruta_err, codificacion, caracteres))
    finally:
        for ruta in (ruta_out, ruta_err):
            try:
                os.unlink(ruta)
            except OSError:
                pass


# --- Vigilancia del servicio (0.37) ---
#
# `servicio_windows.py` corre el agente en un hilo y hasta 0.36 solo esperaba la orden de
# detenerse: si el agente se colgaba, el servicio seguía informando RUNNING y Windows
# nunca lo reiniciaba (ML008-B, 19 al 21-oct-2026: proceso vivo, cero actividad). Ahora
# el servicio mira cada minuto esta función, y si devuelve un motivo termina el proceso
# con error, para que la recuperación del servicio (`configurar_recuperacion_servicio`)
# lo vuelva a levantar.
#
# La señal de vida es el CICLO del hilo de latidos, no el envío: el ciclo avanza aunque no
# haya conexión al broker o la estación no esté enrolada, así que un broker caído no
# provoca reinicios. Solo se dispara si el hilo queda trabado dentro de una vuelta.
TOLERANCIA_LATIDO_MINIMA_SEGUNDOS = 600
NOMBRE_SERVICIO = 'SaidsoftAgente'


def motivo_para_reiniciar(agente, hilo_agente, ahora=None) -> str:
    """'' si el agente está sano; si no, por qué hay que reiniciar el servicio."""
    if not hilo_agente.is_alive():
        return 'el hilo principal del agente terminó'
    ultimo = getattr(agente, 'ultimo_ciclo_latido', None)
    if ultimo is None:
        return ''
    tolerancia = max(TOLERANCIA_LATIDO_MINIMA_SEGUNDOS, 5 * int(agente.args.intervalo_heartbeat))
    atraso = (time.monotonic() if ahora is None else ahora) - ultimo
    if atraso > tolerancia:
        return f'el ciclo de latidos no avanza hace {int(atraso)} s (tolerancia {tolerancia} s)'
    return ''


def configurar_recuperacion_servicio(nombre: str = NOMBRE_SERVICIO) -> bool:
    """Que Windows reinicie el servicio si el proceso termina con error. Mismos tiempos
    que `instalar-servicio.ps1` (30 s, 60 s, 120 s; el contador se reinicia al día). Se
    vuelve a aplicar en cada arranque para las estaciones instaladas por otro camino (el
    paquete de apertura, versiones viejas del instalador): sin esto, el vigilante termina
    el proceso y nadie lo vuelve a levantar."""
    try:
        resultado = subprocess.run(
            ['sc.exe', 'failure', nombre, 'reset=', '86400', 'actions=', 'restart/30000/restart/60000/restart/120000'],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        logging.exception('No se pudo configurar la recuperación del servicio %s', nombre)
        return False
    if resultado.returncode != 0:
        logging.warning('sc.exe failure devolvió %s: el servicio no se reiniciará solo si falla', resultado.returncode)
        return False
    return True


# --- Consulta del visor de eventos: código CONSTANTE, datos por la entrada estándar ---
#
# Hasta 0.36 `_leer_eventos_sistema` armaba el script insertando `log` y `proveedor` del
# catálogo entre comillas dentro del código, y lo corría con `powershell -Command` como
# LocalSystem. Un valor con una comilla cerraba el literal y lo que seguía pasaba a ser
# código (P0-1; docs/auditoria/fase_c1_laboratorio.md, C-5).
#
# Ahora el script es siempre el mismo texto y los filtros llegan como JSON por stdin: el
# script los lee con [Console]::In y los usa como VALORES de un hashtable, nunca como
# texto de código. Va con -EncodedCommand (UTF-16LE en base64) para que la línea de
# comandos no dependa de cómo Windows reparte las comillas. El JSON viaja en ASCII puro
# (`ensure_ascii`), así que la página de códigos de la consola no lo altera.
#
# Lo que el script NO hace es confiar en la forma de los datos: castea cada valor al tipo
# que espera y descarta un log que no sea System/Application. La validación de formato del
# lado de Python (`_filtros_consulta_eventos`) es otra capa, no la que evita la inyección.
SCRIPT_CONSULTA_EVENTOS = r'''
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$ErrorActionPreference = 'SilentlyContinue'
try {
    $datos = [Console]::In.ReadToEnd() | ConvertFrom-Json -ErrorAction Stop
    $inicio = [datetime]::ParseExact([string]$datos.inicio, 's', [Globalization.CultureInfo]::InvariantCulture)
} catch {
    [Console]::Error.WriteLine('entrada invalida para la consulta de eventos')
    exit 2
}
$r = @()
foreach ($f in @($datos.fuentes)) {
    $log = [string]$f.log
    if (@('System', 'Application') -notcontains $log) { continue }
    $ids = @(@($f.ids) | ForEach-Object { [int]$_ })
    if ($ids.Count -eq 0) { continue }
    $filtro = @{ LogName = $log; Id = $ids; StartTime = $inicio }
    $proveedor = [string]$f.proveedor
    if ($proveedor) { $filtro['ProviderName'] = $proveedor }
    $e = Get-WinEvent -FilterHashtable $filtro -ErrorAction SilentlyContinue
    $r += $e | Group-Object Id | ForEach-Object {
        $u = $_.Group | Sort-Object TimeCreated -Descending | Select-Object -First 1
        [PSCustomObject]@{ log = $log; id = [int]$_.Name; cantidad = $_.Count; origen = $u.ProviderName; mensaje = ($u.Message -replace '\s+', ' ') }
    }
}
ConvertTo-Json -InputObject @($r) -Compress -Depth 3
'''
SCRIPT_CONSULTA_EVENTOS_CODIFICADO = base64.b64encode(SCRIPT_CONSULTA_EVENTOS.encode('utf-16-le')).decode('ascii')
LOGS_EVENTOS_PERMITIDOS = ('System', 'Application')
# El mismo formato que valida el servidor (`apps.monitoreo.models.PATRON_PROVEEDOR_EVENTO`).
# Acá es defensa adicional: el agente no confía en que lo que llega pasó por Django.
PATRON_PROVEEDOR_EVENTO = re.compile(r'\A[A-Za-z0-9][A-Za-z0-9 ._/()-]{0,119}\Z')
TIMEOUT_CONSULTA_EVENTOS_SEGUNDOS = 60


def leer_numero_serie() -> str:
    try:
        return correr_powershell('(Get-CimInstance Win32_BIOS).SerialNumber', timeout=10).strip() \
            or 'SN-DESCONOCIDO'
    except Exception:
        return 'SN-DESCONOCIDO'


def leer_mac_de_ip(ip: str) -> str:
    """MAC (AA:BB:CC:DD:EE:FF) de la placa que tiene asignada `ip`, o '' si no se pudo
    leer. Mejor vacía que inventada: una MAC de relleno compartida rompería el cruce con
    la tabla ARP igual que 'SN-DESCONOCIDO' rompe el cruce por serial."""
    try:
        ipaddress.ip_address(ip)  # se interpola en el script: solo una IP válida entra
        salida = correr_powershell(
            f"(Get-NetAdapter -InterfaceIndex (Get-NetIPAddress -IPAddress '{ip}' "
            f"-ErrorAction Stop).InterfaceIndex).MacAddress",
            timeout=15,
        )
    except Exception:
        return ''
    crudo = salida.strip().replace('-', '').replace(':', '').upper()
    if len(crudo) != 12 or not all(c in '0123456789ABCDEF' for c in crudo):
        return ''
    return ':'.join(crudo[i:i + 2] for i in range(0, 12, 2))


def firmar(secreto: str, **campos) -> str:
    """Debe coincidir exactamente con apps.catalogo.services.firmar_payload del
    servidor: HMAC-SHA256 sobre los valores unidos con "|", en el orden en que se
    pasan los kwargs (Python 3.7+ preserva ese orden)."""
    mensaje = '|'.join(str(v) for v in campos.values())
    return hmac.new(secreto.encode(), mensaje.encode(), hashlib.sha256).hexdigest()


def version_catalogo(payload: dict) -> str:
    """Version de un catalogo recibido: los primeros 12 del SHA-256 del texto firmado
    `catalogo`, igual que `version_de` en apps/monitoreo/servicios_pos.py. Vacia si el
    mensaje no lo trae (servidor anterior al catalogo firmado)."""
    cuerpo = payload.get('catalogo')
    if not isinstance(cuerpo, str):
        return ''
    return hashlib.sha256(cuerpo.encode('utf-8')).hexdigest()[:12]


def firma_ed25519_valida(claves, mensaje: bytes, firma_b64) -> bool:
    """True si `firma_b64` es una firma Ed25519 de `mensaje` hecha con la privada de
    alguna de `claves` (públicas en base64). Cualquier dato mal formado es False.

    `cryptography` ya viene dentro del ejecutable. Si faltara, se informa y se rechaza:
    nunca se acepta un catálogo sin poder verificarlo."""
    try:
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    except ImportError:
        logging.error('Falta la librería cryptography: no se puede verificar la firma Ed25519.')
        return False
    try:
        firma = base64.b64decode(str(firma_b64), validate=True)
    except ValueError:
        return False
    for clave in claves:
        try:
            Ed25519PublicKey.from_public_bytes(base64.b64decode(clave, validate=True)).verify(firma, mensaje)
            return True
        except (InvalidSignature, ValueError):
            continue
    return False


def offset_utc_minutos() -> int:
    """Offset de la zona horaria configurada, en minutos (Ecuador: -300).

    `time.timezone` viene en segundos al OESTE de UTC (o sea, con el signo invertido
    respecto de como se escribe un offset: UTC-5 es +18000). `altzone` es el equivalente
    mientras rige horario de verano — Ecuador no lo usa, pero una máquina con la región
    mal asignada puede estar en una zona que sí, y entonces el valor bueno es ese.
    """
    en_horario_de_verano = time.daylight and time.localtime().tm_isdst > 0
    return -round((time.altzone if en_horario_de_verano else time.timezone) / 60)


def leer_zona_horaria() -> str:
    """El identificador de zona horaria de Windows, ej. "SA Pacific Standard Time".

    Se lee del registro y NO con `tzutil /g`: es el mismo dato sin pagar un subproceso,
    así que se puede leer en cada latido en vez de cachearlo.

    La primera versión sí cacheaba al arrancar, con el argumento de que la región casi
    nunca cambia y que al corregirla el agente se reiniciaría igual. Eso resultó falso:
    el 14-sep-2026 se corrigió la zona de ML016-B con un script del panel —que no
    reinicia el agente— y la estación siguió reportando "Eastern Standard Time (Mexico)"
    con la región ya arreglada. Un panel que muestra el problema después de resuelto es
    peor que uno que no lo muestra: manda a arreglar lo que ya está bien.
    """
    try:
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r'SYSTEM\CurrentControlSet\Control\TimeZoneInformation',
        ) as clave:
            return winreg.QueryValueEx(clave, 'TimeZoneKeyName')[0].strip()
    except (ImportError, OSError, ValueError):
        # No es motivo para que el agente falle: sin este dato el panel igual ve el
        # offset, que detecta una región de otro huso (aunque no una del mismo).
        logging.warning('No se pudo leer la zona horaria del registro.', exc_info=True)
        return ''


def timestamp_en_ventana(timestamp, ventana_segundos: int = VENTANA_TIMESTAMP_SEGUNDOS) -> bool:
    try:
        return abs(time.time() - float(timestamp)) <= ventana_segundos
    except (TypeError, ValueError):
        return False


INTENTOS_DESCARGA = 6
# Por operación de socket, no por descarga entera: si en 60 s no llega un solo byte, la
# conexión se da por muerta y se reintenta. Un instalador grande por un enlace lento puede
# tardar media hora y eso está bien mientras siga llegando algo.
TIMEOUT_LECTURA_DESCARGA = 60


def bajar_archivo(url: str, ruta: str, *, intentos: int = INTENTOS_DESCARGA,
                  timeout: float = TIMEOUT_LECTURA_DESCARGA, espera_base: float = 5) -> None:
    """Descarga `url` en `ruta`, retomando desde donde se cortó.

    Reemplaza a `urllib.request.urlretrieve`, que tenía los dos defectos que aparecieron
    al instalar Firefox 158 (95 MB) en varias farmacias a la vez (18-oct-2026):
    - Sin timeout: una conexión que se quedaba colgada dejaba la instalación en
      "descargando" para siempre, sin error y sin reintento.
    - Sin reintento: un corte a mitad (MCU01-ADM: "retrieval incomplete: got only
      7814764 out of 95131488 bytes") era un error final, y el siguiente intento manual
      volvía a bajar desde el byte cero.

    Retoma con `Range`, que nginx sirve para los archivos de /media/. Si el servidor no
    lo respeta (responde 200 en vez de 206), vuelve a empezar el archivo. El tamaño final
    se compara contra el que anunció el servidor; el SHA-256 lo verifica quien llama."""
    import http.client
    import urllib.error

    total = None
    for intento in range(1, intentos + 1):
        ya = os.path.getsize(ruta) if intento > 1 and os.path.exists(ruta) else 0
        cabeceras = {'Range': f'bytes={ya}-'} if ya else {}
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=cabeceras), timeout=timeout) as resp:
                if resp.status == 206:
                    total = int(resp.headers['Content-Range'].rsplit('/', 1)[1])
                    modo = 'ab'
                else:
                    largo = resp.headers.get('Content-Length')
                    total = int(largo) if largo else None
                    modo = 'wb'
                with open(ruta, modo) as f:
                    shutil.copyfileobj(resp, f, 1 << 16)
            if total is None or os.path.getsize(ruta) == total:
                return
            raise http.client.IncompleteRead(b'', total - os.path.getsize(ruta))
        except urllib.error.HTTPError as exc:
            # 416: se pidió desde el final, o sea que ya estaba completo.
            if exc.code == 416 and total is not None and os.path.exists(ruta) and os.path.getsize(ruta) == total:
                return
            if intento == intentos or exc.code < 500 and exc.code != 416:
                raise
            error = exc
        except (OSError, http.client.HTTPException, ValueError) as exc:
            if intento == intentos:
                raise
            error = exc
        bajado = os.path.getsize(ruta) if os.path.exists(ruta) else 0
        logging.warning(
            'Descarga de %s cortada en %s de %s bytes (intento %s/%s: %s). Se retoma.',
            url, bajado, total if total is not None else '?', intento, intentos, error,
        )
        time.sleep(min(60, espera_base * intento))


def _version_mayor_o_igual(version_actual: str, version_candidata: str) -> bool:
    """True si `version_actual` >= `version_candidata`, comparando el sufijo numérico
    ("agente-prueba-0.7" -> (0, 7)). Usada para nunca aplicar un downgrade automático
    -- encontrado en producción (ML006-A, 25/26-ago-2026): un mensaje MQTT retenido
    de actualizar_agente que quedó sin limpiar (la actualización que pedía nunca se
    aplicó con éxito) le llegó a la estación en la SIGUIENTE reconexión -- incluso
    después de haber sido actualizada a mano a una versión más nueva -- y downgradeó
    el agente de vuelta a la vieja. Si no se puede parsear cualquiera de las dos
    versiones, devuelve False (mejor pecar de aplicar la actualización que de
    trabarse por un formato de versión inesperado)."""
    try:
        actual = tuple(int(p) for p in version_actual.rsplit('-', 1)[-1].split('.'))
        candidata = tuple(int(p) for p in version_candidata.rsplit('-', 1)[-1].split('.'))
        return actual >= candidata
    except (ValueError, IndexError):
        return False


class AgentePrueba:
    def __init__(self, args):
        self.args = args
        # (tipo, origen_id) -> orden_uid de la última orden VÁLIDA recibida de ese origen
        # (0.38). Se devuelve en cada reporte para que el servidor sepa a qué orden
        # pertenece, sin adivinar por el origen. Ver _recordar_orden.
        self._orden_de = {}
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
        """Un identidad.json ilegible se aparta y se arranca sin identidad (la estación se
        vuelve a enrolar con su hardware_id), en vez de dejar que la excepción tumbe el
        servicio. Antes reventaba en el constructor, antes de escribir una sola línea de
        log: GSD13-ADM (18-oct-2026) estuvo 15 días sin latir con un identidad.json de 501
        bytes en cero -- un corte de luz a mitad de _guardar_identidad -- y el único rastro
        era el error 0x20000001 del SCM.

        utf-8-sig y no utf-8: lee igual con o sin BOM (mismo motivo que config.json en
        servicio_windows._cargar_config)."""
        if not os.path.exists(ARCHIVO_IDENTIDAD):
            return {}
        try:
            with open(ARCHIVO_IDENTIDAD, encoding='utf-8-sig') as f:
                identidad = json.load(f)
            if not isinstance(identidad, dict):
                raise ValueError(f'se esperaba un objeto JSON, vino {type(identidad).__name__}')
            return identidad
        except (ValueError, UnicodeDecodeError) as exc:
            # JSONDecodeError es subclase de ValueError. Se renombra en vez de borrar: si
            # alguna vez hace falta ver qué tenía, sigue ahí.
            apartado = f'{ARCHIVO_IDENTIDAD}.corrupto-{int(time.time())}'
            os.replace(ARCHIVO_IDENTIDAD, apartado)
            logging.error(
                '%s ilegible (%s): se aparta como %s y la estación se vuelve a enrolar.',
                ARCHIVO_IDENTIDAD, exc, apartado,
            )
            return {}

    def _guardar_identidad(self):
        """Escritura atómica: temporal + fsync + os.replace. Abrir el archivo final con 'w'
        lo truncaba ANTES de escribir, y un corte de luz en ese momento dejaba un
        identidad.json vacío o lleno de ceros (NTFS guarda el tamaño nuevo antes que los
        datos). Con os.replace el archivo es el viejo o el nuevo, nunca uno a medias."""
        temporal = f'{ARCHIVO_IDENTIDAD}.tmp'
        with open(temporal, 'w', encoding='utf-8') as f:
            json.dump(self.identidad, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temporal, ARCHIVO_IDENTIDAD)

    def _token(self) -> str:
        return self.identidad.get('token', '')

    def _firma_valida(self, tipo_mensaje: str, payload: dict, *, verificar_ventana: bool = True, **campos) -> bool:
        """Valida firma HMAC + ventana de timestamp de un mensaje entrante (comando,
        ejecutar_script, despliegue o software) — ver VENTANA_TIMESTAMP_SEGUNDOS.
        `campos` debe pasarse en el mismo orden que el servidor los firmó. Cuando
        `campos` incluye 'estacion' (comandos dirigidos a una sola estación, no los
        mensajes de despliegue/software que son legítimamente para varias), además
        exige que coincida con este agente — defensa en profundidad contra un mensaje
        capturado de OTRA estación y reenviado a este tópico (p.ej. vía la credencial
        MQTT compartida que todavía no se angostó por ACL, ver PLAN_MODERNIZACION.md).

        `verificar_ventana=False` es solo para actualizar_agente vía el tópico
        retenido: ese mensaje se firma UNA vez y puede quedar guardado en el broker
        horas o días hasta que la estación se prenda y se reconecte -- exigirle los
        mismos 120s que a un comando en vivo lo descartaría siempre. El SHA-256 del
        ejecutable sigue siendo la garantía de integridad real, independiente de esto."""
        # Se aceptan dos secretos a propósito, y el orden importa poco porque se
        # prueban ambos: el propio de la estación (que llega en el enrolamiento) y el
        # compartido del config.txt. Durante la migración conviven — el servidor firma
        # con el compartido hasta que la estación reporta una versión que entiende el
        # propio, y los tópicos de difusión (despliegue/software a grupo o cadena) van
        # a seguir firmados con el compartido, porque un mismo payload lo verifican
        # muchas estaciones. Probar los dos cuesta un HMAC de más y evita que una
        # estación quede incomunicada a mitad del rollout.
        firma_recibida = payload.get('firma', '')
        secretos = [s for s in (self.identidad.get('hmac_secret'), self.args.hmac_secret) if s]
        if not any(hmac.compare_digest(firmar(s, **campos), firma_recibida) for s in secretos):
            logging.error('Firma HMAC inválida en %s — se ignora (posible suplantación).', tipo_mensaje)
            return False
        if 'estacion' in campos and campos['estacion'] != self.args.codigo:
            logging.error(
                '%s destinado a otra estación (%s) — se ignora (posible reenvío cruzado).',
                tipo_mensaje, campos['estacion'],
            )
            return False
        if verificar_ventana and not timestamp_en_ventana(payload.get('timestamp')):
            logging.error(
                '%s con timestamp fuera de ventana (posible mensaje reenviado/capturado) — se ignora.',
                tipo_mensaje,
            )
            # Único punto del que puede salir la autocorrección del reloj, y no es
            # casualidad que sea el ÚLTIMO de los tres chequeos: llegar hasta acá
            # significa que la firma HMAC ya se validó y que el mensaje venía dirigido a
            # ESTA estación. O sea que la causa del rechazo solo puede ser la ventana —
            # nunca una firma inválida ni un mensaje cruzado, que cortan antes.
            self._reloj_fuera_de_ventana(payload.get('timestamp'))
            return False
        return True

    # --- autocorrección del reloj ---
    #
    # Rompe un círculo vicioso: pasados los VENTANA_TIMESTAMP_SEGUNDOS la estación
    # descarta TODO mensaje firmado, incluido el script que le arreglaría la hora. Hasta
    # ahora la única salida era entrar por MeshCentral a mano y correr `net time`, lo que
    # a ~1.800 equipos no es una salida.
    #
    # Por qué `net time` y no `w32tm`: medido sobre MAM06-A el 22-sep-2026, el DC
    # (10.0.0.7) NO responde NTP — `w32tm /stripchart` devuelve 0x800705B4 (timeout) — y
    # `w32tm /query /source` dice "Free-running System Clock", o sea que la estación no
    # sincroniza con nada y su reloj deriva libre. `net time` contra el mismo DC sí
    # funciona (exit 0, devuelve la hora) porque va por SMB. Es el mismo camino que ya
    # usa instalar-servicio.ps1 como respaldo.
    #
    # Corre como LocalSystem (`whoami` = nt authority\system, comprobado en la misma
    # medición) y el equipo está unido al dominio, así que `net time /set` tiene el
    # privilegio que necesita sin elevación extra.
    def _servidor_de_hora(self) -> str:
        """Contra quién sincronizar: lo del config, o el dominio del propio equipo.

        El respaldo existe porque el config NO alcanza, y eso se descubrió con el
        mecanismo ya desplegado: `servidor_hora` se agregó al `config.json` en 0.30, pero
        actualizar el agente **no reescribe la configuración**. Las ~39 estaciones ya
        instaladas quedaron con un config sin el campo, así que la autocorrección no
        podía dispararse en ninguna de ellas — justo en las que hacía falta. Comprobado
        en MAM06-A el 22-sep-2026: `servidor_hora = AUSENTE`.

        `Win32_ComputerSystem.Domain` da el dominio al que la estación está unida, que es
        exactamente el servidor de hora correcto y se descubre localmente: no hay nada
        hardcodeado y cada unidad de negocio resuelve el suyo sin configurar nada. En un
        equipo fuera de dominio devuelve el grupo de trabajo (típicamente "WORKGROUP"),
        que no es un servidor — por eso solo se acepta si parece un dominio real.

        Se cachea: esto se consulta desde la ruta de rechazo de comandos y no tiene
        sentido lanzar un PowerShell cada vez.
        """
        if self.args.servidor_hora:
            return self.args.servidor_hora
        if getattr(self, '_dominio_cacheado', None) is not None:
            return self._dominio_cacheado

        self._dominio_cacheado = ''
        try:
            dominio = correr_powershell(
                '(Get-CimInstance Win32_ComputerSystem).Domain', timeout=20,
            ).strip()
        except Exception:
            logging.exception('No se pudo averiguar el dominio del equipo.')
            return ''
        # Un dominio real tiene punto; "WORKGROUP" y los grupos de trabajo no.
        if '.' in dominio:
            self._dominio_cacheado = dominio
            logging.info('Sin ServidorHora en la config: uso el dominio del equipo (%s).', dominio)
        else:
            logging.warning(
                'Sin ServidorHora en la config y el equipo no esta en un dominio (%r): '
                'no puedo corregir el reloj solo.', dominio,
            )
        return self._dominio_cacheado

    def _reloj_fuera_de_ventana(self, timestamp_servidor):
        """Dispara la corrección en un hilo aparte. Nunca bloquea a quien la llama.

        Importa: esto lo invoca `_firma_valida`, que corre en el hilo de red de paho.
        Bloquearlo impediría que salgan los PINGREQ del keepalive y el broker cerraría
        la conexión — el mismo motivo por el que `_manejar_despliegue` despacha a un
        hilo (ver `_on_message`).
        """
        if not self._servidor_de_hora():
            logging.warning(
                'El reloj parece desfasado y no hay contra que sincronizarlo (ni '
                'ServidorHora en la config ni dominio): hay que arreglarlo a mano.',
            )
            return
        threading.Thread(
            target=self._corregir_reloj, args=(timestamp_servidor,), daemon=True,
        ).start()

    def _corregir_reloj(self, timestamp_servidor):
        ahora = time.time()
        ultimo = self.identidad.get('ultimo_intento_reloj', 0)
        if ahora - ultimo < COOLDOWN_CORRECCION_RELOJ_SEGUNDOS:
            faltan = int(COOLDOWN_CORRECCION_RELOJ_SEGUNDOS - (ahora - ultimo))
            logging.info('Reloj desfasado, pero ya intenté corregirlo hace poco (faltan %ss).', faltan)
            return

        # Se marca ANTES de intentar, no después: si `net time` cuelga hasta el timeout o
        # el proceso muere en el medio, el cooldown igual corre. Marcarlo al final dejaría
        # la puerta abierta a reintentar en bucle justo en el caso que el cooldown
        # existe para cubrir.
        self.identidad['ultimo_intento_reloj'] = ahora
        self._guardar_identidad()

        try:
            desfase = ahora - float(timestamp_servidor)
            detalle_desfase = f'{desfase:+.0f}s respecto del servidor'
        except (TypeError, ValueError):
            desfase = None
            detalle_desfase = 'desfase no calculable (timestamp ilegible)'

        servidor = self._servidor_de_hora()
        logging.warning(
            'Reloj fuera de la ventana de %ss (%s). Intento corregirlo contra %s.',
            VENTANA_TIMESTAMP_SEGUNDOS, detalle_desfase, servidor,
        )
        try:
            proc = subprocess.run(
                ['net', 'time', f'\\\\{servidor}', '/set', '/y'],
                capture_output=True, text=True, timeout=60,
                encoding='oem', errors='replace',
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
            )
        except Exception:
            logging.exception('No se pudo ejecutar `net time` contra %s.', servidor)
            return

        salida = ' '.join((proc.stdout or '').split())[:300]
        if proc.returncode == 0:
            self.identidad['autocorrecciones_reloj'] = self.identidad.get('autocorrecciones_reloj', 0) + 1
            self.identidad['ultima_autocorreccion_reloj'] = datetime.now().isoformat(timespec='seconds')
            self._guardar_identidad()
            logging.warning(
                'Reloj corregido contra %s. Van %s autocorrecciones. Hora ahora: %s. %s',
                servidor, self.identidad['autocorrecciones_reloj'],
                datetime.now().isoformat(timespec='seconds'), salida,
            )
        else:
            error = ' '.join((proc.stderr or '').split())[:300]
            logging.error(
                '`net time` falló contra %s (codigo %s). salida=%r error=%r. '
                'La estación sigue sorda a los comandos hasta que alguien le arregle la hora.',
                servidor, proc.returncode, salida, error,
            )

    # --- ciclo de conexión ---
    def _on_connect(self, client, userdata, flags, reason_code, properties=None):
        # paho-mqtt 2.x no se traga las excepciones de los callbacks: una sola mata el hilo
        # de red sin dejar rastro en el log del servicio, y la estación queda conectada,
        # sorda y sin enrolarse. Se registra con traceback para que el próximo fallo se vea.
        try:
            self._al_conectar(client, reason_code)
        except Exception:
            logging.exception('Error atendiendo la conexión al broker')

    def _al_conectar(self, client, reason_code):
        if reason_code != 0:
            logging.error('Falló la conexión al broker: %s', reason_code)
            return
        logging.info('Conectado al broker %s:%s', self.args.host, self.args.puerto)
        client.subscribe(f'/saidsof/enrolamiento/respuesta/{self.args.codigo}/')
        client.subscribe(f'/saidsof/agente/{self.args.codigo}/comando/')
        # Tópico propio y retenido (a diferencia de /comando/, que es fire-and-forget a
        # propósito): si la estación está apagada cuando se publica, EMQX guarda el
        # último mensaje y se lo entrega apenas se reconecta -- sin esto, una estación
        # con un agente muy viejo (sin este comando) que quedó apagada durante el
        # rollout nunca se entera de la actualización pendiente. El servidor limpia el
        # retenido (payload vacío) en cuanto ve el heartbeat con la versión ya aplicada
        # (ver apps.mqtt_worker.services.manejar_heartbeat).
        client.subscribe(f'/saidsof/agente/{self.args.codigo}/actualizar_agente/')
        # Freno de emergencia. Retenido como el de arriba: una estacion que estaba
        # apagada cuando se freno la flota recibe la orden apenas enciende, que es
        # exactamente el caso que un freno tiene que cubrir.
        client.subscribe(f'/saidsof/agente/{self.args.codigo}/pausa/')
        client.subscribe(f'/saidsof/agente/{self.args.codigo}/software/')
        client.subscribe('/saidsof/software/global/')
        # Catalogo de que servicios del POS chequear. Retenido y global: se recibe apenas
        # se conecta, aunque el servidor lo haya publicado hace dias con esta estacion
        # apagada. Ver apps/monitoreo/servicios_pos.py.
        client.subscribe('/saidsof/catalogo/servicios_pos/')
        client.subscribe('/saidsof/catalogo/eventos_sistema/')
        client.subscribe(f'/saidsof/agente/{self.args.codigo}/despliegue/')
        client.subscribe('/saidsof/despliegue/global/')
        if self.identidad.get('farmacia'):
            client.subscribe(f"/saidsof/software/farmacia/{self.identidad['farmacia']}/")
            client.subscribe(f"/saidsof/despliegue/farmacia/{self.identidad['farmacia']}/")
        if self.identidad.get('grupo'):
            client.subscribe(f"/saidsof/software/grupo/{self.identidad['grupo']}/")
            client.subscribe(f"/saidsof/despliegue/grupo/{self.identidad['grupo']}/")

        if not self._token():
            self._enrolar()
        elif not self.identidad.get('hmac_secret'):
            # Ya enrolada pero SIN secreto propio: es una estación que se enroló antes de
            # que el mecanismo existiera y despues solo se le actualizo el ejecutable.
            # Actualizar no re-enrola, asi que nunca lo recibio — y el servidor, viendo la
            # version nueva, podia firmarle con un secreto que ella no tiene. El 16-sep-2026
            # eso dejo mudas a dos estaciones del piloto.
            #
            # Re-enrolarse es seguro y es la unica forma de pedirlo: el servidor exige que
            # el hardware_id coincida con el de la primera vez, asi que devuelve la misma
            # identidad mas el secreto que faltaba.
            logging.info('Tengo identidad pero no mi secreto propio — me re-enrolo para pedirlo.')
            self._enrolar()
        else:
            logging.info('Ya tengo identidad guardada (token existente) — no vuelvo a enrolarme.')

    def _on_disconnect(self, client, userdata, flags, reason_code, properties=None):
        logging.warning('Desconectado del broker (%s), paho reintentará solo.', reason_code)

    def _enrolar(self):
        numero_serie = leer_numero_serie()
        self.identidad['numero_serie'] = numero_serie
        payload = {
            'codigo': self.args.codigo,
            # El orden importa: lo ya fijado manda sobre todo, para que el re-enrolamiento
            # presente la MISMA identidad que la primera vez. Después el valor explícito de
            # `--hardware-id` (una máquina sin registro de Windows) y recién después el
            # registro. Si ninguno responde queda vacío, y eso es a propósito (`A-6`, ver
            # `leer_machine_guid`): mejor sin identidad que con una compartida.
            'hardware_id': (
                self.identidad.get('hardware_id') or self.args.hardware_id or leer_machine_guid()
            ),
            'hostname': socket.gethostname(),
            'numero_serie': numero_serie,
            'so_nombre': f'Windows {platform.win32_ver()[0]}',
            'so_build': platform.win32_ver()[1],
            'version_agente': VERSION_AGENTE_PRUEBA,
        }
        self.identidad['hardware_id'] = payload['hardware_id']
        self._guardar_identidad()
        # El token de apertura (apps.aperturas) solo viaja en el primer enrolamiento: es de
        # un solo uso, así que reenviarlo en un re-enrolamiento no sirve de nada. No se
        # guarda en identidad.json — el servidor ya lo marcó como usado y la estación quedó
        # aprobada, no hay nada que reusar.
        if self.args.token_apertura:
            payload['token_apertura'] = self.args.token_apertura
        self.client.publish('/saidsof/enrolamiento/solicitar/', json.dumps(payload))
        # El log local NUNCA lleva el token en claro: este archivo queda en la estación, y
        # el token es lo único que se interpone entre la red y una estación auto-aprobada.
        logging.info(
            'Enrolamiento solicitado: %s',
            {**payload, 'token_apertura': '<oculto>'} if 'token_apertura' in payload else payload,
        )

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
            elif msg.topic == '/saidsof/catalogo/eventos_sistema/':
                self._aplicar_catalogo_eventos(payload)
            elif msg.topic == '/saidsof/catalogo/servicios_pos/':
                self._aplicar_catalogo_servicios(payload)
            elif msg.topic == f'/saidsof/agente/{self.args.codigo}/pausa/':
                # verificar_ventana=False por el mismo motivo que actualizar_agente: va
                # retenido y puede llegar dias despues, cuando la estacion encienda. Esa
                # es justamente la razon de ser del freno — si solo valiera dentro de la
                # ventana de 120 s, las estaciones apagadas durante la emergencia
                # arrancarian sin frenar, que es el caso que hay que cubrir.
                self._verificar_y_aplicar_pausa(payload)
            elif msg.topic == f'/saidsof/agente/{self.args.codigo}/actualizar_agente/':
                # verificar_ventana=False: este tópico es retenido, puede llegar horas
                # o días después de publicado (ver _on_connect) -- el SHA-256 sigue
                # siendo la garantía de integridad real.
                self._verificar_y_actualizar_agente(payload, verificar_ventana=False)
            # software y despliegue van en un hilo aparte, NO inline: _on_message corre
            # en el hilo de red de paho, y estos dos descargan archivos grandes (un
            # instalador o un paquete de POS pueden tardar minutos). Bloquear ese hilo
            # impide que salgan los PINGREQ del keepalive (60s) y el broker termina
            # cerrando la conexión: el agente se queda "sordo" y sus reportes se pierden
            # en silencio. Encontrado en MAM06-A (26-ago-2026): LibreOffice tardó 5 min
            # en bajar, la estación salió de línea a mitad de camino y ni un solo paso
            # (recibido/descargado/instalando) llegó al panel, que quedó en "pendiente".
            # Mismo patrón que ya usan consultar_perifericos / consultar_software_instalado
            # / escanear_actualizaciones / actualizar_agente.
            elif msg.topic.startswith('/saidsof/software/') or msg.topic.endswith('/software/'):
                self._en_hilo(self._manejar_software, payload)
            elif msg.topic.startswith('/saidsof/despliegue/') or msg.topic.endswith('/despliegue/'):
                self._en_hilo(self._manejar_despliegue, payload)
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
        # Secreto HMAC propio de esta estación. Reemplaza al COMANDO_HMAC_SECRET
        # compartido de la flota, que hasta ahora había que tipear a mano en el
        # config.txt de cada equipo — y que, por ser el mismo para las 700 farmacias,
        # obligaba a que el instalador NO pudiera publicarse completo (§10-Z).
        # Se guarda, pero el compartido se sigue aceptando: ver _firma_valida.
        hmac_propio = payload.get('hmac_secret')
        if hmac_propio:
            self.identidad['hmac_secret'] = hmac_propio

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

    def _ya_procesado(self, clave: str, identificador, timestamp) -> bool:
        """True si este agente ya atendió ESTA publicación de `identificador` para `clave`
        ("instalacion" / "despliegue"). Solo consulta: la marca la pone
        `_marcar_procesado`, y únicamente después de validar la firma.

        Hace falta porque software y despliegue se publican RETENIDOS (ver
        apps.software.services / apps.despliegues.services) y nadie los borra del
        broker: sin esto, cada reconexión del agente vuelve a recibir el mismo mensaje
        y reinstalaría el software una y otra vez. La ventana de timestamp "tapaba"
        esto rechazándolos, pero a costa de que una estación que estaba apagada al
        publicar nunca recibiera la instalación (que es justo el motivo de retenerlos).

        La identidad de una publicación es (id, timestamp firmado), no el id solo.
        Hasta la 0.34 era el id solo, y eso dejaba muerto el "Reanudar" del panel:
        `reintentar_despliegue` reenvía el MISMO despliegue_id (con timestamp nuevo) y el
        agente lo descartaba como "ya procesado", así que la estación que había fallado
        quedaba en PENDIENTE para siempre (auditoría 17-oct-2026, ARQ-03/BH-02). Ahora el
        retenido repetido (mismo timestamp) se sigue ignorando y el reintento (timestamp
        mayor) se atiende.
        """
        if identificador is None:
            return False
        marcas = self.identidad.get(f'{clave}_procesados_ts', {})
        clave_id = str(identificador)
        if clave_id not in marcas:
            return False
        try:
            return int(timestamp) <= marcas[clave_id]
        except (TypeError, ValueError):
            # Sin timestamp legible no hay forma de saber si es una publicación nueva:
            # se trata como repetida, que es lo que hacía la 0.34 con cualquier id visto.
            return True

    def _procesado_antes_de_actualizar(self, clave: str, identificador) -> bool:
        """True si `identificador` figura en la lista de la 0.34 o anterior, que guardaba
        solo el id (sin timestamp).

        Esos ids se atendieron con una publicación cuyo timestamp no quedó guardado. El
        retenido que llega ahora puede ser esa misma publicación, y reaplicarla cerraría
        y reinstalaría el POS en una estación donde ya se aplicó. Por eso el primer
        mensaje que llega para un id heredado se toma como "ya visto" y su timestamp pasa
        a ser la marca: desde ahí, solo un reintento posterior se atiende. No se usa el
        reloj local para decidirlo porque en las farmacias no es confiable (ver
        timestamp_en_ventana).
        """
        if identificador is None:
            return False
        if str(identificador) in self.identidad.get(f'{clave}_procesados_ts', {}):
            # Ya tiene marca con timestamp: de acá en adelante manda `_ya_procesado`.
            return False
        return identificador in self.identidad.get(f'{clave}_procesados', [])

    def _marcar_procesado(self, clave: str, identificador, timestamp) -> None:
        """Registra que se atendió la publicación (`identificador`, `timestamp`). Se llama
        solo con la firma ya validada: si se marcara antes, un mensaje con firma inválida
        y un id predecible inutilizaría el legítimo que llegue después (auditoría
        17-oct-2026, BH-03)."""
        if identificador is None:
            return
        try:
            timestamp = int(timestamp)
        except (TypeError, ValueError):
            timestamp = 0
        marcas = self.identidad.setdefault(f'{clave}_procesados_ts', {})
        clave_id = str(identificador)
        # Se reinserta para que quede al final: el orden del dict es el de "más reciente",
        # y el recorte de abajo descarta los más viejos.
        marcas.pop(clave_id, None)
        marcas[clave_id] = timestamp
        # Se acota para que identidad.json no crezca sin límite.
        for vieja in list(marcas)[:-200]:
            del marcas[vieja]
        self._guardar_identidad()

    def _en_hilo(self, funcion, payload):
        """Corre `funcion(payload)` en un hilo daemon, logueando cualquier excepción.
        El try/except general de _on_message solo cubre lo que corre inline; una
        excepción dentro de un hilo moriría sin dejar rastro."""
        def _correr():
            try:
                funcion(payload)
            except Exception:
                logging.exception('Error procesando %s en segundo plano', funcion.__name__)

        threading.Thread(target=_correr, daemon=True).start()

    def _publicar(self, topico, payload, qos=0):
        """Publica y AVISA si el broker no lo recibió. paho no lanza cuando está
        desconectado: publish() devuelve rc=MQTT_ERR_NO_CONN y descarta el mensaje
        (QoS 0), así que sin este chequeo el agente seguía logueando "Heartbeat
        enviado"/"Instalación #N -> descargado" mientras en realidad no llegaba nada
        -- el log mentía y el panel se quedaba en "pendiente" sin ninguna pista.
        Encontrado en MAM06-A (26-ago-2026) instalando LibreOffice.

        Con `qos=1` (0.38, los reportes de instalaciones, despliegues y scripts) paho NO
        lo descarta: lo guarda y lo envía al reconectar, aunque devuelve el mismo
        MQTT_ERR_NO_CONN. Comprobado contra EMQX 5.8.3 el 22-oct-2026 (publicado antes de
        conectar y durante un corte forzado por el broker: los dos llegaron; un QoS 0 en el
        mismo corte, no). La cola vive en memoria: un reinicio del agente antes de
        reconectar la pierde."""
        info = self.client.publish(topico, json.dumps(payload), qos=qos)
        if qos > 0 and info.rc == mqtt.MQTT_ERR_NO_CONN:
            logging.warning('Sin conexión al broker: el mensaje para %s queda en cola y sale al reconectar.', topico)
            return True
        if info.rc != mqtt.MQTT_ERR_SUCCESS:
            logging.error(
                'No se pudo publicar en %s (rc=%s, ¿sin conexión al broker?) — el mensaje se perdió.',
                topico, info.rc,
            )
            return False
        return True

    def _verificar_y_configurar_nodo_pos(self, payload):
        if not self._firma_valida(
            'comando configurar_nodo_pos', payload,
            comando='configurar_nodo_pos', servidor=payload.get('servidor'), bdd=payload.get('bdd'),
            puerto=payload.get('puerto'), password=payload.get('password'),
            estacion=payload.get('estacion'), timestamp=payload.get('timestamp'),
        ):
            return
        # En un hilo: puede quedarse esperando horas a que cierre el POS.
        self._en_hilo(self._aplicar_nodo_pos, payload)

    def _aplicar_nodo_pos(self, payload):
        """Reapunta el POS a otro nodo reescribiendo Servidor/Bdd/Puerto en su .Config.

        Espera a que el POS se cierre antes de tocar el archivo: así el cambio nunca
        cae en medio de una venta, y el POS toma la config nueva en su siguiente
        arranque (.NET lee el .Config al iniciar, no en caliente).

        Reemplazo QUIRÚRGICO por texto, no reescribiendo el XML con ElementTree: el
        .Config tiene muchas otras claves, comentarios y un formato que el POS y quien
        lo administra esperan encontrar intactos. Solo se cambia el atributo `value` de
        las tres claves, y solo si ya existen -- si alguna falta, se aborta sin tocar
        nada en vez de inventarla.
        """
        servidor = (payload.get('servidor') or '').strip()
        bdd = (payload.get('bdd') or '').strip()
        puerto = (payload.get('puerto') or '').strip()
        # La contraseña NO se .strip(): un espacio al principio o al final puede ser
        # parte de la contraseña real, y recortarlo dejaría al POS sin poder conectar.
        password = payload.get('password') or ''
        if not (servidor and bdd and puerto and password):
            # Nunca se loguea el valor de la contraseña, solo si vino o no.
            logging.error(
                'configurar_nodo_pos con datos incompletos (servidor=%s bdd=%s puerto=%s password=%s) — se ignora.',
                servidor or '(vacío)', bdd or '(vacío)', puerto or '(vacío)',
                '(presente)' if password else '(vacía)',
            )
            return

        ruta = f'{self.args.pos_comando_iniciar}.Config'
        if not os.path.exists(ruta):
            logging.error('No existe el .Config del POS en %s — no se puede reapuntar el nodo.', ruta)
            return

        if self._pos_corriendo():
            logging.info('Nodo del POS -> %s: el POS está abierto, esperando a que se cierre para aplicar...', bdd)
            self._esperar_cierre_pos()

        try:
            with open(ruta, 'r', encoding='utf-8-sig') as f:
                contenido = f.read()

            nuevos = {'Servidor': servidor, 'Bdd': bdd, 'Puerto': puerto, 'Contrasena': password}
            actualizado = contenido
            for clave, valor in nuevos.items():
                # Escapado XML del valor: una contraseña puede traer &, < o " y sin
                # esto dejaría el .Config mal formado — el POS no abriría. (El lambda
                # del subn, además, evita que un "\" del valor se interprete como
                # referencia de grupo en el reemplazo.)
                valor_xml = valor.replace('&', '&amp;').replace('<', '&lt;').replace('"', '&quot;')
                patron = re.compile(
                    r'(<add\s+key\s*=\s*"' + re.escape(clave) + r'"\s+value\s*=\s*")([^"]*)(")',
                    re.IGNORECASE,
                )
                actualizado, reemplazos = patron.subn(lambda m: m.group(1) + valor_xml + m.group(3), actualizado)
                if reemplazos == 0:
                    logging.error(
                        'El .Config del POS no tiene la clave "%s" — se aborta el cambio de nodo sin tocar nada.',
                        clave,
                    )
                    return

            shutil.copy2(ruta, ruta + '.bak')
            with open(ruta, 'w', encoding='utf-8') as f:
                f.write(actualizado)
        except Exception:
            logging.exception('No se pudo reapuntar el POS al nodo %s', bdd)
            return

        # Invalida el caché para que el próximo heartbeat reporte el nodo nuevo — es
        # la confirmación que ve el panel.
        self._cache_config_pos = None
        logging.info(
            'Nodo del POS aplicado: %s @ %s:%s (respaldo en %s.bak). Toma efecto al abrir el POS.',
            bdd, servidor, puerto, ruta,
        )

    def _version_pos_reportable(self) -> dict:
        """`{'version_pos': '3.0.2.28'}` leído del propio ejecutable del POS, o `{}`.

        Hasta 0.19 el agente mandaba la cadena fija 'N/A (agente de prueba)'. Eso dejaba
        sin sentido a toda una cadena que ya existía completa: `Estacion.version_pos`,
        `Grupo.version_objetivo`, la property `Estacion.desactualizada` y el filtro "Solo
        desactualizadas" del panel comparaban un texto que nunca cambiaba. La maquinaria
        estaba entera y no medía nada — ML027-ADM figuraba "desactualizada" solo porque
        'N/A (agente de prueba)' no es igual a '3.0.2.28'.

        Devuelve un dict y no una cadena para poder NO mandar la clave cuando no se pudo
        leer: `manejar_heartbeat` usa `payload.get('version_pos', <valor actual>)`, así
        que mandar '' borraría lo que ya se sabía. Mismo criterio que `_leer_config_pos`.

        Se cachea por mtime, también como `_leer_config_pos`: esto viaja en cada latido y
        el ejecutable solo cambia cuando se actualiza el POS.
        """
        ruta = self.args.pos_comando_iniciar
        try:
            mtime = os.path.getmtime(ruta)
        except OSError:
            return {}

        cacheado = getattr(self, '_cache_version_pos', None)
        if cacheado and cacheado[0] == mtime:
            return cacheado[1]

        try:
            import win32api

            info = win32api.GetFileVersionInfo(ruta, '\\')
            ms, ls = info['FileVersionMS'], info['FileVersionLS']
            version = '%d.%d.%d.%d' % (ms >> 16, ms & 0xFFFF, ls >> 16, ls & 0xFFFF)
            resultado = {'version_pos': version}
        except Exception:
            logging.exception('No se pudo leer la versión del POS en %s', ruta)
            resultado = {}

        self._cache_version_pos = (mtime, resultado)
        return resultado

    def _leer_config_pos(self) -> dict:
        """Servidor/Bdd/Puerto del .Config del POS -- a qué nodo apunta REALMENTE esta
        estación, leído del propio equipo en vez de una planilla.

        El archivo es el `<ejecutable>.Config` de la convención .NET (ej.
        "Zabyca.Pos.Desktop.exe.Config"), así que se deriva de --pos-comando-iniciar:
        si el POS se instala en otra ruta o con otro nombre, sigue funcionando sin
        tocar nada acá.

        Se cachea por mtime porque esto se manda en CADA heartbeat (cada 60s): sin el
        caché se estaría parseando el XML todo el tiempo para un dato que casi nunca
        cambia.

        Nunca lanza: si el POS no está instalado, el archivo no existe o el XML está
        roto, devuelve {} y el panel simplemente no muestra el dato.
        """
        ruta = f'{self.args.pos_comando_iniciar}.Config'
        try:
            mtime = os.path.getmtime(ruta)
        except OSError:
            return {}

        cacheado = getattr(self, '_cache_config_pos', None)
        if cacheado and cacheado[0] == mtime:
            return cacheado[1]

        try:
            import xml.etree.ElementTree as ET
            raiz = ET.parse(ruta).getroot()
            ajustes = {
                (el.get('key') or '').strip().lower(): (el.get('value') or '').strip()
                for el in raiz.iter('add')
            }
        except Exception:
            logging.exception('No se pudo leer la configuración del POS en %s', ruta)
            return {}

        datos = {
            'pos_servidor': ajustes.get('servidor', ''),
            'pos_bdd': ajustes.get('bdd', ''),
            'pos_puerto': ajustes.get('puerto', ''),
        }
        self._cache_config_pos = (mtime, datos)
        logging.info('Configuración del POS leída: %s', datos)
        return datos

    # --- servicios externos de los que depende el POS ---
    #
    # Cuando una caja no puede vender, la pregunta es donde se corta la cadena: la base
    # local, el nodo central, Odoo o el web service de recargas. Averiguarlo exigia entrar
    # al equipo. Esto lo pregunta desde adentro cada pocos minutos.
    #
    # Se chequea desde la ESTACION y no desde el servidor central a proposito: lo que
    # importa no es si el servicio esta vivo en abstracto, sino si ESTA caja lo alcanza.
    # Una base central sana con la ruta rota desde una farmacia es, para esa farmacia, una
    # base caida — y desde el servidor se veria perfecta.

    def _leer_servicios_pos(self) -> list:
        """Los servicios a chequear, leidos del .exe.Config real del POS.

        Hermano de _leer_config_pos y cacheado por mtime igual que el: el archivo no
        cambia entre chequeos y parsear XML cada cinco minutos seria trabajo por nada.

        La contrasena viaja SOLO dentro de este proceso, para poder conectarse; nunca sale
        en el reporte que se publica.
        """
        ruta = f'{self.args.pos_comando_iniciar}.Config'
        try:
            mtime = os.path.getmtime(ruta)
        except OSError:
            return []

        cacheado = getattr(self, '_cache_servicios_pos', None)
        if cacheado and cacheado[0] == mtime:
            return cacheado[1]

        try:
            import xml.etree.ElementTree as ET
            raiz = ET.parse(ruta).getroot()
        except Exception:
            logging.exception('No se pudo leer la configuracion del POS en %s', ruta)
            return []

        ajustes = {
            (el.get('key') or '').strip().lower(): (el.get('value') or '').strip()
            for el in raiz.iter('add')
        }

        servicios = []
        # Los dos Postgres: el central usa las mismas claves con sufijo "Central".
        for nombre, sufijo in (('pg_local', ''), ('pg_central', 'central')):
            host = ajustes.get('servidor' + sufijo, '')
            bdd = ajustes.get('bdd' + sufijo, '')
            if not host or not bdd:
                continue
            servicios.append({
                'servicio': nombre,
                'tipo': 'postgres',
                'host': host,
                'puerto': ajustes.get('puerto' + sufijo, '5432'),
                'bdd': bdd,
                'usuario': ajustes.get('usuario' + sufijo, ''),
                'clave': ajustes.get('contrasena' + sufijo, ''),
                # Sin la base local la caja no vende; sin la central sigue vendiendo y
                # sincroniza despues. Esa diferencia decide si la alerta es critica.
                'critico': nombre == 'pg_local',
            })

        url_odoo = ajustes.get('odooserverurl', '')
        if url_odoo:
            servicios.append({
                'servicio': 'odoo', 'tipo': 'http', 'url': url_odoo, 'critico': False,
            })

        # El de recargas vive en applicationSettings, con otra forma: un setting con un
        # value adentro. Se busca por coincidencia y no por nombre exacto porque el nombre
        # completo incluye el namespace de .NET y cambia entre versiones del POS.
        for setting in raiz.iter('setting'):
            if 'recargas' not in (setting.get('name') or '').lower():
                continue
            valor = setting.find('value')
            url = (valor.text or '').strip() if valor is not None else ''
            if url:
                servicios.append({
                    'servicio': 'recargas_soap', 'tipo': 'http', 'url': url, 'critico': False,
                })
            break

        self._cache_servicios_pos = (mtime, servicios)
        logging.info(
            'Servicios del POS detectados: %s',
            ', '.join('%s -> %s' % (s['servicio'], s.get('url') or s.get('host')) for s in servicios),
        )
        return servicios

    # --- Eventos del visor de Windows ---
    #
    # Que eventos mirar lo manda el servidor por `/saidsof/catalogo/eventos_sistema/`, no
    # es una lista fija aca: descubrir que un ID importa no puede costar un rebuild y un
    # rollout a 1.800 equipos.
    #
    # Se pregunta SOLO por los ID del catalogo y no se trae el visor entero. Medido el
    # 20-sep-2026 sobre una maquina real: 260 eventos de Ntfs 55 en 30 dias, en UNA
    # maquina. Traer todo y filtrar del lado del servidor serian cientos de miles de
    # filas mensuales que nadie lee.

    def _catalogo_verificado(self, tipo_mensaje: str, comando: str, payload: dict):
        """Contenido del catalogo si la firma es valida; None si no lo es.

        Los dos catalogos (servicios del POS y eventos de Windows) eran los UNICOS
        mensajes dirigidos al agente que se aplicaban sin verificar firma, y lo que un
        catalogo decide es contra que host hace `urlopen`, `connect` o `ping` este agente
        —y el resultado vuelve publicado—. Quien alcanzara el topico tenia la red de la
        farmacia a mano desde afuera. Firmados desde el servidor 0.22 (RIESGO-06).

        Se verifica contra el string de la clave `catalogo` TAL COMO LLEGO, sin
        re-serializarlo: por eso el servidor manda el contenido serializado y no un
        objeto (ver `envolver_firmado` en apps/monitoreo/servicios_pos.py).

        Y se lee de ese string, no de las claves de primer nivel: vienen duplicadas para
        los agentes viejos, y si leyeramos de ahi alcanzaria con reenviar una firma
        legitima acompanada de unas claves cambiadas.

        `verificar_ventana=False` como en los otros retenidos: el mensaje puede estar
        guardado en el broker dias hasta que la estacion encienda.

        Firma Ed25519 (0.38): si el catalogo la trae y este agente tiene claves publicas,
        se verifica esa y la HMAC no se mira (las estaciones instaladas desde 0.21 no
        tienen el secreto compartido: H-03). Una vez aceptado un catalogo firmado asi,
        el agente lo recuerda (`catalogo_exige_ed25519` en identidad.json) y desde ahi
        rechaza los que vengan sin ella: si no, bastaria con quitar `firma_ed25519` para
        volver a la HMAC con el secreto compartido (S-11).
        """
        firma_ed25519 = payload.get('firma_ed25519')
        if firma_ed25519 and CLAVES_PUBLICAS_CATALOGO:
            mensaje = '|'.join(
                str(v) for v in (comando, payload.get('catalogo', ''), payload.get('timestamp'))
            ).encode('utf-8')
            if not firma_ed25519_valida(CLAVES_PUBLICAS_CATALOGO, mensaje, firma_ed25519):
                logging.error('Firma Ed25519 invalida en %s — se ignora (posible suplantacion).', tipo_mensaje)
                return None
            try:
                contenido = json.loads(payload['catalogo'])
            except (TypeError, json.JSONDecodeError):
                logging.error('%s con firma valida pero contenido ilegible — se ignora.', tipo_mensaje)
                return None
            if not self.identidad.get('catalogo_exige_ed25519'):
                self.identidad['catalogo_exige_ed25519'] = True
                self._guardar_identidad()
                logging.info('Primer catalogo con firma Ed25519: desde ahora se exige en los catalogos.')
            return contenido
        if self.identidad.get('catalogo_exige_ed25519'):
            logging.error(
                '%s sin firma Ed25519 valida para este agente, que ya la exige — se ignora.', tipo_mensaje,
            )
            return None
        if not payload.get('firma'):
            # Servidor anterior al catalogo firmado. Se acepta para no quedarse con la
            # lista vieja durante el despliegue, pero queda dicho en el log: es el unico
            # mensaje que este agente obedece sin verificar.
            logging.warning(
                '%s sin firma — se aplica por compatibilidad con el servidor anterior.', tipo_mensaje,
            )
            return payload
        if not self._firma_valida(
            tipo_mensaje, payload, verificar_ventana=False,
            comando=comando, catalogo=payload.get('catalogo', ''),
            timestamp=payload.get('timestamp'),
        ):
            return None
        try:
            return json.loads(payload['catalogo'])
        except (TypeError, json.JSONDecodeError):
            logging.error('%s con firma valida pero contenido ilegible — se ignora.', tipo_mensaje)
            return None

    def _aplicar_catalogo_eventos(self, payload: dict):
        """Guarda que eventos vigilar. Se persiste en `identidad.json` para que sobreviva
        a un reinicio: el mensaje es retenido y llegaria igual al reconectar, pero entre
        el arranque y esa reconexion habria un ciclo mirando la lista vieja."""
        contenido = self._catalogo_verificado(
            'catalogo de eventos de Windows', 'catalogo_eventos_sistema', payload,
        )
        if contenido is None:
            return
        eventos = contenido.get('eventos') or []
        self.identidad['catalogo_eventos_sistema'] = eventos
        self.identidad['catalogo_eventos_sistema_version'] = version_catalogo(payload)
        self._guardar_identidad()
        logging.info('Catalogo de eventos de Windows recibido: %d vigilado(s).', len(eventos))

    def _leer_eventos_sistema(self):
        """(eventos, marca_nueva). NO persiste la marca: la guarda el bucle, y solo si el
        publish salio.

        Es la misma disciplina que `_leer_errores_nuevos_pos` tras el hallazgo 2 de la
        auditoria: con QoS 0 paho descarta el mensaje sin avisar, y si la marca avanza
        igual esos eventos no se vuelven a leer NUNCA. Un monitoreo que pierde
        silenciosamente lo que vino a vigilar es peor que no tenerlo.

        La marca es el instante del ultimo barrido, no un numero de registro: el visor de
        Windows rota y purga por su cuenta, asi que un indice no sobrevive.
        """
        catalogo = self.identidad.get('catalogo_eventos_sistema') or []
        if not catalogo:
            return [], None

        desde = self.identidad.get('eventos_sistema_marca')
        if not desde:
            # Primer barrido: una hora hacia atras y no todo el historial. Un equipo con
            # meses de eventos mandaria miles de golpe en su primer reporte, y lo que
            # importa es lo que pasa de ahora en adelante.
            inicio = datetime.now() - timedelta(hours=1)
        else:
            try:
                inicio = datetime.fromisoformat(desde)
            except ValueError:
                inicio = datetime.now() - timedelta(hours=1)

        marca_nueva = datetime.now().isoformat(timespec='seconds')
        filtros = self._filtros_consulta_eventos(catalogo, inicio)
        if not filtros['fuentes']:
            # Habia catalogo pero ninguna entrada valida: no se consulta nada y la marca no
            # avanza, igual que cuando no se puede leer el visor.
            return [], None

        # La salida se fuerza a UTF-8 dentro del script. Sin esto PowerShell escribe en la
        # pagina de codigos del sistema (cp1252 en un Windows en espanol) y `json.loads`
        # revienta con el primer mensaje acentuado -- que son todos, porque el visor esta
        # en espanol. Comprobado el 20-sep-2026: "operacion de paginacion" rompia el parseo.
        #
        # Se agrupa por (log, proveedor, id) EN LA ESTACION: mandar 260 ocurrencias
        # identicas para que el servidor las cuente seria gastar red y base en algo que
        # aca sale gratis.
        try:
            resultado = subprocess.run(
                ['powershell', '-NoProfile', '-NonInteractive', '-EncodedCommand',
                 SCRIPT_CONSULTA_EVENTOS_CODIFICADO],
                input=json.dumps(filtros, ensure_ascii=True),
                capture_output=True, timeout=TIMEOUT_CONSULTA_EVENTOS_SEGUNDOS,
                text=True, encoding='utf-8', errors='replace',
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
            )
        except subprocess.TimeoutExpired:
            logging.error(
                'La consulta al visor de eventos supero %ss y se corto.', TIMEOUT_CONSULTA_EVENTOS_SEGUNDOS,
            )
            return [], None
        except Exception:
            logging.exception('No se pudo leer el visor de eventos de Windows')
            return [], None
        if resultado.returncode != 0:
            logging.error(
                'La consulta al visor de eventos termino con codigo %s: %s',
                resultado.returncode, (resultado.stderr or '').strip()[:300],
            )
            return [], None
        salida = resultado.stdout or ''
        if not salida.strip():
            # El script SIEMPRE imprime al menos `[]` (ConvertTo-Json de @($r)). Una salida
            # vacia con codigo 0 es un fallo, no "sin eventos": no se avanza la marca.
            logging.error('La consulta al visor de eventos termino sin salida; se reintenta en el proximo barrido.')
            return [], None

        try:
            eventos = json.loads(salida)
        except json.JSONDecodeError:
            logging.warning('El visor de eventos devolvio algo que no es JSON, se descarta.')
            return [], None

        motivo = self._motivo_resultado_eventos_invalido(eventos)
        if motivo:
            # Toda la lectura se descarta: publicar la parte "buena" y avanzar la marca
            # perderia para siempre la ventana que no se pudo leer bien.
            logging.error('El visor de eventos devolvio un resultado fuera de contrato (%s); se descarta.', motivo)
            return [], None

        for evento in eventos:
            evento['mensaje'] = (evento.get('mensaje') or '')[:500]
        return eventos, marca_nueva

    # Contrato de salida de SCRIPT_CONSULTA_EVENTOS, y lo que sus consumidores necesitan
    # (`_reportar_eventos_sistema` y `apps.monitoreo.services.registrar_eventos_sistema`):
    # una lista de objetos con exactamente estas claves.
    #   log      str, System o Application (el script no emite otro)
    #   id       int (no bool), 0..65535
    #   cantidad int (no bool), >= 1 (Group-Object nunca agrupa cero)
    #   origen   str o null (el servidor trata null como "sin origen")
    #   mensaje  str o null (null se normaliza a '' mas abajo, como siempre)
    CLAVES_RESULTADO_EVENTOS = frozenset({'log', 'id', 'cantidad', 'origen', 'mensaje'})

    @classmethod
    def _motivo_resultado_eventos_invalido(cls, eventos):
        """None si `eventos` cumple el contrato; si no, un motivo corto (sin datos del equipo)."""
        if not isinstance(eventos, list):
            return f'la raiz es {type(eventos).__name__}, no una lista'
        for posicion, evento in enumerate(eventos):
            if not isinstance(evento, dict):
                return f'el elemento {posicion} no es un objeto'
            if set(evento) != cls.CLAVES_RESULTADO_EVENTOS:
                return f'el elemento {posicion} no tiene exactamente las claves esperadas'
            if evento['log'] not in LOGS_EVENTOS_PERMITIDOS:
                return f'el elemento {posicion} tiene un log no permitido'
            if type(evento['id']) is not int or not 0 <= evento['id'] <= 65535:
                return f'el elemento {posicion} tiene un id invalido'
            if type(evento['cantidad']) is not int or evento['cantidad'] < 1:
                return f'el elemento {posicion} tiene una cantidad invalida'
            if evento['origen'] is not None and not isinstance(evento['origen'], str):
                return f'el elemento {posicion} tiene un origen invalido'
            if evento['mensaje'] is not None and not isinstance(evento['mensaje'], str):
                return f'el elemento {posicion} tiene un mensaje invalido'
        return None

    @staticmethod
    def _filtros_consulta_eventos(catalogo, inicio) -> dict:
        """Los filtros que viajan como DATOS (JSON por stdin) al script constante.

        Se agrupa por (log, PROVEEDOR): el ID solo no identifica un evento. Comprobado
        contra un visor real el 20-sep-2026 -- pidiendo solo `System 55` volvian 260
        avisos de energia del procesador, no corrupcion de NTFS. El ProviderName va dentro
        del propio FilterHashtable: filtrar en el visor es mucho mas barato que traer todo
        y descartar despues en PowerShell.

        Una entrada con forma invalida se descarta y queda contada en el log; no llega al
        proceso. Se conservan los valores por defecto de siempre: log `System` si falta y
        sin filtro de proveedor si viene vacio. El `id` tiene que ser un entero de verdad
        (no bool, no texto): el servidor siempre lo manda asi.
        """
        por_fuente = {}
        descartadas = 0
        for entrada in catalogo if isinstance(catalogo, list) else []:
            if not isinstance(entrada, dict):
                descartadas += 1
                continue
            log = entrada.get('log') or 'System'
            proveedor = entrada.get('proveedor') or ''
            identificador = entrada.get('id')
            valido = (
                log in LOGS_EVENTOS_PERMITIDOS
                and isinstance(proveedor, str)
                and (proveedor == '' or PATRON_PROVEEDOR_EVENTO.match(proveedor) is not None)
                and type(identificador) is int
                and 0 <= identificador <= 65535
            )
            if not valido:
                descartadas += 1
                continue
            por_fuente.setdefault((log, proveedor), []).append(identificador)
        if descartadas:
            logging.warning(
                'Catalogo de eventos: %d entrada(s) descartada(s) por formato invalido.', descartadas,
            )
        return {
            'inicio': inicio.isoformat(timespec='seconds'),
            'fuentes': [
                {'log': log, 'proveedor': proveedor, 'ids': ids}
                for (log, proveedor), ids in por_fuente.items()
            ],
        }

    def bucle_reloj(self):
        """Compara el reloj contra el servidor de hora por su cuenta, cada hora.

        La otra mitad de la autocorrección, y la que de verdad escala. El disparador por
        rechazo de comando solo sirve si ALGUIEN le manda algo a la estación: una caja
        que nadie está tocando se desfasa, queda sorda y no se entera. Medido el
        22-sep-2026: 6 de 39 estaciones (15%) estaban así, y aparecieron porque se las
        fue a buscar una por una. A ~1.800 equipos eso no es una forma de operar.

        **Jitter en el primer ciclo**, no en los siguientes: las estaciones arrancan
        juntas después de un rollout (las 39 aplicaron 0.31 en la misma media hora), y
        sin esto todas consultarían el DC en el mismo segundo cada hora, para siempre. Un
        desfase inicial al azar las reparte y se mantiene repartido. Es la primera vez que
        este agente usa jitter; el resto de los bucles todavía no lo tiene (pendiente #3
        del plan).
        """
        # El jitter va acá, una sola vez, y después el ciclo es fijo: lo que hay que
        # repartir es la fase, no el intervalo.
        time.sleep(random.uniform(0, INTERVALO_CHEQUEO_RELOJ_SEGUNDOS))
        while True:
            if not self._token() or self._pausado():
                time.sleep(INTERVALO_CHEQUEO_RELOJ_SEGUNDOS)
                continue
            try:
                self._chequear_reloj()
            except Exception:
                logging.exception('No se pudo chequear el reloj contra el servidor de hora')
            time.sleep(INTERVALO_CHEQUEO_RELOJ_SEGUNDOS)

    def _chequear_reloj(self):
        """Corre `net time /set` y mide cuánto saltó el reloj.

        No parsea la salida de `net time` a propósito: viene en el idioma y el formato
        regional de Windows ("La hora actual en \\\\dominio es 22/9/2026 16:06:22"), y
        parsear eso es frágil de un modo que solo se nota en la estación equivocada.

        El truco es comparar dos relojes: `time.time()` salta cuando alguien cambia la
        hora del sistema, `time.monotonic()` no. La diferencia entre lo que avanzó uno y
        lo que avanzó el otro ES el salto, sin importar idioma ni formato.
        """
        servidor = self._servidor_de_hora()
        if not servidor:
            return

        pared_antes, mono_antes = time.time(), time.monotonic()
        try:
            proc = subprocess.run(
                ['net', 'time', f'\\\\{servidor}', '/set', '/y'],
                capture_output=True, text=True, timeout=60,
                encoding='oem', errors='replace',
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
            )
        except Exception:
            logging.exception('No se pudo chequear el reloj contra %s.', servidor)
            return

        if proc.returncode != 0:
            logging.warning(
                'El chequeo horario del reloj contra %s fallo (codigo %s): %s',
                servidor, proc.returncode, ' '.join((proc.stderr or '').split())[:200],
            )
            return

        salto = (time.time() - pared_antes) - (time.monotonic() - mono_antes)
        if abs(salto) < SALTO_MINIMO_PARA_CONTAR_SEGUNDOS:
            logging.debug('Reloj chequeado contra %s, sin cambios (%+.1fs).', servidor, salto)
            return

        # Salto grande: esto no es deriva normal, es un equipo con un problema.
        self.identidad['autocorrecciones_reloj'] = self.identidad.get('autocorrecciones_reloj', 0) + 1
        self.identidad['ultima_autocorreccion_reloj'] = datetime.now().isoformat(timespec='seconds')
        self._guardar_identidad()
        logging.warning(
            'Reloj corregido en el chequeo horario: salto %+.0fs contra %s. '
            'Van %s autocorrecciones — si el numero sigue subiendo, revisar la pila de la placa.',
            salto, servidor, self.identidad['autocorrecciones_reloj'],
        )

    def bucle_eventos_sistema(self):
        """Calco de bucle_log_pos, con su propio intervalo."""
        while True:
            time.sleep(self.args.intervalo_eventos_sistema)
            if not self._token():
                continue
            if self._pausado():
                continue
            try:
                self._reportar_eventos_sistema()
            except Exception:
                logging.exception('No se pudieron reportar los eventos de Windows')

    def _reportar_eventos_sistema(self):
        eventos, marca_nueva = self._leer_eventos_sistema()
        if marca_nueva is None:
            # No se pudo leer. NO se avanza la marca: el proximo barrido reintenta desde
            # el mismo punto en vez de saltearse la ventana que no se pudo mirar.
            return
        if not eventos:
            self.identidad['eventos_sistema_marca'] = marca_nueva
            self._guardar_identidad()
            return

        enviado = self._publicar(f'/saidsof/agente/{self.args.codigo}/eventos_sistema/', {
            'token': self._token(), 'eventos': eventos,
        })
        if not enviado:
            # Mismo criterio que el log del POS: si no salio, la ventana se vuelve a leer.
            logging.warning('Los eventos de Windows no se pudieron publicar, se reintentan.')
            return
        self.identidad['eventos_sistema_marca'] = marca_nueva
        self._guardar_identidad()
        logging.info(
            'Eventos de Windows reportados: %s',
            ', '.join(f"{e['log']}/{e['id']} x{e['cantidad']}" for e in eventos),
        )

    # --- Catalogo de servicios, enviado por el servidor ---
    #
    # Hasta aca la lista de que chequear salia UNICAMENTE del .exe.Config del POS, asi
    # que agregar un destino nuevo —una balanza, un servidor de facturacion— exigia tocar
    # el codigo del agente y redistribuir el .exe a ~1.800 estaciones. Ahora el servidor
    # manda un catalogo y esto lo mezcla con lo que el agente descubre solo.
    #
    # Las dos fuentes conviven a proposito: los Postgres y las URLs del POS cambian de
    # farmacia en farmacia y solo el .exe.Config local sabe cuales son; los destinos fijos
    # (una IP igual en todas las farmacias) solo los sabe el servidor.

    def _aplicar_catalogo_servicios(self, payload: dict):
        """Guarda el catalogo que llego por MQTT. No chequea nada acá: solo lo deja
        disponible para el proximo ciclo de bucle_servicios_pos.

        Se persiste en `identidad.json` para que sobreviva a un reinicio del servicio: el
        mensaje es retenido, asi que igual llegaria al reconectar, pero entre el arranque
        y esa reconexion habria un ciclo chequeando la lista vieja.
        """
        contenido = self._catalogo_verificado(
            'catalogo de servicios del POS', 'catalogo_servicios_pos', payload,
        )
        if contenido is None:
            return
        catalogo = {
            'servicios': contenido.get('servicios') or [],
            'criticidad': contenido.get('criticidad') or {},
            'desactivados': contenido.get('desactivados') or [],
        }
        self.identidad['catalogo_servicios_pos'] = catalogo
        self.identidad['catalogo_servicios_pos_version'] = version_catalogo(payload)
        self._guardar_identidad()
        logging.info(
            'Catalogo de servicios recibido: %d con destino fijo, %d desactivado(s).',
            len(catalogo['servicios']), len(catalogo['desactivados']),
        )

    def _catalogo_servicios(self) -> dict:
        return self.identidad.get('catalogo_servicios_pos') or {
            'servicios': [], 'criticidad': {}, 'desactivados': [],
        }

    def _servicios_a_chequear(self) -> list:
        """Lo descubierto del .exe.Config MAS lo que mando el servidor, ya filtrado.

        Orden de precedencia, y cada regla existe por un motivo:

        - Un servicio DESACTIVADO en el catalogo no se chequea, aunque el .exe.Config lo
          siga nombrando. Es el caso de Odoo: esta de baja, sigue en la config de las 9
          estaciones, y cada una abria una alerta por algo que nadie iba a arreglar.
        - La criticidad la manda SIEMPRE el servidor si la conoce. Es lo que permite
          decidir desde el admin que un servicio dejo de ser critico sin redistribuir el
          ejecutable.
        - Si una clave viene de las dos fuentes, gana la del .exe.Config: ahi esta el host
          real de ESTA farmacia, que el servidor no puede saber.
        """
        catalogo = self._catalogo_servicios()
        desactivados = set(catalogo['desactivados'])
        criticidad = catalogo['criticidad']

        servicios = []
        vistos = set()
        for servicio in self._leer_servicios_pos():
            if servicio['servicio'] in desactivados:
                continue
            if servicio['servicio'] in criticidad:
                servicio = dict(servicio, critico=bool(criticidad[servicio['servicio']]))
            servicios.append(servicio)
            vistos.add(servicio['servicio'])

        for servicio in catalogo['servicios']:
            clave = servicio.get('servicio')
            if not clave or clave in desactivados or clave in vistos:
                continue
            servicios.append(dict(servicio))
        return servicios

    def _chequear_ping(self, servicio: dict):
        """(disponible, latencia_ms, mensaje). Nunca lanza.

        Reusa `_pingear`, la misma primitiva del sondeo de activos sin agente: no hace
        falta una segunda forma de preguntar "estas vivo?" en el mismo ejecutable.

        Es el chequeo mas debil de los tres —un equipo puede responder ICMP con el
        servicio caido— y por eso el catalogo permite elegir HTTP o PostgreSQL cuando hay
        algo mejor que preguntar. Para una balanza o una impresora de red, ping es lo
        unico que hay.
        """
        responde, ms = self._pingear(servicio.get('host', ''))
        if responde:
            return True, ms, 'responde al ping'
        return False, None, 'sin respuesta al ping'

    def _chequear_postgres(self, servicio: dict):
        """(disponible, latencia_ms, mensaje). Nunca lanza.

        Un SELECT 1 real y no solo el puerto TCP: Postgres acepta la conexion mucho antes
        de poder atender consultas —arrancando, en recuperacion, sin conexiones libres— y
        en todos esos casos el puerto abierto diria "sano" mientras la caja no vende.
        """
        inicio = time.monotonic()
        try:
            import psycopg2

            conn = psycopg2.connect(
                host=servicio['host'], port=int(servicio['puerto'] or 5432),
                dbname=servicio['bdd'], user=servicio['usuario'], password=servicio['clave'],
                connect_timeout=5,
            )
            try:
                with conn.cursor() as cur:
                    cur.execute('SELECT 1')
                    cur.fetchone()
                version = conn.server_version
            finally:
                conn.close()
            return True, int((time.monotonic() - inicio) * 1000), 'PostgreSQL %s' % version
        except Exception as exc:
            # El texto del error ES el diagnostico: distingue "no hay ruta" de "clave
            # rechazada" de "la base no existe", que llevan a lugares muy distintos.
            texto = str(exc).strip()
            return False, None, (texto.splitlines()[0][:280] if texto else type(exc).__name__)

    def _chequear_http(self, servicio: dict):
        """(disponible, latencia_ms, mensaje). Nunca lanza.

        urllib de la biblioteca estandar y no requests: el agente no la trae, y sumar una
        dependencia por tres peticiones agranda el ejecutable que despues hay que
        distribuir a ~1.800 estaciones.

        Todo lo menor a 500 cuenta como vivo. Un 404 o un 401 significan que el servicio
        esta atendiendo; que la ruta exacta no exista es otro problema. Solo los 5xx y los
        errores de red son "caido".
        """
        inicio = time.monotonic()
        try:
            req = urllib.request.Request(servicio['url'], method='GET')
            with urllib.request.urlopen(req, timeout=8) as resp:
                codigo = resp.status
        except urllib.error.HTTPError as exc:
            codigo = exc.code
        except Exception as exc:
            texto = str(exc).strip()
            return False, None, (texto[:280] if texto else type(exc).__name__)
        return codigo < 500, int((time.monotonic() - inicio) * 1000), 'HTTP %d' % codigo

    def _endpoint_visible(self, servicio: dict) -> str:
        """A que se apunto, SIN credenciales. Es lo unico de la conexion que sale del
        equipo: mismo criterio que EstadoRedActivo, que guarda la IP sondeada y nada mas.
        """
        if servicio['tipo'] == 'postgres':
            return '%s:%s/%s' % (servicio['host'], servicio['puerto'] or '5432', servicio['bdd'])
        if servicio['tipo'] == 'ping':
            return servicio.get('host', '')[:200]
        return servicio['url'][:200]

    def bucle_servicios_pos(self):
        """Calco de bucle_log_pos, con su propio intervalo. Solo corre si el agente sabe
        donde esta el POS: sin eso no hay .exe.Config que leer."""
        while True:
            time.sleep(self.args.intervalo_servicios_pos)
            if not self._token() or not self.args.pos_carpeta_instalacion:
                continue
            if self._pausado():
                continue
            try:
                self._reportar_servicios_pos()
            except Exception:
                logging.exception('No se pudieron chequear los servicios del POS')

    def _reportar_servicios_pos(self):
        # Lo que descubre del .exe.Config MAS lo que mando el servidor por el catalogo.
        servicios = self._servicios_a_chequear()
        if not servicios:
            return
        resultados = []
        for servicio in servicios:
            if servicio['tipo'] == 'postgres':
                disponible, ms, mensaje = self._chequear_postgres(servicio)
            elif servicio['tipo'] == 'ping':
                disponible, ms, mensaje = self._chequear_ping(servicio)
            else:
                disponible, ms, mensaje = self._chequear_http(servicio)
            resultados.append({
                'servicio': servicio['servicio'],
                'disponible': disponible,
                'latencia_ms': ms,
                'mensaje': mensaje,
                'endpoint': self._endpoint_visible(servicio),
                'critico': servicio['critico'],
            })
        # Datos crudos. Decidir si 800 ms es "lento" vive en ReglaAlerta, del lado del
        # panel: un umbral aca obligaria a redistribuir el agente para cambiarlo.
        self._publicar(f'/saidsof/agente/{self.args.codigo}/servicios_pos/', {
            'token': self._token(), 'resultados': resultados,
        })
        logging.info(
            'Servicios del POS: %s',
            ', '.join('%s=%s' % (r['servicio'], 'ok' if r['disponible'] else 'caido')
                      for r in resultados),
        )

    # --- heartbeat ---
    def bucle_heartbeat(self):
        """El latido es lo unico que sostiene la visibilidad de la flota: si este hilo
        muere, la estacion queda muda y el panel la da por caida aunque el POS este
        vendiendo con normalidad.

        Por eso el try/except, que hasta 0.27 este bucle NO tenia (ni `bucle_metricas`;
        los otros tres si). Es un hilo demonio: una excepcion no lo tumba el servicio,
        lo mata a EL y el agente sigue conectado al broker sin reportar nunca mas. Ese
        modo de falla ya costo una tarde el 21-sep-2026 en `bucle_eventos_sistema`, y
        alli el sintoma era una funcion que no andaba; aca seria una estacion que parece
        apagada. A 8 agentes se encuentra a mano; a 1.800 se diagnostica como un problema
        de red o de energia que no existe.

        Las funciones que arma el payload ya estan blindadas una por una
        (`_leer_config_pos`, `_version_pos_reportable` y `leer_zona_horaria` nunca
        lanzan), asi que esto es una red de seguridad, no un parche a un bug conocido —
        y es justo lo que hay que tener antes de un despliegue masivo.

        `ultimo_ciclo_latido` es la senal de vida que mira el servicio (0.37, ver
        `motivo_para_reiniciar`): se marca al empezar cada vuelta, haya o no conexion.
        Si una vuelta se queda trabada, deja de avanzar y el servicio se reinicia.
        """
        while True:
            time.sleep(self.args.intervalo_heartbeat)
            self.ultimo_ciclo_latido = time.monotonic()
            if not self._token():
                continue
            try:
                self._reportar_heartbeat()
            except Exception:
                logging.exception('No se pudo enviar el heartbeat')

    def _reportar_heartbeat(self):
        self._publicar(f'/saidsof/agente/{self.args.codigo}/heartbeat/', {
            'token': self._token(),
            'version_agente': VERSION_AGENTE_PRUEBA,
            # Que catalogo tiene aplicado (0.38): el servidor lo compara con el vigente y
            # el panel muestra si esta desactualizado. Vacio = todavia no recibio ninguno.
            'catalogos': {
                'servicios_pos': self.identidad.get('catalogo_servicios_pos_version', ''),
                'eventos_sistema': self.identidad.get('catalogo_eventos_sistema_version', ''),
            },
            # Se declara, no se deduce de la version: la version dice que este agente
            # ENTIENDE el secreto propio, esto dice que lo TIENE. Son cosas distintas
            # y confundirlas dejo mudas a dos estaciones (ver el bloque de
            # re-enrolamiento en _on_connect y apps.catalogo.services.secreto_de).
            'hmac_propio': bool(self.identidad.get('hmac_secret')),
            # Lo que confirma que el freno se aplico de verdad. Publicar la orden solo
            # prueba que salio del servidor; esto prueba que llego y esta vigente.
            'pausado': self._pausado(),
            'so_nombre': f'Windows {platform.win32_ver()[0]}',
            'so_build': platform.win32_ver()[1],
            'hostname': socket.gethostname(),
            'numero_serie': self.identidad.get('numero_serie', ''),
            # Reloj y zona horaria. El desfase lo calcula el SERVIDOR comparando este
            # epoch con el suyo: el agente no tiene contra qué compararse, porque si
            # su reloj está mal su idea de "ahora" también lo está. Va en el latido y
            # no en consultar_info porque un reloj que se corre deja a la estación sin
            # recibir comandos (ventana de 120 s), o sea que el aviso tiene que llegar
            # solo y no depender de que alguien apriete un botón.
            'reloj_epoch': time.time(),
            'offset_utc_minutos': offset_utc_minutos(),
            'zona_horaria': leer_zona_horaria(),
            # Cuantas veces esta estacion tuvo que corregirse el reloj sola. Va en el
            # latido y no en un topico nuevo porque este mensaje YA es el que lleva los
            # datos de reloj, ya tiene ACL y ya lo procesa el worker. Que el numero suba
            # es la senal que hoy no existe: una estacion que se autocorrige seguido
            # tiene un problema de hardware (pila de CMOS), no de configuracion.
            'autocorrecciones_reloj': self.identidad.get('autocorrecciones_reloj', 0),
            'ultima_autocorreccion_reloj': self.identidad.get('ultima_autocorreccion_reloj', ''),
            # A qué nodo apunta REALMENTE el POS de esta estación (ver
            # _leer_config_pos). Va en el heartbeat y no en consultar_info para que
            # se mantenga solo, sin depender de que alguien apriete un botón.
            **self._version_pos_reportable(),
            **self._leer_config_pos(),
            # IP y MAC con las que sale a la red. Van en el latido para que se
            # mantengan solas si DHCP le cambia la dirección.
            **self._red_lan(),
        })
        logging.info('Heartbeat enviado')

    # La MAC no cambia mientras la IP sea la misma, y leerla cuesta un PowerShell: se
    # vuelve a leer solo si cambió la IP o si pasó este tiempo desde la última lectura
    # (que cubre el caso de una lectura que falló).
    RELEER_MAC_SEGUNDOS = 3600

    def _red_lan(self) -> dict:
        """{'ip_lan': ..., 'mac_lan': ...} de la placa con la que este agente llega al
        broker, o {} si no se pudo saber. Nunca lanza: corre dentro del latido.

        La IP es la dirección LOCAL del socket MQTT ya conectado, no la primera placa que
        liste Windows: una caja con WiFi y cable, o con un adaptador virtual, tiene varias,
        y la que importa es la que efectivamente usa para salir. Detrás del NAT del
        Mikrotik es su IP en la LAN de la farmacia, que es la que ve la tabla ARP.

        Hasta la 0.34 el agente no mandaba ninguna de las dos, y la regla "la IP de un
        equipo con agente la reporta el agente" (Activo.ip_efectiva) se apoyaba en un dato
        que nadie producía: 78 de 79 estaciones sin IP, medido el 17-oct-2026.
        """
        try:
            sock = self.client.socket()
            if sock is None:
                return {}
            ip = sock.getsockname()[0]
        except Exception:
            return {}
        cache = getattr(self, '_red_lan_cache', None)
        if cache and cache[0] == ip and time.time() - cache[2] < self.RELEER_MAC_SEGUNDOS:
            mac = cache[1]
        else:
            mac = leer_mac_de_ip(ip)
            self._red_lan_cache = (ip, mac, time.time())
        return {'ip_lan': ip, **({'mac_lan': mac} if mac else {})}

    # --- métricas periódicas (CPU/RAM/disco) ---
    def bucle_metricas(self):
        """Calco de bucle_heartbeat, con su propio intervalo. Solo reporta si esta
        estación está marcada `monitorear_recursos=True` (viaja en la respuesta de
        enrolamiento) — mismo criterio de volumen que ya usaba el sistema viejo (solo
        servidores/matriz), aplicado ahora también a las cajas si se decide activarlo.

        Calco tambien en el try/except, que hasta 0.27 le faltaba igual que al latido:
        `_medir_recursos` y `_tasa_red_kbps` hacen cuentas sobre lo que devuelve un
        script de PowerShell, y una division por cero o una clave ausente en UNA
        estacion mataba el hilo de metricas de esa estacion para siempre.
        """
        while True:
            time.sleep(self.args.intervalo_metricas)
            if not self._token() or not self.identidad.get('monitorear_recursos'):
                continue
            if self._pausado():
                continue
            try:
                self._reportar_metricas()
            except Exception:
                logging.exception('No se pudieron enviar las metricas')

    def _reportar_metricas(self):
        recursos = self._medir_recursos()
        if not recursos:
            return
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
            return json.loads(correr_powershell(script, timeout=20))
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
            if self._pausado():
                continue
            try:
                errores = self._leer_errores_nuevos_pos()
            except Exception:
                logging.exception('No se pudo leer el log del POS')
                continue
            if errores is None:
                continue  # no se pudo leer (archivo ausente todavía, etc.) — no reportar nada
            errores, posicion_nueva = errores
            enviado = self._publicar(f'/saidsof/agente/{self.args.codigo}/pos_errores/', {
                'token': self._token(), 'errores': errores,
            })
            if not enviado:
                # La posición NO avanza: en el próximo ciclo se relee desde donde estaba y
                # estos errores se reintentan. Antes se guardaba dentro de la lectura,
                # antes de publicar, así que un broker caído —que pasa: el agente se
                # reconecta varias veces al día— se llevaba esos errores para siempre. Y
                # con QoS 0 paho descarta el mensaje sin avisar (ver `_publicar`), así
                # que nada quedaba para reintentar.
                #
                # El riesgo al revés es duplicar si el mensaje sí salió y el agente muere
                # antes de guardar: se elige duplicar. Un contador inflado se nota y se
                # corrige; un error del POS que nunca llegó no deja rastro en ningún lado
                # y la alerta correspondiente no se abre.
                logging.warning(
                    'Errores del POS no enviados (%d tipo(s)) — se reintentan en el próximo ciclo.',
                    len(errores),
                )
                continue
            self.identidad['pos_log_posicion'] = posicion_nueva
            self._guardar_identidad()
            if errores:
                logging.info('Errores del POS reportados: %d tipo(s) distinto(s)', len(errores))

    def _ruta_log_pos(self) -> str:
        return os.path.join(self.args.pos_carpeta_instalacion, self.args.pos_log_relativo)

    def _leer_errores_nuevos_pos(self):
        """Lee desde la última posición guardada (identidad['pos_log_posicion']),
        agrupa por mensaje exacto los niveles ERROR/FATAL, y devuelve
        `([{mensaje, nivel, cantidad}, ...], posicion_nueva)`. None si el archivo no
        existe todavía (POS recién instalado, o nunca generó el log) — distinto de []
        (se leyó, sin errores nuevos), para no pisar en falso la posición guardada.

        **No persiste la posición**: la devuelve para que `bucle_log_pos` la guarde
        recién cuando el reporte se haya publicado. Guardarla acá daba por leído lo que
        todavía no había salido del equipo, y con el broker caído eso se perdía sin
        dejar rastro."""
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
            posicion_nueva = f.tell()

        errores = [{'mensaje': m, 'nivel': d['nivel'], 'cantidad': d['cantidad']} for m, d in conteos.items()]
        return errores, posicion_nueva

    # --- scripts (RMM) ---
    def _manejar_comando(self, payload):
        comando = payload.get('comando')
        # Pausada: no se ejecuta NADA por este canal. `actualizar_agente` no pasa por
        # aca (tiene tópico propio y retenido) justamente para que siga funcionando
        # estando pausada: si lo que hay que frenar es el agente, empujarle la version
        # arreglada es el camino de recuperacion, y cerrarlo dejaria la flota congelada
        # sin salida que no sea visitar 700 farmacias.
        if self._pausado():
            logging.warning('Estoy pausada: ignoro el comando "%s".', comando)
            return
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
        elif comando == 'consultar_perifericos':
            self._verificar_y_consultar_perifericos(payload)
        elif comando == 'consultar_red_farmacia':
            self._verificar_y_consultar_red_farmacia(payload)
        elif comando == 'consultar_activos_farmacia':
            self._verificar_y_consultar_activos_farmacia(payload)
        elif comando == 'configurar_nodo_pos':
            self._verificar_y_configurar_nodo_pos(payload)
        elif comando == 'actualizar_agente':
            self._verificar_y_actualizar_agente(payload)
        else:
            logging.info('Comando "%s" recibido — no implementado en este agente de prueba.', comando)

    def _verificar_y_ejecutar_script(self, payload):
        valido = self._firma_valida(
            'comando ejecutar_script', payload,
            comando='ejecutar_script', ejecucion_id=payload['ejecucion_id'], resultado_id=payload['resultado_id'],
            tipo_script=payload['tipo_script'], timeout_segundos=payload['timeout_segundos'],
            contenido=payload['contenido'], estacion=payload.get('estacion'), timestamp=payload.get('timestamp'),
        )
        if not valido:
            return

        resultado_id = payload['resultado_id']
        self._recordar_orden('script', resultado_id, payload)
        self._reportar_script(resultado_id, 'ejecutando')
        ruta_script = None
        try:
            es_powershell = payload['tipo_script'] == 'powershell'
            extension = '.ps1' if es_powershell else '.bat'
            contenido = payload['contenido']
            # Los acentos iban y volvian rotos, en las DOS direcciones, y como el script
            # igual terminaba con exit 0 no habia nada que delatara el problema: la salida
            # simplemente se leia mal en el panel ("termino inesperadamente" aparecia como
            # "terminÃ³ inesperadamente"). Encontrado el 21-sep-2026 leyendo el visor de
            # eventos de MAM06-A por este mismo camino.
            #
            # DE IDA: PowerShell 5.1 lee un .ps1 SIN BOM como ANSI, asi que un script con
            #   acentos en su texto se corrompe antes de ejecutarse. El BOM (utf-8-sig) es
            #   lo que le dice que es UTF-8. En un .bat no va: cmd escupe el BOM como
            #   basura en la primera linea.
            # DE VUELTA: `text=True` sin `encoding=` decodifica con la pagina de codigos
            #   local (cp1252 en un Windows en espanol), pero el script escribe UTF-8.
            #   Se fuerza la salida a UTF-8 y se decodifica igual, como ya hacia
            #   `_leer_eventos_sistema`. Un .bat queda como estaba: su salida SI sale en
            #   la pagina del sistema y forzarla no tendria donde engancharse.
            #
            # La linea se antepone SALVO que el script empiece con `param(` o `#requires`,
            # que en PowerShell tienen que ser lo primero del archivo: ahi se deja como
            # estaba antes que romper el script. Ninguno de los 49 guardados al 21-sep-2026
            # empieza asi, pero uno futuro puede.
            if es_powershell and not re.match(r'\s*(param\s*\(|#requires)', contenido, re.IGNORECASE):
                contenido = (
                    '[Console]::OutputEncoding = [System.Text.Encoding]::UTF8\n' + contenido
                )
            with tempfile.NamedTemporaryFile(
                'w', suffix=extension, delete=False,
                encoding='utf-8-sig' if es_powershell else 'utf-8',
            ) as f:
                f.write(contenido)
                ruta_script = f.name
            comando_exec = (
                ['powershell', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', ruta_script]
                if es_powershell else [ruta_script]
            )
            # Sin tuberías (0.38): un script que deja un proceso vivo heredando la salida
            # —un `Start-Process`, un servicio que arranca— dejaba al agente esperando para
            # siempre, igual que el instalador de LibreOffice en ML008 (ver
            # `ejecutar_sin_tuberias`). Al vencer el tiempo se termina el árbol completo.
            codigo, stdout, stderr = ejecutar_capturando(
                comando_exec, payload['timeout_segundos'], encoding='utf-8' if es_powershell else None,
            )
            if codigo is None:
                self._reportar_script(resultado_id, 'timeout', stdout=stdout, stderr=stderr)
                return
            estado = 'completado' if codigo == 0 else 'error'
            self._reportar_script(resultado_id, estado, exit_code=codigo, stdout=stdout, stderr=stderr)
        except Exception as exc:
            self._reportar_script(resultado_id, 'error', stderr=str(exc))
        finally:
            if ruta_script:
                try:
                    os.unlink(ruta_script)
                except OSError:
                    pass

    def _recordar_orden(self, tipo, origen_id, payload):
        """Guarda el `orden_uid` del mensaje (0.38) para devolverlo en los reportes de ese
        origen. Solo se llama con la firma ya validada. Un servidor anterior no lo manda:
        entonces se olvida el que hubiera, y los reportes salen sin él, como antes.

        El uid no va firmado (los agentes viejos reconstruyen la lista exacta de campos
        firmados). Solo sirve para que el servidor asocie el reporte a la orden correcta."""
        uid = payload.get('orden_uid')
        if isinstance(uid, str) and 0 < len(uid) <= 64:
            self._orden_de[(tipo, origen_id)] = uid
        else:
            self._orden_de.pop((tipo, origen_id), None)

    def _con_orden(self, cuerpo, tipo, origen_id, orden_uid):
        """`orden_uid=None`: el recordado para ese origen; '' : ninguno (un mensaje que no
        validó la firma no se asocia a ninguna orden)."""
        uid = self._orden_de.get((tipo, origen_id)) if orden_uid is None else orden_uid
        if uid:
            cuerpo['orden_uid'] = uid
        return cuerpo

    def _reportar_script(self, resultado_id, estado, exit_code=None, stdout='', stderr='', orden_uid=None):
        # QoS 1 (0.38, E-01): un corte de red no pierde el paso; el servidor tolera duplicados.
        self._publicar(f'/saidsof/agente/{self.args.codigo}/script_estado/', self._con_orden({
            'token': self._token(), 'resultado_id': resultado_id, 'estado': estado,
            'exit_code': exit_code, 'stdout': stdout, 'stderr': stderr,
        }, 'script', resultado_id, orden_uid), qos=1)
        logging.info('Script #%s -> %s', resultado_id, estado)

    # --- consultar_info (hardware/BitLocker bajo demanda) ---
    def _verificar_y_consultar_info(self, payload):
        # Mismo esquema de firma que ejecutar_script — aunque este comando solo lee
        # info (no ejecuta nada), igual conviene no responder a un "consultar_info"
        # forjado por cualquiera que pueda publicar en el tópico.
        if not self._firma_valida(
            'comando consultar_info', payload,
            comando='consultar_info', estacion=payload.get('estacion'), timestamp=payload.get('timestamp'),
        ):
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
            return json.loads(correr_powershell(script, timeout=20))
        except Exception:
            logging.exception('No se pudo consultar la info del equipo (CIM/BitLocker)')
            return {}

    # --- freno de emergencia ---
    #
    # `pausado` vive en identidad.json y no solo en memoria. El mensaje retenido vuelve a
    # llegar al reconectar, pero eso pasa DESPUES del `_on_connect`, y los bucles ya
    # estan corriendo desde el arranque: sin persistirlo, cada reinicio del servicio daria
    # una ventana de varios segundos en la que una estacion frenada vuelve a reportar y a
    # ejecutar. Con 1.800 equipos reiniciandose, esa ventana deja de ser teorica.
    def _pausado(self) -> bool:
        return bool(self.identidad.get('pausado'))

    def _verificar_y_aplicar_pausa(self, payload):
        if not self._firma_valida(
            'orden de pausa', payload, verificar_ventana=False,
            comando='pausa', pausado=str(bool(payload.get('pausado'))).lower(),
            estacion=payload.get('estacion'), timestamp=payload.get('timestamp'),
        ):
            return

        pausado = bool(payload.get('pausado'))
        if pausado == self._pausado():
            return  # el retenido vuelve a llegar en cada reconexion; no hay nada que hacer
        self.identidad['pausado'] = pausado
        self._guardar_identidad()
        if pausado:
            logging.warning(
                'PAUSADA desde el panel: dejo de ejecutar comandos y de reportar todo '
                'menos el latido. Sigo aceptando actualizaciones del agente.',
            )
        else:
            logging.warning('Reanudada desde el panel: vuelvo a operar con normalidad.')

    # --- reiniciar (equipo completo, no solo el servicio) ---
    def _verificar_y_reiniciar(self, payload):
        # Mismo esquema de firma que consultar_info/ejecutar_script. Este comando
        # reinicia el equipo Windows completo (no el servicio del agente) — el botón
        # del panel avisa "interrumpe cualquier venta en curso en esa caja", y es
        # fire-and-forget: el servidor no espera ninguna confirmación de vuelta.
        if not self._firma_valida(
            'comando reiniciar', payload,
            comando='reiniciar', estacion=payload.get('estacion'), timestamp=payload.get('timestamp'),
        ):
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
        if not self._firma_valida(
            'comando escanear_actualizaciones', payload,
            comando='escanear_actualizaciones', estacion=payload.get('estacion'), timestamp=payload.get('timestamp'),
        ):
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
        if not self._firma_valida(
            'comando consultar_software_instalado', payload,
            comando='consultar_software_instalado',
            estacion=payload.get('estacion'), timestamp=payload.get('timestamp'),
        ):
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
        salida = correr_powershell(script, timeout=30)
        datos = json.loads(salida) if salida.strip() else []
        return datos if isinstance(datos, list) else [datos]

    def _verificar_y_consultar_perifericos(self, payload):
        # Mismo esquema de firma que consultar_software_instalado.
        if not self._firma_valida(
            'comando consultar_perifericos', payload,
            comando='consultar_perifericos',
            estacion=payload.get('estacion'), timestamp=payload.get('timestamp'),
        ):
            return
        threading.Thread(target=self._escanear_y_reportar_perifericos, daemon=True).start()

    def _escanear_y_reportar_perifericos(self):
        try:
            dispositivos = self._listar_perifericos_usb()
        except Exception:
            logging.exception('No se pudo listar los periféricos USB')
            dispositivos = []
        self._publicar(f'/saidsof/agente/{self.args.codigo}/perifericos/', {
            'token': self._token(), 'dispositivos': dispositivos,
        })
        logging.info('Periféricos USB reportados: %d', len(dispositivos))

    def _listar_perifericos_usb(self) -> list:
        """Enumera los dispositivos actualmente conectados por USB (Win32_PnPEntity,
        filtrado a DeviceID que empieza con "USB\\") — impresoras, teclado, mouse,
        almacenamiento, cámaras, cualquier periférico USB. No detecta periféricos
        inalámbricos por Bluetooth (otro DeviceID, fuera de este filtro) ni impresoras
        de red (no pasan por el bus USB de este equipo) — limitación real de WMI, no de
        este código."""
        script = r"""
$ErrorActionPreference = 'SilentlyContinue'
$dispositivos = Get-CimInstance Win32_PnPEntity -Filter "DeviceID LIKE 'USB%'" |
    Where-Object { $_.Status -eq 'OK' } |
    Select-Object @{N='nombre';E={$_.Name}}, @{N='fabricante';E={$_.Manufacturer}}, @{N='clase';E={$_.PNPClass}}, @{N='device_id';E={$_.DeviceID}}
ConvertTo-Json -Compress -InputObject @($dispositivos)
"""
        # Mismo motivo que _listar_software_instalado: -InputObject @(...) fuerza
        # serialización como array sin importar cuántos elementos tenga.
        salida = correr_powershell(script, timeout=20)
        datos = json.loads(salida) if salida.strip() else []
        return datos if isinstance(datos, list) else [datos]

    # --- ancho de banda del enlace de la farmacia (Mikrotik local, sin VPN) ---
    #
    # apps.monitoreo.mikrotik sondea los ~700 Mikrotik de las farmacias por SNMP
    # DESDE EL SERVIDOR CENTRAL -- pero el servidor no tiene ninguna ruta de red
    # hacia esas IPs privadas (192.168/169/170.x.x), confirmado con ping real
    # (24-ago-2026): 100% de pérdida, sin ruta en la tabla del host. Esta estación,
    # en cambio, está en la MISMA LAN que el Mikrotik de su propia farmacia -- lo
    # alcanza directo, sin VPN. Mismos OIDs/protocolo que el lado servidor, pero
    # sondeando un solo router en vez de 700 en paralelo con asyncio.

    # --- ping a los activos sin agente de la farmacia ---
    #
    # Mismo motivo de red que el sondeo del Mikrotik: el servidor central no tiene ruta
    # hacia las IPs privadas de la farmacia, esta estación sí. Una impresora, un medianet
    # o un pinpad no corren agente, así que este ping es la única forma de saber si están
    # vivos.

    def _verificar_y_consultar_activos_farmacia(self, payload):
        if not self._firma_valida(
            'comando consultar_activos_farmacia', payload,
            comando='consultar_activos_farmacia', objetivos=payload.get('objetivos'),
            estacion=payload.get('estacion'), timestamp=payload.get('timestamp'),
        ):
            return
        threading.Thread(
            target=self._pingear_y_reportar_activos, args=(payload.get('objetivos') or '',), daemon=True,
        ).start()

    def _pingear_y_reportar_activos(self, objetivos: str):
        resultados = []
        for parte in objetivos.split(','):
            parte = parte.strip()
            if not parte or ':' not in parte:
                continue
            activo_id, _, ip = parte.partition(':')
            responde, latencia = self._pingear(ip)
            resultados.append({
                'activo_id': activo_id, 'ip': ip, 'responde': responde, 'latencia_ms': latencia,
            })
        if not resultados:
            return
        self._publicar(f'/saidsof/agente/{self.args.codigo}/activos_farmacia/', {
            'token': self._token(), 'resultados': resultados,
        })

    def _pingear(self, ip: str):
        """(responde, latencia_ms). Nunca lanza: un destino inalcanzable es un dato, no
        un error.

        Un solo intento con 1 s de espera. Con varios destinos en serie, subir esto
        multiplica el tiempo total del sondeo; y para la pregunta que se responde —¿está
        vivo?— un equipo que tarda más de un segundo dentro de su propia LAN ya es un
        problema en sí mismo.
        """
        try:
            proc = subprocess.run(
                ['ping', '-n', '1', '-w', '1000', ip],
                capture_output=True, text=True, timeout=5,
                encoding='oem', errors='replace',
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
            )
        except Exception:
            return False, None
        if proc.returncode != 0:
            return False, None
        # "tiempo=3ms", "tiempo<1ms", "time=3ms" según el idioma de Windows.
        encontrado = re.search(r'(?:tiempo|time)[=<](\d+)\s*ms', proc.stdout, re.IGNORECASE)
        return True, int(encontrado.group(1)) if encontrado else None

    def _verificar_y_consultar_red_farmacia(self, payload):
        if not self._firma_valida(
            'comando consultar_red_farmacia', payload,
            comando='consultar_red_farmacia', comunidad=payload.get('comunidad'),
            estacion=payload.get('estacion'), timestamp=payload.get('timestamp'),
        ):
            return
        threading.Thread(
            target=self._escanear_y_reportar_red_farmacia, args=(payload.get('comunidad'),), daemon=True,
        ).start()

    def _escanear_y_reportar_red_farmacia(self, comunidad):
        try:
            contadores = self._leer_contadores_mikrotik(comunidad)
        except Exception:
            logging.exception('No se pudo sondear el Mikrotik local de la farmacia')
            contadores = None
        payload = {'token': self._token()}
        if contadores is not None:
            payload['bytes_recibidos'], payload['bytes_enviados'] = contadores
        self._publicar(f'/saidsof/agente/{self.args.codigo}/red_farmacia/', payload)
        logging.info(
            'Red de farmacia reportada: %s', contadores if contadores is not None else 'sin datos (router no respondió)',
        )

    def _gateway_por_defecto(self) -> str:
        """IP del gateway por defecto de Windows -- en la LAN de una farmacia es el
        propio Mikrotik (confirmado contra ML006: la IP LAN de la estación y
        Farmacia.ip_router caen en la misma subred /24). No se usa el ip_router que
        conoce el servidor porque el agente no lo recibe y porque redescubrirlo
        localmente es más robusto que depender de que ese dato esté siempre al día."""
        try:
            salida = correr_powershell(
                "(Get-NetRoute -DestinationPrefix '0.0.0.0/0' -ErrorAction SilentlyContinue | "
                "Sort-Object -Property RouteMetric | Select-Object -First 1 -ExpandProperty NextHop)",
                timeout=10,
            )
            return salida.strip()
        except Exception:
            logging.exception('No se pudo determinar el gateway por defecto de Windows')
            return ''

    def _leer_contadores_mikrotik(self, comunidad: str):
        """(bytes_recibidos, bytes_enviados) del Mikrotik local, o None si no se pudo
        sondear -- nunca lanza. Mismos OIDs que apps.monitoreo.mikrotik: ipRouteIfIndex
        de la ruta por defecto para resolver la interfaz WAN sin necesitar su nombre
        (no es uniforme entre sitios), después ifHCIn/OutOctets (64 bits, evita
        wraparound) de esa interfaz."""
        import asyncio

        from pysnmp.hlapi.v3arch.asyncio import (
            CommunityData, ContextData, ObjectIdentity, ObjectType, SnmpEngine, UdpTransportTarget, get_cmd,
        )

        ip = self._gateway_por_defecto()
        if not ip:
            return None

        async def _sondear():
            engine = SnmpEngine()
            target = await UdpTransportTarget.create((ip, 161), timeout=3, retries=0)

            _, error_status, _, var_binds = await get_cmd(
                engine, CommunityData(comunidad), target, ContextData(),
                ObjectType(ObjectIdentity('1.3.6.1.2.1.4.21.1.2.0.0.0.0')),
            )
            if error_status:
                return None
            try:
                indice = int(var_binds[0][1])
            except (IndexError, ValueError, TypeError):
                return None

            _, error_status, _, var_binds = await get_cmd(
                engine, CommunityData(comunidad), target, ContextData(),
                ObjectType(ObjectIdentity(f'1.3.6.1.2.1.31.1.1.1.6.{indice}')),
                ObjectType(ObjectIdentity(f'1.3.6.1.2.1.31.1.1.1.10.{indice}')),
            )
            if error_status:
                return None
            try:
                return int(var_binds[0][1]), int(var_binds[1][1])
            except (IndexError, ValueError, TypeError):
                return None

        return asyncio.run(_sondear())

    # --- auto-actualización del agente ---
    NOMBRE_SERVICIO = 'SaidsoftAgente'  # debe coincidir con ServicioAgenteSaidsoft._svc_name_

    def _verificar_y_actualizar_agente(self, payload, *, verificar_ventana: bool = True):
        if not self._firma_valida(
            'actualizar_agente', payload, verificar_ventana=verificar_ventana,
            comando='actualizar_agente', version=payload.get('version'), url=payload.get('url'),
            sha256=payload.get('sha256'), estacion=payload.get('estacion'), timestamp=payload.get('timestamp'),
        ):
            return
        # En un hilo aparte: la descarga y el reemplazo no deben bloquear el loop de MQTT.
        threading.Thread(target=self._aplicar_actualizacion_agente, args=(payload,), daemon=True).start()

    def _aplicar_actualizacion_agente(self, payload):
        version_nueva = payload.get('version', '?')
        if _version_mayor_o_igual(VERSION_AGENTE_PRUEBA, version_nueva):
            # Nunca downgrade automático -- ver docstring de _version_mayor_o_igual
            # sobre el mensaje retenido rezagado que causó justo esto en producción.
            logging.info(
                'Actualización de agente a %s ignorada: ya estoy en %s.', version_nueva, VERSION_AGENTE_PRUEBA,
            )
            return
        if not getattr(sys, 'frozen', False):
            logging.error(
                'Actualización de agente a %s: no corre como ejecutable empaquetado '
                '(modo consola/desarrollo) — se ignora, solo se auto-actualiza corriendo como servicio.',
                version_nueva,
            )
            return
        try:
            ruta_nueva = self._descargar(payload['url'])
            hash_local = self._sha256_de(ruta_nueva)
            if hash_local.lower() != payload['sha256'].lower():
                logging.error(
                    'Actualización de agente a %s: SHA-256 no coincide (esperado %s, obtenido %s) — se descarta.',
                    version_nueva, payload['sha256'], hash_local,
                )
                return
        except Exception:
            logging.exception('No se pudo descargar la actualización de agente a %s', version_nueva)
            return

        exe_actual = sys.executable
        script_swap = self._generar_script_swap(exe_actual, ruta_nueva, version_nueva)
        logging.info(
            'Actualización de agente a %s: lanzando script de reemplazo (%s). El servicio "%s" se '
            'reinicia solo en unos segundos.', version_nueva, script_swap, self.NOMBRE_SERVICIO,
        )
        # DETACHED_PROCESS: sigue vivo después de que el servicio actual se detenga (el
        # propio script es quien detiene el servicio vía "net stop", no este proceso —
        # un servicio de Windows no puede pararse a sí mismo desde adentro más que
        # devolviendo el control a SvcDoRun, que es justo lo que "net stop" dispara).
        #
        # stdin/stdout/stderr=DEVNULL es obligatorio acá: un servicio de Windows no tiene
        # consola, así que sus handles estándar son inválidos — sin esto, CreateProcess
        # falla al intentar heredarlos (WinError 6, "The handle is invalid") y Popen()
        # lanza una excepción que muere en silencio dentro de este hilo. Encontrado en
        # producción (ML006-A, 23-ago-2026).
        #
        # cmd.exe + .bat, NO powershell.exe -ExecutionPolicy Bypass -WindowStyle Hidden:
        # ese patrón (PowerShell oculto, política de ejecución saltada, lanzado por un
        # servicio en segundo plano) es una firma clásica que un antivirus/EDR está
        # diseñado para bloquear -- confirmado en producción (ML006-A, 25-ago-2026): el
        # Popen ya no lanzaba excepción (el fix de arriba funcionaba), pero el .ps1
        # nunca llegaba a ejecutar ni su primera línea (sin log propio, sin autoborrado)
        # -- el proceso se creaba y moría/bloqueaba antes de correr nada. "net stop"/
        # "copy"/"net start" vía cmd.exe son comandos mucho más mundanos, sin ninguna
        # bandera que dispare esa clase de heurística.
        try:
            subprocess.Popen(
                ['cmd.exe', '/c', script_swap],
                creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP,
                close_fds=True,
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        except Exception:
            # Si esto falla, la actualización queda descargada pero nunca aplicada -- sin
            # este log, el único rastro sería un .ps1 huérfano en TEMP que nunca se
            # autoborra (justo lo que pasó en producción antes de este fix).
            logging.exception('No se pudo lanzar el script de reemplazo para la actualización a %s', version_nueva)

    def _generar_script_swap(self, exe_actual: str, exe_nuevo: str, version_nueva: str) -> str:
        """Batch (cmd.exe), no PowerShell -- ver comentario en _aplicar_actualizacion_agente
        sobre por qué (un antivirus/EDR bloqueaba el patrón powershell -ExecutionPolicy
        Bypass -WindowStyle Hidden lanzado por un servicio, encontrado en ML006-A,
        25-ago-2026). Mismo comportamiento que la versión anterior: espera a que el
        servicio quede detenido (recién ahí el .exe actual suelta su lock de archivo),
        reemplaza el binario (con .bak por si hay que revertir a mano), vuelve a
        arrancar el servicio, y se autoborra al terminar ("del %~f0" como última línea
        -- técnica estándar de Windows para que un .bat se borre a sí mismo).

        La espera entre reintentos usa "ping -n 2 127.0.0.1", NO "timeout /t 1": ese
        último requiere una consola/stdin real para funcionar (chequea si hay una
        tecla presionada incluso con /nobreak) y este script corre con stdin=DEVNULL
        (obligatorio, el servicio no tiene consola).

        Sigue sin usar "sc query | findstr" para esperar a que el servicio quede
        detenido -- encontrado en producción (ML006-A, 26-ago-2026, ya con el fix de
        "ping" de arriba aplicado): el .bat seguía sin pasar de su primera línea de
        log en NINGÚN intento, "net stop" funcionaba (el servicio se detenía, según
        el log del propio agente), pero nada después de eso quedaba registrado. En
        vez de intentar detectar con precisión cuándo el proceso soltó el lock del
        .exe (algo en ese `sc query`/pipe seguía sin funcionar en este entorno sin
        consola, sin poder confirmar exactamente qué), esta versión: (1) loguea cada
        paso por separado, para que si vuelve a trabarse el log diga dónde; (2) no
        pisa el .exe en uso directamente -- copia el nuevo a un archivo .tmp (nunca
        toca el archivo bloqueado) y recién al final hace un `move` rápido, con
        reintentos cortos vía "ping" si el .exe todavía está en uso en ese momento."""
        exe_bak = exe_actual + '.bak'
        exe_tmp = exe_actual + '.nuevo'
        log_swap = os.path.join(tempfile.gettempdir(), 'saidsoft-agente-update.log')
        script = f'''@echo off
echo %date% %time% - [1] iniciando actualizacion a {version_nueva} >> "{log_swap}"
net stop "{self.NOMBRE_SERVICIO}"
echo %date% %time% - [2] net stop devolvio %errorlevel% >> "{log_swap}"
if exist "{exe_tmp}" del /f /q "{exe_tmp}"
copy /y "{exe_nuevo}" "{exe_tmp}" >nul
echo %date% %time% - [3] copia a temporal devolvio %errorlevel% >> "{log_swap}"
set intentos=0
:reemplazar
if exist "{exe_bak}" del /f /q "{exe_bak}"
move /y "{exe_actual}" "{exe_bak}" >nul 2>>"{log_swap}"
if not errorlevel 1 goto movido
set /a intentos+=1
echo %date% %time% - [4] .exe actual todavia en uso, intento %intentos% >> "{log_swap}"
if %intentos% geq 20 goto fallo_reemplazo
ping -n 2 127.0.0.1 >nul
goto reemplazar
:movido
move /y "{exe_tmp}" "{exe_actual}" >nul
echo %date% %time% - [5] Actualizacion a {version_nueva} aplicada tras %intentos% reintento(s). >> "{log_swap}"
goto reiniciar
:fallo_reemplazo
echo %date% %time% - [5] ERROR: el .exe actual seguia en uso tras %intentos% reintentos, se aborta. >> "{log_swap}"
:reiniciar
net start "{self.NOMBRE_SERVICIO}"
echo %date% %time% - [6] net start devolvio %errorlevel% >> "{log_swap}"
del "%~f0"
'''
        ruta_script = os.path.join(tempfile.gettempdir(), f'saidsoft-agente-update-{int(time.time())}.bat')
        with open(ruta_script, 'w', encoding='utf-8') as f:
            f.write(script)
        return ruta_script

    # --- software (catálogo) ---
    def _manejar_software(self, payload):
        solicitud_id = payload.get('solicitud_id')
        if self._ya_procesado('instalacion', solicitud_id, payload.get('timestamp')):
            logging.info('Instalación #%s ya procesada antes — se ignora el retenido.', solicitud_id)
            return
        # verificar_ventana=False: este mensaje se publica RETENIDO para que una
        # estación apagada lo reciba al reconectar (apps.software.services), así que
        # legítimamente puede llegar horas o días después de firmado -- exigirle los
        # 120s de la ventana lo hacía imposible de entregar, que era justo lo
        # contrario de retenerlo (encontrado en MAM06-A, 26-ago-2026: la instalación
        # #9 se rechazaba con "timestamp fuera de ventana" en CADA reconexión). El
        # SHA-256 del instalador sigue siendo la garantía de integridad, y
        # solicitud_id actúa de nonce vía _ya_procesado.
        if not self._firma_valida(
            'instalación de software', payload, verificar_ventana=False,
            comando='instalar_software', solicitud_id=payload.get('solicitud_id'),
            aplicacion=payload.get('aplicacion'), version=payload.get('version'), accion=payload.get('accion'),
            url=payload.get('url'), sha256=payload.get('sha256'),
            comando_instalacion_silenciosa=payload.get('comando_instalacion_silenciosa'),
            comando_desinstalacion=payload.get('comando_desinstalacion'),
            argumentos_adicionales=payload.get('argumentos_adicionales'),
            comando_deteccion=payload.get('comando_deteccion'), timestamp=payload.get('timestamp'),
        ):
            if solicitud_id is not None:
                self._reportar_instalacion(
                    solicitud_id, 'error', detalle='Firma HMAC inválida o mensaje expirado.', orden_uid='',
                )
            return
        if self._procesado_antes_de_actualizar('instalacion', solicitud_id):
            self._marcar_procesado('instalacion', solicitud_id, payload.get('timestamp'))
            logging.info('Instalación #%s ya procesada por una versión anterior del agente — se ignora.', solicitud_id)
            return
        self._marcar_procesado('instalacion', solicitud_id, payload.get('timestamp'))
        self._recordar_orden('instalacion', solicitud_id, payload)

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
        # Sin tuberías y con el árbol acotado: ver `ejecutar_sin_tuberias`.
        codigo, salida = ejecutar_sin_tuberias(comando, TIMEOUT_INSTALADOR_SEGUNDOS)
        if codigo is None:
            self._reportar_instalacion(
                solicitud_id, 'error',
                detalle=f'Timeout ({TIMEOUT_INSTALADOR_SEGUNDOS}s) ejecutando el instalador: se terminó junto con '
                        f'sus procesos hijos. Salida: {salida}',
            )
            return
        # 3010 y 1641 son éxito con reinicio pendiente (ERROR_SUCCESS_REBOOT_REQUIRED /
        # _INITIATED): msiexec los devuelve aunque se le pase /norestart. Antes contaban
        # como error y una instalación correcta salía en rojo en el panel.
        if codigo in (0, 3010, 1641):
            if codigo:
                logging.info('Instalación #%s correcta, pide reinicio (código %s).', solicitud_id, codigo)
            self._reportar_instalacion(solicitud_id, 'instalado', version_instalada=payload.get('version', ''))
        else:
            self._reportar_instalacion(
                solicitud_id, 'error', detalle=f'El comando devolvió código {codigo}: {salida}',
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
                # Un solo intento: si el caché falla, el central es el respaldo.
                bajar_archivo(url_cache, ruta, intentos=1)
                logging.info('Descargado del caché de farmacia: %s', url_cache)
                return ruta
            except Exception as exc:
                logging.warning('Caché de farmacia no disponible (%s: %s), cae al central.', url_cache, exc)
        bajar_archivo(url, ruta)
        return ruta

    def _sha256_de(self, ruta: str) -> str:
        hasher = hashlib.sha256()
        with open(ruta, 'rb') as f:
            for bloque in iter(lambda: f.read(65536), b''):
                hasher.update(bloque)
        return hasher.hexdigest()

    def _reportar_instalacion(self, solicitud_id, paso, detalle='', version_instalada='', orden_uid=None):
        # QoS 1 (0.38, E-01): un corte de red no pierde el paso; el servidor tolera duplicados.
        self._publicar(f'/saidsof/agente/{self.args.codigo}/software_estado/', self._con_orden({
            'token': self._token(), 'solicitud_id': solicitud_id, 'paso': paso,
            'detalle': detalle, 'version_instalada': version_instalada,
        }, 'instalacion', solicitud_id, orden_uid), qos=1)
        logging.info('Instalación #%s -> %s%s', solicitud_id, paso, f' ({detalle})' if detalle else '')

    # --- despliegues de POS ---
    def _manejar_despliegue(self, payload):
        despliegue_id = payload.get('despliegue_id')
        if self._ya_procesado('despliegue', despliegue_id, payload.get('timestamp')):
            logging.info('Despliegue #%s ya procesado antes — se ignora el retenido.', despliegue_id)
            return
        # verificar_ventana=False + _ya_procesado: mismo motivo que en
        # _manejar_software (los despliegues también se publican retenidos).
        if not self._firma_valida(
            'despliegue', payload, verificar_ventana=False,
            comando='desplegar', despliegue_id=payload.get('despliegue_id'), version=payload.get('version'),
            url=payload.get('url'), sha256=payload.get('sha256'), modo_aplicacion=payload.get('modo_aplicacion'),
            ventana_fecha_hora=payload.get('ventana_fecha_hora'), timestamp=payload.get('timestamp'),
        ):
            if despliegue_id is not None:
                self._reportar_despliegue(
                    despliegue_id, 'error', detalle='Firma HMAC inválida o mensaje expirado.', orden_uid='',
                )
            return
        if self._procesado_antes_de_actualizar('despliegue', despliegue_id):
            self._marcar_procesado('despliegue', despliegue_id, payload.get('timestamp'))
            logging.info('Despliegue #%s ya procesado por una versión anterior del agente — se ignora.', despliegue_id)
            return
        self._marcar_procesado('despliegue', despliegue_id, payload.get('timestamp'))
        self._recordar_orden('despliegue', despliegue_id, payload)

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
                text=True, timeout=10, encoding='oem', errors='replace',
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
        # Mismo motivo que el Popen de _aplicar_actualizacion_agente: sin stdin/stdout/
        # stderr=DEVNULL, CreateProcess falla al intentar heredar los handles estándar
        # (inválidos bajo un servicio de Windows sin consola) y el POS nunca llega a
        # arrancar -- silencioso, sin excepción visible en ningún lado.
        subprocess.Popen(
            [self.args.pos_comando_iniciar],
            cwd=os.path.dirname(self.args.pos_comando_iniciar) or None,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )

    def _version_pos_actual(self) -> str:
        """Best-effort: versión de archivo del ejecutable del POS antes de tocarlo.
        Si falla (no existe, sin permisos) devuelve '' — no es crítico para aplicar."""
        try:
            salida = correr_powershell(
                f"(Get-Item -LiteralPath '{self.args.pos_comando_iniciar}').VersionInfo.ProductVersion",
                timeout=10,
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

    def _reportar_despliegue(self, despliegue_id, paso, detalle='', version_previa=None, version_nueva=None,
                             orden_uid=None):
        cuerpo = self._con_orden(
            {'token': self._token(), 'despliegue_id': despliegue_id, 'paso': paso, 'detalle': detalle},
            'despliegue', despliegue_id, orden_uid,
        )
        if version_previa is not None:
            cuerpo['version_previa'] = version_previa
        if version_nueva is not None:
            cuerpo['version_nueva'] = version_nueva
        # QoS 1 (0.38, E-01): un corte de red no pierde el paso; el servidor tolera duplicados.
        self._publicar(f'/saidsof/agente/{self.args.codigo}/despliegue_estado/', cuerpo, qos=1)
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
        self.ultimo_ciclo_latido = time.monotonic()
        threading.Thread(target=self.bucle_heartbeat, daemon=True).start()
        threading.Thread(target=self.bucle_metricas, daemon=True).start()
        threading.Thread(target=self.bucle_log_pos, daemon=True).start()
        threading.Thread(target=self.bucle_servicios_pos, daemon=True).start()
        threading.Thread(target=self.bucle_eventos_sistema, daemon=True).start()
        threading.Thread(target=self.bucle_reloj, daemon=True).start()
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
    ('token_apertura', ''),
    # `_enrolar` lo lee siempre que no haya identidad guardada. Sin esta entrada el
    # servicio arma un Namespace sin el atributo y la primera conexión de una estación
    # nueva revienta en `_on_connect` con AttributeError: quedaba conectada, sin
    # suscripciones y sin pedir enrolamiento (MNAJ1-B, 15-oct-2026).
    ('hardware_id', ''),
    ('intervalo_heartbeat', 60),
    ('intervalo_metricas', 300),
    ('intervalo_log_pos', 300),
    ('intervalo_servicios_pos', 300),
    ('intervalo_eventos_sistema', 900),
    ('pos_log_relativo', os.path.join('Logs', 'GeneraXML.txt')),
    ('pos_carpeta_instalacion', ''),
    ('pos_nombre_proceso', ''),
    ('pos_comando_iniciar', ''),
    ('espera_liveness_segundos', 15),
    # Contra quien sincroniza el reloj cuando se detecta que quedo fuera de ventana.
    # Sale del config real y NO se hardcodea: cada unidad de negocio puede apuntar a un
    # controlador de dominio distinto. Vacio = la autocorreccion queda desactivada y el
    # agente solo lo avisa en su log.
    ('servidor_hora', ''),
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
    parser.add_argument(
        '--token-apertura', default='',
        help='Token de apertura (apps.aperturas) para que esta estación se enrole ya aprobada y con '
             'la configuración de su perfil, sin que nadie la apruebe en el panel. De un solo uso y '
             'con vencimiento. Vacío = enrolamiento normal, pendiente de aprobación manual.',
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
        '--servidor-hora', default='',
        help='Contra quien corregir el reloj si queda fuera de la ventana de comandos '
             '(ej. el controlador de dominio). Vacio = no autocorregir.',
    )
    parser.add_argument(
        '--intervalo-eventos-sistema', type=int, default=900,
        help='Cada cuantos segundos barrer el visor de eventos de Windows. 15 min por defecto: el visor no cambia cada minuto y cada barrido lanza un PowerShell, que en una caja vendiendo no es gratis.',
    )
    parser.add_argument(
        '--intervalo-servicios-pos', type=int, default=300,
        help='Segundos entre chequeos de los servicios externos que consume el POS '
             '(Postgres local y central, Odoo, web service de recargas). Default 300 = '
             '5min. Solo corre si --pos-carpeta-instalacion está configurado.',
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
    parser.add_argument(
        '--hardware-id', default='',
        help='Identidad de hardware a reportar en el enrolamiento, para equipos donde no se puede '
             'leer el MachineGuid del registro (una máquina de desarrollo sin Windows). Vacío = se '
             'lee del registro. DISTINTO POR EQUIPO: repetirlo entre dos equipos anula la guarda '
             'anti-suplantación del servidor, que es exactamente el defecto A-6.',
    )
    args = parser.parse_args()

    _configurar_logging()
    AgentePrueba(args).correr()


if __name__ == '__main__':
    main()
