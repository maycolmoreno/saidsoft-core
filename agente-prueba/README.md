# Agente SAIDSOFT (Python)

Reemplazo en Python del agente real perdido (`saidsoft-agente`, C#). Empezó el
3-ago-2026 como "agente de prueba": una implementación de referencia liviana del
protocolo MQTT, pensada para validar el flujo servidor↔estación contra una estación
Windows de verdad sin depender del simulador Django (`python manage.py
simular_agente`, que solo corre en el propio servidor). El código fuente del agente
C# original no existía en este repositorio ni en esta máquina — vivía en otro
proyecto, en la máquina de quien lo desarrolló originalmente.

El 10-ago-2026, durante el primer despliegue de POS real del piloto (ML016-A), se
encontró un bug en el agente C# (comparación de SHA-256 sensible a
mayúsculas/minúsculas — ver `PLAN_MODERNIZACION.md` §10-J) que no se pudo corregir
porque **no se logró ubicar la máquina de build** con el código fuente original. En
vez de perseguirla, se decidió promover este agente de prueba a agente de producción
del piloto, completando lo que le faltaba (despliegues de POS, y la posibilidad de
correr como servicio de Windows). Ver `PLAN_MODERNIZACION.md` §10-K para el detalle
completo de esa decisión.

## Qué cubre

- **Enrolamiento**: se presenta con `hardware_id` (MachineGuid real de Windows),
  `numero_serie` (BIOS real, vía PowerShell/CIM), hostname y SO reales. Guarda el
  token recibido (y `cache_url_base`, si el servidor le asigna un caché de farmacia)
  en `identidad.json` — no vuelve a enrolarse en arranques siguientes.
- **Heartbeat** periódico.
- **Scripts (RMM)**: valida la firma HMAC-SHA256 del comando `ejecutar_script`
  (mismo algoritmo que `apps.catalogo.services.firmar_payload` del servidor — ver
  `firmar()` en `agente_prueba.py`), corre el script real con PowerShell y reporta
  `stdout`/`stderr`/`exit_code`.
- **Catálogo de software**: descarga el instalador, verifica SHA-256, corre
  `comando_instalacion_silenciosa` (o `comando_desinstalacion` si `accion=desinstalar`)
  y reporta cada paso.
- **Despliegues de POS**: descarga el paquete, verifica SHA-256 (comparación
  insensible a mayúsculas/minúsculas — el bug que forzó este reemplazo), aplica según
  `modo_aplicacion` (`inmediato` / `ventana` programada / al `cierre_pos` del POS),
  respalda la carpeta del POS antes de sobrescribirla, relanza el POS y hace rollback
  automático si no vuelve a quedar corriendo. Reporta cada paso de la línea de tiempo
  (`recibido` → `descargado` → `hash_verificado` → `pos_cerrado` → `aplicado` →
  `pos_relanzado` → `ok`/`error`/`rollback`). Requiere `--pos-carpeta-instalacion`,
  `--pos-nombre-proceso` y `--pos-comando-iniciar` configurados; sin eso, reporta
  error inmediato si llega un despliegue.
- Si un despliegue o una instalación de software trae `usar_cache=true`, intenta
  descargar primero del caché de farmacia (`cache_url_base` recibido en el
  enrolamiento) antes de caer al central — best effort, sin bloquear si el caché no
  responde.
- **`consultar_info`**: valida la firma HMAC (mismo esquema que `ejecutar_script`) y
  reporta hostname/número de serie/SO/procesador/RAM/almacenamiento vía CIM, más
  BitLocker del volumen `C:` (habilitado, método de protección, y la clave de
  recuperación + su ID si el protector es de tipo `RecoveryPassword`) — un solo script
  de PowerShell que arma todo en JSON. Si BitLocker no está disponible (Windows Home,
  o sin privilegios suficientes), esos campos quedan vacíos sin romper el resto de la
  consulta. Dispara este comando el botón "Actualizar ahora" en la ficha de la
  estación del panel.
- **`reiniciar`**: valida la firma HMAC y reinicia el **equipo Windows completo** (no
  solo el servicio del agente) con `shutdown /r /t 10` — 10 segundos de margen, no
  inmediato. Es fire-and-forget: el servidor no espera ninguna confirmación de vuelta
  (no hay tópico de "reinicio aplicado"), coherente con que el botón del panel avisa
  "esto interrumpe cualquier venta en curso en esa caja" antes de confirmar.

## Qué NO cubre (fuera de alcance a propósito)

- Servir de caché de farmacia (`es_cache_farmacia`): este agente puede *usar* un
  caché de farmacia al descargar (ver arriba), pero no expone un servidor HTTP local
  para que otras estaciones descarguen de él — sería un componente aparte.
- Comando `reiniciar` — se loguea como "no implementado" si llega.

## Límite real de compatibilidad con Windows 10 viejo — sin verificar

El parque real incluye estaciones con Windows 10 en builds viejos, no solo Windows 11
(ver `PLAN_MODERNIZACION.md`, que fija build 1607 como mínimo para el agente .NET real).
Este `.exe` está compilado con **Python 3.12**, cuya documentación oficial dice soportar
"Windows 10 y más nuevo" **sin especificar un build mínimo** — no hay garantía de que
corra en una máquina Windows 10 vieja sin parchar (podría faltarle el Universal C
Runtime si nunca se actualizó lo suficiente). No tengo forma de confirmarlo desde este
entorno porque no hay ninguna estación Windows 10 vieja disponible para probar.

**Conclusión**: para un Windows 10 realmente viejo sin parchar, esta sigue siendo una
pregunta abierta — solo la responde correr esto en esa máquina específica.

## Probado de verdad, no solo compilado

Validado contra el stack local completo (broker `amqtt` + panel + worker): enrolamiento
real con hardware real detectado, aprobación desde el panel, un script PowerShell real
ejecutado con salida real capturada en `ResultadoEjecucionScript.stdout`, y una
instalación de software real (descarga, hash verificado, comando ejecutado) que dejó un
archivo real en disco.

**Despliegues de POS (10-ago-2026)**: la lógica de descarga/hash/aplicar/rollback se
probó por partes — construcción del agente desde `config.json`, detección de proceso
vivo (`tasklist`), cálculo y comparación de SHA-256 case-insensitive, y el ciclo
completo `install`/`debug`/reconexión del *servicio* de Windows compilado (ver más
abajo) — pero **no** de punta a punta contra un POS real aplicando un paquete real
todavía. Antes de confiar el rollback automático en una farmacia real, conviene un
ensayo con un "POS" de juguete (una carpeta con un .exe cualquiera) para ver el ciclo
cerrar cerrar→respaldar→aplicar→relanzar→verificar sin sorpresas.

## Compilar

```powershell
..\.venv\Scripts\python.exe -m pip install -r requirements.txt   # una vez
..\.venv\Scripts\python.exe ..\.venv\Scripts\pywin32_postinstall.py -install   # una vez
.\build.ps1
```

Genera dos ejecutables en `dist\` (standalone, no necesitan Python en la estación
destino) — `dist/` y `build/` no se versionan, pero `agente_prueba.spec` sí (ver
`.gitignore`, ya no es descartable: define ambos ejecutables y los hiddenimports de
pywin32):

- **`agente_prueba.exe`** — modo consola manual, para pruebas puntuales.
- **`Saidsoft.Agente.exe`** — el mismo agente envuelto como servicio de Windows
  (`servicio_windows.py`), para producción.

## Usar en modo consola (pruebas)

```
agente_prueba.exe --codigo ML001-B --host <ip-del-servidor> --puerto 1883 --hmac-secret <COMANDO_HMAC_SECRET> ^
    --pos-carpeta-instalacion "C:\Program Files (x86)\Farmamia Cia Ltda - Elipsys\Cliente" ^
    --pos-nombre-proceso Zabyca.Pos.Desktop ^
    --pos-comando-iniciar "C:\Program Files (x86)\Farmamia Cia Ltda - Elipsys\Cliente\Zabyca.Pos.Desktop.exe"
```

Opciones (`agente_prueba.exe --help` para el detalle):

| Opción | Default | Para qué |
|---|---|---|
| `--codigo` | *(requerido)* | Código de estación, ej. `ML001-B`. Debe empezar con el código de una `Farmacia` que ya exista. |
| `--host` / `--puerto` | `127.0.0.1` / `1883` | Broker MQTT |
| `--usuario` / `--password` | vacío | Credenciales MQTT (vacío = sin auth, como en dev) |
| `--tls` / `--ca-cert` | desactivado | Para apuntar a producción (EMQX con TLS) |
| `--hmac-secret` | vacío | `COMANDO_HMAC_SECRET` del servidor — sin esto, los scripts se rechazan por firma inválida |
| `--intervalo-heartbeat` | `60` | Segundos entre heartbeats |
| `--pos-carpeta-instalacion` / `--pos-nombre-proceso` / `--pos-comando-iniciar` | vacío | Requeridos para despliegues de POS — ver `.SYNOPSIS` de `instalar-servicio.ps1` |
| `--espera-liveness-segundos` | `15` | Segundos tras relanzar el POS antes de chequear si sigue vivo (si no, dispara rollback) |

## Instalar como servicio de Windows (producción)

### Opción A: paquete de un clic (recomendado para varias estaciones)

Armá una carpeta con `Saidsoft.Agente.exe` (de `dist\`), `cert.pem` (de
`deploy/certs/cert.pem` del servidor), `instalar-servicio.ps1` e `Instalar.bat`, más
`config.txt` (copiá `config.ejemplo.txt` y completá `CentralHost`/`MqttPassword`/
`ComandoHmacSecret` reales — **`config.txt` nunca se versiona**, tiene secretos en
texto plano). Copiá esa carpeta a la estación y doble clic en `Instalar.bat`: se
autoeleva a Administrador, valida que estén los 4 archivos + `config.txt`, y corre
`instalar-servicio.ps1` con los valores de `config.txt` — sin escribir el comando de
PowerShell a mano. El mismo `config.txt` sirve para instalar en tantas estaciones
como haga falta (mismas credenciales de servidor para todas).

### Opción B: PowerShell directo

```powershell
.\instalar-servicio.ps1 -PublishFolder .\dist -CentralHost 10.111.6.20 `
    -MqttPassword "el-password-real" -CaCertPath .\dist\cert.pem `
    -ComandoHmacSecret "el-secreto-compartido-con-el-panel"
```

Cualquiera de las dos opciones registra `Saidsoft.Agente.exe` como el servicio de
Windows `SaidsoftAgente` (arranque automático, reinicio solo si crashea — `sc.exe
failure` con la misma política que usaba el agente C# original), leyendo la config de
`config.json` en vez de argv. Ver `.SYNOPSIS`/`.PARAMETER` de `instalar-servicio.ps1`
(`Get-Help .\instalar-servicio.ps1 -Full`) para el resto de los parámetros. Usa el
mismo nombre de servicio que el agente C# original — lo reemplaza en el lugar, no
convive con él.

Para depurar sin instalar nada: `Saidsoft.Agente.exe debug` lo corre en primer plano en
la consola actual (necesita `config.json` junto al `.exe`), útil para ver los logs de
enrolamiento/conexión en vivo antes de instalarlo como servicio.

Tras el primer enrolamiento, la estación queda **pendiente de aprobación** en
`/estaciones/` del panel (o pendiente_aprobacion en el admin) — hay que aprobarla ahí
antes de que el resto de los flujos (scripts, software) empiecen a funcionar.

**Dónde quedan `identidad.json` y `agente_prueba.log`**: en modo consola
(`agente_prueba.exe`), junto al `.exe`, en la carpeta desde donde se ejecuta. Corriendo
como servicio (`Saidsoft.Agente.exe`), en cambio, van a `C:\ProgramData\Saidsoft\` — NO
en la carpeta de instalación (`C:\Program Files\Saidsoft\Agente\`, donde solo viven el
`.exe`, `cert.pem` y `config.json`, estáticos). Escribir en tiempo de ejecución dentro
de Program Files da `PermissionError` incluso corriendo como Administrador — Windows la
protege por diseño; ProgramData es el lugar pensado para esto, y es el mismo directorio
que ya usaba el agente C# original para su propio `identidad.json`.
