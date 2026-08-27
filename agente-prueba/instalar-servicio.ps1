<#
.SYNOPSIS
    Instala el agente SAIDSOFT (Python, servicio_windows.py) como servicio de Windows
    en esta estación.

.DESCRIPTION
    Equivalente a deploy/docs/instalar-agente.ps1 del agente C# original, pero para el
    reemplazo en Python (ver docstring de agente_prueba.py y PLAN_MODERNIZACION.md
    §10-K sobre por qué existe este reemplazo). Usa el mismo nombre de servicio
    ("SaidsoftAgente") a propósito — reemplaza en el lugar cualquier instalación previa
    del agente C#, no convive con ella.

    A diferencia del original, NO hace falta `Import-Certificate` al store de Windows:
    el agente en Python (paho-mqtt) valida TLS contra el archivo cert.pem directamente
    vía la librería ssl de Python, no contra el store de certificados del SO.

    Debe correr como Administrador en CADA estación. El código de estación sale del
    hostname de Windows por default (debe seguir la convención FARMACIA-SUFIJO, ej.
    ML001-A) — este script NO renombra el equipo.

.PARAMETER PublishFolder
    Carpeta con dist\Saidsoft.Agente.exe y cert.pem (salida de build.ps1 + copiar el
    cert.pem de deploy/certs/cert.pem del servidor).

.PARAMETER CentralHost
    Host o IP del central (el mismo que ARCHIVOS_BASE_URL de saidsoft-core).

.PARAMETER MqttPuerto
    Puerto TLS de EMQX expuesto por el servidor. En el piloto (deploy/docker-compose.yml)
    es 8081, no el 8883 de siempre — EMQX está remapeado porque el firewall del servidor
    solo abre 8080-8085 (mismo detalle que deploy/docs/prueba-agente/paquete-instalacion
    /Instalar.bat ya documentaba para el agente C#).

.PARAMETER MqttPassword
    Password del usuario MQTT "saidsof_agente" (deploy/.env -> MQTT_PASSWORD_AGENTE en
    saidsoft-core).

.PARAMETER CaCertPath
    Ruta al cert.pem de EMQX (deploy/certs/cert.pem en saidsoft-core).

.PARAMETER ComandoHmacSecret
    Debe ser IDÉNTICO a COMANDO_HMAC_SECRET en deploy/.env de saidsoft-core. Sin esto
    el agente arranca y hace heartbeat con normalidad, pero descarta en silencio los
    comandos ejecutar_script por firma HMAC inválida.

.PARAMETER PosCarpetaInstalacion
    Carpeta donde vive el POS real (Farmamia/Elipsys) en la estación.

.PARAMETER PosNombreProceso
    Nombre del proceso SIN extensión .exe.

.PARAMETER PosComandoIniciar
    Ruta completa al .exe que el agente relanza tras aplicar un despliegue.

.EXAMPLE
    .\instalar-servicio.ps1 -PublishFolder .\dist -CentralHost 10.111.6.20 `
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

    [string]$Codigo = $env:COMPUTERNAME,
    [string]$MqttUsuario = "saidsof_agente",
    [int]$MqttPuerto = 8081,
    [int]$IntervaloHeartbeat = 60,
    [string]$InstallPath = "C:\Program Files\Saidsoft\Agente",

    # Servidor de hora al que sincronizar la estación (ver paso 2). Vacío = no tocar la
    # hora. El agente descarta cualquier comando firmado con más de
    # VENTANA_TIMESTAMP_SEGUNDOS (120s) de desfase contra su propio reloj — protección
    # anti-replay, ver agente_prueba.timestamp_en_ventana. Una estación con la hora
    # corrida ignora en silencio TODO script/comando del panel, y el único rastro queda
    # en su log local ("timestamp fuera de ventana"), no en el panel. Encontrado en
    # producción en ML016-B (26-ago-2026).
    [string]$ServidorHora = "farmaciasmia.int",

    # Mismo POS (Farmamia Cia Ltda - Elipsys, Zabyca.Pos.Desktop.exe) para las 3
    # unidades de negocio (SG/MIA/7DIAS) — mismo valor que instalar-agente.ps1 original.
    [string]$PosCarpetaInstalacion = "C:\Program Files (x86)\Farmamia Cia Ltda - Elipsys\Cliente",
    [string]$PosNombreProceso = "Zabyca.Pos.Desktop",
    [string]$PosComandoIniciar = "C:\Program Files (x86)\Farmamia Cia Ltda - Elipsys\Cliente\Zabyca.Pos.Desktop.exe"
)

$ErrorActionPreference = "Stop"
$NombreServicio = "SaidsoftAgente"

if (-not ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw "Este script debe correr como Administrador (clic derecho -> Ejecutar como administrador)."
}
if (-not (Test-Path (Join-Path $PublishFolder "Saidsoft.Agente.exe"))) {
    throw "No se encontró Saidsoft.Agente.exe en '$PublishFolder'. ¿Ya corriste .\build.ps1? Revisa dist\."
}
if (-not (Test-Path $CaCertPath)) {
    throw "No se encontró el certificado CA en '$CaCertPath'."
}
if ($PosNombreProceso.EndsWith(".exe", [StringComparison]::OrdinalIgnoreCase)) {
    throw "PosNombreProceso debe ir SIN '.exe'; recibido: '$PosNombreProceso'."
}
if (-not (Test-Path $PosCarpetaInstalacion)) {
    Write-Warning "No se encontró '$PosCarpetaInstalacion' en esta estación. El agente igual se instala, pero los despliegues de POS van a fallar hasta que el POS esté instalado ahí."
}

Write-Host "0) Sincronizando la hora de la estación..." -ForegroundColor Cyan
if ([string]::IsNullOrWhiteSpace($ServidorHora)) {
    Write-Host "   (omitido: no se indicó ServidorHora)" -ForegroundColor DarkGray
} else {
    # Nunca fatal: una estación con la hora corrida es un problema real (descarta todos
    # los comandos del panel, ver comentario en el parámetro), pero no es motivo para
    # abortar la instalación del agente -- se avisa y se sigue.
    try {
        # W32Time viene deshabilitado en algunas imágenes de Windows; sin esto,
        # w32tm /resync falla con "El servicio no se ha iniciado".
        Set-Service -Name W32Time -StartupType Automatic -ErrorAction Stop
        Start-Service -Name W32Time -ErrorAction SilentlyContinue

        # Se configura el peer ADEMÁS de sincronizar ahora: "net time /set" (lo que se
        # venía haciendo a mano) corrige el reloj una vez, pero no evita que se vuelva a
        # desviar. Con esto Windows lo mantiene sincronizado solo de ahí en adelante.
        & w32tm.exe /config /manualpeerlist:"$ServidorHora" /syncfromflags:manual /update | Out-Null
        & w32tm.exe /resync /force | Out-Null

        if ($LASTEXITCODE -eq 0) {
            Write-Host "   Hora sincronizada contra $ServidorHora (ahora: $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss'))." -ForegroundColor DarkGray
        } else {
            # w32tm puede fallar si el peer no responde NTP pero sí SMB — "net time" es
            # el camino que ya se sabe que funciona en esta red, se usa como respaldo.
            & net.exe time "\\$ServidorHora" /set /yes | Out-Null
            if ($LASTEXITCODE -eq 0) {
                Write-Host "   Hora sincronizada vía 'net time' (ahora: $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss'))." -ForegroundColor DarkGray
            } else {
                Write-Warning "No se pudo sincronizar la hora contra '$ServidorHora'. La instalación sigue, PERO si el reloj está corrido más de 2 minutos la estación va a ignorar todos los scripts y comandos del panel. Corregir a mano y reintentar."
            }
        }
    } catch {
        Write-Warning "No se pudo sincronizar la hora ($($_.Exception.Message)). La instalación sigue -- ver la advertencia de arriba sobre el impacto."
    }
}

Write-Host "1) Deteniendo/quitando servicio previo si existe (agente C# o Python)..." -ForegroundColor Cyan
if (Get-Service -Name $NombreServicio -ErrorAction SilentlyContinue) {
    Stop-Service -Name $NombreServicio -Force -ErrorAction SilentlyContinue
    # sc.exe delete funciona sin importar qué haya registrado el servicio originalmente
    # (New-Service del agente C#, o Saidsoft.Agente.exe install de pywin32) — el SCM no
    # distingue, borra por nombre.
    sc.exe delete $NombreServicio | Out-Null
    Start-Sleep -Seconds 1
}

Write-Host "2) Copiando binarios a $InstallPath..." -ForegroundColor Cyan
New-Item -ItemType Directory -Force -Path $InstallPath | Out-Null
Copy-Item -Path (Join-Path $PublishFolder "Saidsoft.Agente.exe") -Destination $InstallPath -Force
Copy-Item -Path $CaCertPath -Destination (Join-Path $InstallPath "cert.pem") -Force

Write-Host "3) Escribiendo config.json..." -ForegroundColor Cyan
$config = [ordered]@{
    codigo                   = $Codigo
    host                     = $CentralHost
    puerto                   = $MqttPuerto
    usuario                  = $MqttUsuario
    password                 = $MqttPassword
    tls                      = $true
    ca_cert                  = (Join-Path $InstallPath "cert.pem")
    hmac_secret               = $ComandoHmacSecret
    intervalo_heartbeat      = $IntervaloHeartbeat
    pos_carpeta_instalacion  = $PosCarpetaInstalacion
    pos_nombre_proceso       = $PosNombreProceso
    pos_comando_iniciar      = $PosComandoIniciar
    espera_liveness_segundos = 15
}
$config | ConvertTo-Json | Set-Content -Path (Join-Path $InstallPath "config.json") -Encoding utf8

Write-Host "4) Registrando el servicio de Windows..." -ForegroundColor Cyan
$exePath = Join-Path $InstallPath "Saidsoft.Agente.exe"

# Start-Process -Wait -PassThru, NO "& $exePath": Saidsoft.Agente.exe se compila como
# aplicación *windowed* (console=False en agente_prueba.spec, bootloader runw.exe de
# PyInstaller). PowerShell NO espera a un ejecutable de subsistema GUI invocado con
# "&" -- retorna al instante y $LASTEXITCODE queda en $null, así que el chequeo
# "-ne 0" daba verdadero SIEMPRE y este script abortaba con "Falló el registro del
# servicio (código )" (código vacío = la pista) aunque el servicio se hubiera
# registrado bien. Encontrado instalando en ML016-B y ML027-ADM (26-ago-2026): la
# instalación funcionaba de verdad, pero el script moría acá y nunca llegaba a
# configurar el reinicio automático ni a iniciar el servicio.
$proc = Start-Process -FilePath $exePath -ArgumentList '--startup','auto','install' -Wait -PassThru -NoNewWindow
if ($proc.ExitCode -ne 0) { throw "Falló el registro del servicio (código $($proc.ExitCode))." }
# pywin32 no expone política de reinicio automático al fallar — misma configuración que
# usaba instalar-agente.ps1 para el agente C#.
sc.exe failure $NombreServicio reset= 86400 actions= restart/30000/restart/60000/restart/120000 | Out-Null

Write-Host "5) Iniciando el servicio..." -ForegroundColor Cyan
# Mismo motivo que arriba: Start-Process en vez de "&". Se usa "net start" (y no
# "$exePath start") porque no depende del bootloader windowed en absoluto.
Start-Process -FilePath 'net.exe' -ArgumentList 'start',$NombreServicio -Wait -NoNewWindow
Start-Sleep -Seconds 2
Get-Service -Name $NombreServicio | Format-Table -AutoSize

Write-Host ""
Write-Host "Listo. Revisa el Visor de eventos (Application, origen 'SaidsoftAgente') o" -ForegroundColor Green
Write-Host "$InstallPath\agente_prueba.log para confirmar el enrolamiento, y aprueba" -ForegroundColor Green
Write-Host "la estación en el panel (/estaciones/) cuando aparezca como pendiente." -ForegroundColor Green
