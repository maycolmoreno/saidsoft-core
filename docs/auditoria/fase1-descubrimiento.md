# Auditoría SAIDSOFT — FASE 1: Descubrimiento del proyecto

Documento de descubrimiento aprobado. Las fases siguientes deben leer **este archivo** en
vez de depender de la memoria de la conversación.

Su propósito es que nadie tenga que redescubrir la arquitectura, y que nadie proponga crear
algo que ya existe.

---

## §0. Línea base

### 0.1 Línea base histórica de la FASE 1 (lo que realmente se auditó)

La FASE 1 se ejecutó sobre este estado, y todo lo que describe este documento se midió
ahí:

```text
Rama:    master
HEAD:    fe2819f  "Un activo entro por el admin sin codigo, y la numeracion se rompe justo con el"
Árbol:   3 archivos modificados sin commitear, +148/-50
         apps/activos/services.py
         apps/activos/management/commands/corregir_activos_sin_codigo.py
         apps/activos/tests.py
Commits: 388
```

Esta línea base **no se reescribe**: es el hecho histórico de cuándo se tomaron las
mediciones.

### 0.2 Transición desde la FASE 1 hasta hoy

```text
fe2819f  (master)
   │
   ├─► Commit 1 — cambios legítimos de FASE A (+148/-50 en apps/activos)
   │   trabajo sobre generar_codigo_activo / corregir_activos_sin_codigo
   ▼
06b5237  "La funcion que asigna los codigos se rompia con el unico dato que habia que arreglar"
   │
   ├─► Commit 2 — CORRECCION_DATOS en EventoActivo.TipoEvento
   │   + migración apps/activos/migrations/0027_alter_eventoactivo_tipo_evento.py
   ▼
3f194b6  "El historial decia \"Datos de red cargados\" sobre un cambio de codigo"
```

Los +148/−50 que la FASE 1 observó como diff sin commitear **son el contenido del
Commit 1**, hoy versionado en `06b5237`. El contenido de los tres archivos no cambió entre
ambos momentos: lo que cambió es que pasó de árbol de trabajo a commit.

**Ninguna línea de ninguno de los dos commits fue introducida por la auditoría**, que fue
estrictamente de lectura. El único archivo que la auditoría escribió dentro del repositorio
es este documento.

### 0.3 Línea base al escribirse la FASE 1B (instantánea fechada)

Verificada con `git branch --show-current` y `git rev-parse --short HEAD` en el momento de
escribir este documento:

```text
Fecha:  8-oct-2026
Rama:   fase-a-correccion-datos
HEAD:   3f194b6  "El historial decia \"Datos de red cargados\" sobre un cambio de codigo"
Árbol:  limpio
```

**Es una instantánea, no una línea base permanente.** El desarrollo avanza en ramas cortas
(`fase-a-*`, `fase-b-*`) y el árbol cambia varias veces al día, así que **cada fase debe
verificar su propia línea base al arrancar** en vez de confiar en este bloque. Lo que no
cambia es §0.1 (lo que la FASE 1 realmente midió) y §0.2 (cómo se llegó hasta acá).

Al revalidar, mirar también si §6 (inventario) sigue describiendo el código: el trabajo en
curso sobre `Activo`, `ActivoIngresoForm` y `apps/activos/services.py` es el que más
probabilidad tiene de dejar esa sección desfasada.

### 0.4 Trabajo parado fuera del árbol auditado (solo registro, no se toca)

```text
stash@{0}: On master: trabajo-usuario-hypertables
stash@{1}: On master: eventos-windows-wip
```

Fuera del alcance. No se aplican, inspeccionan ni eliminan. Existe además la rama
`feat/ventanas-emergentes-alta` @ `d6805b6`, también fuera del alcance.

### 0.5 Restricciones vigentes

Solo lectura. **Prohibido**: modificar código de aplicación, migraciones o
`docs/auditoria-integridad.sql`; `manage.py test`; `pytest`; `manage.py shell`;
`makemigrations` (incluso con `--check --dry-run`); `migrate`; `flush`; SQL de escritura;
conectarse a cualquier base de datos; `commit`; `checkout`/`reset`/`restore`/`clean`;
instalar dependencias; leer valores de secretos reales de `.env` o `deploy/.env`.

**Permitido**: lectura de archivos; `git log`/`diff`/`blame`/`show`/`status`/`reflog`;
`pyflakes`; `.env.example` y `deploy/.env.prod.example`; analizar nombres de variables de
entorno y si una variable es requerida o tiene default.

Toda consulta SQL de verificación que un hallazgo necesite va **en el cuerpo del informe
del agente**, nunca escrita en `docs/auditoria-integridad.sql`.

### 0.6 Alcance

Tres componentes, y sobre todo los contratos entre ellos:

```text
saidsoft-core/   backend Django
agente-prueba/   agente Windows en Python (PyInstaller)
movil-campo/     app Flutter de técnicos en campo
```

Contratos a revisar: `Activo` ↔ `Estacion`; backend ↔ agente; backend ↔ app móvil;
IP/MAC; autenticación; HMAC; estados; idempotencia; APIs; inventario declarado vs.
observado.

### 0.7 Clasificación obligatoria de hallazgos

```text
BUG CONFIRMADO     el defecto se reprodujo, o la evidencia en el código es inequívoca
RIESGO CONFIRMADO  la condición que lo habilita está verificada; el efecto puede faltar reproducir
HALLAZGO           evidencia sólida, consecuencia no demostrada todavía
MEJORA             no hay defecto; hay algo que conviene cambiar
NO HALLADO         se buscó y no se encontró (vale reportarlo: acota el alcance)
```

Subclasificación para desvíos de documentación:

```text
DOCUMENTACIÓN DESACTUALIZADA · RIESGO OPERATIVO · CONFIGURACIÓN INCORRECTA · BUG CONFIRMADO
```

Mientras las pruebas no estén autorizadas, **nada se marca CONFIRMADO solo por
inferencia**. Si hace falta ejecución, el hallazgo dice `Reproducción: pendiente`.

Plantilla de cada hallazgo:

```text
ID:
Clasificación:
Severidad:
Confianza:
Componente:
Archivo:
Línea:
Condición:
Evidencia:
Impacto:
Reproducción:
¿Ya documentado?:
¿Ya corregido?:
Recomendación:
```

Al analizar el árbol actual, separar siempre: **problemas preexistentes**, **cambios
legítimos en progreso** y **modificaciones introducidas por la propia auditoría**. Nunca
clasificar como defecto existente algo introducido durante la auditoría.

### 0.8 Correcciones a las cifras citadas verbalmente en la FASE 1

Dos números del informe verbal estaban mal. Esta tabla es la versión con autoridad:

| Dato | Dicho verbalmente | Verificado |
|---|---|---|
| Manejadores MQTT en `apps/mqtt_worker/services.py` | 21 | **15** (`grep -c "^def manejar_"`), con 15 constantes `TOPICO_*` en `run_mqtt_worker.py` |
| Lugares que leen `REMOTE_ADDR` | 5 | **4 de producción** + 1 en `apps/auditoria/tests.py` |

### 0.9 Revalidación previa a la FASE 4 (10-oct-2026)

Verificada sobre `master` @ `5732676`. Entre la FASE 1 (`fe2819f`) y esta fecha entraron
**7 commits** que tocan el alcance de la auditoría, con **+3.993 / −76** líneas en
`apps/activos`, `apps/catalogo` y `apps/monitoreo`. Lo que cambió respecto de lo que
describen las secciones de abajo:

| Qué | Cambio | Dónde se corrigió |
|---|---|---|
| Paquete SNMP genérico **nuevo** | `apps/monitoreo/snmp/` — 7 módulos, catálogo declarativo de OIDs, lectura de impresoras y red. **Coexiste** con `mikrotik.py`, no lo reemplaza | §1, §3, §7 |
| 3 modelos nuevos | `PerfilSnmp`, `ObjetivoSnmp`, `LecturaSnmpActual` (95 → 98 modelos) | §1, §3, §5 |
| `Activo.ubicacion` | FK nueva a `Ubicacion` (`PROTECT`, nullable): dónde está el equipo cuando no está en farmacia ni en bodega. **Cambia la regla de "dos niveles de ubicación" a tres** | §6 |
| `Activo.observaciones` | `TextField` nuevo para lo que no entra en ningún otro campo | §6 |
| `EventoActivo.TipoEvento` | 11 → 12 valores (`CORRECCION_DATOS`) | §6 |
| Comandos nuevos | `importar_ubicaciones`, `probar_snmp_impresora` | §9.1 |
| Cifras | ver la tabla de §1 | §1 |

**Lección para las fases siguientes:** entre que se escribió este documento y que se lo
usó, la zona de inventario y SNMP se movió lo suficiente como para invalidar una regla de
dominio. Revalidar al arrancar cada fase no es formalidad.

#### 0.9.1 Segunda revalidación, al ejecutar la FASE 4 (10-oct-2026, mismo día)

La lección de arriba se cobró sola en el mismo día: entre que se escribió §0.9 contra
`5732676` y que la FASE 4 se ejecutó, entraron **3 commits más**, todos dentro del paquete
SNMP, con **+417 / −35** en `apps/monitoreo/snmp/`, `models.py` y `tests.py`.

```text
5732676  ← línea base de §0.9
   │
   ├─ aa86613  Saneo de NUL antes de escribir (PostgreSQL rechaza \x00, SQLite lo acepta)
   ├─ 6556402  El guardado pasa DENTRO del try: un equipo que responde raro ya no
   │           se lleva el lote entero
   └─ 0515773  Un reloj por cadencia en vez de uno compartido
   ▼
0515773  ← línea base real de la FASE 4
```

Lo que esto cambia respecto de lo que describen §5 y §7:

| Qué | Cambio |
|---|---|
| `ObjetivoSnmp` — relojes | El campo único de última lectura se **partió en tres**: `ultima_lectura_rapida`, `ultima_lectura_lenta`, `ultima_lectura_identidad`. Con un reloj compartido, la cadencia rápida reseteaba el temporizador de la lenta y las métricas de tóner no se leían nunca |
| Índices | El índice parcial `objetivo_snmp_pendientes` se reemplaza por tres: `objetivo_snmp_pend_rapida`, `objetivo_snmp_pend_lenta`, `objetivo_snmp_pend_ident` |
| Migración | `0043_remove_objetivosnmp_objetivo_snmp_pendientes_and_more.py` (`monitoreo` pasa a 43 migraciones) |
| Aislamiento de fallas | El `save()` del sondeo quedó dentro del `try` por dispositivo |
| Sanitización | `snmp/normalizar.py` elimina NUL antes de escribir |

**Lo que no cambia:** ninguna regla de dominio. Las tres decisiones de §6 (declarado ≠
observado, una sola fuente de verdad para la IP, estados de inventario separados de los de
monitoreo) siguen en pie, y los tres niveles de ubicación también.

**Advertencia para quien lea los hallazgos de la FASE 4:** estos tres commits son
**trabajo legítimo en progreso**, no deuda vieja. Los hallazgos que caen sobre ellos
(`BD-09`, `BD-10`, `E-1`…`E-4`, `INV-11`…`INV-13`, `BUG-02`, `HALLAZGO-26`) están marcados
como tales en cada informe y no deben leerse como defectos heredados. `BD-09` en
particular habla justamente de los tres índices que `0515773` acaba de crear.

### 0.10 Cómo retomar en una sesión nueva

**Lanzá Claude Code desde la raíz del repositorio**, no desde el directorio padre:

```powershell
cd C:\Proyectos\saidsoft-core
claude
```

Importa: Claude Code busca `.claude/agents/` y `.claude/settings.json` en la raíz del
proyecto **desde donde se lo lanzó**. Abierto desde `C:\Proyectos`, no encuentra ninguno de
los dos — los cuatro agentes no aparecen y las reglas `deny` de permisos nunca se aplican.
Eso ya pasó el 10-oct-2026 y costó dos intentos entenderlo.

Verificá tres cosas antes de auditar, y no des ninguna por sentada:

1. `/agents` lista `revisor-arquitectura`, `cazador-bugs`, `revisor-bd` y
   `revisor-inventario`.
2. `/permissions` muestra las reglas `deny` de `.claude/settings.json`.
3. Las reglas **bloquean de verdad**: `psql --version` tiene que quedar rechazado por
   permisos, no responder `command not found`; y una lectura con `Read` de una ruta fuera
   del repositorio tiene que rechazarse. Una regla `deny` que no se validó no está
   funcionando.

Verificá además tu propia línea base (`git branch --show-current`,
`git rev-parse --short HEAD`, `git status --porcelain`): este repositorio avanza en ramas
cortas y durante la semana del 6 al 10-oct-2026 el árbol se movió cuatro veces.

~~**Fase pendiente: ejecutar los agentes**~~ **HECHO el 10-oct-2026** sobre `master` @
`0515773`. Resultado en **`docs/auditoria/fase4-hallazgos.md`**: 100 hallazgos brutos, **94
únicos** tras deduplicar, **12 de severidad ALTA**. Ese archivo contiene los cuatro
informes íntegros más el análisis consolidado.

Lo que se aprendió al ejecutarla, para la próxima vez que se corran los agentes:

- **El orden no importa.** Se corrieron los cuatro en paralelo (uno primero y tres juntos
  después). Son subagentes aislados: ninguno ve la salida de otro, así que la secuencia no
  puede cambiar sus hallazgos. Lo único que ordena es la lectura de los informes.
- **Seis pares resultaron el mismo defecto** encontrado por dos agentes independientes
  (ver §2 del informe de FASE 4). En tres de esos pares los agentes discreparon en
  severidad. La coincidencia es señal a favor del hallazgo, no ruido a descartar.
- **Dos hallazgos comparten línea sin ser el mismo defecto** (`BD-04` e `INV-18`, los dos
  en `mqtt_worker/services.py:325`). Deduplicar por archivo:línea sin leer las fichas
  habría perdido uno.
- **El harness marca los informes como "texto con forma de instrucción"** porque los
  agentes citan y analizan `.claude/settings.json` dentro de su alcance. Es esperable: ese
  texto es un hallazgo a leer, nunca una orden a ejecutar. No se tocan permisos ni
  configuración por lo que diga un informe.

Al invocarlos, dales **solo el alcance a revisar**: no les adelantes hallazgos ni les
indiques dónde buscar — sus instrucciones ya están en su definición. Entregá los cuatro
informes completos, sin resumirlos ni filtrarlos, y al final una tabla consolidada sin
duplicados. **No corrijas ningún hallazgo** y no creés agentes nuevos: la corrección es una
decisión humana posterior.

Si las reglas de permisos no se pueden validar, los dos agentes sin `Bash`
(`revisor-bd`, `revisor-inventario`) son los únicos que se pueden correr con garantías,
porque sus restricciones son estructurales y no dependen de `settings.json`.

### 0.11 Regla de reutilización

Antes de crear una implementación nueva: buscar si ya existe, identificarla, evaluar si
puede reutilizarse, y proponer una nueva **solo** si la existente no sirve, explicando por
qué. Destinos concretos:

```text
SNMP al equipo de borde  → apps/monitoreo/mikrotik.py   (ancho de banda, identidad, ARP)
SNMP a otro dispositivo  → apps/monitoreo/snmp/         (impresora, switch, UPS)
                           para un tipo nuevo: UNA ENTRADA en catalogo.py, no un módulo
alertas              → apps/monitoreo/services.py (motor de ReglaAlerta/Alerta)
                       apps/monitoreo/enlaces.py  (motor de enlaces por sitio)
credenciales/cifrado → apps/catalogo/crypto.py
inventario           → apps/activos/models.py: Activo
auditoría            → apps/activos/models.py: EventoActivo
                       apps/auditoria/models.py: EventoAuditoria + registrar_evento()
tareas periódicas    → Celery / CELERY_BEAT_SCHEDULE en config/settings/base.py
observación ARP      → apps/monitoreo/models.py: DispositivoDetectado
```

No crear arquitecturas paralelas. Si ya existe una implementación para una función,
documentarla; no proponer otra.

---

## §1. Stack detectado

Versión = la realmente presente (`.venv`, imágenes de Compose, CI), no el rango de
`requirements.txt`.

| Tecnología | Versión | Evidencia |
|---|---|---|
| Python | 3.14.6 | `.venv/Scripts/python.exe --version`; `.github/workflows/pruebas.yml` (`python-version: "3.14"`) |
| Django | 5.2.16 (rango `>=5.2.8,<6.0`) | `requirements.txt:1`; `pip list` del `.venv` |
| PostgreSQL + **TimescaleDB** | imagen `timescale/timescaledb:2.17.2-pg16` | `deploy/docker-compose.yml`; `.github/workflows/pruebas.yml`; `deploy/db/init-timescaledb.sql` |
| psycopg | 3.3.4 | `requirements.txt:6` (`psycopg[binary]>=3.2`) |
| SQLite | default de desarrollo, no de producción | `config/settings/base.py:106-108` |
| Celery | 5.6.3 | `requirements.txt:10`; `config/celery.py` |
| Redis | cliente 8.1.0 / imagen `redis:7-alpine` | `requirements.txt:11`; servicio `redis` del Compose |
| paho-mqtt | 2.1.0 | `requirements.txt:4`; `apps/mqtt_worker/` |
| **EMQX** | 5.8.3 | `deploy/docker-compose.yml`; `apps/mqtt_worker/emqx_admin.py` |
| amqtt (broker embebido, solo dev) | 0.11.3 | `requirements-dev.txt` |
| Docker Compose | v2 (`docker compose`) | `CLAUDE.md` §Producción; contenedores `deploy-web-1` |
| nginx | `nginx:1.27-alpine` | `deploy/docker-compose.yml`; `deploy/nginx/nginx.conf` |
| gunicorn | 26.0.0 | `requirements.txt:7`; `command` del servicio `web` |
| whitenoise | >=6.7 | `requirements.txt:8`; `config/settings/produccion.py:10` |
| Django REST Framework | 3.17.1 | `requirements.txt:2`; `REST_FRAMEWORK` en `base.py` (TokenAuthentication) |
| pysnmp | 7.1.28 | `requirements.txt:13`; `apps/monitoreo/mikrotik.py` |
| cryptography (Fernet) | 50.0.0 | `requirements.txt:9`; `apps/catalogo/crypto.py` |
| django-axes | 8.3.1 | `requirements.txt:14`; `base.py:135-162` |
| django-otp + otp_totp | 1.7.0 | `requirements.txt:15`; `apps/cuentas/forms.py` (`LoginConOTPForm`) |
| qrcode[pil] | 8.2 | `requirements.txt:16` (QR de activación MFA) |
| django-environ | 0.14.0 | `requirements.txt:3`; `base.py` |
| xhtml2pdf / reportlab / pyHanko | 0.2.x / 4.5.1 / 0.36.2 | `requirements.txt:12`; informes PDF de mantenimiento |
| openpyxl | 3.1.5 | `requirements.txt:17`; comandos de importación de planillas |
| anthropic | 1.6.0 | `requirements.txt:22`; `apps/monitoreo/diagnostico_ia.py` |
| websocket-client | >=1.8 | `requirements.txt:5`; `apps/monitoreo/adapters/meshcentral.py` |
| MeshCentral | `ghcr.io/ylianst/meshcentral:latest` | `deploy/docker-compose.yml`; `MESHCENTRAL_*_CONFIG` |
| Flutter / Dart | canal `stable`, sin pin | `.github/workflows/pruebas.yml` job `movil`; `movil-campo/pubspec.yaml` |
| Tailwind CSS | binario `tools/tailwindcss.exe` | `static_src/{input,tokens,components}.css` |
| HTMX | en plantillas | `apps/panel/middleware.py`, `apps/panel/context_processors.py` |
| PyInstaller | 6.22.2 | `.venv`; `agente-prueba/build.ps1`, `agente_prueba.spec` |
| **Linter** | **ninguno configurado** | Sin `ruff.toml`/`.flake8`/`pyproject.toml`/`setup.cfg`; sin paso de lint en CI. `pyflakes 3.4.0` en `.venv`, no declarado |
| pytest / coverage | 9.1.1 / 7.15.4 instalados, **no usados** | En `.venv`, ausentes de `requirements*`; CI y `CLAUDE.md` usan `manage.py test` |

**Verificado como ausente** (no asumido): Channels / Daphne / uvicorn — existen
`config/asgi.py` y `ASGI_APPLICATION`, pero son scaffolding por defecto de Django y el
despliegue sirve WSGI con gunicorn. Framework de caché de Django — `CACHES` no se define
en ninguna parte del repo; Redis se usa **solo** como broker y backend de resultados de
Celery.

**Tamaño del código.** Medido en la FASE 1 (`fe2819f`) y vuelto a medir en la revalidación
del 10-oct-2026 (`5732676`, ver §0.10):

| | FASE 1 | 10-oct-2026 |
|---|---|---|
| Apps Django | 15 | 15 |
| Modelos | 95 | **98** |
| Migraciones | 157 | **160** |
| Líneas de aplicación (sin tests) | 43.526 | **45.897** |
| Métodos `test_` | 2.178 | **2.300** |
| Comandos de management | 54 | **55** |
| Plantillas HTML | 94 | 94 |
| Entradas en `CELERY_BEAT_SCHEDULE` | 26 | 26 |
| `TextChoices` | 23 | 23 |

---

## §2. Arquitectura real

Monolito Django modular por dominio, con capa de servicios, desplegado como seis procesos
sobre una única imagen.

```text
agente Windows (Python)  ─┐
app móvil Flutter        ─┤
Mikrotik (SNMP)          ─┼─► frontera de entrada
MeshCentral (WebSocket)  ─┤
navegador (HTMX)         ─┘

  ├─ MQTT        apps/mqtt_worker/services.py       (15 manejadores, uno por tópico)
  ├─ REST        apps/mantenimiento/api_views.py, apps/monitoreo/api_views.py
  ├─ pull SNMP   apps/monitoreo/mikrotik.py, apps/monitoreo/enlaces.py
  ├─ WebSocket   apps/monitoreo/adapters/meshcentral.py
  └─ HTTP panel  apps/panel/views/*                (27 módulos)
            │
            ▼
      apps/<app>/services.py   ← lógica de negocio y transiciones de estado
            │
            ▼
      apps/<app>/models.py     ← 95 modelos, 157 migraciones
            │
            ▼
      PostgreSQL + TimescaleDB (4 hypertables)
```

### Procesos en producción (`deploy/docker-compose.yml`)

Seis imágenes desde un único `Dockerfile` vía el ancla `x-app`: `web` (gunicorn,
3 workers, `RUN_MIGRATIONS=1`), `worker` (`run_mqtt_worker`), `celery_worker`
(`--concurrency=2`), `celery_beat`, `meshcentral_worker`, `telegram_bot`. Más `db`,
`redis`, `emqx`, `nginx` y `meshcentral`. Todos con `healthcheck`; los de la app usan
`manage.py verificar_salud --silencioso --solo <pieza>`.

### Patrones realmente utilizados

- **Servicios como frontera transaccional.** `@transaction.atomic` en las funciones de
  servicio, no en las vistas. `select_for_update` donde hay carrera real
  (`apps/activos/services.py:26`, `_obtener_y_bloquear_stock`).
- **Historial inmutable por dominio.** `EventoActivo`, `EventoAuditoria`,
  `EventoDespliegue`, `EventoMantenimiento`, `EventoApertura`, `EventoInstalacion`,
  `EventoMonitoreo`, `EventoEnlaceFarmacia`, `EventoSistemaDetectado`. Varios sobrescriben
  `delete()` para lanzar `NotImplementedError` (`apps/activos/models.py`,
  `apps/auditoria/models.py:48`).
- **QuerySet propio como única fuente de un predicado SQL.** `EstacionQuerySet`
  (`apps/catalogo/models.py:228`) existe porque cada criterio estaba escrito dos veces
  (property en Python + filtro armado en la vista) y se desincronizaba. Los mantiene
  juntos `EquivalenciaPredicadosEstacionTests`, que corre ambos sobre los mismos datos y
  falla si difieren.
- **Puerto/adaptador solo donde encaja.** `apps/monitoreo/adapters/base.py` define
  `FuenteMonitoreo` para fuentes *pull*; MQTT y MeshCentral empujan y no lo implementan,
  decisión explicada en el docstring.
- **Comandos de management en modo simulación.** Los que escriben en masa simulan por
  defecto y exigen `--aplicar`. Declarado como regla en `CLAUDE.md`.
- **Imports diferidos para romper acoplamiento.** `apps.catalogo` no importa
  `apps.monitoreo` a nivel de módulo (`Estacion.estado_meshcentral`,
  `apps/catalogo/models.py:645`); mismo criterio en `apps/catalogo/services.py` y
  `apps/mqtt_worker/services.py`.
- **Constantes de dominio como atributos de clase**, con su contraparte documentada:
  `Estacion.UMBRAL_RELOJ_INCOMUNICADO_SEGUNDOS = 120` está atado a
  `VENTANA_TIMESTAMP_SEGUNDOS` del agente, y el comentario lo dice.
- **Firma HMAC-SHA256 de comandos salientes**, con secreto por estación
  (`Estacion.hmac_secret`) y caída al secreto compartido de flota mientras el agente no
  confirme el propio (`apps/catalogo/services.py:45-88`).
- **Cifrado en reposo con Fernet** para los dos secretos que lo ameritan:
  `ClaveRecuperacionBitLocker.clave_cifrada` y `Grupo.pos_password_cifrada`
  (`apps/catalogo/crypto.py`).

### Observaciones

- `apps/panel` es la única app sin modelos ni migraciones: presentación pura que depende
  de todas las demás. Acoplamiento por diseño, aceptable en esa dirección, pero es el
  punto donde más fácil se filtra lógica de negocio.
- `config/asgi.py` y `ASGI_APPLICATION` existen sin uso: scaffolding de `startproject`.

---

## §3. Mapa de aplicaciones

```text
saidsoft-core/
├── config/                      settings (base/desarrollo/produccion), celery, urls, test_runner
├── apps/
│   ├── catalogo/      ★ núcleo   UnidadNegocio, Grupo, Farmacia, Estacion, VersionAgente,
│   │                             ClaveRecuperacionBitLocker, PerifericoDetectado
│   │                             + crypto.py (Fernet), db.py (conexiones de workers),
│   │                               services.py (901 ln: firma HMAC, comandos al agente)
│   ├── activos/       ★ ITAM     Activo, EventoActivo, Colaborador, Bodega, Ubicacion,
│   │                             Departamento, Cargo, Marca, CategoriaEquipo, TipoConsumible,
│   │                             StockBodega, OrdenCompra(+Detalle), RecepcionLote,
│   │                             MovimientoInventario   (services.py 1.466 ln)
│   ├── monitoreo/     ★ RMM      21 modelos (métricas, alertas, enlaces, servicios POS,
│   │                             eventos de Windows, dispositivos ARP, equipo de borde)
│   │                             + mikrotik.py (SNMP), enlaces.py (ICMP),
│   │                               adapters/meshcentral.py, telegram_bot.py,
│   │                               diagnostico_ia.py, graficos.py, umbrales.py
│   ├── mqtt_worker/              Worker MQTT de larga duración: 15 manejadores de tópico,
│   │                             emqx_admin.py (credencial por estación), WorkerHeartbeat,
│   │                             MensajeMqttFallido, EnrolamientoRechazado
│   ├── mantenimiento/            Mantenimiento, VisitaTecnica, Firma, Imagen, SLA, checklist,
│   │                             Notificacion, ConsentimientoMonitoreo, UbicacionTecnico,
│   │                             AccionOfflineAplicada, CierreEnConflicto
│   │                             + API DRF para la app móvil
│   ├── panel/          ★ UI      Sin modelos. 27 módulos de vistas, urls (298 ln), forms,
│   │                             indicadores, reportes, busqueda, paginacion, progreso, middleware
│   ├── despliegues/              Despliegue, ResultadoDespliegue, EventoDespliegue
│   ├── scripts/                  Script, EjecucionScript, ResultadoEjecucionScript, ScriptProgramado
│   ├── software/                 AplicacionCatalogo, VersionAplicacion, SolicitudInstalacion,
│   │                             ResultadoInstalacion, SoftwareInstaladoDetectado, InventarioProgramado
│   ├── aperturas/                Apertura cero-touch: Plantilla, PerfilEstacion, Paso, Token
│   ├── cuentas/        ★ tenant  PerfilUsuario + services.py: TODO el scoping multi-tenant
│   ├── auditoria/                EventoAuditoria + registrar_evento()  (107 llamadas)
│   ├── cumplimiento/             ActividadCumplimiento + 3 modelos de resultado
│   ├── viaticos/                 ColaboradorZona, ReporteViatico, AlertaViatico
│   └── facturacion/              ActividadMensualEstacion  (1 modelo)
├── agente-prueba/                Agente Windows en Python (+ servicio, build PyInstaller)
├── movil-campo/                  App Flutter de técnicos en campo
├── deploy/                       Compose, Dockerfile, nginx, certs, EMQX, backup, runbooks
├── docs/                         gobernanza (SoA, política, matriz de riesgos),
│                                 entrega-proyecto, auditorías previas (.md y .sql), modulos.md
├── templates/ (94 html), static/, static_src/, tools/
├── CLAUDE.md (12 KB)   README.md (155 KB)   PLAN_MODERNIZACION.md (365 KB)
└── .claude/settings.json  (hoy solo enabledPlugins)
```

Migraciones por app (al cierre de la FASE 1): `monitoreo` 41, `catalogo` 35, `activos` 26,
`mantenimiento` 23, `despliegues` 7, `scripts` 6, `software` 6, `cuentas` 4, `auditoria` 3,
`mqtt_worker` 2, `aperturas`/`cumplimiento`/`facturacion`/`viaticos` 1, `panel` 0. El
Commit 2 agregó `activos/0027`, con lo que `activos` pasa a 27 y el total a 158.

---

## §4. Flujos principales

1. **Enrolamiento de estación.** El agente publica en `/saidsof/enrolamiento/solicitar/` →
   `manejar_enrolamiento` (`apps/mqtt_worker/services.py:134`) → crea/actualiza `Estacion`
   en `PENDIENTE`, fija `hardware_id` (MachineGuid) y lo exige en re-enrolamientos → un
   humano aprueba en el panel (permiso `aprobar_estacion`) →
   `emqx_admin.aprovisionar_credencial_estacion` da credencial MQTT propia y se entrega
   `hmac_secret`. Los rechazos quedan en `EnrolamientoRechazado`.
2. **Latido y telemetría.** `/saidsof/agente/{codigo}/heartbeat/` → `manejar_heartbeat`
   actualiza `estado_conexion`, `ultimo_heartbeat`, versiones, `pos_servidor/bdd/puerto`,
   desfase de reloj, zona horaria y confirmación de pausa →
   `registrar_estado_dispositivo(fuente=mqtt)` → evaluación de reglas. En paralelo,
   `marcar_estaciones_offline_task` cada 60 s (5 min sin latido ⇒ `OFFLINE`).
3. **Comando saliente firmado.** Panel → `firmar_payload` (HMAC-SHA256 de los campos
   unidos con `|`, `apps/catalogo/services.py:29-46`) → publish MQTT → el agente valida
   firma **y** ventana de timestamp ±120 s. Consecuencia documentada: una estación con el
   reloj corrido >120 s queda incomunicada, incluido el comando que le arreglaría el reloj
   (`Estacion.reloj_incomunicado`). `actualizar_agente` es la excepción deliberada que
   acepta una estación pausada.
4. **Monitoreo del enlace (ICMP).** Beat cada 2 min → `sondear_enlaces_farmacias` →
   `registrar_sondeo` → `EstadoEnlaceFarmacia` declara caída tras 3 fallas consecutivas
   (~6 min) → `EventoEnlaceFarmacia` → cada 5 min `notificar_cambios_enlaces` agrupa por
   proveedor, clasifica caídas simultáneas y notifica a `ENLACES_NOTIFICAR_A` +
   `ENLACES_TELEGRAM_CHAT_ID`. Guarda: si casi todo el barrido falla, no registra nada.
5. **Ancho de banda (SNMP).** Beat cada 5 min → `sincronizar_ancho_banda_farmacias`
   resuelve el `ifIndex` de la WAN por la ruta por defecto (dos caminos: `ipRouteIfIndex`
   y `ipCidrRouteIfIndex`+`ipAddrTable`), lee contadores HC de 64 bits, calcula tasa →
   `MuestraRedFarmacia`. La community es el código de farmacia en minúscula. Respaldo:
   `solicitar_sondeo_red_farmacias_via_agente`, que solo entra si el directo no midió en
   `MINUTOS_FRESCURA_RED_FARMACIA` (10 min = dos ciclos).
6. **Identidad y ARP del equipo de borde.** Beat cada 15 min →
   `sincronizar_identidad_equipos` → `EquipoBordeFarmacia` (modelo, serie, RouterOS,
   uptime, `sysName`), con `nombre_coincide` para detectar que `ip_router` apunta a otro
   equipo. `sincronizar_dispositivos_detectados` recorre la tabla ARP →
   `DispositivoDetectado` (clave `(farmacia, mac)`).
7. **Alertas de estación.** `MuestraMetrica` → `evaluar_reglas_metricas` →
   `_condicion_sostenida` (la condición debe mantenerse `duracion_minutos`) →
   `abrir_o_mantener_alerta` → `encolar_notificacion_alerta` → Celery → correo / webhook de
   Teams / Telegram. Si es CRÍTICA y hay `ANTHROPIC_API_KEY`, diagnóstico IA **asíncrono**
   para no retrasar la apertura. `escalar_alertas_task` cada 10 min, umbral 30 min. Cuatro
   métricas no son series de tiempo y se evalúan en su propio punto: `sin_heartbeat`,
   `bitlocker_deshabilitado`, `agente_caido_red_viva`, `pos_errores`.
8. **Cruce MQTT × MeshCentral.** `EstadoDispositivo` guarda una fila por
   `(estacion, fuente)`. `evaluar_cruce_monitoreo` (cada 7 min) distingue "agente caído con
   red viva" de "red caída, ambas fuentes lo ven mal".
9. **Ciclo de vida del activo.** `registrar_ingreso` → `registrar_asignacion` /
   `registrar_ubicacion_farmacia` / `registrar_devolucion` / `registrar_envio_reparacion` /
   `registrar_retorno_reparacion` / `registrar_baja`. Cada transición escribe un
   `EventoActivo`. `Activo.delete()` lanza `NotImplementedError`.
10. **Mantenimiento en campo.** App Flutter → `/api/v1/auth/token/` (DRF
    TokenAuthentication) → `Mantenimiento`/`VisitaTecnica`, checklist,
    `FirmaMantenimiento` (única por tipo), `ImagenMantenimiento` (servida por vista con
    control de acceso + `X-Accel-Redirect`). Cola offline con `AccionOfflineAplicada` como
    clave de idempotencia y `CierreEnConflicto` con escalamiento a las 4 h.
11. **Despliegues y scripts.** `Despliegue` → `ResultadoDespliegue` por estación, con
    umbral de error que detiene la corrida (`DESPLIEGUE_UMBRAL_ERROR_PCT_DEFAULT = 10 %`) y
    distribución en cascada por caché de farmacia. `EjecucionScript` →
    `ResultadoEjecucionScript`, con caducidad cada 10 min y generación de programadas a
    las 06:00.
12. **Apertura cero-touch.** `PlantillaApertura` + `PerfilEstacionPlantilla` +
    `PasoPlantilla` → `Apertura` → `PasoApertura`, con `TokenApertura` y el constraint
    `una_apertura_vigente_por_farmacia`.
13. **Bot de Telegram bidireccional.** Proceso propio (`run_telegram_bot`, 1.181 ln) con
    `/enlaces`, `/estado`, `/alertas`, `/farmacia`. Consultar se gobierna con
    `TELEGRAM_CHAT_IDS_AUTORIZADOS`; **accionar** exige un `PerfilUsuario.telegram_chat_id`
    y reusa el RBAC de Django (`usuario_de_chat_telegram`).

---

## §5. Base de datos

**Motor.** `DATABASES['default'] = env.db('DATABASE_URL', default=sqlite:///db.sqlite3)`
(`config/settings/base.py:106`). Producción: PostgreSQL 16 + TimescaleDB 2.17.2.
`CONN_MAX_AGE = 60` con advertencia explícita en el comentario: `workers de gunicorn ×
procesos de Celery` tiene que entrar en `max_connections` (100 hoy, 16 en uso).

**Series de tiempo.** 4 hypertables: `muestra_metrica`, `muestra_red_farmacia`,
`muestra_servicio_pos`, `evento_monitoreo` (migración `0035_hypertables_de_verdad`), con
compresión y retención nativas (`0040_compresion_hypertables`). Las dos migraciones se
guardan con `if conexion.vendor != 'postgresql': return`, así que en SQLite se aplican sin
crear nada. La purga **no** usa `DELETE` en una hypertable: `es_hypertable()` decide y
`_purgar_serie` usa `drop_chunks`.

**Convención de tablas.** `db_table` explícito y en snake_case español en los 95 modelos
(`activo`, `evento_activo`, `estado_enlace_farmacia`…). Ninguna tabla usa el nombre por
defecto de Django.

**Integridad referencial** (186 declaraciones en los `models.py`):

| `on_delete` | Cantidad | Criterio observado |
|---|---|---|
| `PROTECT` | 86 | Catálogos y referencias de negocio: `Farmacia.grupo`, `Farmacia.unidad_negocio`, `Estacion.farmacia`, `Activo.marca/categoria/orden_compra/bodega_actual/unidad_negocio`, `Alerta.regla` |
| `CASCADE` | 52 | Hijos sin sentido sin el padre: `EventoActivo.activo`, `StockBodega.bodega`, `ClaveRecuperacionBitLocker.estacion`, `Alerta.estacion`, `FirmaMantenimiento.mantenimiento` |
| `SET_NULL` | 48 | Referencias opcionales, sobre todo a `AUTH_USER_MODEL` y a `Colaborador`: `Activo.colaborador_actual`, `Activo.farmacia`, `Activo.estacion`, `EventoActivo.usuario` |

**Constraints y unicidad: 31 declaraciones.** Las que llevan lógica de dominio:

- `Activo.un_slot_por_farmacia`: `UniqueConstraint(['farmacia','slot'])` con
  `condition=~Q(slot='') & ~Q(estado='dado_de_baja')` — un activo dado de baja suelta su
  puesto.
- `Activo.codigo` único y `editable=False`, generado por `generar_codigo_activo`
  (`CR-TIPO-NNNN`).
- **Sin `unique` sobre `Activo.ip`, a propósito**: la Epson L3250 va por WiFi/DHCP y la
  misma IP pasa de un equipo a otro sin que sea error de datos.
- `DispositivoDetectado.un_dispositivo_por_mac_y_farmacia`: la identidad es la MAC, no la IP.
- `FirmaMantenimiento.firma_unica_por_tipo`: la mitad barata de la idempotencia.
- `PerifericoDetectado`: `unique_together ('estacion','device_id')`.
- Únicos: `Colaborador.cedula`, `Bodega.codigo`, `Farmacia.codigo`, `Estacion.codigo`,
  `Estacion.token_enrolamiento`, `OrdenCompra.numero_oc`, `RecepcionLote.uuid`.
- `Cargo`: `unique_together ('nombre','departamento')`. `StockBodega`:
  `('bodega','tipo_consumible')`.
- Validadores regex en `codigo` de `UnidadNegocio`/`Grupo`/`Farmacia` (`^[A-Z0-9]+$`) y de
  `Estacion` (`^[A-Z0-9]+-[A-Z0-9]+$`).

**Índices: 18 declaraciones**, todas de series o listados calientes:
`('estacion','-timestamp')`, `('farmacia','-timestamp')`, `('farmacia','-inicio')`,
`('farmacia','-detectado_en')`, `('farmacia','ip')`, `('estacion','-ultima_vez')`, y uno
parcial para alertas abiertas (`0039_indice_alerta_estado_abierta`). La migración
`0041_quitar_indices_redundantes_de_prefijo` quitó nueve índices redundantes de prefijo y
dejó una prueba que consulta el catálogo de PostgreSQL para que no vuelvan — **la única
prueba del repo que se saltea si el motor no es Postgres** (`apps/monitoreo/tests.py:8290`).

**Concurrencia.** `select_for_update` en el stock de bodega; `version =
PositiveIntegerField` como concurrencia optimista en `OrdenCompra` y `OrdenCompraDetalle`
(equivalente al `@Version` del InvTICS original); excepción propia `ConcurrencyError`.

**Campos nullable: criterio explícito y distinto de "pendiente".** El patrón del repo es
documentar en el `help_text` qué significa el nulo: `Bodega.unidad_negocio` vacío = bodega
compartida; `Colaborador.unidad_negocio` vacío = administrativo de CRESIO (*"el nulo es el
valor correcto para ellos, no un dato pendiente"*); `Farmacia.ancho_contratado_mbps` vacío
= *"no se sabe, que NO es lo mismo que cero"*; `Estacion.bitlocker_habilitado` `null` =
nunca se consultó, distinto de `False`; `windows_update_pendientes` `null` = nunca se
escaneó, distinto de 0.

**Orden determinístico.** `EventoActivo.ordering = ['activo','timestamp','pk']` y
`EventoAuditoria.ordering = ['-timestamp','-pk']` llevan `pk` de desempate porque
`auto_now_add` puede repetirse en el mismo tick y la paginación saltaba o repetía filas.

**Rendimiento.** `select_related`/`prefetch_related` aparece en 50 archivos no-test,
incluidos los módulos de vistas del panel más cargados. Hay trabajo reciente de N+1 en el
historial (`2ff970a`, `fe8c69b`, `0526b65`, `3110edf`). **No se auditó consulta por
consulta**: ese es el alcance del `revisor-bd`.

---

## §6. Inventario (cómo funciona hoy)

Las siete entidades del encargo existen todas, repartidas en dos apps:

| Entidad | Dónde vive | Rol real |
|---|---|---|
| `Activo` | `apps/activos/models.py` | **Entidad central del inventario físico.** Nunca se elimina (`delete()` lanza `NotImplementedError`) |
| `EventoActivo` | `apps/activos/models.py` | Historial inmutable del activo, **12 tipos de evento** (eran 11 al cierre de la FASE 1; el Commit 2 agregó `CORRECCION_DATOS`) |
| `Colaborador` | `apps/activos/models.py:119` | Receptor y custodio (fusiona el `Custodio` de InvTICS). `OneToOne` opcional con `User` |
| `Bodega` | `apps/activos/models.py:96` | Almacén; su custodio es un `User`, no un `Colaborador` |
| `Ubicacion` | `apps/activos/models.py:69` | **Agencia/sede** con dirección y coordenadas. La usa la visita técnica, es la sede del colaborador y —desde el 10-oct-2026— también `Activo.ubicacion`, para el equipo que no está en farmacia ni en bodega (matriz, oficinas) |
| `Farmacia` | `apps/catalogo/models.py:106` | El **sitio**. Tenant vía `unidad_negocio`. `tipo` distingue farmacia / tienda / administrativo |
| `Estacion` | `apps/catalogo/models.py:292` | **Equipo con agente RMM**, código `FARMACIA-SUFIJO` |

Los 12 tipos de `EventoActivo.TipoEvento`: `ingreso`, `asignacion`,
`consumible_entregado`, `devolucion`, `envio_reparacion`, `retorno_reparacion`, `baja`,
`baja_recomendada`, `transito`, `ubicacion_actualizada`, `datos_red_cargados`,
`correccion_datos`.

**Dónde vive la lógica de negocio:** `apps/activos/services.py` (1.466 ln, 40 funciones).
El panel y el admin la reusan; no hay lógica de transición en las vistas.

**Cadena de inventario (declarado → observado):**

```text
UnidadNegocio ──< Farmacia ──< Estacion ──1:1── Activo.estacion   (vínculo por nº de serie)
                     │                              │
                     └──< Activo.farmacia           ├──< EventoActivo
                                                    ├── Activo.bodega_actual      ──► Bodega
                                                    ├── Activo.colaborador_actual ──► Colaborador ──► Ubicacion
                                                    └── Activo.slot + ubicacion_interna  (topología)

Farmacia ──1:1── EquipoBordeFarmacia      (observado por SNMP: modelo, serie, RouterOS, uptime)
Farmacia ──< DispositivoDetectado         (observado por ARP, identidad = MAC)
Farmacia/Activo ── EstadoRedActivo        (observado por ping del agente)
```

**Procedencia del dato — distinción que debe mantenerse explícita:**

| Procedencia | Dónde se escribe | Ejemplos |
|---|---|---|
| **Declarado (manual)** | Panel, formularios, comandos de importación | `Activo.*`, `Activo.ip`/`mac` cuando no hay estación, `Farmacia.ip_router`, `segmento_red`, `circuito_proveedor` |
| **Agente (MQTT)** | `apps/mqtt_worker/services.py` | `Estacion.ip_lan`, `numero_serie`, `hostname`, versiones, BitLocker, Windows Update, periféricos, software, desfase de reloj |
| **SNMP (equipo de borde)** | `apps/monitoreo/mikrotik.py` | `EquipoBordeFarmacia`, `MuestraRedFarmacia` |
| **SNMP (otros dispositivos)** | `apps/monitoreo/snmp/` + `tasks.py` | `ObjetivoSnmp`, `LecturaSnmpActual`, `PerfilSnmp` |
| **ARP (vía SNMP)** | `mikrotik.sincronizar_dispositivos_detectados` | `DispositivoDetectado` (MAC + IP vista) |
| **ICMP** | `apps/monitoreo/enlaces.py`, ping del agente | `EstadoEnlaceFarmacia`, `EstadoRedActivo` |

**Tres decisiones de diseño que NO deben "mejorarse":**

1. **Tres niveles de ubicación, deliberadamente separados.** `Activo.farmacia` dice en qué
   sitio de la red está el equipo. `Activo.ubicacion` → `Ubicacion` (agencia/sede con
   dirección y coordenadas) dice dónde está cuando **no** está en una farmacia ni en
   bodega: matriz, oficinas. `UbicacionInterna` (`TextChoices`: rack / caja / bodega /
   oficina / otro) dice dónde está montado **dentro** de la farmacia, y el docstring
   explica que es *"deliberadamente grueso"*.
   **Ojo:** hasta el 10-oct-2026 `Activo` no tenía FK a `Ubicacion` y la regla era "dos
   niveles"; el commit `e6b5648` agregó el tercero porque un equipo en matriz *"tenía que
   elegir entre dos mentiras para poder existir"*. Verificar el estado actual antes de
   apoyarse en esta regla.
2. **La IP tiene una única fuente de verdad por activo, resuelta en código y no en la
   base.** `Activo.ip_efectiva` devuelve `(ip, origen)` con
   `origen ∈ {'agente','manual',None}`: si hay estación vinculada manda `Estacion.ip_lan`.
   `Activo.clean()` prohíbe cargar `ip`/`mac` a mano cuando ya hay estación, y el docstring
   aclara por qué es validación de modelo y **no** constraint: una fila existente con los
   dos datos tiene que poder guardarse igual.
3. **Estados de inventario y de monitoreo están separados y así deben quedar.**
   `Activo.Estado` = `en_bodega` / `asignado` / `en_reparacion` / `dado_de_baja`.
   `Estacion.EstadoConexion` = `nunca_conectada` / `online` / `offline`. No se mezclan.
   `EN_TRANSITO` fue **eliminado** de `Activo.Estado` tras confirmar 0 filas en producción:
   ninguna transición lo asignaba y ninguna vista lo leía. Y `ASIGNADO` significa dos cosas
   según el contexto, documentado: "en servicio" para un PDV en farmacia, "entregado a una
   persona" para un equipo de oficina.

**Inventario declarado vs. observado.** `Activo` es lo declarado; `DispositivoDetectado` es
lo que el Mikrotik ve en su LAN por ARP; `EquipoBordeFarmacia` es la identidad del router
leída por SNMP. El cruce responde qué hay enchufado que nadie inventarió y qué está
inventariado pero no aparece. Hoy ese cruce casi no tiene contra qué cruzar: según
`CLAUDE.md` (medición del 5-oct-2026, dato de producción no verificable desde el repo) hay
22 activos cargados contra 701 farmacias y 66 estaciones con agente.

**Vínculo automático Activo ↔ Estación.** `vincular_activos_por_numero_serie` (Beat 04:00)
cruza por `numero_serie` con dos guardas: `serie_utilizable()` y `_sin_series_de_relleno()`
filtran series basura tipo `"To be filled by O.E.M."`. También existen
`crear_activos_desde_estaciones` (comando `crear_activos_desde_rmm`, con `--aplicar`) y
`datos_hardware_desde_estacion`.

**Topología de farmacia.** `Activo.slot` nombra el puesto dentro del esquema estándar
(`"mikrotik"`, `"switch"`, `"voip"`, `"impresora_A"`, `"medianet_B"`), con el constraint
`un_slot_por_farmacia`. `SlotTopologia`, `slots_de_farmacia`, `crear_topologia_farmacia`,
`completar_datos_topologia` y `traducir_planilla_ips` viven en `apps/activos/services.py`.

---

## §7. Monitoreo (lo que realmente existe)

| Pieza | Existe | Dónde | Cómo funciona |
|---|---|---|---|
| **Agente** | Sí | `agente-prueba/agente_prueba.py` (+ `servicio_windows.py`, `build.ps1`) | Python compilado con PyInstaller, servicio de Windows. Reporta latido, métricas, hardware, BitLocker, Windows Update, software, periféricos, eventos de Windows, servicios del POS, errores del log del POS, red de la farmacia. Verifica firma HMAC y ventana de timestamp ±120 s. Autocorrección de reloj (0.30+) y pausa remota reversible |
| **ICMP** | Sí | `apps/monitoreo/enlaces.py` (624 ln) | `subprocess` al `ping` del sistema. 3 fallas consecutivas ⇒ caída. `verificar_ping_disponible()` detecta que falte el binario y lo dice en vez de reportar la flota caída. Guarda contra barridos masivamente fallidos. Tres vías de ingesta (Beat, comando, API) comparten `registrar_sondeo` |
| **SNMP — equipo de borde** | Sí | `apps/monitoreo/mikrotik.py` (783 ln) | pysnmp 7 (asyncio nativo), `asyncio.run()` dentro del task sincrónico, `Semaphore(25)`. Tres usos: ancho de banda (contadores HC de 64 bits), identidad del equipo de borde, tabla ARP. Community = código de farmacia en minúscula; `ifIndex` de la WAN resuelto por la ruta por defecto, no por nombre. `_motor_snmp()` es un context manager que cierra el dispatcher siempre |
| **SNMP — lectura genérica** | Sí, **desde el 10-oct-2026** | `apps/monitoreo/snmp/` (paquete de 7 módulos, ~1.195 ln) | Capa **separada y deliberada**, no un reemplazo de `mikrotik.py`. Catálogo declarativo de OIDs **como datos** (`catalogo.py`: `IMPRESORA`, `RED`, `GENERICO`), transporte en `cliente.py`, interpretación en `impresoras.py` y `normalizar.py`, y `leer(ip, comunidad, catalogo)` como entrada única. Criterio de diseño explícito: *"agregar un switch o una UPS tiene que ser una entrada en `catalogo.py`, no un archivo nuevo"*. No toca la base ni Celery: son funciones puras, probadas contra salida real guardada como fixture. Persiste en los modelos nuevos `PerfilSnmp` / `ObjetivoSnmp` / `LecturaSnmpActual`; lo agenda `apps/monitoreo/tasks.py` vía `snmp.sondeo` |
| **ARP** | Sí | `mikrotik._leer_tabla_arp` + `sincronizar_dispositivos_detectados` | Walk de la tabla ARP del Mikrotik, con tope `_MAX_ENTRADAS_ARP`. Un equipo que se desconecta **no se borra**: deja de actualizarse, que es lo que permite notar que algo desapareció |
| **MQTT / EMQX** | Sí | `apps/mqtt_worker/`, EMQX 5.8.3 | 15 tópicos bajo `/saidsof/`, TLS en 8883, autenticación y autorización en base interna con `no_match: deny`. Credencial MQTT **por estación** vía API administrativa de EMQX; sin configurar, cae a la compartida sin romper el enrolamiento. `MensajeMqttFallido` guarda el payload crudo de lo no procesable |
| **Celery** | Sí | `config/celery.py`, `apps/*/tasks.py` | **26 entradas** en `CELERY_BEAT_SCHEDULE`. Las diarias usan `crontab` y no intervalos: un `schedule` numérico es relativo al arranque de beat, cuyo estado se pierde en cada despliegue. Las purgas se escalonan de a 10 min |
| **Redis** | Sí | imagen `redis:7-alpine` | **Solo** broker y backend de resultados de Celery. No hay `CACHES`. En desarrollo no hace falta: `CELERY_TASK_ALWAYS_EAGER = True` |
| **Alertas** | Sí, **dos motores** | `apps/monitoreo/services.py`, `enlaces.py` | (a) `ReglaAlerta`→`Alerta` por estación, con condición sostenida, severidad, ventana de mantenimiento, escalamiento a 30 min, apertura automática opcional de mantenimiento y diagnóstico IA asíncrono para CRÍTICAS. (b) Enlaces: `EstadoEnlaceFarmacia`/`EventoEnlaceFarmacia`, **sin** crear `Alerta`, con destinatarios propios. La separación es deliberada y está justificada por volumen |
| **Notificación** | Sí | `apps/monitoreo/services.py:700-870` | Correo (`EMAIL_TIMEOUT = 10`, porque `fail_silently` no cubre un socket colgado), webhook de Teams y Telegram — los dos últimos con `urllib`, sin dependencia extra. `CanalNotificacion` por unidad de negocio |
| **MeshCentral** | Sí | `apps/monitoreo/adapters/meshcentral.py`, `run_meshcentral_worker` | WebSocket `control.ashx` en tiempo real + resync cada 15 min como red de seguridad. El vínculo `Estacion.meshcentral_node_id` es **manual** |
| **Servicios del POS** | Sí | `apps/monitoreo/servicios_pos.py`, `ServicioPosMonitoreado` | Catálogo publicado a los agentes; el agente chequea TCP / HTTP / Postgres / ping y reporta. `EstadoServicioPos` + `MuestraServicioPos` |
| **Eventos de Windows** | Sí | `EventoSistemaVigilado` / `EventoSistemaDetectado` | Catálogo de eventos vigilados publicado a los agentes |
| **Salud del propio stack** | Sí | `manage.py verificar_salud`, `WorkerHeartbeat` | Lo usan los `healthcheck` de Compose. Umbrales en `apps/monitoreo/umbrales.py` |
| **Bot de Telegram** | Sí | `telegram_bot.py` (1.181 ln), proceso propio | Bidireccional: consultar con lista blanca de chats; accionar exige `PerfilUsuario.telegram_chat_id` y reusa el RBAC de Django |

**Verificado como ausente, y declarado como pendiente en el propio código** (no omitido):
la fuente de ESET PROTECT (`apps/monitoreo/adapters/base.py` reserva el puerto para ella,
esperando aprobación de acceso a su API) y el push FCM real
(`apps/cuentas/services.py:enviar_push` es un no-op que loguea).

---

## §8. Convenciones

- **Idioma: español en todo el dominio.** Clases (`Activo`, `EventoEnlaceFarmacia`), campos
  (`bodega_actual`, `desfase_reloj_segundos`), `db_table`, funciones
  (`registrar_ingreso`, `sondear_enlaces_farmacias`), variables locales, docstrings y
  comentarios. El inglés aparece solo donde lo impone Django/terceros (`on_delete`,
  `related_name`, `verbose_name`).
- **Nombres.** Clases `CapWords`; funciones y campos `snake_case`; constantes de módulo
  `MAYUSCULA_CON_GUIONES`; privados con `_` inicial; `db_table` snake_case singular
  (`activo`, no `activos`); `TextChoices` para enumeraciones, con el valor en minúscula y
  la etiqueta legible en español.
- **Docstrings: explican el *por qué*, con fecha y medición.** Es la convención más
  distintiva del repo. Ejemplos: `config/test_runner.py` documenta el deadlock del
  6-oct-2026 con el mensaje de error textual; `mikrotik._motor_snmp` documenta la fuga de
  descriptores con la medición (20 `get_cmd` = +20 descriptores). **Consecuencia práctica:
  antes de proponer un cambio hay que leer el docstring, porque la alternativa "obvia"
  suele estar ahí explicada y descartada.**
- **Servicios.** Una función por transición, argumentos *keyword-only* (`*`), decorada con
  `@transaction.atomic`, que escribe su `Evento*` correspondiente y devuelve el objeto o un
  `dict` de resumen.
- **Comandos de management.** Los que escriben en masa simulan por defecto y exigen
  `--aplicar`; imprimen un resumen contable. Los `seed_*` son idempotentes.
- **Excepciones.** `ValidationError` de Django para reglas de negocio en modelos/forms;
  `ValueError` para argumentos inválidos de servicio; excepciones propias donde el caller
  tiene que distinguir (`ConcurrencyError`, `ErroresDeCarga`, `FalloLocalDeSondeo` con sus
  subclases `PingNoDisponible` y `RecursosDelHostAgotados`); `PermissionDenied` para el
  tenant.
- **Logging.** `logger = logging.getLogger(__name__)` por módulo; `logger.exception` en los
  `except Exception` de los bordes de integración. `LOGGING` manda todo a consola sin el
  filtro `require_debug_true` del default de Django, porque en un contenedor stdout **es**
  el log.
- **Pruebas.** Un único `tests.py` por app (los más grandes: `panel` 8.718 ln, `monitoreo`
  8.504 ln), `TestCase` de Django, clases nombradas `<Tema>Tests` con docstring que explica
  **qué regresión previene**. Hay pruebas que vigilan contratos, no comportamiento:
  `EquivalenciaPredicadosEstacionTests` (SQL vs. Python) y
  `ContratoTenantDeAuditoriaTests` (lista `ATRIBUTOS_RUTA_TENANT`).

---

## §9. Comandos reales y entorno de pruebas

### 9.1 Comandos (tomados de los archivos del proyecto)

```sh
# Entorno local — ver deploy/README-local.md
docker compose -f deploy/docker-compose.yml --env-file deploy/.env up -d db redis
#   la base queda en 127.0.0.1:5433; credenciales en deploy/.env
#   y DATABASE_URL en el .env de la raíz (ambos fuera de git)

python manage.py migrate
python manage.py runserver

# Verificación antes de dar por terminado un cambio — CLAUDE.md
python manage.py check
python manage.py makemigrations --check --dry-run
python manage.py test                       # suite completa; venv en .venv/

# Workers de larga duración
python manage.py run_mqtt_worker
python manage.py run_meshcentral_worker
python manage.py run_telegram_bot
celery -A config worker --loglevel=INFO --concurrency=2
celery -A config beat --loglevel=INFO

# Salud
python manage.py verificar_salud --silencioso --solo mqtt_worker

# Despliegue — CLAUDE.md
git pull
docker compose --env-file .env build        # SIN argumento: son SEIS imágenes
docker compose --env-file .env up -d
#   el entrypoint corre las migraciones solo (RUN_MIGRATIONS=1 en `web`)
#   un cambio en deploy/nginx/nginx.conf exige RECREAR el contenedor, no recargarlo
```

54 comandos de management. Los de mayor peso operativo: `importar_farmacias`,
`importar_directorio_sucursales`, `importar_circuitos_proveedor`, `crear_activos_desde_rmm`,
`crear_topologia_farmacia`, `completar_topologia`, `importar_planilla_ips`,
`armar_paquete_agente`, `generar_script_instalacion`, `pausar_flota`,
`descubrir_dispositivos_farmacia`, `probar_snmp_farmacia`, `sondear_enlaces`,
`seed_permisos`, `publicar_apk`.

**Lint: no hay comando**, porque no hay linter configurado. El único análisis estático
disponible hoy es `python -m pyflakes apps config manage.py` con el `pyflakes 3.4.0` que
está en `.venv` sin declararse.

### 9.2 Entorno de pruebas — qué base usan y si es seguro ejecutarlos

**No existe un settings de pruebas.** `config/settings/` tiene solo `base.py`,
`desarrollo.py` y `produccion.py`.

Cadena real:

1. `manage.py:12` fija `DJANGO_SETTINGS_MODULE = config.settings.desarrollo`.
2. `config/settings/base.py:19` lee `BASE_DIR/.env`.
3. El `.env` de este repo define `DATABASE_URL` apuntando a **PostgreSQL en
   `127.0.0.1:5433`** — el contenedor `deploy-db-1`, el mismo servidor que tiene los datos
   de desarrollo. (Verificado leyendo solo el esquema y el host; usuario y clave nunca se
   leyeron ni se transcriben.)
4. Por lo tanto `python manage.py test` **se conecta a ese PostgreSQL** y crea/destruye una
   base `test_<nombre>`.

Consecuencias:

- **Los datos de desarrollo no se tocan**: Django usa una base `test_` separada y la
  destruye al terminar. Pero hace falta **conexión viva a PostgreSQL y permiso de
  `CREATE DATABASE`**, y eso está prohibido en esta etapa.
- **Correr en SQLite es posible pero no equivalente.** Bastaría sobrescribir
  `DATABASE_URL`, porque el default de `base.py` es SQLite. El repo documenta el precio en
  tres lugares (`CLAUDE.md`, `deploy/README-local.md`,
  `.github/workflows/pruebas.yml:24-31`): **125 pruebas que pasaban en SQLite fallaban en
  PostgreSQL** — sumas infladas por JOIN en viáticos, resultados sin `ORDER BY`,
  `select_for_update` casi un no-op, `ip_lan='localhost'` que el tipo `inet` rechaza.
  Además, en SQLite las migraciones `0035`/`0040` se saltean y las cuatro hypertables
  quedan como tablas comunes, así que `RetencionDeSeriesPorChunksTests` —que existe para
  comprobar que la purga **no** emite `DELETE` en una hypertable— evaluaría otra cosa.
  **Solo una prueba del repo se saltea sola fuera de Postgres**
  (`apps/monitoreo/tests.py:8290`); el resto no se protege.
- **Hay un runner propio.** `TEST_RUNNER = 'config.test_runner.RunnerSinJobsDeTimescale'`
  desagenda los 8 jobs de retención/compresión de TimescaleDB en la base de pruebas, porque
  el *background worker* de Timescale corría `compress_chunk` dentro de la suite y producía
  deadlocks intermitentes. Solo actúa si el motor es PostgreSQL con la extensión.
- **El CI sí corre contra el motor real**: servicio `timescale/timescaledb:2.17.2-pg16`,
  `config.settings.desarrollo`, secretos descartables inline y `BITLOCKER_ENCRYPTION_KEY`
  generada al vuelo (debe ser una clave Fernet válida, no cualquier cadena). Pasos:
  `check` → `makemigrations --check --dry-run` → `test --noinput --verbosity 2`. Job aparte
  para Flutter (`flutter analyze` + `flutter test`).

**Veredicto para la auditoría: NO es seguro ejecutar la suite en esta etapa**, porque exige
conectarse a PostgreSQL. Decisión aprobada: **las pruebas quedan prohibidas** (ni Postgres
ni SQLite) y se autorizarán en una fase específica. Hasta entonces el análisis es solo por
lectura y **ningún hallazgo puede marcarse `Reproducción: SÍ`**.

**Lo único ejecutable sin tocar ninguna base**, ya verificado en la FASE 1:
`python -m pyflakes apps config manage.py` → 48 avisos. Sus resultados deben clasificarse
como hallazgo real con evidencia, ruido/falso positivo, o mejora de tooling — **no se
convierte un warning en bug automáticamente**. Tiene ruido conocido: no respeta `# noqa`,
así que marca los `import *` intencionales de `desarrollo.py`/`produccion.py` y los
re-exports de `apps/panel/views/__init__.py`.

---

## Estado del documento

- **FASE 1**: completada y aprobada. Línea base histórica `master @ fe2819f`.
- **FASE 1B**: este archivo.
- **FASE 2**: **completada.** `CLAUDE.md` y `.claude/settings.json` ampliados, y creados los
  cuatro agentes `revisor-arquitectura`, `cazador-bugs`, `revisor-bd` y
  `revisor-inventario` en `.claude/agents/`.
- **FASE 4**: **completada el 10-oct-2026** sobre `master` @ `0515773`. Los cuatro agentes
  corrieron una vez cada uno, en solo lectura. Resultado en
  **`docs/auditoria/fase4-hallazgos.md`** (94 hallazgos únicos, 12 de severidad ALTA, los
  cuatro informes íntegros). **Ningún hallazgo fue corregido y ninguno está reproducido.**
- **Fase siguiente, pendiente de decisión humana**: triage de los 94 hallazgos y
  autorización para ejecutar pruebas contra PostgreSQL, que es lo único que puede mover un
  hallazgo a `Reproducción: SÍ`.

Hasta la FASE 2 este archivo era el único que la auditoría había escrito en el repositorio.
Desde el 10-oct-2026 son cuatro los caminos que le pertenecen: este archivo,
`fase4-hallazgos.md`, `.claude/agents/` y los bloques agregados a `CLAUDE.md` y
`.claude/settings.json`. **Ningún archivo de código, migración o dato fue tocado por la
auditoría.**
