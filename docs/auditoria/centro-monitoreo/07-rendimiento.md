# 07 · Auditoría de rendimiento

[← 06 Diseño propuesto](06-diseno-propuesto.md) · [Índice](README.md) · [Siguiente: Plan de implementación →](08-plan-implementacion.md)

Consultas por carga, hallazgos con evidencia, cadencias de refresco y manejo de errores de
actualización.

---

## 7.1 Consultas por carga del dashboard

Contadas **por lectura de código**, no medidas (ver `L-2`):

| # | Consulta | Dónde | Nota |
|---|---|---|---|
| 1 | `unidades_negocio_visibles` | `cuentas/services.py:182` | Queryset diferido: se convierte en subconsulta dentro de cada filtro `__in=visibles`. No es N+1 |
| 2 | `calcular_matriz_cumplimiento` | `catalogo/services.py:610` | 1 consulta con **5 `Count(distinct=True)`** sobre `Grupo → Farmacia → Estacion` |
| 3 | `total_pendientes.count()` | `dashboard.py:38` | |
| 4 | `total_alertas_abiertas.count()` | `dashboard.py:43` | Usa el índice `(estado, -abierta_en)` ✅ |
| 5 | `WorkerHeartbeat` (worker) | `dashboard.py:57` | |
| 6 | `alertas_recientes` | `dashboard.py:70` | `select_related('regla','estacion','estacion__farmacia')` ✅ |
| 7 | `sin_reportar.count()` | `dashboard.py:97` | **duplica el filtro de #8** |
| 8 | `sin_reportar[:6]` | al evaluarse en la plantilla | |
| 9 | `enlaces_caidos.count()` | `dashboard.py:122` | **duplica el filtro de #10** |
| 10 | `list(enlaces_caidos[:6])` | `dashboard.py:126` | |
| 11 | `WorkerHeartbeat` (respaldo) | `dashboard.py:133` | **podría unirse con #5** |
| 12 | `despliegues_activos` | al evaluarse `|length` en la plantilla | |
| 13-15 | context processors (unidad de negocio, notificaciones) + sesión + usuario | `base.py:88-90` | En **todas** las páginas del panel |

**≈13-15 consultas por carga.**

**No hay N+1.** Los `select_related` están puestos donde hacen falta, y
`nombre_proveedor(evento.farmacia)` solo lee un atributo ya traído — su propio docstring lo
advierte: *"requiere que los eventos vengan con `select_related('farmacia')`; si no, esto
hace una consulta por evento"*. El dashboard cumple.

---

## 7.2 Hallazgos

| Id | Hallazgo | Evidencia | Mejora | Marca |
|---|---|---|---|---|
| `R-1` | **Tres pares count+slice redundantes.** Cada par recorre el mismo filtro dos veces | `dashboard.py:97/101`, `122/126` | `list(qs[:7])` y contar solo si llegan 7, o `COUNT(*) OVER()`. Ahorra 2-3 consultas por carga | CONFIRMADO |
| `R-2` | **Dos consultas a `WorkerHeartbeat`** para dos filas de una tabla de ~3 filas | `dashboard.py:57` y `:133` | Una sola: `{h.nombre: h for h in WorkerHeartbeat.objects.all()}`. **El patrón ya existe** en `verificar_salud.py:122` | CONFIRMADO |
| `R-3` | **Sin índices en `Estacion`** | `Meta` en `catalogo/models.py:533` no declara `indexes`; ninguna migración de `catalogo` con `AddIndex` | Índice `(estado_aprobacion, ultimo_heartbeat)` | CONFIRMADO |
| `R-4` | **5 `Count(DISTINCT)` sobre dos JOIN por carga** en la matriz de cumplimiento | `catalogo/services.py:610-628` | Medir con `EXPLAIN ANALYZE` primero. Si cuesta: plegada y cargada por HTMX **solo al abrirse** — el rediseño ya lo permite | POR VALIDAR |
| `R-5` | **Sin `CACHES` configurado** → LocMemCache por proceso, inútil con 6 contenedores | `grep CACHES config/settings/` sin resultados | `CACHES` con Redis. **No añade dependencia**: `redis>=5.0` ya está en `requirements.txt` | CONFIRMADO |
| `R-6` | **El dashboard no se refresca** → F5 recarga navegación, CSS y todo el contexto | `dashboard.html` sin `hx-trigger` | Patrón marco + parcial del Centro (`monitoreo.py:277`), ya probado | CONFIRMADO |
| `R-7` | **`estacion.save()` sin `update_fields`** en la ruta más caliente (un latido por minuto por estación) | `mqtt_worker/services.py:326` | Hay precedente de `update_fields` tres líneas más abajo (`:379`). Acota cualquier aumento de cadencia | CONFIRMADO |
| `R-8` | **El Centro dispara ~15 consultas por refresco × 60 s × N agentes** con la pantalla abierta todo el día | `centro_monitoreo_partial` | Cachear `resumen_operacion` 30-45 s en Redis | DISEÑO |

### Por qué `R-3` es el índice correcto y no otro

El criterio es el que el propio proyecto documentó al añadir el índice de `Alerta`: *"UN
índice, y sale de las consultas que existen, no de lo que parezca razonable"*.

El patrón real sobre `estacion` es **filtrar por `estado_aprobacion` y acotar por
`ultimo_heartbeat`**, y aparece en las cuatro consultas más repetidas del sistema:

| Consumidor | Frecuencia |
|---|---|
| `resumen_operacion` (Centro + bot) | cada 60 s × agentes con la pantalla abierta |
| `marcar_estaciones_offline` | **cada 60 s, siempre** |
| `dashboard` (`sin_reportar`) | cada carga |
| listado de Estaciones (3 contadores de tarjeta) | cada carga |

A 1.800 estaciones no es dramático, pero la tarea de los 60 segundos lo paga
indefinidamente. Nota de implementación en `X-4`: usar `AddIndexConcurrently`.

### Sobre `R-5` y la caché

Importante para no equivocar el orden: **cachear es lo último, no lo primero**. No tiene
sentido cachear un número cuya definición todavía va a cambiar (Fase B). Y cuando se
cachee, la marca de frescura debe calcularse sobre el **instante del dato**, nunca sobre el
de la caché (`X-7`) — si no, la caché convierte un dato viejo en un dato viejo *disfrazado
de reciente*, que es peor.

---

## 7.3 Frecuencia de actualización por sección

Las cadencias se **derivan del backend**, no se eligen. El criterio ya existe y está
probado en el Centro:

> *"Sesenta es además la cadencia real más rápida del backend: `marcar-estaciones-offline`
> corre cada 60 s y el sondeo de enlaces cada 2 min. Pollear cada 10 s mostraría nueve veces
> el mismo dato."*
> — `apps/panel/views/monitoreo.py`, `SEGUNDOS_REFRESCO_CENTRO`

| Zona | Cadencia | Deriva de |
|---|---|---|
| 0 · Confianza | **30 s** | El umbral del worker es 90 s: a 30 s el aviso llega en ≤ 2 ciclos |
| 1 · Incidencias | **60 s** | Las alertas se abren en tiempo real al ingerir el mensaje MQTT, pero Telegram y correo **ya salieron** en el momento: un minuto de demora en el tablero de triage es aceptable |
| 2 · Indicadores | **60 s** | `marcar-estaciones-offline` = 60 s |
| 3 · Conectividad | **60 s** | Sondeo cada 2 min: a 60 s nunca se pierde un ciclo |
| 4-5 · Enrolamiento, estaciones | **120 s** | No cambian solos |
| 6-7 · Cumplimiento, hardware | **al abrir** | Diarios o cada 15 min |

**Un solo endpoint por zona, no uno por tarjeta.** Tres `hx-get` independientes son tres
respuestas que pueden contradecirse entre sí en la misma pantalla.

### WebSockets: no se justifican

`ASGI_APPLICATION` existe (`config/settings/base.py:103`), así que serían posibles. No se
recomiendan:

| Argumento | Dato |
|---|---|
| El dato más rápido del backend se recalcula cada 60 s | `CELERY_BEAT_SCHEDULE` |
| Lo único en tiempo real son las alertas, que se abren al ingerir el mensaje MQTT | `abrir_o_mantener_alerta` |
| ...y esas **ya salieron** por Telegram y correo en el instante | `encolar_notificacion_alerta` |
| Un canal persistente por agente y por hora no compra nada que el polling no dé | — |

El encargo pide evaluar WebSockets **solo si existe una necesidad real que el polling no
resuelva**. No existe.

---

## 7.4 Manejo de errores de actualización y marcas de tiempo

### Hoy

- **El dashboard no tiene manejo de error de refresco porque no se refresca.**
- El Centro reemplaza el bloque entero con `hx-swap="innerHTML"`. Si la petición falla,
  HTMX **deja el contenido anterior sin marca alguna**: el operador sigue mirando datos
  viejos creyéndolos actuales. Es el mismo defecto que `fuente_desactualizada` resuelve para
  los datos, sin resolver para el transporte.

### Lo que hace falta — NO EXISTE

| Elemento | Comportamiento |
|---|---|
| `hx-on::response-error` por zona | Marca la zona: "sin contacto con el servidor desde HH:MM". La zona **no** se queda mostrando lo anterior como si fuera actual |
| `hx-indicator` discreto | Sin parpadeo. La animación del Centro (`centro-fade`, 0,35 s) ya es la calibración correcta |
| Las demás zonas siguen refrescando | Un fallo en una zona no congela la pantalla entera |

### Los dos relojes

El encargo pide distinguir la hora de actualización de la interfaz de la hora del último
dato recibido. Son **dos relojes que tienen que estar visibles a la vez y no confundirse**:

| Reloj | Qué dice | Hoy |
|---|---|---|
| **"Calculado 14:32:05"** | Cuándo corrió la consulta | El Centro lo tiene (`r.calculado_en`); el dashboard no tiene nada |
| **"Último dato recibido hace 38 s"** | Cuándo llegó el dato a la base | El Centro lo tiene **solo para el sondeo de red**; falta para heartbeats, enlaces y respaldo |

El segundo es el que de verdad importa, y es el que la Zona 0 del diseño propuesto pone en
cada fila.

---

## Lo que hay que decidir en esta parte

1. **¿Se aprueban las cadencias de la tabla 7.3?** Son el contrato de carga del servidor:
   30 s para la Zona 0 es el valor más agresivo de la propuesta.
2. **¿Se autoriza configurar `CACHES` con Redis?** No añade dependencia, pero sí un punto
   de fallo más — mitigable con lectura directa si Redis no responde (`X-7`).
3. **¿Se autoriza medir `R-4` con `EXPLAIN ANALYZE` contra producción?** Es de solo lectura
   y decide si la matriz necesita carga diferida o basta con plegarla.
4. ¿Se acepta que `R-7` (el `save()` sin `update_fields`) quede **fuera** de este trabajo?
   No es del tablero; pertenece a la ruta de ingesta.

---

[← 06 Diseño propuesto](06-diseno-propuesto.md) · [Índice](README.md) · [Siguiente: Plan de implementación →](08-plan-implementacion.md)
