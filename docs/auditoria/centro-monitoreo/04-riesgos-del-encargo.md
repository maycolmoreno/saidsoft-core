# 04 · Los seis riesgos del encargo

[← 03 Indicadores](03-auditoria-indicadores.md) · [Índice](README.md) · [Siguiente: Auditoría UX/UI →](05-auditoria-ux.md)

El encargo pedía investigar seis riesgos concretos. Esta es la respuesta a cada uno con la
evidencia encontrada. **Es la parte más densa del informe y la que contiene la única
decisión que bloquea el trabajo** (el umbral de "en línea", en el apartado A).

---

## A · Estaciones en línea

### Qué se considera "una estación en línea"

**Tres respuestas distintas, todas vivas en producción** — `D-01` CONFIRMADO:

```python
# 1) apps/panel/views/dashboard.py:23
ONLINE_UMBRAL_MINUTOS = 5
#    -> calcular_matriz_cumplimiento(visibles, umbral_online_minutos=5)
#    -> ultimo_heartbeat >= now-5min

# 2) apps/monitoreo/services.py:1401-1406
TOLERANCIA_FRESCURA_MINUTOS = {'estaciones': 10, ...}
#    -> resumen_operacion() -> Centro de Monitoreo y bot de Telegram
#    -> ultimo_heartbeat >= now-10min

# 3) apps/panel/views/estaciones.py:105
'online': base.filter(estado_conexion=Estacion.EstadoConexion.ONLINE).count()
#    -> campo PERSISTIDO, escrito por marcar_estaciones_offline(minutos=5) cada 60 s
```

**Consecuencia concreta:** una estación cuyo último latido fue hace **7 minutos** está, en
el mismo instante:

| Pantalla | Estado que muestra |
|---|---|
| `/` Dashboard | **caída** (pasó los 5 min) |
| `/centro/` Centro | **en línea** (no llega a los 10 min) |
| `/estaciones/` Listado | **caída** (la tarea ya la pasó a `OFFLINE`) |

### Las seis preguntas del encargo

| Pregunta | Respuesta | Marca |
|---|---|---|
| ¿Qué se considera en línea? | Tres criterios, dos valores. Ver arriba | CONFIRMADO |
| ¿Cuál es el umbral de ausencia de heartbeat? | 5 min (dashboard y tarea) / 10 min (Centro) | CONFIRMADO |
| ¿Cómo se trata una estación que nunca reportó? | `ultimo_heartbeat=None` queda fuera de "en línea" en los tres sitios, que es correcto. **Pero en el dashboard entra en el denominador** `total_estaciones` sin filtro de aprobación | CONFIRMADO |
| ¿Qué ocurre si el último heartbeat está desactualizado? | Nada lo señala en el dashboard: el número se muestra igual, sin marca de frescura. El Centro sí marca el bloque de enlaces, pero **no** el de estaciones | CONFIRMADO |
| ¿Se pueden contar estaciones duplicadas? | **No.** `Estacion.codigo` es `unique` y los `Count(..., distinct=True)` de la matriz están correctamente puestos sobre los dos JOIN | CONFIRMADO (descartado) |
| ¿Puede aparecer en línea aunque el sistema dejó de recibir datos? | **Sí.** `ultimo_heartbeat` es un valor persistido. Si el worker muere, el dashboard muestra la flota en línea durante 5 minutos y luego la declara caída una por una. El aviso de worker caído (90 s) llega antes, pero **los KPI no se degradan a "sin dato": siguen afirmando un número** | CONFIRMADO |

### El denominador — `D-02` CONFIRMADO

`total_estaciones` y `total_online` del dashboard se suman en Python sobre el resultado de
`calcular_matriz_cumplimiento` (`dashboard.py:50-51`), que tiene dos sesgos:

```python
# apps/catalogo/services.py:610
grupos = Grupo.objects.filter(en_alcance).exclude(codigo='PENDIENTE')...
#                                         ^^^^^^^^^^^^^^^^^^^^^^^^^^
#   Excluye el bucket de las farmacias cuyo nodo no se pudo resolver.
#   Correcto para un reporte de cumplimiento de versión; incorrecto para
#   un KPI que se lee como "la flota".
```

Y en ninguno de los cinco `Count` anotados aparece un filtro por `estado_aprobacion`, así
que **las pendientes y las rechazadas suman al total**. La tarjeta contigua dice
"Pendientes de aprobación: 3" — esas 3 están dentro del denominador de la tarjeta de al
lado.

El placeholder `PENDIENTE` lo crea `importar_red_farmacias_xlsx`
(`NODO_PLACEHOLDER = 'PENDIENTE'`, línea 48) para las filas cuyo nodo llega como
`ELIPSYS_CRESIO` o `#N/A`. CLAUDE.md menciona 208 farmacias en esa situación por
desincronización de planillas.

> **POR VALIDAR — `L-1`.** Cuántas estaciones quedan hoy fuera del denominador. La base
> local de desarrollo tiene 2 farmacias y 9 estaciones: no es representativa. La consulta
> está en [09 · Límites](09-riesgos-dependencias.md#l--límites-de-esta-auditoría).

### Las pausadas — `D-09` CONFIRMADO

Una estación pausada **sigue latiendo** por diseño, y el diseño es correcto. Pero cuenta
como "en línea" en los tres contadores, sin que nada lo indique, aunque no ejecuta
comandos ni reporta métricas. "52 en línea" cuando 4 están frenadas es un número que
oculta una decisión operativa deliberada.

---

## B · Estaciones sin reportar

`dashboard.py:85-95` construye **una sola lista** con un `OR`:

```python
limite_online = timezone.now() - timedelta(minutes=ONLINE_UMBRAL_MINUTOS)
sin_reportar = (
    Estacion.objects
    .filter(estado_aprobacion=Estacion.EstadoAprobacion.APROBADA,
            farmacia__unidad_negocio__in=visibles)
    .filter(Q(ultimo_heartbeat__isnull=True) | Q(ultimo_heartbeat__lt=limite_online))
    .select_related('farmacia')
    .order_by(F('ultimo_heartbeat').asc(nulls_first=True))
)
```

Las cuatro poblaciones que el encargo pide distinguir — `D-08`:

| Población | ¿Se distingue? | Evidencia |
|---|---|---|
| Aprobada que **nunca** reportó | **En la fila sí.** `pill critical "nunca reportó"`, y va primero por `nulls_first` | `dashboard.html`; `dashboard.py:94` |
| Reportaba y **dejó** de hacerlo | Sí, muestra `hace {{ timesince }}` | `dashboard.html` |
| **Recuperada** | **No se representa.** No hay historial de transición por estación en esta pantalla (`EstadoDispositivo` existe, no se consulta aquí) | — |
| **Desactualizada por fallo del monitoreo** | **No se distingue en absoluto.** Si el worker cae, las 700 aparecen aquí como caídas individuales | `dashboard.py` no consulta frescura |

El **encabezado y el total mezclan las dos primeras**: el bloque se titula "Estaciones que
dejaron de reportar" y `total_sin_reportar` las suma. Una estación aprobada que jamás dio
señal es una **instalación fallida** —el técnico se fue y nunca funcionó— y una que
reportaba y calló es una **caída**. Son dos trabajos distintos para dos personas distintas.

La propia vista lo tiene claro en su comentario (*"una estación aprobada que jamás dio
señal es una instalación fallida, no una caída"*) y la fila lo refleja. El encabezado y el
contador no siguieron.

**El encargo pide expresamente no unificar estos estados si se pierde información
operativa.** Coincide con el diseño de la fila y choca con el del encabezado: la
recomendación es **dos contadores**, no uno con detalle dentro.

---

## C · Enlaces caídos

**Es el flujo mejor resuelto del sistema.** Casi todo está CONFIRMADO como correcto:

| Pregunta del encargo | Respuesta | Valoración |
|---|---|---|
| ¿Cómo se detecta una caída? | 3 sondeos ICMP fallidos consecutivos (`ConfiguracionMonitoreo.fallas_consecutivas_enlace`, editable sin desplegar), ~6 min con el barrido de 2 min | ✅ Correcto, y el umbral vive en la BD |
| ¿Cómo se confirma la recuperación? | Inmediata al primer sondeo exitoso: *"si respondió, está viva"* | ✅ |
| ¿Cómo se calcula la duración? | `inicio = estado.primer_fallo`, el **primer** fallo y no el tercero (`enlaces.py:232`) | ✅ Correcto para SLA. Con el tercero, toda caída parecería más corta |
| ¿Cómo se evitan eventos abiertos duplicados? | Por la bandera `estaba_caida` (`enlaces.py:196, 236`) | ⚠️ **No hay constraint.** Ver abajo |
| ¿Cómo se relacionan con farmacias y proveedores? | `nombre_proveedor()` lee `tipo_enlace`, **no** `circuito_proveedor`. Punto único reusado por correo, Centro y lista | ✅ Corregido el 20-sep-2026 con medición sobre las 700 farmacias: el circuito tiene forma `cadena-sitio-ciudad`, así que cortarlo por el primer guion daba 164 "proveedores" llamados `sangregorio` |
| ¿Qué ocurre si faltan datos del circuito? | La plantilla cae del evento a la farmacia viva y, si no hay ninguno, **lo dice**: "circuito sin cargar" | ✅ No inventa |
| ¿El estado refleja el último estado conocido o una comprobación actual? | **Último estado conocido** | ⚠️ El Centro lo admite; el dashboard no. Ver abajo |

### Lo que sí hay que corregir

**`D-04` · Dos fuentes para el mismo número** — CONFIRMADO.
El KPI del Centro cuenta `EstadoEnlaceFarmacia(alcanzable=False)`; el dashboard y la lista
del Centro cuentan `EventoEnlaceFarmacia(fin=null)`. `registrar_sondeo` mantiene los dos
coherentes, así que **en operación normal coinciden**. Lo que falta es la garantía
estructural: `EventoEnlaceFarmacia.Meta` solo declara `indexes`, no hay
`UniqueConstraint` parcial sobre `(farmacia, fin=null)`, y la recuperación cierra
únicamente el evento abierto más reciente (`.order_by('-inicio').first()`). Un evento
huérfano abierto quedaría contado para siempre por una pantalla y no por la otra.

> **POR VALIDAR — `L-3`.** Si hay eventos abiertos duplicados hoy. Consulta en la
> parte 09. Hay que medirlo **antes** de intentar el constraint: la migración falla si
> existen.

**`D-07` · El dashboard no admite que el dato puede estar viejo** — CONFIRMADO.
`fuente_desactualizada()` existe y el Centro la usa para marcar "dato posiblemente viejo"
en el bloque de enlaces. El dashboard muestra los mismos eventos **sin ninguna marca de
frescura**. Importa porque el barrido **aborta completo** si falla el 80 % o más — una
protección correcta— y entonces nada se escribe: el panel sigue mostrando los últimos
estados buenos como si fueran de ahora. Ocurrió: 4,7 h sin un solo sondeo el 2-oct-2026, y
el síntoma que llegó fue *"hay enlaces marcados caídos que ya tienen conexión"*.

---

## D · Alertas

### Severidad

Solo dos niveles, en `ReglaAlerta.Severidad` (`monitoreo/models.py:298`):

| Nivel | Significado real |
|---|---|
| `WARNING` | La condición de la regla se cumple y alguien debería mirarlo |
| `CRITICAL` | La condición se cumple y además dispara diagnóstico por IA y puede abrir mantenimiento |

Son severidad **técnica de la regla**, no impacto de negocio. **Ninguna regla declara "la
farmacia no puede vender".** El único indicador con semántica de negocio en todo el sistema
es `EstadoServicioPos.critico`, cuya etiqueta en el Centro es literalmente *"Críticos — la
caja no puede vender"* — y vive en otro modelo, en otra pantalla.

**Respuesta a la pregunta del encargo:** una alerta crítica representa una **condición
técnica**, no un impacto operativo confirmado. No debe presentarse como incidencia de
negocio sin cruzarla con `EstadoServicioPos.critico` o con el estado del enlace del sitio.

### Ciclo de vida y deduplicación

`ABIERTA → RECONOCIDA → RESUELTA`. Reconocer detiene el escalamiento (30 min, configurable
en BD) sin resolver, lo cual es el comportamiento correcto.

La deduplicación es sólida: `_alerta_activa(regla, estacion)` (`services.py:105`) impide
una segunda alerta activa para el mismo par, y `abrir_o_mantener_alerta` no renotifica.

**Pero no deduplica por sitio.** Tres cajas de la misma farmacia con la misma condición
generan tres alertas, y la lista de 6 filas del dashboard puede llenarse con una sola
farmacia. El Centro compensa con I-08 "farmacias afectadas" —y su comentario explica
exactamente por qué: *"27 advertencias pueden ser 27 sitios o uno con 27 síntomas"*—. El
dashboard no tiene ese indicador.

### Relación con los eventos de conectividad

**No existe.** Una farmacia con el enlace caído genera, en paralelo:

- un `EventoEnlaceFarmacia` abierto,
- una alerta `sin_heartbeat` **por cada estación** del sitio (vía
  `marcar_estaciones_offline` → `evaluar_reglas_sin_heartbeat`),
- y filas en "estaciones sin reportar".

Son tres bloques distintos de la pantalla para **un único hecho**. El resumen lo compensa
en el conteo de farmacias afectadas (la unión de ids evita contar la farmacia tres veces),
pero no en la lectura: el operador ve el mismo incidente repetido en tres sitios.

Ese es el argumento principal para la **cola unificada por farmacia** de la Zona 1 del
diseño propuesto ([parte 06](06-diseno-propuesto.md)).

### Falsos positivos: las mitigaciones que ya existen

No son hipotéticas, están implementadas:

- `duracion_minutos` en la regla: la condición tiene que **sostenerse**, no basta un pico
  (`_condicion_sostenida`, `services.py:85`).
- `VentanaMantenimiento`: silencia por destino resuelto, con **un solo chequeo** en
  `abrir_o_mantener_alerta` que cubre las cuatro rutas de evaluación.
- `resolver_alertas_agente_caido_red_viva`: cierra la alerta cuando la red prueba que el
  equipo está vivo aunque el agente calle.
- Las ventanas activas se muestran **arriba de todo** en el Centro, con el texto *"lo que
  esté caído en estos destinos es esperado"*.

### `D-03` · Las dos cifras que no cuadran — CONFIRMADO

```python
# apps/panel/views/dashboard.py:43   -> KPI "Alertas abiertas"
estado__in=[Alerta.Estado.ABIERTA, Alerta.Estado.RECONOCIDA]

# apps/monitoreo/services.py:1465   -> KPI "Alertas críticas" + "Advertencias" del Centro
Alerta.objects.filter(estado=Alerta.Estado.ABIERTA)
```

El dashboard dice "12 alertas abiertas"; el Centro dice "3 críticas + 5 advertencias". La
diferencia son las reconocidas, y ninguna de las dos pantallas lo explica. Las dos cifras
son correctas y responden a preguntas distintas: **el problema es que las dos se llaman
igual.**

---

## E · Salud de la plataforma

La tabla completa de mecanismos está en
[02 · 2.3](02-arquitectura-encontrada.md#23-salud-de-la-propia-plataforma). Aquí, la
respuesta a lo que el encargo pide: distinguir **proceso activo**, **comunicación
funcional**, **procesamiento efectivo** y **disponibilidad de los datos**.

| Componente | Proceso activo | Comunicación | Procesamiento efectivo | Datos disponibles |
|---|---|---|---|---|
| Worker MQTT | ✅ `WorkerHeartbeat` | ❌ no se comprueba | ❌ no se comprueba | ⚠️ inferible por `max(ultimo_heartbeat)` — **no se calcula** |
| Broker EMQX | ❌ nada | ❌ nada | ❌ nada | ❌ |
| Recepción de heartbeats | — | — | ⚠️ `MensajeMqttFallido` existe y **no se muestra** | ⚠️ |
| Celery Beat | ❌ sin latido propio | — | ✅ inferido de `MuestraRedFarmacia` (solo en consola) | ✅ |
| Sondeo de enlaces | — | — | ✅ inferido de `ultima_verificacion` (consola + Centro) | ✅ |
| Servidor de aplicación | implícito (la página carga) | — | — | — |
| Respaldos | ✅ `WorkerHeartbeat` | — | ✅ la fila la escribe el script **al terminar bien** | ✅ |

**El encargo pide expresamente no presentar un componente como operativo solo porque su
proceso existe. Hoy el panel hace exactamente eso con el worker MQTT** (`D-05`), y lo
agrava con la afirmación fija de la topbar (`D-06`, ver [parte 05](05-auditoria-ux.md)).

### Mecanismos de observabilidad que harían falta — NO EXISTEN

| # | Mecanismo | Coste estimado | Qué distingue |
|---|---|---|---|
| 1 | `client.is_connected()` en el latido, o un campo `conectado_al_broker` en `WorkerHeartbeat` | Bajo (1 campo + 1 migración) | Proceso vivo vs. comunicación funcional |
| 2 | Contador de mensajes procesados en la ventana | Bajo | Conectado vs. procesando |
| 3 | `max(Estacion.ultimo_heartbeat)` de la flota | **Trivial** (1 agregado) | **El más barato de todos**: prueba que la cadena entrega de punta a punta |
| 4 | `MensajeMqttFallido(revisado=False)` en pantalla | Trivial (1 count) | Recibe pero no procesa |
| 5 | Estado de EMQX vía `apps/mqtt_worker/emqx_admin.py` (integración **ya existe**) | Medio: debe ir en tarea de fondo, nunca en el render | Broker vivo |
| 6 | Latido propio de Celery Beat | Bajo | Hoy se infiere de una tabla que **también** escribe otra ruta |

El número 3 es la recomendación prioritaria de este apartado: una sola consulta de agregado
convierte "el worker dice estar vivo" en "el último dato de la flota llegó hace 38 s".

---

## F · Despliegues

| Pregunta del encargo | Respuesta | Marca |
|---|---|---|
| ¿Qué estados existen? | Despliegue: `BORRADOR`, `PENDIENTE_APROBACION`, `APROBADO`, `PUBLICANDO`, `PAUSADO`, `COMPLETADO`, `CANCELADO`. Por estación (`ResultadoDespliegue`): `PENDIENTE`, `PUBLICADO`, `DESCARGANDO`, `DESCARGADO`, `VERIFICADO`, `APLICANDO`, `APLICADO`, `ERROR`, `ROLLBACK` | CONFIRMADO |
| ¿Cómo se calcula el progreso? | `apps/panel/progreso.py:resumen_de_progreso`: una sola pasada sobre los resultados ya traídos. `completados` = solo `APLICADO`; `errores` = `ERROR` + `ROLLBACK`; `pct = round(100*completados/total)`, y **0 si no hay resultados**, no una división por cero | CONFIRMADO, y está bien hecho |
| ¿Cómo se registran los errores? | `ResultadoDespliegue` con `PROTECT` (no `CASCADE`) más `EventoDespliegue` colgando: es el **acta** de que una versión llegó o no a esa caja | CONFIRMADO |

**`D-13` · El dashboard no usa nada de eso.** Muestra versión + estado y un `pill`:

```html
<!-- templates/panel/dashboard.html, bloque "Despliegues en curso" -->
<span class="font-medium">v{{ d.version }}</span>
<span class="pill {% if d.estado == 'pausado' %}critical{% else %}info{% endif %}">
    {{ d.get_estado_display }}
</span>
```

No hay forma de ver desde el tablero que un despliegue esté fallando. El encargo advierte
contra inventar porcentajes de progreso: **no hace falta inventarlos, ya están
calculados** en `resumen_de_progreso` y usados en la pantalla de detalle.

Tampoco se deduce nada del estado de las estaciones, que es correcto: el acta es
`ResultadoDespliegue`, no el hecho de que la caja esté en línea.

---

## Lo que hay que decidir en esta parte

### Decisión bloqueante · el umbral de "estación en línea"

Es la única decisión que no puede tomar esta auditoría, y el encargo lo pide
explícitamente: *"los umbrales deben proceder de la configuración o de reglas verificables
del sistema, no de valores inventados durante el rediseño"*.

Hay que elegir **un** valor y, sobre todo, responder si ese valor gobierna también el
umbral que **abre alertas**:

| Opción | Efecto en la pantalla | Efecto en las alertas |
|---|---|---|
| **5 min para todo** | El Centro empieza a mostrar como caídas estaciones que hoy muestra en línea | Ninguno (ya es 5) |
| **10 min para todo** | El dashboard muestra en línea estaciones que hoy marca caídas | **Retrasa 5 min la apertura de alertas `sin_heartbeat`** |
| **Dos umbrales con nombre propio**: visualización 10 min, alerta 5 min | Ninguno respecto de hoy en el Centro | Ninguno | 

La tercera opción es la que menos mueve y la que más hay que documentar: deja dos valores,
pero **con dueño, nombre y motivo**, en vez de tres accidentes. Es la recomendación de esta
auditoría, con la condición de que el tablero diga cuál está usando.

### Otras decisiones de esta parte

1. ¿Se acepta separar "nunca reportó" de "dejó de reportar" en dos contadores?
2. ¿Se acepta la cola unificada por farmacia, aunque implique que un mismo sitio deje de
   aparecer en tres bloques distintos?
3. ¿Se autoriza medir `L-1` y `L-3` contra producción (dos consultas de solo lectura)?

---

[← 03 Indicadores](03-auditoria-indicadores.md) · [Índice](README.md) · [Siguiente: Auditoría UX/UI →](05-auditoria-ux.md)
