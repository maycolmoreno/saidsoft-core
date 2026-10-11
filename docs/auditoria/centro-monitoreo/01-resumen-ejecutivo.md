# 01 · Resumen ejecutivo

[← Índice](README.md) · [Siguiente: Arquitectura encontrada →](02-arquitectura-encontrada.md)

---

## El estado actual, en una línea

**SAIDSOFT ya tiene un centro de monitoreo — pero no es la pantalla que se abre al entrar.**

Existen **dos tableros solapados**, construidos en momentos distintos, que comparten
alrededor del 60 % de su información y no siempre coinciden en las cifras.

| | `/` — Dashboard<br>`apps/panel/views/dashboard.py` | `/centro/` — Centro de Monitoreo<br>`apps/panel/views/monitoreo.py:277` |
|---|---|---|
| Es lo que ve el operador al entrar | **Sí** | No (un clic más, y exige `monitoreo.view_alerta`) |
| Se actualiza solo | **No** | Sí, 60 s vía HTMX |
| Dice de cuándo es el dato | **No** | Sí (`r.calculado_en`) |
| Marca dato viejo en vez de verde falso | **No** | Sí (`stale_enlaces`, `fuente_desactualizada`) |
| Chips de estado accesibles (color + forma + palabra) | **No**, solo pills de color | Sí (`_estado_chip.html`) |
| Servicios del POS, equipos sin sondeo, ventanas de mantenimiento | No | Sí |
| Farmacias afectadas (sitios ≠ síntomas) | No | Sí |
| Umbral de "en línea" | 5 min | 10 min |
| Alertas que cuenta | `ABIERTA` + `RECONOCIDA` | solo `ABIERTA` |

El Centro es técnicamente superior en todo lo que hace a un tablero confiable. El Dashboard
es el que todo el mundo ve. **Eso, y no la estética, es el problema principal.**

---

## Las cinco mejoras de mayor impacto

### 1. Unificar el umbral de "estación en línea" — `D-01`

Hay **tres criterios distintos vivos en tres pantallas**: 5 minutos en el dashboard,
10 minutos en el Centro, y el campo persistido `estado_conexion` en el listado de
Estaciones. Los tres son defendibles por separado y contradictorios juntos: una estación
cuyo último latido fue hace 7 minutos está **caída** en el dashboard, **en línea** en el
Centro y **caída** en Estaciones, al mismo tiempo.

→ Detalle en [04 · Riesgo A](04-riesgos-del-encargo.md#a--estaciones-en-línea).

### 2. Corregir el denominador del KPI más visible — `D-02`

`total_online / total_estaciones` del dashboard sale de `calcular_matriz_cumplimiento`,
que **excluye el grupo `PENDIENTE`** (el bucket de las farmacias sin nodo asignado) y
**no filtra por `estado_aprobacion`**. El numerador y el denominador no describen la misma
población, y ninguna de las dos es "la flota".

→ Detalle en [03 · I-01 e I-02](03-auditoria-indicadores.md#31-tabla-maestra).

### 3. Dejar de afirmar salud que nadie comprobó — `D-05`, `D-06`

Dos problemas distintos que apuntan al mismo sitio:

- La barra superior dice **"Todos los sistemas operativos"** con un LED verde fijo, en
  **todas** las pantallas del panel, incluso mientras el aviso rojo de "Worker MQTT sin
  señal" está tres centímetros más abajo en la misma pantalla.
- El latido del worker MQTT **no prueba que procese mensajes ni que esté conectado al
  broker**: un hilo lo escribe mientras el proceso viva. Si EMQX cae, `loop_forever()`
  reintenta en silencio y el dashboard sigue verde.

→ Detalle en [04 · Riesgo E](04-riesgos-del-encargo.md#e--salud-de-la-plataforma) y
[05 · U-1](05-auditoria-ux.md).

### 4. Poner en pantalla la observabilidad que ya existe — `D-10`, `D-11`, `D-12`

`verificar_salud` comprueba siete componentes; el dashboard muestra dos. Además hay
señales **ya persistidas** que no aparecen en ninguna pantalla:

- `MensajeMqttFallido` (la cola de mensajes que el worker no pudo procesar): cero presencia
  en el panel, solo en el admin de Django. Es la prueba directa de "recibe pero no procesa".
- `EnrolamientoRechazado`: la vista existe, pero `panel:enrolamientos_rechazados_lista`
  **no está enlazada desde ninguna plantilla**. Es la bandeja que explica "instalé el
  agente y nunca apareció", y hay que escribir la URL a mano para llegar.
- El broker EMQX **no se comprueba en ningún sitio**, aunque su API de administración ya
  está integrada en `apps/mqtt_worker/emqx_admin.py`.

### 5. Reorganizar la jerarquía — `U-2`

La matriz de cumplimiento por grupo (planificación de despliegues) ocupa la primera
posición y todo el ancho, por encima de los enlaces caídos, las alertas y las estaciones
sin reportar. Una farmacia que no puede vender queda en el tercio inferior de la pantalla.

→ Detalle en [05 · Auditoría UX](05-auditoria-ux.md) y
[06 · Diseño propuesto](06-diseno-propuesto.md).

---

## Lo que está bien y no debe tocarse

No todo lo encontrado es un defecto, y conviene decirlo antes de proponer cambios:

- El **sistema de diseño** es maduro: tokens con tema claro y oscuro al mismo nivel de
  cuidado, `--serie` deliberadamente fuera de la paleta de estado, `--page-max: 1600px`
  con justificación medida.
- `_estado_chip.html` resuelve bien la accesibilidad (color + forma + palabra, con
  `sr-only`). **Es el estándar correcto; solo falta aplicarlo en el dashboard.**
- La decisión de que el Centro sea de **solo lectura**, y el recorte a N filas con el
  total aparte.
- La detección de caídas de enlace: 3 sondeos fallidos, `inicio = primer fallo` (no el
  tercero), recuperación inmediata, y las tres exclusiones (`activa`,
  `respondio_alguna_vez`, `fin=null`) aplicadas igual en las tres pantallas.
- Los **comentarios de código y plantilla**: cada decisión no obvia tiene su por qué y su
  fecha. Buena parte de esta auditoría fue posible gracias a eso.

---

## Una observación de alcance, dicha una vez

El encargo pide convertir el dashboard en un centro de monitoreo. Ese centro **ya existe**
en `/centro/`, está mejor construido y comparte la mayor parte de la información. La
recomendación es **consolidar en una pantalla, no construir una tercera**.

Si la decisión es mantener los dos separados, la corrección de indicadores (Fase B) sigue
siendo igual de necesaria y urgente: **dos tableros con cifras que no cuadran es peor que
uno solo imperfecto.**

---

## Lo que hay que decidir en esta parte

1. **¿Se consolidan los dos tableros en `/`, o se mantienen separados?**
   De esta respuesta depende el alcance de las fases A y C, no su contenido.
2. ¿Se acepta el orden de prioridad de las cinco mejoras, o hay otra urgencia operativa
   que las desplaza?

---

[← Índice](README.md) · [Siguiente: Arquitectura encontrada →](02-arquitectura-encontrada.md)
