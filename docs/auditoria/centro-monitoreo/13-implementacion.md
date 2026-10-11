# 13 · Qué se implementó

[← 12 Índice de hallazgos](12-indice-hallazgos.md) · [Índice](README.md)

Registro de la implementación de las cuatro fases, hecha el **11-oct-2026** tras la
entrega del informe. Incluye las tres cosas que el informe recomendaba y que al
implementarlas se descartaron, con el motivo.

Entrada correspondiente en el plan: **`PLAN_MODERNIZACION.md` §10-BI**.

| | |
|---|---|
| **Decisión del usuario sobre el umbral** (`X-1`) | Dos umbrales con nombre propio: visualización 10 min, alerta 5 min |
| **Alcance aprobado** | Las cuatro fases, A → D |
| **Suite** | Completa, contra PostgreSQL (TimescaleDB en Docker), no SQLite |
| **Migraciones** | 2: `mqtt_worker/0003`, `catalogo/0036`. Sin drift |

---

## Estado de los hallazgos

| Id | Hallazgo | Estado | Dónde |
|---|---|---|---|
| `D-01` | Tres criterios de "estación en línea" | **CERRADO** | `monitoreo/umbrales.py`, `catalogo/services.py:estado_flota` |
| `D-02` | El denominador del KPI no era la flota | **CERRADO** | `estado_flota` |
| `D-03` | "Alertas abiertas" incluía reconocidas sin decirlo | **CERRADO** | dos contadores, en tablero y Centro |
| `D-04` | Dos fuentes para enlaces caídos; evento fantasma | **CERRADO en la causa**, constraint descartado | `resumen_operacion`, `enlaces.registrar_sondeo` |
| `D-05` | El latido del worker no prueba el servicio | **CERRADO** | `WorkerHeartbeat.conectado`, `run_mqtt_worker._latir` |
| `D-06` | La barra afirmaba salud fija | **CERRADO** | `monitoreo/salud.py`, `base.html` |
| `D-07` | El tablero no decía de cuándo era el dato | **CERRADO** | Zona 0 + "Calculado HH:MM:SS" |
| `D-08` | "Sin reportar" mezclaba dos poblaciones | **CERRADO** | `estado_flota` |
| `D-09` | Las pausadas contaban como en línea en silencio | **CERRADO** | contador propio |
| `D-10` | `enrolamientos_rechazados_lista` era URL huérfana | **CERRADO** | Zona de enrolamiento |
| `D-11` | `MensajeMqttFallido` invisible en el panel | **CERRADO** | Zona 0 |
| `D-12` | El broker no se comprobaba | **CERRADO por otra vía** | se deduce de la conexión del worker |
| `D-13` | Despliegues sin progreso ni errores | **CERRADO** | `Count` condicional + orden por errores |
| `D-14` | Faltaba prueba de equivalencia | **CERRADO** | `catalogo/tests_equivalencia.py` |
| `D-15` | El tablero no usaba `_estado_chip.html` | **CERRADO** | toda la pantalla |
| `U-1`…`U-5`, `U-9`, `U-10` | Lectura de la pantalla | **CERRADOS** | Fases A y C |
| `U-6` | "Está bien" vs. "no se sabe" | **CERRADO** | regla del gris en `salud.py` |
| `U-7` | Dos tableros compitiendo | **PARCIAL** | ver abajo |
| `U-8` | Listas sin el campo que decide | **CERRADO** | la cola lleva farmacia, síntoma, antigüedad y nº de síntomas |
| `R-1` | Pares count+slice | **CERRADO** | el total sale de `estado_flota`; enlaces, un `distinct().count()` |
| `R-2` | Dos consultas a `WorkerHeartbeat` | **CERRADO** | una sola, en `estado_latidos` |
| `R-3` | Sin índices en `Estacion` | **CERRADO** | `catalogo/0036` |
| `R-4` | La matriz se pagaba en cada carga | **CERRADO** | se carga al abrirse |
| `R-5` | Sin `CACHES` | **DESCARTADO**, ver abajo |
| `R-6` | El tablero no se refrescaba | **CERRADO** | marco + parcial, 60 s |
| `R-7` | `save()` sin `update_fields` en el latido | **FUERA DE ALCANCE** (declarado en el informe) |
| `R-8` | Coste del Centro por refresco | **CERRADO en la fuente** | 4 consultas menos en `resumen_operacion` |

---

## Lo que el informe recomendaba y no se hizo

Las tres decisiones se tomaron al implementar, con la evidencia delante. Están en el
código, en el comentario del sitio correspondiente, y en §10-BI.

### 1. El constraint de unicidad en `EventoEnlaceFarmacia` (`D-04`, `X-3`)

**Descartado.** Una migración que imponga `UniqueConstraint(farmacia, condition=fin IS NULL)`
falla si ya hay duplicados en producción, y resolverlos automáticamente significa
inventarle un `fin` a un evento cuya duración real nadie midió — en la tabla que es la
línea base para discutir un SLA con el proveedor.

**Lo que se hizo en su lugar:** atacar la causa del daño. `registrar_sondeo` cierra ahora
**todas** las caídas abiertas de la farmacia y no solo la más reciente, así que un
duplicado no puede sobrevivir como un fantasma eterno (una caída abierta para siempre en un
sitio que responde). Para medir si quedó alguno de antes —el `L-3` del informe— hay un
comando de solo lectura: `manage.py revisar_caidas_duplicadas`.

El constraint sigue siendo deseable **después** de esa medición y de resolver a mano lo que
aparezca. Queda anotado, no hecho.

### 2. `CACHES` con Redis (`R-5`)

**Descartado por falta de consumidor.** Se comprobó: `django-axes` usa el handler de base
de datos (`AXES_HANDLER` no está configurado, y el defecto es la BD), y nada en `apps/`
toca el framework de caché. El único candidato era `resumen_operacion`, y se resolvió mejor
en la fuente: `estado_flota` lo dejó con cuatro consultas menos. Ponerle una caché a un
número cuyo único valor es decir la verdad ahora habría añadido retraso justo donde no
conviene (`X-7`).

Si aparece un consumidor real, la configuración son cinco líneas y la dependencia
(`redis>=5.0`) ya está en `requirements.txt`.

### 3. `AddIndexConcurrently` para el índice de `Estacion` (`X-4`)

**Descartado por tamaño.** La recomendación era correcta como cautela genérica: `AddIndex`
toma un lock de escritura y el entrypoint corre las migraciones solo en cada despliegue.
Pero a decenas de filas hoy y ~1.800 proyectadas, el `CREATE INDEX` son milisegundos,
mientras que la variante concurrente obliga a `atomic = False` y a una operación exclusiva
de PostgreSQL que rompería el desarrollo local sobre SQLite. El motivo está escrito en el
docstring de la migración; si la tabla llegara a cientos de miles de filas, el cambio es de
una línea.

---

## Lo que queda abierto

| # | Qué | Por qué no entró |
|---|---|---|
| `U-7` | **La consolidación de los dos tableros.** El tablero quedó al nivel del Centro —refresco, frescura, chips, cola unificada— y los dos leen ahora de las mismas funciones, así que **ya no pueden discrepar**. Lo que no se hizo es eliminar uno | Es una decisión de producto, no técnica, y el informe la deja planteada (parte 06). Con los números unificados, el costo de mantener los dos bajó mucho |
| `L-1` | Cuántas estaciones entraron al denominador al dejar de excluir el grupo `PENDIENTE` | Requiere la consulta de la parte 09 contra producción. **El número del KPI va a cambiar en el despliegue, y esa es la corrección** |
| `L-3` | Si hay caídas duplicadas hoy | `manage.py revisar_caidas_duplicadas` |
| `L-5` | Si la mesa de ayuda usa `/` o `/centro/` | Una pregunta, no un cambio |
| `L-6` | Latencia real del panel en producción | Hay que medirla allí |
| `R-7` | `estacion.save()` sin `update_fields` | Declarado fuera de alcance: es la ruta de ingesta, no el tablero |

---

## Qué mirar al desplegar

1. **Los números del KPI de flota van a cambiar** y eso es la corrección, no un error: el
   denominador ahora son solo las aprobadas e incluye las farmacias sin nodo asignado, y el
   umbral de visualización pasó de 5 a 10 minutos. Conviene anotar los valores previos y
   avisar a la mesa de ayuda el mismo día.
2. **`build` sin argumento**, no `build web`: la Fase D toca `run_mqtt_worker` y
   `apps/monitoreo/tasks.py`, así que el worker MQTT y Celery necesitan imagen nueva. Con
   `build web` se quedarían con el código viejo (ver `CLAUDE.md`).
3. **La tarea `latido-de-beat` es nueva**: hasta que Beat la dispare por primera vez, la
   Zona 0 mostrará "Tareas periódicas · sin verificar" en gris. Es el comportamiento
   correcto, no un fallo.
4. **`conectado` arranca en `null`** para los workers ya corriendo: hasta que se recreen,
   el panel mostrará "conexión al broker sin verificar" en ámbar. También es correcto.
5. Comprobar que ninguna imagen quedó con código viejo con el `diff` dentro del contenedor
   que documenta `CLAUDE.md`.

---

[← 12 Índice de hallazgos](12-indice-hallazgos.md) · [Índice](README.md)
