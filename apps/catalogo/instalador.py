"""El script de instalación de estaciones y el enlace de un solo uso para bajarlo.

`Instalar-Saidsoft.ps1` lleva la `MQTT_PASSWORD_AGENTE` escrita adentro (ver el docstring
de `generar_script_instalacion`), y por eso nunca se publica en `/media/`. Hay dos formas de
obtenerlo, y las dos arman el mismo texto con `generar_script`:

- `manage.py generar_script_instalacion`, en el servidor, para entregarlo por un canal
  privado.
- Un `EnlaceInstalador` emitido desde el panel: el técnico lo baja con `curl.exe` desde la
  propia estación, por HTTPS, una sola vez y dentro de unos minutos. El script se arma en
  el momento y no queda guardado en el servidor. Ver PLAN_MODERNIZACION.md §10-CG.
"""
import hashlib
import secrets
from datetime import timedelta
from pathlib import Path

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from apps.catalogo.models import EnlaceInstalador

# Lo que tarda un técnico entre pedir el enlace en el panel y pegar el comando en la
# estación. Corto a propósito: el enlace es la clave MQTT, y uno que se pidió y no se usó
# no tiene por qué seguir sirviendo mañana.
MINUTOS_VIGENCIA_ENLACE = 30

PLANTILLA = r'''# Instalador de estación SAIDSOFT — generado por `manage.py generar_script_instalacion`.
#
# CONTIENE UN SECRETO (MqttPassword). No lo subas al repo, a /media/ ni a un chat
# abierto: se entrega a cada técnico por un canal privado y se borra al terminar.
#
# Uso, en PowerShell en la estación (NO hace falta abrirlo como Administrador: si no
# lo está, se reabre solo y Windows pide la clave):
#     .\Instalar-Saidsoft.ps1
#     .\Instalar-Saidsoft.ps1 -Codigo ML123-A     # si el hostname no sigue FARMACIA-SUFIJO
#
# El código de estación sale del hostname, que tiene que respetar FARMACIA-SUFIJO
# (ej. ML001-A). Si no lo respeta, pasalo con -Codigo: este script NO renombra el equipo.

param(
    [string]$Codigo = $env:COMPUTERNAME,
    [switch]$SinMeshCentral
)

$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

# --- 0. Autoelevacion -----------------------------------------------------------
# Antes esto cortaba con "Hay que correr esto como Administrador" y el tecnico tenia
# que cerrar, buscar PowerShell, abrirlo elevado y volver a navegar hasta la carpeta.
# En una farmacia, con la caja ocupada, ese rebote es donde se abandona la instalacion.
# Mismo patron que Instalar.bat, que se autoeleva desde el 6-ago-2026.
if (-not ([Security.Principal.WindowsPrincipal] [Security.Principal.WindowsIdentity]::GetCurrent()
        ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {{
    Write-Host 'Se necesitan permisos de administrador, reabriendo...' -ForegroundColor Yellow

    # $PSCommandPath y no $MyInvocation: con -File el segundo puede venir vacio.
    $argumentos = @(
        '-NoProfile',
        # La politica por defecto (RemoteSigned) bloquea un .ps1 que vino por descarga o
        # por OneDrive, que es exactamente como le llega al tecnico. Sin esto la ventana
        # elevada se abre y muere con "no esta firmado digitalmente".
        '-ExecutionPolicy', 'Bypass',
        # -NoExit deja la ventana abierta al terminar. Es lo unico que permite leer si
        # el servicio quedo corriendo: sin esto la consola se cierra sola y el tecnico
        # no ve ni el OK ni el error.
        '-NoExit',
        '-File', ('"{{0}}"' -f $PSCommandPath)
    )
    # Las comillas van SIEMPRE, no solo si hay espacios: la ruta real del tecnico suele
    # ser "...\OneDrive - EMPRESA\Escritorio\Nueva carpeta (4)", con espacios Y
    # parentesis, y sin comillas PowerShell parte el argumento en pedazos.
    if ($Codigo -ne $env:COMPUTERNAME) {{ $argumentos += @('-Codigo', ('"{{0}}"' -f $Codigo)) }}
    if ($SinMeshCentral) {{ $argumentos += '-SinMeshCentral' }}

    try {{
        Start-Process -FilePath 'powershell.exe' -ArgumentList $argumentos -Verb RunAs
    }} catch {{
        # Cancelar el aviso de Windows tira una excepcion cruda que no dice nada util. El
        # script se borra igual: lleva la clave, y para reintentar hace falta otro enlace.
        Remove-Item -LiteralPath $PSCommandPath -Force -ErrorAction SilentlyContinue
        throw "Se cancelo el permiso de administrador. Pedi otro enlace y acepta el aviso de Windows."
    }}
    exit
}}

# Todo lo que sigue va dentro de try/finally: el script lleva la clave MQTT y tiene que
# borrarse aunque la instalacion falle a mitad. Antes el borrado era la ultima linea, y un
# hostname fuera de FARMACIA-SUFIJO -el error mas comun en campo- lo dejaba en %TEMP%.
try {{
if ($Codigo -notmatch '^[A-Z0-9]+-[A-Z0-9]+$') {{
    throw "El codigo '$Codigo' no respeta FARMACIA-SUFIJO (ej. ML001-A). Pasalo con -Codigo."
}}

$carpeta = 'C:\Instalador-Saidsoft'
Write-Host "== Estacion $Codigo ==" -ForegroundColor Cyan

# --- 1. Paquete del agente -------------------------------------------------------
New-Item -ItemType Directory -Force -Path $carpeta | Out-Null
Set-Location $carpeta
Write-Host '[1/4] Descargando el paquete...'
curl.exe -sS -o agente-instalador.zip '{url_paquete}'
if (-not (Test-Path 'agente-instalador.zip')) {{ throw "No se pudo descargar el paquete." }}
# El paquete viaja por HTTP (lo bajan todas las estaciones sin credenciales). Se compara
# contra el SHA-256 que calculo el servidor al armar este script: del zip salen el
# ejecutable que queda como servicio y la CA con la que el agente valida al broker.
if ((Get-FileHash 'agente-instalador.zip' -Algorithm SHA256).Hash -ne '{sha_paquete}') {{
    throw "El paquete descargado no coincide con el publicado (SHA-256). No se instala nada: pedi otro enlace."
}}
Remove-Item 'Saidsoft.Agente.exe','instalar-servicio.ps1','cert.pem' -ErrorAction SilentlyContinue
tar -xf agente-instalador.zip
foreach ($f in @('Saidsoft.Agente.exe','instalar-servicio.ps1','cert.pem')) {{
    if (-not (Test-Path $f)) {{ throw "El paquete no trajo $f." }}
}}

# --- 2. Instalar el servicio -----------------------------------------------------
# Cuantas lineas tenia el log antes de instalar: la comprobacion del paso 4 mira solo lo
# que escribio el agente recien instalado, no un "Enrolado" de hace semanas. Get-Content y
# no [IO.File]::ReadAllLines: el servicio previo puede tener el log abierto, y solo
# Get-Content lo abre compartido para escritura.
$logAgente = 'C:\ProgramData\Saidsoft\agente_prueba.log'
$lineasLogAntes = 0
if (Test-Path $logAgente) {{ $lineasLogAntes = @(Get-Content $logAgente -Encoding UTF8).Count }}

# La clave va por parametro, no por config.txt: asi no queda un archivo con el secreto
# en disco mas tiempo del necesario. instalar-servicio.ps1 escribe el config.json final.
Write-Host '[2/4] Instalando el servicio SaidsoftAgente...'
& .\instalar-servicio.ps1 `
    -PublishFolder $carpeta `
    -CentralHost '{central_host}' `
    -MqttPuerto {mqtt_puerto} `
    -MqttPassword '{mqtt_password}' `
    -CaCertPath (Join-Path $carpeta 'cert.pem') `
    -Codigo $Codigo

# --- 3. MeshCentral --------------------------------------------------------------
if ($SinMeshCentral) {{
    Write-Host '[3/4] MeshCentral omitido (-SinMeshCentral).'
}} else {{
    Write-Host '[3/4] Instalando el agente de MeshCentral...'
    $rutaMesh = Join-Path $env:TEMP 'meshagent.exe'
    # curl.exe y no Invoke-WebRequest: PowerShell 5.1 no completa la descarga contra el
    # certificado autofirmado de MeshCentral (comprobado en ML006-A y MC001-B).
    curl.exe -k -sS -o $rutaMesh '{url_mesh}'
    if (-not (Test-Path $rutaMesh)) {{ throw "No se pudo descargar el agente de MeshCentral." }}
    # -fullinstall es obligatorio: sin el, meshagent.exe no registra el servicio.
    # --agentName hace que el nodo aparezca con el codigo de la estacion y se vincule
    # solo, sin copiar el node_id a mano desde la consola web.
    Start-Process -FilePath $rutaMesh -ArgumentList '-fullinstall',"--agentName=$Codigo" -Wait
    Remove-Item $rutaMesh -Force -ErrorAction SilentlyContinue
}}

# --- 4. Comprobacion -------------------------------------------------------------
Write-Host '[4/4] Comprobando...'
# Se espera hasta 30 s y no una sola mirada a los 5 s. Al conectarse, el agente puede
# recibir el actualizar_agente retenido y reemplazarse solo (net stop, copia, net start:
# unos 5 s). Si la mirada cae en ese hueco, el servicio figura detenido aunque la
# instalacion salio bien -- MSI01-E, 18-oct-2026: el paquete traia la 0.34, la estacion
# ya tenia retenida la 0.35, y el tecnico vio FALLA con el agente sano.
# En el mismo bucle se espera el resultado del enrolamiento, que llega por MQTT unos
# segundos despues del arranque.
$corriendo = $false
$enrolamiento = $null
for ($i = 0; $i -lt 10; $i++) {{
    Start-Sleep -Seconds 3
    $svc = Get-Service -Name 'SaidsoftAgente' -ErrorAction SilentlyContinue
    $corriendo = [bool]($svc -and $svc.Status -eq 'Running')
    if (Test-Path $logAgente) {{
        $nuevas = @(Get-Content $logAgente -Encoding UTF8 | Select-Object -Skip $lineasLogAntes)
        $linea = $nuevas | Where-Object {{ $_ -match 'Enrolado\. estado_aprobacion=|Ya tengo identidad guardada' }} | Select-Object -Last 1
        if ($linea -match 'estado_aprobacion=(\S+)') {{ $enrolamiento = $Matches[1] }}
        elseif ($linea) {{ $enrolamiento = 'ya_enrolada' }}
    }}
    if ($corriendo -and $enrolamiento) {{ break }}
}}
if ($corriendo) {{
    Write-Host "  OK  Servicio SaidsoftAgente corriendo." -ForegroundColor Green
}} else {{
    Write-Host "  FALLA  El servicio no esta corriendo. Mira $logAgente" -ForegroundColor Red
}}
if (-not $SinMeshCentral) {{
    $mesh = Get-Service -Name 'Mesh Agent' -ErrorAction SilentlyContinue
    if ($mesh -and $mesh.Status -eq 'Running') {{
        Write-Host "  OK  Mesh Agent corriendo." -ForegroundColor Green
    }} else {{
        Write-Host "  AVISO  Mesh Agent no quedo corriendo." -ForegroundColor Yellow
    }}
}}

Write-Host ''
# Antes decia PENDIENTE siempre, y una reinstalacion o una estacion con token de apertura
# quedan aprobadas: el tecnico salia a buscar en el panel algo que no tenia que hacer.
switch ($enrolamiento) {{
    'aprobada' {{
        Write-Host "Listo. $Codigo quedo enrolada y APROBADA: ya recibe comandos." -ForegroundColor Green
    }}
    'ya_enrolada' {{
        Write-Host "Listo. $Codigo ya estaba enrolada y conserva su estado en el panel:" -ForegroundColor Green
        Write-Host '  {url_panel}'
    }}
    'pendiente' {{
        Write-Host "Listo. $Codigo queda PENDIENTE DE APROBACION en el panel:" -ForegroundColor Cyan
        Write-Host '  {url_panel}'
        Write-Host 'Hasta aprobarla, la estacion se enrola pero no recibe comandos.'
    }}
    default {{
        if ($enrolamiento) {{ $estado = "quedo en estado '$enrolamiento'" }} else {{ $estado = 'no confirmo el enrolamiento en 30 s' }}
        Write-Host "  AVISO  $Codigo $estado. Revisa el panel y $logAgente" -ForegroundColor Yellow
        Write-Host '  {url_panel}'
    }}
}}
Write-Host ''
}} finally {{
    # Se borra solo: lleva la clave MQTT y dejarlo en el equipo depende de que alguien se
    # acuerde de borrarlo. El paquete del agente que queda en la carpeta no tiene secretos.
    Remove-Item -LiteralPath $PSCommandPath -Force -ErrorAction SilentlyContinue
    Write-Host 'El instalador se borro solo (llevaba la clave MQTT).' -ForegroundColor Yellow
}}
'''


class ScriptNoDisponible(Exception):
    """Falta algo del servidor para armar el script (la clave o una versión del agente)."""


def generar_script(password: str, *, central_host: str = '', mqtt_puerto: int = 8081) -> str:
    """Arma `Instalar-Saidsoft.ps1` con la clave dada. No guarda nada."""
    from apps.catalogo.models import VersionAgente

    if not password:
        raise ScriptNoDisponible('Falta MQTT_PASSWORD_AGENTE en el servidor.')
    if not VersionAgente.objects.exists():
        raise ScriptNoDisponible('No hay ninguna VersionAgente cargada; no hay paquete que instalar.')

    paquete = Path(settings.MEDIA_ROOT) / 'agente-instalador' / 'agente-instalador.zip'
    if not paquete.is_file():
        raise ScriptNoDisponible('No está publicado el paquete del agente (correr armar_paquete_agente).')
    sha_paquete = hashlib.sha256(paquete.read_bytes()).hexdigest()

    base = settings.ARCHIVOS_BASE_URL.rstrip('/')
    conf = settings.MESHCENTRAL_CONFIG
    url_mesh = (
        f"{conf['SERVER_URL']}/meshagents?id={conf['AGENT_ARCH_ID']}"
        f"&meshid={conf['MESH_ID']}&installflags={conf['INSTALL_FLAGS']}"
    )
    return PLANTILLA.format(
        url_paquete=f'{base}/media/agente-instalador/agente-instalador.zip',
        central_host=central_host or base.split('//')[-1].split(':')[0],
        mqtt_puerto=mqtt_puerto,
        mqtt_password=_escapar_comillas_powershell(password),
        sha_paquete=sha_paquete,
        url_mesh=url_mesh,
        url_panel=f'{base}/estaciones/',
    )


# PowerShell cierra una cadena entre comillas simples no solo con ' sino también con las
# tipográficas ‘ ’ ‚ ‛, y en las cuatro se escapa duplicándolas. Escapar solo la recta
# dejaba que una clave con ’ cortara la cadena e inyectara código en el script.
_COMILLAS_SIMPLES_POWERSHELL = "'‘’‚‛"


def _escapar_comillas_powershell(texto: str) -> str:
    for comilla in _COMILLAS_SIMPLES_POWERSHELL:
        texto = texto.replace(comilla, comilla * 2)
    return texto


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode('utf-8')).hexdigest()


def emitir_enlace(usuario) -> tuple[EnlaceInstalador, str]:
    """Crea el enlace y devuelve `(instancia, token_en_claro)`. El token no se persiste."""
    token = secrets.token_urlsafe(32)
    enlace = EnlaceInstalador.objects.create(
        token_hash=_hash(token), prefijo=token[:8], creado_por=usuario,
        expira_en=timezone.now() + timedelta(minutes=MINUTOS_VIGENCIA_ENLACE),
    )
    return enlace, token


def consumir_enlace(token: str, *, ip: str | None = None) -> EnlaceInstalador | None:
    """Marca el enlace como usado y lo devuelve, o None si no existe, venció o ya se usó.

    Es un UPDATE condicional y no leer-y-después-guardar: dos descargas simultáneas del
    mismo enlace no pueden llevarse las dos el script. Lo que no se distingue hacia afuera
    es POR QUÉ no sirve: a quien no tiene un enlace válido no hay nada que explicarle.
    """
    ahora = timezone.now()
    with transaction.atomic():
        marcados = EnlaceInstalador.objects.filter(
            token_hash=_hash(token), usado_en__isnull=True, expira_en__gt=ahora,
        ).update(usado_en=ahora, ip_uso=ip)
        if not marcados:
            return None
        return EnlaceInstalador.objects.get(token_hash=_hash(token))
