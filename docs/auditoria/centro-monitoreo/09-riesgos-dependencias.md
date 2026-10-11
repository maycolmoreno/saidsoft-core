# 09 · Riesgos y dependencias

[← 08 Plan de implementación](08-plan-implementacion.md) · [Índice](README.md) · [Siguiente: Pruebas de aceptación →](10-pruebas-aceptacion.md)

Los riesgos de implementar lo propuesto, y los límites de esta auditoría con el
procedimiento exacto para cerrar cada uno.

---

## X · Riesgos de implementación

| Id | Riesgo | Nivel | Fase | Mitigación |
|---|---|---|---|---|
| `X-1` | **Cambiar el umbral de "en línea" mueve números que alguien usa.** Si se unifica hacia 10 min, `marcar_estaciones_offline` **retrasa 5 minutos la apertura** de las alertas `sin_heartbeat`; hacia 5 min, el Centro empieza a declarar caídas estaciones que hoy muestra en línea | **Alto** | B | **Es una decisión del usuario, no del rediseño.** Separar explícitamente "umbral de visualización" de "umbral de alerta" si no deben coincidir, y escribir el motivo de cada uno |
| `X-2` | **Degradación por permisos.** `/centro/` exige `monitoreo.view_alerta`; `/` no exige ninguno. Unificar en `/` puede exponer incidencias a quien hoy no las ve, o cerrar la portada a quien sí la ve | **Alto** | A, B | Degradar **por bloque** con `{% if perms %}`, nunca elevando el requisito de la portada. El patrón ya existe en el bloque de cierres en conflicto del Centro |
| `X-3` | **Constraint parcial sobre datos existentes.** `UniqueConstraint(farmacia, condition=fin__isnull=True)` **falla la migración** si hoy hay duplicados | **Medio** | D5 | Medir `L-3` antes. Cerrar los duplicados en una **migración de datos aparte**, simulable (patrón `--aplicar` del proyecto), nunca desde la migración del constraint |
| `X-4` | **Índice en producción.** `AddIndex` bloquea la tabla, y el entrypoint corre las migraciones solo en cada despliegue | **Medio** | D5 | `AddIndexConcurrently` + `atomic = False`. **Verificar antes** cómo se comporta el entrypoint con una migración no atómica |
| `X-5` | **El context processor de salud corre en todas las páginas del panel**, no solo en el tablero | **Medio** | A | Una sola consulta a `WorkerHeartbeat` (~3 filas), cacheada 30 s, con `assertNumQueries` que lo fije |
| `X-6` | **Consultar EMQX en el camino del render** mete una llamada de red en cada carga | **Medio** | D2 | Tarea de fondo que persiste el resultado; el panel lee la fila. Nunca en línea |
| `X-7` | **Cachear puede ocultar que el dato está viejo** | **Medio** | C | La frescura se calcula sobre el **instante del dato**, nunca sobre el de la caché. Es la diferencia entre un dato viejo y un dato viejo disfrazado de reciente |
| `X-8` | **Fusionar los tableros puede borrar decisiones bien argumentadas.** El dashboard ordena los enlaces por **antigüedad** (para decidir a qué ir primero) y el Centro **por proveedor** (porque así se abre el reclamo: uno por proveedor, no uno por farmacia). Están documentadas las dos | **Medio** | A, B | **Conservar ambas vistas del mismo dato** en zonas distintas, no elegir una. Son dos usos reales |
| `X-9` | **CI y suite.** 2.119 pruebas sobre TimescaleDB. La Fase B toca servicios que comparten el panel, el bot de Telegram y `verificar_salud` | **Medio** | B, C, D | Suite completa contra PostgreSQL en cada fase, **no SQLite** (`CLAUDE.md`: 125 pruebas que pasaban en SQLite fallaban en PostgreSQL). Y recordar que **una prueba que lee algo ignorado por git no puede pasar en CI** |
| `X-10` | **El rediseño no resuelve el cuello de botella real.** `CLAUDE.md` es explícito: *"la plataforma está construida; el despliegue no"* — 66 equipos con agente de ~1.800 estimados | **Informativo** | todas | Acotar la inversión: A y B son días; C y D deben competir en prioridad con el rollout. Un tablero mejor no instala agentes |

### Dependencias entre cambios

```
Fase A  (visual + topbar)
  |
  +--> Fase B  (indicadores)  ---> Fase C  (refresco + rendimiento)
  |
  +--> Fase D  (observabilidad)        [puede ir en paralelo a B o C]

Decision del usuario sobre el umbral  ---> bloquea Fase B
Medicion de L-3                        ---> bloquea Fase D5
Verificacion del entrypoint            ---> bloquea Fase D5
```

### Cambios que requieren modificar lógica o arquitectura

Respuesta directa a lo que pide el encargo en su §10:

| Cambio | ¿Solo plantillas? | Qué más toca |
|---|---|---|
| Reordenar zonas, chips, plegar matriz | **Sí** | — |
| Topbar con estado real | No | Context processor + settings |
| Eliminar KPI duplicados | **Sí** | — |
| Umbral único de "en línea" | No | `umbrales.py`, 2 servicios, 3 vistas, 1 tarea |
| Separar poblaciones (I-06, I-09) | No | Vistas y plantillas; **sin** cambio de modelos |
| Fuente única de enlaces caídos | No | Vista del Centro; **sin** cambio de modelos |
| Indicadores nuevos I-18 a I-21 | No | Vistas; los datos ya existen |
| Progreso de despliegue en el tablero | No | Vista: `Count` condicional, reusando `progreso.py` |
| Refresco por zona | No | Vistas nuevas + urls + plantillas |
| Caché | No | **Settings** (nuevo `CACHES`) |
| Estado real del worker / EMQX / Beat | No | **Modelos + migraciones** y el bucle del worker |
| Índice y constraint | No | **Modelos + migraciones** |

Nada de lo propuesto exige cambiar la arquitectura: no se sustituye Django, no se introduce
un framework de frontend, no se añade una dependencia nueva, no se elimina ninguna
funcionalidad y no se toca el flujo de ingesta MQTT ni el de sondeo.

---

## L · Límites de esta auditoría

Lo que **no** se pudo comprobar, y el procedimiento exacto para cerrarlo. Todas las
consultas son de **solo lectura**.

| Id | Qué falta | Cómo se comprueba | Bloquea |
|---|---|---|---|
| `L-1` | Cuántas estaciones quedan fuera del denominador por el grupo `PENDIENTE`. La base local tiene 2 farmacias y 9 estaciones: no es representativa. `CLAUDE.md` menciona 208 farmacias | Consulta 1, abajo | Dimensionar `D-02` |
| `L-2` | El número real de consultas por carga y su coste | `assertNumQueries` en una prueba nueva, o `EXPLAIN ANALYZE` sobre el volumen real | `R-4` |
| `L-3` | Si hay eventos de enlace abiertos duplicados hoy | Consulta 2, abajo | **Fase D5** (`X-3`) |
| `L-4` | Si el worker MQTT ha estado "verde sin procesar" alguna vez | Cruzar `WorkerHeartbeat.ultimo_latido` con el histograma de `Estacion.ultimo_heartbeat` y los logs del worker | Priorizar D1 |
| `L-5` | **Si alguien usa hoy el dashboard o todos han migrado al Centro** | Preguntar a la mesa de ayuda | **Cambia el orden del plan**: si ya usan el Centro, la Fase A se aplica allí primero |
| `L-6` | Latencia real del panel en producción (NUC con 6 contenedores) | Medir sobre `https://10.111.6.20:8084/` | Dimensionar la Fase C |
| `L-7` | Estado del broker EMQX y si su API de administración responde desde el contenedor web | Requiere credenciales de producción, que **no están en el repo** y no se piden en una auditoría de lectura | Fase D2 |

### Consultas de verificación (solo lectura)

**Consulta 1 — `L-1`: población excluida del denominador**

```sql
-- Estaciones que el KPI del dashboard NO cuenta, por el grupo PENDIENTE
SELECT count(*) AS estaciones_invisibles
FROM estacion e
JOIN farmacia f ON e.farmacia_id = f.id
JOIN grupo g    ON f.grupo_id    = g.id
WHERE g.codigo = 'PENDIENTE';

-- Y las que el denominador SUMA sin ser flota operativa
SELECT estado_aprobacion, count(*)
FROM estacion
GROUP BY estado_aprobacion;
```

**Consulta 2 — `L-3`: eventos de enlace abiertos duplicados**

```sql
-- Tiene que devolver 0 filas antes de intentar el constraint de la Fase D5
SELECT farmacia_id, count(*) AS abiertos
FROM evento_enlace_farmacia
WHERE fin IS NULL
GROUP BY farmacia_id
HAVING count(*) > 1;
```

**Consulta 3 — contexto general, para dimensionar el rediseño**

```sql
SELECT
  (SELECT count(*) FROM farmacia WHERE activa) AS farmacias_activas,
  (SELECT count(*) FROM farmacia WHERE activa AND ip_router IS NOT NULL) AS farmacias_vigiladas,
  (SELECT count(*) FROM estacion WHERE estado_aprobacion = 'aprobada') AS aprobadas,
  (SELECT count(*) FROM estacion WHERE pausado) AS pausadas,
  (SELECT count(*) FROM mensaje_mqtt_fallido WHERE NOT revisado) AS mqtt_fallidos,
  (SELECT count(*) FROM enrolamiento_rechazado WHERE NOT revisado) AS enrolamientos_rechazados,
  (SELECT max(ultimo_heartbeat) FROM estacion) AS ultimo_latido_flota;
```

La consulta 3 es, en sí misma, una demostración de lo que falta en el tablero: cuatro de sus
siete columnas no existen hoy en ninguna pantalla.

---

## Lo que hay que decidir en esta parte

1. **¿Cómo se resuelve `X-1`?** Es la decisión bloqueante del plan. Las tres opciones están
   en [04 · decisión bloqueante](04-riesgos-del-encargo.md#decisión-bloqueante--el-umbral-de-estación-en-línea).
2. **¿Cómo se resuelve `X-2`?** Confirmar que degradar por bloque es aceptable, o decidir
   qué permiso debe exigir la portada.
3. **¿Se autorizan las tres consultas de verificación** contra producción? Son `SELECT`
   puros, sin escritura.
4. **`L-5`: ¿la mesa de ayuda usa hoy `/` o `/centro/`?** Es la pregunta de una sola línea
   que más puede cambiar el orden del plan.

---

[← 08 Plan de implementación](08-plan-implementacion.md) · [Índice](README.md) · [Siguiente: Pruebas de aceptación →](10-pruebas-aceptacion.md)
