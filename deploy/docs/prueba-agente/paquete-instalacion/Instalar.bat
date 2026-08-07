@echo off
setlocal enabledelayedexpansion

rem Doble clic para instalar el agente SAIDSOFT en esta estación, sin escribir
rem nada en consola. Requiere que junto a este .bat estén (ver README.md de
rem esta carpeta para armar el paquete):
rem   agente\                 (publish self-contained del agente)
rem   cert.pem                (CA de EMQX)
rem   instalar-agente.ps1     (copiado de ..\instalar-agente.ps1)
rem   config.txt              (copiado de config.ejemplo.txt, con los valores reales)

cd /d "%~dp0"

rem Auto-elevar a Administrador si hace falta (instalar-agente.ps1 lo exige).
net session >nul 2>&1
if not %errorlevel%==0 (
    echo Se necesitan permisos de administrador, reabriendo...
    powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
    exit /b
)

if not exist "config.txt" (
    echo No se encontro config.txt en esta carpeta.
    echo Copia config.ejemplo.txt como config.txt y completa los valores reales antes de correr esto.
    pause
    exit /b 1
)
if not exist "agente\Saidsoft.Agente.exe" (
    echo No se encontro agente\Saidsoft.Agente.exe. Falta copiar la carpeta "agente" junto a este .bat.
    pause
    exit /b 1
)
if not exist "cert.pem" (
    echo No se encontro cert.pem junto a este .bat.
    pause
    exit /b 1
)
if not exist "instalar-agente.ps1" (
    echo No se encontro instalar-agente.ps1 junto a este .bat.
    pause
    exit /b 1
)

set "CENTRAL_HOST="
set "MQTT_PUERTO="
set "MQTT_PASSWORD="
set "HMAC_SECRET="
for /f "usebackq tokens=1,* delims==" %%A in ("config.txt") do (
    if /i "%%A"=="CentralHost" set "CENTRAL_HOST=%%B"
    if /i "%%A"=="MqttPuerto" set "MQTT_PUERTO=%%B"
    if /i "%%A"=="MqttPassword" set "MQTT_PASSWORD=%%B"
    if /i "%%A"=="ComandoHmacSecret" set "HMAC_SECRET=%%B"
)
if "%CENTRAL_HOST%"=="" (
    echo config.txt no tiene una linea "CentralHost=...". Revisa el formato contra config.ejemplo.txt.
    pause
    exit /b 1
)
rem instalar-agente.ps1 default es 8883, pero el firewall del servidor solo abre
rem 8080-8085 (ver docker-compose.yml: EMQX se remapea a 8081 externamente) — sin
rem esto el agente instala "Running" pero nunca conecta.
if "%MQTT_PUERTO%"=="" set "MQTT_PUERTO=8081"

echo Instalando el agente SAIDSOFT en esta estacion (%COMPUTERNAME%)...
echo.
powershell -NoProfile -ExecutionPolicy Bypass -File ".\instalar-agente.ps1" ^
    -PublishFolder ".\agente" -CentralHost "%CENTRAL_HOST%" -MqttPuerto %MQTT_PUERTO% ^
    -MqttPassword "%MQTT_PASSWORD%" -CaCertPath ".\cert.pem" ^
    -ComandoHmacSecret "%HMAC_SECRET%"

echo.
echo Listo. Revisa arriba si dijo "Running" en el servicio, y confirma en el panel
echo (/estaciones/) que %COMPUTERNAME% aparece como pendiente de aprobacion.
pause
