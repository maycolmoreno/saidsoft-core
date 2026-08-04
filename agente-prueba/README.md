# Agente de prueba SAIDSOFT

Implementación de referencia liviana del protocolo MQTT que habla el agente real
(`saidsoft-agente`, C#). **No es el agente de producción** — es una herramienta de
prueba para validar el flujo servidor↔estación contra una estación Windows de verdad,
sin depender del simulador Django (`python manage.py simular_agente`, que solo corre
en el propio servidor y no sirve para probar en una caja física/VM separada).

El agente real (`saidsoft-agente`) no existe en este repositorio ni en esta máquina —
vive en otro proyecto, en la máquina de quien lo desarrolló originalmente. Este agente
de prueba se construyó justamente porque no había nada para instalar y probar.

## Qué cubre

- **Enrolamiento**: se presenta con `hardware_id` (MachineGuid real de Windows),
  `numero_serie` (BIOS real, vía PowerShell/CIM), hostname y SO reales. Guarda el
  token recibido en `identidad.json` — no vuelve a enrolarse en arranques siguientes.
- **Heartbeat** periódico.
- **Scripts (RMM)**: valida la firma HMAC-SHA256 del comando `ejecutar_script`
  (mismo algoritmo que `apps.catalogo.services.firmar_payload` del servidor — ver
  `firmar()` en `agente_prueba.py`), corre el script real con PowerShell y reporta
  `stdout`/`stderr`/`exit_code`.
- **Catálogo de software**: descarga el instalador, verifica SHA-256, corre
  `comando_instalacion_silenciosa` (o `comando_desinstalacion` si `accion=desinstalar`)
  y reporta cada paso.

## Qué NO cubre (fuera de alcance a propósito)

- Despliegues de POS (`/saidsof/despliegue/...`) — no se pidió para esta prueba.
- Servir de caché de farmacia (`es_cache_farmacia`) — un solo agente de prueba no
  tiene a quién servirle.
- Comandos `reiniciar`/`consultar_info` — se loguean como "no implementado" si llegan.

## Probado de verdad, no solo compilado

Se validó contra el stack local completo (broker `amqtt` + panel + worker corriendo en
este mismo entorno): enrolamiento real con hardware real detectado, aprobación desde el
panel, un script PowerShell real ejecutado con salida real capturada de vuelta en
`ResultadoEjecucionScript.stdout`, y una instalación de software real (descarga desde
el servidor Django, hash verificado, comando ejecutado) que dejó un archivo real en el
disco de esta máquina — no es una demo de humo, corrió el ciclo completo.

## Compilar

```powershell
..\.venv\Scripts\python.exe -m pip install -r requirements.txt   # una vez
.\build.ps1
```

Genera `dist\agente_prueba.exe` (standalone, ~9 MB, no necesita Python en la estación
destino). `dist/` y `build/` no se versionan (ver `.gitignore`).

## Usar

Copiar `dist\agente_prueba.exe` a la estación de prueba y correr desde una consola:

```
agente_prueba.exe --codigo ML001-B --host <ip-del-servidor> --puerto 1883 --hmac-secret <COMANDO_HMAC_SECRET>
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

Tras el primer enrolamiento, la estación queda **pendiente de aprobación** en
`/estaciones/` del panel (o pendiente_aprobacion en el admin) — hay que aprobarla ahí
antes de que el resto de los flujos (scripts, software) empiecen a funcionar.

`identidad.json` y `agente_prueba.log` quedan junto al `.exe`, en la carpeta desde
donde se ejecuta.
