# Matriz de prioridad operativa SAIDSOFT 2030

**Fecha:** 27-sep-2026
**Qué es:** los 15 módulos, el agente y la infraestructura, clasificados por lo único que
importa para llegar a 1.300 farmacias: si están o no en el camino de poner la flota a
operar.
**Estado del repo al clasificar:** `HEAD = 0034f18`, rama `master`.

> **Documento hermano de** [`auditoria-capacidades.md`](auditoria-capacidades.md) (qué
> tiene hoy) y [`capacidad-2030.md`](capacidad-2030.md) (¿aguanta el crecimiento?).

---

## Los cuatro estados

| Estado | Significado |
|---|---|
| 🔴 **Camino crítico** | Sin esto no podemos desplegar ni operar la flota |
| 🟠 **Necesario para escalar** | Funciona hoy, pero debe fortalecerse antes de crecer |
| 🟡 **Aguas abajo** | Tiene valor cuando exista suficiente operación o datos |
| ⚪ **Prematuro** | Existe, pero todavía no tiene justificación operacional |

**Conteo:** 8 🔴 · 8 🟠 · 4 🟡 · 3 ⚪

> **Actualización del 28-sep-2026.** Con acceso al servidor se midieron las dos cifras que
> quedaban abiertas: `ActividadPlanificada` tiene **0 filas** y las zonas de viáticos son
> **0**. Las dos clasificaciones ⚪ quedan confirmadas, no revisadas. Ver el final.

---

## El dato que ordena toda la matriz

> **De las 8 cosas en camino crítico, 6 ya están hechas. Solo 2 bloquean, y ninguna es
> software:** el techo de conexiones del broker y la máquina donde corre todo.

El rollout no está esperando a que se programe nada — está esperando infraestructura.

---

## 🔴 Camino crítico

*8 elementos · 2 bloquean hoy*

| Módulo | Por qué es crítico | ¿Bloquea hoy? |
|---|---|---|
| `agente-prueba` | Única vía por la que entra información y salen acciones. Todo lo demás es consecuencia de que esto esté instalado | **No** — la 0.32 funciona |
| EMQX / broker | El canal con toda la flota. Nodo único por decisión explícita | **SÍ** — techo de conexiones y `ulimit` sin declarar |
| Infraestructura | Un solo NUC con los once contenedores, saliendo por WiFi. Es el piso de todo | **SÍ** — sin redundancia ni límites de recursos |
| `mqtt_worker` | Único consumidor de lo que reporta la flota. Si se detiene, el panel sigue andando y deja de entrar información | **No** — satura estimado hacia ~900 farmacias |
| `catalogo` | Identidad, enrolamiento y aprobación de cada estación. Una estación que no existe acá no existe en ningún lado | **No** |
| `despliegues` | Cómo se actualiza el agente en 1.800 máquinas sin visitar ninguna. Por olas, con aprobación y pausa | **No** — pero nunca se usó a escala real |
| `cuentas` | Autenticación, MFA y alcance multi-tenant. Sin esto nadie opera nada | **No** |
| `panel` | La única superficie donde se aprueba una estación, se ve la flota y se actúa | **No** |

---

## 🟠 Necesario para escalar

*8 elementos*

| Módulo | Qué lo hace frágil al crecer |
|---|---|
| `aperturas` | **La clasificación menos obvia de la matriz.** Parece administrativo y es el *vehículo de enrolamiento*: el agente se identifica con un `TokenApertura` y el perfil se aplica a la estación al enrolarse. Si van de 700 a 1.300 farmacias, son ~600 aperturas — la operación más repetida de la década, y por donde entra cada agente nuevo |
| `monitoreo` | Maduro y midiendo desde dentro de la farmacia. Lo que no está probado es el volumen: a 1.300 farmacias son cientos de mensajes por segundo y el ruido de alertas se multiplica por 1,86 sobre lo modelado hoy |
| `auditoria` | **Crece sin techo.** Solo cinco tablas tienen purga o retención y `evento_auditoria` no es una de ellas. Con 133 puntos de registro y 1.300 farmacias, es la tabla que nadie está mirando crecer |
| Respaldo y restauración | Diario y cifrado, sí. Pero `BACKUP_OFFSITE_DESTINO` sin definir deja la copia en la misma máquina que protege, y la restauración nunca se probó contra las hypertables |
| `activos` | El ciclo de vida está completo. Falta lo que el crecimiento vuelve caro: sin MAC en `Estacion`, sin relaciones entre activos, con 12 tipos fijos por enumeración |
| `mantenimiento` | La regla de "al menos un equipo" impide representar trabajo a nivel de farmacia, y el SLA se define pero no se mide. Las dos cosas duelen más cuanto más volumen hay |
| `scripts` | Aprobación, timeout y motivo de no-respuesta funcionan. Lo no probado es ejecutar sobre miles de estaciones a la vez contra un worker de un solo hilo |
| `movil-campo` | Cola offline y permisos reales desde el 26-sep. Escala con la cantidad de técnicos, no de farmacias — pero es la única vía de trabajo en sitio |

---

## 🟡 Aguas abajo

*4 elementos*

| Módulo | De qué depende para rendir |
|---|---|
| `software` | Catálogo, versiones e instalaciones remotas por estación, farmacia o grupo. Todo eso viaja por el agente: **no rinde nada hasta que el rollout ocurra** |
| Reportes y BI | MTTR, disponibilidad, ranking de proveedores y cumplimiento de SLA son consultas sin escribir sobre datos que ya se capturan. **Pero antes hay que decidir la retención**: con 30 días, ninguna pregunta histórica de más de un mes tiene respuesta posible |
| `cumplimiento` | Mide adherencia por estación, farmacia y colaborador. Necesita una operación cargada para que el número signifique algo |
| Diagnóstico con IA | Bien acotado —solo alertas críticas, una vez por incidente—. Necesita alertas fluyendo y alguien leyéndolas para justificar su costo por llamada |

---

## ⚪ Prematuro

*3 elementos*

| Módulo | Por qué |
|---|---|
| `viaticos` | Completo —bandeja, observación, reenvío, zonas, consolidado, CSV— y con **0 zonas cargadas** (medido el 28-sep) más 1 solo reporte, así que la alerta "fuera de zona" no puede dispararse nunca. Además es finanzas y RRHH, no operación de TI: no comparte camino con nada del anillo crítico |
| `facturacion` | La idea es correcta —cobrar por endpoint activo en vez de contrato fijo— y es la única tabla que se conserva indefinidamente, con razón. Pero su valor depende de un modelo de cobro entre unidades de negocio que **no se pudo verificar que exista**, y hoy paga un `get_or_create` en *cada latido* para una fila que cambia una vez al mes |
| `ActividadPlanificada` | El único caso real de redundancia en 97 modelos, y la única de las tres agendas sin API móvil ni historial. **Medido el 28-sep: 0 filas.** Sus tres capacidades propias —rango de fechas, actividad interna, trabajo sin equipo— son entonces teóricas: nadie las usó nunca. Queda como el candidato más limpio a retirarse. Ver [`tres-agendas.md`](tres-agendas.md) |

> **Prematuro no significa equivocado.** Los tres están bien construidos y probados.
> Significa que hoy no compiten por atención con nada del anillo crítico, y que
> congelarlos no cuesta nada.

---

## Cómo leer la matriz

**El color no mide calidad, mide urgencia.** Varios módulos en 🟡 y ⚪ están mejor
construidos que algunos en 🔴. La pregunta que responde cada fila no es "¿está bien
hecho?" sino "¿qué pasa si esto no avanza este año?".

**La lectura de conjunto:** el anillo crítico está esencialmente terminado en software y
detenido en infraestructura. Mientras el broker no acepte más conexiones y todo siga en
una sola máquina por WiFi, avanzar en cualquier otra fila no cambia el resultado — porque
el valor de `software`, `cumplimiento` y los reportes aparece recién cuando hay flota
operando.

**Y una advertencia sobre las tres 🟠 menos visibles:** `aperturas` es el vehículo del
rollout y suele leerse como administrativo; `evento_auditoria` crece sin techo y nadie la
está mirando; y el respaldo tiene copia única en la máquina que protege. Las tres son
baratas de atender ahora y caras después.

---

## Núcleo del producto

Una lectura complementaria a la matriz: **qué es el producto y qué lo rodea.** Conviene
separar "indispensable" de "insustituible".

### El anillo insustituible — lo que nadie vende hecho

- **El agente + `mqtt_worker` + EMQX.** Transporte propio, con comandos firmados y pausa
  remota. Es la única vía por la que entra información.
- **`catalogo`.** La jerarquía unidad → grupo → farmacia → estación, el enrolamiento y la
  aprobación. Todo lo demás cuelga de acá.
- **La medición desde adentro de la farmacia.** Que el agente lea el `.exe.Config` real
  del POS y pruebe sus cuatro servicios con `SELECT 1` desde la caja. Un enlace sano visto
  desde el datacenter puede estar roto para la sucursal — **eso es lo genuinamente
  diferenciado y ninguna herramienta de estante lo da**.

### El anillo indispensable pero de mercado

`monitoreo`, `activos`, `mantenimiento`, `scripts` + `despliegues`, `auditoria` +
`cuentas`, `panel`. Necesarios, y con alternativas comerciales.

> Si algún día hay que recortar, el primer anillo es el producto. El segundo es lo que lo
> hace usable.

---

## Lo incompleto, en dos categorías distintas

Vale separar dos cosas que se confunden:

**Media función construida** — el valor está en la mitad que falta:

- **Windows Update escanea y no instala.** El valor del módulo está en aplicar el parche.
- **El SLA se define, ordena el trabajo y nunca se mide.** Hoy es un criterio de
  ordenamiento disfrazado de compromiso.

**Consulta sin escribir** — el dato ya se captura:

- MTTR, disponibilidad por farmacia, ranking de proveedores, cumplimiento de SLA. Es el
  arreglo más barato de toda la lista y es exactamente lo que pide gerencia.

**Hueco estructural, no una función faltante:**

- **Sin relaciones entre CIs.** Bloquea análisis de impacto. El dato crudo
  —`DispositivoDetectado.interfaz_indice`— ya se está guardando sin usar.
- **Retención de 30 días.** No es un reporte faltante: cualquier pregunta histórica de más
  de un mes ya no tiene respuesta posible.

---

## Lo medido el 28-sep-2026

La matriz se armó sin acceso al servidor. Con los números reales en la mano, **ninguna
clasificación cambió** y dos quedaron confirmadas:

| Dato | Medido | Efecto en la matriz |
|---|---|---|
| `ActividadPlanificada` | **0 filas** | ⚪ confirmado, y ahora sin ambigüedad |
| Zonas de viáticos | **0** | ⚪ confirmado: la alerta no puede dispararse |
| Agentes vivos | **29** (eran 8 al 7-sep) | El rollout avanza, pero va por el ~1,6% |
| Activos · mantenimientos | **21 · 3** | Los 🟡 siguen esperando operación |
| EMQX `max_conns` / `ulimit -n` | **1024 / 1024** | 🔴 confirmado, y el techo es doble |

**Lo único que empeoró** está en 🟠 *Respaldo y restauración*: corre a diario y termina
bien, pero `pg_dump` avisa en cada corrida de `circular foreign-key constraints` sobre el
catálogo de TimescaleDB, con la advertencia de que el volcado **puede no restaurarse**.
El riesgo dejó de ser teórico.

**Sigue sin resolverse** la clasificación de `facturacion`: si el cobro entre unidades de
negocio existe como práctica, pasa de ⚪ a 🟠. Eso es una pregunta de negocio, no de datos
—hay 47 filas registradas, que solo prueban que el contador funciona—.
