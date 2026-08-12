# Plan de Modernización SAIDSOFT

**Fecha:** 16 de julio de 2026 · **Última actualización:** 31 de julio de 2026
**Alcance:** ~600 farmacias · ~1.800 estaciones Windows · servidor propio (LAN/VPN)

> **Estado (31-jul-2026):** las Fases 0-5 de este documento (núcleo, panel de
> despliegues, activos, agente, integración POS, infraestructura de despliegue) están
> completas en código. Sobre esa base se construyó una segunda etapa no prevista en el
> plan original — convertir el sistema en multi-cliente (RMM tipo MSP) para las
> unidades de negocio de CRESIO — documentada en la **sección 9**. La **sección 10**
> es la auditoría de lo que falta cerrar antes de un rollout real (pruebas,
> producción validada de verdad, empaquetado del agente). Este archivo se actualiza
> junto con cada fase que se cierra — ver `CLAUDE.md` para la convención.

---

## 1. Diagnóstico del sistema actual

| Componente | Hoy | Problema |
|---|---|---|
| Panel web | Django 1.8 / Python 2.7 | Sin soporte desde 2018/2020, vulnerabilidades |
| Servicio MQTT | Node.js (mqtt v2, pg v7) | Obsoleto, lógica duplicada en 2 lenguajes |
| Base de datos | PostgreSQL (`bd_saidsof`) | Se conserva — los modelos usan `db_table` explícito |
| Credenciales | En texto plano en el código | Rotar y mover a variables de entorno (urgente) |
| Agente en equipos | No está en el repo | Se reescribe en .NET 10 |

## 2. Jerarquía y conceptos del negocio

```
Grupo TRX (canal de versión POS)     ej. TRX001, TRX004
  └── Farmacia                       ej. ML001, MAM01
        └── Estación                 ej. ML001-ADM, ML001-A, MAM01-B
```

- Cada farmacia pertenece a **un** grupo TRX. El grupo es el canal de versión del POS.
- Las estaciones son cajas iguales (no hay servidor local); `-ADM` queda previsto como rol de caché opcional a futuro.
- Parque mixto Windows 10 (incluye builds viejos) y Windows 11.

## 3. Arquitectura objetivo

```
┌─ Servidor central (LAN/VPN, Docker) ───────────────────────┐
│  Panel Django 5.2 LTS (Python 3.12) + PostgreSQL 16        │
│  + TimescaleDB (métricas)  + Worker MQTT Python (aiomqtt)  │
│  Broker EMQX con TLS y ACLs por tópico                     │
└────────────────────────────────────────────────────────────┘
                    ▲  MQTT sobre TLS (VPN)
                    ▼
┌─ Cada estación Windows ────────────────────────────────────┐
│  Agente SAIDSOFT: .NET 10 LTS, Windows Service,            │
│  self-contained (no depende del estado del SO),            │
│  MQTTnet, auto-enrolamiento, auto-actualizable             │
│      ▲ librería compartida Saidsoft.Client (NuGet interno) │
│  POS C# ┘ reporta versión/estado, recibe avisos            │
└────────────────────────────────────────────────────────────┘
```

**Decisiones clave y su porqué:**
- **MQTT se mantiene** — ideal para 1.800 clientes con conexión intermitente; mensajes retenidos para equipos apagados.
- **Agente separado del POS** — un exe no puede reemplazarse a sí mismo; el agente actualiza aunque el POS esté caído; corre como servicio con privilegios.
- **.NET 10 LTS** — soporte hasta nov-2028 (.NET 8 y 9 mueren en nov-2026). Self-contained: corre en Win10 desde build 1607 sin instalar nada.
- **Node.js desaparece** — su lógica pasa a un worker Python del mismo proyecto.
- **EMQX ≥ Mosquitto** — dashboard, ACLs y métricas de conexiones a esta escala.

## 4. Módulo A — Despliegues (objetivo principal)

### Flujo de un despliegue
1. Subir `.zip` (ejecutables POS) → el servidor calcula SHA-256, se asigna versión, ruta y comando.
2. Destino: **toda la cadena** / **grupos TRX** / farmacias específicas / tipo de equipo.
3. Modo de aplicación elegible por envío: inmediato · descargar ya y aplicar en ventana (ej. 22:00) · aplicar al cierre del POS.
4. Distribución en **olas escalonadas** (~150 equipos por ola) con límite de ancho de banda.
5. **Anillos**: piloto → 5% → resto, con freno automático si los errores superan umbral.

### Ciclo en el agente
```
descarga → verifica SHA-256 → espera ventana → cierra POS → respalda versión
→ copia archivos → relanza POS → ¿OK? → reporta OK
                                 └ no → rollback automático + reporta ERROR
```

### Versiones por canal
- Cada grupo TRX tiene versión objetivo; cada estación reporta su versión real en el heartbeat.
- El panel muestra matriz de cumplimiento y alerta desviaciones (equipo fuera de versión).

### Tópicos MQTT (contrato)
```
/saidsof/despliegue/global/            /saidsof/despliegue/grupo/{trx}/
/saidsof/despliegue/farmacia/{id}/     /saidsof/agente/{estacion}/estado/
/saidsof/agente/{estacion}/heartbeat/  (versión POS, versión agente, SO, serie HW)
```

## 5. Módulo B — Inventario de Activos (flujos CRESIO)

Mismo panel, misma base, misma auditoría. Modelo:

- **OrdenCompra** — N° OC (trazador), proveedor, fecha, bodega destino, novedades.
- **Bodega** — Machala, Loja, Cuenca, Portoviejo… con custodio responsable.
- **Activo** — código `CR-[TIPO]-[NNNN]` (secuencial global por tipo), marca, modelo, serie, garantía, OC origen. Estados: `En bodega / Asignado / En reparación / En tránsito / Dado de baja`. **Nunca se elimina.**
- **Consumible + StockBodega** — control por cantidad; tóner asociado a su CR-IMP.
- **Colaborador** — carga manual al inicio (importación CSV/API RRHH prevista para después).
- **Asignacion** — activo × colaborador, estado físico entrega/devolución, consumibles, quién registró.
- **EventoActivo** — historial inmutable: ingreso, asignación, devolución, reparación, baja.

Flujos: Compra → Ingreso a bodega (etiquetado CR obligatorio) → Asignación → Desvinculación (motivo A: salida del colaborador; motivo B: baja/reparación).

**Sinergia con Módulo A:** el agente reporta el número de serie del hardware → vinculación automática estación (ML001-A) ↔ activo (CR-DSK-0047). Detecta equipos movidos sin registro y activos "dados de baja" que siguen vivos.

## 6. Auditoría (transversal a ambos módulos)

1. **Acciones del panel** — registro inmutable de quién subió/creó/lanzó/pausó cada despliegue y cada cambio de catálogo (`django-auditlog`). Flujo de **cuatro ojos** para envíos a toda la cadena.
2. **Trazabilidad end-to-end** — línea de tiempo por despliegue × estación con timestamps de cada paso (recibido, descargado, verificado, aplicado, rollback).
3. **Historial de versiones por estación** — qué versión corrió cada equipo y cuándo, cruzado con heartbeat.
4. **Reportes exportables** (CSV/PDF) y retención configurable (eventos 2 años, heartbeats 30 días).

## 7. Plan por fases

| Fase | Contenido | Duración | Estado |
|---|---|---|---|
| **0. Preparación** | Backup BD, Git, rotar credenciales, variables de entorno, documentar tópicos | 1 sem | ✅ Hecho |
| **1. Núcleo** | Django 5.2/Py 3.12, modelo nuevo (grupos/farmacias/estaciones/despliegues/auditoría), worker MQTT Python (absorbe Node.js) | 2-3 sem | ✅ Hecho — tareas periódicas vía Celery Beat (ver `config/celery.py` y servicios `celery_worker`/`celery_beat` en `docker-compose.yml`, agregados después como fundación async) |
| **2. Panel despliegues** | HTMX + Tailwind, dashboard tiempo real, matriz de versiones por TRX, anillos y olas, aprobación cuatro ojos, reportes | 2-3 sem | ✅ Hecho — dashboard por polling HTMX, no Channels (suficiente a esta escala) |
| **2b. Módulo activos** | OC, bodegas, activos CR, consumibles, asignaciones, etiquetas, reportes CRESIO | 3-4 sem | ✅ Hecho |
| **3. Agente .NET 10** | Windows Service self-contained, MQTTnet, auto-enrolamiento por token, ciclo descarga/verifica/aplica/rollback, MSI para GPO, auto-actualización | 3-4 sem | 🟡 Agente funcional y probado manualmente (`C:\Proyectos\saidsoft-agente`); **falta el instalador MSI** — ver §10 |
| **4. Integración POS** | Librería Saidsoft.Client, heartbeat con versión/serie, vinculación activo↔estación, flujo cierre-actualiza-relanza | 2 sem | ✅ Hecho |
| **5. Despliegue** | docker-compose (Django+worker+PostgreSQL+EMQX/TLS), piloto 2-3 farmacias (incluir el Win10 más viejo), rollout por anillos, respaldos | 1-2 sem | 🟡 Stack escrito y validado sin Docker (config carga, YAML válido); **`docker compose up` nunca se corrió en un servidor real** — ver §10 |

**Total original: 16-20 semanas.** Prioridad acordada: Módulo A (despliegues) primero; Módulo B (activos) después de la Fase 2.

Con las Fases 0-5 completas, el proyecto pasó a una segunda etapa no prevista en el
plan original: convertirlo en una plataforma multi-cliente (RMM tipo MSP) para las
unidades de negocio de CRESIO. Ver **sección 9** para esas fases y **sección 10** para
lo que falta cerrar antes de un rollout real.

## 8. Riesgos y mitigaciones

| Riesgo | Mitigación |
|---|---|
| Actualización mala llega a 600 farmacias | Anillos + freno automático por umbral de error + rollback local |
| VPN saturada por descargas masivas | Olas escalonadas + throttling; rol caché en -ADM previsto como plan B |
| Win10 builds viejos fallan con el agente | Self-contained .NET 10; piloto incluye la máquina más antigua; mínimo build 1607 |
| Win10 viejo sin TLS 1.2 forzado rompe scripts RMM ad-hoc (Invoke-WebRequest) | TLS 1.2 forzado explícito en `generar_comando_instalacion_meshcentral` (ver §10-G); falta validar contra una estación Windows 10 real sin parchar |
| Windows 10 sin soporte (oct-2025) | El inventario de SO del heartbeat alimenta el plan de migración a W11 |
| Instalar agente en 1.800 equipos | MSI + GPO/script, una sola vez; después se auto-actualiza por su propio canal |

## 9. Extensión multi-tenant / RMM (fases R1-R6a)

El sistema pasó de servir una sola operación a ser un RMM tipo MSP para las
unidades de negocio de CRESIO (San Gregorio, MIA, 7DIAS — ver `UnidadNegocio` en
`apps/catalogo/models.py`). Fases completas, en orden:

| Fase | Contenido | Estado |
|---|---|---|
| **R1 — Multi-tenancy** | `UnidadNegocio` como raíz del tenant. `Farmacia.unidad_negocio` obligatoria; `Despliegue`/`EjecucionScript` con `unidad_negocio` obligatoria; RBAC centralizado en `apps/cuentas/services.py` (`scope_por_unidad_negocio`, `verificar_acceso`, variantes `_opcional`/`_activa`); selector de "unidad de negocio activa" en el panel | ✅ Hecho |
| **R2 — Motor de alertas** | `ReglaAlerta`/`Alerta` en `apps/monitoreo`; evaluación en tiempo real desde `manejar_metricas` (mqtt_worker) + `marcar_estaciones_offline` (regla `sin_heartbeat`); notificación por correo al abrir una alerta | ✅ Hecho |
| **R3 — Scripting remoto** | Ya existía antes de esta etapa (`apps/scripts`, comando `ejecutar_script` del agente) | ✅ Hecho (preexistente) |
| **R4 — Parcheo de terceros + recurrencia** | `ScriptProgramado` (política "correr cada N días", mismo patrón que `MantenimientoProgramado`), scripts winget sembrados con `seed_scripts_parcheo`. No tocó el agente — ya soportaba `ejecutar_script` | ✅ Hecho |
| **R5 — Acceso remoto (MeshCentral)** | Ya existía antes de esta etapa | ✅ Hecho (preexistente) |
| **RBAC fast-follow** | `activos`/`mantenimiento`/`cumplimiento` conectados al RBAC de R1 (antes solo `@login_required`, sin escopar por cliente). `ColaboradorForm` ganó el campo `unidad_negocio`; `registrar_asignacion` lo hereda del colaborador al activo | ✅ Hecho |
| **R6a — Reportes por cliente** | Resumen imprimible por unidad de negocio (`/reportes/cliente/`) + CSVs de activos/alertas + `reporte_cumplimiento` ahora escopado (antes mostraba todo sin filtrar) | ✅ Hecho |

**Fuera de alcance de esta etapa, explícitamente diferido:**
- **ACLs MQTT/EMQX por tenant** — hoy todos los agentes comparten una sola credencial
  (`deploy/bootstrap-emqx.sh`), sin prefijo de unidad de negocio en los tópicos. El
  aislamiento de R1 es solo a nivel de aplicación/BD, no del broker. Requiere decidir
  el enfoque (prefijo de tópico vs. credencial por tenant + ACL `%u`) y casi seguro
  tocar `saidsoft-agente` (repo aparte).
- **Facturación por endpoint** (resto de R6) — conteo de estaciones activas por unidad
  de negocio por período; falta definir qué cuenta como "endpoint activo" (decisión de
  negocio, no técnica).
- **API REST pública** (resto de R6) — exponer despliegues/estaciones/alertas vía DRF
  con auth por token, escopada por `unidad_negocio`. Hoy solo existe API
  (`apps.mantenimiento.api_urls`) para mantenimiento.
- **Windows Update nativo** (resto de R4) — parchar el SO en sí, no apps de terceros.
  Requiere código nuevo en el agente (API de Windows Update, comando MQTT nuevo,
  tópico de reporte de cumplimiento).

## 10. Auditoría de pendientes (31-jul-2026)

Revisión de lo que falta cerrar antes de operar esto en producción real, más allá de
features nuevas. Por prioridad de riesgo:

**A. Cobertura de pruebas — ✅ Hecho (31-jul-2026)** para el lado Python; queda abierto
solo el lado del agente .NET:
- `apps/despliegues`: tests de `publicar_despliegue`, `evaluar_freno_automatico`,
  `verificar_completado` (incluye el caso de fuga entre unidades de negocio que
  comparten `Grupo`).
- `apps/mqtt_worker`: tests de los 6 handlers (`manejar_enrolamiento`, `manejar_heartbeat`,
  `manejar_estado_despliegue`, `manejar_info_equipo`, `manejar_estado_script`,
  `manejar_metricas`) y del enrutamiento por tópico de `Command._on_message`
  (`run_mqtt_worker.py`) — antes solo se probaba a mano contra un broker real.
  Además, `client.reconnect_delay_set(min_delay=1, max_delay=30)` acota el backoff
  entre reintentos (antes usaba el máximo por defecto de paho, 120s).
- `apps/cuentas` y `apps/auditoria` ya tienen suite propia (antes solo se ejercitaban
  indirectamente vía `apps/panel/tests.py`).
- **`saidsoft-agente`: sigue en cero tests automatizados** (no hay proyecto xUnit/NUnit);
  toda la validación de reconexión/rollback documentada en su README sigue siendo manual
  — no se tocó en esta fase (repo aparte).

**B. Producción nunca corrida de verdad — 🟢 mayormente cerrado, validado contra el
stack real (31-jul-2026):** Docker Desktop resultó estar disponible en la máquina de
desarrollo (el README decía lo contrario) — se levantó `docker compose up` de verdad
por primera vez en la historia del proyecto (`db`+`emqx`+`web`+`worker`+`scheduler`),
contra PostgreSQL 16 + TimescaleDB 2.17.2 y EMQX 5.8.3 reales, no simulados.

- **✅ Hecho y verificado**: nuevo servicio `scheduler` (`deploy/scheduler.sh`) corre
  `marcar_estaciones_offline`, `purgar_metricas` y `generar_ejecuciones_programadas`
  dentro del stack. **Bug real encontrado y corregido**: en el primer arranque,
  `scheduler` corrió su primer ciclo antes de que `web` terminara de migrar (no hay
  dependencia entre ambos), y al fallar marcaba el día como "hecho" igual — no
  reintentaba hasta el día siguiente. Ahora solo marca el día como hecho si las tareas
  salieron bien; si fallan, reintenta en el siguiente ciclo (60s). Confirmado
  funcionando limpio tras el fix.
- **✅ Hecho**: `deploy/backup.sh` (pg_dump + media, retención de 14 días locales) —
  no probado en esta sesión (necesita una BD con datos para tener sentido probarlo).
- **✅ Hecho y verificado end-to-end contra EMQX real**: `bootstrap-emqx.sh` siembra
  usuarios + ACLs por rol. **Dos hallazgos reales al probarlo**:
  1. EMQX **no trae `built_in_database` habilitada por defecto** — trae una fuente
     tipo `file` cuya última regla es literalmente `{allow, all}` (con un comentario
     del propio EMQX diciendo "cambiar esto en producción"). `no_match: deny` solo
     sirve una vez que `built_in_database` se declara explícitamente como fuente
     (`EMQX_AUTHORIZATION__SOURCES__1__TYPE` en `docker-compose.yml`) — sin eso, la
     regla `{allow, all}` de la fuente por defecto seguía ganando.
  2. El puerto por defecto del script (18083) no coincidía con el mapeo real de
     `docker-compose.yml` (8082) — corregido.
  Con ambos fixes, se probó de punta a punta con un publisher/subscriber MQTT reales
  sobre TLS: un tópico permitido entregó el mensaje, uno sin regla nunca llegó al
  suscriptor (bloqueado por el `no_match: deny`), aunque el publisher igual recibe el
  PUBACK (así es el protocolo MQTT — el ack no implica autorización, hay que
  verificar con un suscriptor real, no con el ack). Sigue sin resolver la
  segmentación por tenant (agente sigue siendo una credencial compartida, ver §9).
- **❌ Intentado y revertido**: el hypertable de TimescaleDB sigue **sin activar**
  (sin cambios respecto al intento anterior — ver detalle completo en
  `deploy/README-produccion.md`). Confirmado además contra TimescaleDB real: el
  mensaje de rechazo de `create_hypertable` es exactamente el esperado (`cannot
  create a unique index without the column "timestamp"`), y el resto de las
  migraciones (incluida toda la de R1-R6a) corre limpio en Postgres real por primera
  vez. Decisión tomada con el usuario: dejarlo documentado, no urgente para el piloto.

**C. Empaquetado del agente — 🟢 cerrado con decisión explícita (31-jul-2026):**
- `deploy/instalar-agente.ps1` (en `saidsoft-agente`) copia el publish self-contained,
  confía el CA de EMQX, registra el servicio de Windows (`New-Service` + reinicio
  automático si el proceso muere) y lo arranca. **Decisión del usuario**: script en
  vez de un `.msi` real (WiX) — mismo criterio de simplicidad que `scheduler.sh`/
  `backup.sh`; se distribuye igual por GPO o RMM.
- **Dos huecos reales encontrados y cerrados al revisar el script contra el código del
  agente** (no estaban en el script original, que solo cubría la conexión MQTT):
  1. No configuraba `ComandoHmacSecret` — sin él, el agente arranca y hace heartbeat
     normal, pero descarta en silencio todo comando remoto (`reiniciar`,
     `ejecutar_script`, `consultar_info`) por firma HMAC inválida, sin error visible
     en el panel. Ahora es parámetro obligatorio (`-ComandoHmacSecret`, debe coincidir
     con `COMANDO_HMAC_SECRET` de `deploy/.env`).
  2. No configuraba el POS real — se quedaba con los defaults de `appsettings.json`
     apuntando a `PosSimulado` (el POS de mentira de pruebas). Confirmado con el
     usuario: el POS real es Farmamia Cia Ltda - Elipsys (`Zabyca.Pos.Desktop.exe`,
     instalado en `C:\Program Files (x86)\Farmamia Cia Ltda - Elipsys\Cliente`), el
     mismo para San Gregorio/MIA/7DIAS (lo que cambia por estación es el nodo TRX de
     la config interna del propio POS, no la ruta de instalación). Ahora son
     parámetros del script con esos valores como default.
- **Bug de seguridad encontrado de paso, ya corregido**: no existía `.dockerignore` en
  `saidsoft-core`, así que `COPY . .` en `deploy/Dockerfile` horneaba dentro de la
  imagen `.env`, `deploy/.env` y `deploy/certs/*.pem` aunque estuvieran en
  `.gitignore` (`.gitignore` no protege el build context de Docker). Esto además
  explicaba por qué `COMANDO_HMAC_SECRET` "funcionaba" en la prueba en vivo de la
  Fase B sin estar declarado en `docker-compose.yml`: el contenedor terminó leyendo
  el `.env` de desarrollo horneado por accidente, no `deploy/.env`. Se agregó
  `.dockerignore` y se declaró `COMANDO_HMAC_SECRET` explícitamente en
  `docker-compose.yml` (`web` y `worker`) y en `deploy/.env.prod.example`.
- **`deploy/empaquetar-pos.ps1` (nuevo, en `saidsoft-agente`)**: arma un `.zip` de lo
  YA instalado en una estación, para probar el pipeline completo de despliegue
  (descarga → hash → cierre POS → respaldo → aplicar → relanzar → `ok`) sin cambiar
  nada funcional del POS real — un "piloto de humo" antes de arriesgar una
  actualización de verdad. Probado de verdad contra una carpeta simulada (zip
  generado y contenido inspeccionado).
- **Nodos TRX resueltos por exclusión (dato del usuario, 31-jul-2026)**: el
  `Zabyca.Pos.Desktop.exe.Config` del POS trae el nodo por estación
  (`<add key="Bdd" value="trx002" />` / `hub_...`), distinto por grupo — el proceso
  manual previo era editar esa línea, re-comprimir el cliente y enviar un zip por
  grupo. Como el apply del agente es un overlay, `empaquetar-pos.ps1` excluye ese
  archivo por defecto: cada estación conserva su nodo y un solo paquete sirve para
  toda la cadena (con `-IncluirConfig` para el caso raro de cambio de esquema, que
  entonces se apunta por grupo). Cambios de nodo deliberados: vía la ejecución de
  scripts del panel (R3) dirigida al grupo, no re-desplegando el POS.
- **`python manage.py cambiar_nodo_pos` (nuevo)**: automatiza en una sola pasada lo
  que antes era manual — arma el script PowerShell (edita la clave `Bdd` del `.Config`
  vía XPath `//add[@key='Bdd']`, con respaldo `.bak-<timestamp>` antes de escribir),
  crea el `Script` ad-hoc y registra + envía la `EjecucionScript` al grupo, todo en un
  solo comando. Valida el valor de `--nodo` contra `^[A-Za-z0-9_-]{1,50}$` (se inserta
  directo en el script) antes de tocar la base. Deliberadamente NO reinicia el POS
  (una estación puede tener una venta en curso) — el cambio toma efecto en el próximo
  arranque. 9/9 tests de `apps/scripts` en verde, incluida la fuga entre grupos.
- **Bug de encoding real encontrado y corregido en los dos `.ps1` de `deploy/`**:
  Windows PowerShell 5.1 asume codificación ANSI si el script no trae BOM UTF-8; con
  tildes o guion largo eso rompe el parseo completo del archivo con un error que no
  apunta al carácter real (reproducido con un caso mínimo). Afectaba a
  `instalar-agente.ps1` desde antes de esta sesión, no solo a lo agregado hoy. Ambos
  scripts ahora se guardan con BOM.
- **Sigue pendiente**: no existe todavía ningún paquete de despliegue real para
  Zabyca/Elipsys (todo lo probado hasta hoy fue contra `PosSimulado`); antes de
  publicar el primer despliegue real hay que verificar en una estación piloto que el
  `.zip` subido al panel trae exactamente lo que el POS real necesita.

**D. Deuda menor:**
- Cargos migrados con `Departamento` placeholder *"Sin clasificar (migrado)"*
  (`apps/activos/migrations/0010_migrar_cargo_texto_a_fk.py`) pendientes de
  reasignación manual.

**E. MeshCentral integrado al stack de producción — 🟢 cerrado (31-jul-2026):**
- Antes corría aparte con un `docker run` suelto (fuera del ciclo de vida del resto del
  stack); ahora es un servicio más de `deploy/docker-compose.yml` (`meshcentral_data`
  propio, puerto `8083:443` dentro del rango ya abierto en firewall). `docker compose
  up`/`down` lo levanta y apaga junto con todo lo demás.
- **Bug real encontrado y corregido de paso**: `MESHCENTRAL_SERVER_URL`/
  `MESHCENTRAL_MESH_ID` tienen default en `config/settings/base.py` (a diferencia de
  `COMANDO_HMAC_SECRET`), así que si `docker-compose.yml` no las declara explícitamente
  para `web`, un `deploy/.env` que sí las define de todos modos no llegaría al
  contenedor — pero SIN fallar al arrancar, solo generando links rotos a `localhost` en
  silencio. Mismo patrón de bug que el de `COMANDO_HMAC_SECRET` en Fase B/C, esta vez
  detectado antes de que afectara algo real. Se declararon con el mismo default que
  Django (`${VAR:-mismo-default-que-base.py}`) para que un `.env` incompleto se comporte
  igual con o sin Compose de por medio.
- **Sigue igual que antes (no es nuevo)**: la primera cuenta/admin y el device group se
  siguen creando a mano desde la consola web la primera vez (MeshCentral no lo expone
  por variable de entorno); el vínculo estación↔`node_id` sigue siendo manual (ver
  "diferido a propósito" abajo); los `viewmode` de escritorio/terminal siguen sin
  verificarse contra una instancia real.
- **Stack completo levantado de nuevo con estos cambios (31-jul-2026) y se encontraron
  dos bugs reales más, ambos corregidos y verificados**:
  1. El `.dockerignore` de la Fase C (necesario para no hornear secretos en la imagen)
     excluía `deploy/certs/*.pem` completo — pero `cert.pem` (público, no la llave) lo
     necesitan `web`/`worker` DENTRO de la imagen (`MQTT_CA_CERT=/app/deploy/certs/
     cert.pem`) para verificar TLS contra EMQX. El worker crasheaba en bucle
     (`FileNotFoundError` en `tls_set`). Corregido: solo se excluye `key.pem`.
  2. `scheduler` corre bajo `config.settings.produccion` igual que `web`/`worker`, así
     que también necesita `COMANDO_HMAC_SECRET` (sin default) — se me había quedado
     afuera al agregarlo a Fase C. `scheduler` tronaba en bucle (`ImproperlyConfigured`)
     en sus tareas diarias. Corregido, agregado a su `environment` en
     `docker-compose.yml`.
  Con ambos fixes: los 6 servicios (`db`/`emqx`/`meshcentral`/`scheduler`/`web`/
  `worker`) quedaron `Up` sin reinicios, `scheduler` corrió sus 3 tareas limpio, `worker`
  conectó al broker, `web` y `meshcentral` respondieron 200. El volumen `deploy_emqx_data`
  de la sesión anterior tenía credenciales desalineadas con el `deploy/.env` actual —
  se eliminó (solo data de prueba) y se re-sembró con `bootstrap-emqx.sh`.

**F. 🔴 Bug real encontrado en el agente .NET: no reconecta al MQTT configurado
(6-ago-2026, piloto ML016-A):**
- Instalado con el paquete de un clic (`deploy/docs/prueba-agente/paquete-instalacion/`)
  contra el piloto real (`10.111.6.20:8081`). Enroló y quedó aprobado (`identidad.json`
  en la estación muestra `Aprobada: true` con token), pero **nunca volvió a conectar
  después de la primera sesión** — sin heartbeats sostenidos ni respuesta a
  `consultar_info` (por eso el modal de la estación en el panel queda con
  procesador/RAM/almacenamiento/BitLocker/número de serie vacíos indefinidamente).
- Diagnosticado descartando las causas obvias directamente en la estación: un solo
  proceso `Saidsoft.Agente.exe` corriendo (no hay un proceso viejo sin configurar de
  fondo), un solo servicio `SaidsoftAgente` (`Running`/`Automatic`), y
  `C:\Program Files\Saidsoft\Agente\appsettings.Production.json` con el `MqttHost`/
  `MqttPuerto` correctos (`10.111.6.20`/`8081`) — el archivo que escribe
  `instalar-agente.ps1` está bien.
- El Visor de eventos (Application) de la estación muestra en cambio decenas de
  reintentos consecutivos (`ConectarConReintentosAsync`, categoría
  `Saidsoft.Agente.Mqtt.ClienteMqttSaidsoft`) fallando contra
  `'Unspecified/localhost:1883'` — el host/puerto configurados no llegan a esa ruta de
  reconexión, que cae al default de MQTTnet. Conclusión: la conexión inicial sí lee
  `appsettings.Production.json` (por eso pudo enrolar), pero el código de
  reconexión arma el `MqttClientOptions` sin pasarle el host/puerto configurados —
  bug de código en `saidsoft-agente` (repo aparte, no vive en esta máquina), no un
  problema de configuración ni del panel/worker de `saidsoft-core`.
- **Sin esto arreglado, ninguna estación real sobrevive más allá de su primera
  sesión de conexión** — bloqueante para el rollout de las ~600 farmacias, no solo
  para completar la info de hardware de ML016-A. Corregir `ConectarConReintentosAsync`
  (o el método equivalente) para que reutilice el `MqttHost`/`MqttPuerto` configurados
  en cada reintento, no solo en la conexión inicial.

**G. Bug real encontrado y corregido: script de instalación de MeshCentral no
soportaba el TLS del piloto (6-ago-2026, ML016-A):**
- `generar_comando_instalacion_meshcentral` (`apps/catalogo/services.py`) arma un
  one-liner de PowerShell que corre `Invoke-WebRequest` contra la consola de
  MeshCentral (certificado autofirmado en el piloto). Sin más, Windows PowerShell 5.1
  lo rechazaba en dos pasos sucesivos, cada uno encontrado al probar contra la
  estación real:
  1. "No se puede establecer una relación de confianza para el canal seguro
     SSL/TLS" — el certificado autofirmado no está en el almacén de confianza de la
     estación. Corregido con
     `[System.Net.ServicePointManager]::ServerCertificateValidationCallback = {$true}`
     antes de la descarga (no depende de `-SkipCertificateCheck`, que recién existe en
     PowerShell 7+, para no romper en Windows 10 con solo PowerShell 5.1).
  2. Ya sin el error de certificado, "error inesperado de envío" al conectar — Windows
     PowerShell 5.1 no negocia TLS 1.2 por default en muchas instalaciones de .NET
     Framework, y MeshCentral (Node.js moderno) no ofrece nada más viejo. Corregido con
     `[System.Net.ServicePointManager]::SecurityProtocol = [...]::Tls12` antes de la
     descarga.
- **Sin verificar todavía**: si esto alcanza en una estación Windows 10 realmente vieja
  y sin parchar (no solo sin TLS 1.2 forzado, sino potencialmente sin los cipher suites
  modernos que un Node.js reciente prefiere, o con un .NET Framework tan viejo que
  `SecurityProtocolType.Tls12` exista pero SChannel no lo tenga habilitado a nivel de
  SO). El piso documentado (build 1607) trae TLS 1.2 a nivel de SO, pero eso no
  garantiza que el handshake completo funcione sin parches acumulativos razonablemente
  recientes. Validar contra la estación más vieja disponible antes de asumir que este
  fix cierra el tema para todo el parque de ~600 farmacias (ver fila correspondiente en
  §8).

**H. Primer despliegue real del piloto: cuatro bugs apilados, todos de la misma
familia "falla en silencio" (6-ago-2026, ML016-A) — 🟢 corregidos:**

Subir un `.zip` de 32 MB desde una farmacia y publicarlo a una estación destapó
cuatro defectos independientes, cada uno tapando al siguiente. Ninguno mostraba un
error accionable: la página quedaba "cargando", o el formulario volvía idéntico, o
el agente reportaba un mensaje genérico.

1. **`gunicorn --timeout 120`** mataba al worker a mitad de la subida (la VPN de la
   farmacia da ~50-126ms de RTT y MSS 1394; 32 MB tardan varios minutos). Subido a
   600s en `docker-compose.yml`. Se agregó `--access-logfile -`: el diagnóstico se
   hizo con `tcpdump` porque los requests no se veían en `docker-compose logs web`.
2. **El formulario de despliegue nunca renderizaba `unidad_negocio`** (obligatorio
   desde R1) y solo mostraba los errores de `version`/`archivo`. Un usuario con
   acceso a varias unidades no podía crear despliegues: el POST volvía con "este
   campo es obligatorio" sobre un campo inexistente en pantalla, y ese error tampoco
   se imprimía. Corregido en `templates/panel/despliegue_form.html` (campo + errores
   de todos los campos + `non_field_errors`).
3. **`/app/media` era de root dentro del contenedor** (el volumen `media_data` toma
   el dueño del punto de montaje de la imagen la primera vez que se monta vacío, y
   el directorio no existía en la imagen) → `PermissionError` al guardar el `.zip`.
   Corregido en `deploy/Dockerfile`; en un despliegue ya existente hay que correr
   una vez `docker exec -u root deploy_web_1 chown appuser:appuser /app/media`.
4. **Nadie servía `/media/` en producción**: `static()` devuelve `[]` con
   `DEBUG=False`, así que todas las descargas de agentes daban 404 — el agente lo
   reportaba como "No se pudo descargar/verificar el paquete de ninguna fuente", sin
   distinguirlo de un problema de red o de hash. Corregido en `config/urls.py` con
   una ruta explícita a `django.views.static.serve`, más `rstrip('/')` en
   `ARCHIVOS_BASE_URL` (`apps/despliegues/services.py`, `apps/software/services.py`):
   una barra final producía `//media/...`, que no matchea el patrón y daba el mismo
   404 opaco. Hay tests de regresión para los dos casos en `apps/despliegues/tests.py`.

**Pendiente relacionado**: servir media con Django ocupa un worker de gunicorn
mientras dura cada descarga. Alcanza para el piloto; a escala real (~1.800
estaciones, aun con la distribución en cascada por caché de farmacia) esto necesita
nginx sirviendo el volumen directo.

**Quinto bug de la misma sesión, en el flujo de recuperación**: con el freno
automático activado por el 404 del bug 4, "Reanudar" en el detalle del despliegue
solo cambiaba el estado a `publicando` **sin volver a publicar nada por MQTT** — el
operador reanudaba un despliegue que nunca reintentaba de verdad; solo avanzaba si
el agente reconectaba por su cuenta y recogía el mensaje retenido original.
Corregido con `reintentar_despliegue()` (`apps/despliegues/services.py`), que
publica al tópico individual de cada estación pendiente en vez del tópico agregado
de grupo/farmacia/cadena — así no le reenvía el paquete a las estaciones que ya lo
aplicaron con éxito (les cerraría y reinstalaría el POS sin necesidad). Tests en
`apps/despliegues/tests.py::ReintentarDespliegueTests` y
`DespliegueReanudarVistaTests`.

**I. Auditoría de bugs "ocultos" del panel a partir de los anteriores (6-ago-2026)
— 🟢 corregidos:** los defectos de arriba tenían patrones repetibles, así que se
auditó el panel entero buscando más casos de cada uno:

- **`hx-post` sin token CSRF** (Django responde 403 y htmx no actualiza nada, así
  que el clic "no hace nada"): afectaba a aprobar/rechazar estación, aprobar en lote
  y "Actualizar ahora" del modal de estación. Eran los únicos `hx-post` fuera de un
  `<form>` con `{% csrf_token %}`; todos corregidos con `hx-headers`.
- **Polling que pisa estado de la UI**: los partials con `hx-trigger="every Ns"`
  reemplazan el nodo completo, borrando lo que el operador estaba haciendo. Casos
  encontrados y corregidos: el `<details>` de "Ver salida" en ejecuciones de scripts
  (`hx-preserve`), las casillas de aprobación en lote de estaciones (el refresco de
  15s ahora se pausa mientras haya alguna marcada — si no, la selección se borraba y
  "Aprobar seleccionadas" enviaba una lista vacía), el input del Node ID de
  MeshCentral (`hx-preserve`; el refresco de 2s lo vaciaba mientras se pegaba el ID,
  probable causa de que la vinculación nunca prosperara) y la clave de recuperación
  de BitLocker (el refresco de 2s la borraba de pantalla y obligaba a pedirla de
  nuevo, generando un evento de auditoría por intento — ahora el polling se apaga
  mientras la clave está visible).
- **Acciones que no dicen nada cuando no hacen nada**: `estaciones_aprobar_lote` con
  cero seleccionadas devolvía la misma tabla sin aviso (indistinguible de un fallo
  silencioso); ahora avisa, y también cuando aprueba menos de las seleccionadas.
- **Errores de campo invisibles** (mismo patrón que el bug 2 de §10-H): el formulario
  de firma de mantenimiento solo imprimía `non_field_errors`, así que "Falta capturar
  la firma" (un error de campo) no se veía nunca. Corregido. El resto de los
  formularios usa `panel/accion_form.html`, que itera `{% for field in form %}` y
  siempre muestra todos los errores — no tenían el problema.
- **Latente, corregido de paso**: `DespliegueForm.__init__` usaba
  `Farmacia.objects.none()` como fallback del queryset de `unidad_negocio` (modelo
  equivocado); no explotaba solo porque está vacío y las vistas siempre pasan `user`.

**J. Segundo despliegue real del piloto: verificación de hash siempre fallaba por
mayúsculas/minúsculas (10-ago-2026, ML016-A) — 🟡 diagnosticado, fix pendiente en
`saidsoft-agente` (repo aparte, no vive acá):**

Con el fix del bug H.4 ya desplegado en producción (`/media/` sirviendo bien), volver a
publicar a ML016-A seguía dando el mismo mensaje genérico "No se pudo
descargar/verificar el paquete de ninguna fuente" — sin ningún request nuevo en
`docker-compose logs web`, lo que hacía sospechar de red otra vez. Se descartó paso a
paso: `Test-NetConnection` al puerto del panel OK, `Invoke-WebRequest` a la URL exacta
del `.zip` OK (mismo tamaño en bytes que el original: 32714043). El problema apareció
al comparar el SHA-256: el servidor guarda/envía el hash en minúsculas
(`hashlib.hexdigest()` de Python siempre es lowercase), y el agente casi con certeza lo
calcula con `Convert.ToHexString()` de .NET, que devuelve **mayúsculas** por defecto.
Los dos hashes eran el mismo valor, solo con distinto casing — la descarga nunca fue el
problema, la comparación de string sí (`==` en vez de
`StringComparison.OrdinalIgnoreCase`). Mismo patrón que H.4: dos causas raíz
completamente distintas (404 de red vs. mismatch de casing) cayendo en el mismo mensaje
opaco del lado del agente, sin loguear nada localmente (ni archivo ni Visor de
eventos) que distinga una de otra.

**No corregible desde este repo**: la comparación vive en `saidsoft-agente` (C#), que
no está clonado en este entorno. Pendiente: normalizar el hash calculado a minúsculas
(o usar `StringComparison.OrdinalIgnoreCase`) en el método que verifica el paquete tras
la descarga, recompilar (`dotnet publish`) y reinstalar en las estaciones con
`instalar-agente.ps1` / el paquete de `deploy/docs/prueba-agente/paquete-instalacion/`.

**Superado por K**: no se logró ubicar la máquina de build del agente C# (ver K). El
piloto sigue con este bug hasta que ML016-A se migre al reemplazo en Python, que ya
nace con la comparación insensible a mayúsculas/minúsculas corregida.

**K. Reemplazo del agente C# perdido por una extensión en Python del "agente de
prueba" (10-ago-2026) — 🟢 hecho y validado de punta a punta en ML016-A contra el POS
real (descarga → hash → backup → aplicar → relanzar → `Confirmado OK`):**

Al intentar corregir el bug J, no se pudo ubicar ninguna máquina con el código fuente
de `saidsoft-agente` (C#) — ni en este entorno, ni en la ruta de compilación que
delataba un stack trace de un log previo (`C:\Proyectos\saidsoft-agente\...`), ni en
ninguna PC que el usuario reconociera. Con el repo fuente efectivamente perdido, la
opción de "corregir una línea y recompilar" no estaba disponible.

En vez de reconstruir el agente C# desde cero, se decidió promover
`agente-prueba/agente_prueba.py` (documentado hasta entonces como herramienta de
prueba, no de producción — ver `agente-prueba/README.md`) a agente de producción del
piloto, completándolo con lo único que le faltaba para tener paridad funcional:

1. **Despliegues de POS**: descarga, verificación de SHA-256 (insensible a
   mayúsculas/minúsculas desde el día uno — corrige el bug J de origen), aplicación
   según `modo_aplicacion` (inmediato / ventana programada / al cierre del POS),
   respaldo completo de la carpeta del POS antes de sobrescribir, relanzamiento y
   rollback automático si el POS no vuelve a quedar corriendo (chequeo de liveness por
   `tasklist`). Reporta la misma línea de tiempo de eventos que ya esperaba el
   servidor (`EventoDespliegue.Paso`) — no hizo falta tocar `saidsoft-core` para esto.
2. **Descarga con caché de farmacia**: si el despliegue trae `usar_cache=true`, intenta
   primero `cache_url_base` (recibido en el enrolamiento) antes de caer al central —
   mismo contrato que ya usaba el catálogo de software. Sigue sin implementarse que
   una estación *actúe* de caché para otras (`es_cache_farmacia`); eso queda fuera de
   alcance, como antes.
3. **Servicio de Windows**: `agente-prueba/servicio_windows.py` (pywin32) +
   `agente-prueba/instalar-servicio.ps1`, con la misma política de reinicio automático
   que tenía el agente C# (`sc.exe failure ... restart/30000/restart/60000/
   restart/120000`) y el mismo nombre de servicio (`SaidsoftAgente`) — lo reemplaza en
   el lugar. Antes el "agente de prueba" solo corría como consola manual.
   `agente_prueba.spec` pasó de ser un archivo autogenerado descartable a versionado a
   propósito (define los dos ejecutables — consola y servicio — más los
   `hiddenimports` de pywin32 que necesita el segundo; ver comentario en el archivo y
   `.gitignore`).
4. **Bug encontrado al probar el servicio compilado** (no relacionado al bug J):
   `correr()` usaba `client.connect()` (bloqueante) antes de `loop_forever()` — si el
   primer intento de conexión fallaba (broker caído o red no lista al bootear como
   servicio), la excepción moría en el hilo del agente en silencio y el servicio de
   Windows quedaba "Running" sin hacer nada, sin reintentar nunca. Corregido con
   `connect_async()` + `loop_forever(retry_first_connection=True)`, que sí reintenta
   desde el primer intento igual que reintenta reconexiones posteriores. Se detectó
   corriendo `Saidsoft.Agente.exe debug` (modo primer plano de pywin32, sin instalar
   nada) contra un broker inexistente — antes del fix el hilo moría con traceback, con
   el fix sigue reintentando sin cortar el proceso.
5. **Bug encontrado en la primera instalación real (ML016-A)**: el servicio se
   registraba bien (`sc.exe qc` mostraba el `BINARY_PATH_NAME` correctamente citado —
   no era el típico problema de espacio sin comillas en "Program Files") pero fallaba
   al arrancar: *"El servicio no respondió a tiempo a la solicitud de inicio o de
   control"*. Reproducido corriendo `Saidsoft.Agente.exe debug` directo en la estación:
   `PermissionError` al intentar abrir `agente_prueba.log` dentro de
   `C:\Program Files\Saidsoft\Agente\` — esa carpeta no está pensada para escritura en
   tiempo de ejecución, ni corriendo como Administrador. Corregido: `identidad.json` y
   el log ahora se escriben en `C:\ProgramData\Saidsoft\` (mismo directorio que ya
   usaba el agente C# original para su identidad), separado de la carpeta de
   instalación (que queda con `.exe`/`cert.pem`/`config.json`, estáticos).
6. **Segundo bug encontrado reinstalando en ML016-A con el fix del bug 5**: mismo
   síntoma exacto (*"El servicio no respondió a tiempo..."*), causa distinta.
   `sc.exe qc`/Visor de eventos no daban más detalle que el error genérico 1053; se
   volvió a reproducir con `Saidsoft.Agente.exe debug` en la estación — esta vez
   `json.load` fallaba con *"Unexpected UTF-8 BOM (decode using utf-8-sig)"* al leer
   `config.json`. Causa: `instalar-servicio.ps1` lo escribe con
   `Set-Content -Encoding utf8`, que en Windows PowerShell 5.1 (no Core/pwsh) siempre
   antepone un BOM — el lector en `servicio_windows.py` abría el archivo con
   `encoding='utf-8'` a secas, que no lo tolera. No se reprodujo en esta máquina de
   desarrollo en el primer intento porque acá se usa PowerShell 7 (pwsh), cuyo
   `-Encoding utf8` NO agrega BOM — la discrepancia de versión de PowerShell entre la
   máquina de build/pruebas y la estación real tapó el bug hasta instalarlo de verdad.
   Corregido leyendo con `encoding='utf-8-sig'` (tolera archivos con o sin BOM), y
   validado localmente forzando un BOM real (`EF BB BF`) a mano para replicar
   exactamente el archivo que produce la estación.
7. **Tercer bug — la causa raíz real del 1053, encontrada reproduciendo el servicio
   completo en la máquina de dev**: aun con los fixes 5 y 6, el servicio seguía sin
   arrancar (mismo 1053), con una firma muy específica: el proceso **nunca aparecía**
   en la lista de procesos, ni un instante, sin excepción, sin log, sin evento de
   crash — pero `debug`/`install`/`stop` desde consola funcionaban perfecto. Se
   descartaron por prueba directa: permisos de cuenta (falló igual con `LocalSystem`
   y con una cuenta de servicio dedicada), la carpeta Temp del perfil de SYSTEM
   (existía/creada, sin cambio), antivirus (Defender sin detecciones) y el subsistema
   de consola del exe (`console=False`, sin cambio). La causa real: el `__main__` de
   `servicio_windows.py` llamaba solo a `win32serviceutil.HandleCommandLine(...)`.
   Cuando el SCM lanza el binario de un servicio lo hace **sin argumentos**, y
   `HandleCommandLine` con argv vacío imprime el texto de uso y **sale
   inmediatamente** — el proceso moría en milisegundos (por eso nunca se veía) y el
   SCM esperaba 30s una conexión que nunca iba a llegar. En el setup normal de
   pywin32 el binario registrado es `PythonService.exe`, que sí se conecta al
   dispatcher; empaquetado con PyInstaller, nuestro exe ES el binario del servicio y
   debe hacerlo él mismo. Corregido: con `len(sys.argv) == 1` va a
   `servicemanager.Initialize()` + `PrepareToHostSingle` +
   `StartServiceCtrlDispatcher()`; con argumentos, sigue en `HandleCommandLine`.
   Gotcha adicional del camino: una cuenta de servicio creada por línea de comandos
   no recibe el derecho "Iniciar sesión como servicio" (error 1069 en el Visor de
   eventos, mucho más claro que el 1053) — `services.msc` lo otorga solo, `sc.exe
   config`/pywin32 no. No hizo falta: `LocalSystem` funciona bien (la teoría de una
   política corporativa bloqueando SYSTEM quedó descartada), y es lo que usa
   `instalar-servicio.ps1` por defecto.

**Validado en este entorno (máquina de dev, con permiso del usuario, servicio real
instalado y luego desinstalado)**: `instalar-servicio.ps1` + `Start-Service` dejan el
servicio **Running** bajo `LocalSystem`, con el proceso vivo, el log escribiéndose en
`C:\ProgramData\Saidsoft\` y el loop MQTT reintentando (no hay broker local, como se
espera). También validado: los dos ejecutables compilan, `debug` corre en primer
plano, y el `config.json` con BOM real de Windows PowerShell 5.1 se lee bien.
**Validado en ML016-A**: con el fix del bug 7, `instalar-servicio.ps1` deja el servicio
**Running** de verdad ahí también, reemplazando al agente C# previo. Reintentando el
despliegue #3 (ahora versión de prueba "7.7.7.7") desde el panel, el ciclo llegó hasta
`Descargado` → `Hash verificado` → `POS cerrado` → `Archivos aplicados` — la lógica de
descarga/hash/backup/aplicar funciona de punta a punta contra un despliegue real. Se
frenó en relanzar el POS con `[WinError 2] El sistema no puede encontrar el archivo
especificado`: **no es un bug, ML016-A todavía no tiene el POS real instalado**
(`instalar-servicio.ps1` ya lo había advertido). Depurando ese frenazo se encontró un
bug real:

8. **El rollback no reportaba `rollback` si fallaba al relanzar el POS**: `_rollback()`
   respalda, restaura los archivos y vuelve a llamar `_iniciar_pos()` para relanzar —
   pero si ESE segundo intento también fallaba (mismo motivo que el original: el POS
   no existe ahí), la excepción se escapaba de `_rollback()` sin llegar nunca a la
   línea que reporta el paso `rollback`, subía hasta `_procesar_despliegue()` y
   terminaba como un `error` genérico — indistinguible de "no se aplicó nada".
   Encontrado exactamente así en ML016-A. Corregido envolviendo el segundo
   `_iniciar_pos()` en su propio `try/except`: si falla, se loguea aparte pero el
   rollback de archivos igual se reporta con el motivo original.

**Otro hallazgo, de configuración, no de código**: `ARCHIVOS_BASE_URL` en
`deploy/.env` del servidor estaba `http://10.111.6.20`, **sin el puerto `:8080`** —
por eso el agente recibía 404 al construir la URL de descarga (el navegador/`curl`
manual siempre se probó con `:8080` a mano, por eso nunca se notó). Corregido en el
`.env` del servidor; no requirió cambios de código.

**No validado todavía**: el ciclo completo llegando a `Confirmado OK` contra un POS
real o un ejecutable de prueba en `pos_comando_iniciar` (recomendado antes de confiar
el rollback automático en una farmacia real con el POS de verdad instalado), y el fix
del bug 8 reinstalado en ML016-A (se corrigió después de este intento).

Con el POS real instalado en ML016-A (una carpeta con `Zabyca.Pos.Desktop.exe`
copiada a mano, sin instalador) y el fix del bug 8, el despliegue reportó
`Confirmado OK` — pero al revisar la estación, la carpeta del POS había quedado
intacta: apareció una **subcarpeta nueva** (`Cliente\Cliente\...`) al lado, sin pisar
los archivos reales.

9. **`_extraer_paquete` no contaba con que el `.zip` trajera su propia carpeta raíz
   envolvente**: `zf.extractall(destino)` extrae tal cual viene el archivo — si el
   `.zip` tiene todo debajo de una carpeta (ej. `Cliente/Zabyca.Pos.Desktop.exe` en
   vez del ejecutable suelto en la raíz del zip), el resultado es una subcarpeta
   nueva dentro de `pos_carpeta_instalacion`, no un reemplazo de los archivos reales
   — y como no tira ninguna excepción, el despliegue "tiene éxito" sin haber
   actualizado nada. Corregido: si todo el contenido del zip cuelga de una única
   carpeta raíz común, se extrae salteando ese primer nivel para que caiga directo en
   `pos_carpeta_instalacion`; si el zip ya viene plano (sin envoltorio), se comporta
   igual que antes. Probado localmente con ambos casos (zip anidado y zip plano)
   antes de recompilar.

**Validado**: reinstalado en ML016-A con el fix del bug 9, subcarpeta `Cliente\
Cliente\` limpiada a mano, despliegue reintentado — llegó a `Confirmado OK` con los
archivos del POS real actualizados de verdad (sin subcarpeta anidada). El circuito
completo de despliegue de POS (servidor → agente Python como servicio de Windows →
POS real) queda cerrado y probado de punta a punta por primera vez en el piloto.

**Pendiente, no bloqueante**: revertir el `ServicesPipeTimeout=120000` que se probó
en ML016-A durante el diagnóstico del bug 7 (no era la causa; `Remove-ItemProperty
-Path "HKLM:\SYSTEM\CurrentControlSet\Control" -Name ServicesPipeTimeout` + reinicio,
o dejarlo si no molesta — solo alarga el timeout de arranque de todos los servicios).

**L. El catálogo de software nunca pudo publicar nada por MQTT — ACLs de EMQX
incompletas (10-ago-2026) — 🟢 corregido y validado de punta a punta (actualización
real de Firefox en ML016-A, descarga → hash → instalación silenciosa → confirmado):**

Probando el catálogo de software por primera vez contra una estación real (actualizar
Firefox en ML016-A), la `SolicitudInstalacion` se publicaba sin error visible en el
panel pero el agente nunca la recibía — se quedaba en `Pendiente` para siempre, sin
ningún evento posterior. El agente estaba conectado y mandando heartbeats con
normalidad (descartaba problema de red/agente); los despliegues de POS sí funcionaban
por el mismo broker.

Causa: `deploy/bootstrap-emqx.sh` (que siembra las ACLs por tópico en EMQX) nunca se
actualizó cuando se agregó el catálogo de software — el usuario `panel` tenía permiso
de `publish` para los tópicos de `/saidsof/despliegue/...` y `/saidsof/agente/+/
comando/`, pero no para `/saidsof/software/...` ni `/saidsof/agente/+/software/`; el
`worker` tampoco tenía permiso de `subscribe` para `/saidsof/agente/+/
software_estado/`. Con `EMQX_AUTHORIZATION__NO_MATCH=deny` (ver comentario en el
propio script), cualquier tópico sin regla explícita se descarta en silencio — ni el
publisher ni el broker devuelven un error visible, por eso el mensaje simplemente
desaparecía sin dejar rastro en ningún lado. Mismo patrón que el gotcha de EMQX ya
documentado (fuente `file` con `{allow,all}`), pero en la dirección opuesta: acá la
regla que faltaba era la que debía *permitir*.

Corregido agregando las reglas faltantes a `bootstrap-emqx.sh` (publish de software
para `panel`, subscribe de `software_estado` para `worker`).

**Segundo bug, encontrado al aplicar el fix anterior contra el EMQX real**: el
endpoint que usaba el script para sembrar ACLs
(`POST .../authorization/sources/built_in_database/rules/users`) **no es idempotente**
— es un import de una sola vez; si el usuario ya tenía reglas cargadas (como acá,
porque el script ya se había corrido antes), devuelve `409 ALREADY_EXISTS` y no
actualiza nada. El comentario original en el script decía lo contrario (que era
seguro re-correrlo); era una suposición nunca puesta a prueba, hasta que hubo un
motivo real para correrlo dos veces. Corregido reemplazando el `POST` masivo por un
`PUT` a `.../rules/users/{username}` (uno por usuario) — ese endpoint sí es un "set"
real: crea si no existe, reemplaza si ya había reglas.

**Tercer bug, mismo diagnóstico en caliente**: el `PUT` por sí solo no alcanzaba —
EMQX devolvía `400 BAD_REQUEST` (`required_field: root.username`) porque el body
necesita `"username"` explícito además de `"rules"`, no alcanza con que vaya en la
URL. Corregido agregándolo al body. Verificado armando el JSON con un `curl` de
mentira (sin pegarle a la red real) y confirmando que cada payload parsea bien y
lleva ambos campos.

**Validado**: `bootstrap-emqx.sh` corrido en el servidor real con los tres fixes
aplicados (ACLs de software + endpoint idempotente + `username` en el body) — las
tres credenciales (`saidsof_agente`, `saidsof_worker`, `saidsof_panel`) quedaron
definidas sin error. Una solicitud de instalación de Firefox nueva, bien dirigida a
ML016-A, llegó de punta a punta: recibido → descargado → hash_verificado →
instalando → instalado, y la versión nueva quedó confirmada en la estación (tras
cerrar y reabrir Firefox, que estaba corriendo durante la instalación silenciosa).

**Pendiente de verificar en general**: si hay más tópicos que quedaron sin ACL desde
que se agregaron software/scripts (esta sesión encontró el gap en software porque se
probó por primera vez hoy; scripts sí tenía sus reglas completas desde antes — no se
auditaron sistemáticamente todos los tópicos del protocolo contra
las ACLs sembradas, esto fue reactivo a un síntoma puntual).

**M. Comandos `consultar_info`/`reiniciar` implementados en el agente, y paquete de
instalación "un clic" (11-ago-2026) — 🟢 hecho:**

Instalando una segunda estación real (ML006-A, primer despliegue en Windows 10 22H2 —
sin problemas de compatibilidad, es una build reciente y parchada), se notó que el
botón "Actualizar ahora" del panel no hacía nada: el agente Python solo tenía
implementado `ejecutar_script`, el resto de los comandos caían al log "no
implementado" (`consultar_info` y `reiniciar`, heredado del alcance original de
"agente de prueba"). Ambos quedaron implementados con el mismo esquema de firma HMAC
que ya usaba `ejecutar_script`:

- **`consultar_info`**: un solo script de PowerShell arma hostname/procesador/RAM/
  almacenamiento (vía CIM) + BitLocker del volumen `C:` como JSON, que el agente
  parsea y publica a `/saidsof/agente/{codigo}/info_equipo/`. Si BitLocker no está
  disponible, esos campos quedan vacíos sin romper el resto. Verificado corriendo el
  script real en esta máquina (devolvió hardware real) y comparando la firma HMAC que
  arma el agente contra `apps.catalogo.services.firmar_payload` del servidor (mismo
  `COMANDO_HMAC_SECRET`) — coinciden.
- **`reiniciar`**: reinicia el equipo Windows completo (no el servicio del agente) con
  `shutdown /r /t 10`, fire-and-forget, sin canal de confirmación de vuelta — coherente
  con que el botón del panel ya avisa "esto interrumpe cualquier venta en curso". No se
  ejecutó el shutdown real en ninguna máquina de prueba (por motivos obvios).

**Paquete "un clic"** (`agente-prueba/Instalar.bat` + `config.ejemplo.txt`), mismo
patrón que ya existía para el agente C# (`deploy/docs/prueba-agente/paquete-
instalacion/`), para no tener que escribir el comando de `instalar-servicio.ps1` a
mano en cada estación nueva. Encontrados y corregidos dos bugs reales probándolo:

1. **`setlocal enabledelayedexpansion` (copiado del patrón viejo) hacía perder
   cualquier `!` dentro de `MqttPassword`/`ComandoHmacSecret` al leer `config.txt`**
   — silencioso, sin error. El script no usa `!variable!` en ningún lado, así que la
   delayed expansion no hacía falta; sacarla lo resuelve. Las contraseñas que genera
   este proyecto sí pueden traer `!` (ver la del `svc_saidsoft` de más arriba en este
   mismo documento), así que era un bug real, no teórico.
2. **Una tilde o raya larga en un comentario `rem` rompe el parseo de `cmd.exe`** —
   problema de codepage: el archivo se escribe en UTF-8 pero `cmd.exe` lo lee con la
   página de códigos OEM/ANSI activa, y los bytes multibyte de un caracter acentuado
   corrompen el `rem` de esa línea (y a veces de líneas vecinas), haciendo que
   fragmentos del comentario se intenten ejecutar como comandos — aunque el script
   igual termina funcionando bien después. Corregido quitando toda tilde/raya larga de
   `Instalar.bat` (queda en ASCII puro). Verificado reproduciendo el problema aislado
   (una línea con "—" o con "ñ" alcanza para romperlo) y confirmando que, sin
   caracteres no-ASCII, el archivo corre limpio de punta a punta con un
   `instalar-servicio.ps1` de mentira que solo imprime los parámetros recibidos
   (incluyendo secretos con `!`/`$`/`#`, que llegaron intactos).

**N. Exposición accidental de `MQTT_PASSWORD_AGENTE`/`COMANDO_HMAC_SECRET` en un
commit público (11-ago-2026) — 🟢 rotados:** al completar el `config.ejemplo.txt` de
la plantilla del paquete de un clic (§10-M), quedaron pegados ahí los valores reales
en vez de en `config.txt` (que sí está en `.gitignore`) — y como
`saidsoft-core` es un repo **público**, el commit los dejó visibles en el historial de
GitHub. Corregido el archivo de inmediato, pero como el historial de un repo público
no se puede considerar "borrado" con reescribirlo (GitHub cachea, puede haber clones),
se rotaron los dos secretos de verdad: valores nuevos generados, aplicados en
`deploy/.env`, en EMQX (contraseña del usuario `saidsof_agente`) y reinstalados en
las dos estaciones activas (ML016-A, ML006-A). De paso se encontró y corrigió que
`bootstrap-emqx.sh` tampoco sabía *actualizar* la contraseña de un usuario MQTT ya
existente (el `POST` de creación con `409 ALREADY_EXISTS` no hacía nada más) — ahora
hace `PUT` a `/users/{user_id}` en ese caso. **Lección**: `config.ejemplo.txt` es la
plantilla versionada — nunca debe llevar valores reales, ni siquiera "para probar
rápido"; los reales van solo en `config.txt`, gitignoreado.

**O. Comentarios `{# ... #}` multilínea se renderizaban como texto visible en el panel
(11-ago-2026) — 🟢 corregido:** el usuario reportó ver texto de comentarios de
desarrollo (explicaciones de por qué existe tal `hx-preserve` o tal pausa de polling)
como contenido visible arriba de la tabla de `/estaciones/` y en el modal de info de
estación. El código fuente parecía correcto a simple vista (`{# comentario #}`, la
sintaxis válida de Django) y el archivo desplegado en el contenedor coincidía exacto
con el repo — descartando problemas de despliegue. La causa real: **Django no permite
que un comentario `{# #}` ocupe más de una línea** (documentado, fácil de no saber);
si lo hace, el parser no lo reconoce como comentario y el texto completo —incluidos
los delimitadores— se imprime tal cual. Los seis casos del proyecto que usaban esta
sintaxis multilínea (introducidos en distintos commits a lo largo del desarrollo,
todos con la misma buena intención de documentar el "por qué" de un workaround)
quedaron convertidos a `{% comment %}...{% endcomment %}`, que sí soporta varias
líneas. Verificado con un test que confirma que el texto ya no aparece en
`/estaciones/`, más la suite completa. **A tener en cuenta a futuro**: cualquier
comentario Django de más de una línea debe ir en `{% comment %}`, nunca en `{# #}`.

**P. Formularios de destino (despliegue/solicitud de instalación/promoción de anillo)
rediseñados con checklist real + mostrar/ocultar por tipo de destino (11-ago-2026) —
🟢 cerrado:** el usuario reportó que `/solicitudes-instalacion/nueva/` (y por
extensión los otros dos formularios con el mismo patrón de destino) eran "muy
básicos": al elegir "Toda la cadena" seguían mostrándose los campos de
grupos/farmacias/estaciones (sin sentido, ya que ese destino no los usa), y elegir
farmacias o estaciones específicas usaba un `<select multiple>` nativo (mala
usabilidad con listas largas, sin buscador). Se aplicó el mismo arreglo a los tres
formularios que comparten el patrón `destino_tipo` + `grupos`/`farmacias`/
`estaciones`:
- `DespliegueForm`, `SolicitudInstalacionForm` (`apps/software/forms.py`) y
  `PromoverDespliegueForm` — los tres widgets pasaron de `SelectMultiple`/
  `forms.SelectMultiple(size=6)` a `forms.CheckboxSelectMultiple`.
- Plantillas dedicadas (`despliegue_form.html`, `solicitud_instalacion_form.html`
  nueva, `despliegue_promover_form.html` nueva) con JS que oculta/muestra el bloque
  de grupos/farmacias/estaciones según `destino_tipo` (nada se muestra para
  "cadena"), y un buscador de texto por checklist (`data-checklist-buscar` filtra las
  `<label>` del checklist por coincidencia de texto).
- De paso, `activo_crear` (`apps/panel/views/activos.py`) y
  `solicitud_instalacion_crear` dejaron de usar el `accion_form.html` genérico y
  pasaron a plantillas propias; `activo_form.html` nueva además solo muestra
  Procesador/RAM/Almacenamiento cuando `tipo` es de cómputo (`LAP`/`DSK`/`SRV`/`TAB`),
  corrigiendo que esos campos aparecieran también para Impresora/UPS/etc.
- Verificado con la suite completa de `apps.panel`/`apps.despliegues`/`apps.software`
  y, para cada formulario, una captura headless (Edge `--headless --screenshot`) del
  HTML renderizado vía el `Client` de pruebas (login real + vista real, no
  `render_to_string` suelto — este último no pasa por `AuthenticationMiddleware` y
  termina renderizando el `{% block content_anon %}` vacío de `base.html`).

**Q. El "bug de caché de Docker" que quedó pendiente de §10-O/P en realidad eran dos
binarios `docker compose` distintos peleándose por el mismo `docker-compose.yml`
(11-ago-2026) — 🟢 causa raíz encontrada y documentada:** tras el fix de §P, un rebuild
en el NUC de producción parecía no tomar el código nuevo (`docker exec deploy_web_1
grep ...` seguía dando 0 con el commit correcto ya en el checkout del host). Se
descartó despliegue incompleto (`git log`/`git status` limpios) y se armó un
diagnóstico: `docker images` mostró **dos familias de imágenes para los mismos
servicios** — `deploy_web`/`deploy_worker`/... (guion bajo, 11-ago, recién construidas)
y `deploy-web`/`deploy-worker`/... (guion, 6-ago, viejas). Causa real: el NUC tiene
instalados **tanto `docker-compose` v1 (el binario legado, sin espacio) como el plugin
`docker compose` v2 (con espacio)** — `deploy/README-produccion.md` decía que v2 "no
está instalado por default" (cierto cuando se escribió esa nota, dejó de serlo en algún
momento) y mezclaba ejemplos de ambos comandos en distintas secciones. Los contenedores
que sirven tráfico real (`deploy_web_1`, con el puerto 8080 publicado) los administra
v1; en algún momento se corrió un `docker compose build`/`up` (v2) siguiendo la sección
de instalación inicial del README, que construyó/gestionó un stack paralelo con
namespace de imagen y contenedor distinto (`deploy-web-1`, guion) — completamente
invisible para quien mira `deploy_web_1`. No era caché stale, eran dos stacks
coexistiendo. Fix: `deploy/README-produccion.md` reescrito para usar `docker-compose`
(v1, sin espacio) de forma consistente en **todos** los comandos de ejemplo, con una
sección nueva "NUNCA usar `docker compose` v2" explicando el porqué. **Pendiente de
limpieza no urgente**: borrar las imágenes `deploy-*` (guion) huérfanas del NUC con
`docker rmi` para que dejen de aparecer en `docker images` y confundir a futuro.

**Diferido a propósito (diseño v1, no deuda):** sync de RRHH (`SyncEjecucion`,
`Colaborador.origen_sync` — esquema listo, sin conector porque no hay sistema de RRHH
definido todavía), verificación automática de cumplimiento (v1 es atestación manual
por decisión, ver `apps/cumplimiento/services.py`), sincronización automática
MeshCentral↔panel (vínculo manual de `node_id`), temperatura de CPU en monitoreo
(siempre `null`, sin sensor confiable disponible).
