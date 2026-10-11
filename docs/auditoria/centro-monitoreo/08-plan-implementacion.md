# 08 · Plan de implementación

[← 07 Rendimiento](07-rendimiento.md) · [Índice](README.md) · [Siguiente: Riesgos y dependencias →](09-riesgos-dependencias.md)

Cuatro fases. **Ninguna combina un cambio visual grande con uno estructural**, como pide el
encargo. Cada fase termina con una condición explícita para pasar a la siguiente.

---

## Clasificación de los cambios por nivel

### Nivel 1 · Cambios visuales (no alteran lógica de negocio)

Reordenar zonas · aplicar `_estado_chip.html` en el dashboard · plegar la matriz de
cumplimiento · eliminar el KPI duplicado · mover el botón de nuevo despliegue · añadir
etiqueta textual a cada estado.

### Nivel 2 · Mejoras de consultas y contexto (reorganizan información ya disponible)

Umbral único de "en línea" · separar nunca-reportó de dejó-de-reportar · separar alertas sin
reconocer de en atención · unificar la fuente de enlaces caídos · añadir farmacias
vigiladas, pausadas, enrolamientos rechazados, mensajes fallidos, último latido de la flota
y progreso de despliegue · consolidar las dos consultas a `WorkerHeartbeat` · eliminar los
pares count+slice · marco + parcial con refresco y frescura por zona.

### Nivel 3 · Mejoras de observabilidad (necesitan mecanismos nuevos)

`client.is_connected()` en el latido del worker · contador de mensajes procesados ·
comprobación del broker EMQX vía `emqx_admin` · latido propio de Celery Beat · exponer
`verificar_salud` en el panel.

### Nivel 4 · Cambios estructurales

Índice en `Estacion` · `CACHES` con Redis · constraint de unicidad parcial en
`EventoEnlaceFarmacia` · histórico del resumen para tendencias (ya anotado como pendiente en
`PLAN_MODERNIZACION.md` §10-AK).

---

## Fase A · Reorganización visual y la afirmación falsa de la topbar

| | |
|---|---|
| **Nivel** | 1, con el mínimo de 2 necesario para que la topbar diga la verdad |
| **Objetivo** | Que el orden de lectura corresponda al orden de urgencia, y que ninguna pantalla afirme salud no comprobada |
| **Complejidad** | Baja |
| **Prioridad** | **1 — la primera intervención** |

### Alcance

Solo plantillas, más un context processor nuevo para el estado de salud agregado.
**Ningún número cambia de valor en esta fase.**

### Archivos involucrados

- `templates/panel/dashboard.html` — reordenar zonas, chips, plegar matriz
- `templates/panel/base.html` — reemplazar `sys-status` (línea 255)
- `apps/panel/context_processors.py` — nuevo: estado de salud agregado
- `config/settings/base.py` — registrar el context processor
- `static_src/components.css` + `static/css/app.css` — estilos del nuevo indicador de topbar

### Dependencias

Ninguna. Es la única fase que puede empezar hoy.

### Riesgos

**Bajo.** El único real es `X-5`: el context processor corre en **todas** las páginas del
panel. Mitigación obligatoria — una sola consulta a `WorkerHeartbeat` (~3 filas), cacheada
30 s, con una prueba de `assertNumQueries` que lo fije.

### Criterios de aceptación

1. Con el worker sin latido > 90 s, la topbar dice "MONITOREO DEGRADADO" en **todas** las
   pantallas, y en ninguna dice "Todos los sistemas operativos".
2. Un componente sin forma de verificarse aparece como **"sin verificar" en gris**, nunca en
   verde.
3. Enlaces caídos y alertas críticas aparecen **por encima** de la matriz de cumplimiento en
   el orden del HTML.
4. Todo estado lleva palabra, no solo color — comprobable quitando el CSS de color.
5. El KPI "Total de estaciones" ya no existe.
6. **Ningún número cambia de valor respecto de antes de la fase.** Este es el criterio que
   distingue la Fase A de la B.

### Pruebas automatizadas

- Una por cada estado de la topbar: sano / degradado / sin verificar.
- Una de orden de bloques en el HTML renderizado.
- `assertNumQueries` sobre una página cualquiera del panel: el context processor no puede
  costar más de una consulta.

### Validación manual

Con el worker detenido, recorrer 5 pantallas distintas y confirmar que ninguna afirma salud.
Revisar la pantalla en 1920px, 1366px y 768px.

### Condición para continuar

Fase A desplegada y usada **un día** por la mesa de ayuda sin reportes de números cambiados.

---

## Fase B · Corrección y normalización de indicadores

| | |
|---|---|
| **Nivel** | 2 |
| **Objetivo** | Un concepto, un número, en toda la aplicación |
| **Complejidad** | Media-alta |
| **Prioridad** | 2 — y es donde está el valor real del trabajo |

### Alcance

Una función de dominio única para el estado de la flota, consumida por el dashboard, el
Centro y el listado de Estaciones. Umbral único en `apps/monitoreo/umbrales.py`. Separación
de las poblaciones de I-06 y I-09. Fuente única para I-05. Los seis indicadores nuevos. La
cola unificada por farmacia de la Zona 1.

### Archivos involucrados

- `apps/monitoreo/umbrales.py` — el umbral deja de vivir en una vista
- `apps/catalogo/services.py` — `calcular_matriz_cumplimiento`, nueva función de flota
- `apps/monitoreo/services.py` — `resumen_operacion`
- `apps/panel/views/dashboard.py` · `views/monitoreo.py` · `views/estaciones.py`
- `apps/catalogo/tasks.py` — el literal `minutos=5`
- plantillas del dashboard y del Centro

### Dependencias

**Fase A completa.** No mezclar un cambio visual con un cambio de cifras: si los números se
mueven el mismo día que la pantalla, nadie podrá decir qué causó qué.

### Riesgos

**El más alto del plan.** Los números van a cambiar y alguien va a preguntar por qué.

| Riesgo | Mitigación |
|---|---|
| El umbral es una decisión operativa, no técnica | **Decidirlo con el usuario antes de escribir código** — ver [04 · decisión bloqueante](04-riesgos-del-encargo.md#decisión-bloqueante--el-umbral-de-estación-en-línea) |
| Un umbral más laxo **retrasa alertas reales**; uno más estricto genera falsos positivos a escala de flota | Decidir si `estado_conexion` deriva del mismo umbral o conserva el suyo **con nombre propio y motivo escrito** |
| La mesa de ayuda ve otras cifras de un día para otro | Nota de cambio visible en la pantalla el día del despliegue |
| `resumen_operacion` lo consumen el Centro **y el bot de Telegram** | La suite completa contra PostgreSQL; `ResumenOperacionCompartidoTests` ya cubre que el bot use el servicio |

### Criterios de aceptación

1. Dashboard, Centro y listado de Estaciones dan **el mismo número** de estaciones en línea
   para los mismos datos.
2. El denominador son **solo** estaciones `APROBADA`, **incluidas** las de grupo `PENDIENTE`.
3. Administrativas y pausadas se muestran aparte, nunca sumadas en silencio.
4. "Nunca reportó" y "dejó de reportar" tienen contador propio.
5. "Alertas sin reconocer" y "en atención" son dos cifras distintas.
6. "Enlaces caídos" sale de una sola fuente en las tres pantallas.

### Pruebas automatizadas

**El entregable real de la fase es una prueba:** `EquivalenciaIndicadoresTests`, con el
patrón que el proyecto ya usa dos veces —`EquivalenciaPredicadosEstacionTests` en `catalogo`
y `ResumenOperacionCompartidoTests` para el bot—: misma población, tres vistas, mismo
número; falla si difieren.

Más una prueba por cada población de
[04 · B](04-riesgos-del-encargo.md#b--estaciones-sin-reportar).

### Validación manual

Contra producción, comparar las tres pantallas **en el mismo minuto**, antes y después del
cambio. Anotar los valores previos.

### Condición para continuar

La prueba de equivalencia en verde **y** los números revisados con quien opera el sistema.

---

## Fase C · Actualización automática y rendimiento

| | |
|---|---|
| **Nivel** | 2, con un toque de 4 (`CACHES`) |
| **Objetivo** | El tablero se mantiene solo, dice de cuándo es cada dato, y cuesta menos que antes |
| **Complejidad** | Media |
| **Prioridad** | 3 |

### Alcance

Partir la pantalla en marco + parciales por zona (patrón del Centro) · `fuente_desactualizada`
por zona · `CACHES` con Redis · eliminar los pares count+slice (`R-1`) y la doble consulta a
`WorkerHeartbeat` (`R-2`) · cachear `resumen_operacion` (`R-8`) · manejo de error de refresco.

### Archivos involucrados

- `apps/panel/views/dashboard.py` → varias vistas parciales
- `apps/panel/urls.py`
- nuevas plantillas `panel/_zona_*.html`
- `config/settings/base.py` — `CACHES`
- `apps/monitoreo/services.py` — caché de `resumen_operacion`

### Dependencias

**Fase B completa.** No cachear un número cuya definición todavía va a cambiar.

### Riesgos

**Medio.**

| Riesgo | Mitigación |
|---|---|
| Redis caído deja el panel sin responder | `CACHES` con fallo silencioso y lectura directa. Prueba con Redis detenido |
| La caché enmascara un dato viejo | La marca de frescura se calcula sobre el **instante del dato**, nunca sobre el de la caché (`X-7`) |
| Zonas con cadencias distintas muestran estados inconsistentes entre sí | Cada zona imprime **su propia** hora de cálculo |

### Criterios de aceptación

1. Cada zona se refresca a su cadencia y muestra "calculado HH:MM:SS".
2. La Zona 0 muestra la hora del **último dato**, distinta de la de la pantalla.
3. Si un parcial falla, la zona lo dice; **no** se queda mostrando lo anterior como actual.
4. Consultas por refresco de zona **menores** que las de la carga completa actual, medido
   con `assertNumQueries`.
5. Con Redis detenido, el panel sigue respondiendo.

### Pruebas automatizadas

- `assertNumQueries` **por parcial**. Hoy hay exactamente **uno** en todo
  `apps/panel/tests.py` (línea 4535): este es el hueco de cobertura más claro del panel.
- Una prueba de que la cadencia declarada en la plantilla no es menor que la del backend —
  extender `test_el_polling_no_es_mas_rapido_que_el_backend` a cada zona.
- Una de degradación sin caché.

### Validación manual

Dejar la pantalla abierta 8 horas y confirmar que no se degrada. Cortar la red y ver el
aviso. `EXPLAIN ANALYZE` de la matriz sobre producción (`R-4`).

### Condición para continuar

Una jornada completa de uso real sin incidencias de rendimiento.

---

## Fase D · Observabilidad real de la plataforma

| | |
|---|---|
| **Nivel** | 3 y 4 |
| **Objetivo** | Que la Zona 0 afirme solo lo que se puede probar, y que lo hoy improbable pase a ser probable |
| **Complejidad** | Media-alta (es la única fase que toca modelos y migraciones) |
| **Prioridad** | 4 — compite honestamente con el rollout de agentes |

### Alcance, por sub-entrega

| # | Entrega | Nivel |
|---|---|---|
| D1 | `client.is_connected()` + contador de mensajes en el latido del worker | 3 |
| D2 | Comprobación del broker EMQX vía `apps/mqtt_worker/emqx_admin.py` (la integración **ya existe**) | 3 |
| D3 | Latido propio de Celery Beat (hoy se infiere de `MuestraRedFarmacia`, que **también** escribe otra ruta) | 3 |
| D4 | `verificar_salud` consultable desde el panel, **sin perder** su ejecución externa por systemd | 3 |
| D5 | Índice `(estado_aprobacion, ultimo_heartbeat)` en `Estacion` y constraint parcial en `EventoEnlaceFarmacia` | 4 |

### Archivos involucrados

`apps/mqtt_worker/models.py` + migración · `run_mqtt_worker.py` · `emqx_admin.py` ·
`apps/monitoreo/umbrales.py` · `apps/catalogo/models.py` + migración ·
`apps/monitoreo/models.py` + migración · nueva vista y plantilla de salud.

### Dependencias

**Fase A** (la Zona 0 debe existir para recibir esto). No depende de B ni de C.

### Riesgos

**Medio-alto.** Es la única fase que toca persistencia.

| Riesgo | Mitigación |
|---|---|
| `AddIndex` bloquea la tabla y el entrypoint corre las migraciones solo | `AddIndexConcurrently` + `atomic = False`; **verificar el comportamiento del entrypoint antes** (`X-4`) |
| El constraint parcial **falla si ya hay duplicados** | Medir `L-3` primero y cerrarlos en una migración de datos aparte, simulable, nunca desde la migración del constraint (`X-3`) |
| Consultar EMQX en el camino del render añade una llamada de red | Tarea de fondo que **persiste** el resultado; el panel lee la fila (`X-6`) |
| Sustituir `verificar_salud` por una vista web rompería su diseño | Se **expone** el resultado, no se reemplaza el mecanismo. `--solo` debe seguir funcionando para el healthcheck del contenedor |

### Criterios de aceptación

1. Con EMQX detenido y el worker vivo, el panel dice "worker sin conexión al broker" —
   **no** "worker activo".
2. Con el worker vivo y conectado pero sin procesar, se distingue de "procesando".
3. Cada componente de `verificar_salud` tiene su fila en la Zona 0, o está declarado
   explícitamente como "sin verificar".
4. `python manage.py makemigrations --check --dry-run` sin drift.

### Pruebas automatizadas

- Una por combinación proceso / conexión / procesamiento (8 teóricas; bastan las 4 reales).
- Una que el constraint rechace un segundo evento abierto para la misma farmacia.
- Una que `verificar_salud --solo mqtt_worker` siga funcionando igual.

### Validación manual

Detener EMQX en el entorno local **con el mismo motor que producción** y confirmar los tres
estados. **No ensayar esto contra el broker de producción.**

---

## Resumen del plan

| Fase | Nivel | Complejidad | Cambia números | Toca modelos | Prioridad |
|---|---|---|---|---|---|
| **A** · Visual + topbar | 1 (+2 mínimo) | Baja | **No** | No | **1** |
| **B** · Indicadores | 2 | Media-alta | **Sí** | No | 2 |
| **C** · Refresco y rendimiento | 2 (+4) | Media | No | No | 3 |
| **D** · Observabilidad | 3 y 4 | Media-alta | No | **Sí** | 4 |

**Dependencias:** A → B → C. D depende solo de A y puede ir en paralelo a B o C si hay dos
personas.

**Verificación al cerrar cada fase** (de `CLAUDE.md`):

```
python manage.py check
python manage.py makemigrations --check --dry-run
python manage.py test          # suite completa, contra PostgreSQL, no SQLite
```

---

## Lo que hay que decidir en esta parte

1. **¿Qué fases se aprueban?** La recomendación es aprobar A de inmediato y B en cuanto esté
   decidido el umbral.
2. **¿Fase D entra en este trabajo o compite con el rollout?** `CLAUDE.md` es explícito:
   *"la plataforma está construida; el despliegue no"* — 66 de ~1.800 equipos. Un tablero
   mejor no instala agentes.
3. ¿Hay una persona o dos? Determina si D va en paralelo.

---

[← 07 Rendimiento](07-rendimiento.md) · [Índice](README.md) · [Siguiente: Riesgos y dependencias →](09-riesgos-dependencias.md)
