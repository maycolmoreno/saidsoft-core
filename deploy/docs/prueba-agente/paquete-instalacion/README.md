# Paquete de instalación de un clic

Arma una carpeta autocontenida que cualquiera puede copiar a una estación
(USB, carpeta compartida, etc.) e instalar el agente con un solo doble clic
en `Instalar.bat` — sin abrir PowerShell ni escribir ningún comando ahí.

## Armar el paquete (una vez, desde la máquina de build)

1. Publicar el agente si no lo hiciste todavía:
   ```powershell
   dotnet publish ..\..\src\Saidsoft.Agente\Saidsoft.Agente.csproj -c Release -r win-x64 `
       --self-contained true -p:PublishSingleFile=true -o ..\..\publish\agente
   ```
2. Copiar a esta carpeta (`deploy\paquete-instalacion\`):
   - `..\..\publish\agente\` → como subcarpeta `agente\`
   - `..\instalar-agente.ps1`
   - `cert.pem` de `deploy/certs/cert.pem` en `saidsoft-core` (del servidor)
3. Copiar `config.ejemplo.txt` como `config.txt` y completar los valores reales
   (`MqttPassword` = `MQTT_PASSWORD_AGENTE` de `deploy/.env` en saidsoft-core;
   `ComandoHmacSecret` = `COMANDO_HMAC_SECRET` del mismo archivo).

Al terminar, la carpeta debe verse así:
```
paquete-instalacion\
  agente\
    Saidsoft.Agente.exe
    ...
  cert.pem
  instalar-agente.ps1
  config.txt          <- con los valores reales, NUNCA se sube a git
  config.ejemplo.txt
  Instalar.bat
  README.md
```

## Usar el paquete (en cada estación nueva)

1. Copiar toda la carpeta a la estación (USB, red, lo que tengas a mano).
2. Confirmar que el nombre de equipo de Windows sigue la convención
   `FARMACIA-SUFIJO` (ej. `ML016-C`) — si no, renombrar y reiniciar antes.
3. Doble clic en `Instalar.bat`. Pide confirmación de UAC una vez (para
   correr como Administrador) y hace todo solo: instala el servicio, confía
   el certificado, arranca el agente.
4. Confirmar en el panel (`/estaciones/`) que la estación aparece como
   pendiente de aprobación, y aprobarla ahí.

`config.txt` trae las mismas credenciales para todas las estaciones — se arma
una sola vez y sirve para instalar en tantas estaciones como haga falta,
reusando la misma carpeta copiada.
