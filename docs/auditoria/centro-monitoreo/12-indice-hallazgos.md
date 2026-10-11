# 12 · Índice de hallazgos

[← 11 Recomendación final](11-recomendacion-final.md) · [Índice](README.md)

Tabla única de todo lo encontrado, con la evidencia que lo sostiene. **Pensada para revisar
uno por uno**: abrir el archivo en la línea citada y marcar la última columna.

Para marcar: `[ ]` sin revisar · `[x]` confirmado · `[!]` refutado · `[?]` necesita más
datos. Si se refuta alguno, anotar debajo por qué — el informe debe quedar corregido, no
discutido aparte.

---

## D · Confiabilidad del dato (15 hallazgos)

| Id | Hallazgo | Evidencia | Severidad | Fase | Revisado |
|---|---|---|---|---|---|
| `D-01` | **Tres criterios distintos de "estación en línea"** en tres pantallas (5 min / 10 min / `estado_conexion`) | `panel/views/dashboard.py:23` · `monitoreo/services.py:1401` · `panel/views/estaciones.py:105` | **Alta** | B | `[ ]` |
| `D-02` | **El denominador del KPI principal excluye el grupo `PENDIENTE`** y no filtra por `estado_aprobacion` | `catalogo/services.py:610-628` · `dashboard.py:50-51` | **Alta** | B | `[ ]` |
| `D-03` | **"Alertas abiertas" incluye reconocidas** en el dashboard y no en el Centro: las dos cifras no cuadran | `dashboard.py:43` vs `monitoreo/services.py:1465` | **Alta** | B | `[ ]` |
| `D-04` | **Dos fuentes para "enlaces caídos"** (evento vs. estado) y ningún constraint que impida dos eventos abiertos | `dashboard.py:122` · `monitoreo/services.py:1483` · `monitoreo/models.py` Meta de `EventoEnlaceFarmacia` | **Media** | B, D5 | `[ ]` |
| `D-05` | **El latido del worker MQTT no prueba conexión al broker ni procesamiento**: un hilo lo escribe mientras el proceso viva | `mqtt_worker/management/commands/run_mqtt_worker.py:107-109` | **Alta** | D1 | `[ ]` |
| `D-06` | **La topbar afirma "Todos los sistemas operativos" con LED verde fijo**, en todas las pantallas | `templates/panel/base.html:255` · `static_src/components.css:149` | **Alta** | **A** | `[ ]` |
| `D-07` | **El dashboard no marca el dato viejo**: no consume `fuente_desactualizada()` ni muestra hora de cálculo | `dashboard.py` (ausencia) vs `monitoreo.py:383` | **Alta** | A, C | `[ ]` |
| `D-08` | **"Sin reportar" mezcla dos poblaciones** en el encabezado y en el total: nunca reportó vs. dejó de reportar | `dashboard.py:85-101` · `dashboard.html` | **Media** | B | `[ ]` |
| `D-09` | **Las estaciones pausadas cuentan como en línea** en los tres contadores, sin que nada lo indique | `catalogo/models.py` campo `pausado` · los tres contadores de `D-01` | **Media** | B | `[ ]` |
| `D-10` | **`panel:enrolamientos_rechazados_lista` es una URL huérfana**: ninguna plantilla la enlaza | `panel/urls.py:92` · `grep` sin resultados en `templates/` | **Media** | A | `[ ]` |
| `D-11` | **`MensajeMqttFallido` no aparece en ninguna pantalla del panel**, solo en el admin de Django | `mqtt_worker/models.py:25` · `grep` sin resultados en `apps/panel/` ni `templates/` | **Media** | A | `[ ]` |
| `D-12` | **El broker EMQX no se comprueba en ningún sitio**, aunque su API de administración está integrada | `verificar_salud.py` (ausencia) · `mqtt_worker/emqx_admin.py` (existe) | **Media** | D2 | `[ ]` |
| `D-13` | **Los despliegues del tablero no muestran progreso ni errores**, aunque `progreso.py` ya los calcula | `dashboard.html` bloque de despliegues · `panel/progreso.py:29` | **Media** | B | `[ ]` |
| `D-14` | **No hay prueba de equivalencia** entre las tres pantallas para "estaciones en línea", aunque el patrón existe dos veces | `apps/panel/tests.py` (ausencia) · `catalogo` `EquivalenciaPredicadosEstacionTests` | **Media** | B | `[ ]` |
| `D-15` | **El dashboard no usa `_estado_chip.html`**: comunica estado solo con color, mientras el Centro sí lo usa | `dashboard.html` (`pill`) vs `centro_monitoreo_partial.html` | **Media** | **A** | `[ ]` |

---

## U · Lectura de la pantalla (10 hallazgos)

| Id | Hallazgo | Evidencia | Severidad | Fase | Revisado |
|---|---|---|---|---|---|
| `U-1` | La afirmación falsa permanente de la topbar (= `D-06`) | `base.html:255` | **Alta** | **A** | `[ ]` |
| `U-2` | Jerarquía invertida: la matriz de cumplimiento va primero y a todo el ancho | `dashboard.html` | **Alta** | **A** | `[ ]` |
| `U-3` | Franja de 5 KPI con 3 datos y `tone-*` fijo que no depende del valor | `dashboard.html` | Media | **A** | `[ ]` |
| `U-4` | Color como único canal de estado (= `D-15`) | `dashboard.html` | Media | **A** | `[ ]` |
| `U-5` | Sin hora del dato ni auto-refresco (= `D-07`) | `dashboard.html` | **Alta** | A, C | `[ ]` |
| `U-6` | Sin distinción entre "está bien" y "no se sabe" | `dashboard.py` | **Alta** | A, C | `[ ]` |
| `U-7` | Dos tableros compitiendo, con criterios distintos para el mismo dato | `dashboard.html` · `centro_monitoreo_partial.html` | Media | A, B | `[ ]` |
| `U-8` | Listas sin el campo que permite priorizar | `dashboard.html` | Baja | B | `[ ]` |
| `U-9` | La acción primaria del tablero es publicar software | `dashboard.html:6` | Baja | **A** | `[ ]` |
| `U-10` | Ancho dedicado a lo menos urgente; huecos con 5 tarjetas en 1600px | `dashboard.html` · `components.css:127` | Baja | **A** | `[ ]` |

---

## R · Rendimiento (8 hallazgos)

| Id | Hallazgo | Evidencia | Severidad | Fase | Revisado |
|---|---|---|---|---|---|
| `R-1` | Tres pares count+slice redundantes: cada uno recorre el mismo filtro dos veces | `dashboard.py:97/101`, `122/126` | Media | C | `[ ]` |
| `R-2` | Dos consultas a `WorkerHeartbeat` para dos filas de una tabla de ~3 | `dashboard.py:57` y `:133` | Baja | C | `[ ]` |
| `R-3` | Sin índices en `Estacion`, pese a que el patrón `(estado_aprobacion, ultimo_heartbeat)` es el más repetido del sistema | `catalogo/models.py:533` Meta sin `indexes`; ninguna migración con `AddIndex` | Media | D5 | `[ ]` |
| `R-4` | 5 `Count(DISTINCT)` sobre dos JOIN por carga en la matriz de cumplimiento | `catalogo/services.py:610-628` | **POR VALIDAR** | C | `[ ]` |
| `R-5` | Sin `CACHES` configurado: LocMemCache por proceso, inútil con 6 contenedores | `grep CACHES config/settings/` sin resultados | Media | C | `[ ]` |
| `R-6` | El dashboard no se refresca: F5 recarga navegación, CSS y todo el contexto | `dashboard.html` sin `hx-trigger` | Media | C | `[ ]` |
| `R-7` | `estacion.save()` sin `update_fields` en la ruta más caliente | `mqtt_worker/services.py:326` | Baja (fuera de alcance) | — | `[ ]` |
| `R-8` | El Centro dispara ~15 consultas por refresco × 60 s × N agentes | `centro_monitoreo_partial` | Media | C | `[ ]` |

---

## X · Riesgos de implementación (10)

Detalle completo en [09 · Riesgos](09-riesgos-dependencias.md#x--riesgos-de-implementación).

| Id | Riesgo | Nivel | Decidido |
|---|---|---|---|
| `X-1` | El umbral de "en línea" mueve números y afecta a la apertura de alertas | **Alto** | `[ ]` |
| `X-2` | Degradación por permisos al unificar los tableros | **Alto** | `[ ]` |
| `X-3` | El constraint parcial falla si ya hay duplicados | Medio | `[ ]` |
| `X-4` | `AddIndex` bloquea la tabla; el entrypoint migra solo | Medio | `[ ]` |
| `X-5` | El context processor de salud corre en todas las páginas | Medio | `[ ]` |
| `X-6` | Consultar EMQX en el camino del render | Medio | `[ ]` |
| `X-7` | Cachear puede ocultar que el dato está viejo | Medio | `[ ]` |
| `X-8` | Fusionar puede borrar decisiones bien argumentadas (orden por antigüedad vs. por proveedor) | Medio | `[ ]` |
| `X-9` | Suite de 2.119 pruebas; la Fase B toca servicios compartidos con el bot | Medio | `[ ]` |
| `X-10` | El rediseño no resuelve el cuello de botella real (66 de ~1.800 agentes) | Informativo | `[ ]` |

---

## L · Límites de esta auditoría (7)

Consultas de verificación en [09 · Límites](09-riesgos-dependencias.md#l--límites-de-esta-auditoría).

| Id | Qué falta | Bloquea | Medido |
|---|---|---|---|
| `L-1` | Estaciones fuera del denominador por grupo `PENDIENTE` | Dimensionar `D-02` | `[ ]` |
| `L-2` | Número real de consultas por carga y su coste | `R-4` | `[ ]` |
| `L-3` | Si hay eventos de enlace abiertos duplicados hoy | **Fase D5** | `[ ]` |
| `L-4` | Si el worker ha estado "verde sin procesar" alguna vez | Priorizar D1 | `[ ]` |
| `L-5` | Si la mesa de ayuda usa `/` o `/centro/` | **El orden del plan** | `[ ]` |
| `L-6` | Latencia real del panel en producción | Dimensionar Fase C | `[ ]` |
| `L-7` | Estado de EMQX y si su API responde desde el contenedor web | Fase D2 | `[ ]` |

---

## Resumen por severidad

| Severidad | Cuántos | Cuáles |
|---|---|---|
| **Alta** | 7 | `D-01` `D-02` `D-03` `D-05` `D-06` `D-07` + `U-2` |
| **Media** | 13 | `D-04` `D-08` `D-09` `D-10` `D-11` `D-12` `D-13` `D-14` `D-15` · `R-1` `R-3` `R-5` `R-6` `R-8` |
| **Baja** | 5 | `U-8` `U-9` `U-10` · `R-2` `R-7` |
| **Por validar** | 1 + 7 límites | `R-4` · `L-1` a `L-7` |

**De los 7 de severidad alta, 3 se resuelven en la Fase A** (`D-06`, `D-07` parcial, `U-2`)
y **4 en la Fase B** (`D-01`, `D-02`, `D-03`, y `D-05` en la D).

---

## Qué hacer con esta tabla

1. Revisar los 7 de severidad **Alta** primero: son los que sostienen la recomendación.
2. Si alguno se refuta, **corregir el informe** en la parte correspondiente y recalcular el
   resumen de arriba.
3. Marcar las decisiones de `X-1` y `X-2`, que son las que bloquean la Fase B.
4. Autorizar (o no) las tres consultas de solo lectura de la parte 09 para cerrar `L-1`,
   `L-3` y el contexto general.

---

[← 11 Recomendación final](11-recomendacion-final.md) · [Índice](README.md)
