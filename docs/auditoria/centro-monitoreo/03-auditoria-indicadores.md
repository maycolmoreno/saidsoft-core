# 03 · Auditoría de indicadores

[← 02 Arquitectura](02-arquitectura-encontrada.md) · [Índice](README.md) · [Siguiente: Los seis riesgos del encargo →](04-riesgos-del-encargo.md)

Los 21 indicadores que hoy existen (o que faltan), con su origen, su regla de cálculo real,
su fiabilidad y la mejora recomendada. Los seis riesgos específicos que pidió el encargo
tienen su propia parte: [04](04-riesgos-del-encargo.md).

**Cómo leer la columna "Fiabilidad":**

| Nivel | Significado |
|---|---|
| **Alta** | El número dice lo que su etiqueta promete y la población está bien definida |
| **Media** | El número es correcto, pero su etiqueta o su presentación admiten dos lecturas |
| **Baja** | La población contada no corresponde a lo que la etiqueta afirma |

---

## 3.1 Tabla maestra

| Id | Indicador | Origen | Regla de cálculo | Fiabilidad | Limitación CONFIRMADA | Mejora recomendada |
|---|---|---|---|---|---|---|
| I-01 | **Estaciones en línea** (dashboard) | `calcular_matriz_cumplimiento`, `catalogo/services.py:618` | `ultimo_heartbeat >= now-5min`, sobre **todas** las estaciones de grupos distintos de `PENDIENTE` | **Baja** | No filtra `estado_aprobacion`; excluye el grupo `PENDIENTE`; incluye administrativas y pausadas | Fuente única de dominio; filtrar `APROBADA`; administrativas aparte, como ya hace el Centro |
| I-02 | **Total de estaciones** | idem | `Count('farmacias__estaciones', distinct=True)` | **Baja** | Mismo sesgo que I-01; además **duplica el denominador** que la tarjeta contigua ya muestra | Eliminar como KPI propio: vive dentro de I-01 |
| I-03 | **Estaciones en línea** (Centro) | `resumen_operacion`, `monitoreo/services.py:1443` | `ultimo_heartbeat >= now-10min`, solo `APROBADA`, administrativas contadas aparte | **Alta** | Umbral distinto del dashboard y de `estado_conexion` | **Es la definición correcta**: promoverla a fuente única |
| I-04 | **Pendientes de aprobación** | `dashboard.py:38` | `estado_aprobacion=PENDIENTE` | **Alta** | Aparece dos veces en la misma pantalla (aviso + KPI); no incluye los enrolamientos rechazados, que es el caso que de verdad duele | Fusionar en un bloque "Enrolamiento" junto a I-20 |
| I-05 | **Enlaces caídos** | dashboard y lista del Centro: `EventoEnlaceFarmacia(fin=null)`; **KPI del Centro**: `EstadoEnlaceFarmacia(alcanzable=False)` | Tres exclusiones idénticas en los tres sitios: `fin=null`, `farmacia.activa`, `respondio_alguna_vez` | **Media-alta** | Dos fuentes para el mismo número; nada estructural impide dos eventos abiertos por farmacia | El EVENTO como fuente única (sabe *cuándo* empezó) + constraint de unicidad parcial |
| I-06 | **Alertas abiertas** (dashboard) | `dashboard.py:43` | `estado in {ABIERTA, RECONOCIDA}` | **Media** | Incluye reconocidas sin decirlo; el Centro cuenta solo `ABIERTA`, así que los dos tableros no cuadran | Dos cifras: "sin reconocer" y "en atención" |
| I-07 | **Alertas críticas / advertencias** | `resumen_operacion:1467` | `estado=ABIERTA` + `regla.severidad` | **Alta** | La deduplicación es por `(regla, estación)`: una farmacia con 3 cajas genera 3 alertas iguales | Mantener; mostrar **siempre** junto a I-08 |
| I-08 | **Farmacias afectadas** | `resumen_operacion:1518` | Unión en Python de 3 querysets de ids (alertas, enlaces, POS) | **Alta** | Excluye a propósito `activos_sin_sondeo` y `estaciones_fuera` — decisión documentada y correcta | **Subirlo al dashboard**: es el número que dimensiona la jornada |
| I-09 | **Sin reportar** | `dashboard.py:85` | `APROBADA` y (`ultimo_heartbeat` nulo o `< now-5min`) | **Media** | Mezcla "nunca reportó" (instalación fallida) y "dejó de reportar" (caída) en el título y en el total | Dos contadores separados; la fila ya los distingue, el encabezado no |
| I-10 | **Despliegues activos** | `dashboard.py:35` | `estado in {PUBLICANDO, PAUSADO}` | **Alta** para "cuántos" | No dice progreso ni errores, aunque `apps/panel/progreso.py` ya los calcula | Anotar `Count` condicional de resultados en `ERROR` / `ROLLBACK` |
| I-11 | **Worker MQTT activo** | `dashboard.py:57` | `now - ultimo_latido < 90 s` | **Baja como prueba de servicio** | Un hilo lo escribe mientras el proceso viva: no prueba conexión al broker ni procesamiento | Complementar con "último latido recibido de la flota" y con I-19 |
| I-12 | **Respaldo al día** | `dashboard.py:139` | `now - ultimo_latido < 26 h`; `None` cuenta como atrasado | **Alta** | Ninguna. Incluido el tratamiento de `None`, que es el caso de un servidor recién montado **y** el de uno donde el timer nunca se instaló | Mantener; mover a la cinta de confianza (Zona 0) |
| I-13 | **Matriz de cumplimiento** | `calcular_matriz_cumplimiento` | `version_pos == grupo.version_objetivo`; `pct=None` si no hay estaciones | **Media** | Mismo sesgo de población que I-01 / I-02. `version_pos=''` cuenta como no conforme, que es correcto | Mantener el cálculo; mover **al final** de la pantalla |
| I-14 | **Servicios del POS** | `EstadoServicioPos(disponible=False, ultima_respuesta != null)` | Separados en críticos / no críticos | **Alta** | Solo existe en el Centro | Subir los **críticos** al dashboard: es el único indicador con semántica de negocio ("la caja no puede vender") |
| I-15 | **Equipos sin sondeo** | `EstadoRedActivo.ultima_verificacion < now-H` | `HORAS_VERIFICACION_VIGENTE` | **Alta** | Solo en el Centro | Mantener donde está. La distinción "silencio, no caída" es conceptualmente valiosa |
| I-16 | **Último sondeo de red** | `MuestraRedFarmacia` más reciente | `fuente_desactualizada(..., 'red_farmacias')` | **Alta** | Solo marca el bloque de enlaces del Centro | Extender el patrón a **todas** las zonas |
| I-17 | **Cierres en conflicto** | `contar_cierres_en_conflicto(unidades)` | — | **Alta** | Solo en el Centro, condicionado por permiso | Correcto como está; es el patrón de degradación por permiso a imitar |
| I-18 | **Farmacias monitoreadas** | **NO EXISTE** | — | — | **No hay ningún indicador de cuántos sitios se vigilan.** Sin denominador, "4 caídas" no se puede interpretar | Añadir: `Farmacia(activa=True, ip_router != null)`. Una consulta |
| I-19 | **Mensajes MQTT fallidos** | `MensajeMqttFallido(revisado=False)` — **NO EXISTE en el panel** | — | — | Modelo poblado por el worker; **cero presencia en el panel**, solo en el admin de Django | Añadir a la Zona 0: es la prueba directa de "recibe pero no procesa" |
| I-20 | **Enrolamientos rechazados** | `EnrolamientoRechazado(revisado=False)` — **NO EXISTE en el panel** | La vista existe (`views/enrolamientos.py:26`) | — | `panel:enrolamientos_rechazados_lista` **no está enlazada desde ninguna plantilla**: URL huérfana | Añadir al bloque de enrolamiento junto a I-04 |
| I-21 | **Estaciones pausadas** | `Estacion.pausado=True` — **NO EXISTE** | — | — | Una pausada **sigue latiendo** y cuenta como "en línea" en los tres contadores, aunque no ejecuta nada | Contador propio: "52 en línea, 4 pausadas" |

---

## 3.2 Los hallazgos de confiabilidad que salen de esta tabla

Identificadores estables, para poder referirse a ellos en la revisión. El detalle de cada
uno está en la parte indicada.

| Id | Hallazgo | Marca | Detalle |
|---|---|---|---|
| `D-01` | Tres criterios distintos de "estación en línea" en tres pantallas | CONFIRMADO | [04 · A](04-riesgos-del-encargo.md#a--estaciones-en-línea) |
| `D-02` | El denominador del KPI principal excluye el grupo `PENDIENTE` y no filtra por aprobación | CONFIRMADO | I-01, I-02 |
| `D-03` | "Alertas abiertas" incluye reconocidas en el dashboard y no en el Centro | CONFIRMADO | I-06, [04 · D](04-riesgos-del-encargo.md#d--alertas) |
| `D-04` | Dos fuentes para "enlaces caídos", sin constraint que impida dos eventos abiertos | CONFIRMADO | I-05, [04 · C](04-riesgos-del-encargo.md#c--enlaces-caídos) |
| `D-08` | "Sin reportar" mezcla dos poblaciones en el título y en el total | CONFIRMADO | I-09, [04 · B](04-riesgos-del-encargo.md#b--estaciones-sin-reportar) |
| `D-09` | Las estaciones pausadas cuentan como en línea en los tres contadores | CONFIRMADO | I-21 |
| `D-10` | `enrolamientos_rechazados_lista` es una URL huérfana | CONFIRMADO | I-20 |
| `D-11` | `MensajeMqttFallido` no aparece en ninguna pantalla del panel | CONFIRMADO | I-19 |
| `D-13` | Los despliegues del dashboard no muestran progreso ni errores | CONFIRMADO | I-10, [04 · F](04-riesgos-del-encargo.md#f--despliegues) |

---

## 3.3 Qué se mantiene, qué cambia, qué se elimina

### Se mantienen sin cambio de lógica

I-07 · I-08 · I-12 · I-14 · I-15 · I-16 · I-17, más tres patrones transversales que
conviene nombrar porque son buenos y hay que conservarlos:

- El **recorte a N filas con el total aparte** (`_MAX_FILAS_CENTRO = 15`, 6 en el dashboard).
- La pantalla de **solo lectura** con enlace al detalle donde la acción ya existe.
- El bloque de **ventanas de mantenimiento arriba de todo**: si hay una ventana activa, lo
  que siga puede estar caído por diseño, y verlo después de la lista de incidentes es verlo
  tarde.

### Se modifican

| Indicador | Cambio | Problema que resuelve |
|---|---|---|
| I-01 / I-02 / I-03 | Fuente única, un umbral, solo `APROBADA`, administrativas aparte, pausadas señaladas | `D-01`, `D-02`, `D-09`: tres números para un concepto |
| I-05 | El evento como fuente única del número y de la lista | `D-04`: dos fuentes para una cifra |
| I-06 | Separar "sin reconocer" de "en atención" | `D-03`: dos tableros con cifras incompatibles |
| I-09 | Dos contadores: nunca reportó / dejó de reportar | `D-08`: instalación fallida no es una caída |
| I-10 | Añadir progreso y errores con `Count` condicional | `D-13`: "2 despliegues activos" no dice si uno está fallando |
| I-11 | Complementar con último latido de la flota + I-19 | `D-05`: el latido no prueba el servicio |
| I-13 | Mover al final, plegado | `U-2`: jerarquía invertida |

### Se añaden

Los seis usan **datos ya persistidos**; ninguno exige un mecanismo nuevo de recolección:

I-18 farmacias vigiladas · I-19 mensajes MQTT fallidos · I-20 enrolamientos rechazados ·
I-21 estaciones pausadas · último latido recibido de la flota (`max(ultimo_heartbeat)`) ·
progreso y errores de despliegue (`resumen_de_progreso`, ya implementado).

### Se eliminan o se mueven

| Elemento | Destino | Por qué |
|---|---|---|
| KPI "Total de estaciones" | **eliminar** | Duplica el denominador del KPI contiguo |
| KPI "Pendientes de aprobación" | Zona 4 | Ya tiene aviso propio; es trabajo de enrolamiento, no estado de operación |
| Botón `+ Nuevo despliegue` | pantalla de Despliegues | La acción primaria de un tablero de monitoreo no es publicar software |
| `sys-status` fijo de la topbar | **reemplazar** por estado calculado | Afirma algo que nadie comprobó (`D-06`) |
| Matriz de cumplimiento | Zona 6, plegada | Es planificación, no incidencia |
| Eventos de Windows | Zona 7 (sigue al final) | Correcto donde está, y su comentario explica bien por qué |

---

## Lo que hay que decidir en esta parte

1. **¿Se acepta la lista de indicadores que se eliminan?** En particular "Total de
   estaciones" y el botón de nuevo despliegue, que son los dos cambios que alguien podría
   echar en falta.
2. **¿Se acepta subir I-08 (farmacias afectadas) y I-14 (POS críticos) al tablero
   principal?** Son los dos indicadores del Centro con más valor operativo.
3. ¿Hay algún indicador de la tabla que **no** deba tocarse por un motivo operativo que
   esta auditoría no pudo ver desde el código?

---

[← 02 Arquitectura](02-arquitectura-encontrada.md) · [Índice](README.md) · [Siguiente: Los seis riesgos del encargo →](04-riesgos-del-encargo.md)
