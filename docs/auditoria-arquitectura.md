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
