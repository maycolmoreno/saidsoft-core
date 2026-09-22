"""Genera el script de PowerShell que corren los técnicos para instalar una estación.

    # en el servidor, desde deploy/
    docker compose exec -T -e MQTT_PASSWORD_AGENTE="$(grep ^MQTT_PASSWORD_AGENTE= .env | cut -d= -f2-)" \
        web python manage.py generar_script_instalacion > Instalar-Saidsoft.ps1

El script resultante hace, en una sola corrida y sin preguntar nada:

  1. baja el paquete del agente de /media/ y lo descomprime
  2. escribe config.txt **con la MqttPassword ya puesta**
  3. instala el servicio SaidsoftAgente
  4. instala el agente de MeshCentral con `--agentName=<código>`, que es lo que hace que
     el nodo se vincule solo a la estación sin copiar node_id a mano

**Por qué esto y no meter la clave en el zip publicado.** `armar_paquete_agente` deja
`MqttPassword` afuera a propósito: `/media/` se sirve por HTTP **sin autenticación**
—tiene que serlo, es de donde descargan los agentes— así que todo lo que entre al zip
queda legible para cualquiera que alcance el servidor.

Y no alcanza con angostar la ACL del usuario compartido: aun después de correr
`deploy/emqx-narrow-acl-agente.sh`, esa credencial conserva `subscribe` sobre
`/saidsof/enrolamiento/respuesta/+/` **con comodín**, así que quien la tenga puede
escuchar la respuesta de enrolamiento de CUALQUIER estación y quedarse con su
credencial propia y su secreto HMAC. Publicarla sería regalar esa capacidad.

Este script, en cambio, se le entrega a cada técnico por un canal privado. Es el mismo
secreto, pero deja de estar publicado: quien lo tiene es alguien a quien ya se lo
confiaste.

**Nunca lo escribas dentro del repo ni lo subas a /media/.**
"""
import os

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from apps.catalogo.models import VersionAgente

PLANTILLA = r'''# Instalador de estación SAIDSOFT — generado por `manage.py generar_script_instalacion`.
#
# CONTIENE UN SECRETO (MqttPassword). No lo subas al repo, a /media/ ni a un chat
# abierto: se entrega a cada técnico por un canal privado y se borra al terminar.
#
# Uso, en PowerShell COMO ADMINISTRADOR, en la estación:
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

if (-not ([Security.Principal.WindowsPrincipal] [Security.Principal.WindowsIdentity]::GetCurrent()
        ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {{
    throw "Hay que correr esto como Administrador."
}}

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
Remove-Item 'Saidsoft.Agente.exe','instalar-servicio.ps1','cert.pem' -ErrorAction SilentlyContinue
tar -xf agente-instalador.zip
foreach ($f in @('Saidsoft.Agente.exe','instalar-servicio.ps1','cert.pem')) {{
    if (-not (Test-Path $f)) {{ throw "El paquete no trajo $f." }}
}}

# --- 2. Instalar el servicio -----------------------------------------------------
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
Start-Sleep -Seconds 5
$svc = Get-Service -Name 'SaidsoftAgente' -ErrorAction SilentlyContinue
if ($svc -and $svc.Status -eq 'Running') {{
    Write-Host "  OK  Servicio SaidsoftAgente corriendo." -ForegroundColor Green
}} else {{
    Write-Host "  FALLA  El servicio no esta corriendo. Mira C:\ProgramData\Saidsoft\agente_prueba.log" -ForegroundColor Red
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
Write-Host "Listo. $Codigo queda PENDIENTE DE APROBACION en el panel:" -ForegroundColor Cyan
Write-Host '  {url_panel}'
Write-Host 'Hasta aprobarla, la estacion se enrola pero no recibe comandos.'
Write-Host ''
Write-Host 'Borra esta carpeta cuando termines: el instalador lleva la clave MQTT.' -ForegroundColor Yellow
'''


class Command(BaseCommand):
    help = 'Genera el script de PowerShell de instalación (incluye la MqttPassword — canal privado).'

    def add_arguments(self, parser):
        parser.add_argument(
            '--mqtt-password', default='',
            help='MQTT_PASSWORD_AGENTE. Si se omite, se lee de la variable de entorno del mismo nombre.',
        )
        parser.add_argument(
            '--central-host', default='',
            help='Host del central. Por defecto se deduce de ARCHIVOS_BASE_URL.',
        )
        parser.add_argument('--mqtt-puerto', type=int, default=8081)

    def handle(self, *args, **opciones):
        password = opciones['mqtt_password'] or os.environ.get('MQTT_PASSWORD_AGENTE', '')
        if not password:
            raise CommandError(
                'Falta la clave. Pasala con --mqtt-password o exportá MQTT_PASSWORD_AGENTE.\n'
                'Nunca la escribas en el repo: vive solo en deploy/.env del servidor.',
            )

        version = VersionAgente.objects.order_by('-fecha_creacion').first()
        if version is None:
            raise CommandError('No hay ninguna VersionAgente cargada; no hay paquete que instalar.')

        base = settings.ARCHIVOS_BASE_URL.rstrip('/')
        central_host = opciones['central_host'] or base.split('//')[-1].split(':')[0]

        conf = settings.MESHCENTRAL_CONFIG
        if not conf.get('MESH_ID'):
            self.stderr.write(
                'AVISO: MESHCENTRAL_MESH_ID no está configurado — el script va a quedar '
                'sin la parte de MeshCentral utilizable.',
            )
        url_mesh = (
            f"{conf['SERVER_URL']}/meshagents?id={conf['AGENT_ARCH_ID']}"
            f"&meshid={conf['MESH_ID']}&installflags={conf['INSTALL_FLAGS']}"
        )

        self.stdout.write(PLANTILLA.format(
            url_paquete=f'{base}/media/agente-instalador/agente-instalador.zip',
            central_host=central_host,
            mqtt_puerto=opciones['mqtt_puerto'],
            mqtt_password=password.replace("'", "''"),  # escape de PowerShell
            url_mesh=url_mesh,
            url_panel=f'{base}/estaciones/',
        ))
        self.stderr.write(
            f'\nGenerado para {version.version}. CONTIENE LA CLAVE MQTT: entregalo por un '
            'canal privado y no lo subas al repo ni a /media/.',
        )
