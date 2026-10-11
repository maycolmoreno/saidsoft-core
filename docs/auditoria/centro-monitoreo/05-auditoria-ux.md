# 05 · Auditoría UX/UI

[← 04 Riesgos del encargo](04-riesgos-del-encargo.md) · [Índice](README.md) · [Siguiente: Diseño propuesto →](06-diseno-propuesto.md)

Qué dificulta hoy la lectura de la pantalla, y qué está bien y no debe tocarse. El diseño
propuesto va en la [parte 06](06-diseno-propuesto.md).

---

## 5.1 Lo que dificulta la lectura

| # | Problema | Evidencia | Qué cuesta | Marca |
|---|---|---|---|---|
| `U-1` | **La afirmación falsa permanente** | `templates/panel/base.html:255` y `static_src/components.css:149` | Entrena a no creerle a la pantalla. Es el peor defecto posible en un NOC | CONFIRMADO |
| `U-2` | **Jerarquía invertida** | `templates/panel/dashboard.html` | Lo que se lee primero es lo menos urgente; una farmacia que no vende queda en el tercio inferior | CONFIRMADO |
| `U-3` | **Franja de 5 KPI con redundancia y sin estado** | `dashboard.html`, bloque `.kpi-strip` | Cinco tarjetas para tres datos. Solo una cambia de color según su valor | CONFIRMADO |
| `U-4` | **Color como único canal de estado** | `dashboard.html` usa `pill`; `_estado_chip.html` existe y el Centro sí lo usa | Inaccesible bajo deuteranopía y protanopía, en la pantalla que se deja abierta todo el día | CONFIRMADO |
| `U-5` | **Sin hora del dato ni auto-refresco** | `dashboard.html` sin `hx-trigger`; sin equivalente de `r.calculado_en` | Un tablero abierto desde las 8:00 muestra las 8:00 y no lo dice | CONFIRMADO |
| `U-6` | **Sin distinción entre "está bien" y "no se sabe"** | `dashboard.py` no consume `fuente_desactualizada()` | Barrido abortado = pantalla tranquila con datos de hace horas | CONFIRMADO |
| `U-7` | **Dos tableros compitiendo** | `dashboard.html` y `centro_monitoreo_partial.html` | Duplicación cognitiva: hay que saber en qué pantalla se está para interpretar el número | CONFIRMADO |
| `U-8` | **Listas sin el campo que decide** | `dashboard.html` | Hay que abrir el detalle para poder priorizar | DISEÑO |
| `U-9` | **La acción principal es publicar software** | `dashboard.html:6` | El único botón primario de un tablero de monitoreo lanza un despliegue | CONFIRMADO |
| `U-10` | **Ancho dedicado a lo menos urgente** | `dashboard.html`; `components.css:127` | La matriz usa el 100 % del ancho; con 5 KPI el `auto-fit minmax(190px)` deja huecos en 1600px | CONFIRMADO |

---

## 5.2 El detalle de los tres más graves

### `U-1` · La topbar afirma salud que nadie comprobó

```html
<!-- templates/panel/base.html:255 -->
<div class="sys-status"><span class="led"></span> Todos los sistemas operativos</div>
```

```css
/* static_src/components.css:149 */
.led{ width:7px; height:7px; border-radius:50%; background:var(--good); ... }
```

Texto fijo, LED siempre verde, **en todas las pantallas del panel**, para todos los
usuarios. Se muestra incluso mientras el aviso rojo *"Worker MQTT sin señal"* está tres
centímetros más abajo, en la misma pantalla, contradiciéndolo.

Lo que lo convierte en el hallazgo más claro del informe es que **contradice una regla que
el propio proyecto ya decidió y escribió tres veces**:

| Dónde | Qué dice |
|---|---|
| `monitoreo/services.py:1568` `fuente_desactualizada()` | `None` cuenta como desactualizado, *"porque un tablero en verde con datos viejos es peor que no tener tablero"* |
| `panel/umbrales.py:clasificar()` | Devuelve `sin_dato`, no `ok`: *"un recurso que no se pudo medir no es un recurso sano, y pintarlo verde diría que está bien algo que nadie midió"* |
| `templates/panel/_estado_chip.html` | Tiene un estado "sin dato" con el `title` *"No hay medición: no es lo mismo que estar bien"* |

La topbar es la única excepción, y es la que se ve siempre.

### `U-2` · Jerarquía invertida

Orden actual de `dashboard.html`, de arriba abajo:

```
1. Título + boton "+ Nuevo despliegue"
2. Aviso worker MQTT        (condicional, correcto que vaya arriba)
3. Aviso respaldo           (condicional, correcto)
4. Aviso pendientes         (condicional)
5. Franja de 5 KPI
6. MATRIZ DE CUMPLIMIENTO POR GRUPO     <- todo el ancho, tabla de 7 columnas
7. Enlaces caidos                        <- el fallo de mayor impacto
8. Alertas abiertas | Estaciones sin reportar   (dos columnas)
9. Despliegues en curso
```

La posición 6 es cumplimiento de versión del POS: un dato de **planificación de
despliegues**, que cambia de día en día, no de minuto en minuto. Ocupa la primera posición
del cuerpo y el ancho completo. Los enlaces caídos —cuyo propio comentario en la vista dice
*"una farmacia sin enlace no vende"*— van después.

El bloque 7 se añadió el 3-oct-2026 precisamente porque faltaba, y se colocó en el hueco
disponible en vez de en el primero. El comentario de la plantilla dice *"va ARRIBA de las
alertas y las estaciones"*, lo cual es cierto — pero está debajo de la matriz.

### `U-3` · La franja de KPI

| Tarjeta | Qué muestra | Problema |
|---|---|---|
| Estaciones en línea | `55/66` | Correcto como concepto; población mal definida (`D-02`) |
| Total de estaciones | `66` | **Repite el denominador de la tarjeta anterior** |
| Pendientes de aprobación | `3` | **Repite el aviso que está 100 px más arriba** |
| Despliegues activos | `2` | Útil |
| Alertas abiertas | `12` | **La única que cambia de color según su valor** |

Tres de las cinco tarjetas tienen un `tone-*` fijo en la plantilla (`tone-good`,
`tone-info`, `tone-warning`) que **no depende del valor**: la tarjeta de pendientes es
ámbar aunque el número sea 0, y la de estaciones en línea es verde aunque estén todas
caídas. El color decora, no informa.

---

## 5.3 Lo que está bien y no debe tocarse

Conviene nombrarlo explícitamente antes de proponer cambios, porque el riesgo de un
rediseño es borrar decisiones que están bien argumentadas.

### El sistema de diseño

- `static_src/tokens.css`: tema oscuro como identidad primaria y tema claro *"al mismo
  nivel de cuidado, no una inversión"*, con precedencia clara (`:root` →
  `prefers-color-scheme` → `[data-theme]`).
- `--serie` (gris azulado) **deliberadamente fuera de la paleta de estado**: verde, ámbar y
  rojo quedan reservados para "cómo está esto". Es exactamente el criterio correcto.
- `--page-max: 1600px` con justificación medida: *"sin tope, en un monitor de 27'' las
  tablas se estiran hasta 2400px y la vista salta del código de la estación a su último
  latido cruzando media pantalla vacía"*.

### `_estado_chip.html`

Color **+ forma + palabra**, con `sr-only` para el lector de pantalla y `title`
explicativo. Cinco estados, incluido `sin_umbral` ("ni regla ni defecto: el número se
muestra, pero nadie puede afirmar que esté bien") y `sin_dato`.

**Es el estándar correcto. El trabajo no es diseñar uno nuevo, es aplicarlo en el
dashboard.**

### Las decisiones del Centro

| Decisión | Por qué está bien |
|---|---|
| Solo lectura | *"una pantalla que se mira de reojo no puede tener una acción destructiva a un clic"*. Coherente con el bot de Telegram |
| Refresco de 60 s, no 10 | Derivado de la cadencia real del backend, con prueba que lo fija |
| Animación de entrada suave, sin sonido ni parpadeo | *"algo que llama la atención cada minuto se vuelve ruido"* |
| Ventanas de mantenimiento **arriba de todo** | *"si hay una ventana activa, lo que sigue puede estar caído por diseño"* |
| "Farmacias afectadas" junto a los contadores de síntomas | *"27 advertencias pueden ser 27 sitios o uno con 27 síntomas"* |
| Administrativas contadas aparte | Se apagan de noche; sumarlas haría saltar el contador ~100 todas las noches |
| Eventos de Windows **al final** | *"casi nunca exige acción en el momento"* |
| Recorte a N filas con el total aparte | *"es un tablero, no un listado"* |
| Marca "dato posiblemente viejo" por bloque | Para no teñir toda la pantalla cuando una sola tarea se cae |

### Los comentarios de código y plantilla

Buena parte de esta auditoría fue posible porque cada decisión no obvia tiene su por qué y
su fecha. **Cualquier cambio debería mantener ese estándar**, incluida la razón de por qué
se movió cada bloque.

---

## 5.4 Lo que el encargo pide evitar, y cómo queda

El encargo enumera diez cosas a evitar. Estado actual y compromiso del rediseño:

| A evitar | Estado hoy | En la propuesta |
|---|---|---|
| Exceso de tarjetas | 5 KPI con 3 datos | 4 tarjetas, cada una con estado real |
| Grandes espacios vacíos | `auto-fit` deja huecos con 5 tarjetas en 1600px | 4 tarjetas encajan en el grid existente |
| Tablas innecesariamente extensas | Matriz de 7 columnas en primera posición | Plegada, al final |
| Iconos decorativos sin significado | Los `kpi-icon` con `tone-*` fijo no informan | Chips de estado que cambian con el valor |
| Animaciones que distraigan | La del Centro está bien calibrada | Se conserva tal cual |
| Gráficos que no permitan decidir | **No hay ninguno hoy**, y el Centro explica por qué (no hay histórico del resumen) | **No se añade ninguno.** Sigue pendiente en PLAN_MODERNIZACION §10-AK |
| Semáforos que oculten el significado | `pill` sin texto de estado en el dashboard | Chip con palabra, siempre |
| Uso indiscriminado del rojo | 3 avisos `sev-critical` seguidos pueden apilarse arriba | Rojo solo para condición crítica confirmada; ámbar para degradación |
| Información duplicada | Pendientes ×2; total de estaciones ×2; enlaces en dos pantallas | Se elimina la duplicación; las dos vistas de enlaces se conservan **porque sirven a usos distintos** (ver `X-8`) |
| Cambiar la identidad visual | — | **No se cambia nada**: mismos tokens, mismos componentes |
| Framework de frontend nuevo | HTMX + CSS propio | **No se introduce ninguno.** Ver [parte 07](07-rendimiento.md) |

---

## Lo que hay que decidir en esta parte

1. **¿Se acepta corregir `U-1` (la topbar)?** Es el cambio más pequeño y el de mayor efecto
   sobre la confianza. Implica un context processor nuevo que corre en todas las páginas
   del panel: ver `X-5`.
2. **¿Se acepta el reordenamiento de `U-2`?** Concretamente: mover la matriz de
   cumplimiento al final y plegada.
3. ¿Se acepta eliminar los dos KPI redundantes y mover el botón de nuevo despliegue?
4. ¿Hay alguna decisión de la sección 5.3 que el equipo quiera revisar de todos modos?

---

[← 04 Riesgos del encargo](04-riesgos-del-encargo.md) · [Índice](README.md) · [Siguiente: Diseño propuesto →](06-diseno-propuesto.md)
