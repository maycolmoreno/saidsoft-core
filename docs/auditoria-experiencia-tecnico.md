# Auditoría de la experiencia del técnico de soporte

**Fecha:** 3-oct-2026
**Alcance:** SAIDSOFT completo, mirado desde la jornada de un técnico que monitorea la
flota. No es una auditoría de arquitectura — esa es `auditoria-arquitectura.md` — sino de
si el técnico puede detectar, entender y resolver un problema rápido.
**Regla de la auditoría:** solo lectura. Nada de código se modificó para escribirla.
**Horizonte:** ~1.300 farmacias.

**Pregunta con la que se evaluó cada pantalla:** ¿esto ayuda al técnico a tomar una
decisión? Si la respuesta es no, se anota como candidata a ocultar, resumir o mover.

---

## 1. Resumen

**15 apps Django, 95 modelos, 190 vistas de panel en 166 rutas, 15 endpoints DRF.**

El monitoreo real vive en cuatro apps: `catalogo` (la flota), `monitoreo` (21 modelos:
alertas, métricas, enlaces, servicios POS, red), `mqtt_worker` (la ingesta) y `panel`.

**La conclusión principal:** la cadena *detectar → registrar* está completa y bien
construida. Lo que falta es la capa de arriba — **agrupar, priorizar y guiar**. El técnico
hoy tiene que armar el diagnóstico en su cabeza, cruzando pantallas.

**Lo que más sorprendió:** varias de las piezas que parecían faltar ya existen, pero en el
lugar equivocado. El diagnóstico operativo de una farmacia —enlace, router, circuito,
tráfico, estaciones, servicios POS caídos— **ya está implementado en Telegram**
(`_comando_farmacia`) y **no existe en el panel**. Que exista en el bot prueba que los
datos alcanzan: lo que falta es la pantalla, no la información.

**Una advertencia sobre la escala.** El código ya opera ~700 farmacias con IP cargada y
sus docstrings razonan sobre ~1.800 estaciones. Pasar a 1.300 farmacias es **menos del
doble**, no un orden de magnitud. Eso cambia la urgencia: el trabajo de escalabilidad es
acotado y concreto, no un rediseño.

---

## 2. Núcleo que debe conservarse

| Componente | Archivo | Estado | Por qué |
|---|---|---|---|
| `resumen_operacion()` | `apps/monitoreo/services.py` | **CONSERVAR** | 15 KPI con `count()` agregados, escopeados por tenant, y ya excluye ruido: PCs administrativas aparte (se apagan de noche), farmacias que nunca respondieron, farmacias dadas de baja |
| `centro_monitoreo_partial` | `apps/panel/views/monitoreo.py:270` | **CONSERVAR** | ya es un centro de trabajo: solo lectura, refresco HTMX, 15 filas por bloque, frescura por bloque, ventanas de mantenimiento separadas de lo crítico |
| `clasificar_caidas_simultaneas` | `apps/monitoreo/enlaces.py:431` | **CONSERVAR** | correlación real entre farmacias: `proveedor` / `ceguera` / `individual` |
| `evaluar_cruce_monitoreo` | `apps/monitoreo/services.py` | **CONSERVAR** | cruce MQTT × MeshCentral: distingue "agente caído, red viva" de "red caída" |
| `diagnostico_ia.py` | `apps/monitoreo/diagnostico_ia.py` | **CONSERVAR** | ya produce CAUSA PROBABLE / QUÉ VERIFICAR / DESCARTAR; solo críticas, una vez por incidente, guardado en campo propio y rotulado como hipótesis sin verificar |
| `_comando_farmacia` | `apps/monitoreo/telegram_bot.py:348` | **FUNCIONA PERO ESTÁ AISLADO** | el diagnóstico por farmacia existe solo en Telegram; además tiene un N+1 |
| `alertas_lista?vista=agrupada` | `apps/panel/views/alertas.py` | **CONSERVAR** | ya agrupa por regla y cuenta estaciones afectadas: un bug sistémico en 40 farmacias es 1 fila, no 40 |
| `registrar_evento` / `EventoAuditoria` | `apps/auditoria/` | **CONSERVAR** | 20 de los 25 módulos de vistas lo usan |
| Aprobación de ejecuciones | `apps/scripts/models.py:104` | **CONSERVAR** | cuatro ojos según el **alcance del destino**; quien crea no aprueba |
| Ingesta MQTT | `apps/mqtt_worker/services.py` | **CONSERVAR** | recién saneada: cada app recibe su propia ingesta |
| `fuente_desactualizada` / `TOLERANCIA_FRESCURA_MINUTOS` | `apps/monitoreo/services.py` | **CONSERVAR** | distingue "no hay problema" de "no sé si hay problema" |

---

## 3. Experiencia actual del técnico: **6 / 10**

No es bajo por mala ingeniería. Las piezas están bien construidas y los docstrings explican
el porqué. Es bajo porque **el trabajo de interpretación quedó del lado del técnico**.

**Lo que sube la nota.** El Centro de Monitoreo es una buena pantalla de triage y ya toma
decisiones que mucha gente no toma: separar lo que está dentro de una ventana de
mantenimiento de un incidente real, distinguir "silencio" de "caído", marcar frescura por
bloque en vez de teñir toda la pantalla. Telegram es un modo guardia de hecho.

**Lo que la baja, con evidencia:**

1. **El dashboard no es de problemas.** `apps/panel/views/dashboard.py` arma matriz de
   cumplimiento, despliegues activos, aprobaciones pendientes y salud de plataforma. **No
   incluye enlaces caídos**: el fallo de mayor impacto operativo no está en la pantalla de
   inicio. (Esto ya lo había notado `auditoria-arquitectura.md` §1, desde el ángulo
   opuesto: no hay duplicación de conteos de enlaces porque el dashboard no muestra
   ninguno.)
2. **No existe vista de farmacia.** Verificado sobre las 166 rutas: ninguna es un detalle
   operativo de farmacia. Todo es por estación o por flota. Pero el técnico piensa en
   farmacias: "ML001 está caída".
3. **Cinco síntomas de un problema son cinco filas.** Falta correlación vertical.
4. **Nueve pantallas responden "cómo está la flota"**: `dashboard`, `centro_monitoreo`,
   `monitoreo_lista`, `monitoreo_detalle`, `tendencia_flota`, `alertas_lista`,
   `pos_errores_flota`, `enlaces_farmacias_lista`, `red_farmacias_lista`,
   `estaciones_lista`.

---

## 4. Problemas de usabilidad, priorizados

1. **No hay vista operativa de farmacia.** Para entender un sitio hay que visitar
   `enlaces_farmacias_lista`, `estaciones_lista` y `monitoreo_detalle` y cruzar a mano.
2. **El dashboard no responde "¿hay problemas?"** y omite enlaces.
3. **Sin correlación vertical** dentro de una farmacia.
4. **No hay "mis pendientes".** `Alerta` **no tiene campo de asignación** — solo
   `reconocida_por`. Lo más cercano posible hoy es "alertas que yo reconocí".
5. **Sin prioridad operativa.** `ReglaAlerta.Severidad` tiene **dos** valores
   (`warning` / `critical`). No hay P1–P4 ni nada que pondere cuántas estaciones o qué
   farmacia está afectada.
6. **Sin SLA.** `escalar_alertas_abiertas` es un recordatorio de **un solo paso** con
   minutos configurables (`ConfiguracionMonitoreo.minutos_escalamiento_alerta`). No hay
   vencimiento por severidad ni "SLA por vencer".
7. **Sin recurrencia.** Nada responde "esta farmacia ya falló 5 veces este mes", aunque
   `EventoEnlaceFarmacia` tiene el historial para calcularlo.

---

## 5. Problemas técnicos, priorizados

1. **`Alerta` no tiene ningún índice.** Su `Meta` (`apps/monitoreo/models.py:383`) declara
   solo `db_table` y `ordering`. Se la filtra por `estado` + join a
   `estacion__farmacia__unidad_negocio` ordenando por `-abierta_en`, y eso ocurre en el
   dashboard, el Centro de Monitoreo, `alertas_lista` y el bot de Telegram. Es el arreglo
   más barato y de mayor efecto del informe.
2. **`alertas_lista` no paginar ni acotar.** `apps/panel/views/alertas.py` pasa el
   queryset completo a la plantilla; con `?todas=1` son todas las alertas de la historia.

   **Corrección a una primera versión de este informe:** decía "solo `estaciones.py`
   paginar", y es falso. El grep buscaba `Paginator` y no encontró
   `apps/panel/paginacion.py`, un helper compartido (`paginar`, 25 filas por página, con
   `query_filtros` para no perder los filtros al cambiar de página) que **ya usan cuatro
   vistas**: `activos`, `auditoria`, `enlaces` y `estaciones`, con el parcial
   `templates/panel/_paginador.html`. El problema entonces no es que no exista
   paginación en el panel: es que `alertas_lista` no la usaba teniéndola disponible.
3. **N+1 en `_comando_farmacia`** (`telegram_bot.py:396`): consulta `EstadoServicioPos`
   dentro del bucle de estaciones.
4. **La fuga de descriptores SNMP recién arreglada no está desplegada** (commit
   `f73a26f`). Mientras no se despliegue, `celery_worker` sigue filtrando ~11.000
   descriptores por hora y el monitoreo de enlaces se ciega solo cada pocas horas.

---

## 6. Información duplicada

| Se repite | Dónde | Qué consolidar |
|---|---|---|
| Estado de la flota | `dashboard`, `centro_monitoreo`, `monitoreo_lista` | el dashboard debería **ser** el centro, no otra vista más |
| Enlaces | `enlaces_farmacias_lista`, `red_farmacias_lista`, bloque del centro | dos listas separadas para el enlace y el tráfico del mismo sitio |
| Errores del POS | `pos_errores_flota`, `/toperrores`, bloque del centro | el rollup ya está factorizado en `_top_mensajes_pos_errores`; reusarlo |
| Estaciones | `estaciones_lista`, `monitoreo_lista`, `tendencia_flota` | tres formas de listar lo mismo con distinto énfasis |

---

## 7. Funcionalidades existentes reutilizables

- `resumen_operacion()` — los KPI de cabecera, ya agregados y escopeados.
- `clasificar_caidas_simultaneas` — correlación horizontal entre farmacias.
- `evaluar_cruce_monitoreo` — correlación de dos fuentes en una estación.
- `_comando_farmacia` — el diagnóstico de farmacia, a portar a web.
- `alertas_lista?vista=agrupada` — agrupación por regla.
- `diagnostico_ia` — causa probable para críticas.
- `fuente_desactualizada` / `TOLERANCIA_FRESCURA_MINUTOS` — frescura por fuente.
- `EstacionQuerySet` (`desactualizadas`, `con_reloj_desincronizado`,
  `incomunicadas_por_reloj`, `con_zona_incorrecta`) — predicados de dominio con un solo dueño.
- `scope_por_unidad_negocio_activa` / `unidades_negocio_en_foco` — alcance por tenant.
- `apps.panel.paginacion.paginar` + `templates/panel/_paginador.html` — paginación
  compartida con conservación de filtros; cualquier listado nuevo debe usarla.
- `EstadoServicioPos` con `pg_local` / `pg_central` — el `SELECT 1` contra la base del POS
  ya existe.
- `registrar_evento` — auditoría de acciones humanas.

---

## 8. Funcionalidades que faltan

**CRÍTICAS**
- Vista operativa de farmacia en el panel.
- Correlación vertical → incidente por farmacia.
- Contador **"farmacias afectadas"**: no está entre las claves de `resumen_operacion`.
- Enlaces caídos en el dashboard.

**IMPORTANTES**
- Prioridad P1–P4 derivada del impacto medido.
- Asignación de alerta a técnico (`Alerta.asignado_a`).
- Recurrencia por farmacia.
- Estados `EN INVESTIGACIÓN` y `CERRADA`.

**MEJORAS**
- Cadena de diagnóstico explícita (primer punto de falla).
- Topología de la farmacia.
- `/guardia` en Telegram — **se descarta**, ver §16.

---

## 9. Flujo operativo propuesto

```
Regla incumplida → Alerta(ABIERTA) → correlación agrupa por farmacia
  → Incidente con prioridad calculada → notificación (Telegram / correo)
  → el técnico RECONOCE (corta el escalamiento)
  → investiga con la vista de farmacia
  → actúa (script aprobado / sincronizar / abrir mantenimiento)
  → el sistema verifica que la condición desapareció → RESUELTA
  → queda EventoAuditoria + EventoEnlaceFarmacia / EventoMonitoreo
```

Casi todo esto ya existe. **Lo nuevo son los dos pasos del medio: correlación y
prioridad.**

---

## 10. Dashboard propuesto

Convertir el dashboard **en** el Centro de Monitoreo en vez de mantener los dos.

Franja superior con los números de `resumen_operacion()` más uno nuevo
(`farmacias_afectadas`), cada uno enlazando al filtro que lo explica:

```
CRÍTICAS 12    ADVERTENCIAS 27    FARMACIAS AFECTADAS 8
ENLACES CAÍDOS 4    POS CRÍTICOS 6    ESTACIONES FUERA 15
```

Debajo, en este orden: **incidentes por farmacia** (agrupados, con prioridad y duración) →
**mis pendientes** → ventanas de mantenimiento activas → eventos de Windows.

Cumplimiento y despliegues se mueven a su propia pantalla: no son trabajo del técnico de
guardia.

---

## 11. Vista de farmacia (nueva)

Portar `_comando_farmacia` a web, en este orden de prioridad:

1. **Estado general e incidente** — qué pasa y desde cuándo.
2. **Conexión** — enlace, latencia, circuito del proveedor, tráfico.
3. **Estaciones** — heartbeat y servicios POS caídos de cada una.
4. **Equipos de red** — lo que `DispositivoDetectado` ya ve.
5. **Historial corto** — últimas 5 caídas y reinicios (ya existe en
   `apps/panel/views/enlaces.py:330`).
6. **Acciones** — las que ya existen, sin agregar ninguna nueva.

Resolviendo el N+1 con `prefetch_related` sobre `EstadoServicioPos`.

---

## 12. Vista de estación

`monitoreo_detalle` ya existe. No hace falta una pantalla nueva: hace falta **reordenar por
prioridad** y colapsar lo de abajo.

¿Hay alerta activa? → agente (heartbeat, versión) → POS y servicios → reloj y zona horaria
→ conectividad → recursos → inventario.

---

## 13. Vista de alerta / incidente

`Alerta.Estado` tiene **tres** valores: `ABIERTA`, `RECONOCIDA`, `RESUELTA`.

Agregar `EN_INVESTIGACION` y `CERRADA` es compatible con lo que hay: `escalar_alertas_abiertas`
filtra por `ABIERTA` y las pantallas por `ABIERTA`/`RECONOCIDA`.

**No romper `reconocida_por` ni `escalada_en`.** Son lo que corta el escalamiento y están
bien pensados: `RECONOCIDA` ya significa "alguien la vio aunque no la haya resuelto".

---

## 14. Correlación de problemas

**Ya existe, en tres formas:**

1. **Entre farmacias, por ISP y tiempo** — `clasificar_caidas_simultaneas`: varias del
   mismo proveedor cayendo juntas es un corte del proveedor; de varios ISP a la vez es
   ceguera nuestra.
2. **Entre dos fuentes de una estación** — `evaluar_cruce_monitoreo`: MQTT callado +
   MeshCentral viéndola en línea = se cayó el agente, no la red.
3. **Entre estaciones, por regla** — `alertas_lista?vista=agrupada`.

**Falta una:** la **vertical dentro de una farmacia**.

**La dificultad real, y hay que tenerla en cuenta antes de diseñar:** `Alerta` cuelga de
`Estacion`, y el enlace cuelga de `Farmacia`. El incidente tiene que agrupar por farmacia
cruzando `EstadoEnlaceFarmacia` + `Estacion.ultimo_heartbeat` + `EstadoServicioPos` +
`Alerta`. **No hace falta una tabla nueva**: alcanza una función de servicio que calcule
los incidentes al momento de pedirlos.

**Regla de inferencia que los datos ya soportan:** si el enlace de la farmacia está caído,
todo lo demás de esa farmacia es **consecuencia** y se muestra como indicador del
incidente, no como alerta propia.

---

## 15. Diagnóstico automático

**Existe:** `diagnostico_ia` (causa probable para críticas, rotulada como hipótesis), el
cruce de dos fuentes, `verificar_salud` (salud de la plataforma), `EstadoServicioPos` con
`pg_local`/`pg_central`.

**Falta:** la cadena explícita

```
Internet → Router → SNMP → Estación → Agente → POS → Servicios → Base de datos
```

que señale el **primer punto de falla**. Todos los datos existen; nadie los ordena en
secuencia.

---

## 16. Telegram

**Mantener.** Los 12 comandos, y sobre todo que **`/reconocer` exista y resolver no**: el
código argumenta que una resolución no se puede verificar desde un teléfono. Es correcto;
no tocarlo.

**Mejorar.** El N+1 de `_comando_farmacia`.

**No agregar.** `/guardia` es innecesario: `/estado` + `/criticas` ya dan el resumen
operativo. El bot no debe volverse un panel.

---

## 17. Seguridad y permisos

**Bien:** permisos de Django por vista, `verificar_acceso` por tenant, auditoría en 20 de
los 25 módulos de vistas, cuatro ojos por alcance del destino, MFA.

**A revisar:**
- **`viaticos/facturas/` se sirve sin sesión.** `PREFIJOS_MEDIA_PROTEGIDOS` en
  `config/urls.py` cubre solo `mantenimiento/`, y en producción `/media/` lo sirve nginx:
  el `location /media/mantenimiento/` es `internal`, el resto es público a propósito
  (los agentes bajan paquetes de ahí). Se resuelve al quitar la app de viáticos.
- El riesgo de un script se mide por **alcance del destino**, no por peligrosidad del
  contenido: un script destructivo sobre una sola estación no pasa por aprobación.

---

## 18. Escalabilidad a 1.300 farmacias

Es **menos del doble** de la escala actual, lo que vuelve el trabajo acotado.

1. **Índices de `Alerta`** — lo más barato y de mayor efecto.
2. **Paginar `alertas_lista`** y las demás listas sin cota.
3. El Centro de Monitoreo ya está bien: `count()` agregados y 15 filas por bloque.
4. Los barridos ya van por lotes (`evaluar_cruce_monitoreo` hace dos consultas y cruza en
   Python, explícitamente por la escala); las series son hypertables con retención de 30 días.

---

## 19. Plan de implementación

### FASE 1 — alto impacto, bajo riesgo

| Tarea | Archivos | Riesgo | Resultado esperado |
|---|---|---|---|
| Índices en `Alerta` (`estado`, `-abierta_en`) | `apps/monitoreo/models.py` + migración | Bajo | las cuatro pantallas que la consultan dejan de escanear |
| `farmacias_afectadas` en `resumen_operacion` | `apps/monitoreo/services.py` | Bajo | el número que falta, con un `count(distinct)` |
| Enlaces caídos en el dashboard | `apps/panel/views/dashboard.py` | Bajo | el fallo de mayor impacto deja de estar escondido |
| Paginar `alertas_lista` | `apps/panel/views/alertas.py` | Bajo | deja de renderizar sin cota |

### FASE 2 — vista de farmacia

Portar `_comando_farmacia` a web con `prefetch_related`; arreglar el N+1 del bot de paso.
Riesgo bajo: es solo lectura. Depende de la Fase 1 solo por conveniencia.

### FASE 3 — correlación y prioridad

Función de servicio que calcule incidentes por farmacia; prioridad derivada del impacto; el
dashboard pasa a mostrar incidentes en vez de alertas sueltas. **Riesgo medio**: es lógica
nueva y necesita pruebas propias.

### FASE 4 — estados y asignación

`EN_INVESTIGACION`, `CERRADA`, `Alerta.asignado_a`, "mis pendientes", recurrencia. Riesgo
medio; lleva migración.

### FASE 5 — cadena de diagnóstico y topología

---

## 20. Recomendación final

**¿Está bien encaminado?** Sí. La capa de detección y registro es sólida y está documentada
con el porqué. Ya tomó decisiones correctas que no son obvias: ventanas separadas de lo
crítico, "silencio" distinguido de "caído", reconocer ≠ resolver, la hipótesis de la IA en
un campo propio para que nadie la confunda con una medición. **No hace falta rediseñar nada
estructural.**

**Qué NO cambiar:** la ingesta MQTT, `resumen_operacion`, el diseño del Centro de
Monitoreo, `reconocida_por` / `escalada_en`, que Telegram no resuelva, la aprobación por
cuatro ojos, el guardado de la IA en campo aparte.

**Qué mejorar primero:** los cuatro puntos de la Fase 1. Son poco trabajo y mueven la
aguja.

**Qué dejar para después:** topología, cadena de diagnóstico formal, SLA con vencimientos.

**El cambio de mayor impacto para un técnico:** la **vista de farmacia**. Hoy entender un
sitio exige tres pantallas y cruzar datos mentalmente — y la vista ya existe en Telegram,
lo que prueba que los datos alcanzan. Segundo: la **correlación**, que convierte cinco
alertas en un incidente con una causa probable.

---

## Corrección a conteos previos

Durante el análisis se corrigió un conteo propio: un primer recuento dio "119 modelos"
porque el grep contaba también las clases `TextChoices`. El número real, medido con
`django.apps.get_models()`, es **95**. Por lo mismo quedaron sobrestimados los tamaños por
app en la discusión sobre qué recortar: `mantenimiento` son **18** modelos y no 24,
`activos` **15** y no 19, `monitoreo` **21** y no 23, `viaticos` **3** y no 6.

La diferencia con `auditoria-arquitectura.md` (12-sep-2026: "16 apps, 86 modelos") es real
y esperable: pasaron tres semanas de desarrollo y se dio de baja el esquema de sync con
RRHH.
