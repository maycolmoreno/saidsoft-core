<#
.SYNOPSIS
    Instala el agente SAIDSOFT como servicio de Windows en esta estación.

.DESCRIPTION
    No existe todavía un instalador MSI (ver README.md del proyecto), así que este
    script hace a mano lo que haría uno: copia el publish self-contained a
    Program Files, confía en el certificado del broker MQTT (self-signed en el
    piloto), escribe la configuración de conexión al central, registra el servicio
    de Windows y lo arranca.

    Debe correr como Administrador en CADA estación (o distribuirse por GPO/RMM).
    El código de estación sale del hostname de Windows (debe seguir la convención
    FARMACIA-SUFIJO, ej. ML001-A) — este script NO renombra el equipo.

.PARAMETER PublishFolder
    Carpeta con el publish self-contained del agente (contiene Saidsoft.Agente.exe).
    Generarla una sola vez en la máquina de build con:
        dotnet publish src\Saidsoft.Agente\Saidsoft.Agente.csproj -c Release -r win-x64 `
            --self-contained true -p:PublishSingleFile=true -o publish\agente
    y copiar esa carpeta (o un .zip) a la estación antes de correr este script.

.PARAMETER CentralHost
    Host o IP del central (el mismo que ALLOWED_HOSTS/ARCHIVOS_BASE_URL de saidsoft-core).

.PARAMETER MqttPassword
    Password del usuario MQTT "saidsof_agente" (deploy/.env -> MQTT_PASSWORD_AGENTE
    en saidsoft-core). Se pide en la línea de comandos para no dejarla escrita en
    ningún script versionado.

.PARAMETER CaCertPath
    Ruta al cert.pem de EMQX (deploy/certs/cert.pem en saidsoft-core). Necesario
    mientras EMQX use el certificado self-signed del piloto; con un CA público
    real este paso deja de hacer falta.

.PARAMETER ComandoHmacSecret
    Debe ser IDÉNTICO a COMANDO_HMAC_SECRET en deploy/.env de saidsoft-core. Sin
    esto el agente arranca y hace heartbeat con normalidad, pero descarta en
    silencio todo comando remoto (reiniciar/ejecutar_script/consultar_info) por
    firma HMAC inválida — no hay error visible en el panel, solo el comando nunca
    se ejecuta. Es el error más fácil de no notar de todo el instalador.

.PARAMETER PosCarpetaInstalacion
    Carpeta donde vive el POS real (Farmamia/Elipsys) en la estación. Mismo valor
    para San Gregorio/MIA/7DIAS — el POS es el mismo ejecutable para las 3
    unidades de negocio; lo que cambia por estación es el nodo TRX de su propio
    archivo de configuración interno, no esta ruta.

.PARAMETER PosNombreProceso
    Nombre del proceso SIN extensión .exe (Process.GetProcessesByName lo exige
    así) — se usa para saber si el POS sigue vivo tras aplicar un despliegue y
    disparar el rollback automático si no.

.PARAMETER PosComandoIniciar
    Ruta completa al .exe que el agente relanza tras aplicar un despliegue.

.EXAMPLE
    .\instalar-agente.ps1 -PublishFolder .\publish\agente -CentralHost 10.111.15.50 `
        -MqttPassword "el-password-real" -CaCertPath .\cert.pem `
        -ComandoHmacSecret "el-secreto-compartido-con-el-panel"
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory)] [string]$PublishFolder,
    [Parameter(Mandatory)] [string]$CentralHost,
    [Parameter(Mandatory)] [string]$MqttPassword,
    [Parameter(Mandatory)] [string]$CaCertPath,
    [Parameter(Mandatory)] [string]$ComandoHmacSecret,

    [string]$MqttUsuario = "saidsof_agente",
    [int]$MqttPuerto = 8883,
    [string]$InstallPath = "C:\Program Files\Saidsoft\Agente",
    [string]$ServiceName = "SaidsoftAgente",

    # Mismo POS (Farmamia Cia Ltda - Elipsys, Zabyca.Pos.Desktop.exe) para las 3
    # unidades de negocio (SG/MIA/7DIAS) — confirmado con el usuario 31-jul-2026.
    # Quedan como parámetro, no hardcodeados, por si algún POS cambia a futuro.
    [string]$PosCarpetaInstalacion = "C:\Program Files (x86)\Farmamia Cia Ltda - Elipsys\Cliente",
    [string]$PosNombreProceso = "Zabyca.Pos.Desktop",
    [string]$PosComandoIniciar = "C:\Program Files (x86)\Farmamia Cia Ltda - Elipsys\Cliente\Zabyca.Pos.Desktop.exe"
)

$ErrorActionPreference = "Stop"

if (-not ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw "Este script debe correr como Administrador (clic derecho -> Ejecutar como administrador)."
}
if (-not (Test-Path (Join-Path $PublishFolder "Saidsoft.Agente.exe"))) {
    throw "No se encontró Saidsoft.Agente.exe en '$PublishFolder'. ¿Ya corriste el dotnet publish? Ver ayuda: Get-Help .\instalar-agente.ps1 -Full"
}
if (-not (Test-Path $CaCertPath)) {
    throw "No se encontró el certificado CA en '$CaCertPath'."
}
if ($PosNombreProceso.EndsWith(".exe", [StringComparison]::OrdinalIgnoreCase)) {
    throw "PosNombreProceso debe ir SIN '.exe' (Process.GetProcessesByName lo exige así); recibido: '$PosNombreProceso'."
}
if (-not (Test-Path $PosCarpetaInstalacion)) {
    Write-Warning "No se encontró '$PosCarpetaInstalacion' en esta estación. El agente igual se instala, pero el rollback/liveness-check del POS no va a funcionar hasta que el POS esté instalado ahí. Confirma la ruta antes de dar por buena esta estación."
}

Write-Host "1) Confiando en el certificado de EMQX ($CaCertPath)..." -ForegroundColor Cyan
Import-Certificate -FilePath $CaCertPath -CertStoreLocation Cert:\LocalMachine\Root | Out-Null

Write-Host "2) Deteniendo servicio previo si existe..." -ForegroundColor Cyan
if (Get-Service -Name $ServiceName -ErrorAction SilentlyContinue) {
    Stop-Service -Name $ServiceName -Force -ErrorAction SilentlyContinue
    sc.exe delete $ServiceName | Out-Null
    Start-Sleep -Seconds 1
}

Write-Host "3) Copiando binarios a $InstallPath..." -ForegroundColor Cyan
New-Item -ItemType Directory -Force -Path $InstallPath | Out-Null
Copy-Item -Path (Join-Path $PublishFolder "*") -Destination $InstallPath -Recurse -Force

Write-Host "4) Escribiendo configuración de producción..." -ForegroundColor Cyan
$config = @{
    Logging = @{ LogLevel = @{ Default = "Information"; "Microsoft.Hosting.Lifetime" = "Information" } }
    Agente  = @{
        MqttHost             = $CentralHost
        MqttPuerto           = $MqttPuerto
        MqttUsuario          = $MqttUsuario
        MqttPassword         = $MqttPassword
        MqttUsarTls          = $true
        ComandoHmacSecret    = $ComandoHmacSecret
        CarpetaInstalacionPos = $PosCarpetaInstalacion
        NombreProcesoPos     = $PosNombreProceso
        ComandoIniciarPos    = $PosComandoIniciar
    }
}
# appsettings.Production.json pisa a appsettings.json; el ambiente por defecto del
# Generic Host ya es "Production" cuando no se define DOTNET_ENVIRONMENT, así que
# se aplica solo, sin tocar variables de entorno del servicio.
$config | ConvertTo-Json -Depth 5 | Set-Content -Path (Join-Path $InstallPath "appsettings.Production.json") -Encoding utf8

Write-Host "5) Registrando el servicio de Windows..." -ForegroundColor Cyan
$exePath = Join-Path $InstallPath "Saidsoft.Agente.exe"
New-Service -Name $ServiceName -BinaryPathName "`"$exePath`"" -DisplayName "Saidsoft Agente" `
    -Description "Agente de despliegue y monitoreo SAIDSOFT" -StartupType Automatic | Out-Null
sc.exe failure $ServiceName reset= 86400 actions= restart/30000/restart/60000/restart/120000 | Out-Null

Write-Host "6) Iniciando el servicio..." -ForegroundColor Cyan
Start-Service -Name $ServiceName
Start-Sleep -Seconds 2
Get-Service -Name $ServiceName | Format-Table -AutoSize

Write-Host ""
Write-Host "Listo. Revisa el Visor de eventos (Application) o" -ForegroundColor Green
Write-Host "C:\ProgramData\Saidsoft\ para confirmar el enrolamiento, y aprueba" -ForegroundColor Green
Write-Host "la estación en el panel (/estaciones/) cuando aparezca como pendiente." -ForegroundColor Green
