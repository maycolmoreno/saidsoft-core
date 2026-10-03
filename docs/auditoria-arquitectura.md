# Auditoría de arquitectura del panel

**Fecha:** 12-sep-2026
**Alcance:** todo el proyecto, no solo el módulo de enlaces.
**Regla de la auditoría:** solo lectura. Los cambios vinieron después y por separado, con
aprobación explícita de cada prioridad.

---

## 1. Resumen

16 apps Django, **86 modelos**, **174 vistas de panel + 14 endpoints DRF**, **156 URLs**.

El hallazgo principal es que **la mayoría de las redundancias aparentes son deliberadas y
están documentadas en los propios docstrings**. El patrón `Resultado* + Evento* inmutable`
aparece 12 veces y `DestinoTipo` está declarado 4 veces, pero la resolución del destino
**sí** está centralizada en `apps.catalogo.services.resolver_estaciones`. Son enums por
modelo que Django necesita para las migraciones, no copy-paste.

La redundancia real y no intencional resultó ser menor y acotada.

**Se descartó una hipótesis de partida:** no hay pantallas que dupliquen los conteos de
enlaces. El dashboard no muestra ninguno, y `EstadoEnlaceFarmacia` se consulta solo desde
`apps/panel/views/monitoreo.py`, `apps/monitoreo/enlaces.py` y su admin. El riesgo de "dos
pantallas con el mismo número calculado distinto" no existía ahí — sí existía en otro lado
(ver §4).

## 2. Inventario de vistas

| Módulo | Vistas | Líneas | HTMX |
|---|---|---|---|
| `activos.py` | 28 | 829 | partial |
| `mantenimiento.py` | 24 | 558 | partial |
| `estaciones.py` | 19 | 397 | modal + polling |
| `viaticos.py` | 15 | 313 | partial |
| `reportes.py` | 14 | 275 | no |
| `monitoreo.py` | 13 | 505 | modal + polling |
| `scripts.py` | 12 | 214 | polling |
| `aperturas.py` | 11 | 279 | polling |
| `software.py` | 10 | 200 | polling |
| `despliegues.py` | 9 | 224 | polling |
| `alertas.py` | 7 | 158 | no |
| `cumplimiento.py` | 5 | 99 | no |
| `mfa` / `dashboard` / `tenant` / `auditoria` | 7 | 231 | no |

**API DRF** (`/api/v1/`): 12 endpoints de `mantenimiento` (app móvil Flutter) + 2 de
`monitoreo` (ingesta de sondeos de enlace).

## 3. Datos compartidos entre módulos

```
FARMACIA  (el eje real del sistema)
├── monitoreo.py      enlaces y ancho de banda
├── activos.py        ubicación de activos
├── cumplimiento.py   objetivos por farmacia
├── viaticos.py       zonas y visitas
├── aperturas.py      vía Apertura.farmacia
└── panel/forms.py    selectores de destino

ESTACION
├── estaciones · monitoreo · despliegues
├── scripts · software · aperturas
└── mqtt_worker (escribe, 12 handlers)

UNIDAD DE NEGOCIO (tenant)
└── 13 de 17 módulos de vistas + 8 admins
```

## 4. Lógica duplicada encontrada

### 4.1 Umbrales de color de CPU/RAM/disco — **corregido**

Los seis umbrales estaban escritos como literales en las **dos** vistas que los usan
(`monitoreo_lista` y `monitoreo_detalle_partial`):

```python
_clasificar(ultima.cpu_carga_pct if ultima else None, 75, 90)
```

Ajustar uno y olvidar el otro pintaba **la misma estación de un color en la lista y de
otro en su ficha**, sin que nada fallara: ambas vistas seguían devolviendo 200 y la suite
no lo notaba. Es la única divergencia de este tipo que la auditoría encontró.

Resuelto con `estados_de_recursos(muestra)`, que devuelve los tres colores juntos. Se
devuelven juntos y no de a uno a propósito: el problema no era el valor de cada umbral
sino que existieran dos caminos para llegar al color.

### 4.2 Aritmética de progreso repetida — **corregido, y la auditoría la contó mal**

`despliegues.py:110-115` y `software.py:163-168` compartían el patrón
`total = count()` → `conteo = {estado: 0}` → bucle → porcentaje.

**Corrección de esta auditoría:** se reportó como "tres veces" incluyendo `scripts.py`.
Es falso. Esa vista no calcula progreso: muestra un estado general y la salida de cada
estación, sin barra ni desglose. Forzarla al mismo molde habría inventado una
duplicación que no existía. `apps/aperturas` tampoco encaja: su avance son pasos de
distinta naturaleza sobre estaciones distintas, no "N de M".

Resuelto con `apps/panel/progreso.py` + `templates/panel/_barra_progreso.html`, que
comparten las dos vistas que sí lo necesitaban.

**Hallazgo secundario, más grave que la duplicación:** ese cálculo **no tenía ninguna
prueba**. El refactor se hizo sin red, y las 343 pruebas que pasaban no verificaban un
solo porcentaje. Ahora hay 6, incluidos los casos que un cálculo ingenuo se come: envío
sin resultados (división por cero) y rollback contando como error — una estación que
volvió atrás quedó sin la versión nueva, aunque el agente no haya roto el POS.

### 4.3 `DestinoTipo` ×4 y `Resultado*`/`Evento*` ×12 — **no tocar**

Redundancia aparente, no real. Cada módulo tiene su propio ciclo de vida y la lógica
compartida ya está centralizada. Unificarlos obligaría a migraciones en 4 apps para no
ganar nada.

## 5. Consultas pesadas

| Ubicación | Patrón | Estado |
|---|---|---|
| `monitoreo_lista` | `estacion.metricas.first()` en bucle | **corregido** — de 1 consulta por servidor a 15 constantes |
| `estaciones_lista` | `[e for e in estaciones if e.desactualizada]` | **corregido** — pasó a filtro de base |
| `enlaces_farmacias_lista` | `farmacia.muestras_red.first()` en bucle | corregido antes de la auditoría (Subquery) |
| `tendencia_flota` | 3 `.count()` × 12 semanas = 36 consultas | pendiente, impacto medio |
| `mantenimiento.py:187` | `ActividadChecklist.objects` en bucle | pendiente, impacto medio |

## 6. Riesgo en producción

**🔴 Alto** — no tocar sin plan:
`apps/mqtt_worker/services.py` (12 handlers, escribe en 6 apps, único canal con las
estaciones) · `apps/cuentas/services.py` (scoping de tenant: 13 módulos + 8 admins; un
error expone datos entre clientes) · `apps/catalogo/models.Estacion` · las **14 tareas de
Celery Beat**.

**🟡 Medio:**
`resolver_estaciones` (4 módulos) · `apps/monitoreo/enlaces.py` (corre cada 2 min sobre
700 sitios) · `registrar_evento` de auditoría · `templates/panel/base.html`.

**🟢 Bajo:**
Vistas de listado, reportes CSV, templates de progreso, admin.

## 7. Problemas de arquitectura pendientes

1. **`activos.py`: 829 líneas y 28 vistas** en un solo archivo. Candidato natural a
   dividir (bodega / órdenes / activos / colaboradores).
2. **`monitoreo.py` mezcla tres dominios**: métricas de estación, enlaces por farmacia y
   ventanas de mantenimiento. Ciclos de vida distintos.
3. **Filtros implementados distinto en cada pantalla.** No hay patrón compartido; la
   paginación sí se unificó en `apps/panel/paginacion.py` al aparecer la segunda vista.

## 8. Qué se corrigió y qué no

| Prioridad | Qué | Estado |
|---|---|---|
| 1 | Umbrales de CPU/RAM/disco en un solo cálculo | hecho (`75854c6`) |
| 2 | Paginar `estaciones_lista` + N+1 de `monitoreo_lista` | hecho |
| 3 | Unificar la barra de progreso (×2, no ×3) | hecho |
| — | Dividir `activos.py`, separar dominios de `monitoreo.py` | pendiente |

La prioridad 2 se hizo **antes** del rollout del agente a propósito: hoy son 8 estaciones
y no duele, pero el parque son ~1.800. Paginar una lista de 8 filas es barato; hacerlo
cuando ya hay 1.800 en producción, no.

---

## 9. Addendum: modularidad y ciclo de vida de una estación

**Fecha:** 29-sep-2026
**Alcance:** clasificación basada en imports, modelos, servicios y rutas del código; no
incluye mediciones de producción ni propone una migración.

### Conclusión arquitectónica

SaidSoft es un **monolito modular Django con procesos de ejecución especializados**. No
es correcto describirlo como un único proceso: web, worker MQTT, Celery y otros workers
pueden correr por separado (ver `deploy/docker-compose.yml` y
`apps/mqtt_worker/management/commands/run_mqtt_worker.py`). Sin embargo, comparten el
mismo proyecto Django, configuración, registro de apps y base de datos; no son servicios
con propiedad de datos o contratos independientes. `INSTALLED_APPS` está centralizado en
`config/settings/base.py`, la base predeterminada es `DATABASES['default']` y las rutas
web/API se montan desde `config/urls.py`.

Por tanto, **“monolito modular” describe la organización del código**, mientras que los
procesos separados describen cómo se ejecutan algunas responsabilidades. Las apps son
fronteras de paquetes y vocabulario de dominio, pero no constituyen por sí solas
fronteras de despliegue ni de datos.

### Núcleo y dependencias observadas

| App | Responsabilidad real observada | Dependencias que la conectan al resto |
|---|---|---|
| `catalogo` | Identidad de tenant, grupo, farmacia y estación; servicios para comandos MQTT y datos sensibles | Núcleo referenciado por monitoreo, enrolamiento, despliegues, activos, aperturas, facturación, scripts y panel |
| `mqtt_worker` | Entrada MQTT y coordinación de cambios de estado | Escribe en `catalogo`, `monitoreo`, `despliegues`, `software` y `facturacion`; delega reglas a servicios de esas apps |
| `monitoreo` | Muestras, estado de dispositivos, reglas y ciclo de alertas | Depende de `catalogo`; puede abrir mantenimiento; agenda diagnóstico asíncrono con Celery |
| `cuentas` | Identidad de usuario, permisos y alcance por unidad de negocio | Su perfil enlaza `auth.User`, `activos.Colaborador` y `catalogo.UnidadNegocio`; sus servicios de scope son usados por el panel y dominios tenant-aware |
| `panel` | Superficie web y coordinación de operaciones | Sus vistas importan directamente modelos, formularios y servicios de numerosos dominios; no es una capa de presentación completamente desacoplada |
| `despliegues` / `software` / `scripts` | Solicitudes de acción y resultados por estación | Comparten `Estacion`; el worker procesa respuestas y persiste resultados/eventos |
| `mantenimiento` | Órdenes y operación de técnicos | Se integra con alertas y activos; panel y API móvil delegan en `mantenimiento.services` |
| `aperturas` | Alta cero-touch y pasos de instalación | Se invoca desde enrolamiento y coordina el aprovisionamiento de una estación nueva |
| `facturacion` | Actividad mensual de endpoints | Consulta `catalogo`; `mqtt_worker` registra actividad desde heartbeat y confirmación de despliegue |
| `activos`, `cumplimiento`, `viaticos` | ITAM, iniciativas de cumplimiento y gastos de campo | Reutilizan entidades compartidas de farmacia, unidad de negocio, estación, colaborador y usuario |
| `auditoria` | Registro transversal de acciones | Lo llaman las superficies que realizan acciones; no es un límite aislado de ejecución |

La tabla resume cruces comprobados, no un inventario exhaustivo de cada import. Los
hubs más claros son `catalogo` (modelo compartido), `mqtt_worker` (integración de agentes)
y `panel` (coordinación de operaciones); `cuentas` es transversal para el aislamiento por
tenant.

### Recorrido verificable de una estación

1. **Primer contacto:** `apps.mqtt_worker.services.manejar_enrolamiento` recibe el código
    e identidad reportada. Si la estación existe, comprueba el `hardware_id` para el
    re-enrolamiento. Para una nueva, valida que el alta esté habilitada, intenta consumir
    un token de `apps.aperturas` y, si no aplica, resuelve la farmacia desde el código.
    Sin farmacia, rechaza y registra el intento en la bandeja de triage. En el alta manual
    crea `catalogo.Estacion` pendiente de aprobación.
2. **Respuesta al agente:** `_respuesta_aceptado` devuelve identidad/configuración,
    credenciales MQTT cuando están disponibles y el secreto HMAC. La confirmación del
    secreto propio se basa en lo reportado por el agente; no se infiere solo por versión.
3. **Aprobación y operación:** el panel ofrece aprobar/rechazar estaciones. En
    `manejar_heartbeat`, el worker valida código y token, ignora estaciones no aprobadas,
    persiste el estado y el último heartbeat, registra actividad facturable y actualiza
    `EstadoDispositivo`; también evalúa reglas concretas de reloj. Otros handlers MQTT
    procesan métricas, eventos, instalaciones, despliegues e información del equipo.
4. **Monitoreo y alerta:** `apps.monitoreo.services` centraliza evaluación, apertura y
    resolución de alertas. `abrir_o_mantener_alerta` evita duplicar alertas activas,
    respeta ventanas de mantenimiento, encola notificación y puede solicitar diagnóstico
    Celery o abrir mantenimiento cuando la regla lo indica. Las tareas periódicas cubren
    verificaciones que no ocurren en cada mensaje.
5. **Acción remota y resultado:** `apps.catalogo.services.enviar_comando` y
    `enviar_script` publican al tópico MQTT individual con payload HMAC. Los comandos
    puntuales usan `retain=False`: que el broker acepte la publicación confirma envío al
    broker, **no** recepción ni ejecución por la estación. El resultado depende del
    mensaje de respuesta del agente y de su handler en `mqtt_worker` (por ejemplo,
    despliegue, instalación o script).

### Tenancy, seguridad y superficies de entrada

- El eje relacional de tenant es `UnidadNegocio -> Farmacia -> Estacion`. El panel y la
   API de sondeos aplican funciones de alcance de `apps.cuentas.services`; no debe
   confundirse autenticación con autorización tenant: la API de monitoreo comprueba
   además un permiso específico y limita las farmacias visibles antes de escribir.
- Hay varias superficies, pero no varios sistemas de dominio: panel HTMX, API DRF móvil,
   API DRF de sondeos, MQTT y tareas Celery terminan leyendo/escribiendo los mismos
   modelos.
- La API móvil de mantenimiento declara que delega en los mismos servicios que el panel;
   eso es una reutilización real de reglas de negocio, aunque ambos adaptadores HTTP
   tengan autenticación y serialización propias.
- Los comandos MQTT están firmados con HMAC; el secreto por estación se usa solo cuando
   el agente confirmó que lo tiene. MQTT distingue publicar el comando de recibir una
   confirmación de ejecución.

### Clasificación de modularidad

| Clasificación | Apps / corte | Evidencia y matiz |
|---|---|---|
| **Más modular** | `facturacion`, `viaticos`, `cumplimiento` | Tienen modelos/servicios reconocibles y reglas locales; siguen dependiendo de modelos compartidos de catálogo, usuario o activos |
| **Acoplado** | `despliegues`, `software`, `mantenimiento`, `activos`, `aperturas` | Dominios identificables, pero conectados directamente a estación/farmacia/colaborador y a flujos de otros módulos |
| **Entrelazado / transversal** | `catalogo`, `mqtt_worker`, `monitoreo`, `cuentas`, `panel`, `auditoria` | Son hubs de datos, integración, seguridad o coordinación; los cambios pueden propagarse a varias áreas |

“Más modular” no significa autónomo: ninguna de estas apps observadas tiene por sí sola
base de datos, autenticación o despliegue independientes. Tampoco se concluye aquí que
haya ciclos de imports estáticos; el acoplamiento comprobado es principalmente por
imports directos, relaciones entre modelos y llamadas de servicio.

### Archivos de referencia

- `config/settings/base.py` y `config/urls.py`: registro compartido, base y composición
   de rutas.
- `apps/catalogo/models.py`, `apps/catalogo/services.py`: entidades centrales y comandos
   MQTT firmados.
- `apps/mqtt_worker/services.py`: enrolamiento, heartbeat y procesamiento de respuestas.
- `apps/monitoreo/models.py`, `apps/monitoreo/services.py`, `apps/monitoreo/tasks.py`:
   persistencia, evaluación y procesamiento periódico/asíncrono.
- `apps/cuentas/models.py`, `apps/cuentas/services.py`: perfil y alcance tenant.
- `apps/mantenimiento/api_views.py`, `apps/mantenimiento/services.py`: adaptador móvil y
   lógica de mantenimiento reutilizada.
- `apps/monitoreo/api_views.py`: permisos y alcance en la ingesta de sondeos.
- `deploy/docker-compose.yml`: procesos desplegados; contrasta con la separación lógica
   de las apps.

**Límite de esta conclusión:** es una auditoría estática del código disponible a
29-sep-2026. No verifica configuración efectiva ni conectividad de producción. Para
confirmar el recorrido MQTT exacto por tópico hasta cada handler hace falta cotejar el
despachador del worker y el contrato/versionado del agente; este addendum no atribuye ese
detalle a los handlers por nombre solamente.
