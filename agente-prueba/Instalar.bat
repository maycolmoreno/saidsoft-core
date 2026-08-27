@echo off
rem NO "enabledelayedexpansion": no se usa !variable! en ningun lado de este script, y
rem con delayed expansion activo cualquier "!" dentro de MqttPassword/ComandoHmacSecret
rem en config.txt se pierde en silencio al leerlo con el FOR /F de abajo (probado:
rem "abc123!$%" quedaba como "abc123$%") - un problema real, las contrasenas que
rem genera este proyecto pueden traer "!".
setlocal

rem Doble clic para instalar el agente SAIDSOFT (Python, como servicio de Windows) en
rem esta estacion, sin escribir nada en consola. Requiere que junto a este .bat esten:
rem   Saidsoft.Agente.exe     (build.ps1 -> dist\Saidsoft.Agente.exe)
rem   cert.pem                (CA de EMQX, deploy/certs/cert.pem del servidor)
rem   instalar-servicio.ps1
rem   config.txt              (copiado de config.ejemplo.txt, con los valores reales)

cd /d "%~dp0"

rem Auto-elevar a Administrador si hace falta (instalar-servicio.ps1 lo exige).
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
if not exist "Saidsoft.Agente.exe" (
    echo No se encontro Saidsoft.Agente.exe junto a este .bat.
    pause
    exit /b 1
)
if not exist "cert.pem" (
    echo No se encontro cert.pem junto a este .bat.
    pause
    exit /b 1
)
if not exist "instalar-servicio.ps1" (
    echo No se encontro instalar-servicio.ps1 junto a este .bat.
    pause
    exit /b 1
)

set "CENTRAL_HOST="
set "MQTT_PUERTO="
set "MQTT_PASSWORD="
set "HMAC_SECRET="
set "SERVIDOR_HORA="
for /f "usebackq tokens=1,* delims==" %%A in ("config.txt") do (
    if /i "%%A"=="CentralHost" set "CENTRAL_HOST=%%B"
    if /i "%%A"=="MqttPuerto" set "MQTT_PUERTO=%%B"
    if /i "%%A"=="MqttPassword" set "MQTT_PASSWORD=%%B"
    if /i "%%A"=="ComandoHmacSecret" set "HMAC_SECRET=%%B"
    if /i "%%A"=="ServidorHora" set "SERVIDOR_HORA=%%B"
)
if "%CENTRAL_HOST%"=="" (
    echo config.txt no tiene una linea "CentralHost=...". Revisa el formato contra config.ejemplo.txt.
    pause
    exit /b 1
)
rem instalar-servicio.ps1 ya tiene 8081 como default (EMQX remapeado por el firewall del
rem servidor, ver docker-compose.yml), pero lo tomamos de config.txt si esta explicito.
if "%MQTT_PUERTO%"=="" set "MQTT_PUERTO=8081"
rem Idem ServidorHora: instalar-servicio.ps1 ya tiene un default, config.txt lo pisa.
rem Poner "ServidorHora=" (vacio) en config.txt desactiva la sincronizacion de hora.
if "%SERVIDOR_HORA%"=="" set "SERVIDOR_HORA=farmaciasmia.int"

echo Instalando el agente SAIDSOFT en esta estacion (%COMPUTERNAME%)...
echo.
powershell -NoProfile -ExecutionPolicy Bypass -File ".\instalar-servicio.ps1" ^
    -PublishFolder "." -CentralHost "%CENTRAL_HOST%" -MqttPuerto %MQTT_PUERTO% ^
    -MqttPassword "%MQTT_PASSWORD%" -CaCertPath ".\cert.pem" ^
    -ComandoHmacSecret "%HMAC_SECRET%" -ServidorHora "%SERVIDOR_HORA%"

echo.
echo Listo. Revisa arriba si dijo "Running" el servicio, y confirma en el panel
echo (/estaciones/) que %COMPUTERNAME% aparece como pendiente de aprobacion.
pause
