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

Sobre los valores concretos que la maqueta declaraba como "los que muestra hoy SAIDSOFT"
(38/66, 5 enlaces, 9 farmacias, 5 alertas, 28 sin reportar…): durante la auditoría no se pudieron
confirmar, porque las lecturas contra producción estaban **denegadas por el clasificador de
permisos** y no se intentó rodearlas. Se midieron al terminar (§8), y conviene no leer esa
coincidencia como validación general: **5 alertas abiertas** coincide, pero resultan estar todas
en la cubeta de más de 7 días; los enlaces caídos eran **4** en el momento de medir, no 5, y ese
número cambia hora a hora. El "18× apagón inesperado en 24 h" de `GLG01-ADM` **sí existe como
fila pero no como dato de 24 horas**: son 18 ocurrencias acumuladas desde semanas antes (§8.6).

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

**Dos factores más, descubiertos al medir** (ver §8.2 y §9), que la decisión tiene que
contemplar y que no estaban en el análisis inicial:

1. **`EventoMonitoreo` tiene DOS fuentes.** En las últimas 24 h hubo 59 transiciones de `mqtt` y
   44 de `meshcentral`. El KPI `estado_flota` sale de `Estacion.ultimo_heartbeat`, que es el
   latido MQTT, así que una serie que mezclara las dos fuentes contaría en línea a estaciones
   que el KPI cuenta caídas — y ese cruce es precisamente la señal que
   `evaluar_cruce_monitoreo` usa para detectar "el agente se cayó pero la red vive". La serie
   tiene que filtrar `fuente='mqtt'` o declarar que mide otra cosa.
2. **El efecto nocturno es masivo y la protección contra él no funciona** (§9). Medido a las
   23:29, con las farmacias cerradas: 29 de 66 estaciones en línea. Una serie de 24 h caería a
   ~44 % todas las noches sin que nada esté roto, y la maqueta dibujaba una línea de "SLA 90 %"
   sobre esa curva. Sin distinguir horario de operación, la gráfica mostraría una caída masiva
   diaria que no es una caída.

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

**Medido después (§8.4): el universo por agente son 38 farmacias** (MIA 24, SG 14) contra ~697
sondeadas por enlace. 38 celdas se leen de un vistazo y es casi lo que la maqueta dibujaba; ~697
no. El dato que faltaba para decidir esta fase ya está.

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
| D | Serie de disponibilidad de 24 h | **Pendiente** — el dato existe y es barato (§8.2); falta **decidir** §4: umbral, fuente y efecto nocturno |
| E | Mosaico de farmacias | **Pendiente** — el universo ya está medido en 38 (§8.4); falta **decidir** si es ese |
| F | Sparklines históricos de los KPI | **Pendiente** — depende de D |
| G | Histórico temporal de eventos de Windows | **No recomendada** — exige modelo nuevo y cambio en el agente, y la aproximación mentiría un 42 % (§3, §8.6) |

**La evolución del modo NOC no está terminada.** Las autorizaciones cubrieron A, B y C.

Después de medir, **D y E ya no están bloqueadas por falta de datos sino por una decisión**: los
números que faltaban están en §8. D arrastra además el hallazgo de §9, que conviene resolver
antes o junto con ella.

## 8. Mediciones contra producción (12-oct-2026, 23:29 ECT)

Durante la auditoría las lecturas contra producción estaban **denegadas por el clasificador de
permisos**, así que los siete puntos quedaron sin verificar. Se midieron después de desplegar
A/B/C (`1b9f842`), todas con consultas de solo lectura a través del ORM. Esto es lo que hay.

### 8.1 TimescaleDB: aplicado, y la ventana caliente está sin comprimir

`timescaledb 2.17.2`. Las cuatro hypertables existen con sus dos políticas:

| Hypertable | Chunks | Retención | Compresión |
|---|---|---|---|
| `evento_monitoreo` | 5 (3 comprimidos) | 30 días | a los 7 días |
| `muestra_metrica` | 27 | 30 días | a los 7 días |
| `muestra_red_farmacia` | 5 | 30 días | a los 7 días |
| `muestra_servicio_pos` | 26 | 30 días | a los 7 días |

**HV** — De los 5 chunks de `evento_monitoreo`, 3 están comprimidos y 2 no: la ventana de las
últimas 24 h cae siempre en chunk sin comprimir, que es el caso óptimo. Lo que la auditoría
había inferido de las migraciones 0035 y 0040 se confirma en el servidor.

### 8.2 `evento_monitoreo`: la Fase D es viable y barata

| Medición | Valor |
|---|---|
| Filas totales | 2.889 |
| Rango | 10-sep-2026 → 13-oct-2026 (33 días, coherente con la retención de 30) |
| **Últimas 24 h** | **103 filas** |
| Últimas 48 h / 7 días | 196 / 899 |
| Estaciones distintas con transición en 24 h | 31 (de 66) |
| Transiciones a offline / a online en 24 h | 56 / 47 |
| Por fuente en 24 h | `mqtt` 59, `meshcentral` 44 |

**HV** — 103 filas para reconstruir una serie de 24 puntos es trivial en coste. El dato existe,
está fresco y la tabla está indexada por `(estacion, -timestamp)`. **Técnicamente la Fase D se
puede hacer.**

### 8.3 Alertas abiertas: las cinco son deuda, ninguna es un incidente

| Cubeta | Total |
|---|---|
| menos de 1 h | 0 |
| 1 – 24 h | 0 |
| 1 – 7 días | 0 |
| **más de 7 días** | **5** |

Las 5 sin reconocer, 0 en atención. La más vieja abierta desde el **28-sep-2026** (15 días).

**Es el valor que la Fase B vino a aportar, confirmado con datos reales:** el tablero venía
diciendo "5 alertas sin reconocer", que se lee como urgencia, y las cinco son deuda acumulada.
Ninguna requiere que alguien corra hoy.

### 8.4 Farmacias: el universo del mosaico son 38, no 700

| Medición | Valor |
|---|---|
| Farmacias totales / activas | 703 / 702 |
| Activas con `ip_router` (sondeables) | 702 |
| Con fila en `EstadoEnlaceFarmacia` | 703 |
| De esas, `respondio_alguna_vez=False` | 6 → **~697 sondeadas de verdad** |
| **Activas con ≥1 estación APROBADA** | **38** |
| Reparto por unidad (activas) | SG 393, MIA 307, 7DIAS 2 |
| Reparto de las 38 con agente | MIA 24, SG 14 |

**HV — Esto decide la Fase E.** Un mosaico por enlace son ~697 celdas: ilegible. Un mosaico por
agente son **38**, repartidas en dos clientes, que es casi exactamente lo que la maqueta dibujaba
(mostraba 30). 38 celdas se leen de un vistazo.

### 8.5 Enlaces caídos en curso

4 caídas, las cuatro de TELCONET, de 39, 83, 138 y 206 minutos. **Ninguna recortada**: las cuatro
caben dentro de la ventana de 6 h. El bloque de la Fase C las mostró con el orden correcto (lo
más reciente arriba).

### 8.6 `evento_sistema_detectado`: cuánto mentiría un "24 h" aproximado

| Medición | Valor |
|---|---|
| Filas | 43 |
| Ocurrencias acumuladas (`cantidad_total`) | 287 |
| Rango de `primera_vez` | 21-sep → 12-oct-2026 |
| Filas con `ultima_vez` en 24 h | 16, sumando 43 ocurrencias |
| **De esas, filas que EMPEZARON antes de 24 h** | **1, que aporta 18 ocurrencias** |

**HV — La aproximación está cuantificada: 18 de 43, el 42 % del número, sería histórico
disfrazado de "últimas 24 h".** Y es exactamente la fila que la maqueta mostraba como
"GLG01-ADM · 18× apagón inesperado → revisar UPS/energía": esos 18 apagones no son de las
últimas 24 horas, son el acumulado desde que se empezó a recolectar.

Confirma que **la Fase G no se puede aproximar**: hace falta la serie de ocurrencias.

## 9. Hallazgo nuevo: la exclusión de equipos administrativos no excluye nada

Esto no estaba en la auditoría original; salió de medir el punto 8.4.

**HV** — `catalogo.services.estado_flota` y la lista de "estaciones sin reportar" del tablero
excluyen las estaciones cuya **farmacia** es de tipo `ADMINISTRATIVO`. El comentario que
justifica esa exclusión dice:

> *"Se apagan de noche y el fin de semana, así que sumadas harían saltar esta lista ~100 todas
> las noches y el número dejaría de significar 'hay un problema'."*

**La protección no opera.** Medido:

- **No existe ninguna farmacia de tipo `ADMINISTRATIVO`**: 701 son `farmacia` y 2 son `tienda`.
- `estado_flota(None)` devuelve **`admin_total = 0`**, así que las tres cifras que el diccionario
  calcula para administrativas son constantes en cero.
- De las 66 estaciones aprobadas, **23 llevan `-ADM` en el código** (`GLG01-ADM`, `GSD01-ADM`,
  `GSD02-ADM`…), que son los equipos de oficina del administrador de cada local. **Las 23 están
  en farmacias normales**, así que se cuentan como flota.

El efecto, medido a las **23:29 de Ecuador** (farmacias cerradas):

| Grupo | Total | En línea |
|---|---|---|
| Estaciones `-ADM` | 23 | 11 |
| Resto | 43 | 18 |
| **`estado_flota`** | **66** | **29** · 37 sin reportar, 0 nunca reportó |

O sea: de noche el tablero muestra **29/66 en línea y 37 "sin reportar"**, y ninguna de esas 37
es una incidencia — son locales cerrados. 12 son equipos `-ADM`, los que la exclusión pretendía
sacar; las otras 25 son cajas de farmacias que cerraron.

**Dos consecuencias.**

1. El fenómeno nocturno es **más amplio que los `-ADM`**: incluso excluyéndolos, 25 de 43 cajas
   están apagadas. El criterio por tipo de farmacia no habría alcanzado ni funcionando.
2. **Es un insumo crítico para la Fase D.** Una serie de disponibilidad de 24 h caería a ~44 %
   todas las noches sin que nada esté roto, y la maqueta dibujaba una línea de "SLA 90 %" sobre
   esa curva. Graficar la flota sin distinguir horario de operación produciría una gráfica que
   parece una caída masiva diaria.

No se corrigió nada de esto: estaba fuera del alcance autorizado (A, B y C) y toca un umbral y
un criterio de conteo que el proyecto fijó deliberadamente. Queda anotado como **decisión
pendiente**, y conviene resolverlo antes o junto con la Fase D.
