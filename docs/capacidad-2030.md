# ¿Aguanta SAIDSOFT 1.300 farmacias? Diagnóstico de capacidad

**Fecha:** 27-sep-2026
**Escenario de planificación:** ~1.300 farmacias para 2030.
**Alcance:** solo diagnóstico. No se desarrolló, modificó ni configuró nada, y **no se
ejecutó ninguna prueba de carga.**
**Estado del repo al auditar:** `HEAD = 0034f18`, rama `master`.

> **Documento hermano de** [`auditoria-capacidades.md`](auditoria-capacidades.md) (qué
> tiene hoy) y [`prioridad-2030.md`](prioridad-2030.md) (qué atender primero).

---

## Cómo leer este documento

**Producción estaba inalcanzable cuando se escribió este documento** (`10.111.6.20` sin
responder por SSH ni HTTPS), así que el análisis original salió del código.

> **Actualización del 28-sep-2026:** ya hubo acceso al servidor y se midió. Los valores
> reales están en [Medición en producción](#medición-en-producción--28-sep-2026), al
> final, y varios `NO DETERMINADO` de abajo quedaron resueltos.

Cada afirmación lleva una etiqueta literal:

| Etiqueta | Significa |
|---|---|
| **HECHO** | Evidencia directa en código, configuración o documentación del repo |
| **INFERENCIA** | Conclusión deducida de la arquitectura encontrada |
| **ESTIMACIÓN** | Cálculo teórico para evaluar el escenario. **No es una medición** |
| **NO DETERMINADO** | No hay información suficiente para concluirlo |

---

## 1. Resumen ejecutivo

1. **El problema no es de software, es de infraestructura.** El código está construido y
   probado; lo que no escala es el lugar donde corre.

2. **Todo corre en un único Intel NUC, en sitio.** *(HECHO)* Once contenedores —base de
   datos, broker, web, workers, MeshCentral— en una sola máquina de escritorio
   (`glpi-NUC11TNKv5`). No hay redundancia de ningún componente.

3. **Ese NUC sale a la red por WiFi.** *(HECHO)* Documentado en `CLAUDE.md`: interfaz
   `wlo1` con ruta por defecto, sin VPN dedicada. Es el enlace por el que pasarían las
   ~26.000 conexiones TLS del escenario más alto.

4. **EMQX es de nodo único y por decisión explícita.** *(HECHO)*
   `EMQX_NODE__NAME: emqx@127.0.0.1`, comentado como "nodo único, sin cluster". Escalar
   horizontalmente el broker no es cambiar un número.

5. **El worker MQTT es de un solo hilo y procesa en línea.** *(HECHO)* `loop_forever()`
   de paho, con el manejo dentro del callback. Es el único oído de toda la flota y no
   reparte trabajo.

6. **No hay un solo límite de recursos declarado.** *(HECHO)* Ni `ulimits`, ni
   `mem_limit`, ni `deploy.resources`, ni réplicas en todo el `docker-compose.yml`.

7. **El punto de partida es 700 farmacias, no cero.** *(HECHO)* SG 393, MIA 305, 7DIAS 2.
   Llegar a 1.300 es multiplicar por 1,86 lo ya modelado — pero por ~160 lo efectivamente
   desplegado, que eran 8 equipos con agente al 7-sep-2026.

8. **El volumen de datos es manejable; el de conexiones no.** *(ESTIMACIÓN)* A 20
   endpoints por farmacia, la telemetría residente ronda las decenas de GB gracias a la
   retención de 30 días. El problema son las ~26.000 sesiones TLS permanentes contra un
   broker de un nodo.

9. **La retención de 30 días es la mejor decisión de capacidad ya tomada.** *(HECHO)* Tres
   hypertables con política de retención acotan lo que más crece. Lo que no tiene límite
   es `media/`: fotos y PDFs de mantenimiento.

10. **El respaldo existe y la restauración nunca se probó.** *(HECHO)* Diario, cifrado, 14
    días locales — y `BACKUP_OFFSITE_DESTINO` sin definir, o sea copia única en la misma
    máquina que se quiere proteger.

---

## 2. Arquitectura actual

Explicado simple: **un agente en cada PC de farmacia habla por MQTT cifrado con un único
servidor, que guarda todo en una base de datos con series de tiempo y lo muestra en un
panel web.** Alrededor hay tareas programadas, una app móvil para técnicos y una
herramienta de escritorio remoto.

```text
   ~1.800 estaciones objetivo (8 con agente al 7-sep-2026)
        │  MQTT sobre TLS, comandos firmados con HMAC
        ▼
   ┌──────────────────────── UN SOLO NUC, POR WiFi ────────────────────────┐
   │                                                                        │
   │   emqx  ──────►  worker  ─────────┐        nginx ──► web (gunicorn x3) │
   │   1 nodo         1 hilo           │                      │             │
   │   TLS 8883       loop_forever()   ▼                      ▼             │
   │                              ┌─────────────────────────────┐           │
   │   celery_worker (x2) ───────►│   db — TimescaleDB / PG16   │           │
   │   celery_beat (23 tareas) ──►│   97 tablas, 3 hypertables  │           │
   │   redis (cola + beat)        │   retención 30 días         │           │
   │                              └─────────────────────────────┘           │
   │   meshcentral + meshcentral_worker      telegram_bot                   │
   └────────────────────────────────────────────────────────────────────────┘
        │  SNMP                    │  HTTPS                │  webhook
        ▼                          ▼                       ▼
   MikroTik de cada farmacia   app móvil Flutter      Teams · Telegram · correo
```

### Cómo se comunica cada cosa *(HECHO)*

| Camino | Protocolo | Quién inicia | Evidencia |
|---|---|---|---|
| Agente ↔ servidor | MQTT sobre TLS 8883 (publicado como 8081) | El agente | `docker-compose.yml:86` |
| Servidor → MikroTik | SNMP | Tarea Celery | `monitoreo/mikrotik.py` |
| Servidor → MeshCentral | WebSocket de control | Worker dedicado | `adapters/meshcentral.py` |
| App móvil → servidor | HTTPS, token DRF | La app | `api_urls.py` |
| Sonda externa → servidor | HTTPS con token de permiso único | La sonda | `deploy/sonda-enlaces/` |
| Servidor → avisos | SMTP, webhook Teams, Telegram | El servidor | `CanalNotificacion` |

### Frecuencias reales del agente *(HECHO)*

Valores por defecto en `CAMPOS_CONFIG`, configurables por estación:

| Bucle | Intervalo | ¿Siempre publica? |
|---|---|---|
| Heartbeat | 60 s | Sí |
| Métricas (CPU/RAM/disco/red) | 300 s | Solo si `monitorear_recursos` |
| Log del POS | 300 s | Solo si hay POS instalado |
| Servicios del POS | 300 s | Solo si hay POS instalado |
| Eventos de Windows | 900 s | Solo si detecta alguno vigilado |
| Chequeo de reloj | 3.600 s | Solo si corrige |

---

## 3. Carga teórica por escenario *(ESTIMACIÓN)*

**Supuestos declarados**, tomados de los intervalos reales del agente y llevados al peor
caso: todo endpoint tiene POS y monitoreo de recursos activo, así que publica heartbeat
cada 60 s más cuatro reportes cada 300 s y uno cada 900 s. Eso da **≈1,67 mensajes por
minuto por endpoint**. En la práctica será menor: los bucles de POS y de eventos solo
publican cuando corresponde.

| Escenario | Endpoints | Conexiones MQTT | Mensajes/s | Filas métricas/día | Residentes (30 d) | Telemetría en disco |
|---|---|---|---|---|---|---|
| **Hoy** (objetivo 1.800) | 1.800 | ~1.800 | 50 | 518 mil | 15,5 M | ~5 GB |
| **A** · 1.300 × 5 | 6.500 | ~6.500 | 181 | 1,9 M | 56 M | ~15 GB |
| **B** · 1.300 × 10 | 13.000 | ~13.000 | 361 | 3,7 M | 112 M | ~27 GB |
| **C** · 1.300 × 15 | 19.500 | ~19.500 | 542 | 5,6 M | 169 M | ~39 GB |
| **D** · 1.300 × 20 | 26.000 | ~26.000 | 723 | 7,5 M | 225 M | ~50 GB |

Disco estimado a ~200 bytes por fila incluyendo índices, sumando `muestra_red_farmacia`
(sondeo cada 2 minutos por farmacia). **No incluye `media/`, que no tiene retención.**

### La lectura de esos números

**El volumen de datos no es el problema.** Incluso en el escenario D, ~50 GB residentes
son perfectamente manejables para PostgreSQL con TimescaleDB. La retención de 30 días es
lo que lo mantiene acotado, y fue una buena decisión.

**El problema son las conexiones y el procesamiento en serie.** 26.000 sesiones TLS
permanentes contra un broker de un nodo, con todos los mensajes procesados por un solo
hilo a 723 por segundo, sobre una máquina que también corre la base de datos.

---

## 4. Matriz de capacidad por escala

*(INFERENCIA salvo donde se indique. Supone 10 endpoints por farmacia — escenario B — y la
infraestructura actual sin cambios.)*

| Capacidad | Hoy (700) | 500 | 700 | 900 | 1.100 | 1.300 |
|---|---|---|---|---|---|---|
| Conexiones MQTT | Techo cerca | Excede | Excede | Excede | Excede | Excede |
| Worker MQTT (1 hilo) | Holgado | Presión | Presión | Saturado | Saturado | Saturado |
| Base de datos (volumen) | Holgado | Holgado | Holgado | Holgado | Holgado | Holgado |
| Base de datos (escrituras) | Holgado | Presión | Presión | Presión | Crítico | Crítico |
| CPU del host | No determinado | Presión | Crítico | Crítico | Crítico | Crítico |
| RAM del host | No determinado | No det. | No det. | No det. | No det. | No det. |
| Red del host (WiFi) | Presión | Crítico | Crítico | Crítico | Crítico | Crítico |
| Almacenamiento | Holgado | Holgado | Holgado | Vigilar | Vigilar | Vigilar |
| API / panel (gunicorn x3) | Holgado | Holgado | Presión | Presión | Crítico | Crítico |
| Celery (concurrencia 2) | Holgado | Presión | Presión | Crítico | Crítico | Crítico |
| Sondeo SNMP (50 concurrentes) | Holgado | Presión | Presión | Crítico | Crítico | Crítico |
| Respaldo (ventana nocturna) | Holgado | Holgado | Vigilar | Vigilar | Vigilar | Vigilar |
| Dashboards (polling HTMX) | Holgado | Presión | Presión | Presión | Crítico | Crítico |

> **La fila que manda es la primera.** El techo de conexiones se topa *antes* que
> cualquier otra cosa, y ya es un bloqueante conocido en la escala de hoy. El resto de las
> filas solo importa después de resolverla.

### Qué pasa con el broker a cada escala

| Endpoints | Situación conceptual |
|---|---|
| 1.000 | Dentro del techo observado (1024), pero al borde: cualquier reconexión masiva lo cruza |
| 2.000 | Supera el techo. Se resuelve subiendo el límite del listener y el de descriptores de archivo del contenedor — es el bloqueante ya identificado |
| 5.000 | EMQX de un nodo puede sostenerlo *si la máquina acompaña*. Acá el límite pasa a ser RAM y CPU del NUC compartidas con la base |
| 10.000 | *(INFERENCIA)* Un solo nodo ya no es prudente: el broker necesita máquina propia |
| 20.000 | Fuera del alcance de la arquitectura actual sin cluster de broker y separación de servicios |

---

## 5. Límites técnicos identificados

| Límite | Valor | Clasificación | Evidencia |
|---|---|---|---|
| Conexiones del listener TLS de EMQX | 1024 | **HECHO — re-verificado el 28-sep** contra el broker (`emqx ctl listeners`). No figura en el compose: es el default de la imagen | `max_conns: 1024`, 32 conexiones |
| Descriptores de archivo del contenedor EMQX | 1024 | **HECHO — medido el 28-sep.** El techo es doble | `ulimit -n` |
| Nodos del broker | 1 | HECHO | `EMQX_NODE__NAME` |
| Hilos del worker MQTT | 1 | HECHO | `loop_forever()` |
| Workers de gunicorn | 3 | HECHO | `docker-compose.yml:113` |
| Concurrencia de Celery | 2 | HECHO | `docker-compose.yml:425` |
| Sondeos SNMP simultáneos | 25 | HECHO | `_MAX_SONDEOS_CONCURRENTES` |
| Pings simultáneos de la sonda | 50 | HECHO | `MAX_CONCURRENTES` |
| Reutilización de conexión a la base | 60 s | HECHO | `CONN_MAX_AGE` |
| Retención de telemetría | 30 d | HECHO | `DIAS_RETENCION` |
| Retención local de respaldos | 14 d | HECHO | `backup.sh:63` |
| Límites de CPU/RAM por contenedor | ninguno | HECHO | sin `deploy.resources` ni `mem_limit` |
| Descriptores de archivo (`ulimit`) | sin declarar | HECHO | sin `ulimits` en el compose |
| Réplicas de cualquier servicio | 1 | HECHO | sin `deploy.replicas` |
| CPU del NUC | 8 hilos | **HECHO — medido el 28-sep.** Intel i5-1145G7 | `nproc`, `/proc/cpuinfo` |
| RAM del NUC | 15 GiB | **HECHO — medido el 28-sep.** 10 GiB disponibles | `free -h` |
| Disco del NUC | 457 GB | **HECHO — medido el 28-sep.** 171 GB libres (61% usado) | `df -h /` |
| Tamaño actual de la base | 136 MB | **HECHO — medido el 28-sep.** Con 42 estaciones | `pg_database_size` |
| Ancho de banda del enlace del NUC | — | **NO DETERMINADO** — se confirmó que sale por `wlo1` (WiFi), no su capacidad | `ip route` |

---

## 6. Puntos únicos de falla

En la arquitectura actual, **todo componente es un punto único de falla**, porque todos
corren una sola vez en una sola máquina. Ordenados por alcance del daño:

| Componente | Si falla | Alcance |
|---|---|---|
| **El NUC** | Se cae todo: base, broker, panel, workers, escritorio remoto y los respaldos guardados ahí mismo | Toda la cadena |
| **El WiFi del NUC** | Ningún agente reporta y nadie entra al panel. La plataforma está sana y es inalcanzable | Toda la cadena |
| **Base de datos** | Sin réplica ni conmutación. La recuperación depende de un respaldo cuya restauración nunca se probó | Toda la cadena |
| **Broker EMQX** | Nodo único. Además, si cambia el nombre del nodo arranca en blanco y se pierden usuarios y ACLs — ya pasó varias veces | Toda la flota |
| **Worker MQTT** | Único consumidor. El panel sigue andando y deja de entrar información: **falla en silencio** | Toda la flota |
| **ACL de EMQX** | Autoriza por lista blanca y niega sin avisar. El PUBACK confirma recepción, no autorización | Parcial y mudo |
| **Celery Beat** | Instancia única. Si se detiene, dejan de generarse mantenimientos, escaneos, purgas y escalamientos | Procesos diferidos |
| **Respaldo** | Copia única en la misma máquina que protege, sin prueba de restauración | Pérdida total |

---

## 7. Superficies de riesgo de seguridad

Señaladas, no corregidas:

- **Ejecución remota de PowerShell sobre toda la flota.** Bien protegida —firma HMAC,
  aprobación, ventana anti-replay— pero es, por diseño, la capacidad más peligrosa del
  sistema. Quien controle el servidor controla ~1.800 equipos.
- **Claves BitLocker en la base**, custodiadas y auditadas, en la misma máquina que todo
  lo demás.
- **MeshCentral** da escritorio y terminal remotos; es un tercero dentro del mismo host.
- **Dashboard de EMQX expuesto** en un puerto, restringido solo por firewall.
- **Seguridad física no evaluada.** Documentado explícitamente: el NUC está en sitio y
  nunca hubo visita de evaluación.
- **Media sin autenticación** según la documentación de producción.

---

## 8. Almacenamiento y crecimiento

**Lo que está acotado:** las tres hypertables (`muestra_metrica`, `muestra_red_farmacia`,
`evento_monitoreo`) con retención de 30 días, más `muestra_servicio_pos` y
`ubicacion_tecnico` (60 días) por purgas de Celery. **Cinco tablas en total.**

**Lo que crece sin techo** *(HECHO — se verificó que no existe purga ni retención)*:

- `evento_auditoria` — con 133 puntos de registro, es la que más crece de este grupo.
- `alerta`, `evento_activo`, `evento_mantenimiento`, `evento_despliegue`.
- `software_instalado_detectado`, `dispositivo_detectado`.
- `actividad_mensual_estacion` — **a propósito**: es la única fuente para facturar meses
  pasados.
- `media/` — fotos de mantenimiento e informes PDF. Sin política de retención.

---

## 9. Respaldo y recuperación

| | |
|---|---|
| Frecuencia | Diaria, 02:00, vía systemd timer con `Persistent=true` |
| Contenido | `pg_dump` completo + `media/` en tar |
| Cifrado | GPG AES256 |
| Retención local | 14 días |
| Destino externo | `rsync` sobre SSH — **`BACKUP_OFFSITE_DESTINO` sin definir**: el script avisa y sigue |
| Restauración | Script existe (`restaurar-backup.sh`). **Nunca se probó contra hypertables** |
| Disaster recovery | **No existe** procedimiento documentado |

> Antecedente registrado en el repo: durante una auditoría previa se descubrió que **nunca
> existió ningún cron de respaldo** (crontab vacío, sin `.sql.gz` en el filesystem). El
> timer de systemd es la corrección de eso.

---

---

## Medición en producción — 28-sep-2026

**Este bloque reemplaza estimaciones por datos leídos del servidor.** Se corrió una
revisión de solo lectura sobre `10.111.6.20`. Lo que no aparece acá sigue siendo lo que
dice el resto del documento.

| Qué | Valor medido |
|---|---|
| Hardware | Intel i5-1145G7, **8 hilos**; **15 GiB RAM** (10 disponibles); disco 457 GB con **171 GB libres** (61% usado) |
| Interfaz de salida | `wlo1` — **WiFi confirmado** |
| Contenedores | Los 11, `healthy` |
| Tamaño de la base | **136 MB.** La tabla más grande es `farmacia` con 1 MB: casi todo el peso es el catálogo de TimescaleDB, no datos |
| Farmacias activas | **701** |
| Estaciones | **42** (40 aprobadas, 3 nunca reportaron) |
| Agentes vivos | **29 con heartbeat < 15 min**, 30 en 24 h |
| Versiones de agente | 0.32 → 38 · 0.29 → 3 · 0.31 → 1 |
| Activos · colaboradores | 21 · 10 |
| Mantenimientos · visitas | 3 · 1 |
| Alertas (históricas / abiertas) | 184 / 3 |
| **EMQX `max_conns`** | **1024**, con 32 conexiones actuales |
| **EMQX `ulimit -n`** | **1024** — el techo es doble: listener *y* descriptores de archivo |
| `EMAIL_HOST_USER` | **vacía** — confirmado |
| `BACKUP_OFFSITE_DESTINO` | **vacía** — confirmado |
| `ANTHROPIC_API_KEY` | **vacía** — el diagnóstico con IA nunca se ejecutó en producción |

### Lo que cambió respecto de lo estimado

- **La adopción subió**: de 8 agentes al 7-sep a **29 vivos** al 28-sep. Sigue siendo el
  ~1,6% de las ~1.800 estaciones objetivo, pero la tendencia es real.
- **El techo de EMQX quedó re-verificado** y es peor de lo documentado: no es solo
  `max_conns` del listener, también el `ulimit -n` del contenedor, ambos en 1024.
- **El respaldo SÍ corre.** Diario, última ejecución exitosa el 28-sep 08:42, con latido
  registrado en el panel. Escribe en `/home/glpi/backups/saidsoft/`.
- **Pero `pg_dump` avisa que el volcado puede no restaurarse.** En cada corrida emite
  `circular foreign-key constraints` sobre `hypertable`, `chunk` y `continuous_agg` —el
  catálogo de TimescaleDB— con la advertencia *"You might not be able to restore the dump
  without using --disable-triggers"*. **Es evidencia directa de que el riesgo de
  restauración no es teórico**, y sigue sin haber una prueba de restauración.
- **El servidor corre código viejo**: `53f968c`, anterior a la baja de `integraciones`.
  Sus dos tablas siguen existiendo, ambas con **0 filas** — así que la migración `0033`
  va a poder borrarlas sin cortar el deploy.

## 10. Conclusión

> **Parcialmente preparada — y la división es nítida: el software sí, la infraestructura
> no.**

**El modelo de datos y la aplicación soportan 1.300 farmacias sin cambios estructurales.**
*(INFERENCIA)* La jerarquía unidad → grupo → farmacia → estación ya sostiene 700 farmacias
modeladas; nada en ella cambia al duplicarlas. La retención de 30 días mantiene la
telemetría en decenas de GB incluso en el escenario más alto, que PostgreSQL con
TimescaleDB maneja con holgura. El multi-tenant, los permisos, la auditoría y el
despliegue por olas están diseñados para escala.

**La infraestructura no soporta ni la escala de hoy.** *(HECHO)* El techo de conexiones
del broker ya bloquea el despliegue a las ~1.800 estaciones actuales. Y por debajo de ese
bloqueo hay tres decisiones que solo funcionan en escala pequeña: **un único NUC**
corriendo los once contenedores, **un broker de un solo nodo** por decisión explícita, y
**un worker MQTT de un solo hilo** que es el único oído de toda la flota.

Hay una cuarta que conviene no pasar por alto porque no es de software: **ese servidor
sale a la red por WiFi**. En el escenario de 26.000 conexiones TLS permanentes, esa
interfaz es el cuello de botella antes que cualquier línea de código.

### La pregunta que esta auditoría no pudo responder

**Cuánto de lo construido está realmente en uso.** La documentación dice 700 farmacias
modeladas y 8 equipos con agente al 7-sep-2026, con módulos prácticamente vacíos —9
activos, 9 colaboradores, 0 zonas de viáticos—. Si eso sigue así, la brecha de 2030 no es
de arquitectura sino de adopción: *se construye más rápido de lo que se pone en uso*, tal
como ya está anotado en `CLAUDE.md` como el riesgo real del proyecto.

Esa parte queda **NO DETERMINADO** hasta la primera conexión a producción. Sin contar
filas no se sabe si el desafío hacia 1.300 es técnico o es operativo — y la evidencia
disponible apunta más a lo segundo.
