<#
.SYNOPSIS
    Wrapper de instalar-agente.ps1 para distribución masiva por GPO (Computer
    Startup Script), sin tocar cada estación a mano.

.DESCRIPTION
    instalar-agente.ps1 ya no necesita nada estación-específico: el código de
    estación lo resuelve el propio agente desde el hostname de Windows (debe
    seguir la convención FARMACIA-SUFIJO, ej. ML016-A — ver comentario en
    instalar-agente.ps1). Los 5 parámetros obligatorios de ese script son
    IDÉNTICOS para las ~600 estaciones, así que este wrapper los deja fijos
    acá arriba y le agrega solo dos cosas que instalar-agente.ps1 no resuelve
    por sí solo:

    1. Idempotencia: un GPO Computer Startup Script corre en CADA arranque,
       no una sola vez. Si el servicio ya existe, sale sin hacer nada — sin
       esto, cada reinicio de las 600 estaciones para el servicio, borra el
       registro y lo reinstala (flapping innecesario, ventana muerta de
       monitoreo en cada boot).
    2. Log persistente: un GPO startup script no tiene consola visible. Sin
       transcript, un fallo en una estación puntual entre 600 es invisible
       hasta que alguien nota que esa estación nunca aparece en el panel.

    Requiere completar los valores fijos de abajo ANTES de publicar este
    script en la GPO (mismos valores que MQTT_PASSWORD_AGENTE/
    COMANDO_HMAC_SECRET de deploy/.env en saidsoft-core, y el cert.pem real
    del servidor — NO el de deploy/docs/cert.pem, que es un CA de desarrollo).

.NOTES
    Publicar en Computer Configuration -> Policies -> Windows Settings ->
    Scripts -> Startup, apuntando a este archivo. Corre como SYSTEM (ya
    elevado, cumple el chequeo de administrador de instalar-agente.ps1).

    $PublishFolder y $CaCertPath deben resolver a una ruta alcanzable por
    TODAS las estaciones (share de solo lectura dedicado — no SYSVOL: el
    publish self-contained pesa demasiado para replicar por SYSVOL). Permisos
    de ese share: solo "Domain Computers" + admins, no "Authenticated Users" /
    "Everyone" — MqttPassword y ComandoHmacSecret quedan en texto plano en
    este script, mismo criterio de riesgo ya aceptado hoy (secreto compartido
    por las ~1.800 estaciones, ver deploy/README-produccion.md).
#>

$ServiceName = 'SaidsoftAgente'
$LogPath = 'C:\ProgramData\Saidsoft\desplegar-agente-gpo.log'

# --- Valores fijos del rollout: completar antes de publicar en la GPO ---
$PublishFolder     = '\\<share>\saidsoft-agente\publish'   # dotnet publish, ver instalar-agente.ps1
$CentralHost       = '10.111.6.20'
$MqttPassword      = '<MQTT_PASSWORD_AGENTE de deploy/.env>'
$CaCertPath        = '\\<share>\saidsoft-agente\cert.pem'  # CA real del servidor, NO deploy/docs/cert.pem
$ComandoHmacSecret = '<COMANDO_HMAC_SECRET de deploy/.env>'
# -------------------------------------------------------------------------

New-Item -ItemType Directory -Force -Path (Split-Path $LogPath) | Out-Null
Start-Transcript -Path $LogPath -Append | Out-Null

try {
    if (Get-Service -Name $ServiceName -ErrorAction SilentlyContinue) {
        Write-Host "$(Get-Date -Format s) - Servicio '$ServiceName' ya instalado en $env:COMPUTERNAME, no se hace nada."
        exit 0
    }

    Write-Host "$(Get-Date -Format s) - Instalando agente en $env:COMPUTERNAME..."
    & (Join-Path $PSScriptRoot 'instalar-agente.ps1') `
        -PublishFolder $PublishFolder `
        -CentralHost $CentralHost `
        -MqttPassword $MqttPassword `
        -CaCertPath $CaCertPath `
        -ComandoHmacSecret $ComandoHmacSecret

    Write-Host "$(Get-Date -Format s) - Instalación completada en $env:COMPUTERNAME."
}
catch {
    Write-Host "$(Get-Date -Format s) - ERROR instalando en $env:COMPUTERNAME: $_"
    Stop-Transcript | Out-Null
    exit 1
}

Stop-Transcript | Out-Null
