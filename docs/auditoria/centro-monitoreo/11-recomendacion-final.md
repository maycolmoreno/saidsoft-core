# 11 · Recomendación final

[← 10 Pruebas de aceptación](10-pruebas-aceptacion.md) · [Índice](README.md) · [Siguiente: Índice de hallazgos →](12-indice-hallazgos.md)

---

## La primera intervención

**No es rediseñar el dashboard. Es la Fase A, y dentro de ella, una sola línea:**

```html
<!-- templates/panel/base.html:255 -->
<div class="sys-status"><span class="led"></span> Todos los sistemas operativos</div>
```

```css
/* static_src/components.css:149 */
.led{ ... background:var(--good); ... }
```

---

## Por qué esta y no otra

### 1. Es la única afirmación falsa permanente del sistema

Todo lo demás que esta auditoría encontró es un número impreciso (`D-02`), una jerarquía
discutible (`U-2`), un dato sin marca de frescura (`D-07`) o una señal que existe y no se
muestra (`D-10`, `D-11`).

Esto es distinto: es **una pantalla afirmando que todo está bien sin haberlo comprobado
nunca**, en todas las vistas del panel, para todos los usuarios, incluso mientras el aviso
rojo *"Worker MQTT sin señal"* está tres centímetros más abajo contradiciéndola.

### 2. Contradice una regla que el proyecto ya decidió y escribió tres veces

| Dónde | Qué dice |
|---|---|
| `monitoreo/services.py:1568` | `None` cuenta como desactualizado, *"porque un tablero en verde con datos viejos es peor que no tener tablero"* |
| `panel/umbrales.py` | Devuelve `sin_dato` y no `ok`: *"un recurso que no se pudo medir no es un recurso sano, y pintarlo verde diría que está bien algo que nadie midió"* |
| `templates/panel/_estado_chip.html` | Tiene un estado "sin dato" cuyo `title` es *"No hay medición: no es lo mismo que estar bien"* |

El equipo ya tomó esta decisión y la documentó en tres lugares. **La topbar es la excepción,
y es la que se ve siempre.** Corregirla no es introducir un criterio nuevo: es aplicar el que
ya existe donde falta.

### 3. Cuesta poco y no puede romper nada

| Pieza | Estado |
|---|---|
| Umbrales | Ya centralizados en `apps/monitoreo/umbrales.py` |
| Chip de estado con color + forma + palabra | Ya existe: `_estado_chip.html` |
| Consulta necesaria | Una, a una tabla de ~3 filas, cacheable |
| Lógica de negocio tocada | **Ninguna** |
| Números que cambian de valor | **Ninguno** |

El único riesgo real es `X-5` —el context processor corre en todas las páginas— y se cierra
con una prueba de `assertNumQueries`.

### 4. Es la condición para que el resto sirva

Mejorar la jerarquía, unificar los indicadores y añadir refresco automático son inversiones
en una pantalla **a la que el operador ya aprendió a no creerle**. Arreglar la confianza
primero es lo que hace que las fases siguientes tengan dónde apoyarse.

---

## Después de eso, en orden

| Orden | Qué | Esfuerzo | Bloqueado por |
|---|---|---|---|
| 1 | El resto de la **Fase A**: jerarquía, chips, plegar la matriz, quitar los KPI redundantes | Días | Nada |
| 2 | **Fase B**: equivalencia de indicadores | Media-alta | **La decisión del usuario sobre el umbral de "en línea"** |
| 3 | **Fase C**: refresco por zona y rendimiento | Media | Fase B |
| 4 | **Fase D**: observabilidad real | Media-alta | Fase A, y medir `L-3` |

Las fases C y D deben **competir honestamente en prioridad con el rollout de agentes**. Lo
dice el propio `CLAUDE.md` y conviene repetirlo aquí: *"la plataforma está construida; el
despliegue no"* — 66 equipos con agente de ~1.800 estimados. **Un tablero mejor no instala
agentes.** Las fases A y B valen la pena porque el tablero existente miente; C y D son
mejoras sobre un tablero que ya funcionaría bien.

---

## Lo que esta auditoría necesita del usuario para continuar

Tres respuestas, por orden de impacto:

### 1. El umbral de "estación en línea" — **bloquea la Fase B**

| Opción | Efecto en la pantalla | Efecto en las alertas |
|---|---|---|
| **5 min para todo** | El Centro empieza a mostrar como caídas estaciones que hoy muestra en línea | Ninguno (ya es 5) |
| **10 min para todo** | El dashboard muestra en línea estaciones que hoy marca caídas | **Retrasa 5 min la apertura de alertas `sin_heartbeat`** |
| **Dos umbrales con nombre propio**: visualización 10 min, alerta 5 min ← *recomendado* | Ninguno respecto de hoy | Ninguno |

La tercera es la que menos mueve y la que más hay que documentar: deja dos valores, pero
**con dueño, nombre y motivo escrito**, en vez de tres accidentes repartidos entre una
vista, un servicio y una tarea. La condición es que el tablero diga cuál está usando.

### 2. ¿La mesa de ayuda usa hoy `/` o `/centro/`? — `L-5`

Una pregunta de una línea que puede cambiar el orden del plan: si ya migraron al Centro, la
Fase A se aplica **allí** primero y el dashboard pasa a ser el que redirige.

### 3. ¿Se consolidan los dos tableros, o se mantienen separados?

Si se mantienen separados, la Fase B sigue siendo igual de necesaria y urgente: **dos
tableros con cifras que no cuadran es peor que uno solo imperfecto.**

---

## Estado de este trabajo

- **No se modificó ningún archivo del repositorio.** Lo único creado son los trece archivos
  de esta carpeta.
- No se ejecutaron pruebas, ni migraciones, ni `manage.py shell`, ni consultas a producción.
  La única ejecución fue `python manage.py check` → *System check identified no issues
  (1 silenced)*.
- Los hallazgos marcados **CONFIRMADO** se sostienen por sí solos en el código citado.
  Los marcados **POR VALIDAR** tienen su consulta en
  [09 · Límites](09-riesgos-dependencias.md#l--límites-de-esta-auditoría).

**Esperando autorización para empezar la Fase A.**

---

[← 10 Pruebas de aceptación](10-pruebas-aceptacion.md) · [Índice](README.md) · [Siguiente: Índice de hallazgos →](12-indice-hallazgos.md)
