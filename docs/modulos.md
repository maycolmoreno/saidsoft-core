# Los 15 módulos: qué hace cada uno y qué mirar cuando falla

**Para quién es esto.** Mesa de ayuda y equipo interno de CRESIO: gente con contexto
técnico que no escribió el código y necesita orientarse rápido. No es un manual de
usuario (eso es `docs/entrega-proyecto/manual-usuario.md`) ni documentación para
desarrolladores externos.

**Qué NO está acá, para no duplicar:**

| Busco... | Está en |
|---|---|
| Cómo se usa una funcionalidad del panel | [`README.md`](../README.md) |
| Por qué se decidió algo, y qué costó descubrirlo | [`PLAN_MODERNIZACION.md`](../PLAN_MODERNIZACION.md) |
| Cómo levantar, desplegar y qué pendientes hay | [`CLAUDE.md`](../CLAUDE.md) |
| Problemas de arquitectura y consultas pesadas | [`auditoria-arquitectura.md`](auditoria-arquitectura.md) |
| Casos de uso y diagrama de clases | [`entrega-proyecto/`](entrega-proyecto/) |
| Alcance y decisiones de la app de campo | [`movil-campo/README.md`](../movil-campo/README.md) |

---

## El mapa en 30 segundos

Tres ejes sostienen todo lo demás:

```
UNIDAD DE NEGOCIO  (SG · MIA · 7DIAS)   ← el tenant: casi todo se filtra por acá
    └── FARMACIA   (701 modeladas)      ← el eje real del sistema
            └── ESTACION                ← el equipo con agente (40 aprobadas)
```

Y dos "motores" que funcionan sin que nadie los mire:

- **`mqtt_worker`** — recibe todo lo que los agentes reportan. Si esto se cae, la
  plataforma queda ciega aunque el panel siga andando.
- **Celery Beat** — 21 tareas periódicas: sondeos, alertas, purgas, escalamientos.

**Regla que atraviesa el proyecto:** los comandos de management que escriben en masa
**simulan por defecto** y exigen `--aplicar`. Si corriste uno y "no hizo nada",
probablemente sea eso.

---

## Los módulos

### `catalogo` — el eje
**Para qué existe:** define qué es una unidad de negocio, una farmacia y una estación, y
gobierna el ciclo de vida del agente instalado en cada estación.

**Modelos:** `UnidadNegocio`, `Grupo` (nodo POS), `Farmacia`, `Estacion`, `VersionAgente`,
`ClaveRecuperacionBitLocker`, `PerifericoDetectado`.

**Flujos clave:** `enviar_comando`, `enviar_script`, `enviar_actualizacion_agente`,
`enviar_pausa` (freno de emergencia), `aprovisionar_credencial_estacion`, `secreto_de`.
Todos firman con HMAC y una ventana de 120 s.

**Tareas:** `marcar_estaciones_offline_task` (cada 60 s).

**Relación:** todo depende de él. `mqtt_worker` le escribe, el panel lo lee,
`monitoreo`/`scripts`/`software`/`despliegues` le mandan órdenes.

> **Gotcha.** Un comando a una estación con el reloj corrido más de 120 s **se descarta en
> silencio**. Desde el agente 0.30 la estación se autocorrige sola; antes había que entrar
> por MeshCentral. Si una orden "no llega", mirá el desfase de reloj en la ficha antes que
> nada.

> **Gotcha.** `/saidsof/agente/<código>/comando/` **no es retenido**: lo publicado mientras
> la estación estaba apagada se pierde. Las actualizaciones de agente y la pausa sí van
> retenidas y llegan al encender.

---

### `mqtt_worker` — el oído
**Para qué existe:** único proceso que escucha el broker MQTT y traduce lo que reportan
los agentes a filas en la base.

**Modelos:** `WorkerHeartbeat` (latido del propio worker), `MensajeMqttFallido` (cola de
revisión manual).

**Flujos clave:** `manejar_enrolamiento`, `manejar_heartbeat`, `manejar_metricas`,
`manejar_eventos_sistema`, `manejar_servicios_pos`, `manejar_pos_errores`,
`manejar_estado_script`, `manejar_estado_despliegue`. El ruteo vive en
`run_mqtt_worker.py`.

**Relación:** escribe en `catalogo`, `monitoreo`, `scripts`, `software`, `despliegues`,
`facturacion`.

> **Gotcha, y es la que más veces mordió en este proyecto.** EMQX autoriza por lista
> blanca y **deniega en silencio**; el PUBACK confirma que recibió el paquete, no que lo
> autorizara. Si algo "se publica con éxito" y no llega, revisá la ACL en
> `deploy/bootstrap-emqx.sh`. Hay pruebas estructurales que atan esto
> (`AclDelPanelCubreLoQuePublicaTests`).

> **Gotcha.** Es **monohilo**: procesa un mensaje por vez. Medido: 9 ms por latido, techo
> de ~111 msg/s. A 1.800 estaciones la carga estimada es ~44 msg/s.

---

### `monitoreo` — el más grande (14.800 líneas)
**Para qué existe:** todo lo que se vigila — métricas de estación, enlaces de farmacia,
servicios del POS, eventos de Windows, alertas y sus notificaciones.

**Modelos principales:** `ReglaAlerta` + `Alerta` (el motor configurable),
`EstadoEnlaceFarmacia` + `EventoEnlaceFarmacia`, `EstadoServicioPos`,
`EventoSistemaVigilado` + `EventoSistemaDetectado`, `VentanaMantenimiento`,
`CanalNotificacion`, `ConfiguracionMonitoreo` (umbrales globales).

**Flujos clave:** `evaluar_reglas_metricas`, `evaluar_regla_reloj`,
`evaluar_regla_evento_sistema`, `abrir_o_mantener_alerta`, `notificar_alerta`,
`escalar_alertas_abiertas`, `resumen_operacion` (el Centro de Monitoreo).

**Tareas:** 16 de las 21 del sistema. Sondeos de enlace (120 s), cruce de monitoreo
(420 s), escalamiento (600 s), purgas diarias a las 03:00.

**Relación:** lee de `catalogo`, notifica por correo/Teams/Telegram, y `mantenimiento`
puede abrir un ticket desde una alerta (`abrir_mantenimiento_desde_alerta`).

> **Resuelto el 26-sep-2026.** La lista de alertas tiene botón **"Abrir mantenimiento"**.
> Antes solo se abría sola, cuando la regla tenía `abre_mantenimiento`; para el resto
> había que ir a Mantenimientos y volver a escribir de qué alerta venía.

> **Gotcha.** Los umbrales viven en `ReglaAlerta`, no en el agente: cambiarlos **no**
> requiere redistribuir el ejecutable a las estaciones. Lo mismo para qué servicios del
> POS y qué eventos de Windows se vigilan.

> **Gotcha.** El escalamiento reenvía **por los mismos canales y a la misma gente**; no
> hay lista de escalamiento separada. Hoy el único canal vivo es un chat de Telegram: el
> correo está configurado pero `EMAIL_HOST_USER` está vacío, así que **los 10
> destinatarios con correo cargado no reciben nada**.

---

### `panel` — la cara (159 URLs)
**Para qué existe:** todas las pantallas web. Django + HTMX, sin framework de frontend.

**Estructura:** `apps/panel/views/` con un archivo por dominio (`activos.py`,
`mantenimiento.py`, `estaciones.py`, `viaticos.py`, `monitoreo.py`, ...). Las vistas de
cada módulo de negocio viven acá, no en su propio app.

**Flujos clave:** `dashboard`, `centro_monitoreo` (+ `_partial`, refresco cada 60 s por
HTMX), `estacion_info_modal`, `alertas_lista`.

> **Gotcha.** Las pantallas que se refrescan solas están partidas en dos vistas: el marco
> y el parcial. Si tocás el contenido, tocás el `_partial`.

> **Gotcha.** Los comentarios `{# ... #}` multilínea **se renderizan como texto visible**.
> Usá `{% comment %}`.

---

### `activos` — el inventario (ITAM)
**Para qué existe:** qué equipos hay, dónde están, quién los tiene y cómo se mueven.

**Modelos:** `Activo` (el central), `CategoriaEquipo`, `Marca`, `Colaborador`, `Bodega`,
`StockBodega`, `MovimientoInventario`, `OrdenCompra` + `OrdenCompraDetalle`,
`RecepcionLote`, `TipoConsumible`, `Ubicacion`, `Departamento`, `Cargo`.

**Flujos clave:** `registrar_ingreso`, `registrar_asignacion`, `registrar_devolucion`,
`registrar_envio_reparacion` / `registrar_retorno_reparacion`, `registrar_baja`,
`recibir_orden_compra`, `registrar_recepcion_lote`, `anular_recepcion_lote`,
`registrar_traslado_bodega`, `registrar_ajuste_inventario`.

**Tareas:** `vincular_activos_por_serie_task` (diaria, 04:00) — enlaza lo que el RMM
descubre con lo que hay cargado a mano.

**Relación:** `Activo.estacion` lo conecta con `catalogo`; `mantenimiento` abre tickets
sobre activos; `monitoreo` los sondea si tienen IP.

> **Gotcha.** `MovimientoInventario` es append-only: los movimientos no se editan, se
> compensan con otro movimiento. `anular_recepcion_lote` existe justamente por eso.

---

### `mantenimiento` — servicio en campo
**Para qué existe:** los tickets de mantenimiento, su ciclo de vida, la evidencia
(fotos, firmas, repuestos) y la app móvil del técnico.

**Modelos:** `Mantenimiento` (el central), `TipoMantenimiento`, `MantenimientoProgramado`,
`ActividadChecklist` + `ActividadRealizada`, `EventoMantenimiento`, `FirmaMantenimiento`,
`ImagenMantenimiento`, `RepuestoUtilizado`, `AcuerdoNivelServicio`, `ActividadPlanificada`,
`Notificacion`, `ConsentimientoMonitoreo`.

**Flujos clave:** `crear_mantenimiento_manual`, `abrir_mantenimiento_desde_alerta`,
`iniciar_mantenimiento`, `registrar_actividad_checklist`, `cerrar_mantenimiento`,
`cancelar_mantenimiento`, `firmar_mantenimiento`, `generar_informe_pdf`.

**Tareas:** `generar_mantenimientos_programados_task` (06:15),
`notificar_mantenimientos_vencimiento_task` (07:00), `generar_informe_pdf_task`.

**Relación:** único app con **API DRF** (12 endpoints) que consume la app Flutter
`movil-campo`. Se conecta con `activos` (el equipo), `monitoreo` (alerta → ticket) y
`catalogo` (la farmacia).

> **Gotcha.** El SLA se mide contra `AcuerdoNivelServicio`, que es por tipo y unidad de
> negocio. Si un ticket "no tiene SLA", probablemente falte el acuerdo cargado para esa
> combinación.

> **Resuelto el 26-sep-2026.** Cerrar o cancelar ahora le avisa a **quien abrió** el
> ticket, que antes tenía que volver a mirar para enterarse. Ojo: `Mantenimiento` no
> tiene campo `creado_por` — quien lo abrió sale del evento `PROGRAMADO`
> (`_quien_lo_abrio`). Los que abre el sistema (una alerta, la tarea de programados) no
> avisan a nadie porque no hay a quién.

---

### `scripts` — ejecución remota (RMM)
**Para qué existe:** correr PowerShell/batch en estaciones, con aprobación y seguimiento.

**Modelos:** `Script`, `EjecucionScript`, `ResultadoEjecucionScript`, `ScriptProgramado`.

**Flujos clave:** `crear_script_adhoc`, `registrar_ejecucion_script`,
`aprobar_ejecucion_script`, `recalcular_estado_ejecucion`,
`caducar_resultados_vencidos`.

**Tareas:** `generar_ejecuciones_programadas_task` (06:00),
`caducar_resultados_vencidos_task` (cada 10 min).

> **Gotcha.** El `timeout_segundos` se aplica **en la estación**, al proceso. Si el agente
> nunca contesta, el servidor lo cierra recién en el barrido de los 10 minutos y anota el
> motivo en `motivo_sin_respuesta` — mirá ahí antes de suponer.

> **Gotcha.** El fan-out está acotado por unidad de negocio: "toda la cadena" significa
> toda la cadena **de ese tenant**, no las tres.

---

### `software` — distribución de aplicaciones
**Para qué existe:** catálogo de aplicaciones, versiones e instalaciones remotas, más el
inventario de lo que ya está instalado.

**Modelos:** `AplicacionCatalogo`, `VersionAplicacion`, `SolicitudInstalacion`,
`ResultadoInstalacion`, `EventoInstalacion`, `SoftwareInstaladoDetectado`,
`InventarioProgramado`.

**Tareas:** `generar_escaneos_programados_task` (06:30).

> **Gotcha.** Los nombres de software con acentos venían guardándose corruptos hasta el
> agente 0.27 (`Configuración` → `ConfiguraciÃ³n`). Lo ya guardado **no se arregló solo**:
> requiere un reescaneo.

---

### `despliegues` — actualizaciones del POS
**Para qué existe:** llevar una versión nueva del POS a las estaciones, con respaldo y
reversión automática.

**Modelos:** `Despliegue`, `ResultadoDespliegue`, `EventoDespliegue`.

**Flujos clave:** `evaluar_freno_automatico` (corta el despliegue si fallan demasiadas),
`verificar_completado`.

> **Gotcha.** El agente es quien **aplica** un despliegue, así que este módulo **no sirve
> para instalar el agente mismo**. Esa instalación es manual (ver
> `generar_script_instalacion`).

---

### `aperturas` — alta de farmacia "cero-touch"
**Para qué existe:** abrir una farmacia nueva siguiendo una plantilla de pasos, con
tokens que permiten que las estaciones se enrolen ya aprobadas y configuradas.

**Modelos:** `PlantillaApertura`, `PerfilEstacionPlantilla`, `PasoPlantilla`, `Apertura`,
`PasoApertura`, `EventoApertura`, `TokenApertura`.

**Flujos clave:** `crear_apertura`, `aprobar_apertura`, `emitir_tokens`, `consumir_token`,
`enrolar_estacion_de_apertura`, `ejecutar_paso`, `evaluar_verificacion`.

> **Estado real:** **nunca se usó en producción** (0 aperturas, 0 tokens). Es
> exactamente el mecanismo que haría viable enrolar 1.800 estaciones sin tocar cada una,
> y está construido y sin estrenar.

---

### `viaticos` — control de gastos de campo
**Para qué existe:** que un técnico reporte sus gastos, que su coordinador los apruebe, y
detectar lo que se sale de la política (GFI-GTC-PR002).

**Modelos:** `ReporteViatico`, `ColaboradorZona`, `AlertaViatico`.

**Flujos clave:** `registrar_reporte`, `aprobar_reporte`, `observar_reporte`,
`rechazar_reporte`, `evaluar_alertas`, `consolidado_mensual`.

**Flujo completo:** el técnico carga → el coordinador aprueba, observa o rechaza → si lo
observó, el técnico **corrige y reenvía** (`viatico_editar` → `reenviar_reporte`), y el
reporte vuelve a PENDIENTE con las alertas re-evaluadas.

> **Estado real:** **0 zonas cargadas**, así que la alerta "fuera de zona" —la razón de
> ser del módulo— no se dispara nunca.

> **Resuelto el 26-sep-2026.** Un reporte observado no tenía cómo corregirse: el técnico
> veía el pedido del coordinador y el reporte se quedaba en OBSERVADO para siempre. Era
> el único callejón sin salida literal del sistema. Ahora se corrige sobre el mismo
> registro —no se duplica— y ambos lados reciben aviso en la bandeja in-app.

---

### `cumplimiento` — objetivos por estación/farmacia/colaborador
**Para qué existe:** seguir campañas del tipo "todas las estaciones tienen que tener X"
(antivirus, AD, cifrado) y medir el avance.

**Modelos:** `ActividadCumplimiento` y sus tres tablas de resultado (por estación, por
farmacia, por colaborador).

**Flujos clave:** `resolver_objetivos`, `generar_resultados`, `marcar_completado`,
`calcular_avance`.

---

### `cuentas` — quién puede ver qué
**Para qué existe:** el RBAC multi-tenant. Un solo modelo, pero gobierna todo el acceso.

**Modelo:** `PerfilUsuario` (unidades de negocio visibles, `acceso_todas_unidades`,
`telegram_chat_id`).

**Flujos clave:** `scope_por_unidad_negocio`, `verificar_acceso`,
`usuario_de_chat_telegram`.

**Roles** (ver `seed_permisos.py`): Administrador, Mesa de Ayuda, Soporte Técnico,
Técnico, Bodeguero, Auditor, Operador RMM, Coordinador de Viáticos.

> **Gotcha.** Mesa de Ayuda es **diagnóstico, no intervención**: ve alertas pero no las
> reconoce ni las cierra. Es deliberado.

---

### `auditoria` — el registro de quién hizo qué
**Para qué existe:** una fila por acción sensible. Un modelo, 176 líneas, y es lo que
responde "¿quién apagó esto?".

**Uso:** `registrar_evento(usuario=..., accion='estacion.pausar', objeto=..., request=...)`.

---

### `facturacion` — actividad mensual por estación
**Para qué existe:** contar estaciones activas por mes para facturar por endpoint. Es la
única tabla que **no se purga**.

**Modelo:** `ActividadMensualEstacion` (una fila por estación por mes).

> **Gotcha.** `registrar_actividad_mensual` hace `get_or_create` **en cada latido**. A
> 1.800 estaciones son ~30 escrituras/segundo para una fila que solo cambia una vez al mes.

---

> ### ~~`integraciones`~~ — dada de baja el 26-sep-2026
>
> Era andamiaje para conectores externos (Odoo, AD, ESET) que nunca se escribieron.
> `registrar_sync_pendiente`, único creador de filas, jamás se llamó desde fuera de la
> app, y el admin tenía `has_add_permission = False`: no había forma de crear datos ni a
> mano ni por código. Las tablas `sincronizacion_externa` y `evento_sync_externo` se
> borran en `catalogo/0033_baja_de_integraciones`, que **corta el deploy** si encuentra
> filas en vez de tirarlas.
>
> Para volver atrás: `git revert` del commit y `migrate integraciones`.

---

## La cuarta superficie: la app de campo

Los módulos de arriba se ven desde el **panel HTMX**. Pero el mismo backend tiene hoy
**cuatro** superficies, y tres de ellas no son el panel:

| Superficie | Qué es | Cómo habla con el backend |
|---|---|---|
| **Panel HTMX** | `apps/panel` — la web que usa mesa de ayuda y coordinación | Vistas Django + templates |
| **Agente RMM** | El ejecutable en cada estación | MQTT contra EMQX (`apps/mqtt_worker`) |
| **API REST** | `apps/mantenimiento/api_views.py` + `apps/monitoreo/api_urls.py` | `/api/v1/`, DRF con `TokenAuthentication` |
| **SAIDSOFT Campo** | `movil-campo/` — la app Flutter del técnico | Consume la API REST de arriba |

### `movil-campo` — la app del técnico

**Para qué existe:** lo que un técnico hace parado en la farmacia, y nada más. Cuatro
pantallas: **Trabajo** (sus mantenimientos, ordenados por urgencia de SLA y no por
fecha), **Visitas** (llegada y cierre de visitas técnicas), **Avisos** (su bandeja de
notificaciones) y **Ubicación** (envío de posición con consentimiento explícito).
Reemplaza a `movil/`, que arrastraba el modelo de datos de InvTICS.

**Cómo se explica sola:** [`movil-campo/README.md`](../movil-campo/README.md) — alcance,
decisiones de diseño, firma del APK y huella del certificado. No se duplica acá.

**Qué endpoints consume** (todos bajo `/api/v1/`, ver `apps/mantenimiento/api_urls.py`):

```
auth/token/  auth/yo/  catalogos/  equipos/nuevo/  notificaciones/[count|<id>/leer]
mantenimientos/  .../<id>/[checklist|iniciar|cerrar|cancelar|firmar|imagenes|repuestos]
visitas/  visitas/<id>/[iniciar|cerrar]
consentimiento-monitoreo/  ubicaciones-tecnico/
```

**De qué modelos depende directamente:** `Mantenimiento`, `VisitaTecnica`,
`ActividadChecklist`, `UbicacionTecnico`, `ConsentimientoMonitoreo`, `Notificacion`,
`Activo`. Un cambio de forma en cualquiera de ellos sale por el serializer y llega al
teléfono **sin que nada lo avise**: no hay versionado de API ni contrato compartido.

> **RESUELTO (26-sep-2026) — el orden por urgencia ya no está escrito dos veces.** La
> regla vivía solo en `pantalla_mantenimientos.dart` y decidía el orden de la app,
> mientras el panel ordenaba por `-fecha_programada`: la misma lista, dos órdenes, en
> dos lenguajes — mesa de ayuda y el técnico hablaban de "lo primero de la lista"
> mirando cosas distintas. Ahora vive en `services.ordenar_por_urgencia`, la consumen
> las dos superficies, y la app renderiza lo que recibe. El panel ordena por urgencia
> **por defecto** y conserva el orden por fecha en un selector, porque "qué entró hoy"
> es otra pregunta.
>
> De paso se cerró un N+1 que nadie había medido: `estado_sla` y `limite_resolucion`
> consultan `AcuerdoNivelServicio` en cada acceso, **2 consultas por fila** — unas 3.600
> en un listado de 1.800 mantenimientos. `precargar_acuerdos_sla` los carga una vez;
> para un objeto suelto (la pantalla de detalle) la propiedad sigue funcionando igual.
>
> Los **catálogos compilados** (`resultadosTecnicos`, `estadosGenerales` en
> `mantenimiento.dart`) siguen siendo copias a mano de las choices del backend, y eso es
> deliberado: el cierre tiene que funcionar sin señal. Desde el 26-sep-2026 hay UNA sola
> copia por catálogo dentro de la app —el alta usaba la de la API y el cierre la
> compilada— y **los dos tienen guard de desfase**.
>
> El guard que manda es `CatalogosCompiladosEnLaAppTests`, del lado de Django: lee el
> fuente Dart y lo compara contra `ResultadoTecnico.choices` /
> `EstadoGeneralEquipo.choices`, así que falla en el mismo commit que introduce la
> diferencia. La prueba de Flutter compara contra
> `test/datos/catalogos_produccion.json`, que es un **snapshot** y por sí sola no
> alcanzaba: si nadie lo refresca, pasa comparando la app contra una foto vieja. Por eso
> la de Django comprueba **también que el fixture esté al día**.
>
> **RESUELTO (26-sep-2026) — el gating por permisos.** Era la tercera: `sesion.dart`
> escondía botones que la API aceptaba igual, porque sus endpoints eran
> `IsAuthenticated` a secas mientras el panel exigía `view_`/`change_mantenimiento` y
> `view_`/`change_visitatecnica`. Ahora la API evalúa **los mismos codenames**, con un
> mapa explícito por acción en `apps/mantenimiento/api_permissions.py`
> (`DjangoModelPermissions` no servía: no chequea nada en GET y manda todo `@action`
> POST a `add_`, cuando el panel pide `change_`). El mapa es **fail-closed**: un
> `@action` nuevo sin permiso declarado se rechaza en vez de quedar abierto.
>
> En el mismo movimiento se cerró el hueco del otro lado: el grupo **'Soporte Técnico'**
> —donde están los 9 técnicos reales, no el grupo 'Técnico'— no tenía
> `view/change_visitatecnica` ni `add_ubicaciontecnico`, así que la app les ocultaba
> las pestañas **Visitas** y **Ubicación**, y sin posiciones GPS la verificación de
> presencia del panel devolvía `sin_datos` siempre. Ver `seed_permisos.py`.

> **Gotcha — todo lo que muta estado se encola offline** (`ColaOffline`, SQLite en el
> teléfono) y se sube al recuperar señal.
>
> **RESUELTO (26-sep-2026) — la hora de la acción.** La acción no llevaba la hora en que
> se hizo y el backend la fechaba al recibirla: un cierre hecho a las 10:00 y
> sincronizado a las 18:00 quedaba a las 18:00, corriendo el SLA de resolución, el de
> respuesta (`inicio_real`) y la ventana de verificación GPS. Ahora la cola manda
> `ocurrido_en` (`destinoDeAccion` en `sincronizador.dart`) y los servicios la usan para
> fechar cierre, llegada y firma. El campo es **opcional a propósito**: la app se
> distribuye a mano, así que durante semanas conviven APKs con y sin él, y el que no lo
> manda se sigue fechando con la hora del servidor.
>
> **RESUELTO (26-sep-2026) — reintentos y conflictos.** Eran dos problemas con el
> mismo síntoma ("1 pendiente" que nunca baja):
>
> - **Reintento.** El timeout de 20 s de `api.dart` se traduce a `SinConexion`, así que
>   una acción que el servidor SÍ procesó se reencolaba y se reintentaba. Ahora cada
>   acción viaja con `origen_id` —el id de su fila en `ColaOffline`, clave natural por
>   dispositivo— y `AccionOfflineAplicada` devuelve la respuesta original en vez de
>   duplicar el hecho o re-fallar. Para **firmar** alcanzó con la unicidad
>   `(mantenimiento, tipo_firma)` + `update_or_create`: firmar dos veces no es un hecho
>   nuevo, es el mismo hecho.
> - **Conflicto real.** Si alguien mueve el mantenimiento desde el panel mientras el
>   técnico lo cierra sin señal, el guard **no se relaja** (pisar la decisión más nueva
>   sería peor), pero ahora el rechazo es un **409** con `codigo: conflicto_de_estado`
>   en vez de un 400 genérico: la app lo distingue, deja de reintentar y se lo muestra
>   al técnico. El trabajo se guarda en **`CierreEnConflicto`** en el mismo momento en
>   que se devuelve el 409, y mesa de ayuda lo resuelve desde
>   `/mantenimientos/conflictos/` con dos acciones: *aplicar el cierre del técnico*
>   (reabre y cierra con su payload y su hora real, pasando por `cerrar_mantenimiento`
>   completo para que el activo vuelva de reparación) o *descartar con motivo*.
>
> **No espera a que alguien se acuerde de mirarlo:** el conteo sin revisar sale en el
> Centro de Monitoreo, y `escalar_cierres_en_conflicto` (Celery Beat, cada 15 min)
> reenvía el aviso pasadas **4 h** sin revisión — media jornada, para que caiga antes
> del cambio de turno y lo reciba quien ya tiene el contexto.
>
> **Hueco conocido:** `CierreEnConflicto` cubre solo el cierre de mantenimiento. Una
> visita que llega tarde devuelve el 409 (la app deja de reintentar) pero sus
> observaciones no se guardan en ninguna bandeja — `VisitaTecnica` no tiene modelo de
> eventos propio.

> **RESUELTO (26-sep-2026) — lo que se hace desde la app ahora deja rastro.** El panel
> registraba 15 acciones en `auditoria` y la API ninguna. Para mantenimientos el hueco
> era parcial (`EventoMantenimiento` ya quedaba, desde services); para **visitas no
> quedaba rastro en ningún lado**, porque `VisitaTecnica` no tiene modelo de eventos
> propio — una visita cerrada desde el celular era invisible mientras la misma acción
> desde la web dejaba fila. Ahora las 12 acciones de la API auditan con **los mismos
> nombres** que el panel (`mantenimiento.cerrar`, `visita.iniciar`, `activo.ingreso`…),
> y el `detalle` lleva `origen: app_movil` para poder distinguirlas. Un reintento
> idempotente no duplica la fila: la respuesta cacheada no vuelve a ejecutar la acción.

> **RESUELTO (26-sep-2026) — los altas de la API ya no aceptan ids de otro tenant.** Un
> `PrimaryKeyRelatedField` valida contra un queryset fijado en tiempo de import, así que
> `MantenimientoCrearSerializer.equipos` y `ActivoCrearSerializer.farmacia`/`bodega`
> aceptaban cualquier id existente: un técnico de MIA podía abrir un mantenimiento sobre
> un activo de San Gregorio pasando el id directo. El panel no tenía el agujero porque
> sus formularios arman los desplegables ya acotados. `AcotadoPorUnidadNegocioMixin` los
> acota con el mismo `scope_opcional_*` que usa `EquipoListView` — la variante opcional
> y no la estricta, porque `Activo.unidad_negocio` es nullable y el vacío significa
> "compartido".

> **RESUELTO (26-sep-2026) — el consentimiento de ubicación se podía retirar en
> teoría y no en la práctica.** El endpoint validaba con `.filter(aceptado=True)
> .exists()` sobre el HISTÓRICO, así que una revocación no revocaba nada: el `True`
> viejo seguía en la tabla y el servidor seguía guardando posiciones de alguien que
> había dicho que no. Además ninguna superficie permitía revocar. Ahora vale **el
> último** consentimiento (`services.puede_registrar_ubicacion`, la misma función que
> usa el GET, para que las dos mitades del flujo no vuelvan a discrepar), y la app tiene
> "Retirar el consentimiento" con confirmación. Revocar es un `POST` con
> `aceptado: false` y no un DELETE: el historial es append-only porque hay que poder
> demostrar qué se aceptó y cuándo.
>
> Una posición rechazada por esto llega con `codigo: sin_consentimiento` y la app la
> **descarta** en vez de reintentarla: es la única acción de la cola que se tira, porque
> reintentarla no va a funcionar nunca y conservarla en el teléfono sería guardar justo
> el dato que la persona pidió no registrar.

> **RESUELTO (26-sep-2026) — las posiciones ahora se purgan de verdad.** No se purgaban
> nunca, pese a que el comentario de `cerrar_mantenimiento` lo afirmaba para justificar
> persistir la distancia. Retención: **60 días** (`DIAS_RETENCION_UBICACIONES`, tarea
> diaria a las 3:40). El número está **atado a `ANTIGUEDAD_MAXIMA`** (30 días, lo máximo
> que una acción puede quedar en la cola offline): purgar a 30 correría contra esa
> ventana y un cierre que llega el día 29 verificaría contra posiciones recién borradas.
> **Las dos tienen que moverse juntas.** Lo que se pierde es la posición cruda, no el
> hecho: `distancia_verificacion_metros` se persiste al cerrar justo para sobrevivir a
> esto.

> **Gotcha — el certificado va empaquetado** (`movil-campo/assets/certs/cert.pem`). Si el
> servidor rota el suyo, la app deja de conectar con "el certificado del servidor no
> coincide" y hay que **regenerar el asset y publicar un APK nuevo**. Es la única copia
> versionada del certificado de producción: `deploy/certs/*.pem` está fuera de git.
>
> **RESUELTO (26-sep-2026) — se puede comprobar antes de que lo note un técnico.**
> `deploy/verificar-certificado.sh` compara la huella que sirve el servidor contra la
> que empaqueta la app y avisa si quedan menos de 90 días de vigencia. El runbook, con
> los cinco consumidores de `cert.pem` y **el orden** (primero el APK, después el
> servidor), está en `deploy/README-produccion.md`.

> **RESUELTO (26-sep-2026) — la app avisa que hay versión nueva.** La distribución sigue
> siendo manual, pero el problema real era que nadie se enteraba: un técnico podía pasar
> semanas con un APK viejo sin ninguna señal. `publicar_apk` deja el archivo con nombre
> canónico y escribe `media/movil/version.json`; `GET /api/v1/version-app/` lo lee y la
> app muestra un banner descartable. **Ojo con la vuelta:** los teléfonos que ya están en
> la calle no tienen el aviso, así que empieza a servir recién a partir de la versión
> siguiente a la que lo introduce.

> **Gotcha — sin el keystore no hay actualizaciones.** El release se firma con
> `movil-campo/android/cresio-campo-release.jks`, fuera de git. Perderlo obliga a
> desinstalar y reinstalar en cada teléfono, perdiendo la cola offline pendiente.

---

## Qué mirar primero cuando algo falla

| Síntoma | Mirá esto, en este orden |
|---|---|
| Una estación no obedece comandos | Desfase de reloj (>120 s = sorda) → ¿pausada? → ¿aprobada? → `motivo_sin_respuesta` de la ejecución |
| Un script quedó "en progreso" | Se cierra solo a los 10 min con el motivo. Si es viejo, el motivo dice que no hay diagnóstico confiable |
| Publicamos algo y no llegó a nadie | ACL de EMQX en `deploy/bootstrap-emqx.sh`. El PUBACK **no** confirma autorización |
| No llegan alertas por correo | `EMAIL_HOST_USER` está vacío en el `.env` del servidor |
| Una tarea periódica no corre | `docker compose logs celery_beat`. Ojo: un `schedule` numérico es relativo al arranque de beat, `crontab()` es absoluto |
| El panel anda pero no entra nada nuevo | El contenedor `worker` (MQTT). Es monohilo y es el único oído |
| Una estación "desapareció" | ¿Apagada, o el hilo del agente murió? Los eventos de Windows (`7031`) delatan al agente cayéndose |

## Probar los flujos a mano

Tres flujos del panel necesitan un estado previo que no se llega a armar clickeando: un
viático **ya observado**, un mantenimiento abierto **por otro usuario**, y una alerta
abierta sobre una estación **con equipo vinculado**.

```sh
python manage.py sembrar_escenarios_prueba              # simula
python manage.py sembrar_escenarios_prueba --aplicar
python manage.py sembrar_escenarios_prueba --limpiar --aplicar
```

Crea tres usuarios (`prueba.tecnico`, `prueba.coordinador`, `prueba.mesa`) con una
contraseña **generada al azar que se muestra una sola vez** — no hay ninguna clave fija
en el repo. Todo lo que crea lleva el prefijo `PRUEBA-` y `--limpiar` lo borra por ahí.

> **Si lo corrés en producción, corré el `--limpiar` después.** Crea usuarios reales con
> acceso al panel.

## Cosas que valen para todo el proyecto

- **Probar contra PostgreSQL, no SQLite.** No es preferencia: 125 pruebas que pasaban en
  SQLite fallaban en Postgres.
- **Los comandos masivos simulan por defecto.** `--aplicar` para que escriban.
- **Los secretos viven solo en `deploy/.env` del servidor.** Nunca en el repo.
- **Antes de borrar datos, mirarlos.** Un inventario de "huérfanos" mal hecho marcó como
  sobrante el agente vigente.
