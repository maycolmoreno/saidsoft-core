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
set "TOKEN_APERTURA="
for /f "usebackq tokens=1,* delims==" %%A in ("config.txt") do (
    if /i "%%A"=="CentralHost" set "CENTRAL_HOST=%%B"
    if /i "%%A"=="MqttPuerto" set "MQTT_PUERTO=%%B"
    if /i "%%A"=="MqttPassword" set "MQTT_PASSWORD=%%B"
    if /i "%%A"=="ComandoHmacSecret" set "HMAC_SECRET=%%B"
    if /i "%%A"=="ServidorHora" set "SERVIDOR_HORA=%%B"
    if /i "%%A"=="TokenApertura" set "TOKEN_APERTURA=%%B"
    if /i "%%A"=="Codigo" set "CODIGO=%%B"
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

rem Codigo de estacion. Hasta hoy salia SIEMPRE del hostname, que servia mientras
rem todas las estaciones eran cajas de farmacia con el nombre ya puesto a la
rem convencion SITIO-EQUIPO. Para una PC administrativa ese nombre no existe, y
rem renombrar Windows en 20 maquinas es mucho mas caro que escribir el codigo aca.
rem
rem OJO: el prefijo antes del guion tiene que ser un sitio que YA EXISTA en el
rem panel. Si no existe, el servidor rechaza el enrolamiento y la estacion no
rem aparece nunca (queda en /estaciones/enrolamientos-rechazados/).
if "%CODIGO%"=="" (
    set /p "CODIGO=Codigo de estacion [%COMPUTERNAME%]: "
)
if "%CODIGO%"=="" set "CODIGO=%COMPUTERNAME%"

echo Instalando el agente SAIDSOFT en esta estacion (%CODIGO%)...
echo.
powershell -NoProfile -ExecutionPolicy Bypass -File ".\instalar-servicio.ps1" ^
    -PublishFolder "." -CentralHost "%CENTRAL_HOST%" -MqttPuerto %MQTT_PUERTO% ^
    -MqttPassword "%MQTT_PASSWORD%" -CaCertPath ".\cert.pem" ^
    -ComandoHmacSecret "%HMAC_SECRET%" -ServidorHora "%SERVIDOR_HORA%" ^
    -TokenApertura "%TOKEN_APERTURA%" -Codigo "%CODIGO%"

echo.
rem Los parentesis de "/estaciones/" van escapados con ^ en el echo de abajo.
rem Sin eso, el parentesis de cierre termina el bloque del else antes de tiempo y
rem cmd ejecuta el resto de la linea como si fuera un comando, imprimiendo
rem "No se esperaba que en este momento" despues de una instalacion exitosa.
rem Por el mismo motivo este comentario va afuera del bloque: un rem con un
rem parentesis adentro de uno lo cierra igual.
if not "%TOKEN_APERTURA%"=="" (
    echo Listo. Revisa arriba si dijo "Running" el servicio. Con token de apertura,
    echo %CODIGO% debe aparecer en el panel ya APROBADA, dentro de la apertura
    echo de su farmacia, con sus pasos en marcha.
) else (
    echo Listo. Revisa arriba si dijo "Running" el servicio, y confirma en el panel
    echo ^(/estaciones/^) que %CODIGO% aparece como pendiente de aprobacion.
)
pause
