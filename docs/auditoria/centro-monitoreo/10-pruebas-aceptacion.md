# 10 · Pruebas de aceptación

[← 09 Riesgos y dependencias](09-riesgos-dependencias.md) · [Índice](README.md) · [Siguiente: Recomendación final →](11-recomendacion-final.md)

Los 13 escenarios que pide el encargo, con lo que hoy está cubierto y el criterio de
aceptación. **El criterio es doble en todos los casos: el dato correcto *y* su
representación.** Que la plantilla renderice sin error no cuenta como aprobado.

---

## 10.1 Cobertura actual

Lo que ya existe en `apps/panel/tests.py` (588 pruebas en el archivo; números de línea del
árbol de trabajo, ver la nota del [índice](README.md)):

| Clase | Qué cubre |
|---|---|
| `DashboardTests` | Que el dashboard no reviente con latido de worker reciente |
| `DashboardTableroTests` | Las dos listas del tablero: recorte por cliente, pendiente que no cuenta como caída, la que nunca reportó primero, total vs. filas mostradas, alertas resueltas excluidas |
| `SaludRespaldoDashboardTests` | 6 pruebas: sin respaldo, reciente, de ayer, de hace dos días, creación y actualización de la fila, no pisar el latido de otro worker |
| `DashboardEnlacesCaidosTests` | 10 pruebas: sin caídas no muestra el bloque, proveedor y circuito, copia del evento vs. farmacia viva, sin circuito lo dice, otro cliente, dada de baja, nunca respondió, la más vieja primero, recorte con total |
| `CentroMonitoreoTests` | Permisos, solo lectura, enlace al detalle, marca de dato viejo, cadencia de polling no más rápida que el backend, POS críticos y no críticos |
| `ResumenOperacionCompartidoTests` | Que el bot de Telegram use el servicio y no su propia copia |

**Es una base sólida.** Los huecos están en los escenarios 5, 7, 8, 11 y 13, y en las
pruebas transversales de la sección 10.3.

---

## 10.2 Los 13 escenarios

| # | Escenario | Cobertura hoy | Criterio de aceptación |
|---|---|---|---|
| 1 | **Todas las estaciones reportan correctamente** | ✅ `DashboardTableroTests` | KPI en línea = total de `APROBADA`; estado vacío con texto explicativo, no un cero suelto; **los tres números coinciden en las tres pantallas** |
| 2 | **Una estación deja de enviar heartbeats** | ✅ parcial | Aparece en "dejaron de reportar" con los minutos transcurridos; **no** en "nunca reportó"; el KPI baja en 1; **el mismo minuto en el dashboard y en el Centro** |
| 3 | **Una aprobada nunca ha reportado** | ✅ `test_la_que_nunca_reporto_va_primero` | Contador propio "nunca reportó"; etiqueta "instalación fallida"; va primero; **no suma al contador de caídas** |
| 4 | **Un router deja de responder** | ✅ `DashboardEnlacesCaidosTests` | Tras 3 sondeos aparece la caída; `inicio` = primer fallo, no el tercero; antigüedad visible; proveedor desde `tipo_enlace`; **el sitio no se duplica** en incidencias por sus alertas `sin_heartbeat` |
| 5 | **Una farmacia recupera su conectividad** | ⚠️ sin prueba de UI | Desaparece de caídos al siguiente sondeo exitoso; el evento queda con `fin`; el contador baja; **el KPI del Centro y la lista del dashboard bajan en el mismo refresco** |
| 6 | **Existe una alerta crítica abierta** | ✅ parcial | Aparece en la cola con farmacia, problema, severidad y antigüedad; si está reconocida se distingue; **no cuenta en "sin reconocer"** |
| 7 | **El worker MQTT deja de procesar mensajes** | ❌ **el hueco principal** | Proceso vivo + broker caído ⇒ "sin conexión al broker"; vivo + conectado + sin procesar ⇒ se distingue de "procesando"; **los KPI de flota se degradan a "último dato conocido"** en vez de seguir afirmando un número |
| 8 | **Los datos del monitoreo quedan desactualizados** | ⚠️ solo en el Centro y solo para enlaces | Cada zona marca su propia fuente vieja; **ninguna zona con dato vencido se pinta verde**; la marca distingue "nunca se midió" de "se midió hace mucho" donde la diferencia sea operativa |
| 9 | **El último respaldo supera el umbral** | ✅ `SaludRespaldoDashboardTests` | Aviso con las horas y el runbook; `None` cuenta como atrasado; el umbral sale de `umbrales.py`, no de un literal |
| 10 | **No hay despliegues activos** | ✅ `_estado_vacio.html` | Estado vacío explicativo, no un cero; el bloque **no acapara espacio** |
| 11 | **Un despliegue está pausado o presenta errores** | ❌ **no existe** | Pausado se distingue de publicando; **errores visibles con su número**; progreso desde `resumen_de_progreso`, **sin inventar porcentaje**; el que tiene errores se ordena primero |
| 12 | **No hay farmacias ni estaciones registradas** | ⚠️ parcial | Estados vacíos en todas las zonas; **ningún 0/0 ni división por cero** — `pct_conforme=None` y `pct_completado=0` ya lo resuelven en sus dominios respectivos |
| 13 | **Se produce un error al actualizar una sección** | ❌ **no existe** | La zona dice "sin contacto desde HH:MM"; **no se queda mostrando lo anterior como actual**; las demás zonas siguen refrescando |

### Detalle de los tres huecos principales

**Escenario 7 — es el que da sentido a la Fase D.** Hoy no se puede escribir esta prueba
porque no hay nada que medir: el latido no distingue los tres estados. La prueba y el
mecanismo se construyen juntos.

**Escenario 11** — los datos existen (`resumen_de_progreso`, `ResultadoDespliegue.ERROR`,
`ROLLBACK`); solo falta llevarlos al tablero. Es una prueba fácil de escribir en cuanto la
Fase B anote los contadores.

**Escenario 13** — exige que exista el refresco por zona (Fase C). Hoy el dashboard no se
refresca, y el Centro reemplaza el bloque entero sin marcar el fallo.

---

## 10.3 Pruebas transversales nuevas, por orden de valor

### 1. `EquivalenciaIndicadoresTests` — la más importante

Misma población, tres vistas, mismo número. **Es la prueba que impide que `D-01` vuelva a
aparecer**, y hoy no existe para "estaciones en línea".

El patrón ya está en el proyecto dos veces, con su justificación escrita:

> *"Dos copias del mismo criterio se desincronizan, y el síntoma es silencioso: la tarjeta
> dice '12 desactualizadas', se la pulsa y salen 9. Nada falla; el número simplemente deja
> de ser confiable, y con él la pantalla."*
> — `EstacionQuerySet`, `apps/catalogo/models.py:238`

`EquivalenciaPredicadosEstacionTests` (en `catalogo`) corre los dos lados del mismo
predicado sobre los mismos datos y falla si difieren. `ResumenOperacionCompartidoTests` hace
lo propio para el bot. Falta el tercero: dashboard contra Centro contra listado.

### 2. `assertNumQueries` por parcial

Hoy hay **exactamente uno** en todo `apps/panel/tests.py` (línea 4535). Es el hueco de
cobertura más claro del panel, y sin él la Fase C no tiene criterio objetivo de éxito.

Mínimo necesario:

- una página cualquiera del panel (fija el coste del context processor de salud, `X-5`),
- un parcial por zona del tablero.

### 3. Accesibilidad de estado

Ningún estado se comunica **solo** con color. Comprobable recorriendo los `pill` y
`estado-chip` del HTML renderizado y exigiendo que cada uno lleve texto.

### 4. La cadencia de la plantilla no es menor que la del backend

Extender `test_el_polling_no_es_mas_rapido_que_el_backend` a cada zona. La prueba ya existe
para el Centro y su razón está escrita: *"pollear cada 10 s un dato que se recalcula cada 60
mostraría seis veces lo mismo, multiplicado por cada agente que deja la pantalla abierta"*.

### 5. El context processor de salud no cuesta más de una consulta

Cierra `X-5` de forma permanente, en cualquier página del panel.

---

## 10.4 Procedimiento de validación manual

El mismo para cada fase, con el añadido propio de cada una (ver
[parte 08](08-plan-implementacion.md)):

1. `python manage.py check`
2. `python manage.py makemigrations --check --dry-run`
3. `python manage.py test` — suite completa, **contra PostgreSQL, no SQLite**
4. Recorrer el tablero en 1920px, 1366px y 768px
5. Recorrer el tablero con el tema claro **y** el oscuro
6. Quitar el CSS de color y confirmar que la pantalla sigue leyéndose
7. Con el worker detenido: confirmar que ninguna pantalla afirma salud
8. Comparar los números de las tres pantallas **en el mismo minuto**

El paso 8 es el que de verdad cierra la Fase B, y conviene anotar los valores **antes** del
cambio para poder explicar la diferencia.

---

## Lo que hay que decidir en esta parte

Nada: esta parte es el criterio de cierre, no una propuesta. Lo único a confirmar es que
los 13 criterios de la tabla 10.2 son los correctos, y si falta algún escenario propio de
la operación de CRESIO que esta auditoría no pudo ver desde el código.

---

[← 09 Riesgos y dependencias](09-riesgos-dependencias.md) · [Índice](README.md) · [Siguiente: Recomendación final →](11-recomendacion-final.md)
