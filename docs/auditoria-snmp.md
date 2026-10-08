# Auditoría técnica: incorporar monitoreo SNMP genérico a SAIDSOFT

**Fecha:** 7-oct-2026
**Alcance:** auditoría del código existente antes de escribir una línea de SNMP nuevo.
Primer caso de uso: impresoras administrativas HP / Ricoh / Xerox. Extensión posterior:
switches, routers, MikroTik, APs, UPS, firewalls, PDU.
**Prioridad declarada:** ESTABILIDAD > REUTILIZACIÓN > MANTENIBILIDAD > ESCALABILIDAD > NUEVA FUNCIONALIDAD.

Este documento **no propone código**. Propone qué reusar, qué no tocar, y en qué orden.

---

## 0.0 Estado de medición (REGLA 0.5)

**Regla vigente del proyecto:** no crear modelos, migraciones, tareas Celery ni código de
polling basándose en estimaciones. Si un dato se puede obtener de la base o de una
impresora real, se mide primero.

Esta tabla dice de dónde viene cada afirmación de este documento. Hay que mantenerla al
día: lo que esté en "ASUMIDO" no puede justificar una migración.

| Afirmación | Origen | Estado |
|---|---|---|
| Estructura de apps, modelos, servicios, adaptadores | lectura del código | **MEDIDO** |
| `Alerta.estacion` es FK obligatoria a `Estacion` | `models.py:357` | **MEDIDO** |
| `Activo` tiene `ip`, `mac`, `ubicacion_interna` (migr. 0022) y `slot` (migr. 0024) | migraciones | **MEDIDO** |
| 4 hypertables, retención 30 d, compresión | migr. 0035 y 0040 | **MEDIDO** |
| `test_runner.py` desagenda 8 jobs | código | **MEDIDO** |
| La community no aparece en ningún `logger` | búsqueda en todo el repo | **MEDIDO** |
| Existe cifrado Fernet reusable (`catalogo/crypto.py`) | código | **MEDIDO** |
| Servicios desplegados y comandos de cada uno | `docker-compose.yml` | **MEDIDO** |
| `Semaphore(25)`, `timeout=3`, `retries=0` | `mikrotik.py` | **MEDIDO** |
| OIDs de Printer-MIB y HOST-RESOURCES-MIB | RFC 3805 / RFC 2790 | **MEDIDO** (norma) |
| Punto de quiebre del polling ~800-1.000 dispositivos | aritmética sobre los parámetros medidos | **CALCULADO** — se valida con la prueba de carga de la FASE 4 |
| Volúmenes de almacenamiento | aritmética, 200 B/fila | **CALCULADO** |
| Inventario de impresoras: cantidad, marca, IP, estación | **producción, 7-oct-2026** | **MEDIDO** — ver abajo |
| Respuesta SNMP de los dispositivos reales | **producción, 7-oct-2026** | **MEDIDO** — ver abajo |
| **Si algún contrato se factura por clic** | — | **SIN MEDIR** — es una pregunta a administración, no al sistema |

---

## 0.1 MEDICIÓN CONTRA PRODUCCIÓN — 7-oct-2026

Ejecutada por SSH contra `10.111.6.20`, **solo lectura**, con la base y el contenedor de
Celery reales. Las consultas usaron las variables de entorno del propio contenedor de
PostgreSQL, así que ninguna credencial se leyó ni quedó escrita en ningún archivo.

### El resultado cambia la premisa del proyecto

| Pregunta | Respuesta medida |
|---|---|
| ¿Cuántas impresoras hay en el inventario? | **3** |
| ¿Cuántas son HP, Ricoh o Xerox? | **CERO.** 2 Epson (una L3250) y 1 sin marca |
| ¿Cuántas tienen IP? | **1 de 3** |
| ¿Cuántas tienen número de serie? | **0 de 3** |
| ¿Cuántas tienen `estacion`? | **0 de 3** |
| ¿Cuántas tienen `farmacia`? | 2 de 3 (ML016); la que tiene IP **no** tiene farmacia |
| ¿La única con IP responde SNMP? | **NO.** Probada con `public`, `private`, `epson`, `admin`, `internal`, `public1` |
| ¿Responde ping? | **SÍ, 10,8 ms** desde dentro del contenedor de Celery |
| ¿Algún dispositivo visto por ARP habla SNMP? | **NINGUNO de 29** (con `public`) |
| ¿El instrumento de medición sirve? | **SÍ** — control positivo abajo |

**Total de activos en producción: 22**, repartidos así: DSK 8, IMP 3, PIN 3, NET 2, TEL 2,
CAM 1, BIO 1, LAP 1, ALM 1. Contra 701 farmacias modeladas.

### Control positivo — por qué los "no responde" son creíbles

Sin esto, un barrido todo en timeout no distingue "los equipos no hablan SNMP" de "mi
herramienta está rota". Se sondearon tres Mikrotik con su community (el código de farmacia
en minúscula, convención de `_comunidad_para`):

```
gat01   10.201.1.129    RouterOS RB951Ui-2nD
gnb01   10.101.50.65    RouterOS RB951Ui-2nD
mcar3   10.101.41.193   RouterOS RB951Ui-2HnD
```

Los tres respondieron. **El instrumento funciona; el silencio de las impresoras es real.**

### 8-oct-2026: la primera Ricoh real — la compuerta de la FASE 0 SE CIERRA

`RICOH MP C2503` en `10.111.9.66`, sondeada desde `deploy-celery_worker-1` con
`probe_snmp_impresora.py`. Responde ping a 12 ms y **responde SNMP con community
`public`: no hubo nada que habilitar**.

| Dato | Valor | OID |
|---|---|---|
| `sysDescr` | `RICOH MP C2503 1.22 / RICOH Network Printer…` | `1.3.6.1.2.1.1.1.0` |
| `sysObjectID` | `1.3.6.1.4.1.367.1.1` → Ricoh | `1.3.6.1.2.1.1.2.0` |
| **Número de serie** | **`E215M660354`** | `prtGeneralSerialNumber` |
| **Contador** | **`509.664`** | `prtMarkerLifeCount` |
| **Unidad del contador** | **`8` = HOJAS, no impresiones** | `prtMarkerCounterUnit` |
| Estado | `hrPrinterStatus=3` (idle), `hrDeviceStatus=3` (**warning**) | HOST-RESOURCES |
| Display | `No hay papel: Bandeja 2` | `prtConsoleDisplayBufferText` |
| Alerta | `No hay papel: Bandeja 2 {13300}` | `prtAlertDescription` |
| Bandejas | `165`, `0`, `0` hojas | `prtInputCurrentLevel` |

**Suministros: 5 filas, y el índice NO es CMYK.** Es la trampa de §4.3 confirmada en datos
reales:

| Índice | `Type` | `Colorant` | Descripción | Nivel |
|---|---|---|---|---|
| `.1.1` | 3 (tóner) | black | Tóner negro | **80 %** |
| `.1.2` | **4 (residual)** | other | **Tóner residual** | 100 % |
| `.1.3` | 3 | cyan | Tóner cian | 90 % |
| `.1.4` | 3 | magenta | Tóner magenta | 80 % |
| `.1.5` | 3 | yellow | Tóner amarillo | 90 % |

Quien hubiera cableado `...9.1.1` a `...9.1.4` como CMYK habría leído **el tóner residual
como "cian al 100 %"** y el cian real como magenta. Hay que caminar la tabla y leer
`prtMarkerSuppliesType` — ahora con una prueba viva de por qué.

`SupplyUnit = 19` (percent) y `MaxCapacity = 100`, así que acá el nivel **ya es** un
porcentaje y no hay centinelas negativos. No se puede generalizar desde un equipo: la
lógica igual tiene que calcular `level/maxCapacity` y manejar `-1/-2/-3`.

**Hallazgo que cambia el diseño del estado:** `hrPrinterDetectedErrorState` devuelve
`0x00` —**ningún bit encendido**— con la impresora reportando falta de papel. En esta
Ricoh el bitmask estándar **no sirve**. Lo que sí:

1. `hrDeviceStatus = 3` (warning) — binario pero confiable.
2. `prtConsoleDisplayBufferText` y `prtAlertDescription` — **ya vienen en español**.
3. `prtInputCurrentLevel` — la bandeja 2 en `0` hojas: es el mismo hecho, numérico y
   umbralizable, que es lo que sirve para una `ReglaAlerta`.

(`prtAlertSeverityLevel` devolvió `808`, que no es un valor válido del enum 1-4. No se usa.)

**Perfiles propietarios: CERO, confirmado contra el equipo.** El árbol de tóner de Ricoh
(`367.3.2.1.2.24.1.1.5`) devuelve `80, 90, 80, 90` — **idéntico al estándar**, sin el bug
de "50 % constante" de LibreNMS. Regla 0.5.8: propietario presente + estándar OK = **no se
hace perfil**. Xerox (`253`) y HP (`11`) ausentes, como corresponde.

**La única excepción, y solo si se factura por clic:** `367.3.2.1.2.19` trae 20 filas con
`1.0 = 509.664` (coincide con el estándar), `2.0 = 445.008`, `4.0 = 64.413`. Eso es el
**desglose blanco y negro / color** que el MIB estándar no da. Si el contrato se factura
por clic diferenciado, ahí está; si no, el estándar alcanza.

### Consecuencias directas sobre este documento

1. **§3.3 queda RESUELTA, y en el peor sentido.** El atajo "colgar la alerta de la
   estación" **no sirve para ninguna impresora**: cero tienen `estacion`. Los tres caminos
   se reducen a dos: **B (v1 sin alertas, riesgo LOW)** o **A (generalizar `Alerta`, riesgo
   CRITICAL)**. No hay opción barata.
2. **§4.4 y la FASE 8 quedan SIN OBJETO por ahora.** No se puede medir qué OID responde una
   HP, una Ricoh o una Xerox **porque no hay ninguna en el inventario**. La pregunta sobre
   perfiles propietarios no tiene sujeto.
3. **Hoy no hay nada que monitorear por SNMP salvo los Mikrotik, que ya se monitorean.**
   El único candidato con IP responde ping y no responde SNMP en seis communities. Como el
   ping llega, el paquete llega: **el SNMP está apagado en el equipo**, o usa otra
   community. Eso se arregla en la impresora, no en SAIDSOFT.
4. **La premisa del brief no está en los datos.** Las impresoras administrativas HP / Ricoh
   / Xerox probablemente existan físicamente en matriz, pero **SAIDSOFT no sabe que
   existen**. El trabajo previo no es SNMP: es cargarlas en el inventario con su IP.

Esto es exactamente el riesgo que CLAUDE.md señala como el principal del proyecto —*"se
construye más rápido de lo que se pone en uso"*—, medido: 22 activos contra 701 farmacias,
y un módulo de SNMP para impresoras que hoy no tendría ni un dispositivo que leer.

### Hallazgos colaterales de la medición (no son de SNMP, pero aparecieron)

| # | Hallazgo | Evidencia | Impacto |
|---|---|---|---|
| A | **El descubrimiento por ARP no está agendado.** `sincronizar_dispositivos_detectados` solo se invoca desde el comando manual `descubrir_dispositivos_farmacia`; no está en `CELERY_BEAT_SCHEDULE` | Los 29 `dispositivo_detectado` tienen `visto_por_ultima_vez` = **15-sep-2026**, tres semanas de antigüedad | El inventario verificado está congelado. La pregunta "qué hay enchufado que nadie inventarió" se contesta con datos viejos |
| B | **Un `Activo` tipo IMP con `codigo` VACÍO** (id 20, Epson, la única con IP, creada 16-sep-2026) | consulta directa | `Activo.codigo` es `unique=True, editable=False` y autogenerado. Un vacío indica una fila creada por un camino que no pasa por esa generación. Vale revisar cómo entró |
| C | **Tres contenedores tienen prefijo de hash**: `1abca43cf424_deploy-db-1`, `af6685108031_deploy-redis-1`, `e4f12b574b67_deploy-meshcentral-1` | `docker ps` | **Cualquier runbook que diga `docker exec deploy-db-1` falla.** Hay que resolver el nombre con `docker ps \| grep "deploy-db-1$"`. CLAUDE.md documenta los nombres sin prefijo |
| D | Los 29 dispositivos vistos por ARP están **todos** sin inventariar | consulta #5 | Confirma que el cruce `Activo` × `DispositivoDetectado` no se está usando |

### Instrumentos de medición (validados contra producción)

De **solo lectura**, fuera del repo, desechables:

1. **`inventario-impresoras.sql`** — 6 consultas: resumen que decide §3.3, desglose por
   fabricante, inventario detallado, **red de seguridad para impresoras mal tipificadas**
   (encontró 2 HP, pero son un desktop y una laptop, no impresoras), y dispositivos vistos
   por ARP sin inventariar.
2. **`probe_snmp_impresora.py`** — probe pysnmp 7: 11 escalares, 9 tablas, y **presencia**
   (no interpretación) de los árboles propietarios de Xerox, Ricoh y HP. Corre **dentro del
   contenedor**, de donde sondearía el código real. Validado contra un destino inalcanzable
   y contra tres Mikrotik reales.
3. **`sweep.py`** — barrido de `sysDescr` + `sysObjectID` sobre una lista de IP, para
   contestar "de todo esto, qué habla SNMP y qué dice ser".

---

## 0. El hallazgo principal, antes de todo lo demás

La pregunta era *"¿cómo evito que SNMP quede amarrado a impresoras?"*. Esa pregunta está
bien hecha y es fácil de resolver. Pero no es el problema real del código.

**El problema real es que el núcleo de monitoreo de SAIDSOFT está amarrado a `Estacion`**
—un equipo Windows con agente— y cada fuente sin agente que se agregó tuvo que escaparse
de ese núcleo por su cuenta. Hoy hay **cinco sujetos de monitoreo distintos** conviviendo:

| Modelo | Sujeto | Fuente |
|---|---|---|
| `EstadoDispositivo`, `MuestraMetrica`, `EventoMonitoreo`, `Alerta` | `Estacion` | agente MQTT, MeshCentral |
| `EstadoEnlaceFarmacia` | `Farmacia` | ICMP |
| `MuestraRedFarmacia` | `Farmacia` | SNMP (Mikrotik) |
| `EquipoBordeFarmacia` | `Farmacia` | SNMP (Mikrotik) |
| `DispositivoDetectado` | `Farmacia` | SNMP (tabla ARP) |
| `EstadoRedActivo` | `Activo` | ICMP vía agente |

Y está documentado en el propio código que esto ya fue un choque consciente.
`apps/monitoreo/adapters/base.py` define el puerto `FuenteMonitoreo`, y
`apps/monitoreo/mikrotik.py` **se niega explícitamente a implementarlo**:

> *"No implementa `apps.monitoreo.adapters.base.FuenteMonitoreo` a propósito: ese puerto
> es para avisar estado online/offline de un DISPOSITIVO (EstadoDispositivo, estación-
> scoped) vía `registrar_estado_dispositivo` — esto es una serie de tiempo numérica por
> SITIO (farmacia), forma distinta, forzarlo sería una abstracción que no encaja."*

Esa decisión fue correcta en su momento. Pero significa que **la "capa genérica de
monitoreo" que parece existir no es genérica**: `registrar_estado_dispositivo` se
documenta como *"puerto de entrada único para cualquier fuente de monitoreo"* y su firma
es `(estacion, *, fuente, en_linea, detalle)` — primer parámetro, una estación.

**Consecuencia para este proyecto:** si agregás SNMP de impresoras como un sexto sujeto,
funciona. Cuando agregues switches, UPS y PDU vas a tener ocho. El código no "explota":
se vuelve un conjunto de tuberías paralelas que hacen lo mismo con nombres distintos, y
cada pantalla nueva tiene que saber de las ocho.

**La cosa genérica que hay que introducir no es "un motor SNMP". Es la identidad del
sujeto monitoreado.** Y ya existe: se llama `Activo`.

---

## 1. Estado actual — mapa de arquitectura real

Leído del código, no inferido.

### 1.1 Apps Django (16)

```
apps/
├── activos         ITAM: Activo, Categoria, Bodega, StockBodega, OrdenCompra, consumibles
├── aperturas       aperturas de local
├── auditoria       bitácora
├── catalogo        Estacion, Farmacia, UnidadNegocio, Grupo, PerifericoDetectado, crypto.py
├── cuentas         usuarios, MFA, scoping multi-cliente
├── cumplimiento
├── despliegues     paquetes al agente
├── facturacion     ActividadMensualEstacion
├── mantenimiento   órdenes de trabajo, ubicaciones de técnico
├── monitoreo       ← NÚCLEO DE MONITOREO
├── mqtt_worker     ingesta push desde el agente
├── panel           vistas HTMX (25 módulos de vista)
├── scripts         ejecución remota
├── software        inventario de software
└── viaticos
```

### 1.2 `apps/monitoreo` por dentro

```
monitoreo/
├── models.py          MuestraMetrica, MuestraRedFarmacia, Metrica(TextChoices),
│                      EstadoDispositivo, EventoMonitoreo, ReglaAlerta, Alerta,
│                      PosErrorDetectado, VentanaMantenimiento, CanalNotificacion,
│                      EquipoBordeFarmacia, DispositivoDetectado, EstadoRedActivo, …
├── services.py        motor de alertas, notificación, purgas, resumen (1.568+ líneas)
├── mikrotik.py        ÚNICO cliente SNMP existente (pysnmp 7, asyncio)
├── enlaces.py         sondeo ICMP + clasificación de caídas por proveedor
├── adapters/
│   ├── base.py        puerto FuenteMonitoreo (pull)
│   └── meshcentral.py única implementación
├── umbrales.py        umbrales de salud de la plataforma
├── tasks.py           tareas Celery
├── telegram_bot.py    bot
├── diagnostico_ia.py  hipótesis con LLM para alertas CRÍTICAS
├── graficos.py        series para el panel
├── servicios_pos.py   chequeo de dependencias del POS
├── api_urls.py / api_views.py / serializers.py
└── management/commands/  13 comandos, incluido probar_snmp_farmacia.py
```

### 1.3 Infraestructura desplegada (`deploy/docker-compose.yml`)

| Servicio | Imagen / comando | Rol |
|---|---|---|
| `db` | `timescale/timescaledb:2.17.2-pg16` | PostgreSQL + hypertables |
| `redis` | `redis:7-alpine` | broker Celery + beat |
| `emqx` | `emqx/emqx:5.8.3` | MQTT de los agentes |
| `web` | gunicorn, 3 workers | panel + API |
| `worker` | `manage.py run_mqtt_worker` | ingesta MQTT |
| `celery_worker` | `celery worker --concurrency=2` | **capacidad actual de polling** |
| `celery_beat` | `celery beat` | scheduler |
| `meshcentral_worker` | WebSocket de MeshCentral | presencia |
| `telegram_bot` | long polling | notificación + comandos |
| `nginx` | 1.27-alpine | TLS, estáticos |
| `meshcentral` | upstream | acceso remoto |

**Ya existe todo lo que FASE 9 pide** (scheduler, cola, workers, Redis, async). No hay que
incorporar ninguna tecnología nueva.

### 1.4 Rutas de ingesta: tres, y asimétricas

```
PUSH  agente ──MQTT──> emqx ──> worker ──> mqtt_worker.services ──┐
PUSH  MeshCentral ──WS──> meshcentral_worker ────────────────────┤
                                                                 ├──> registrar_estado_dispositivo(estacion, …)
PULL  celery_beat ──> celery_worker ──> enlaces.py (ICMP) ────────┘  (NO pasa por ahí)
                                   └──> mikrotik.py (SNMP)  ──────┘  (NO pasa por ahí)
```

Las dos rutas PUSH convergen en un puerto único. Las dos rutas PULL **no**: escriben
directo en sus propias tablas. Eso es el hallazgo §0 visto desde el flujo.

### 1.5 Persistencia de series: 4 hypertables, retención 30 días

De `apps/monitoreo/migrations/0035_hypertables_de_verdad.py` y `0040_compresion_hypertables.py`:

| Tabla | chunk | `segmentby` de compresión | Retención |
|---|---|---|---|
| `muestra_metrica` | 1 día | `estacion_id` | 30 días |
| `muestra_servicio_pos` | 1 día | `estacion_id, servicio` | 30 días |
| `evento_monitoreo` | 7 días | `estacion_id, fuente` | 30 días |
| `muestra_red_farmacia` | 7 días | `farmacia_id` | 30 días |

Política nativa de TimescaleDB **más** purga en Python como respaldo
(`_purgar_serie`, suelta chunks donde hay hypertable). Dos mecanismos para el mismo
número, deliberadamente.

---

## 2. Equivalencias: qué de lo que pedís ya existe

FASE 2 y FASE 6 piden no duplicar. Esta es la tabla de correspondencia real.

| Concepto pedido | Existe en SAIDSOFT como | Veredicto |
|---|---|---|
| `Agent` | agente Python + `apps/mqtt_worker` | reusar |
| `MQTT` / `MQTT Worker` | EMQX + `run_mqtt_worker` | no aplica a SNMP |
| `Heartbeat` | `Estacion.ultimo_heartbeat`, `marcar_estaciones_offline` | solo agente |
| `Ping` / `ICMP` | `enlaces.py` (sitio) + `EstadoRedActivo` (activo) | **reusar: ya pingea impresoras** |
| `Device` / `Host` / `Asset` | **`Activo`** (`tipo`, `ip`, `mac`, `slot`, `farmacia`, `estacion`) | **reusar — no crear `device`** |
| `Inventory` | `apps/activos` + `DispositivoDetectado` (ARP) | reusar |
| `Monitoring` | `apps/monitoreo` | reusar, con la salvedad §0 |
| `Metrics` | `MuestraMetrica` (estación), `MuestraRedFarmacia` (sitio) | **ninguna sirve: falta el sujeto `Activo`** |
| `Alerts` | `ReglaAlerta` + `Alerta` + dedup + escalamiento | **reusar el motor; la FK bloquea** |
| `Events` | `EventoMonitoreo` (estación), `EventoActivo` (activo) | reusar `EventoActivo` |
| `Tasks` / `Scheduler` | Celery + Beat + Redis | reusar |
| `Notification` | `CanalNotificacion` (Telegram, Teams, correo) | reusar sin cambios |
| `Credential` | `apps/catalogo/crypto.py` (Fernet) + `ClaveRecuperacionBitlocker` + `GrupoPos.password_cifrada` | **reusar — el patrón ya está** |
| `Location` / `Site` | `Farmacia` | reusar |
| `Organization` | `UnidadNegocio` (multi-cliente ya resuelto) | reusar |
| `Monitor` (definición) | `ReglaAlerta` (umbral, operador, duración, severidad, por unidad) | reusar |

**Tablas nuevas realmente necesarias: 4.** No 10. Detalle en §6.

---

## 3. El motor de alertas ya resuelve FASE 14 y FASE 15

Esto es importante porque es el pedido más detallado del brief y **ya está construido y
probado en producción**. No hay que diseñarlo.

### 3.1 Deduplicación y transición de estado — ya existe

`apps/monitoreo/services.py`:

```
abrir_o_mantener_alerta(regla, estacion, valor)   # línea 133
├── ventana_mantenimiento_activa(estacion) → silencia  (FASE 15: cooldown por mantenimiento)
├── _alerta_activa(regla, estacion)        → si hay ABIERTA o RECONOCIDA, NO crea otra
├── Alerta.objects.create(...)             → una sola alerta activa por (regla, sujeto)
├── encolar_notificacion_alerta(alerta)    → notifica UNA vez
├── _pedir_diagnostico_ia(alerta)          → solo si CRITICAL, una vez por incidente
└── abrir_mantenimiento_desde_alerta()     → opcional por regla

resolver_condicion(regla, estacion)               # línea 182
└── marca RESUELTA + resuelta_en                  (FASE 15: recovery)

escalar_alertas_abiertas()                        # línea 974, Beat cada 10 min
└── reenvía si sigue ABIERTA sin reconocer (UMBRAL_ESCALAMIENTO_MINUTOS = 30)
```

El escenario que FASE 15 quiere evitar (alerta a las 10:00, 10:05, 10:10…) **no puede
ocurrir**: `_alerta_activa` filtra por `estado__in=[ABIERTA, RECONOCIDA]` y
`abrir_o_mantener_alerta` retorna `None` si existe. Los estados son
`ABIERTA → RECONOCIDA → RESUELTA`.

El docstring de `_pedir_diagnostico_ia` lo dice explícitamente: *"Se dispara UNA VEZ por
incidente, y eso lo garantiza `abrir_o_mantener_alerta`… Es control de costo, no solo de
ruido."*

### 3.2 Umbrales configurables — ya existen

`ReglaAlerta` tiene `metrica`, `operador` (GTE/LTE), `umbral` (float),
`duracion_minutos`, `severidad` (WARNING/CRITICAL), `unidad_negocio` (global o por
cliente), `activo`, `abre_mantenimiento`. Más `_condicion_sostenida`, que exige que
**todas** las muestras no nulas de la ventana incumplan **y** que haya historial previo
suficiente — o sea, ya resuelve el falso positivo por pico aislado y por equipo recién
enrolado.

Lo que FASE 14 prohíbe (`if toner < 20` dentro del código de HP) está estructuralmente
impedido si se usa este motor: el umbral es una fila, no una constante.

### 3.3 El bloqueo: `Alerta.estacion`

```python
# apps/monitoreo/models.py:357
estacion = models.ForeignKey(Estacion, on_delete=models.CASCADE, related_name='alertas')
```

**FK obligatoria.** Una impresora administrativa sin estación asociada no puede tener una
`Alerta`. Además `ventana_mantenimiento_activa` navega `estacion.farmacia.unidad_negocio_id`
para resolver el silenciamiento y el scoping multi-cliente, así que no es solo la columna.

Tres caminos, con su riesgo real (FASE 21):

| Opción | Qué implica | Riesgo |
|---|---|---|
| **A. Generalizar `Alerta`** | `estacion` nullable + FK `activo` + resolver unidad de negocio por los dos caminos. Toca `abrir_o_mantener_alerta`, `_alerta_activa`, `resolver_condicion`, `notificar_alerta`, `escalar_alertas_abiertas`, `ventana_mantenimiento_activa`, `abrir_mantenimiento_desde_alerta`, el panel, el bot de Telegram y buena parte de las ~2.119 pruebas | **CRITICAL** |
| **B. v1 sin alertas** | Solo visibilidad, como hizo `mikrotik.py` (*"Solo visibilidad en v1: no crea Alerta ni notifica"*). Cero cambios de esquema en tablas existentes | **LOW** |
| **C. Motor de alertas propio para SNMP** | Duplica dedup, escalamiento, notificación, ventanas de mantenimiento | **prohibido por el brief** — es la trampa |

**Recomendación: B para v1, A como proyecto propio con su propia revisión.** C es
exactamente la deuda que esta auditoría tiene que prevenir.

**El atajo que parecía existir NO existe — medido el 7-oct-2026 (§0.1).** La idea era:
si la impresora tiene `Activo.estacion`, la alerta se cuelga de esa estación sin tocar el
esquema. **Cero de las 3 impresoras en producción tienen `estacion`.** Así que la opción A
no se puede esquivar: o se generaliza `Alerta` (CRITICAL), o v1 va sin alertas (B).

---

## 4. Lo que ya existe de SNMP, y lo que trae de malo

### 4.1 `apps/monitoreo/mikrotik.py` — el único cliente SNMP

Lo bueno, y es mucho; esto es conocimiento ganado contra equipos reales y **hay que
reusarlo, no reescribirlo**:

- **`_motor_snmp()`** — contextmanager que cierra el `SnmpEngine` por cualquier camino de
  salida. Existe porque pysnmp 7 **filtraba un descriptor de archivo por consulta**:
  ~8.400 descriptores por hora. La causa fue difícil de encontrar justamente porque se
  creía que esa tarea "no lograba nada". **Cualquier código SNMP nuevo que abra su propio
  engine va a reintroducir esa fuga.**
- **`_texto_snmp()`** — pysnmp devuelve `"No Such Object currently exists at this OID"`
  como *valor*, no como error. Sin este filtro, esa frase se guarda como número de serie.
- **Concurrencia acotada** — `asyncio.run()` dentro del task Celery sincrónico, con
  `Semaphore(_MAX_SONDEOS_CONCURRENTES = 25)`. No `ThreadPoolExecutor`.
- **`timeout=3, retries=0`** por consulta.
- **`_calcular_tasa`** — devuelve `None` si el contador bajó (reinicio del equipo) en vez
  de calcular una tasa negativa. **Aplica igual al contador de páginas de una impresora.**
- **Resolución dinámica** — el índice de la interfaz WAN se resuelve por SNMP contra la
  ruta por defecto, porque el nombre no era uniforme. Dos supuestos de diseño resultaron
  falsos contra un router real y se resolvieron solos en vez de pedir un dato uniforme
  que no lo era. **Esa lección aplica directo a la tabla de suministros de una impresora.**

Lo malo, que hay que **documentar y no arrastrar**:

| Problema | Ubicación | Impacto | Recomendación |
|---|---|---|---|
| OIDs como constantes de módulo, estándar y propietarias mezcladas en el mismo `get_cmd` | `mikrotik.py:480-487` (`_OID_SYS_DESCR` junto a `_OID_MTXR_VERSION`, empresa 14988) | Es "lógica de fabricante mezclada con lógica genérica" en pequeño. Funciona porque pysnmp devuelve "No Such Object" por OID, no por consulta | No tocar. En el código nuevo, separar catálogo estándar de catálogo por fabricante desde el día uno |
| Credencial derivada en código | `_comunidad_para()` — community = código de farmacia en minúscula | La regla de credenciales es código, no dato. No generaliza a impresoras (vienen en `public`) | El modelo nuevo guarda credenciales como dato. **No migrar el camino Mikrotik ahora** |
| Caché mutable a nivel de módulo | `_cache_indice_interfaz: dict` | Con varios workers Celery queda por proceso e inconsistente | Documentado. El código nuevo no debe tener estado de módulo |
| La community se imprime | `probar_snmp_farmacia.py:54` y en la sugerencia de `snmpwalk` | Es stdout de un comando de diagnóstico, no un log — pero alguien va a pegar esa salida en un ticket | Enmascarar. Riesgo LOW, arreglo de 2 líneas |
| Dos escritores para `muestra_red_farmacia` | sondeo directo + vía agente | Ya mitigado con ventana de frescura (`MINUTOS_FRESCURA_RED_FARMACIA`), tras medir 21 filas/hora donde debían ser 10 | **Precedente a NO repetir**: un solo escritor por serie |

### 4.2 MIB y OID: qué hay (FASE 5)

- **No hay archivos MIB en el repo. No hay compilador de MIB. No hay tabla de OIDs.**
- pysnmp se usa con **OIDs numéricos crudos** vía `ObjectIdentity('1.3.6.1.2.1...')`.

**Esto es correcto y hay que mantenerlo.** Compilar MIBs en runtime agrega un directorio
de MIBs al contenedor, latencia de arranque y una clase entera de fallas nuevas, para
resolver un problema que no tenés: los OIDs de Printer-MIB son fijos y conocidos.

**Una tabla `oid` en PostgreSQL es sobreingeniería** hasta que alguien no-técnico necesite
agregar OIDs desde el panel. Hoy el catálogo correcto es un módulo Python de constantes
declarativas.

### 4.3 Catálogo de métricas de impresora (estándar, verificado)

Nada de esto está inventado: `Printer-MIB` es RFC 3805, `HOST-RESOURCES-MIB` es RFC 2790.

| Métrica | MIB | Objeto | OID | Tipo | Unidad | Interpretación |
|---|---|---|---|---|---|---|
| Nivel de suministro | Printer-MIB | `prtMarkerSuppliesLevel` | `1.3.6.1.2.1.43.11.1.1.9.1.x` | Integer32 | según `SupplyUnit` | **No es %.** `-1`=other, `-2`=unknown, `-3`=partial (queda algo, cantidad indeterminada) |
| Capacidad máxima | Printer-MIB | `prtMarkerSuppliesMaxCapacity` | `...43.11.1.1.8.1.x` | Integer32 | ídem | Denominador. Mismos centinelas negativos |
| Unidad del suministro | Printer-MIB | `prtMarkerSuppliesSupplyUnit` | `...43.11.1.1.7.1.x` | enum | — | `19`=percent, `7`=impressions, `8`=sheets, `13`=tenthsOfGrams |
| Tipo de suministro | Printer-MIB | `prtMarkerSuppliesType` | `...43.11.1.1.5.1.x` | enum | — | `3`=toner, `4`=wasteToner, `5`=ink, `9`=developer, `6`=inkCartridge |
| Clase | Printer-MIB | `prtMarkerSuppliesClass` | `...43.11.1.1.4.1.x` | enum | — | `3`=se consume, `4`=se llena (residual) |
| Descripción | Printer-MIB | `prtMarkerSuppliesDescription` | `...43.11.1.1.6.1.x` | OctetString | texto | ej. "Black Toner Cartridge" |
| Color | Printer-MIB | `prtMarkerColorantValue` | `1.3.6.1.2.1.43.12.1.1.4.1.x` | OctetString | texto | black / cyan / magenta / yellow |
| **Contador de páginas** | Printer-MIB | `prtMarkerLifeCount` | `1.3.6.1.2.1.43.10.2.1.4.1.1` | Counter32 | ver unidad | Monótono. **Se reinicia al cambiar la placa** |
| Unidad del contador | Printer-MIB | `prtMarkerCounterUnit` | `1.3.6.1.2.1.43.10.2.1.3.1.1` | enum | — | `7`=impresiones, `8`=hojas. **Dúplex: 1 hoja = 2 impresiones** |
| Bandeja: nivel | Printer-MIB | `prtInputCurrentLevel` | `1.3.6.1.2.1.43.8.2.1.10.1.x` | Integer32 | hojas | Mismos centinelas |
| Estado de impresora | HOST-RESOURCES | `hrPrinterStatus` | `1.3.6.1.2.1.25.3.5.1.1.x` | enum | — | `1`=other `2`=unknown `3`=idle `4`=printing `5`=warmup |
| Errores detectados | HOST-RESOURCES | `hrPrinterDetectedErrorState` | `1.3.6.1.2.1.25.3.5.1.2.x` | BITS | bitmask | sin papel, atasco, tapa abierta, sin tóner, offline, servicio |
| Estado del dispositivo | HOST-RESOURCES | `hrDeviceStatus` | `1.3.6.1.2.1.25.3.2.1.5.x` | enum | — | `2`=running `3`=warning `4`=testing `5`=down |
| Número de serie | Printer-MIB | `prtGeneralSerialNumber` | `1.3.6.1.2.1.43.5.1.1.17.1` | OctetString | texto | **Llena `Activo.numero_serie` solo** |
| Texto del display | Printer-MIB | `prtConsoleDisplayBufferText` | `1.3.6.1.2.1.43.16.5.1.2.1.1` | OctetString | texto | Lo que muestra la pantallita |
| Descripción / modelo | SNMPv2-MIB | `sysDescr` | `1.3.6.1.2.1.1.1.0` | OctetString | texto | Ya usado en `mikrotik.py` |
| Nombre | SNMPv2-MIB | `sysName` | `1.3.6.1.2.1.1.5.0` | OctetString | texto | Ya usado |
| Uptime | SNMPv2-MIB | `sysUpTime` | `1.3.6.1.2.1.1.3.0` | TimeTicks | centésimas | Ya usado, con `_CENTESIMAS_POR_SEGUNDO` |
| Interfaces | IF-MIB | `ifDescr`, `ifHCInOctets`, … | `1.3.6.1.2.1.2.2.1.2`, `31.1.1.1.6` | — | — | Ya usado. **No aplica a impresoras**; sí a switches |

**No disponible por MIB estándar** — hay que decirlo claro:

- **Versión de firmware.** No existe en Printer-MIB. A veces viaja dentro de `sysDescr`,
  a veces solo en el árbol propietario. **Requiere perfil por fabricante.**
- **Medidores de facturación separados (negro vs color, A4 vs A3).** `prtMarkerLifeCount`
  da un total. Xerox los expone en `1.3.6.1.4.1.253.8.53.13.2.1.6.1.20.x` y Ricoh en
  `1.3.6.1.4.1.367.3.2.1.2.19.*`. **Requiere perfil por fabricante si el contrato se
  factura por clic.**
- **Vida restante de tambor/fusor en %.** Aparecen como filas de `prtMarkerSuppliesTable`
  solo si el equipo las publica. No se puede asumir.
- **Nivel de tinta en inyección de tanque** (la Epson L3250 de las farmacias). Soporte
  parcial o inexistente. **Hay que medirlo antes de prometerlo.**

### 4.4 ¿Hacen falta perfiles HP / Ricoh / Xerox? (FASE 5)

**Para v1: cero perfiles propietarios.**

| Marca | Printer-MIB estándar | Perfil propietario necesario |
|---|---|---|
| **HP** | Bueno. HP declara cumplimiento RFC 1759/3805 para "percent life remaining" | **No.** Salvedad: el % de HP es una *estimación*, no una medición — su propio white paper lo dice, y la interfaz web del equipo puede discrepar del SNMP. Usarlo para disparar un umbral, no para mostrar un medidor con decimales |
| **Xerox** | Bueno | **Solo si facturás por clic.** El árbol `253.8.53.13.2.1.6.1.20.x` son los medidores de facturación, y el índice varía por modelo |
| **Ricoh** | Aceptable | **Solo como respaldo.** Su OID propietario `367.3.2.1.2.24.1.1.5.{1..4}` es grueso (estados, no porcentajes) y hay un bug conocido en LibreNMS de Ricoh devolviendo 50% constante. Preferir el estándar |

**Medido el 7-oct-2026: esta tabla no tiene sujeto.** No hay ninguna HP, Ricoh ni Xerox en
el inventario de producción (§0.1), así que no hay contra qué comprobar si el estándar
alcanza. La tabla queda como referencia para cuando esos equipos se carguen; la decisión
sobre perfiles propietarios se toma recién entonces.

---

## 5. Arquitectura propuesta

### 5.1 Lo que NO conviene construir

El diagrama de FASE 4 (`SNMP Engine → v1/v2c/v3 → Device Adapter → Printer/Network/UPS →
HP/Ricoh/Xerox`) describe un producto, no este proyecto. Al tamaño actual:

- **pysnmp ya abstrae v1/v2c/v3** detrás de `CommunityData` y `UsmUserData`. Envolverlo en
  una jerarquía propia duplica la librería y no agrega nada.
- Una jerarquía `Printer → HP/Ricoh/Xerox` con **cero diferencias de comportamiento en v1**
  son tres clases vacías. Es el `PrinterMonitor`/`HPMonitor` que el brief prohíbe, con otro
  nombre.
- `snmp-service`, `printer-service`, `metrics-service`: **no**. No hay escalabilidad
  independiente que justificarlos, ni aislamiento de seguridad, ni despliegue separado.
  Un subpaquete dentro de `apps/monitoreo` es el límite correcto.

### 5.2 Lo que sí

Dos piezas genéricas y un catálogo declarativo:

```
            Activo  (ITAM — el sujeto monitoreado, YA EXISTE)
               │
   ┌───────────┼───────────┬──────────────┐
   │           │           │              │
 agente      ICMP        SNMP         (futuro: API ESET)
(Estacion) (EstadoRed-  (ObjetivoSnmp)
            Activo)         │
                            │  leer(objetivo, [oids]) ──> [LecturaSnmp]
                            │          (función, no jerarquía)
                            ▼
                    CATÁLOGO DECLARATIVO
         ┌──────────────┬──────────────┬──────────────┐
      IMPRESORA       SWITCH          UPS          GENERICO
      (Printer-MIB)  (IF-MIB)     (UPS-MIB)      (SNMPv2-MIB)
                            │
                            ▼
               normalizar() ──> LecturaSnmpActual (último valor)
                            └─> MuestraSnmp       (serie, 30 días)
                            └─> ContadorMensualSnmp (inmutable, indefinido)
```

La clave: **agregar un tipo de dispositivo nuevo (switch, UPS, PDU) debe ser una entrada
nueva en el catálogo, no un archivo nuevo de código.** Ese es el criterio de éxito del
diseño, y es verificable: la FASE 9 del plan (§10) consiste justamente en agregar switches
y medir si hizo falta escribir código.

```python
# Forma del catálogo — ilustrativa, no implementación
METRICAS_IMPRESORA = (
    MetricaSnmp(clave='toner.{color}.nivel', oid=OID_SUPPLIES_LEVEL,
                tabla=True, unidad=Unidad.PORCENTAJE, cadencia=Cadencia.LENTA),
    MetricaSnmp(clave='paginas.total', oid=OID_MARKER_LIFE_COUNT,
                unidad=Unidad.CONTADOR, cadencia=Cadencia.LENTA, monotono=True),
    MetricaSnmp(clave='estado', oid=OID_HR_PRINTER_STATUS,
                unidad=Unidad.ENUM, cadencia=Cadencia.RAPIDA),
)
```

### 5.3 `monitoring_method`: no agregarlo (FASE 7)

El brief propone `monitoring_method ∈ {AGENT, ICMP, SNMP, AGENT_AND_SNMP, …}` y pide
evaluar si ya hay un mecanismo equivalente. **Lo hay, y el enum sería peor.**

El método ya es **derivable de la existencia de filas**:

| Método | Cómo se sabe hoy |
|---|---|
| AGENT | `Activo.estacion` no nulo |
| ICMP | existe `EstadoRedActivo` para ese activo |
| SNMP | existiría `ObjetivoSnmp` para ese activo |

Un enum denormalizado sería una **sexta fuente de verdad** que se desincroniza en la
primera baja de equipo: alguien borra el objetivo SNMP y el enum sigue diciendo
`AGENT_AND_SNMP`. Es exactamente el tipo de duplicación que el brief lista como deuda.
Y el combinatorio crece mal: con UPS y firewall vas a necesitar `ICMP_AND_SNMP_AND_API`.

**Recomendación: propiedad derivada en Python (`Activo.metodos_monitoreo`), no columna.**
Si alguna vez hace falta filtrar por método en SQL a gran escala, se resuelve con un
índice o una vista materializada — no cambiando el modelo de datos ahora.

---

## 6. Modelo de datos propuesto

Cuatro tablas nuevas. Cero tablas nuevas de alertas, eventos, dispositivos o
notificaciones — todas ya existen.

### 6.1 `PerfilSnmp` — credenciales y transporte

| Campo | Tipo | Nota |
|---|---|---|
| `nombre` | CharField unique | "Impresoras oficina", "Mikrotik farmacia" |
| `version` | CharField choices | `2c` v1, `3` después |
| `comunidad_cifrada` | TextField | **Token Fernet**, igual que `GrupoPos.password_cifrada` |
| `puerto` | PositiveSmallIntegerField | default 161 |
| `timeout_segundos` | PositiveSmallIntegerField | default 3 |
| `reintentos` | PositiveSmallIntegerField | default 1 |
| `unidad_negocio` | FK nullable | vacío = global, mismo criterio que `ReglaAlerta` |

**Propósito:** que la credencial sea dato y no código (hoy `_comunidad_para` la deriva en
Python). **Volumen:** unidades, no miles. **Retención:** permanente. **Índices:** el unique
de `nombre` alcanza.

Campos v3 (`usuario`, `nivel_seguridad`, `auth_protocolo`, `auth_clave_cifrada`,
`priv_protocolo`, `priv_clave_cifrada`) se agregan **cuando se implemente v3**, no antes:
columnas nullable que nadie llena son deuda.

### 6.2 `ObjetivoSnmp` — qué sondear, con qué perfil, y cómo viene saliendo

| Campo | Tipo | Nota |
|---|---|---|
| `activo` | **OneToOne a `activos.Activo`** | el sujeto. `CASCADE` |
| `perfil` | FK `PerfilSnmp` | `PROTECT` |
| `ip_sondeada` | GenericIPAddressField | se guarda aparte de `Activo.ip`: la del activo puede cambiar y entonces el estado habla de otro destino — **mismo criterio que `EstadoRedActivo.ip_sondeada`** |
| `catalogo` | CharField choices | `IMPRESORA`, `RED`, `UPS`, `GENERICO` |
| `habilitado` | BooleanField | |
| `ultima_lectura` | DateTimeField null | |
| `ultimo_exito` | DateTimeField null | distingue "nunca respondió" de "se cayó recién", igual que `EstadoRedActivo.ultima_respuesta` |
| `fallas_consecutivas` | PositiveSmallIntegerField | **para el backoff**, §8 |
| `ultimo_error` | CharField | código clasificado, **nunca la credencial** |

**Propósito:** estado del polling, separado del estado del dispositivo. **Por qué separado
de `EstadoRedActivo`:** los modos de falla son distintos y la combinación es información.
"Responde ICMP pero SNMP da timeout" = SNMP apagado o firewall. "No responde ninguno" =
equipo apagado. Fundirlos pierde justamente el diagnóstico.
**Volumen:** 1 fila por dispositivo SNMP (cientos → miles). **Índices:** OneToOne +
`(habilitado, ultima_lectura)` para que el scheduler elija a quién le toca.

### 6.3 `LecturaSnmpActual` — último valor por métrica

| Campo | Tipo |
|---|---|
| `objetivo` | FK `ObjetivoSnmp`, CASCADE |
| `clave` | CharField — `toner.black.nivel`, `paginas.total`, `estado` |
| `valor` | FloatField null |
| `valor_crudo` | IntegerField null — **el centinela `-2`/`-3` sin interpretar** |
| `unidad` | CharField choices |
| `texto` | CharField — descripción del suministro, texto del display |
| `actualizado_en` | DateTimeField |

`UniqueConstraint(objetivo, clave)`. Se **actualiza en el lugar**, no crece.

**Por qué existe además de la serie:** el panel pregunta "¿cómo está esta impresora ahora?"
y resolverlo con un `ORDER BY timestamp DESC LIMIT 1` por métrica sobre un hypertable es
caro y N+1. **Es el mismo patrón que el código ya usa dos veces**: `EstadoDispositivo`
(snapshot) junto a `EventoMonitoreo` (histórico), y `EquipoBordeFarmacia` (casi nunca
cambia) junto a `MuestraRedFarmacia` (serie).

**Guardar `valor` y `valor_crudo` por separado no es redundancia**: es la diferencia entre
"la impresora no sabe cuánto tóner queda" y "está vacía". Con una sola columna, `-2` se
convierte en `-2%` o en `0%`, y las dos son mentira.

**Nota sobre tabla estrecha (clave/valor):** sé que parece EAV. No lo es en el sentido
peligroso: no reemplaza una relación, es una serie de medidas con unidad, que es
exactamente para lo que TimescaleDB está. La alternativa —una tabla ancha por tipo de
dispositivo, como `MuestraMetrica`— **no generaliza a UPS y PDU**, que es el requisito
explícito. El costo real de la tabla estrecha es la pérdida de tipado, y se paga con el
catálogo declarativo validando claves y unidades en un solo lugar.

### 6.4 `ContadorMensualSnmp` — la lectura que se factura

| Campo | Tipo |
|---|---|
| `objetivo` | FK `ObjetivoSnmp`, **PROTECT** |
| `anio`, `mes` | PositiveSmallIntegerField |
| `clave` | CharField — `paginas.total`, `paginas.color` |
| `valor` | BigIntegerField |
| `unidad` | CharField — **`impresiones` vs `hojas`, obligatorio** |
| `leido_en` | DateTimeField |

`unique_together = (objetivo, anio, mes, clave)`.

**Esta tabla es la que más fácil se construye mal, y es la única cuyo error cuesta plata.**

- **NO es hypertable y NO tiene política de retención.** Si el contador de páginas vive en
  una serie purgada a 30 días, la base de la facturación desaparece y no hay vuelta atrás.
  El patrón correcto ya está en el repo: `ActividadMensualEstacion`
  (`apps/facturacion/models.py`) existe con ese docstring exacto — *"esta tabla es la única
  que se conserva indefinidamente para poder facturar meses pasados"*.
- **`PROTECT` y no `CASCADE`:** dar de baja una impresora no debe borrar el historial con
  el que se facturó.
- **La unidad se guarda con el valor.** Dúplex: una hoja son dos impresiones. Reportar
  hojas donde el contrato factura impresiones es un error de hasta 2x.
- **El contador se reinicia** al cambiar la placa. El delta mensual negativo se marca como
  no computable; no se factura un número inventado. `_calcular_tasa` ya hace exactamente
  esto para los octetos del Mikrotik — mismo criterio, mismo código conceptual.

**Volumen:** `dispositivos × 12 × claves` por año. 100 impresoras × 2 claves = 2.400
filas/año. Despreciable, y vale conservarlo para siempre.

### 6.5 `MuestraSnmp` — la serie (FASE 6 de implementación, no v1)

Solo si hace falta graficar tendencia. Hypertable, `segmentby='objetivo_id'`, chunk 1 día,
retención 30 días **para igualar a las otras cuatro** (consistencia vale más que optimizar
este número en aislamiento).

**Dos gotchas verificados leyendo el repo:**

1. **`config/test_runner.py` desagenda los 8 jobs de TimescaleDB** (4 de retención + 4 de
   compresión) en la base de pruebas, por un deadlock real del 6-oct-2026 entre
   `compress_chunk` y una prueba insertando. Una quinta hypertable son **10 jobs**: si el
   runner queda en 8, el deadlock intermitente vuelve.
2. **Las migraciones 0002, 0006 y 0021 fallaron en silencio** durante meses intentando
   `create_hypertable` (`"cannot create a unique index without the column timestamp"` — el
   PK `id` que Django crea solo no incluye la columna de particionado) y el despliegue
   pasaba sin que nadie se enterara. La 0035 lo arregló. **Cualquier hypertable nueva tiene
   que seguir el patrón de la 0035, no el de las tres que fallaron.**

---

## 7. Credenciales (FASE 8)

### 7.1 Lo que ya existe y hay que reusar

`apps/catalogo/crypto.py`: Fernet (AES-128-CBC + HMAC) sobre `BITLOCKER_ENCRYPTION_KEY`,
**sin default** — exige clave real en `.env`. Ya se usa en dos lugares:

- `ClaveRecuperacionBitlocker.clave_cifrada` — *"Token Fernet — nunca texto plano"*
- `GrupoPos.password_cifrada` — contraseña de la BD del nodo POS

Los secretos viven en `deploy/.env` del servidor, **no en el repo** (CLAUDE.md lo dice
explícitamente, incluidas las credenciales SSH). `.env.example` lista las claves con
valores de ejemplo: `SECRET_KEY`, `MQTT_PASSWORD`, `EMQX_API_SECRET`,
`COMANDO_HMAC_SECRET`, `BITLOCKER_ENCRYPTION_KEY`, `MESHCENTRAL_API_PASSWORD`,
`TELEGRAM_BOT_TOKEN`, `ANTHROPIC_API_KEY`.

**Recomendación:** `PerfilSnmp.comunidad_cifrada` usa el mismo `crypto.py`. No introducir
Vault, Docker secrets ni un segundo mecanismo: un proyecto con un mecanismo de cifrado
usado de forma consistente es más seguro que uno con tres usados a medias.

Una decisión pendiente, menor pero real: la clave se llama `BITLOCKER_ENCRYPTION_KEY` y
va a cifrar cosas que no son BitLocker. Renombrarla a `FERNET_KEY` con compatibilidad
hacia atrás es LOW risk; dejarla así es deuda cosmética. **No bloquea nada.**

### 7.2 Community strings y logs

**Estado actual: aprobado con una observación.** Verificado por búsqueda: ningún
`logger.*` del repo emite la community. `mikrotik.py` loguea `('Mikrotik %s: …', ip)`.

La única aparición es `probar_snmp_farmacia.py:54`, que la imprime en stdout de un comando
de diagnóstico interactivo, y la repite en la sugerencia de `snmpwalk -c <community>`.
Para un operador es útil; el riesgo es que esa salida termine pegada en un ticket.

**Reglas para el código nuevo:**

1. Un helper `_enmascarar(valor)` → `"pub****"`, usado en **todo** camino de salida.
2. `PerfilSnmp.__str__` devuelve el nombre, nunca la credencial. `__repr__` tampoco.
3. La credencial nunca entra en `ObjetivoSnmp.ultimo_error` ni en ninguna excepción
   propagada: se clasifica el error a un código (§9) y se loguea el código.
4. El admin de Django **no** expone `comunidad_cifrada` como campo editable en texto.
5. `SNMPv3`: `authKey` y `privKey` jamás se loguean, ni en DEBUG.

**Recordatorio operativo, no de código:** SNMP v2c manda la community en texto plano por
la red. La community de las impresoras tiene que ser de **solo lectura**. El docstring de
`_comunidad_para` ya razona esto mismo para los Mikrotik y el razonamiento aplica igual.

---

## 8. Polling y escalabilidad (FASE 9, 10, 11)

### 8.1 Tecnología: toda existente

Celery + Redis + Beat + `asyncio` dentro del task. **Cero dependencias nuevas.**
Nada de procesos infinitos por dispositivo; el brief descarta el `while(true)` y el código
actual tampoco lo hace.

### 8.2 El límite del patrón actual — con números

`sincronizar_ancho_banda_farmacias` es **un solo task Celery** que recorre ~700 farmacias
en un `asyncio` loop con `Semaphore(25)`, cada 5 minutos. A 700 dispositivos y una
consulta por dispositivo, funciona: escribe 252 de 270 farmacias con muestra en la última
hora (medido 5-oct-2026).

El brief pide que no falle a los 1.000. **Falla justo ahí**, y conviene verlo:

Peor caso = todos los destinos muertos, cada consulta agota el timeout. Una impresora
necesita ~3 viajes (un GET de escalares + walk de suministros + walk de colorantes):

```
tiempo_ciclo ≈ dispositivos × viajes × timeout / concurrencia

   100 disp × 3 × 3s / 25 =    36 s   ✓ cabe en 5 min
   500 disp × 3 × 3s / 25 =   180 s   ✓ justo
 1.000 disp × 3 × 3s / 25 =   360 s   ✗ YA no cabe en 5 min
 5.000 disp × 3 × 3s / 25 = 1.800 s   ✗ 30 min
10.000 disp × 3 × 3s / 25 = 3.600 s   ✗ 1 hora
```

El punto de quiebre está en **~800-1.000 dispositivos con destinos muertos**, y "destinos
muertos" es el caso **normal**, no el excepcional: de noche las farmacias cierran y apagan
los equipos. El `Semaphore` es un número fijo en un módulo; subirlo mueve el cuello al
ancho de banda y a los descriptores del contenedor, no lo elimina.

### 8.3 Diseño que escala: abanico de tareas

```
celery_beat  (cada N min, por clase de cadencia)
     │  selecciona objetivos vencidos y los parte en lotes de ~50
     ▼
Redis  ──> N tareas `sondear_lote_snmp(ids)` en paralelo
     ▼
celery_worker (hoy --concurrency=2)   cada lote = su propio asyncio loop + Semaphore(25)
     ▼
normalizar  ──> LecturaSnmpActual (upsert)  +  MuestraSnmp (bulk_create)
```

Lo que cambia: la perilla de escalado pasa de "el semáforo de un task" a **"cuántos
workers Celery hay"**, que es horizontal y se ajusta en el compose sin tocar código.

Reglas que esto impone:

- **Un escritor por serie.** El precedente de los dos escritores de `muestra_red_farmacia`
  —21 filas/hora donde debían ser 10, con `_calcular_tasa` promediando ventanas de 113 a
  420 s en la misma columna— **no se repite**. Un objetivo pertenece a una sola tarea.
- **Lotes idempotentes.** Un lote reintentado no debe duplicar filas: `LecturaSnmpActual`
  es upsert por `(objetivo, clave)`; `MuestraSnmp` tolera una fila extra.
- **Sin estado de módulo.** Nada de `_cache_indice_interfaz` en el código nuevo: con varios
  workers queda por proceso.
- **`_motor_snmp()` siempre.** Un engine abierto a mano reintroduce la fuga de descriptores.
- **La guarda de barrido masivo.** `sondear_enlaces_farmacias` no registra nada si casi
  todo el barrido falla, porque *"704 farmacias no se caen a la vez; lo que se cayó es la
  ruta desde donde se está sondeando"*. Replicar: si un ciclo SNMP falla casi entero, no
  escribir "todo en 0".

### 8.4 Tres clases de cadencia (lo que de verdad acota el volumen)

Sondear todo cada 5 minutos es lo que hace explotar el almacenamiento. Atera sondea cada
2 minutos; para tóner eso es desperdicio puro.

| Clase | Intervalo | Métricas | Justificación |
|---|---|---|---|
| **RÁPIDA** | 5 min | estado, errores detectados, alcanzable | Acá el valor *es* la velocidad de detección |
| **LENTA** | 60 min | tóner, contadores, bandejas | El tóner no se mueve en 5 minutos |
| **IDENTIDAD** | diaria | serie, modelo, firmware, descripción | No cambia nunca |

Volumen resultante, 200 B/fila:

| Escenario | Filas/día | 30 días |
|---|---|---|
| 100 impresoras (3 rápidas + 6 lentas) | 100×(288×3 + 24×6) ≈ **101 k** | ~600 MB → **comprimido, decenas de MB** |
| 1.000 dispositivos mixtos | ~1 M | ~6 GB crudos |
| 10.000 dispositivos, 20 métricas, **todo a 5 min** | **57,6 M** | **~345 GB — inviable** |

La última fila es la respuesta a *"no quiero que funcione con 20 y falle con 1.000"*: el
diseño que falla no es el motor SNMP, **es sondear todo a la misma frecuencia**. Con tres
clases de cadencia, 10.000 dispositivos quedan en ~4 M filas/día, que con la compresión
que ya tenés configurada es sostenible.

### 8.5 Timeout, reintentos y backoff (FASE 11)

| Parámetro | Valor | Por qué |
|---|---|---|
| `timeout` | 3 s | Mismo que `mikrotik.py`, ya validado contra equipos reales |
| `retries` | **1** (no 0 como Mikrotik) | Las impresoras duermen y tardan en despertar; un reintento recupera muchos falsos negativos. Un router no duerme |
| Concurrencia por lote | `Semaphore(25)` | Mismo que `_MAX_SONDEOS_CONCURRENTES` |
| Umbral de "caído" | **3 fallas consecutivas** | Misma convención que `EstadoEnlaceFarmacia.UMBRAL_FALLAS_CONSECUTIVAS` |
| Backoff | saltar `2^min(fallas,5)` ciclos | Un dispositivo de baja no debe costar 3 timeouts cada 5 min para siempre |

**Las alertas duplicadas por reintento ya están impedidas** por `abrir_o_mantener_alerta`
(§3.1), sin necesidad de un cooldown nuevo: la alerta queda abierta y no se crea otra. El
reintento es una decisión de transporte, no de alertas.

---

## 9. Normalización y manejo de errores (FASE 12, 19)

### 9.1 Normalización — con una corrección al brief

El brief pide que el frontend reciba "Tóner negro 18% WARNING" y no un OID. **De acuerdo,
y el stack ya lo garantiza**: el panel es Django + HTMX **renderizado en el servidor**, no
un frontend JS consumiendo JSON. El OID no puede llegar a la plantilla si la vista no lo
pasa.

**Corolario anti-sobreingeniería: no hace falta un endpoint JSON nuevo para esto.** La app
Flutter (`movil-campo`) es para trabajo en campo; no necesita niveles de tóner. Construir
una API REST de métricas SNMP que nadie consume es un contrato público que hay que mantener
para siempre a cambio de nada. Si después hace falta, se agrega.

La normalización sí existe, pero como **estructura interna del servicio**, no como contrato
externo:

```python
@dataclass(frozen=True)
class LecturaSnmp:
    clave: str        # 'toner.black.nivel'
    valor: float|None # 18.0  — None si el equipo no sabe
    crudo: int|None   # -2    — el centinela, sin interpretar
    unidad: str       # 'porcentaje'
    texto: str        # 'Black Toner Cartridge HP 26A'
```

Para el color en pantalla **se reusa `apps/panel/umbrales.py::clasificar`**, que ya devuelve
`ok | warning | critical | sin_dato | sin_umbral` y cuyo docstring ya tomó la decisión
correcta para este caso: *"`None` es 'sin_dato' y no 'ok': un recurso que no se pudo medir
no es un recurso sano, y pintarlo verde diría que está bien algo que nadie midió."* Es
exactamente el tratamiento que necesita un `-2 unknown` de tóner.

### 9.2 Catálogo de errores y comportamiento definido

| Escenario | Cómo se detecta | Comportamiento |
|---|---|---|
| Timeout | `errorIndication` de pysnmp | `fallas_consecutivas += 1`, backoff. Sin alerta hasta 3 |
| Connection refused | excepción de socket | Igual a timeout, pero **se distingue**: el paquete llegó → hay ruta, SNMP apagado |
| SNMP deshabilitado | ICMP ok + SNMP timeout | `ultimo_error='SNMP_SIN_RESPUESTA'`. **Accionable: hay que habilitarlo en el equipo** |
| Community inválida | timeout (v2c no distingue) | `'POSIBLE_CREDENCIAL'`. El comando de diagnóstico prueba `public` para desambiguar — igual que `probar_snmp_farmacia` |
| Fallo de autenticación (v3) | `errorIndication` específico | `'AUTENTICACION'`. **Nunca loguear la clave** |
| OID inexistente | **`"No Such Object"` como valor** | `_texto_snmp()` lo filtra. La métrica queda ausente, no en 0 |
| MIB no soportada | la tabla vuelve vacía | Dispositivo marcado "sin soporte para esta métrica". **No es un error** |
| Valor inválido | centinelas `-1/-2/-3`, texto donde se esperaba número | `valor=None`, `crudo` guardado. **Nunca `0`** |
| Dispositivo apagado | ICMP + SNMP fallan | Esperado de noche. **No alerta** — mismo criterio que el docstring de `EstadoRedActivo`: *"alertar sobre eso serían cientos de falsos positivos por noche y el resultado conocido es que se terminan ignorando todas"* |
| Dispositivo reiniciando | `sysUpTime` bajo, contador que retrocede | `_calcular_tasa` devuelve `None`. Mes no computable en `ContadorMensualSnmp` |
| Red del servidor caída | **casi todo el barrido falla** | **No escribir nada.** Guarda de `sondear_enlaces_farmacias` |
| Falta `ping`/binario en la imagen | `verificar_ping_disponible()` | Ya resuelto: `PingNoDisponible` se reporta como fallo local, no como flota caída |
| Recursos del host agotados | `RecursosDelHostAgotados` | Ya existe la jerarquía `FalloLocalDeSondeo` en `enlaces.py` |

**La distinción más valiosa de este catálogo ya está construida en `enlaces.py`**: separar
"el destino está mal" de "nuestro host está mal". Costó un incidente real (la imagen sin
`iputils-ping` reportando la flota entera como caída). Reusar esa jerarquía de excepciones
en SNMP, no inventar otra.

---

## 10. Logging y observabilidad (FASE 18)

**Estado actual:** `logging` estándar de Python, estilo printf
(`logger.warning('Mikrotik %s: …', ip)`). No hay logs estructurados, ni trazas, ni métricas
de la plataforma salvo `manage.py verificar_salud` + `umbrales.py`.

**Recomendación: NO introducir `structlog` ahora.** Un segundo estilo de logging conviviendo
con el existente en 16 apps es peor que un estilo consistente aunque sea simple. El brief
pide campos (`device_id`, `device_ip`, `protocol`, `operation`, `duration`, `status`,
`error`); eso se consigue con un prefijo estable y `extra=`:

```python
logger.info('snmp.sondeo objetivo=%s ip=%s op=%s dur_ms=%d estado=%s',
            objetivo.pk, ip, 'walk_supplies', duracion_ms, 'ok')
```

Grepeable, agregable, y **sin credenciales por construcción** porque el perfil nunca se
pasa al logger. Si más adelante hace falta JSON, un `logging.Formatter` lo convierte sin
tocar los call sites.

**Lo que sí hay que agregar y hoy falta:** el panel no muestra `EstadoRedActivo` en ninguna
parte (§11). Se recoge y no se ve. Un dato que nadie mira no es observabilidad.

---

## 11. Frontend (FASE 17)

**Lo que existe:** 25 módulos de vista en `apps/panel/views/`, y plantillas relevantes:
`activo_detalle.html`, `activo_form.html`, `activos_lista.html`, `activos_avisos.html`,
`alertas_lista.html`, `reglas_alerta_lista.html`, `enlaces_farmacias_lista.html`,
`enlace_farmacia_modal.html`.

`activo_detalle.html` tiene hoy dos secciones: **"Especificaciones de equipo"** e
**"Historial completo"**.

**Hallazgo:** `estado_red` **no aparece en `activo_detalle.html`** (cero coincidencias).
`EstadoRedActivo` se recoge por sondeo del agente y no se renderiza en la página del
activo. Es una brecha preexistente, y es justamente el punto de integración.

**Recomendación: no crear ninguna pantalla nueva.**

| Necesidad | Dónde va |
|---|---|
| Lista de impresoras | `activos_lista.html` **ya existe**, filtrada por `tipo='IMP'`. No una pantalla "Impresoras" |
| Ficha del dispositivo | Nueva sección **"Monitoreo"** en `activo_detalle.html`, que muestre ICMP y SNMP juntos — y de paso tape la brecha de `EstadoRedActivo` |
| Alertas | `alertas_lista.html` sin cambios (cuando §3.3 se resuelva) |
| Umbrales | `reglas_alerta_lista.html` sin cambios |
| Consumibles | Dentro de la sección "Monitoreo", con `clasificar()` para el color |
| Tendencia | `graficos.py` ya genera series para el panel |

```
Activo · RICOH MP C3004                     (activo_detalle.html)
├── Identificación           ← ya existe
├── Especificaciones         ← ya existe (serie y modelo los llena SNMP solo)
├── Monitoreo                ← NUEVO: una sección, no una pantalla
│     ICMP   responde · 4 ms · hace 2 min
│     SNMP   ok · hace 12 min
│     Estado idle
│     Tóner  Negro 18% ⚠  Cian 72%  Magenta 64%  Amarillo 81%
│     Páginas 235.421 (impresiones)
└── Historial completo       ← ya existe
```

Riesgo a vigilar: la vista de detalle tiene que traer suministros con `prefetch_related`.
Cuatro colores × una consulta cada uno es el N+1 clásico, y en la lista filtrada por
`tipo='IMP'` se multiplica por la cantidad de impresoras.

---

## 12. Testing (FASE 20)

**La infraestructura existe y es buena** — el brief pregunta si hay que incorporarla; no
hay que incorporar nada.

- ~**2.119 pruebas** (según CLAUDE.md, 5-oct-2026).
- `config/test_runner.py`: `DiscoverRunner` propio que desagenda los jobs de TimescaleDB.
- CI sobre `timescale/timescaledb`. **Las pruebas corren contra PostgreSQL, no SQLite**:
  125 pruebas que pasaban en SQLite fallaban en PostgreSQL.
- **Patrón de mock de SNMP ya establecido:**
  `patch('apps.monitoreo.mikrotik._sondear_farmacia', new_callable=AsyncMock)` y
  `patch('apps.monitoreo.mikrotik._leer_identidad')`. Se mockea **el borde del módulo, no
  pysnmp**.

Plan de pruebas, reusando todo eso:

| Tipo | Qué | Cómo |
|---|---|---|
| Unitarias | Interpretación de `-1/-2/-3`; `level/maxCapacity`; unidad `19` vs `7` vs `8`; resolución de qué fila es qué color; bitmask de `hrPrinterDetectedErrorState`; contador que retrocede | **Sin red.** Fixtures de salida real de `snmpwalk` como dicts Python |
| Integración | `sondear_lote_snmp` escribe `LecturaSnmpActual` y `ContadorMensualSnmp`; upsert idempotente | `patch` en el borde `snmp.leer` |
| Mock SNMP | Simular timeout, "No Such Object", tabla vacía, valor de texto donde se espera número | `side_effect` |
| Timeout / retry | `fallas_consecutivas` sube; backoff saltea ciclos; se reinicia al primer éxito | Reloj congelado |
| Dedup / recovery | Reusar las pruebas que ya existen de `abrir_o_mantener_alerta` | Ya cubierto |
| Carga | 1.000 y 5.000 objetivos falsos **todos en timeout**; medir duración del ciclo | **La prueba que importa.** Valida §8.2 |
| HP / Ricoh / Xerox real | Contra equipos reales, **fuera de CI** | `manage.py probar_snmp_impresora` |

**Dos reglas no negociables, de errores ya cometidos en este repo:**

1. **Ninguna prueba puede leer un archivo ignorado por git.** `95720f2` agregó pruebas que
   leían `deploy/certs/cert.pem` —ignorado— y el CI estuvo **38 días en rojo** mientras
   pasaba en verde en local. Las fixtures de `snmpwalk` van **en el repo**.
2. **Ninguna prueba toca un dispositivo real.** Un CI que depende de que una Ricoh esté
   encendida es un CI rojo intermitente.

---

## 13. Compatibilidad: impacto por archivo (FASE 21)

Archivos **nuevos** — riesgo LOW por construcción, no pueden romper lo que existe:

```
apps/monitoreo/snmp/__init__.py
apps/monitoreo/snmp/cliente.py        leer(objetivo, oids) → [LecturaSnmp]
apps/monitoreo/snmp/catalogo.py       MetricaSnmp declarativas por tipo
apps/monitoreo/snmp/impresoras.py     interpretación Printer-MIB
apps/monitoreo/snmp/normalizar.py
apps/monitoreo/management/commands/probar_snmp_impresora.py
apps/monitoreo/migrations/00XX_snmp.py
```

Archivos **modificados**:

| Archivo | Motivo | Dependencias | Impacto | Riesgo |
|---|---|---|---|---|
| `apps/monitoreo/models.py` | 4 modelos nuevos | migración en app con hypertables | Aditivo, nada existente cambia | **LOW** |
| `config/settings/base.py` | `SNMP_CONFIG` + entradas de Beat | `celery_beat` | Aditivo | **LOW** |
| `apps/monitoreo/tasks.py` | tareas nuevas | Celery | Aditivo | **LOW** |
| `apps/monitoreo/admin.py` | alta de perfiles y objetivos | — | Aditivo | **LOW** |
| `apps/catalogo/crypto.py` | ninguno — **se importa tal cual** | — | Cero | **NINGUNO** |
| `apps/panel/views/activos.py` | sección Monitoreo + `prefetch_related` | `activo_detalle.html` | Riesgo de N+1 si se hace mal | **MEDIUM** |
| `templates/panel/activo_detalle.html` | sección nueva | — | Visual | **LOW** |
| `apps/monitoreo/mikrotik.py` | **mover `_motor_snmp` y `_texto_snmp`** a `snmp/cliente.py` e importarlos de vuelta | **Es la fuente del ancho de banda de 252/270 farmacias.** Toca la fuga de descriptores | Cambio de 2 líneas, cubierto por pruebas, **en su propio commit** | **MEDIUM-HIGH** |
| `config/test_runner.py` | 8 → 10 jobs, **solo si se agrega hypertable** | suite completa | Omitirlo = deadlock intermitente | **LOW, fácil de olvidar** |
| `apps/monitoreo/models.py::Alerta` | generalizar el sujeto (§3.3) | 9 funciones + panel + bot + ~2.119 pruebas | Rompe el motor de alertas si sale mal | **CRITICAL — diferir** |

**Alternativa a tocar `mikrotik.py`:** importar los privados desde el módulo nuevo
(`from .mikrotik import _motor_snmp, _texto_snmp`). Feo —importar privados de otro módulo—
pero **riesgo cero** sobre el camino que hoy mide el ancho de banda de toda la red. Si la
prioridad declarada es ESTABILIDAD por encima de todo, esta es la opción coherente para
v1, y la extracción se hace después con calma.

---

## 14. Deuda técnica preexistente (documentada, NO arreglada)

Según la regla del brief: documentar, no arreglar sobre la marcha.

| # | Problema | Ubicación | Impacto | Riesgo | Recomendación |
|---|---|---|---|---|---|
| 1 | **Núcleo de monitoreo atado a `Estacion`**; 6 sujetos distintos conviviendo | `services.py`, `models.py`, `adapters/base.py` | Cada fuente sin agente duplica plomería. Bloquea alertas de impresora | **ALTO y creciente** | Convergir en `Activo` para lo agentless. Generalizar `Alerta` como proyecto propio |
| 2 | `EstadoRedActivo` se recoge y **no se muestra** | `activo_detalle.html` | Dato ciego; nadie puede diagnosticar "no imprime" aunque el dato exista | MEDIO | Taparlo con la sección Monitoreo — costo marginal cero |
| 3 | OIDs estándar y propietarias mezcladas en un `get_cmd` | `mikrotik.py:480-487` | Fabricante mezclado con genérico | BAJO (funciona) | No tocar. Separar en el código nuevo |
| 4 | Credencial derivada en código | `_comunidad_para()` | La regla es código, no dato | BAJO | Que el camino nuevo use `PerfilSnmp`. No migrar Mikrotik ahora |
| 5 | Caché mutable a nivel de módulo | `_cache_indice_interfaz` | Inconsistente entre workers | BAJO hoy, MEDIO con abanico | Prohibido en el código nuevo |
| 6 | Community impresa en stdout | `probar_snmp_farmacia.py:54` | Fuga al pegar la salida en un ticket | BAJO | Enmascarar, 2 líneas |
| 7 | Dos escritores de `muestra_red_farmacia` | directo + vía agente | Ya mitigado, pero es un precedente | BAJO | **No repetir.** Un escritor por serie |
| 8 | `BITLOCKER_ENCRYPTION_KEY` va a cifrar cosas que no son BitLocker | `crypto.py` | Cosmético | MUY BAJO | Renombrar algún día |
| 9 | Clave de cifrado sin rotación documentada | `crypto.py` | Si se filtra, hay que redescifrar y recifrar todo | MEDIO | Fuera de alcance, pero anotarlo |

---

## 15. Plan de implementación

Cada fase tiene una **compuerta medible**. No se pasa a la siguiente sin cerrarla.

### FASE 0 — Medir — **HECHA el 7-oct-2026, ver §0.1**

Resultado: **3 impresoras en inventario, cero HP/Ricoh/Xerox, una sola con IP, y esa no
responde SNMP en seis communities aunque sí responde ping.** Control positivo con tres
Mikrotik, que sí respondieron.

**La compuerta NO se cerró, y por el motivo que menos se esperaba: no hay dispositivos.**
Lo que falta no es medir mejor, es que existan los datos. Antes de la FASE 1:

1. **Cargar las impresoras administrativas en el inventario** con marca, modelo e IP. Sin
   esto, todo lo que sigue no tiene sobre qué correr.
2. **Habilitar SNMP de solo lectura en esos equipos**, desde su interfaz web, y anotar la
   community. Se arregla en la impresora, no en SAIDSOFT.
3. Confirmar si algún contrato se factura por clic. **Decide si `ContadorMensualSnmp` y los
   perfiles Xerox/Ricoh entran al alcance.** Es una pregunta a administración.
4. Recién entonces: correr `probe_snmp_impresora.py` contra una de cada marca y llenar la
   tabla de §4.3 con lo que cada una entregó.

**Compuerta real:** al menos una impresora administrativa cargada, con IP, respondiendo
`prtMarkerSuppliesLevel`. Hasta ahí, cualquier modelo o migración sería código sin usuario.

### FASE 1 — `probar_snmp_impresora` (una tarde)

Comando de diagnóstico calcado de `probar_snmp_farmacia.py`: ping → SNMP → fallback a
`public` → lectura real → qué soporta y qué no. **Cero esquema, cero migración.**

**Compuerta:** corre contra las tres marcas y la salida coincide con la FASE 0. Queda como
herramienta permanente de "esta impresora no reporta, ¿por qué?".

### FASE 2 — Cliente y catálogo (funciones puras)

`snmp/cliente.py` (reusando `_motor_snmp` y `_texto_snmp`), `snmp/catalogo.py`,
`snmp/impresoras.py`, `snmp/normalizar.py`. **Sin base de datos, sin Celery.**

**Compuerta:** pruebas unitarias con fixtures reales de las tres marcas, incluyendo
`-1/-2/-3`, unidades `19`/`7`/`8`, y una tabla de suministros con tóner residual y fusor
mezclados. Esta es la fase donde se gana o se pierde la corrección.

### FASE 3 — Modelos y migración

`PerfilSnmp`, `ObjetivoSnmp`, `LecturaSnmpActual`. **Sin polling todavía.** Alta por admin.

**Compuerta:** migración aplicada en local **sobre PostgreSQL**, pruebas de constraints,
y `comunidad_cifrada` verificada como token Fernet (no texto plano) en la base.

### FASE 4 — Motor de polling (solo visibilidad)

Abanico Beat → lotes → `celery_worker`, con backoff y la guarda de barrido masivo.
**Sin alertas** (§3.3 opción B).

**Compuerta — la más importante del plan:** prueba de carga con 1.000 y 5.000 objetivos
falsos **todos en timeout**, midiendo duración de ciclo. Si un ciclo excede su intervalo,
el diseño no pasó y se corrige acá, no en producción.

### FASE 5 — Panel

Sección "Monitoreo" en `activo_detalle.html`, con ICMP y SNMP juntos. Tapa la deuda #2.

**Compuerta:** ficha de una impresora real con tóner y páginas, y **conteo de consultas SQL
estable** al crecer la cantidad de impresoras (no N+1).

### FASE 6 — Serie e histórico

`MuestraSnmp` (hypertable, patrón de la migración 0035, **+2 jobs en `test_runner.py`**) y
`ContadorMensualSnmp`. Solo si la FASE 0 mostró contadores legibles.

**Compuerta:** retención y compresión verificadas como las otras cuatro tablas; suite
completa sin deadlocks intermitentes.

### FASE 7 — Generalizar `Alerta` (proyecto propio)

Riesgo CRITICAL. Revisión de impacto propia antes de empezar. Recién acá aparecen
`TONER_LOW`, `PAPER_EMPTY`, `DOOR_OPEN` como `ReglaAlerta` configurables, reusando dedup,
escalamiento, ventanas de mantenimiento y notificación **sin escribir un motor nuevo**.

### FASE 8 — Perfiles por fabricante (solo si hacen falte)

Medidores de facturación Xerox, respaldo de tóner Ricoh. **Cero si la FASE 0 dijo que el
estándar alcanza.**

### FASE 9 — Cruce con bodega (el diferenciador)

`toner.*.nivel ≤ umbral` → `StockBodega` → sugerir `OrdenCompra`. Ninguna de las siete
herramientas comerciales del mercado conecta nivel de tóner con stock y orden de compra;
vos ya tenés las tres tablas en el mismo sistema.

### FASE 10 — Extender a switches y UPS: **la prueba del diseño**

Agregar un catálogo de switch (IF-MIB, ya hay OIDs en `mikrotik.py`) y uno de UPS.

**Compuerta:** si hizo falta escribir código nuevo más allá de las entradas del catálogo,
**el diseño de la FASE 2 falló** y hay que corregirlo antes de seguir sumando tipos.

---

## 16. Veredicto sobre sobreingeniería

| Propuesto en el brief | Veredicto | Motivo |
|---|---|---|
| `snmp-service`, `printer-service`, `metrics-service` | **No** | Sin escalabilidad independiente, sin aislamiento, sin despliegue separado que lo justifique |
| Jerarquía `SNMP Engine → v1/v2c/v3` | **No** | pysnmp ya lo abstrae. Envolverlo duplica la librería |
| `DeviceAdapter → Printer → HP/Ricoh/Xerox` | **No en v1** | Tres clases sin diferencia de comportamiento. Es `HPMonitor` con otro nombre |
| Tabla de OIDs en PostgreSQL | **No** | Hasta que alguien no-técnico cargue OIDs desde el panel |
| Compilador de MIB / archivos MIB | **No** | OIDs numéricos crudos, como ya hace `mikrotik.py` |
| API REST de métricas SNMP | **No** | Nadie la consume. Panel renderizado en servidor |
| `monitoring_method` como columna | **No** | Derivable. Sexta fuente de verdad que se desincroniza |
| `structlog` | **No ahora** | Dos estilos de logging es peor que uno simple |
| Motor de alertas para SNMP | **No** | Ya existe y es bueno. Generalizar el sujeto, no duplicar el motor |
| Celery + Redis + Beat + asyncio | **Sí** | Ya desplegado y probado |
| `crypto.py` Fernet para credenciales | **Sí** | Patrón ya usado dos veces |
| Catálogo declarativo + una función `leer()` | **Sí** | Es la abstracción correcta a este tamaño |

---

## 17. Las cinco cosas que hay que recordar

1. **El riesgo no es que SNMP quede atado a impresoras. Es que el núcleo de monitoreo está
   atado a `Estacion`.** Converger en `Activo` para todo lo agentless, y aceptar que
   generalizar `Alerta` es un proyecto propio con riesgo CRITICAL.
2. **No construir ningún motor de alertas.** `abrir_o_mantener_alerta` +
   `_alerta_activa` + `resolver_condicion` + `escalar_alertas_abiertas` ya resuelven FASE
   14 y 15 completas, en producción. Lo único que falta es que acepten un sujeto que no
   sea una estación.
3. **El patrón de polling actual se rompe alrededor de los 1.000 dispositivos** cuando los
   destinos están muertos — que es el caso normal de noche. La solución no es subir el
   semáforo: es abanico de tareas Celery y **tres clases de cadencia**. Sin las clases de
   cadencia, 10.000 dispositivos son ~345 GB de serie.
4. **`prtMarkerSuppliesLevel` no es un porcentaje** (`-1` other, `-2` unknown, `-3`
   partial), **el índice de la tabla no es fijo** (hay que caminarla y leer
   `prtMarkerSuppliesType`), y **el contador de páginas se reinicia**. Guardar crudo y
   derivado por separado, y la unidad con el valor — el dúplex hace que hoja ≠ impresión,
   y eso se factura.
5. **El contador mensual no puede vivir en una serie purgada a 30 días.**
   `ActividadMensualEstacion` ya existe por este motivo exacto. Tabla aparte, `PROTECT`,
   retención indefinida.

---

## Apéndice: fuentes externas

- RFC 3805 — Printer MIB v2: https://www.rfc-editor.org/rfc/rfc3805.html
  (valores especiales `other(-1)`, `unknown(-2)`, `partial(-3)`)
- RFC 2790 — Host Resources MIB
- HP, *LaserJet Percent Life Remaining* (white paper): el % es estimación, no medición
- Xerox, *Read the Billing Meters / Usage Counters*: https://www.support.xerox.com/en-us/article/KB0238205
- Xerox, *SNMP OIDs for interpreting printer status*: https://forum.support.xerox.com/t5/Security-Accounting-Auditron/SNMP-OIDs-for-interpreting-printer-status/td-p/205287
- LibreNMS #6801 — niveles de tóner Ricoh incorrectos (50% constante):
  https://github.com/librenms/librenms/issues/6801
- OpsRamp — plantilla de impresoras Ricoh: https://docs.opsramp.com/support/reference/gateway-template-details/ricoh-printers/

## Apéndice: fuentes internas leídas

`CLAUDE.md` · `apps/monitoreo/{models,services,mikrotik,enlaces,umbrales}.py` ·
`apps/monitoreo/adapters/{base,meshcentral}.py` ·
`apps/monitoreo/management/commands/probar_snmp_farmacia.py` ·
`apps/monitoreo/migrations/{0002,0006,0021,0035,0040}*.py` ·
`apps/activos/{models,services}.py` · `apps/catalogo/{models,crypto}.py` ·
`apps/facturacion/models.py` · `apps/panel/umbrales.py` ·
`templates/panel/activo_detalle.html` · `config/{celery,test_runner}.py` ·
`config/settings/base.py` · `deploy/docker-compose.yml` · `requirements.txt` · `.env.example`
