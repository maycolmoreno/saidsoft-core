# 06 · Diseño propuesto

[← 05 Auditoría UX/UI](05-auditoria-ux.md) · [Índice](README.md) · [Siguiente: Rendimiento →](07-rendimiento.md)

Todo lo de esta parte está marcado **DISEÑO**: es una recomendación argumentada, no un
hallazgo. Cada zona usa **únicamente datos que ya existen**; lo que no existe está marcado
como tal y señalado en [03 · I-18 a I-21](03-auditoria-indicadores.md#31-tabla-maestra).

---

## 6.1 Decisión estructural previa

**No construir un tercer tablero.** La recomendación es **una sola pantalla de monitoreo en
`/`**, que absorba lo que el Centro ya hace bien, dejando `/centro/` como alias.

Razón: el dashboard es lo que el 100 % de los usuarios ve al entrar y es el más débil; el
Centro es el mejor y requiere un clic más un permiso. **Mantener los dos garantiza que los
números sigan divergiendo**, porque cada pantalla tiene su propia definición de las mismas
cosas.

**Dependencia a resolver antes de tocar nada** (`X-2`): `/centro/` exige
`monitoreo.view_alerta` y `/` no exige ningún permiso. La pantalla unificada debe **degradar
por bloque** —ocultar las incidencias a quien no tiene el permiso— en vez de elevar el
requisito de la portada. El patrón ya existe en el propio Centro:

```django
{% if r.cierres_en_conflicto or perms.mantenimiento.view_cierreenconflicto %}
```

---

## 6.2 Esquema textual de la distribución propuesta

```
+- TOPBAR ----------------------------------------------------------------------------+
| [buscar]      [cliente v]    (!) MONITOREO DEGRADADO . 2 de 7        14:32:07       |
|                                  ^ reemplaza "Todos los sistemas operativos":       |
|                                    estado real calculado, o "sin verificar"          |
+-------------------------------------------------------------------------------------+

ZONA 0 . CONFIANZA EN EL DATO                         refresco 30 s
+-------------------------------------------------------------------------------------+
| (X) Worker MQTT    sin senal hace 4 min    | (v) Sondeo enlaces   hace 1 min        |
| (v) Ultimo latido  hace 38 s (ML016-A)     | (v) Beat (inferido)  hace 3 min        |
| (!) Respaldo       hace 31 h (umbral 26)   | (v) Mensajes fallidos  0 sin revisar   |
+-------------------------------------------------------------------------------------+
  Cada fila: chip (color + forma + palabra) . hora del ULTIMO DATO, no de la pantalla.
  Sin medicion -> "sin verificar" en gris, NUNCA verde.

ZONA 1 . LO QUE DUELE AHORA                           refresco 60 s
+-------------------------------------------------------------------------------------+
| 7 farmacias afectadas  .  4 sin enlace  .  2 sin POS  .  3 alertas criticas         |
|  +--------------------------------------------------------------------------------+ |
|  | (X) CRIT  ML016  Sta. Elena   sin enlace . TELCONET . 2 h 14 min   [3 cajas]   | |
|  | (X) CRIT  GP063  Buena Fe     POS sin base de datos . pg_local     1 h 02 min  | |
|  | (X) CRIT  MAM06  Manta        CPU sostenida 97%                      . 18 min  | |
|  | (!) ADV   ML001  Quito        reloj desincronizado 312 s   [reconocida 14:02]  | |
|  +--------------------------------------------------------------------------------+ |
+-------------------------------------------------------------------------------------+
  UNA cola unificada, agrupada por FARMACIA: un sitio = una fila.
  Orden: enlace caido > POS critico > alerta critica > advertencia; a igual nivel,
  la mas antigua primero. Solo aparece si hay algo.

ZONA 2 . INDICADORES OPERATIVOS                       refresco 60 s
+------------------+------------------+------------------+------------------+
| Farmacias        | Estaciones       | Sin reportar     | Despliegues      |
|  697 vigiladas   |  52/58 en linea  |  4 nunca         |  2 en curso      |
|    4 caidas (X)  |  [#######...]    |  2 dejaron (!)   |  1 con errores(!)|
|                  |  +18 admin.      |                  |  87% . 3 errores |
|                  |  (4 pausadas)    |                  |                  |
+------------------+------------------+------------------+------------------+

ZONA 3 . CONECTIVIDAD POR PROVEEDOR     ZONA 4 . ENROLAMIENTO
+-------------------------------+      +--------------------------------------+
| TELCONET (3)                  |      | 3 esperando aprobacion        -> ver |
|  ML016  10.11.3.1  2 h 14 min |      | 2 enrolamientos rechazados    -> ver |
|  GP012  10.11.8.1     47 min  |      |   MLO16-A (codigo mal escrito)       |
| PUNTO NET (1)                 |      +--------------------------------------+
|  MM233  10.21.4.1     12 min  |        refresco 120 s
| Ultimo sondeo: hace 1 min     |
+-------------------------------+
  refresco 60 s

ZONA 5 . ESTACIONES, POR QUE CALLAN                   refresco 120 s
+-------------------------------------------------------------------------------------+
| NUNCA REPORTO (4) - instalacion fallida | DEJARON DE REPORTAR (2) - caida           |
|  ML900-C  aprobada hace 6 d             |  ML900-B  hace 3 h 12 min                 |
|  MM144-A  aprobada hace 2 d             |  GP063-A  hace 41 min                     |
| PAUSADAS (4) - latiendo, sin ejecutar   | EN MANTENIMIENTO (0) - esperado           |
+-------------------------------------------------------------------------------------+
  Cuatro poblaciones separadas. NUNCA sumadas en un solo numero.

ZONA 6 . CUMPLIMIENTO Y DESPLIEGUES                   al abrir
+-------------------------------------------------------------------------------------+
| > Cumplimiento por grupo (7 grupos . 94% conforme)                                  |
| > Despliegues en curso (2)                                                          |
+-------------------------------------------------------------------------------------+

ZONA 7 . HARDWARE Y SISTEMA                           al abrir
  Equipos sin sondeo reciente . Eventos de Windows . Cierres en conflicto
                                            Calculado 14:32:05 . se actualiza en 60 s
```

---

## 6.3 Qué debe ver primero el operador

El orden de lectura responde a cuatro preguntas, en secuencia:

| # | Pregunta | Zona | Por qué en ese orden |
|---|---|---|---|
| 1 | **¿Puedo creerle a esta pantalla?** | 0 | Si el dato está viejo, todo lo demás se lee con esa salvedad. Hoy esta pregunta no tiene respuesta en el dashboard, y la topbar la responde mal |
| 2 | **¿Hay algo roto y dónde?** | 1 | Agrupado por sitio: "7 farmacias afectadas" dimensiona la jornada mucho mejor que "27 alertas" |
| 3 | **¿Cuál primero?** | 1 (orden) | Enlace caído (no se vende) → POS crítico (no se cobra) → alerta crítica (se degrada) → advertencia |
| 4 | **¿Hay algo que mañana será un problema?** | 4-7 | Enrolamiento, cumplimiento, hardware. Disponible, sin competir por la atención |

### Por qué la cola unificada de la Zona 1

Es el cambio de mayor efecto sobre la lectura, y sale directamente de
[04 · D](04-riesgos-del-encargo.md#d--alertas): una farmacia con el enlace caído aparece hoy
**tres veces** en la misma pantalla —evento de enlace, alertas `sin_heartbeat` por cada
caja, y filas en "estaciones sin reportar"— para un único hecho.

La cola unificada agrupa por farmacia y muestra el síntoma de mayor impacto con el recuento
de los demás ("3 cajas"). No pierde información: cada fila enlaza al detalle, que es donde
ya existe el desglose.

**Coste real:** es la pieza de más trabajo del rediseño, porque implica fusionar tres
querysets con un criterio de prioridad. Pertenece a la Fase B, no a la A.

---

## 6.4 Zona 0 en detalle: la regla de los cuatro estados

El encargo pide distinguir siempre estado real confirmado, último estado conocido, ausencia
de datos y error del mecanismo. Así se aplica, con lo que hoy se puede afirmar:

| Fila | Dato que la alimenta | Verde cuando | Ámbar cuando | Rojo cuando | Gris cuando |
|---|---|---|---|---|---|
| Worker MQTT | `WorkerHeartbeat['mqtt_worker']` | latido < 90 s **y** conectado al broker (`NO EXISTE`, Fase D) | latido < 90 s, conexión **sin verificar** | latido > 90 s o nunca | — |
| Último latido de la flota | `max(Estacion.ultimo_heartbeat)` (`NO EXISTE`) | < 2 min | < 10 min | > 10 min con estaciones aprobadas | sin estaciones aprobadas |
| Mensajes fallidos | `MensajeMqttFallido(revisado=False)` (`NO EXISTE` en panel) | 0 | 1-9 | 10 o más | — |
| Sondeo de enlaces | `max(EstadoEnlaceFarmacia.ultima_verificacion)` | dentro de `TOLERANCIA_FRESCURA_MINUTOS['enlaces']` = 6 min | — | fuera de tolerancia | ninguna farmacia con `ip_router` |
| Beat (inferido) | `max(MuestraRedFarmacia.timestamp)` | < 20 min (`BEAT_UMBRAL_MINUTOS`) | — | > 20 min | sin muestras |
| Respaldo | `WorkerHeartbeat['respaldo']` | < 26 h (`RESPALDO_UMBRAL_HORAS`) | — | > 26 h **o nunca** | — |
| Broker EMQX | `NO EXISTE` (Fase D) | — | — | — | **"sin verificar"** hasta que exista |

Tres reglas que no se negocian, y que el proyecto ya aplica en otros sitios:

1. **Ningún componente sin mecanismo de comprobación se pinta verde.** Se pinta gris con la
   palabra "sin verificar". Hoy EMQX entraría así, y eso es información útil: dice que falta
   construirlo.
2. **Cada fila muestra la hora del último dato, no la del cálculo de la pantalla.** Son dos
   relojes distintos y los dos tienen que estar visibles (ver [07 · 7.4](07-rendimiento.md)).
3. **Todo estado lleva palabra, no solo color.** Comprobable quitando el CSS de color: la
   pantalla debe seguir leyéndose.

---

## 6.5 Inventario del cambio

La versión completa, indicador por indicador, está en
[03 · 3.3](03-auditoria-indicadores.md#33-qué-se-mantiene-qué-cambia-qué-se-elimina).
Resumen por zona:

| Zona | De dónde sale | Trabajo |
|---|---|---|
| 0 · Confianza | Avisos del dashboard + `verificar_salud` + 3 indicadores nuevos | **Nuevo** (Fase A el marco, Fase D lo que falta medir) |
| 1 · Incidencias | Fusión de `criticas` + `advertencias` + `enlaces_caidos` + `pos_criticos` | **Nuevo** (Fase B) |
| 2 · Indicadores | KPI del dashboard, corregidos, + I-18 y I-21 | Fase A reordena, Fase B corrige |
| 3 · Conectividad | **Tal cual del Centro** (`_agrupar_por_proveedor`) | Mover |
| 4 · Enrolamiento | Aviso del dashboard + I-20 | Fase A |
| 5 · Estaciones | `sin_reportar` partido en cuatro | Fase B |
| 6 · Cumplimiento | **Tal cual del dashboard**, plegado | Fase A |
| 7 · Hardware | **Tal cual del Centro** | Mover |

**Lo que no se toca:** la lógica de dominio. Ni `registrar_sondeo`, ni
`abrir_o_mantener_alerta`, ni `manejar_heartbeat`, ni ninguna regla de alerta. Las Fases A-C
no cambian cómo se detecta nada; cambian qué se muestra y cómo se cuenta lo ya detectado.

---

## 6.6 Escritorio y resoluciones menores

| Ancho | Distribución | Cambio de CSS necesario |
|---|---|---|
| **≥ 1600px** | Zona 1 a dos columnas (cola de incidencias \| conectividad). `--page-max: 1600px` ya evita el estiramiento | Ninguno |
| **1180-1600px** (el caso normal) | Zona 1 a una columna, Zona 2 en 4 tarjetas, Zonas 3-4 en dos columnas | Ninguno: `auto-fit minmax(190px)` ya lo resuelve |
| **860-1180px** | Zona 2 a 2×2, Zonas 3-4 apiladas | Ninguno |
| **< 860px** (tablet de supervisión) | Todo a una columna; Zonas 6-7 plegadas | Las tablas → listas de definición, patrón ya presente en el panel |

**Pantalla mural:** si alguna vez se cuelga un monitor en la mesa de ayuda, Zonas 0-2 solas
con tipografía mayor. **No proponerlo hasta que alguien lo pida**: no hay evidencia en el
código ni en la documentación de que ese caso de uso exista.

**Nada de esto requiere un framework nuevo.** HTMX y el CSS existente alcanzan, y el patrón
marco + parcial ya está probado en producción en el Centro.

---

## Lo que hay que decidir en esta parte

1. **¿Es correcto el esquema de ocho zonas y su orden?** En particular, la Zona 0 arriba de
   todo: ocupa espacio permanente para decir "todo bien" la mayor parte del tiempo. La
   alternativa es que solo aparezca cuando algo está mal — pero entonces vuelve el problema
   de no poder distinguir "verde" de "no comprobado".
2. **¿Se acepta la cola unificada por farmacia de la Zona 1?** Es el cambio con más trabajo
   detrás y el que más altera la lectura actual.
3. ¿La Zona 7 (hardware, eventos de Windows) se queda en esta pantalla o se mueve a una
   propia? Hoy está al final del Centro y su comentario justifica bien esa posición.

---

[← 05 Auditoría UX/UI](05-auditoria-ux.md) · [Índice](README.md) · [Siguiente: Rendimiento →](07-rendimiento.md)
