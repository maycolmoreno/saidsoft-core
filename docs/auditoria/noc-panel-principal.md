# Auditoría del panel principal para modo NOC

Auditoría de solo lectura del tablero (`/`) contra una maqueta de propuesta de modo NOC,
realizada el 12-oct-2026 sobre el commit `221b7d4`. Este documento es la **base de trabajo de
las fases pendientes (D–G)**: la implementación de A, B y C y sus decisiones están en
`PLAN_MODERNIZACION.md` §10-BX.

Las etiquetas son las de la auditoría: **HV** hecho verificado (con ruta y símbolo),
**IT** inferencia técnica, **PV** pendiente de verificación.

## 1. La conclusión de fondo

**El panel ya era un NOC en casi todo lo que la maqueta proponía, menos en una cosa: no tenía
memoria.**

**HV** — El tablero ya tenía tema oscuro (`--bg:#0b1220`, `color-scheme:dark` en
`static/css/app.css`), refresco automático de 60 s, zonas ordenadas por pregunta operativa,
cola unificada de incidencias por farmacia, franja de salud con nueve señales y aviso de
pérdida de contacto con el servidor. La maqueta reproducía todo eso.

El delta real era **histórico y densidad, no estética**. De ahí que la intervención no tocara
la paleta ni reemplazara nada: las fases A/B/C se montaron sobre la pantalla existente.

**HV** — La ausencia de series ya estaba identificada como pendiente antes de esta auditoría.
`templates/panel/centro_monitoreo_partial.html` lo dice literalmente: *"No llevan sparkline:
`resumen_operacion` es un conteo del momento, no una serie. Para una tendencia de estos números
haría falta guardar el histórico del resumen, que hoy no existe — anotado como pendiente en
PLAN_MODERNIZACION §10-AK."* Y §10-AK lo cierra con *"modelo nuevo, decisión del usuario"*.

**HV** — La maqueta generaba sus series con una función pseudoaleatoria
(`Math.sin((i+seed)/3)*amp + ((i*7919+seed*31)%11-5)*amp/8`), que alimentaba la curva de 24 h y
los cinco sparklines. No se portó nada de eso, y cada bloque implementado tiene una prueba de
que con la base vacía muestra su estado vacío en vez de una barra.

## 2. Matriz: cada indicador de la maqueta contra el dato real

| # | Indicador de la maqueta | ¿Dato real? | Evidencia | Fase |
|---|---|---|---|---|
| 1 | 9 chips de salud de plataforma | **Sí, ya en pantalla** | `monitoreo.salud.estado_latidos()` | **A (hecha)** |
| 2 | Estaciones en línea | **Sí** | `catalogo.services.estado_flota()` | ya existía |
| 3 | Farmacias sin enlace | **Sí** | `total_enlaces_caidos` en la vista del tablero | ya existía |
| 4 | Farmacias afectadas | **Sí** | `monitoreo.incidencias.cola_incidencias()` | ya existía |
| 5 | Alertas sin reconocer / en atención | **Sí** | `conteo_alertas` en la vista | ya existía |
| 6 | Estaciones sin reportar | **Sí** | `estado_flota()['sin_reportar']` | ya existía |
| 7 | Antigüedad de la más vieja | **Sí** | `Alerta.abierta_en` | **B (hecha)** |
| 8 | Histograma de antigüedad (4 cubetas) | **Sí, exacto** | `antiguedad_alertas_sin_resolver()` | **B (hecha)** |
| 9 | Serie 24 h de disponibilidad | **Reconstruible**, con conflicto de umbral (§4) | `EventoMonitoreo` | D |
| 10 | Sparkline "farmacias sin enlace" | **Sí, exacto** | `EventoEnlaceFarmacia.inicio`/`fin` | F |
| 11 | Sparkline "alertas sin reconocer" | **Reconstruible** | `abierta_en`/`reconocida_en`/`resuelta_en` | F |
| 12 | Sparkline "estaciones sin reportar" | **Sí**, complemento de #9 | mismo cálculo que #9 | F |
| 13 | Sparkline "farmacias afectadas" | **Parcial y caro** | dos de sus tres fuentes son snapshots sin historia | F (no recomendado) |
| 14 | Línea de tiempo de enlaces caídos | **Sí, exacto** | `linea_tiempo_caidas_en_curso()` | **C (hecha)** |
| 15 | Mosaico: farmacias con incidencia | **Sí** | `cola_incidencias()` | E |
| 16 | Mosaico: farmacias operativas | **Universo sin definir** (§5) | — | E |
| 17 | Eventos de Windows por tipo (24 h) | **No** | `cantidad_total` acumulativo (§3) | G |
| 18 | Equipos más ruidosos (24 h) | **No** | mismo | G |
| 19 | Selector por cliente | **Sí, ya existe** | `cuentas.services.unidades_negocio_en_foco()` | ya existía |
| 20 | Selector de ventana temporal | **No existe** | requiere parámetro nuevo | D |

**PV** — Ninguno de los valores concretos que la maqueta declaraba como "los que muestra hoy
SAIDSOFT" (38/66, 5 enlaces, 9 farmacias, 5 alertas, 28 sin reportar, 4 MQTT fallidos, respaldo
hace 19 h) pudo confirmarse: las lecturas contra producción fueron **denegadas por el
clasificador de permisos** y no se intentó rodearlas. Tampoco se verificó que las estaciones
citadas (`GEC12`, `GSD12`, `MC001`, `MSI01`, `GLG01-ADM`…) existan con esos estados.

## 3. El dato que no existe: ocurrencias de eventos de Windows

**HV** — `monitoreo.services.ingerir_eventos_sistema()` hace `get_or_create` por
`(estacion, log, origen, identificador)` y luego `cantidad_total = F('cantidad_total') + cantidad`,
sobrescribiendo `ultima_vez`. El docstring de `EventoSistemaDetectado` justifica la decisión:
*"Una sola máquina generó 260 `Ntfs 55` en 30 días; a 1.800 estaciones eso es medio millón de
filas mensuales que nadie lee."*

La decisión es correcta para su propósito y **destruye la dimensión temporal**. Consecuencia
concreta: filtrar `ultima_vez >= ahora - 24 h` y sumar `cantidad_total` devuelve un número que
incluye ocurrencias de hace semanas, así que rotularlo "24 h" sería un dato falso en pantalla.
Es el mismo problema que §9 del plan documenta para `PosErrorDetectado`.

`monitoreo.services.eventos_sistema_recientes()` ya evita el problema: ordena por `-ultima_vez`
y no promete ventana.

**Para la Fase G** harían falta una tabla de ocurrencias (o cubetas horarias), un cambio en el
reporte del agente, migración, ingesta y retención nuevas. No recomendada por ahora.

## 4. El conflicto que bloquea la Fase D

**HV** — La serie de 24 h **sí** es reconstruible y barata:

- `registrar_estado_dispositivo()` escribe en `EventoMonitoreo` solo cuando `en_linea` cambia, y
  `catalogo.services.marcar_estaciones_offline()` cierra el circuito llamándola con
  `en_linea=False`. Las caídas dejan rastro.
- La migración `monitoreo/0035_hypertables_de_verdad.py` convierte `evento_monitoreo` en
  hypertable (chunks de 7 días, retención 30) y la `0040_compresion_hypertables.py`
  **comprime a los 7 días**, con `segmentby = estacion_id, fuente` (110× medido) y
  `orderby timestamp DESC`. Las últimas 24 h caen siempre en chunks sin comprimir: el caso
  óptimo.

**HV — El problema.** `marcar_estaciones_offline()` usa por defecto el umbral de **alerta**
(`ALERTA_SIN_LATIDO_UMBRAL_MINUTOS = 5`), mientras los KPI de pantalla usan el de
**visualización** (`ONLINE_UMBRAL_MINUTOS = 10`). Una serie reconstruida desde `EventoMonitoreo`
habla con el reloj de 5 minutos. La regla vigente en `monitoreo/umbrales.py` es explícita:

> *"el umbral de VISUALIZACIÓN es el único que decide lo que se cuenta en una pantalla; el de
> ALERTA es el único que decide cuándo se abre una alerta."*

**IT** — El síntoma sería que el último punto de la curva no coincide con el número grande de al
lado, en la misma tarjeta y en el mismo instante: exactamente la contradicción que el hallazgo
D-01 vino a cerrar. La maqueta además rotulaba la curva *"% de la flota con latido en los
últimos 10 min"*, que es lo que la serie reconstruida **no** mediría.

Tres salidas, a decidir antes de escribir código:

| Opción | Qué implica | Coste |
|---|---|---|
| **D-1** | Graficar con el reloj de 5 min y rotularlo así. Honesto, sin tocar nada; curva y KPI miden cosas distintas y se dice | bajo |
| **D-2** | Reconstruir el estado a 10 min desde las transiciones: un punto = "¿tuvo latido en los 10 min previos?". Coincide con el KPI | medio |
| **D-3** | Persistir un snapshot horario del resumen (lo que §10-AK dejó como decisión del usuario). Resuelve también la Fase F | alto |

Recomendación de la auditoría: **D-2**, el único que hace coincidir la curva con el número de al
lado sin crear un modelo. Conviene medir su coste real antes de fijarlo en el refresco de 60 s.

## 5. El universo del mosaico (Fase E)

**HV** — `CLAUDE.md` registra **701 farmacias modeladas y 66 equipos con agente** de ~1.800
estimados. El docstring de `EventoEnlaceFarmacia` documenta que, medido el 18-sep-2026,
*"690 de las 700 farmacias no tenían ningún hijo —solo 10 tienen estaciones—"*.

La maqueta dibujaba 30 celdas en dos clientes y mezclaba dos criterios en el mismo cuadrito:
`MP009` "sin enlace" (sondeo ICMP, aplica a ~700 sitios) y `MC001` "agente caído" (aplica a los
pocos con estaciones). Son dos poblaciones con dos órdenes de magnitud de diferencia. Un mosaico
de 701 celdas no se lee; uno de 30 no representa el parque de enlaces. Por eso la propia maqueta
admitía que sus verdes eran ilustrativos.

Antecedente a no repetir: `/monitoreo/` sigue sin paginar y a 700 farmacias son 700 tarjetas
(§10-AK).

**PV** — Cuántas farmacias activas hay hoy, cuántas se sondean y cuántas tienen al menos una
estación aprobada. **Es el dato que define esta fase.**

## 6. Rendimiento y seguridad del tablero

**IT** — `_contexto_vivo` hacía del orden de 12–14 consultas por refresco antes de esta
intervención (conteo por lectura de código, no medido); las fases B y C añadieron **una cada
una**, con prueba propia de `assertNumQueries(1)`.

**HV** — No hay un `assertNumQueries` absoluto para el tablero completo:
`TableroRefrescoYCostoTests` fija el presupuesto de forma relativa. Cualquier bloque nuevo
debería llegar con presupuesto absoluto, como hicieron B y C.

**HV** — `DivisionDeVistasTests` impone 550 líneas por módulo de vistas, con la regla escrita:
*"Si un módulo lo pasa, la respuesta es dividirlo, no subir el límite."* `dashboard.py` quedó en
373 líneas. El cálculo de series **no** cabe ahí y, por la arquitectura vigente, tampoco debe:
va en `apps/monitoreo/`.

**HV** — `dashboard` es `@login_required` sin permiso adicional; `centro_monitoreo` exige
`monitoreo.view_alerta`. La Zona 0 se muestra **sin filtro de tenant a propósito**, documentado
en la vista: *"es salud de la infraestructura, no dato de cliente... Acotarlas al alcance del
usuario haría que un cliente sin estaciones leyera 'sin datos' y concluyera que la plataforma
está caída."*

**IT** — Riesgo a vigilar en D/E/G: mosaico, línea de tiempo y panel de equipos ruidosos exponen
códigos de farmacia y de estación, que **sí** son dato de cliente, y deben filtrarse por
`unidades_negocio_en_foco` como ya hacen la cola y los dos bloques implementados. El riesgo es
de omisión, no de diseño: la maqueta mostraba dos clientes juntos sin indicar que el filtro
aplica.

## 7. Estado de las siete fases

| Fase | Qué es | Estado |
|---|---|---|
| A | Franja de salud compacta | **Implementada** (§10-BX) |
| B | Histograma de antigüedad de alertas | **Implementada** (§10-BX) |
| C | Línea de tiempo de enlaces caídos | **Implementada** (§10-BX) |
| D | Serie de disponibilidad de 24 h | **Pendiente** — requiere decidir §4 |
| E | Mosaico de farmacias | **Pendiente** — requiere definir §5 |
| F | Sparklines históricos de los KPI | **Pendiente** — depende de D |
| G | Histórico temporal de eventos de Windows | **Pendiente** — requiere modelo nuevo y cambio en el agente (§3) |

**La evolución del modo NOC no está terminada.** Las autorizaciones cubrieron A, B y C.

## 8. Lo que queda sin verificar

Las lecturas contra producción fueron denegadas por el clasificador de permisos. Siguen sin
medir, todas con consultas de solo lectura:

1. Que TimescaleDB esté activo y las cuatro hypertables con sus políticas realmente aplicadas
   (hay evidencia de las migraciones 0035 y 0040, no del estado del servidor).
2. Volumen y rango real de `evento_monitoreo` — **decide si la Fase D es viable**.
3. Los valores concretos que la maqueta declaraba como actuales.
4. Farmacias activas, sondeadas y con estación aprobada — **define la Fase E**.
5. Distribución real de antigüedad de alertas abiertas.
6. Caídas de enlace de las últimas 6 h.
7. Rango de `primera_vez`/`ultima_vez` en `evento_sistema_detectado`, para dimensionar cuánto
   distorsionaría un "24 h" aproximado.
