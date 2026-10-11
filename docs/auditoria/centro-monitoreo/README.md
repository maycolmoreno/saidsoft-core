# Auditoría del tablero de operación — Centro de Monitoreo

Auditoría técnica y funcional del dashboard (`/`) y del Centro de Monitoreo (`/centro/`),
con propuesta de rediseño. **Solo lectura: no se modificó código, ni modelos, ni
migraciones, ni datos, ni configuración.** Los únicos archivos creados son los de esta
carpeta.

| | |
|---|---|
| **Fecha** | 10-oct-2026 |
| **Línea base al empezar** | `master` @ `5cfbf03`, con cuatro archivos sin commitear |
| **Línea base al terminar** | `master` @ `083c3d1` — esos cambios se commitearon durante la auditoría, en `38da5e9` y `083c3d1` |
| **Verificación ejecutada** | `python manage.py check` → *System check identified no issues (1 silenced)* |
| **Pruebas ejecutadas** | ninguna (exigen PostgreSQL/TimescaleDB en Docker; ver [límites](09-riesgos-dependencias.md#l--límites-de-esta-auditoría)) |
| **Acceso a producción** | ninguno |

> **Sobre los números de línea citados.** Salen del árbol de trabajo tal como se leyó,
> y la línea base se movió mientras se escribía el informe. Los dos commits que entraron
> tocaron `apps/panel/views/reportes.py`, `templates/panel/reportes_index.html`,
> `templates/panel/activo_detalle.html` y `apps/panel/tests.py`: **ninguno sostiene ningún
> hallazgo de este informe**, salvo por los números de línea de `tests.py`, que se corrieron
> unas +69 líneas a partir de la 1692. Para ese archivo conviene buscar por **nombre de
> clase** (`DashboardTableroTests`, `CentroMonitoreoTests`, …), que es estable. Los demás
> archivos citados no fueron tocados y sus líneas coinciden con `083c3d1`.

---

## Cómo revisar esto por partes

Trece partes, más este índice. Cada parte se lee sola y termina con **lo que hay que decidir** en esa parte.
Tres recorridos posibles, según el tiempo disponible:

### Recorrido corto (15 min) — para decidir si vale la pena
1. [01 · Resumen ejecutivo](01-resumen-ejecutivo.md)
2. [13 · Qué se implementó](13-implementacion.md) — el estado de cada hallazgo hoy
3. [12 · Índice de hallazgos](12-indice-hallazgos.md) — la tabla con los 15 hallazgos confirmados

> **Las cuatro fases ya están implementadas** (11-oct-2026). El informe se conserva tal
> como se entregó, con su diagnóstico en presente, porque es la evidencia que sostiene cada
> cambio; lo que cambió desde entonces está en la parte 13 y en `PLAN_MODERNIZACION.md`
> §10-BI.

### Recorrido de revisión (1-2 h) — para validar los hallazgos
En orden: 01 → 02 → 03 → 04 → 05 → 06 → 07 → 09 → 11. Cada hallazgo cita archivo y línea;
la revisión consiste en abrir esa línea y confirmar o refutar.

### Recorrido de planificación — para ordenar el trabajo
[08 · Plan de implementación](08-plan-implementacion.md) y
[09 · Riesgos y dependencias](09-riesgos-dependencias.md), con
[10 · Pruebas de aceptación](10-pruebas-aceptacion.md) como criterio de cierre de cada fase.

---

## Índice

| # | Parte | Qué contiene | Decisión que pide |
|---|---|---|---|
| 01 | [Resumen ejecutivo](01-resumen-ejecutivo.md) | Los dos tableros solapados y las cinco mejoras de mayor impacto | Si se consolidan los dos tableros o se mantienen separados |
| 02 | [Arquitectura encontrada](02-arquitectura-encontrada.md) | Flujo real agente → MQTT → worker → BD → vista → plantilla; conectividad; salud; mapa de umbrales | Ninguna (es la base de evidencia) |
| 03 | [Auditoría de indicadores](03-auditoria-indicadores.md) | Los 21 indicadores con origen, regla de cálculo, fiabilidad, límite y mejora | Cuáles se mantienen, cambian o se eliminan |
| 04 | [Los seis riesgos del encargo](04-riesgos-del-encargo.md) | A estaciones en línea · B sin reportar · C enlaces · D alertas · E salud · F despliegues | **El umbral de "estación en línea"** |
| 05 | [Auditoría UX/UI](05-auditoria-ux.md) | Qué dificulta la lectura (U-1…U-10) y qué no debe tocarse | Si se acepta el reordenamiento de zonas |
| 06 | [Diseño propuesto](06-diseno-propuesto.md) | Esquema textual de las 8 zonas, orden de lectura, inventario del cambio, resoluciones | Si el esquema propuesto es el correcto |
| 07 | [Auditoría de rendimiento](07-rendimiento.md) | Consultas por carga, R-1…R-8, cadencias por zona, manejo de errores | Cadencias de refresco; si se configura caché |
| 08 | [Plan de implementación](08-plan-implementacion.md) | Fases A-D con alcance, archivos, riesgos, aceptación, pruebas y condición de avance | Qué fases se aprueban y en qué orden |
| 09 | [Riesgos y dependencias](09-riesgos-dependencias.md) | X-1…X-10 y los siete límites de esta auditoría | Cómo se mitiga X-1 y X-2 |
| 10 | [Pruebas de aceptación](10-pruebas-aceptacion.md) | Los 13 escenarios del encargo con su estado actual y criterio | Ninguna (es el criterio de cierre) |
| 11 | [Recomendación final](11-recomendacion-final.md) | Cuál es la primera intervención y por qué | **Autorización para empezar** |
| 12 | [Índice de hallazgos](12-indice-hallazgos.md) | Tabla única D/U/R/X/L con severidad, evidencia y estado de revisión | Confirmar o refutar cada hallazgo |
| 13 | [Qué se implementó](13-implementacion.md) | Estado de cada hallazgo tras las cuatro fases, lo que se descartó con su motivo, y qué mirar al desplegar | Ninguna (es el registro de lo hecho) |

---

## Clasificación usada en todo el informe

Cada afirmación lleva una de estas cuatro marcas, como pidió el encargo:

| Marca | Significado |
|---|---|
| **CONFIRMADO** | La evidencia en el código es inequívoca por sí sola. Se cita archivo y línea. No significa que se haya ejecutado nada |
| **POR VALIDAR** | Problema potencial cuya magnitud o existencia real exige medir en producción o ejecutar algo. Cada uno tiene su consulta o procedimiento en la parte 09 |
| **DISEÑO** | Recomendación de diseño. Es una opinión argumentada, no un hallazgo |
| **NO EXISTE** | Funcionalidad que se propone y hoy no está implementada. Nunca se presenta como existente |

Las familias de identificadores:

| Prefijo | Familia | Dónde |
|---|---|---|
| `D-xx` | Confiabilidad del dato | partes 03, 04, 12 |
| `I-xx` | Indicador auditado | parte 03 |
| `U-x` | Lectura de la pantalla | parte 05 |
| `R-x` | Rendimiento | parte 07 |
| `X-x` | Riesgo de implementación | parte 09 |
| `L-x` | Límite de esta auditoría | parte 09 |

---

## Regla que gobierna este informe

Ningún hallazgo se da por cierto por inferencia. Donde no se pudo comprobar algo, el
informe dice qué falta y cómo se comprueba, en vez de rellenarlo. No se inventaron campos,
modelos, rutas, endpoints, umbrales ni porcentajes.

**Siguiente paso:** leer la parte 01 y la 11, y decidir sobre el umbral de la parte 04.
