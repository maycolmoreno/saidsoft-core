# SAIDSOFT — núcleo (RMM multi-cliente para CRESIO)

Reemplazo de `projectDJango` (Django 1.8/Python 2.7) + `projectNodeJS` del sistema
original (código de referencia en `C:\Proyectos\SAIDSOFT`, sin tocar), sobre
Django 5.2 LTS / Python 3.14. Ver [PLAN_MODERNIZACION.md](PLAN_MODERNIZACION.md)
para el panorama completo (fases 0-5 del reemplazo original + fases R1-R6a de la
extensión multi-tenant/RMM) — esta es la copia viva del plan, se actualiza aquí
**cada vez que se cierra una fase** (ver `CLAUDE.md`).

Este es un proyecto independiente: no comparte carpeta con el sistema viejo.

Nació como el reemplazo del panel de una sola operación (despliegues de POS +
inventario de activos IT para CRESIO), y se extendió a una plataforma multi-cliente
tipo RMM/MSP: cada unidad de negocio de CRESIO (San Gregorio, MIA, 7DIAS — modelo
`UnidadNegocio` en `apps/catalogo`) es un tenant aislado, con su propio catálogo de
farmacias/estaciones, despliegues, scripts, alertas y reportes.

## Estructura

```
config/                  settings (base/desarrollo/produccion), urls, wsgi/asgi
apps/catalogo/           UnidadNegocio (tenant), Grupo (TRX), Farmacia, Estación
apps/despliegues/        Despliegue, ResultadoDespliegue, EventoDespliegue (línea de tiempo inmutable)
apps/scripts/            Script (biblioteca RMM), EjecucionScript, ScriptProgramado (recurrente)
apps/monitoreo/          MuestraMetrica (RAM/CPU/swap/latencia), ReglaAlerta, Alerta
apps/auditoria/          EventoAuditoria (acciones del panel) + registrar_evento()
apps/mqtt_worker/        worker MQTT (reemplaza projectNodeJS/index.js) + simulador de agente
apps/activos/            inventario de activos CRESIO: Bodega, Colaborador, OrdenCompra,
                          Activo (código CR-TIPO-NNNN), EventoActivo (historial inmutable)
apps/mantenimiento/      mantenimientos correctivos/programados, checklist, firmas, visita técnica
apps/cumplimiento/       actividades de cumplimiento (AD, ESET, checklists) por unidad de negocio
apps/cuentas/            PerfilUsuario (RBAC por unidad de negocio) — apps/cuentas/services.py
                          centraliza el scoping de tenant, lo usan todas las apps de arriba
apps/panel/              panel HTMX: dashboard, estaciones, despliegues, scripts, monitoreo,
                          alertas, activos, mantenimiento, cumplimiento, auditoría, reportes
templates/panel/          plantillas del panel (Tailwind + HTMX)
static_src/input.css      fuente de Tailwind (@source apunta a templates/ y apps/)
static/css/app.css        CSS compilado (versionado, no requiere Node en el servidor)
static/js/htmx.min.js     HTMX vendorizado (sin CDN)
tools/                    tailwindcss.exe standalone (NO versionado, ver abajo)
deploy/                   infraestructura de producción (Docker Compose + TimescaleDB +
                          EMQX con TLS) — ver deploy/README-produccion.md
```

El **panel HTMX** (`/`) es la interfaz principal desde la Fase 2. El **Django admin**
(`/admin/`) se mantiene como vista avanzada/de respaldo (línea de tiempo detallada
de cada despliegue, edición directa de catálogos).

## Arrancar en local

```bash
python -m venv .venv
source .venv/Scripts/activate        # Windows/git-bash
pip install -r requirements-dev.txt  # incluye el broker MQTT embebido para pruebas locales
cp .env.example .env                 # completar SECRET_KEY, etc.

python manage.py migrate             # siembra SG/MIA (UnidadNegocio) como parte de la migración de catalogo
python manage.py seed_demo           # carga TRX001/ML001 (MIA) y TRX004/MAM01 (SG)
python manage.py createsuperuser
python manage.py seed_permisos       # grupos Django (Administrador, Técnico, Operador RMM, ...)
python manage.py seed_activos        # carga bodegas, colaboradores, una OC y activos de ejemplo
python manage.py seed_scripts_parcheo  # scripts winget compartidos (biblioteca RMM)
```

Un usuario nuevo no ve nada de tenant hasta que se le asigne acceso: desde el admin,
`PerfilUsuario.unidades_negocio` (o `acceso_todas_unidades=True` para equipo interno).

Necesitas **tres procesos corriendo en paralelo**:

```bash
amqtt                                # broker MQTT (solo desarrollo; en prod: EMQX/Mosquitto)
python manage.py runserver --noreload  # panel web (http://localhost:8000/)
python manage.py run_mqtt_worker     # escucha enrolamiento/heartbeat/estado de despliegues
```

(`--noreload` evita que el autoreload de Django deje procesos hijos huérfanos en Windows/git-bash;
si editas código con el server corriendo, reinícialo a mano.)

## Modificar estilos (Tailwind)

El CLI standalone de Tailwind (~110MB) no se versiona. Para descargarlo de nuevo:

```bash
curl -sL -o tools/tailwindcss.exe \
  https://github.com/tailwindlabs/tailwindcss/releases/latest/download/tailwindcss-windows-x64.exe
```

Y recompilar tras tocar templates o `static_src/input.css`:

```bash
./tools/tailwindcss.exe -i static_src/input.css -o static/css/app.css --minify
```

`static/css/app.css` sí se versiona (es el artefacto final), así que producción
no necesita Node.js ni el binario de Tailwind — solo sirve el CSS ya compilado.

## Probar el flujo completo sin el agente C#

El agente real ya existe (`C:\Proyectos\saidsoft-agente`, Fase 3-4), pero para
pruebas rápidas del backend sin él sigue disponible el simulador:

```bash
python manage.py simular_agente ML001-A
python manage.py simular_agente MAM01-B --forzar-error   # prueba el rollback
```

El simulador se enrola, se suscribe a los tópicos MQTT de su farmacia/grupo/cadena,
descarga el `.zip` del despliegue publicado, verifica el hash y reporta cada paso del
ciclo (`recibido → descargado → hash_verificado → pos_cerrado → aplicado → pos_relanzado → ok`,
o `error → rollback`) — el mismo protocolo que habla el agente C# real.

Para probar el enrolamiento de una estación nueva (cola de aprobación del panel):

```bash
python -c "
import paho.mqtt.publish as p, json
p.single('/saidsof/enrolamiento/solicitar/', json.dumps({
    'codigo': 'ML001-B', 'numero_serie': 'SN-001',
    'so_nombre': 'Windows 11', 'so_build': '24H2', 'version_agente': '1.0.0'
}), hostname='localhost', port=1883)
"
```

## Módulo de Activos (Fase 2b)

Implementa los 4 flujos CRESIO (Compra → Ingreso a bodega → Asignación → Desvinculación)
con el mismo patrón que despliegues: `Activo` guarda el estado actual, `EventoActivo`
es el historial inmutable (ingreso, asignación, consumible entregado, devolución, envío/
retorno de reparación, baja) con `detalle` en JSON. Toda la lógica de transición de
estado vive en `apps/activos/services.py`, reutilizada por panel y admin.

- **Código de activo** `CR-[TIPO]-[NNNN]`: secuencial global por tipo (no reinicia por
  bodega/año), generado en `services.generar_codigo_activo`.
- **Un activo nunca se elimina** — `Activo.delete()` y `EventoActivo.delete()` lanzan
  `NotImplementedError`; "Dado de baja" es un estado, no un borrado. `ActivoAdmin` también
  bloquea el permiso de borrado.
- **Colaboradores**: carga manual por ahora (según lo acordado); la integración con
  RRHH/nómina queda prevista para más adelante sin cambiar el modelo.
- **Stock de consumibles**: se descuenta al entregar (`registrar_consumible_entregado`,
  valida que haya suficiente) y se repone desde "Bodegas y stock" en el panel.

## Despliegue por anillos y aprobación por lotes (Fase 4)

- **Anillos**: un despliegue completado (`estado=completado`) puede **promoverse**
  con un clic (`/despliegues/<id>/promover/`) — crea un nuevo `Despliegue` con el
  mismo archivo, versión y hash (sin volver a subir nada), pero con un destino más
  amplio. `Despliegue.despliegue_origen` guarda la trazabilidad entre anillos, visible
  en ambos sentidos en la página de detalle. El anillo promovido sigue pasando por la
  aprobación de cuatro ojos normal — promover no salta ningún control.
- **Aprobación por lotes**: la cola de estaciones pendientes (`/estaciones/`) permite
  marcar varias con checkbox y aprobarlas todas de una (`estaciones_aprobar_lote`),
  para cuando llegan muchos enrolamientos de golpe (ej. instalación inicial en 600
  farmacias).
- El campo `respuesta de enrolamiento` ahora incluye `farmacia` y `grupo` (no solo
  `token`), para que el agente sepa a qué tópicos de despliegue suscribirse sin tener
  que consultar la base de datos directamente.

## Multi-tenancy (unidades de negocio)

Cada `Farmacia` pertenece a una `UnidadNegocio` (obligatoria) — el tenant. Un `Grupo`
(canal TRX) **puede estar compartido** entre unidades de negocio a propósito (varias
marcas de CRESIO corriendo el mismo canal de versión), así que el aislamiento nunca se
apoya en el Grupo, siempre en la unidad de negocio de la farmacia/estación/colaborador.

- **RBAC centralizado** en `apps/cuentas/services.py`: `unidades_negocio_visibles(user)`,
  `verificar_acceso(user, unidad_negocio)` (403 si no tiene acceso; `None` = objeto
  compartido, visible para todos), `scope_por_unidad_negocio[_activa]` (listados,
  campo obligatorio) y `scope_opcional_por_unidad_negocio[_activa]` (listados, campo
  opcional — ej. `Colaborador`/`Activo`, donde `unidad_negocio=None` significa
  "compartido", no "excluido"). `PerfilUsuario.unidades_negocio` (M2M) +
  `acceso_todas_unidades` (equipo interno/superusuarios) definen qué ve cada usuario.
- **`Despliegue`/`EjecucionScript`/`ScriptProgramado`** tienen `unidad_negocio`
  obligatoria — "toda la cadena" siempre significa la cadena de esa unidad, nunca de
  otra, aunque el destino use un `Grupo` compartido.
- **`Script`** (biblioteca RMM) puede ser compartido (`unidad_negocio=None`, ej. los
  scripts de winget sembrados por `seed_scripts_parcheo`) o privado de un cliente.
- **Selector de unidad activa**: en la barra superior del panel, si el usuario tiene
  acceso a más de una unidad, puede enfocar los listados/dashboard en una sola —
  puramente de presentación, no reemplaza el RBAC (`unidad_negocio_activa` en sesión).
- **Alcance deliberado**: `activos`/`mantenimiento`/`cumplimiento` están conectados al
  mismo RBAC (`Colaborador`/`Activo` con `unidad_negocio` opcional, heredada
  automáticamente al asignar un activo a un colaborador). `Bodega`, `OrdenCompra` y los
  catálogos (`Marca`, `CategoriaEquipo`, etc.) **no** se escopan — son infraestructura
  de TI centralizada de CRESIO, compartida entre sus marcas, no de un cliente.
- **Gap conocido**: el aislamiento es de aplicación/BD, no del broker MQTT — todos los
  agentes hoy comparten una sola credencial (`deploy/bootstrap-emqx.sh`), sin ACLs por
  tenant. Ver PLAN_MODERNIZACION.md §9.

### Alta masiva de farmacias desde un inventario de red (CSV)

Cuando llega un listado de sitios de red (ciudad, código de sitio, nodo TRX — el
formato típico que maneja el equipo de infraestructura, ej. una exportación de Excel
con columnas "Ciudad" / "Id de ..." / "NODO"), no hace falta darlas de alta una por
una desde el admin:

```bash
python manage.py importar_farmacias sitios.csv --dry-run   # previsualizar antes de escribir
python manage.py importar_farmacias sitios.csv             # crear de verdad
python manage.py importar_farmacias sitios.csv --actualizar  # además, sobreescribe
    # ubicación/grupo de las que ya existían (por defecto se omiten sin tocar)
```

El comando (`apps/catalogo/management/commands/importar_farmacias.py`) detecta las
columnas de código/ciudad/provincia/nodo/segmento de red/tipo de enlace/backup por
coincidencia parcial del encabezado (no importan mayúsculas ni el nombre exacto), y
usa el nodo de red como código de `Grupo` (se crea solo si no existe — un canal de
POS no tiene implicancias de seguridad). Si hay columna de provincia, la ubicación
queda como "Ciudad, Provincia". "Login" (usuario del circuito ante el proveedor de
internet) se ignora — es dato del proveedor, no de SAIDSOFT. La `UnidadNegocio`
**no** se adivina libremente: se deduce de la primera letra del código de farmacia con
un mapeo fijo del negocio (`M`→MIA, `G`→SG, dígito→7DIAS — ver
`PREFIJOS_UNIDAD_NEGOCIO` en el comando) y, si esa unidad todavía no existe en
SAIDSOFT, la fila se reporta como error en vez de crear un tenant nuevo sin querer.

`Farmacia` guarda además `segmento_red`, `tipo_enlace` y `tiene_backup` (columnas
"Segmento de Red"/"Tipo de Enlace"/"Backup" del mismo inventario) — son datos de
infraestructura de red por sitio que antes solo existían en el Excel del equipo de
infraestructura; tenerlos en SAIDSOFT deja diagnosticar problemas de conectividad
(o priorizar qué farmacias no tienen enlace de respaldo) sin volver a esa planilla.
Visibles/filtrables desde `/admin/catalogo/farmacia/`.

## Monitoreo de servidores

Migra y unifica el monitoreo del sistema viejo (`log_servidor_memoria` + `log_servidor_cpu`,
que eran dos tablas/tópicos) en una sola muestra por instante:

- **`MuestraMetrica`** guarda RAM/swap/cache (MB), CPU (%), temperatura (°C) y latencia (ms)
  ligados a una `Estacion`. Solo las estaciones con `monitorear_recursos=True` reportan
  (el flag viaja en la respuesta de enrolamiento); así se controla el volumen, igual que
  en el sistema viejo solo reportaban los servidores matriz.
- El agente C# mide con **APIs nativas de Windows** (`GlobalMemoryStatusEx`, `GetSystemTimes`)
  y hace ping al central para la latencia — sin dependencias externas. La temperatura del
  CPU queda en `null` por ahora (no se expone de forma fiable sin sensores/WMI).
- El panel (`/monitoreo/`) grafica las últimas ~60 muestras por servidor como **SVG inline**
  (sin CDN), con auto-refresco HTMX cada 10s y stat tiles que cambian de color por umbral.
- **Retención**: el comando `purgar_metricas --dias 30` (para cron) borra muestras viejas,
  reemplazando el `vaciar_logs` del sistema viejo (que borraba TODO cada domingo). En
  producción, esta tabla va sobre **TimescaleDB** con retención nativa.

## Motor de alertas

`ReglaAlerta` (umbral + duración + severidad, global o de un cliente) evaluada en
tiempo real:
- **Reglas de métrica** (`cpu_carga_pct`, `ram_usada_pct`, `latencia_ms`,
  `temperatura_c`): se evalúan en `apps/mqtt_worker/services.py::manejar_metricas`
  justo tras guardar cada `MuestraMetrica`. Anti-flapping: la condición debe sostenerse
  `duracion_minutos` completos (todas las muestras no nulas de la ventana incumplen, y
  hay historial que cubra toda la ventana) antes de abrir la `Alerta` — un pico
  aislado no dispara nada.
- **Regla `sin_heartbeat`**: se evalúa dentro de `marcar_estaciones_offline` (mismo
  comando de cron que ya marcaba estaciones caídas).
- **Resolución automática**: si la condición deja de cumplirse (nueva muestra normal, o
  vuelve el heartbeat), la alerta abierta se resuelve sola.
- **Notificación**: correo (`django.core.mail.send_mail`, backend configurado en
  `config/settings/{desarrollo,produccion}.py`) a quienes tengan acceso a la unidad de
  negocio de la estación, solo al **abrir** una alerta (no en cada muestra que la
  sostiene).
- Panel: `/alertas/` (reconocer/resolver manualmente) y `/monitoreo/reglas/` (CRUD de
  reglas).

## Scripts RMM y parcheo

`apps/scripts` — biblioteca de scripts PowerShell que corren sobre el mismo canal de
comandos MQTT/HMAC que usa `enviar_comando` (comando `ejecutar_script`, ya soportado
por el agente desde antes de esta etapa — ver `EjecutorScript.cs` en `saidsoft-agente`).

- **`Script`**: biblioteca reutilizable (o ad-hoc, "ejecutar sin guardar"), global o de
  un cliente.
- **`EjecucionScript`**: una corrida contra un destino (mismo shape que `Despliegue`:
  cadena/grupos/farmacias/estaciones), con `ResultadoEjecucionScript` por estación
  (exit code, stdout, stderr).
- **`ScriptProgramado`**: política "correr este script cada N días" — mismo patrón que
  `MantenimientoProgramado`, generado por el comando `generar_ejecuciones_programadas`
  (cron). Es la base del **parcheo de terceros**: `python manage.py
  seed_scripts_parcheo` crea dos scripts compartidos (`winget upgrade` de diagnóstico,
  y `winget upgrade --all --silent` para aplicar) que cualquier cliente puede programar.
- **`python manage.py cambiar_nodo_pos --unidad-negocio SG --grupo TRX002 --nodo trx002
  --usuario admin`**: cambia el nodo TRX (clave `Bdd` del `.Config` de Zabyca) de todas
  las estaciones aprobadas de un grupo en una sola pasada — arma el script ad-hoc,
  registra la ejecución y la envía por MQTT, sin pasar por el panel a mano. Reemplaza
  el proceso manual anterior (editar la línea a mano, re-comprimir el cliente y
  reenviarlo por estación). Solo edita el archivo (con respaldo `.bak-<timestamp>`
  automático); no reinicia el POS — ver `apps/scripts/management/commands/
  cambiar_nodo_pos.py`.
- **No cubre** parchar el SO (Windows Update nativo) — requiere código nuevo en el
  agente, ver PLAN_MODERNIZACION.md §9.

## Reportes exportables (CSV) y resumen por cliente

En `/reportes/`, en `apps/panel/reportes.py` (todos salvo auditoría aceptan
`unidad_negocio`/`unidades_negocio` para acotar a un cliente):
- **Resumen por cliente** (`/reportes/cliente/`): página imprimible (no CSV — sigue la
  convención de `mantenimiento_orden_trabajo`, sin generación de PDF en servidor) que
  consolida cumplimiento de versión, despliegues del período, alertas e inventario de
  activos de una sola unidad de negocio.
- **Cumplimiento de versión**: qué versión corre cada estación vs. la objetivo de su
  grupo (filtrable por grupo y por unidad de negocio).
- **Resultado de un despliegue**: estado por estación + timestamps de recibido/aplicado/ok.
- **Activos** y **Alertas**: inventario y alertas de un cliente en un rango de fechas.
- **Bitácora de auditoría**: acciones sobre el panel en un rango de fechas —
  deliberadamente **sin escopar por cliente** (el modelo es polimórfico, sin FK real al
  objeto auditado; es una herramienta de cumplimiento interno, no algo que se le
  entregue a un cliente).

## Producción (Docker + TimescaleDB + EMQX)

Todo el stack de producción está en `deploy/` (Dockerfile, docker-compose con web/worker/
TimescaleDB/EMQX-con-TLS, config, scripts de bootstrap). Ver **`deploy/README-produccion.md`**
para los pasos. Reemplaza el SQLite y el broker `amqtt` de desarrollo. La config de Django
para producción (`config/settings/produccion.py`) usa WhiteNoise para estáticos, PostgreSQL
vía `DATABASE_URL`, y endurecimiento HTTPS/HSTS.

## Distribución en cascada (caché por farmacia)

Reduce el tráfico VPN a escala (1.800 equipos): en vez de que las 3 cajas de cada
farmacia bajen el paquete del central, una estación designada como **caché**
(`es_cache_farmacia`, típicamente la -ADM) lo baja una vez y lo sirve por LAN a las otras.

- El agente caché descarga del central, verifica el hash, guarda el paquete por su
  `sha256` y lo sirve por HTTP (`GET /paquete/{sha256}`) en la LAN de su farmacia.
- Las cajas normales reciben en la respuesta de enrolamiento el `cache_url_base` de su
  farmacia (el servidor lo arma con la `ip_lan`/`puerto` que la caché reporta en su
  heartbeat, solo si está fresca). Al aplicar un despliegue, **intentan el caché primero**;
  si el caché no tiene el paquete todavía (404), está caído, o el hash no cuadra, **caen
  al central** — best-effort, nunca rompe el despliegue.
- El `sha256` en la URL es la clave y la verificación implícita: la caja pide exactamente
  el hash que espera, y lo vuelve a verificar tras descargar.

Verificado end-to-end: con caché ML001-ADM y caja ML001-A, la caja descargó **desde el
caché LAN** mientras el caché descargaba del central; y el servidor HTTP del caché
responde 200 con el paquete presente y 404 cuando no lo tiene (lo que gobierna el
fallback). El `client_id` MQTT se hizo único por estación (antes dos agentes en la misma
LAN de pruebas colisionaban; en producción el hostname ya es único).

**Producción**: el caché debe aceptar conexiones de la LAN, lo que requiere una urlacl
(`netsh http add urlacl url=http://+:8766/ ...`) que configurará el instalador MSI. En
desarrollo cae a `localhost` automáticamente.

## Acceso remoto (MeshCentral)

Control remoto interactivo (ver pantalla / terminal en vivo) de una estación, para lo que
ni el canal de comandos MQTT/HMAC ni Scripts RMM sirven (ambos son fire-and-forget, no
sesiones interactivas). Se resuelve con [MeshCentral](https://github.com/Ylianst/MeshCentral)
autoalojado, **completamente aparte** del canal MQTT/HMAC existente — la sesión viaja
navegador ↔ servidor MeshCentral ↔ MeshAgent, sin tocar `saidsoft-agente` ni el broker.

**En producción** (`deploy/docker-compose.yml`) ya es un servicio más del stack —
`docker compose up` lo levanta junto con `db`/`emqx`/`web`/`worker`/`redis`/
`celery_worker`/`celery_beat`, con su propio volumen (`meshcentral_data`) y puerto
`8083:443` (dentro del rango 8080-8085 abierto en firewall). Ver `deploy/README-produccion.md`.

**Levantar una instancia local para desarrollo/pruebas:**

```bash
docker run -d --name meshcentral \
  -p 443:443 \
  -v meshcentral-data:/opt/meshcentral/meshcentral-data \
  ghcr.io/ylianst/meshcentral:latest
```

1. Entrar a `https://localhost:443` (o `https://<host>:8083` en producción) — la primera
   cuenta que se crea queda como admin. Esto NO se automatiza por Compose: MeshCentral
   pide crear la cuenta desde la consola web la primera vez que alguien entra.
2. Crear un único device group para todas las estaciones (ej. "Estaciones SAIDSOFT" bajo
   "Mis dispositivos") — no hace falta uno por farmacia.
3. Copiar el Mesh ID del grupo (panel de detalles del grupo) a `MESHCENTRAL_MESH_ID` en `.env`.
4. Reiniciar `python manage.py runserver` (o `docker compose restart web worker` en
   producción) para que `MESHCENTRAL_CONFIG` tome el valor nuevo.

**Vincular una estación**: desde su ficha en el panel (botón "Instalar agente ahora"), se
prellena un script ad-hoc de Scripts RMM que descarga e instala el agente de MeshCentral en
modo silencioso (`installflags=2`, solo servicio, sin UI) vía el endpoint
`/meshagents?id=<arch>&meshid=<mesh_id>&installflags=2` que expone MeshCentral. Tras
instalarse, el dispositivo aparece en la consola de MeshCentral con un `node_id` propio; ese
`node_id` se copia a mano al panel (no hay sincronización automática contra la API de
MeshCentral todavía — ver `apps/catalogo/models.py::Estacion.meshcentral_node_id`).

**Permisos**: el botón y las tres vistas nuevas exigen el permiso Django
`catalogo.acceso_remoto_estacion` (el primer permiso *custom*, no-CRUD, del proyecto — hasta
ahora toda acción del panel, incluso reiniciar una estación, solo pedía sesión iniciada). Se
asigna al Group `Administrador` automáticamente vía `seed_permisos`.

**Nota sobre `viewmode`**: los valores de `MESHCENTRAL_VIEWMODE_ESCRITORIO`/`_TERMINAL` que
saltan directo a la pestaña de escritorio o terminal del dispositivo no se verificaron contra
una instancia real — al validar, abrir el link `?node=<id>` una vez, navegar a mano a cada
pestaña y ajustar el `.env` con el valor que MeshCentral ponga en su propia URL.

## BitLocker (estado de cifrado y clave de recuperación)

Se reporta junto con el resto de info de hardware (comando `consultar_info`, mismo
canal que procesador/RAM/almacenamiento — ver "Probar el flujo completo" arriba):
`bitlocker_habilitado` y `bitlocker_metodo_proteccion` en `Estacion` no piden permiso
especial, se muestran igual que el resto de la ficha del equipo.

La **clave de recuperación** es otra historia — con ella se descifra el disco completo:

- Viaja del agente al servidor por el mismo canal MQTT/TLS de `info_equipo` (no es un
  canal nuevo), pero **nunca se guarda en texto plano**: se cifra con Fernet
  (`apps/catalogo/crypto.py`) usando `BITLOCKER_ENCRYPTION_KEY` antes de tocar la base
  de datos (`ClaveRecuperacionBitLocker.clave_cifrada`).
- Verla desde el panel exige el permiso `catalogo.ver_clave_bitlocker` — **separado**
  de `acceso_remoto_estacion` y `supervision_auditoria_estacion` a propósito, nadie lo
  hereda de los otros dos. Por defecto solo lo tiene el grupo `Administrador`
  (vía `seed_permisos`); si algún rol más angosto lo necesita, hay que agregarlo a mano
  en `PERMISOS_LITERALES` (`apps/activos/management/commands/seed_permisos.py`) — es
  una decisión de a quién confiarle esto, no la tomamos por defecto.
- Cada vista de la clave queda auditada (`estacion.bitlocker_clave_ver` en
  `EventoAuditoria`), igual que el resto de acciones sensibles del panel.
- `BITLOCKER_ENCRYPTION_KEY` es una setting requerida (sin default, igual que
  `COMANDO_HMAC_SECRET`): generar con
  `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`.

## Seguridad y robustez

- **Enrolamiento verificado por hardware**: el agente reporta un `hardware_id` estable
  (MachineGuid de Windows). En el primer enrolamiento se fija; en cada re-enrolamiento
  (el agente perdió su `identidad.json`) el servidor exige que coincida antes de
  devolver el token. Así un equipo ajeno en la VPN no puede pedir el token de una
  estación existente solo con su código. Si no coincide, se rechaza y se registra como
  posible suplantación (`apps/mqtt_worker/services.py::manejar_enrolamiento`).
- **Reporte de despliegue solo de estaciones aprobadas**: `manejar_estado_despliegue`
  valida token *y* estado de aprobación.
- **Acciones de estado solo por POST**: aprobar/publicar/pausar/reanudar despliegues y
  aprobar/rechazar estaciones usan `@require_POST`, para que ni un link ni un
  `<img src>` malicioso disparen la acción por GET (donde CSRF no aplica).
- **Estaciones OFFLINE**: el comando `marcar_estaciones_offline` (corre por cron/Programador
  de tareas, ej. cada minuto) pasa a offline las estaciones sin heartbeat reciente. El
  agente solo sabe ponerse online; esto cierra el otro lado.

## Notas de diseño

- **Aprobación de cuatro ojos**: quien crea un despliegue no puede aprobarlo — verificado
  tanto en `apps/panel/views.py::despliegue_aprobar` como en el admin.
- **Freno automático**: si el % de estaciones en error supera `umbral_error_pct`, el despliegue
  pasa a `pausado` solo (`apps/despliegues/services.py::evaluar_freno_automatico`). Al
  **reanudar** manualmente se marca `freno_omitido`, para que no se vuelva a frenar en bucle
  (el operador ya vio los errores y decidió continuar).
- **Mensajes MQTT con `retain=True`**: una estación apagada al momento de publicar el
  despliegue lo recibe igual al encender.
- `version_pos` de una `Estacion` se actualiza tanto por heartbeat como al confirmar `ok`
  de un despliegue (no espera al siguiente heartbeat para reflejar la realidad).
- **Dashboard en vivo sin Channels**: el progreso de despliegues y la cola de aprobación
  se refrescan con `hx-trigger="every Ns"` (polling HTMX), no WebSockets — mucho menos
  infraestructura que Django Channels y suficiente a esta escala. Si más adelante se
  necesita latencia menor a 1s, ahí sí vale migrar a Channels.
- **Panel vs. admin**: el panel cubre el flujo diario (crear/aprobar/publicar despliegues,
  aprobar estaciones, ver cumplimiento). El admin sigue siendo el lugar para la línea de
  tiempo completa evento por evento de un `ResultadoDespliegue` y edición fina de catálogos.
