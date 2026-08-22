# Plan de Modernización SAIDSOFT

**Fecha:** 16 de julio de 2026 · **Última actualización:** 17 de agosto de 2026
**Alcance:** ~600 farmacias · ~1.800 estaciones Windows · servidor propio (LAN/VPN)

> **Estado (31-jul-2026):** las Fases 0-5 de este documento (núcleo, panel de
> despliegues, activos, agente, integración POS, infraestructura de despliegue) están
> completas en código. Sobre esa base se construyó una segunda etapa no prevista en el
> plan original — convertir el sistema en multi-cliente (RMM tipo MSP) para las
> unidades de negocio de CRESIO — documentada en la **sección 9**. La **sección 10**
> es la auditoría de lo que falta cerrar antes de un rollout real (pruebas,
> producción validada de verdad, empaquetado del agente). Este archivo se actualiza
> junto con cada fase que se cierra — ver `CLAUDE.md` para la convención.

---

## 1. Diagnóstico del sistema actual

| Componente | Hoy | Problema |
|---|---|---|
| Panel web | Django 1.8 / Python 2.7 | Sin soporte desde 2018/2020, vulnerabilidades |
| Servicio MQTT | Node.js (mqtt v2, pg v7) | Obsoleto, lógica duplicada en 2 lenguajes |
| Base de datos | PostgreSQL (`bd_saidsof`) | Se conserva — los modelos usan `db_table` explícito |
| Credenciales | En texto plano en el código | Rotar y mover a variables de entorno (urgente) |
| Agente en equipos | No está en el repo | Se reescribe en .NET 10 |

## 2. Jerarquía y conceptos del negocio

```
Grupo TRX (canal de versión POS)     ej. TRX001, TRX004
  └── Farmacia                       ej. ML001, MAM01
        └── Estación                 ej. ML001-ADM, ML001-A, MAM01-B
```

- Cada farmacia pertenece a **un** grupo TRX. El grupo es el canal de versión del POS.
- Las estaciones son cajas iguales (no hay servidor local); `-ADM` queda previsto como rol de caché opcional a futuro.
- Parque mixto Windows 10 (incluye builds viejos) y Windows 11.

## 3. Arquitectura objetivo

```
┌─ Servidor central (LAN/VPN, Docker) ───────────────────────┐
│  Panel Django 5.2 LTS (Python 3.12) + PostgreSQL 16        │
│  + TimescaleDB (métricas)  + Worker MQTT Python (aiomqtt)  │
│  Broker EMQX con TLS y ACLs por tópico                     │
└────────────────────────────────────────────────────────────┘
                    ▲  MQTT sobre TLS (VPN)
                    ▼
┌─ Cada estación Windows ────────────────────────────────────┐
│  Agente SAIDSOFT: .NET 10 LTS, Windows Service,            │
│  self-contained (no depende del estado del SO),            │
│  MQTTnet, auto-enrolamiento, auto-actualizable             │
│      ▲ librería compartida Saidsoft.Client (NuGet interno) │
│  POS C# ┘ reporta versión/estado, recibe avisos            │
└────────────────────────────────────────────────────────────┘
```

**Decisiones clave y su porqué:**
- **MQTT se mantiene** — ideal para 1.800 clientes con conexión intermitente; mensajes retenidos para equipos apagados.
- **Agente separado del POS** — un exe no puede reemplazarse a sí mismo; el agente actualiza aunque el POS esté caído; corre como servicio con privilegios.
- **.NET 10 LTS** — soporte hasta nov-2028 (.NET 8 y 9 mueren en nov-2026). Self-contained: corre en Win10 desde build 1607 sin instalar nada.
- **Node.js desaparece** — su lógica pasa a un worker Python del mismo proyecto.
- **EMQX ≥ Mosquitto** — dashboard, ACLs y métricas de conexiones a esta escala.

## 4. Módulo A — Despliegues (objetivo principal)

### Flujo de un despliegue
1. Subir `.zip` (ejecutables POS) → el servidor calcula SHA-256, se asigna versión, ruta y comando.
2. Destino: **toda la cadena** / **grupos TRX** / farmacias específicas / tipo de equipo.
3. Modo de aplicación elegible por envío: inmediato · descargar ya y aplicar en ventana (ej. 22:00) · aplicar al cierre del POS.
4. Distribución en **olas escalonadas** (~150 equipos por ola) con límite de ancho de banda.
5. **Anillos**: piloto → 5% → resto, con freno automático si los errores superan umbral.

### Ciclo en el agente
```
descarga → verifica SHA-256 → espera ventana → cierra POS → respalda versión
→ copia archivos → relanza POS → ¿OK? → reporta OK
                                 └ no → rollback automático + reporta ERROR
```

### Versiones por canal
- Cada grupo TRX tiene versión objetivo; cada estación reporta su versión real en el heartbeat.
- El panel muestra matriz de cumplimiento y alerta desviaciones (equipo fuera de versión).

### Tópicos MQTT (contrato)
```
/saidsof/despliegue/global/            /saidsof/despliegue/grupo/{trx}/
/saidsof/despliegue/farmacia/{id}/     /saidsof/agente/{estacion}/estado/
/saidsof/agente/{estacion}/heartbeat/  (versión POS, versión agente, SO, serie HW)
```

## 5. Módulo B — Inventario de Activos (flujos CRESIO)

Mismo panel, misma base, misma auditoría. Modelo:

- **OrdenCompra** — N° OC (trazador), proveedor, fecha, bodega destino, novedades.
- **Bodega** — Machala, Loja, Cuenca, Portoviejo… con custodio responsable.
- **Activo** — código `CR-[TIPO]-[NNNN]` (secuencial global por tipo), marca, modelo, serie, garantía, OC origen. Estados: `En bodega / Asignado / En reparación / En tránsito / Dado de baja`. **Nunca se elimina.**
- **Consumible + StockBodega** — control por cantidad; tóner asociado a su CR-IMP.
- **Colaborador** — carga manual al inicio (importación CSV/API RRHH prevista para después).
- **Asignacion** — activo × colaborador, estado físico entrega/devolución, consumibles, quién registró.
- **EventoActivo** — historial inmutable: ingreso, asignación, devolución, reparación, baja.

Flujos: Compra → Ingreso a bodega (etiquetado CR obligatorio) → Asignación → Desvinculación (motivo A: salida del colaborador; motivo B: baja/reparación).

**Sinergia con Módulo A:** el agente reporta el número de serie del hardware → vinculación automática estación (ML001-A) ↔ activo (CR-DSK-0047). Detecta equipos movidos sin registro y activos "dados de baja" que siguen vivos.

## 6. Auditoría (transversal a ambos módulos)

1. **Acciones del panel** — registro inmutable de quién subió/creó/lanzó/pausó cada despliegue y cada cambio de catálogo (`django-auditlog`). Flujo de **cuatro ojos** para envíos a toda la cadena.
2. **Trazabilidad end-to-end** — línea de tiempo por despliegue × estación con timestamps de cada paso (recibido, descargado, verificado, aplicado, rollback).
3. **Historial de versiones por estación** — qué versión corrió cada equipo y cuándo, cruzado con heartbeat.
4. **Reportes exportables** (CSV/PDF) y retención configurable (eventos 2 años, heartbeats 30 días).

## 7. Plan por fases

| Fase | Contenido | Duración | Estado |
|---|---|---|---|
| **0. Preparación** | Backup BD, Git, rotar credenciales, variables de entorno, documentar tópicos | 1 sem | ✅ Hecho |
| **1. Núcleo** | Django 5.2/Py 3.12, modelo nuevo (grupos/farmacias/estaciones/despliegues/auditoría), worker MQTT Python (absorbe Node.js) | 2-3 sem | ✅ Hecho — tareas periódicas vía Celery Beat (ver `config/celery.py` y servicios `celery_worker`/`celery_beat` en `docker-compose.yml`, agregados después como fundación async) |
| **2. Panel despliegues** | HTMX + Tailwind, dashboard tiempo real, matriz de versiones por TRX, anillos y olas, aprobación cuatro ojos, reportes | 2-3 sem | ✅ Hecho — dashboard por polling HTMX, no Channels (suficiente a esta escala) |
| **2b. Módulo activos** | OC, bodegas, activos CR, consumibles, asignaciones, etiquetas, reportes CRESIO | 3-4 sem | ✅ Hecho |
| **3. Agente .NET 10** | Windows Service self-contained, MQTTnet, auto-enrolamiento por token, ciclo descarga/verifica/aplica/rollback, MSI para GPO, auto-actualización | 3-4 sem | 🟡 Agente funcional y probado manualmente (`C:\Proyectos\saidsoft-agente`); **falta el instalador MSI** — ver §10 |
| **4. Integración POS** | Librería Saidsoft.Client, heartbeat con versión/serie, vinculación activo↔estación, flujo cierre-actualiza-relanza | 2 sem | ✅ Hecho |
| **5. Despliegue** | docker-compose (Django+worker+PostgreSQL+EMQX/TLS), piloto 2-3 farmacias (incluir el Win10 más viejo), rollout por anillos, respaldos | 1-2 sem | 🟡 Stack escrito y validado sin Docker (config carga, YAML válido); **`docker compose up` nunca se corrió en un servidor real** — ver §10 |

**Total original: 16-20 semanas.** Prioridad acordada: Módulo A (despliegues) primero; Módulo B (activos) después de la Fase 2.

Con las Fases 0-5 completas, el proyecto pasó a una segunda etapa no prevista en el
plan original: convertirlo en una plataforma multi-cliente (RMM tipo MSP) para las
unidades de negocio de CRESIO. Ver **sección 9** para esas fases y **sección 10** para
lo que falta cerrar antes de un rollout real.

## 8. Riesgos y mitigaciones

| Riesgo | Mitigación |
|---|---|
| Actualización mala llega a 600 farmacias | Anillos + freno automático por umbral de error + rollback local |
| VPN saturada por descargas masivas | Olas escalonadas + throttling; rol caché en -ADM previsto como plan B |
| Win10 builds viejos fallan con el agente | Self-contained .NET 10; piloto incluye la máquina más antigua; mínimo build 1607 |
| Win10 viejo sin TLS 1.2 forzado rompe scripts RMM ad-hoc (Invoke-WebRequest) | TLS 1.2 forzado explícito en `generar_comando_instalacion_meshcentral` (ver §10-G); falta validar contra una estación Windows 10 real sin parchar |
| Windows 10 sin soporte (oct-2025) | El inventario de SO del heartbeat alimenta el plan de migración a W11 |
| Instalar agente en 1.800 equipos | MSI + GPO/script, una sola vez; después se auto-actualiza por su propio canal |

## 9. Extensión multi-tenant / RMM (fases R1-R9 + roadmap de monitoreo M1-M5 + ancho de banda + gestión de activos)

El sistema pasó de servir una sola operación a ser un RMM tipo MSP para las
unidades de negocio de CRESIO (San Gregorio, MIA, 7DIAS — ver `UnidadNegocio` en
`apps/catalogo/models.py`). Fases completas, en orden:

| Fase | Contenido | Estado |
|---|---|---|
| **R1 — Multi-tenancy** | `UnidadNegocio` como raíz del tenant. `Farmacia.unidad_negocio` obligatoria; `Despliegue`/`EjecucionScript` con `unidad_negocio` obligatoria; RBAC centralizado en `apps/cuentas/services.py` (`scope_por_unidad_negocio`, `verificar_acceso`, variantes `_opcional`/`_activa`); selector de "unidad de negocio activa" en el panel | ✅ Hecho |
| **R2 — Motor de alertas** | `ReglaAlerta`/`Alerta` en `apps/monitoreo`; evaluación en tiempo real desde `manejar_metricas` (mqtt_worker) + `marcar_estaciones_offline` (regla `sin_heartbeat`); notificación por correo al abrir una alerta | ✅ Hecho |
| **R3 — Scripting remoto** | Ya existía antes de esta etapa (`apps/scripts`, comando `ejecutar_script` del agente) | ✅ Hecho (preexistente) |
| **R4 — Parcheo de terceros + recurrencia** | `ScriptProgramado` (política "correr cada N días", mismo patrón que `MantenimientoProgramado`), scripts winget sembrados con `seed_scripts_parcheo`. No tocó el agente — ya soportaba `ejecutar_script` | ✅ Hecho |
| **R5 — Acceso remoto (MeshCentral)** | Ya existía antes de esta etapa | ✅ Hecho (preexistente) |
| **RBAC fast-follow** | `activos`/`mantenimiento`/`cumplimiento` conectados al RBAC de R1 (antes solo `@login_required`, sin escopar por cliente). `ColaboradorForm` ganó el campo `unidad_negocio`; `registrar_asignacion` lo hereda del colaborador al activo | ✅ Hecho |
| **R6a — Reportes por cliente** | Resumen imprimible por unidad de negocio (`/reportes/cliente/`) + CSVs de activos/alertas + `reporte_cumplimiento` ahora escopado (antes mostraba todo sin filtrar) | ✅ Hecho |
| **R6b — Facturación por endpoint** (13-ago-2026) | `ActividadMensualEstacion` (`apps/facturacion`) registra, por estación y mes calendario, que hubo al menos un heartbeat (`registrar_actividad_mensual`, llamado desde `manejar_heartbeat`/`manejar_estado_despliegue` en `apps/mqtt_worker/services.py`; idempotente, no se puede reconstruir para meses previos a que se activó). "Endpoint activo" = esa fila. `resumen_facturacion`/`estaciones_facturables` cuentan por unidad de negocio y período; CSV en `/reportes/facturacion.csv` + integrado al resumen por cliente (`/reportes/cliente/`) | ✅ Hecho |
| **Windows Update nativo v1 (resto de R4)** (13-ago-2026) | Comando `escanear_actualizaciones` (botón "Escanear ahora" en la ficha de estación): v1 es solo escaneo/reporte, el agente nunca instala ni reinicia solo. El agente (Python, ver §10-K) chequea conectividad a internet antes de escanear (`_hay_conexion_a_internet`, endpoint NCSI de Microsoft, 5s timeout — muchas estaciones del piloto no tienen salida a internet y `Search()` de Windows Update se cuelga varios minutos sin ese chequeo) y, si falla, reporta el motivo en `Estacion.windows_update_ultimo_error` para que el panel se lo muestre al operador en vez de dejar el escaneo colgado. Ver `agente-prueba/README.md` y `apps.mqtt_worker.services.manejar_windows_update` | ✅ Hecho (v1: solo escaneo, no instala) |
| **Credenciales MQTT por estación** (adelanto parcial de "ACLs MQTT/EMQX por tenant", 13-ago-2026) | `apps.mqtt_worker.emqx_admin.aprovisionar_credencial_estacion` le da a cada estación, en su enrolamiento, una credencial MQTT propia (no la compartida) con ACL restringida a sus propios tópicos — aislamiento real a nivel de broker, no solo de aplicación/BD como hasta ahora. Opcional y sin romper nada mientras no se configure (`EMQX_ADMIN_CONFIG` vacío = desactivado, sigue usando la credencial compartida). Rollout gradual: requiere que la estación ya tenga el agente Python nuevo (§10-K) y volver a enrolarse — `python manage.py seed_scripts_migracion_mqtt` crea el script que fuerza ese re-enrolamiento. `deploy/emqx-narrow-acl-agente.sh` (nuevo, confirmación manual) angosta la ACL de la credencial compartida recién cuando toda la flota ya migró | 🟡 Implementado, rollout pendiente |
| **Monitoreo cruzado MQTT × MeshCentral** (13-ago-2026, **validado contra el servidor real de producción el mismo día**) | `EstadoDispositivo`/`EventoMonitoreo` (`apps/monitoreo`) — snapshot e histórico de transiciones de conectividad por (estación, fuente). Puerto de entrada único `registrar_estado_dispositivo`, llamado desde `manejar_heartbeat`/`marcar_estaciones_offline` (fuente MQTT) y desde `apps.monitoreo.adapters.meshcentral.AdaptadorMeshCentral` (fuente MeshCentral, WebSocket `control.ashx`, eventos `nodeconnect` en tiempo real). Worker de larga duración nuevo `python manage.py run_meshcentral_worker` (calco de `run_mqtt_worker`), servicio `meshcentral_worker` en `docker-compose.yml`. Nueva métrica `agente_caido_red_viva` en `ReglaAlerta` (evaluada cada ~7min por `evaluar_cruce_monitoreo`, Celery Beat): distingue "el agente se cayó" (MQTT offline, MeshCentral sigue viendo el equipo) de "se cayó la red" (las dos fuentes lo ven mal). Opcional (`MESHCENTRAL_API_CONFIG` vacío = el worker no arranca, sin afectar nada más). Pill de estado nuevo en la ficha de estación (`Estacion.estado_meshcentral`). El puerto `FuenteMonitoreo` (`apps/monitoreo/adapters/base.py`) queda listo para sumar ESET PROTECT cuando se apruebe el acceso a su API — ver nota abajo | ✅ Hecho y validado |
| **R7 — Inventario de software instalado** (16-ago-2026) | Gap identificado comparando saidsoft-core contra propuestas comerciales reales de Aranda ADM/Patch y NinjaOne (ambas lo tienen como capacidad núcleo). Comando `consultar_software_instalado` (botón "Escanear software instalado" en la ficha de estación, mismo patrón "info bajo demanda" que Windows Update): el agente lee las claves de registro `Uninstall` (64/32 bits + `HKCU`, sin usar `Win32_Product`/WMI — lento y con efectos secundarios) y reporta `[{nombre, version, fabricante}]` al tópico `/saidsof/agente/{codigo}/software_instalado/`. Nuevo modelo relacional `SoftwareInstaladoDetectado` (`apps/software`, no un JSONField — el valor es poder *buscar* "qué estaciones tienen instalado X"), semántica de snapshot (`manejar_software_instalado` reemplaza el inventario completo de la estación en cada escaneo). Reporte de flota en `/reportes/software-instalado.csv`, filtrable por nombre — valor real para compliance de licenciamiento y detectar software no autorizado en ~1.935 estaciones. Requirió agregar la ACL de `subscribe` del tópico nuevo para el usuario `worker` en `deploy/bootstrap-emqx.sh` (el usuario `agente` ya tenía `/saidsof/#` completo, no hizo falta tocarlo) — mismo tipo de gotcha que el bug de §10-L, evitado esta vez de entrada | ✅ Hecho (servidor probado — 328 tests OK; agente solo `py_compile`, sin validar aún contra una estación real) |
| **R8 — Monitoreo continuo CPU/RAM/disco** (16-ago-2026) | Hallazgo real de esta fase: el pipeline de métricas (`MuestraMetrica`, `ReglaAlerta`, tópico `/saidsof/agente/+/metricas/`, handler `manejar_metricas`) ya existía del lado servidor y estaba probado, pero **ningún agente real lo alimentaba** — se había construido pensando en el agente C# original (nunca completado en ese punto) y el reemplazo Python (§10-K) tampoco lo implementó hasta ahora. Se agregó `bucle_metricas` al agente (`agente-prueba/agente_prueba.py`, calco de `bucle_heartbeat`, `--intervalo-metricas` default 300s) que mide CPU/RAM/disco vía CIM y publica al tópico ya existente — sin tocar ACLs de EMQX. Nuevos campos `disco_total_gb`/`disco_libre_gb` (+ property `disco_usado_pct`) en `MuestraMetrica` y choice `Metrica.DISCO_USADO_PCT` para alertar por poco espacio en disco, mismo mecanismo genérico (`getattr(muestra, regla.metrica)`) que ya evaluaba CPU/RAM sin cambios. El flag `Estacion.monitorear_recursos` (ya viajaba en la respuesta de enrolamiento, pero ningún agente lo leía) ahora sí se persiste en `identidad.json` y controla si el agente reporta — se respetó el mecanismo ya diseñado en vez de reportar sin condición para toda la flota. Panel (`/monitoreo/`) con tarjeta y gráfico de disco nuevos, mismo patrón visual que CPU/RAM | ✅ Hecho (servidor probado — 332 tests OK; agente solo `py_compile`, sin validar aún contra una estación real) |
| **R9 — Políticas de energía, solo lectura v1** (16-ago-2026) | Último gap identificado frente a Aranda (lo publicita como capacidad propia, caso de ahorro del 94%). Mismo criterio conservador que Windows Update v1: v1 es solo reportar el plan activo, nunca aplicar/forzar uno — aplicar queda diferido a una decisión de negocio futura (afecta cajas en producción, riesgo comparable a "reiniciar"). No hizo falta un comando nuevo: se agregó una línea al script PowerShell que ya corre `consultar_info` (`Get-CimInstance -Namespace root\cimv2\power -ClassName Win32_PowerPlan`), así que viaja en el mismo payload y usa el mismo botón "Actualizar ahora" que ya existía — cero UX nueva. Dos campos nuevos en `Estacion` (`power_plan_actual`, `power_plan_ultima_verificacion`), `manejar_info_equipo` los guarda solo si el agente reportó un valor (conserva el último conocido si no, mismo criterio que Windows Update ante un escaneo fallido). Sin permiso nuevo (ya cubierto por `catalogo.consultar_info_estacion`) | ✅ Hecho (servidor probado — 336 tests OK; agente solo `py_compile`, sin validar aún contra una estación real) |
| **Mejoras de rollout de R7/R8** (16-ago-2026) | Autocrítica pedida por el usuario tras cerrar R7-R9: sin esto, ninguna de las dos fases se iba a usar en la práctica a escala de ~1.935 estaciones. (a) **Activar monitoreo en lote**: acciones nuevas en `/admin/catalogo/estacion/` (`activar_monitoreo_recursos`/`desactivar_monitoreo_recursos`) — antes solo se podía tildar `monitorear_recursos` fila por fila (`list_editable`); ahora se aplica a toda la selección (filtrable por grupo/farmacia), auditado por estación. (b) **Escaneo de software programado**: nuevo modelo `InventarioProgramado` (`apps/software`, mismo shape de destino que `ScriptProgramado`, sin `Script` de por medio — dispara directo el comando fijo `consultar_software_instalado`), servicio `generar_escaneos_vencidos`, comando de management y tarea diaria de Celery Beat (`generar-escaneos-programados`), administrado desde `/admin/software/inventarioprogramado/`. Quedó fuera de alcance (explícitamente no pedido): que el cambio de `monitorear_recursos` llegue al agente sin esperar al próximo re-enrolamiento | ✅ Hecho (343 tests OK) |
| **Monitoreo de errores del POS vía su propio log** (16-ago-2026) | Surgió de una pregunta del usuario sobre si el `.exe.Config` del POS real (Zabyca.Pos.Desktop, Farmamia/Elipsys) servía para monitorear sus llamadas a la API central — no (es config estática), pero el POS también trae `log4net.config`, que sí escribe un log real (`Logs\GeneraXML.txt` pese al nombre — el usuario compartió un fragmento de producción real y confirmó que captura errores generales de la app, incluidos timeouts de conexión a base y un bug de esquema de fidelización repetido cientos de veces sin que nadie se enterara). Agente: hilo nuevo `bucle_log_pos` (calco de `bucle_metricas`, `--intervalo-log-pos` default 300s) lee el log desde la última posición guardada en `identidad.json` (`pos_log_posicion`, con detección de truncado/rotación), agrupa por mensaje exacto los niveles ERROR/FATAL (regex validado contra el fragmento real que compartió el usuario) y descarta el resto del stack trace — no viaja al servidor. Reusa `--pos-carpeta-instalacion` que ya existía, sin argumento nuevo obligatorio. Servidor: nuevo modelo `PosErrorDetectado` (`apps/monitoreo`, acumulativo por `(estación, mensaje)`, a diferencia del snapshot de R7), handler `manejar_pos_errores`, métrica nueva `Metrica.POS_ERRORES` y evaluador `evaluar_regla_pos_errores` que reusa `_cumple`/`abrir_o_mantener_alerta`/`resolver_condicion` del motor de alertas existente sin tocarlo — **alerta real desde el día uno** (correo al abrir, resolución automática con una ventana limpia), decisión explícita del usuario. Limitación de v1 aceptada: mensajes con detalle variable en la misma línea (ej. "VENTA SIN LOTE: <código>...") no dedupan entre sí — no es el caso de uso principal (conectividad/esquema, que sí repiten idéntico) | ✅ Hecho (357 tests OK; agente solo `py_compile` + regex validado a mano contra el log real compartido) |
| **Clasificación sistema/negocio del monitoreo de errores del POS** (17-ago-2026) | El usuario confirmó que "VENTA SIN LOTE" (ver fila de arriba) es **rutinario en la operación real**, no esporádico — contarlo igual que un timeout de conexión habría inundado la alerta de falsos positivos apenas se activara en una farmacia con volumen normal de ventas (una preocupación real de escala a 600+ farmacias, no teórica). `PosErrorDetectado.categoria` nuevo (`sistema`/`negocio`) + `apps.monitoreo.services.clasificar_error_pos` (lista chica de prefijos conocidos, mismo criterio que `PERMISOS_LITERALES` — se edita a mano a medida que se encuentran nuevos patrones en producción, sin modelo/admin todavía). Ante un mensaje no reconocido, la clasificación por defecto es `sistema` (ante la duda, se trata como señal real). `manejar_pos_errores` solo suma a `evaluar_regla_pos_errores` los de categoría `sistema`; los de `negocio` se siguen guardando y quedan visibles en la ficha de la estación, distinguidos con un pill neutral en vez de crítico. Se reclasifica en cada reporte (no solo al crear la fila), así que si la lista de prefijos gana un patrón nuevo más adelante, las filas viejas se ponen al día solas sin migración de datos | ✅ Hecho |
| **M1 — Rollup de alertas entre estaciones** (17-ago-2026) | Primera fase del roadmap de "monitoreo a estándar de industria" (M1-M5, ver plan aprobado en sesión — el usuario pidió una evaluación honesta de si esto llega a ese estándar; la respuesta fue "como RMM sí, como práctica de monitoreo madura todavía no", con 6 gaps identificados). M1 ataca el primero y más urgente: sin agrupar, un bug sistémico en 40 farmacias generaba 40 alertas idénticas, entrenando a ignorar `/alertas/` a escala. Dos rollups nuevos en `apps/panel/views/alertas.py`: **(a)** `alertas_lista` gana `?vista=agrupada` — agrupa `Alerta` ABIERTA/RECONOCIDA por `regla_id` (`Count('estacion', distinct=True)`), toggle nuevo en `alertas_lista.html` (clase `.segmented`, agregada a `static_src/components.css` — no existía antes). **(b)** `pos_errores_flota` (nuevo, `/alertas/errores-pos/`): agrupa `PosErrorDetectado` (categoría `sistema` solamente) por *mensaje exacto* — la regla `pos_errores` sola no alcanza para distinguir "una farmacia con un timeout" de "40 farmacias con el mismo bug de esquema", que es justo el caso real que motivó esta fase (ver fila del bug de fidelización arriba). Reusa `scope_por_unidad_negocio_activa` en ambas, ningún modelo nuevo. Se validó un mockup visual navegable (Artifact, no el editor de Claude Design — el entorno no tiene Node/Bun) antes de programar, aprobado por el usuario | ✅ Hecho (374 tests OK) |
| **M2 — Ventanas de mantenimiento** (17-ago-2026) | Segunda fase del roadmap: silenciar a propósito las alertas de un destino de estaciones durante una acción operativa propia (despliegue, reinicio masivo) para que no se confunda con un problema real. Nuevo modelo `VentanaMantenimiento` (`apps/monitoreo`, mismo shape de destino que `ScriptProgramado`/`Despliegue` — `unidad_negocio`/`destino_tipo`/`grupos`/`farmacias`/`estaciones`, resuelto con `apps.catalogo.services.resolver_estaciones`), más `desde`/`hasta`/`motivo`/`activo`. Un solo hook nuevo — `ventana_mantenimiento_activa(estacion)` consultado al principio de `abrir_o_mantener_alerta` — cierra el silenciamiento para las cuatro rutas de evaluación existentes (métricas, sin heartbeat, bitlocker, pos_errores) y las futuras, sin tocar cada evaluador por separado. CRUD en `/monitoreo/mantenimiento/` (mismo patrón que `scripts_programados_lista`/`script_programado_crear`, permiso propio `monitoreo.add_ventanamantenimiento` otorgado al rol "Operador RMM"), enlazado desde el botón "Mantenimiento" en `/alertas/`. Aviso "En mantenimiento hasta HH:MM" en la ficha de la estación cuando aplica ahora mismo (mismo `ventana_mantenimiento_activa`, vía un helper `_render_info_modal` nuevo que centraliza el contexto común de las 6 vistas que renderizan ese modal) | ✅ Hecho (387 tests OK) |
| **M3 — Notificación por Teams + escalamiento** (18-ago-2026) | Tercera fase, con tres decisiones de negocio que el plan dejaba abiertas a propósito, resueltas con el usuario antes de programar: (1) canal nuevo = solo webhook de **Microsoft Teams** (no Slack), (2) umbral de escalamiento **global**, no por `ReglaAlerta`, (3) el segundo aviso va a los **mismos destinatarios/canales** que el original (sin lista de escalamiento separada). Nuevo modelo `CanalNotificacion` (`apps/monitoreo`, `unidad_negocio` nullable = canal global, `tipo` limitado hoy a `webhook_teams`, admin-only — no justifica un CRUD de panel para 3 unidades de negocio). `notificar_alerta` (`apps/monitoreo/services.py`) ahora, además del correo de siempre, hace `POST {"text": ...}` (formato clásico del conector O365) a todo `CanalNotificacion` activo que aplique a la unidad de negocio de la alerta (global o propio) — vía `urllib.request` (mismo patrón stdlib que `apps.mqtt_worker.emqx_admin`, sin sumar `requests` como dependencia nueva); un webhook caído nunca rompe la notificación (`try/except` alrededor, mismo criterio que `fail_silently=True` de `send_mail`). **Escalamiento**: campo nuevo `Alerta.escalada_en`; `escalar_alertas_abiertas()` (tarea Celery Beat `escalar-alertas-abiertas`, cada 10 min) reenvía por los mismos canales cualquier `Alerta` en estado `ABIERTA` (nunca `RECONOCIDA`) más vieja que `UMBRAL_ESCALAMIENTO_MINUTOS` (constante global = 30, mismo estilo que `FRESCURA_MESHCENTRAL_MINUTOS`), marcando `escalada_en` para no repetir el aviso en cada corrida — el asunto/cuerpo llevan el prefijo "SIN ATENDER" | ✅ Hecho (398 tests OK) |
| **M5 — Dashboard de tendencia de flota** (18-ago-2026) | Última fase de código del roadmap (M4 sigue bloqueada por el servidor real, ver abajo). Nueva vista `/monitoreo/tendencia/` (`apps.panel.views.monitoreo.tendencia_flota`): series de las últimas 12 semanas (buckets lunes-domingo calculados a mano, sin `TruncWeek` de la ORM, para no depender de cómo trunca semanas cada backend) — **(a)** alertas abiertas por semana por severidad + resueltas por semana, sobre `Alerta` escopeado por tenant; **(b)** promedio de CPU/RAM/disco de la flota (solo estaciones con `monitorear_recursos=True`) por semana — RAM/disco se calculan como razón de promedios (`avg(ram_usada)/avg(ram_total)`, no promedio de razones por muestra) para evitar traer cada `MuestraMetrica` a Python fila por fila a la escala de la flota. Reusa `apps.monitoreo.graficos.construir_grafico` (mismo SVG inline que ya grafica CPU/RAM/disco por estación individual) para las 6 series, sin sumar una librería de gráficos nueva. **(c)** Top de errores del POS: decisión explícita del usuario tras plantearle el gap — `PosErrorDetectado` solo guarda un contador acumulado de por vida (sin registro de cuándo ocurrió cada reporte), así que no hay con qué armar una tendencia semanal real sin agregar un modelo nuevo; el dashboard muestra el mismo "top actual" de `pos_errores_flota` (factorizado a `apps.panel.views.alertas._top_mensajes_pos_errores` para no duplicar la query), acotado a 5 filas, con link a la vista completa — no una serie de tiempo | ✅ Hecho (404 tests OK) |
| **Monitoreo de ancho de banda por estación** (19-ago-2026) | Motivado por un problema real: farmacias reportando lentitud, y al revisar el usuario confirmó estaciones acaparando el enlace. Extiende R8 (`bucle_metricas`) en vez de crear un subsistema nuevo — el agente ya mide CPU/RAM/disco cada ciclo, se le sumó `Get-NetAdapterStatistics` sobre el adaptador de la ruta por defecto (`Get-NetRoute` a `0.0.0.0/0`, no se suman todas las interfaces) al mismo script PowerShell. Los contadores son acumulados (bytes desde que arrancó el adaptador); como el agente corre como servicio de Windows de larga duración (confirmado en producción esta sesión), la tasa (kbps) se calcula en memoria de proceso comparando contra la muestra anterior — sin necesidad de persistir nada extra. Campos nuevos `red_recibido_kbps`/`red_enviado_kbps` en `MuestraMetrica` + property `red_total_kbps` + `Metrica.RED_TOTAL_KBPS`, mismo mecanismo genérico de alertas que CPU/RAM/disco (cero cambios en el evaluador). Tile nuevo en `/monitoreo/` (lista y detalle) y en `/monitoreo/tendencia/`, mismo patrón visual que latencia (sin escala fija — kbps no tiene techo natural) | ✅ Hecho (420 tests OK, junto con la fila de abajo; agente solo `py_compile`, sin validar aún contra una estación real) |
| **Monitoreo de ancho de banda por farmacia (Mikrotik/SNMP)** (19-ago-2026) | Complementa la fila de arriba: el Mikrotik de cada farmacia (~600, uno por sitio, config uniforme) no reparte tráfico por estación (sin Queues por IP/MAC — confirmado con el usuario), así que solo puede dar el consumo TOTAL del enlace, no por equipo. Subsistema nuevo, no una extensión de R8: nuevo modelo `MuestraRedFarmacia` (`apps/monitoreo`, farmacia-scoped en vez de estación-scoped — sigue el precedente de modelos paralelos por granularidad que ya usa `apps.cumplimiento` con `ResultadoCumplimientoEstacion`/`Farmacia`, no se fuerza en `EstadoDispositivo`/`Alerta` ni en el puerto `FuenteMonitoreo`, que son para presencia online/offline de un dispositivo, forma distinta). Nuevo campo `Farmacia.ip_router`. Poller nuevo `apps/monitoreo/mikrotik.py`: WALK sobre `ifDescr` para resolver el `ifIndex` de la interfaz WAN (cacheado en proceso), GET de `ifHCInOctets`/`ifHCOutOctets` (contadores de 64 bits, evita wraparound) — primera dependencia no-stdlib de este tipo en el proyecto (`pysnmp>=7`, que es asyncio-nativo, sin API sincrónica; concurrencia acotada con `asyncio.Semaphore` en vez de threads). La tasa se calcula diferenciando contra la última `MuestraRedFarmacia` persistida en BD (no en memoria de proceso — a diferencia de la estación, este poller corre en un task de Celery Beat cada 5 min que puede reiniciar entre corridas). Config global (`MIKROTIK_SNMP_CONFIG`, mismo patrón que `MESHCENTRAL_API_CONFIG`) — vacío no rompe nada. **Decisión de alcance confirmada con el usuario**: v1 es solo visibilidad (`/monitoreo/red-farmacias/`, coloreado por umbral fijo) — sin `Alerta` ni notificación todavía, para no hacer `Alerta.estacion` opcional (FK obligatoria hoy, usada en decenas de lugares de esta sesión); se automatiza en v2 una vez confirmado que el dato SNMP es confiable, mismo criterio que Windows Update v1/Plan de energía v1. **Bug real encontrado y corregido durante la implementación**: `GenericIPAddressField` normaliza `''` a `None` al guardar — un `.exclude(ip_router='')` encadenado después de `.exclude(ip_router__isnull=True)` terminaba excluyendo TODAS las filas (incluidas las que sí tienen una IP real), porque en SQL `columna = NULL` nunca es verdadero; `__isnull=True` alcanza solo | ✅ Hecho (420 tests OK; sin validar contra un Mikrotik real todavía — falta confirmar el `ifIndex`/nombre de interfaz real y si la comunidad SNMP puede habilitarse de forma uniforme en los ~600 sitios) |
| **Ajuste de la community SNMP tras validar contra un Mikrotik real** (20-ago-2026) | Al configurar el primer sitio piloto (`ML006`) se encontró que la community SNMP **no es compartida/global como se había asumido al diseñar la fase de arriba** — es el código de la farmacia en minúscula (`ml006` para ML006), confirmado por el usuario como la convención en los ~600 sitios. `apps.monitoreo.mikrotik._comunidad_para(farmacia)` la deriva de `Farmacia.codigo.lower()` en vez de leer un valor fijo de `MIKROTIK_SNMP_CONFIG` — simplifica el rollout (no hace falta cargar ni distribuir ningún secreto compartido, cada Mikrotik ya trae la suya). `MIKROTIK_SNMP_CONFIG` queda solo con `PUERTO`/`INTERFAZ_WAN` | ✅ Hecho |
| **Interfaz WAN también resuelta sola, no por nombre configurado** (20-ago-2026) | Mismo patrón que la fila de arriba: `/ip route print` contra `ML006` mostró que la interfaz WAN real es `ether3_Telconet`, no `"ether1"` (el default asumido al diseñar la fase) — el nombre tampoco es uniforme entre sitios. Se reemplazó `MIKROTIK_SNMP_INTERFAZ_WAN` (config por sitio) por resolución automática vía SNMP: GET de `ipRouteIfIndex` (IP-MIB, OID `1.3.6.1.2.1.4.21.1.2.0.0.0.0`) sobre la ruta por defecto activa, que da directo el `ifIndex` de la interfaz que el router usa ahora mismo para salir a Internet — sin necesitar su nombre. `MIKROTIK_SNMP_CONFIG` queda solo con `PUERTO` (default 161); no hace falta cargar ningún dato de Mikrotik por sitio más allá de `Farmacia.ip_router`. **Validado de punta a punta contra dos routers reales de producción (`MC001`, `ML006`)**: `ipRouteIfIndex` devolvió el índice correcto en `MC001` (`3`), la lectura de `ifHCInOctets`/`ifHCOutOctets` con ese índice funcionó, y `sincronizar_ancho_banda_farmacias()` guardó una `MuestraRedFarmacia` real — la segunda corrida ya mostró una tasa calculada real en `/monitoreo/red-farmacias/` (1004,8 kb/s bajada / 170,6 kb/s subida). `ML006` (todavía bloqueada por firewall del lado de la farmacia, pendiente de habilitación) falló con un `WARNING` prolijo en el log sin interrumpir el sondeo de `MC001` — confirma en producción real el comportamiento "un router caído no tumba la corrida" | ✅ Hecho y validado contra hardware real |
| **Aviso de software desactualizado (catálogo manual)** (20-ago-2026) | Pedido del usuario: "algo parecido al agente de ESET que escanee las app y notifique una actualización". Alcance acordado con el usuario (catálogo manual de versiones, sin integrar ningún feed externo; una lista puntual de apps vigiladas, no todo el inventario; solo visibilidad v1, sin `Alerta`/correo todavía — mismo criterio que Windows Update v1/Plan de energía v1/red por farmacia). Campo nuevo `AplicacionCatalogo.version_mas_reciente_conocida` (cargado a mano en la ficha de la app — vacío = esa app no se vigila). Nuevo servicio `apps.software.services.estaciones_desactualizadas(aplicacion)` cruza el inventario ya detectado por R7 (`SoftwareInstaladoDetectado`, coincidencia por nombre `icontains` — el nombre real en el registro de Windows no siempre coincide letra por letra con el del catálogo, ej. sufijo "(64-bit)") contra esa versión conocida, excluyendo las que ya coinciden exacto. Nueva vista `/aplicaciones/desactualizadas/` (`software_desactualizado_lista`): una tarjeta por app vigilada con las estaciones que quedaron atrás. Sin modelo ni migración de alertas — reusa el inventario y el catálogo que ya existían | ✅ Hecho (431 tests OK) |
| **Gestión de activos: aislamiento multi-tenant de Bodega/OC/Kardex** (20-ago-2026) | El usuario pidió retomar `apps/activos` (sin tocar en toda la sesión). Se repitió el ejercicio de comparar contra ITAM comercial (Aranda Asset/NinjaOne/GLPI) y se encontró un **bug real, no una funcionalidad nueva**: `Bodega`/`OrdenCompra`/`OrdenCompraDetalle`/`RecepcionLote`/`MovimientoInventario`/`StockBodega` no tenían ningún campo `unidad_negocio` — cualquier usuario autenticado veía las bodegas, compras y kardex de las tres unidades de negocio (SG/MIA/7DIAS) sin filtro (solo `Activo`/`Colaborador` estaban escopados). Confirmado con el usuario: además de bodegas/compras propias de cada unidad, también hay compartidas (bodega central) — mismo criterio "compartida o del tenant" que `Script`/`AplicacionCatalogo` (`Bodega.unidad_negocio`/`OrdenCompra.unidad_negocio` nuevos, ambos nullable; `OrdenCompraDetalle`/`RecepcionLote` se escopan transitivamente vía `orden_compra`). `MovimientoInventario` no tiene su propio `unidad_negocio` (no siempre aplica: puede ser un ingreso con solo `bodega_destino`, o un traslado con las dos) — nuevo `apps.activos.services.scope_movimientos_visibles` exige que el/los lado(s) presentes (`bodega_origen`/`bodega_destino`) sean compartidos o visibles, sin tocar el lado que no aplica. Migración sin backfill: los registros existentes quedan `unidad_negocio=None` (compartidos), que es el comportamiento actual — nadie pierde visibilidad de golpe | ✅ Hecho (457 tests OK) |
| **Gestión de activos: vínculo automático Activo↔Estación por número de serie** (20-ago-2026) | El agente RMM ya reporta el número de serie del hardware (`Estacion.numero_serie`, R7-R9) pero nunca se cruzaba contra `Activo.numero_serie` — quedó marcado en el código como pendiente ("vínculo con Módulo de Activos, Fase 4"). Nuevo `Activo.estacion` (OneToOne nullable). `apps.activos.services.vincular_activos_por_numero_serie()`: por cada estación con serie no vinculada, si matchea con **exactamente un** Activo (nunca adivina ante 0 o 2+ coincidencias) los vincula; idempotente, corre diario vía Celery Beat (`vincular-activos-por-serie`) + comando manual `vincular_activos_por_serie`. Esto habilita dos detecciones automáticas de valor real para una flota de ~1.935 estaciones: `activos_dados_de_baja_pero_conectados()` (un activo marcado destruido/robado cuya estación sigue reportando heartbeat — baja mal hecha o serie duplicada) y `activos_movidos_sin_registro()` (el equipo aparece operando en una farmacia de una unidad de negocio distinta a la registrada en ITAM) | ✅ Hecho (457 tests OK) |
| **Gestión de activos: avisos de garantía vencida y stock bajo** (20-ago-2026) | Último de los tres gaps priorizados por el usuario para esta ronda. `Activo.vencimiento_garantia` existía pero nadie lo miraba proactivamente (solo columna de CSV); `StockBodega.cantidad` no tenía umbral de reorden. Confirmado con el usuario: **v1 es solo panel de visibilidad, sin correo** — mismo criterio ya usado repetidas veces (Windows Update v1, software desactualizado, red por farmacia). Campo nuevo `TipoConsumible.stock_minimo` (0 = no vigilado, mismo criterio "0/vacío = no vigilado" que `version_mas_reciente_conocida`). Nueva vista `/activos/avisos/` (`activos_avisos`): garantías vencidas/por vencer (30 días), stock bajo mínimo, y las dos anomalías red↔activo de la fila de arriba, todo escopado por tenant. Quedaron fuera de esta ronda, priorización explícita del usuario: depreciación/TCO, QR/código de barras, conteo de inventario físico | ✅ Hecho (457 tests OK) |

**M4 — Activar TimescaleDB en producción** sigue bloqueada: requiere el servidor real
para retomar desde el error exacto ya documentado (`cannot create a unique index
without the column "timestamp"`), no se puede resolver a ciegas sin una instancia
contra la que probar. Con M1, M2, M3 y M5 cerrados, **el roadmap de monitoreo
proactivo (M1-M5) queda completo en código salvo M4**.

**Con R7-R9, sus mejoras de rollout, el monitoreo de errores del POS, su
clasificación sistema/negocio, M1, M2, M3, M5 y el monitoreo de ancho de banda
(estación + farmacia) cerrados, todo lo de esta sesión (16/17/18/19-ago-2026) quedó
validado por test suite (420 tests OK) y `py_compile` del agente durante la sesión de
código — la validación contra el servidor real empezó el 18-ago-2026 (ver bloque de
abajo)**: `migrate`/`seed_permisos` corridos sin problema en el servidor real
(confirmó los 8 permisos nuevos de "Operador RMM" para M2), y se encontró y corrigió
un bug real de infraestructura (`bootstrap-emqx.sh` nunca se había corrido — ver el
bloque de validación de abajo), tras el cual **el heartbeat básico MQTT (agente →
worker → panel) quedó confirmado funcionando de punta a punta en `ML006-A`/`ML016-A`**.
Lo que sigue sin validar porque el agente instalado en esas estaciones es una build
**anterior** a esta sesión (solo se desplegó el código Django/servidor, no se
reconstruyó ni reinstaló el `.exe` del agente): (a) R7 (inventario de software), R8
(`bucle_metricas`), R9 (`Win32_PowerPlan`), el monitoreo de errores del POS
(`bucle_log_pos`) y el consumo de red por estación (mismo `bucle_metricas`) —
ninguno corre todavía en una estación real, hace falta `agente-prueba/build.ps1` +
`instalar-servicio.ps1` de nuevo con el código actual. (b) M3 (webhook de Teams) —
pendiente que el usuario tenga una URL de webhook real para probar de punta a punta.
(c) M5 — la vista carga, pero sin R8 desplegado en el agente los gráficos de
CPU/RAM/disco/red de la flota siguen sin datos reales. (d) `PREFIJOS_ERROR_DE_NEGOCIO`
hoy solo tiene `"VENTA SIN LOTE"` — falta revisar un log real completo de varias
farmacias para ver si hay otros mensajes de negocio rutinarios sin identificar
todavía (la lista es chica y se edita a mano a propósito, pero eso implica que
empieza incompleta). El regex de `bucle_log_pos` sigue sin probarse contra el archivo
`Logs\GeneraXML.txt` completo de una estación real (solo contra el fragmento
compartido en esta sesión) — mismo caveat que ya tenía Windows Update. (e) El
sondeo SNMP a Mikrotik **ya se validó contra hardware real** (`MC001`, `ML006` —
ver filas de arriba): la resolución automática de community y de interfaz WAN
funcionan, y `MC001` reporta una tasa real en `/monitoreo/red-farmacias/`. Sigue
pendiente habilitar SNMP/firewall en el resto de los ~600 sitios (`ML006` está
bloqueada por firewall de ese lado, en trámite) y cargar `Farmacia.ip_router` en
cada uno a medida que se habiliten.

**Validación contra el servidor real (13/14-ago-2026, 10.111.6.20:8083, cuenta admin
`romo`) — tres bugs reales encontrados y corregidos, ninguno visible contra el código
fuente solo:**
1. **TLS**: MeshCentral autogenera su propio certificado autofirmado por instancia (no
   hay un CA fijo versionado como `deploy/certs/cert.pem` de EMQX) — la conexión fallaba
   siempre con `CERTIFICATE_VERIFY_FAILED`, incluso siendo el servidor legítimo. Se
   agregaron `MESHCENTRAL_API_CA_CERT` (pinnear el cert real, preferido) y
   `MESHCENTRAL_API_VERIFICAR_TLS` (default `True`; `False` como salida rápida mientras
   no se extraiga el cert real — así quedó en producción por ahora, **pendiente
   pinnear el cert real y volver a `True`**).
2. **Carrera en el login**: un `{"action":"nodes"}` mandado inmediatamente después del
   `userAuth` se pierde de forma consistente — el servidor todavía está armando la
   sesión del usuario (manda `serverinfo`/`userinfo`/`traceinfo` primero) y recién ahí
   queda listo para procesar comandos. `_solicitar_nodes` reintenta una vez si no llega
   respuesta a tiempo; sin esto, `sincronizar_todo()` se colgaba siempre en el primer
   llamado tras loguear.
3. **Reconexión falsa por timeout de inactividad**: en `escuchar_eventos`, `ws.recv()`
   heredaba un timeout corto (8-15s) del resync inicial — cuando no llegaba nada nuevo
   en ese lapso (el caso normal la mayor parte del tiempo), el timeout se trataba como
   conexión perdida y forzaba reconectar (re-auth + resync completo) en loop constante
   cada 8-15s, disfrazando el diseño "push, sin polling" en un poll agresivo. Fix:
   `ws.settimeout(30)` explícito antes del bucle de escucha + `except
   WebSocketTimeoutException: continue` en vez de tratarlo como error.

Con los tres fixes, validación de punta a punta contra producción real, sin simular
nada:
- Login real con la cuenta `romo`, `{"action":"nodes"}` trajo la estación piloto real
  `ML016-B` (la misma del piloto de despliegue, ver §10-F), y `sincronizar_todo()`
  escribió el `EstadoDispositivo` correcto en la base.
- Se instaló MeshCentral en una segunda estación piloto (`ML006-A`, vía `curl.exe` —
  `Invoke-WebRequest` fallaba por un problema de TLS de PowerShell 5.1 no relacionado)
  y se vinculó desde el panel.
- **Evento `nodeconnect` espontáneo confirmado en tiempo real**: se paró y volvió a
  arrancar el servicio Windows "Mesh Agent" en `ML006-A` con el worker ya escuchando —
  llegaron dos eventos push (sin poll, sin resync forzado) que quedaron en
  `EventoMonitoreo` con los timestamps exactos de cada transición.
  `registrar_estado_dispositivo` ahora loguea cada transición real (antes no dejaba
  rastro en `docker-compose logs`, dificultó esta misma verificación).
- **La alerta `agente_caido_red_viva` disparó sola contra un caso real**: `ML006-A`
  llevaba (y sigue llevando) ~2 días sin heartbeat MQTT propio (ver hallazgo separado en
  §10-T) pero MeshCentral la ve conectada — el cruce (`evaluar_cruce_monitoreo`, Celery
  Beat) abrió la alerta automáticamente en su primera corrida tras el resync, sin
  intervención manual.

**Sigue pendiente**: pinnear el certificado real de MeshCentral (`MESHCENTRAL_API_CA_CERT`
en vez de `VERIFICAR_TLS=False`).

**Validación contra el servidor real (18-ago-2026) — bug de infraestructura real
encontrado y corregido: `bootstrap-emqx.sh` nunca se había corrido con éxito contra
este EMQX.** Tras desplegar el código de esta sesión (R7-R9, POS, M1-M3+M5) y correr
`migrate`/`seed_permisos` sin problema, el panel seguía mostrando `ML006-A`/`ML016-A`
"fuera de línea" con el último heartbeat de más de una semana atrás, pese a que el
`ping` a la estación respondía normal. El log del agente mostraba heartbeats
"enviados" con éxito intermitente pero también `Not authorized` recurrente al
reconectar; el log del contenedor `worker` mostraba **`Not authorized` en loop
constante, sin lograr conectarse ni una vez**. Al correr `sh bootstrap-emqx.sh`, los
tres usuarios MQTT (`saidsof_panel`/`saidsof_worker`/`saidsof_agente`) se crearon
**de cero** (ninguno devolvió "ya existía") — confirma que la base interna de
credenciales de EMQX (`built_in_database`) estaba vacía, probablemente desde el
primer arranque del stack en este servidor. Esto probablemente sea la causa real
detrás del "~2 días sin heartbeat MQTT propio" de `ML006-A` que ya se había notado
el 13/14-ago (ver arriba) — no una falla puntual de esa estación, sino el broker
completo sin credenciales sembradas. Tras `docker-compose restart worker`, el worker
conectó (`[MQTT] Conectado al broker`) y ambas estaciones piloto volvieron a "En
línea" con heartbeat del mismo minuto. **Lección operativa**: `bootstrap-emqx.sh`
(paso 5 de `deploy/README-produccion.md`) no es solo para ACLs de tópicos nuevos —
es la única fuente de las credenciales de autenticación en sí; si el volumen de datos
de EMQX se recrea (reinstalación, `docker volume rm`, cambio de host), hay que
volver a correrlo o el broker rechaza a todo el mundo sin ningún error visible del
lado de Django (el 500/403 nunca ocurre: el fallo queda enterrado en los logs de
`worker`/agente, que nadie mira a menos que ya se sospeche de MQTT).

**Fuera de alcance de esta etapa, explícitamente diferido:**
- **ACLs MQTT/EMQX por tenant** — el aislamiento por credencial propia ahora es *por
  estación* (ver fila "Credenciales MQTT por estación" arriba), no por unidad de
  negocio/tenant como se planteaba originalmente acá; en la práctica cada estación
  pertenece a una sola unidad de negocio, así que el resultado es equivalente, pero
  sigue siendo un rollout manual y gradual (no automático) y la credencial compartida
  `agente` sigue activa con ACL amplia hasta que se confirme que toda la flota migró.
- **API REST pública** (resto de R6) — exponer despliegues/estaciones/alertas vía DRF
  con auth por token, escopada por `unidad_negocio`. Hoy solo existe API
  (`apps.mantenimiento.api_urls`) para mantenimiento.
- **Windows Update: instalar/aplicar parches** — v1 (ver fila arriba) es solo
  escaneo/reporte; falta el paso de instalar y coordinar el reinicio, mismo cuidado que
  ya existe para despliegues de POS (ventana de mantenimiento, freno automático).
- **ESET PROTECT como tercera fuente del monitoreo cruzado** — pendiente de aprobación
  del proveedor para acceder a su API. El modelo de datos y el puerto
  `FuenteMonitoreo` (ver fila "Monitoreo cruzado MQTT × MeshCentral") ya quedaron
  listos para sumarla sin romper nada: agregar el choice `eset` a
  `EstadoDispositivo.Fuente` y un `apps/monitoreo/adapters/eset.py` que implemente
  `sincronizar_todo()` (su API es de consulta, no push, así que sí calza en ese puerto,
  a diferencia de MQTT/MeshCentral que empujan directo a `registrar_estado_dispositivo`).

## 10. Auditoría de pendientes (31-jul-2026)

Revisión de lo que falta cerrar antes de operar esto en producción real, más allá de
features nuevas. Por prioridad de riesgo:

**A. Cobertura de pruebas — ✅ Hecho (31-jul-2026)** para el lado Python; queda abierto
solo el lado del agente .NET:
- `apps/despliegues`: tests de `publicar_despliegue`, `evaluar_freno_automatico`,
  `verificar_completado` (incluye el caso de fuga entre unidades de negocio que
  comparten `Grupo`).
- `apps/mqtt_worker`: tests de los 6 handlers (`manejar_enrolamiento`, `manejar_heartbeat`,
  `manejar_estado_despliegue`, `manejar_info_equipo`, `manejar_estado_script`,
  `manejar_metricas`) y del enrutamiento por tópico de `Command._on_message`
  (`run_mqtt_worker.py`) — antes solo se probaba a mano contra un broker real.
  Además, `client.reconnect_delay_set(min_delay=1, max_delay=30)` acota el backoff
  entre reintentos (antes usaba el máximo por defecto de paho, 120s).
- `apps/cuentas` y `apps/auditoria` ya tienen suite propia (antes solo se ejercitaban
  indirectamente vía `apps/panel/tests.py`).
- **`saidsoft-agente`: sigue en cero tests automatizados** (no hay proyecto xUnit/NUnit);
  toda la validación de reconexión/rollback documentada en su README sigue siendo manual
  — no se tocó en esta fase (repo aparte).

**B. Producción nunca corrida de verdad — 🟢 mayormente cerrado, validado contra el
stack real (31-jul-2026):** Docker Desktop resultó estar disponible en la máquina de
desarrollo (el README decía lo contrario) — se levantó `docker compose up` de verdad
por primera vez en la historia del proyecto (`db`+`emqx`+`web`+`worker`+`scheduler`),
contra PostgreSQL 16 + TimescaleDB 2.17.2 y EMQX 5.8.3 reales, no simulados.

- **✅ Hecho y verificado**: nuevo servicio `scheduler` (`deploy/scheduler.sh`) corre
  `marcar_estaciones_offline`, `purgar_metricas` y `generar_ejecuciones_programadas`
  dentro del stack. **Bug real encontrado y corregido**: en el primer arranque,
  `scheduler` corrió su primer ciclo antes de que `web` terminara de migrar (no hay
  dependencia entre ambos), y al fallar marcaba el día como "hecho" igual — no
  reintentaba hasta el día siguiente. Ahora solo marca el día como hecho si las tareas
  salieron bien; si fallan, reintenta en el siguiente ciclo (60s). Confirmado
  funcionando limpio tras el fix.
- **✅ Hecho**: `deploy/backup.sh` (pg_dump + media, retención de 14 días locales) —
  no probado en esta sesión (necesita una BD con datos para tener sentido probarlo).
- **✅ Hecho y verificado end-to-end contra EMQX real**: `bootstrap-emqx.sh` siembra
  usuarios + ACLs por rol. **Dos hallazgos reales al probarlo**:
  1. EMQX **no trae `built_in_database` habilitada por defecto** — trae una fuente
     tipo `file` cuya última regla es literalmente `{allow, all}` (con un comentario
     del propio EMQX diciendo "cambiar esto en producción"). `no_match: deny` solo
     sirve una vez que `built_in_database` se declara explícitamente como fuente
     (`EMQX_AUTHORIZATION__SOURCES__1__TYPE` en `docker-compose.yml`) — sin eso, la
     regla `{allow, all}` de la fuente por defecto seguía ganando.
  2. El puerto por defecto del script (18083) no coincidía con el mapeo real de
     `docker-compose.yml` (8082) — corregido.
  Con ambos fixes, se probó de punta a punta con un publisher/subscriber MQTT reales
  sobre TLS: un tópico permitido entregó el mensaje, uno sin regla nunca llegó al
  suscriptor (bloqueado por el `no_match: deny`), aunque el publisher igual recibe el
  PUBACK (así es el protocolo MQTT — el ack no implica autorización, hay que
  verificar con un suscriptor real, no con el ack). Sigue sin resolver la
  segmentación por tenant (agente sigue siendo una credencial compartida, ver §9).
- **❌ Intentado y revertido**: el hypertable de TimescaleDB sigue **sin activar**
  (sin cambios respecto al intento anterior — ver detalle completo en
  `deploy/README-produccion.md`). Confirmado además contra TimescaleDB real: el
  mensaje de rechazo de `create_hypertable` es exactamente el esperado (`cannot
  create a unique index without the column "timestamp"`), y el resto de las
  migraciones (incluida toda la de R1-R6a) corre limpio en Postgres real por primera
  vez. Decisión tomada con el usuario: dejarlo documentado, no urgente para el piloto.

**C. Empaquetado del agente — 🟢 cerrado con decisión explícita (31-jul-2026):**
- `deploy/instalar-agente.ps1` (en `saidsoft-agente`) copia el publish self-contained,
  confía el CA de EMQX, registra el servicio de Windows (`New-Service` + reinicio
  automático si el proceso muere) y lo arranca. **Decisión del usuario**: script en
  vez de un `.msi` real (WiX) — mismo criterio de simplicidad que `scheduler.sh`/
  `backup.sh`; se distribuye igual por GPO o RMM.
- **Dos huecos reales encontrados y cerrados al revisar el script contra el código del
  agente** (no estaban en el script original, que solo cubría la conexión MQTT):
  1. No configuraba `ComandoHmacSecret` — sin él, el agente arranca y hace heartbeat
     normal, pero descarta en silencio todo comando remoto (`reiniciar`,
     `ejecutar_script`, `consultar_info`) por firma HMAC inválida, sin error visible
     en el panel. Ahora es parámetro obligatorio (`-ComandoHmacSecret`, debe coincidir
     con `COMANDO_HMAC_SECRET` de `deploy/.env`).
  2. No configuraba el POS real — se quedaba con los defaults de `appsettings.json`
     apuntando a `PosSimulado` (el POS de mentira de pruebas). Confirmado con el
     usuario: el POS real es Farmamia Cia Ltda - Elipsys (`Zabyca.Pos.Desktop.exe`,
     instalado en `C:\Program Files (x86)\Farmamia Cia Ltda - Elipsys\Cliente`), el
     mismo para San Gregorio/MIA/7DIAS (lo que cambia por estación es el nodo TRX de
     la config interna del propio POS, no la ruta de instalación). Ahora son
     parámetros del script con esos valores como default.
- **Bug de seguridad encontrado de paso, ya corregido**: no existía `.dockerignore` en
  `saidsoft-core`, así que `COPY . .` en `deploy/Dockerfile` horneaba dentro de la
  imagen `.env`, `deploy/.env` y `deploy/certs/*.pem` aunque estuvieran en
  `.gitignore` (`.gitignore` no protege el build context de Docker). Esto además
  explicaba por qué `COMANDO_HMAC_SECRET` "funcionaba" en la prueba en vivo de la
  Fase B sin estar declarado en `docker-compose.yml`: el contenedor terminó leyendo
  el `.env` de desarrollo horneado por accidente, no `deploy/.env`. Se agregó
  `.dockerignore` y se declaró `COMANDO_HMAC_SECRET` explícitamente en
  `docker-compose.yml` (`web` y `worker`) y en `deploy/.env.prod.example`.
- **`deploy/empaquetar-pos.ps1` (nuevo, en `saidsoft-agente`)**: arma un `.zip` de lo
  YA instalado en una estación, para probar el pipeline completo de despliegue
  (descarga → hash → cierre POS → respaldo → aplicar → relanzar → `ok`) sin cambiar
  nada funcional del POS real — un "piloto de humo" antes de arriesgar una
  actualización de verdad. Probado de verdad contra una carpeta simulada (zip
  generado y contenido inspeccionado).
- **Nodos TRX resueltos por exclusión (dato del usuario, 31-jul-2026)**: el
  `Zabyca.Pos.Desktop.exe.Config` del POS trae el nodo por estación
  (`<add key="Bdd" value="trx002" />` / `hub_...`), distinto por grupo — el proceso
  manual previo era editar esa línea, re-comprimir el cliente y enviar un zip por
  grupo. Como el apply del agente es un overlay, `empaquetar-pos.ps1` excluye ese
  archivo por defecto: cada estación conserva su nodo y un solo paquete sirve para
  toda la cadena (con `-IncluirConfig` para el caso raro de cambio de esquema, que
  entonces se apunta por grupo). Cambios de nodo deliberados: vía la ejecución de
  scripts del panel (R3) dirigida al grupo, no re-desplegando el POS.
- **`python manage.py cambiar_nodo_pos` (nuevo)**: automatiza en una sola pasada lo
  que antes era manual — arma el script PowerShell (edita la clave `Bdd` del `.Config`
  vía XPath `//add[@key='Bdd']`, con respaldo `.bak-<timestamp>` antes de escribir),
  crea el `Script` ad-hoc y registra + envía la `EjecucionScript` al grupo, todo en un
  solo comando. Valida el valor de `--nodo` contra `^[A-Za-z0-9_-]{1,50}$` (se inserta
  directo en el script) antes de tocar la base. Deliberadamente NO reinicia el POS
  (una estación puede tener una venta en curso) — el cambio toma efecto en el próximo
  arranque. 9/9 tests de `apps/scripts` en verde, incluida la fuga entre grupos.
- **Bug de encoding real encontrado y corregido en los dos `.ps1` de `deploy/`**:
  Windows PowerShell 5.1 asume codificación ANSI si el script no trae BOM UTF-8; con
  tildes o guion largo eso rompe el parseo completo del archivo con un error que no
  apunta al carácter real (reproducido con un caso mínimo). Afectaba a
  `instalar-agente.ps1` desde antes de esta sesión, no solo a lo agregado hoy. Ambos
  scripts ahora se guardan con BOM.
- **Sigue pendiente**: no existe todavía ningún paquete de despliegue real para
  Zabyca/Elipsys (todo lo probado hasta hoy fue contra `PosSimulado`); antes de
  publicar el primer despliegue real hay que verificar en una estación piloto que el
  `.zip` subido al panel trae exactamente lo que el POS real necesita.

**D. Deuda menor:**
- Cargos migrados con `Departamento` placeholder *"Sin clasificar (migrado)"*
  (`apps/activos/migrations/0010_migrar_cargo_texto_a_fk.py`) pendientes de
  reasignación manual.

**E. MeshCentral integrado al stack de producción — 🟢 cerrado (31-jul-2026):**
- Antes corría aparte con un `docker run` suelto (fuera del ciclo de vida del resto del
  stack); ahora es un servicio más de `deploy/docker-compose.yml` (`meshcentral_data`
  propio, puerto `8083:443` dentro del rango ya abierto en firewall). `docker compose
  up`/`down` lo levanta y apaga junto con todo lo demás.
- **Bug real encontrado y corregido de paso**: `MESHCENTRAL_SERVER_URL`/
  `MESHCENTRAL_MESH_ID` tienen default en `config/settings/base.py` (a diferencia de
  `COMANDO_HMAC_SECRET`), así que si `docker-compose.yml` no las declara explícitamente
  para `web`, un `deploy/.env` que sí las define de todos modos no llegaría al
  contenedor — pero SIN fallar al arrancar, solo generando links rotos a `localhost` en
  silencio. Mismo patrón de bug que el de `COMANDO_HMAC_SECRET` en Fase B/C, esta vez
  detectado antes de que afectara algo real. Se declararon con el mismo default que
  Django (`${VAR:-mismo-default-que-base.py}`) para que un `.env` incompleto se comporte
  igual con o sin Compose de por medio.
- **Sigue igual que antes (no es nuevo)**: la primera cuenta/admin y el device group se
  siguen creando a mano desde la consola web la primera vez (MeshCentral no lo expone
  por variable de entorno); el vínculo estación↔`node_id` sigue siendo manual (ver
  "diferido a propósito" abajo); los `viewmode` de escritorio/terminal siguen sin
  verificarse contra una instancia real.
- **Stack completo levantado de nuevo con estos cambios (31-jul-2026) y se encontraron
  dos bugs reales más, ambos corregidos y verificados**:
  1. El `.dockerignore` de la Fase C (necesario para no hornear secretos en la imagen)
     excluía `deploy/certs/*.pem` completo — pero `cert.pem` (público, no la llave) lo
     necesitan `web`/`worker` DENTRO de la imagen (`MQTT_CA_CERT=/app/deploy/certs/
     cert.pem`) para verificar TLS contra EMQX. El worker crasheaba en bucle
     (`FileNotFoundError` en `tls_set`). Corregido: solo se excluye `key.pem`.
  2. `scheduler` corre bajo `config.settings.produccion` igual que `web`/`worker`, así
     que también necesita `COMANDO_HMAC_SECRET` (sin default) — se me había quedado
     afuera al agregarlo a Fase C. `scheduler` tronaba en bucle (`ImproperlyConfigured`)
     en sus tareas diarias. Corregido, agregado a su `environment` en
     `docker-compose.yml`.
  Con ambos fixes: los 6 servicios (`db`/`emqx`/`meshcentral`/`scheduler`/`web`/
  `worker`) quedaron `Up` sin reinicios, `scheduler` corrió sus 3 tareas limpio, `worker`
  conectó al broker, `web` y `meshcentral` respondieron 200. El volumen `deploy_emqx_data`
  de la sesión anterior tenía credenciales desalineadas con el `deploy/.env` actual —
  se eliminó (solo data de prueba) y se re-sembró con `bootstrap-emqx.sh`.

**F. 🔴 Bug real encontrado en el agente .NET: no reconecta al MQTT configurado
(6-ago-2026, piloto ML016-A):**
- Instalado con el paquete de un clic (`deploy/docs/prueba-agente/paquete-instalacion/`)
  contra el piloto real (`10.111.6.20:8081`). Enroló y quedó aprobado (`identidad.json`
  en la estación muestra `Aprobada: true` con token), pero **nunca volvió a conectar
  después de la primera sesión** — sin heartbeats sostenidos ni respuesta a
  `consultar_info` (por eso el modal de la estación en el panel queda con
  procesador/RAM/almacenamiento/BitLocker/número de serie vacíos indefinidamente).
- Diagnosticado descartando las causas obvias directamente en la estación: un solo
  proceso `Saidsoft.Agente.exe` corriendo (no hay un proceso viejo sin configurar de
  fondo), un solo servicio `SaidsoftAgente` (`Running`/`Automatic`), y
  `C:\Program Files\Saidsoft\Agente\appsettings.Production.json` con el `MqttHost`/
  `MqttPuerto` correctos (`10.111.6.20`/`8081`) — el archivo que escribe
  `instalar-agente.ps1` está bien.
- El Visor de eventos (Application) de la estación muestra en cambio decenas de
  reintentos consecutivos (`ConectarConReintentosAsync`, categoría
  `Saidsoft.Agente.Mqtt.ClienteMqttSaidsoft`) fallando contra
  `'Unspecified/localhost:1883'` — el host/puerto configurados no llegan a esa ruta de
  reconexión, que cae al default de MQTTnet. Conclusión: la conexión inicial sí lee
  `appsettings.Production.json` (por eso pudo enrolar), pero el código de
  reconexión arma el `MqttClientOptions` sin pasarle el host/puerto configurados —
  bug de código en `saidsoft-agente` (repo aparte, no vive en esta máquina), no un
  problema de configuración ni del panel/worker de `saidsoft-core`.
- **Sin esto arreglado, ninguna estación real sobrevive más allá de su primera
  sesión de conexión** — bloqueante para el rollout de las ~600 farmacias, no solo
  para completar la info de hardware de ML016-A. Corregir `ConectarConReintentosAsync`
  (o el método equivalente) para que reutilice el `MqttHost`/`MqttPuerto` configurados
  en cada reintento, no solo en la conexión inicial.

**G. Bug real encontrado y corregido: script de instalación de MeshCentral no
soportaba el TLS del piloto (6-ago-2026, ML016-A):**
- `generar_comando_instalacion_meshcentral` (`apps/catalogo/services.py`) arma un
  one-liner de PowerShell que corre `Invoke-WebRequest` contra la consola de
  MeshCentral (certificado autofirmado en el piloto). Sin más, Windows PowerShell 5.1
  lo rechazaba en dos pasos sucesivos, cada uno encontrado al probar contra la
  estación real:
  1. "No se puede establecer una relación de confianza para el canal seguro
     SSL/TLS" — el certificado autofirmado no está en el almacén de confianza de la
     estación. Corregido con
     `[System.Net.ServicePointManager]::ServerCertificateValidationCallback = {$true}`
     antes de la descarga (no depende de `-SkipCertificateCheck`, que recién existe en
     PowerShell 7+, para no romper en Windows 10 con solo PowerShell 5.1).
  2. Ya sin el error de certificado, "error inesperado de envío" al conectar — Windows
     PowerShell 5.1 no negocia TLS 1.2 por default en muchas instalaciones de .NET
     Framework, y MeshCentral (Node.js moderno) no ofrece nada más viejo. Corregido con
     `[System.Net.ServicePointManager]::SecurityProtocol = [...]::Tls12` antes de la
     descarga.
- **Sin verificar todavía**: si esto alcanza en una estación Windows 10 realmente vieja
  y sin parchar (no solo sin TLS 1.2 forzado, sino potencialmente sin los cipher suites
  modernos que un Node.js reciente prefiere, o con un .NET Framework tan viejo que
  `SecurityProtocolType.Tls12` exista pero SChannel no lo tenga habilitado a nivel de
  SO). El piso documentado (build 1607) trae TLS 1.2 a nivel de SO, pero eso no
  garantiza que el handshake completo funcione sin parches acumulativos razonablemente
  recientes. Validar contra la estación más vieja disponible antes de asumir que este
  fix cierra el tema para todo el parque de ~600 farmacias (ver fila correspondiente en
  §8).

**H. Primer despliegue real del piloto: cuatro bugs apilados, todos de la misma
familia "falla en silencio" (6-ago-2026, ML016-A) — 🟢 corregidos:**

Subir un `.zip` de 32 MB desde una farmacia y publicarlo a una estación destapó
cuatro defectos independientes, cada uno tapando al siguiente. Ninguno mostraba un
error accionable: la página quedaba "cargando", o el formulario volvía idéntico, o
el agente reportaba un mensaje genérico.

1. **`gunicorn --timeout 120`** mataba al worker a mitad de la subida (la VPN de la
   farmacia da ~50-126ms de RTT y MSS 1394; 32 MB tardan varios minutos). Subido a
   600s en `docker-compose.yml`. Se agregó `--access-logfile -`: el diagnóstico se
   hizo con `tcpdump` porque los requests no se veían en `docker-compose logs web`.
2. **El formulario de despliegue nunca renderizaba `unidad_negocio`** (obligatorio
   desde R1) y solo mostraba los errores de `version`/`archivo`. Un usuario con
   acceso a varias unidades no podía crear despliegues: el POST volvía con "este
   campo es obligatorio" sobre un campo inexistente en pantalla, y ese error tampoco
   se imprimía. Corregido en `templates/panel/despliegue_form.html` (campo + errores
   de todos los campos + `non_field_errors`).
3. **`/app/media` era de root dentro del contenedor** (el volumen `media_data` toma
   el dueño del punto de montaje de la imagen la primera vez que se monta vacío, y
   el directorio no existía en la imagen) → `PermissionError` al guardar el `.zip`.
   Corregido en `deploy/Dockerfile`; en un despliegue ya existente hay que correr
   una vez `docker exec -u root deploy_web_1 chown appuser:appuser /app/media`.
4. **Nadie servía `/media/` en producción**: `static()` devuelve `[]` con
   `DEBUG=False`, así que todas las descargas de agentes daban 404 — el agente lo
   reportaba como "No se pudo descargar/verificar el paquete de ninguna fuente", sin
   distinguirlo de un problema de red o de hash. Corregido en `config/urls.py` con
   una ruta explícita a `django.views.static.serve`, más `rstrip('/')` en
   `ARCHIVOS_BASE_URL` (`apps/despliegues/services.py`, `apps/software/services.py`):
   una barra final producía `//media/...`, que no matchea el patrón y daba el mismo
   404 opaco. Hay tests de regresión para los dos casos en `apps/despliegues/tests.py`.

**Pendiente relacionado**: servir media con Django ocupa un worker de gunicorn
mientras dura cada descarga. Alcanza para el piloto; a escala real (~1.800
estaciones, aun con la distribución en cascada por caché de farmacia) esto necesita
nginx sirviendo el volumen directo.

**Quinto bug de la misma sesión, en el flujo de recuperación**: con el freno
automático activado por el 404 del bug 4, "Reanudar" en el detalle del despliegue
solo cambiaba el estado a `publicando` **sin volver a publicar nada por MQTT** — el
operador reanudaba un despliegue que nunca reintentaba de verdad; solo avanzaba si
el agente reconectaba por su cuenta y recogía el mensaje retenido original.
Corregido con `reintentar_despliegue()` (`apps/despliegues/services.py`), que
publica al tópico individual de cada estación pendiente en vez del tópico agregado
de grupo/farmacia/cadena — así no le reenvía el paquete a las estaciones que ya lo
aplicaron con éxito (les cerraría y reinstalaría el POS sin necesidad). Tests en
`apps/despliegues/tests.py::ReintentarDespliegueTests` y
`DespliegueReanudarVistaTests`.

**I. Auditoría de bugs "ocultos" del panel a partir de los anteriores (6-ago-2026)
— 🟢 corregidos:** los defectos de arriba tenían patrones repetibles, así que se
auditó el panel entero buscando más casos de cada uno:

- **`hx-post` sin token CSRF** (Django responde 403 y htmx no actualiza nada, así
  que el clic "no hace nada"): afectaba a aprobar/rechazar estación, aprobar en lote
  y "Actualizar ahora" del modal de estación. Eran los únicos `hx-post` fuera de un
  `<form>` con `{% csrf_token %}`; todos corregidos con `hx-headers`.
- **Polling que pisa estado de la UI**: los partials con `hx-trigger="every Ns"`
  reemplazan el nodo completo, borrando lo que el operador estaba haciendo. Casos
  encontrados y corregidos: el `<details>` de "Ver salida" en ejecuciones de scripts
  (`hx-preserve`), las casillas de aprobación en lote de estaciones (el refresco de
  15s ahora se pausa mientras haya alguna marcada — si no, la selección se borraba y
  "Aprobar seleccionadas" enviaba una lista vacía), el input del Node ID de
  MeshCentral (`hx-preserve`; el refresco de 2s lo vaciaba mientras se pegaba el ID,
  probable causa de que la vinculación nunca prosperara) y la clave de recuperación
  de BitLocker (el refresco de 2s la borraba de pantalla y obligaba a pedirla de
  nuevo, generando un evento de auditoría por intento — ahora el polling se apaga
  mientras la clave está visible).
- **Acciones que no dicen nada cuando no hacen nada**: `estaciones_aprobar_lote` con
  cero seleccionadas devolvía la misma tabla sin aviso (indistinguible de un fallo
  silencioso); ahora avisa, y también cuando aprueba menos de las seleccionadas.
- **Errores de campo invisibles** (mismo patrón que el bug 2 de §10-H): el formulario
  de firma de mantenimiento solo imprimía `non_field_errors`, así que "Falta capturar
  la firma" (un error de campo) no se veía nunca. Corregido. El resto de los
  formularios usa `panel/accion_form.html`, que itera `{% for field in form %}` y
  siempre muestra todos los errores — no tenían el problema.
- **Latente, corregido de paso**: `DespliegueForm.__init__` usaba
  `Farmacia.objects.none()` como fallback del queryset de `unidad_negocio` (modelo
  equivocado); no explotaba solo porque está vacío y las vistas siempre pasan `user`.

**J. Segundo despliegue real del piloto: verificación de hash siempre fallaba por
mayúsculas/minúsculas (10-ago-2026, ML016-A) — 🟡 diagnosticado, fix pendiente en
`saidsoft-agente` (repo aparte, no vive acá):**

Con el fix del bug H.4 ya desplegado en producción (`/media/` sirviendo bien), volver a
publicar a ML016-A seguía dando el mismo mensaje genérico "No se pudo
descargar/verificar el paquete de ninguna fuente" — sin ningún request nuevo en
`docker-compose logs web`, lo que hacía sospechar de red otra vez. Se descartó paso a
paso: `Test-NetConnection` al puerto del panel OK, `Invoke-WebRequest` a la URL exacta
del `.zip` OK (mismo tamaño en bytes que el original: 32714043). El problema apareció
al comparar el SHA-256: el servidor guarda/envía el hash en minúsculas
(`hashlib.hexdigest()` de Python siempre es lowercase), y el agente casi con certeza lo
calcula con `Convert.ToHexString()` de .NET, que devuelve **mayúsculas** por defecto.
Los dos hashes eran el mismo valor, solo con distinto casing — la descarga nunca fue el
problema, la comparación de string sí (`==` en vez de
`StringComparison.OrdinalIgnoreCase`). Mismo patrón que H.4: dos causas raíz
completamente distintas (404 de red vs. mismatch de casing) cayendo en el mismo mensaje
opaco del lado del agente, sin loguear nada localmente (ni archivo ni Visor de
eventos) que distinga una de otra.

**No corregible desde este repo**: la comparación vive en `saidsoft-agente` (C#), que
no está clonado en este entorno. Pendiente: normalizar el hash calculado a minúsculas
(o usar `StringComparison.OrdinalIgnoreCase`) en el método que verifica el paquete tras
la descarga, recompilar (`dotnet publish`) y reinstalar en las estaciones con
`instalar-agente.ps1` / el paquete de `deploy/docs/prueba-agente/paquete-instalacion/`.

**Superado por K**: no se logró ubicar la máquina de build del agente C# (ver K). El
piloto sigue con este bug hasta que ML016-A se migre al reemplazo en Python, que ya
nace con la comparación insensible a mayúsculas/minúsculas corregida.

**K. Reemplazo del agente C# perdido por una extensión en Python del "agente de
prueba" (10-ago-2026) — 🟢 hecho y validado de punta a punta en ML016-A contra el POS
real (descarga → hash → backup → aplicar → relanzar → `Confirmado OK`):**

Al intentar corregir el bug J, no se pudo ubicar ninguna máquina con el código fuente
de `saidsoft-agente` (C#) — ni en este entorno, ni en la ruta de compilación que
delataba un stack trace de un log previo (`C:\Proyectos\saidsoft-agente\...`), ni en
ninguna PC que el usuario reconociera. Con el repo fuente efectivamente perdido, la
opción de "corregir una línea y recompilar" no estaba disponible.

En vez de reconstruir el agente C# desde cero, se decidió promover
`agente-prueba/agente_prueba.py` (documentado hasta entonces como herramienta de
prueba, no de producción — ver `agente-prueba/README.md`) a agente de producción del
piloto, completándolo con lo único que le faltaba para tener paridad funcional:

1. **Despliegues de POS**: descarga, verificación de SHA-256 (insensible a
   mayúsculas/minúsculas desde el día uno — corrige el bug J de origen), aplicación
   según `modo_aplicacion` (inmediato / ventana programada / al cierre del POS),
   respaldo completo de la carpeta del POS antes de sobrescribir, relanzamiento y
   rollback automático si el POS no vuelve a quedar corriendo (chequeo de liveness por
   `tasklist`). Reporta la misma línea de tiempo de eventos que ya esperaba el
   servidor (`EventoDespliegue.Paso`) — no hizo falta tocar `saidsoft-core` para esto.
2. **Descarga con caché de farmacia**: si el despliegue trae `usar_cache=true`, intenta
   primero `cache_url_base` (recibido en el enrolamiento) antes de caer al central —
   mismo contrato que ya usaba el catálogo de software. Sigue sin implementarse que
   una estación *actúe* de caché para otras (`es_cache_farmacia`); eso queda fuera de
   alcance, como antes.
3. **Servicio de Windows**: `agente-prueba/servicio_windows.py` (pywin32) +
   `agente-prueba/instalar-servicio.ps1`, con la misma política de reinicio automático
   que tenía el agente C# (`sc.exe failure ... restart/30000/restart/60000/
   restart/120000`) y el mismo nombre de servicio (`SaidsoftAgente`) — lo reemplaza en
   el lugar. Antes el "agente de prueba" solo corría como consola manual.
   `agente_prueba.spec` pasó de ser un archivo autogenerado descartable a versionado a
   propósito (define los dos ejecutables — consola y servicio — más los
   `hiddenimports` de pywin32 que necesita el segundo; ver comentario en el archivo y
   `.gitignore`).
4. **Bug encontrado al probar el servicio compilado** (no relacionado al bug J):
   `correr()` usaba `client.connect()` (bloqueante) antes de `loop_forever()` — si el
   primer intento de conexión fallaba (broker caído o red no lista al bootear como
   servicio), la excepción moría en el hilo del agente en silencio y el servicio de
   Windows quedaba "Running" sin hacer nada, sin reintentar nunca. Corregido con
   `connect_async()` + `loop_forever(retry_first_connection=True)`, que sí reintenta
   desde el primer intento igual que reintenta reconexiones posteriores. Se detectó
   corriendo `Saidsoft.Agente.exe debug` (modo primer plano de pywin32, sin instalar
   nada) contra un broker inexistente — antes del fix el hilo moría con traceback, con
   el fix sigue reintentando sin cortar el proceso.
5. **Bug encontrado en la primera instalación real (ML016-A)**: el servicio se
   registraba bien (`sc.exe qc` mostraba el `BINARY_PATH_NAME` correctamente citado —
   no era el típico problema de espacio sin comillas en "Program Files") pero fallaba
   al arrancar: *"El servicio no respondió a tiempo a la solicitud de inicio o de
   control"*. Reproducido corriendo `Saidsoft.Agente.exe debug` directo en la estación:
   `PermissionError` al intentar abrir `agente_prueba.log` dentro de
   `C:\Program Files\Saidsoft\Agente\` — esa carpeta no está pensada para escritura en
   tiempo de ejecución, ni corriendo como Administrador. Corregido: `identidad.json` y
   el log ahora se escriben en `C:\ProgramData\Saidsoft\` (mismo directorio que ya
   usaba el agente C# original para su identidad), separado de la carpeta de
   instalación (que queda con `.exe`/`cert.pem`/`config.json`, estáticos).
6. **Segundo bug encontrado reinstalando en ML016-A con el fix del bug 5**: mismo
   síntoma exacto (*"El servicio no respondió a tiempo..."*), causa distinta.
   `sc.exe qc`/Visor de eventos no daban más detalle que el error genérico 1053; se
   volvió a reproducir con `Saidsoft.Agente.exe debug` en la estación — esta vez
   `json.load` fallaba con *"Unexpected UTF-8 BOM (decode using utf-8-sig)"* al leer
   `config.json`. Causa: `instalar-servicio.ps1` lo escribe con
   `Set-Content -Encoding utf8`, que en Windows PowerShell 5.1 (no Core/pwsh) siempre
   antepone un BOM — el lector en `servicio_windows.py` abría el archivo con
   `encoding='utf-8'` a secas, que no lo tolera. No se reprodujo en esta máquina de
   desarrollo en el primer intento porque acá se usa PowerShell 7 (pwsh), cuyo
   `-Encoding utf8` NO agrega BOM — la discrepancia de versión de PowerShell entre la
   máquina de build/pruebas y la estación real tapó el bug hasta instalarlo de verdad.
   Corregido leyendo con `encoding='utf-8-sig'` (tolera archivos con o sin BOM), y
   validado localmente forzando un BOM real (`EF BB BF`) a mano para replicar
   exactamente el archivo que produce la estación.
7. **Tercer bug — la causa raíz real del 1053, encontrada reproduciendo el servicio
   completo en la máquina de dev**: aun con los fixes 5 y 6, el servicio seguía sin
   arrancar (mismo 1053), con una firma muy específica: el proceso **nunca aparecía**
   en la lista de procesos, ni un instante, sin excepción, sin log, sin evento de
   crash — pero `debug`/`install`/`stop` desde consola funcionaban perfecto. Se
   descartaron por prueba directa: permisos de cuenta (falló igual con `LocalSystem`
   y con una cuenta de servicio dedicada), la carpeta Temp del perfil de SYSTEM
   (existía/creada, sin cambio), antivirus (Defender sin detecciones) y el subsistema
   de consola del exe (`console=False`, sin cambio). La causa real: el `__main__` de
   `servicio_windows.py` llamaba solo a `win32serviceutil.HandleCommandLine(...)`.
   Cuando el SCM lanza el binario de un servicio lo hace **sin argumentos**, y
   `HandleCommandLine` con argv vacío imprime el texto de uso y **sale
   inmediatamente** — el proceso moría en milisegundos (por eso nunca se veía) y el
   SCM esperaba 30s una conexión que nunca iba a llegar. En el setup normal de
   pywin32 el binario registrado es `PythonService.exe`, que sí se conecta al
   dispatcher; empaquetado con PyInstaller, nuestro exe ES el binario del servicio y
   debe hacerlo él mismo. Corregido: con `len(sys.argv) == 1` va a
   `servicemanager.Initialize()` + `PrepareToHostSingle` +
   `StartServiceCtrlDispatcher()`; con argumentos, sigue en `HandleCommandLine`.
   Gotcha adicional del camino: una cuenta de servicio creada por línea de comandos
   no recibe el derecho "Iniciar sesión como servicio" (error 1069 en el Visor de
   eventos, mucho más claro que el 1053) — `services.msc` lo otorga solo, `sc.exe
   config`/pywin32 no. No hizo falta: `LocalSystem` funciona bien (la teoría de una
   política corporativa bloqueando SYSTEM quedó descartada), y es lo que usa
   `instalar-servicio.ps1` por defecto.

**Validado en este entorno (máquina de dev, con permiso del usuario, servicio real
instalado y luego desinstalado)**: `instalar-servicio.ps1` + `Start-Service` dejan el
servicio **Running** bajo `LocalSystem`, con el proceso vivo, el log escribiéndose en
`C:\ProgramData\Saidsoft\` y el loop MQTT reintentando (no hay broker local, como se
espera). También validado: los dos ejecutables compilan, `debug` corre en primer
plano, y el `config.json` con BOM real de Windows PowerShell 5.1 se lee bien.
**Validado en ML016-A**: con el fix del bug 7, `instalar-servicio.ps1` deja el servicio
**Running** de verdad ahí también, reemplazando al agente C# previo. Reintentando el
despliegue #3 (ahora versión de prueba "7.7.7.7") desde el panel, el ciclo llegó hasta
`Descargado` → `Hash verificado` → `POS cerrado` → `Archivos aplicados` — la lógica de
descarga/hash/backup/aplicar funciona de punta a punta contra un despliegue real. Se
frenó en relanzar el POS con `[WinError 2] El sistema no puede encontrar el archivo
especificado`: **no es un bug, ML016-A todavía no tiene el POS real instalado**
(`instalar-servicio.ps1` ya lo había advertido). Depurando ese frenazo se encontró un
bug real:

8. **El rollback no reportaba `rollback` si fallaba al relanzar el POS**: `_rollback()`
   respalda, restaura los archivos y vuelve a llamar `_iniciar_pos()` para relanzar —
   pero si ESE segundo intento también fallaba (mismo motivo que el original: el POS
   no existe ahí), la excepción se escapaba de `_rollback()` sin llegar nunca a la
   línea que reporta el paso `rollback`, subía hasta `_procesar_despliegue()` y
   terminaba como un `error` genérico — indistinguible de "no se aplicó nada".
   Encontrado exactamente así en ML016-A. Corregido envolviendo el segundo
   `_iniciar_pos()` en su propio `try/except`: si falla, se loguea aparte pero el
   rollback de archivos igual se reporta con el motivo original.

**Otro hallazgo, de configuración, no de código**: `ARCHIVOS_BASE_URL` en
`deploy/.env` del servidor estaba `http://10.111.6.20`, **sin el puerto `:8080`** —
por eso el agente recibía 404 al construir la URL de descarga (el navegador/`curl`
manual siempre se probó con `:8080` a mano, por eso nunca se notó). Corregido en el
`.env` del servidor; no requirió cambios de código.

**No validado todavía**: el ciclo completo llegando a `Confirmado OK` contra un POS
real o un ejecutable de prueba en `pos_comando_iniciar` (recomendado antes de confiar
el rollback automático en una farmacia real con el POS de verdad instalado), y el fix
del bug 8 reinstalado en ML016-A (se corrigió después de este intento).

Con el POS real instalado en ML016-A (una carpeta con `Zabyca.Pos.Desktop.exe`
copiada a mano, sin instalador) y el fix del bug 8, el despliegue reportó
`Confirmado OK` — pero al revisar la estación, la carpeta del POS había quedado
intacta: apareció una **subcarpeta nueva** (`Cliente\Cliente\...`) al lado, sin pisar
los archivos reales.

9. **`_extraer_paquete` no contaba con que el `.zip` trajera su propia carpeta raíz
   envolvente**: `zf.extractall(destino)` extrae tal cual viene el archivo — si el
   `.zip` tiene todo debajo de una carpeta (ej. `Cliente/Zabyca.Pos.Desktop.exe` en
   vez del ejecutable suelto en la raíz del zip), el resultado es una subcarpeta
   nueva dentro de `pos_carpeta_instalacion`, no un reemplazo de los archivos reales
   — y como no tira ninguna excepción, el despliegue "tiene éxito" sin haber
   actualizado nada. Corregido: si todo el contenido del zip cuelga de una única
   carpeta raíz común, se extrae salteando ese primer nivel para que caiga directo en
   `pos_carpeta_instalacion`; si el zip ya viene plano (sin envoltorio), se comporta
   igual que antes. Probado localmente con ambos casos (zip anidado y zip plano)
   antes de recompilar.

**Validado**: reinstalado en ML016-A con el fix del bug 9, subcarpeta `Cliente\
Cliente\` limpiada a mano, despliegue reintentado — llegó a `Confirmado OK` con los
archivos del POS real actualizados de verdad (sin subcarpeta anidada). El circuito
completo de despliegue de POS (servidor → agente Python como servicio de Windows →
POS real) queda cerrado y probado de punta a punta por primera vez en el piloto.

**Pendiente, no bloqueante**: revertir el `ServicesPipeTimeout=120000` que se probó
en ML016-A durante el diagnóstico del bug 7 (no era la causa; `Remove-ItemProperty
-Path "HKLM:\SYSTEM\CurrentControlSet\Control" -Name ServicesPipeTimeout` + reinicio,
o dejarlo si no molesta — solo alarga el timeout de arranque de todos los servicios).

**L. El catálogo de software nunca pudo publicar nada por MQTT — ACLs de EMQX
incompletas (10-ago-2026) — 🟢 corregido y validado de punta a punta (actualización
real de Firefox en ML016-A, descarga → hash → instalación silenciosa → confirmado):**

Probando el catálogo de software por primera vez contra una estación real (actualizar
Firefox en ML016-A), la `SolicitudInstalacion` se publicaba sin error visible en el
panel pero el agente nunca la recibía — se quedaba en `Pendiente` para siempre, sin
ningún evento posterior. El agente estaba conectado y mandando heartbeats con
normalidad (descartaba problema de red/agente); los despliegues de POS sí funcionaban
por el mismo broker.

Causa: `deploy/bootstrap-emqx.sh` (que siembra las ACLs por tópico en EMQX) nunca se
actualizó cuando se agregó el catálogo de software — el usuario `panel` tenía permiso
de `publish` para los tópicos de `/saidsof/despliegue/...` y `/saidsof/agente/+/
comando/`, pero no para `/saidsof/software/...` ni `/saidsof/agente/+/software/`; el
`worker` tampoco tenía permiso de `subscribe` para `/saidsof/agente/+/
software_estado/`. Con `EMQX_AUTHORIZATION__NO_MATCH=deny` (ver comentario en el
propio script), cualquier tópico sin regla explícita se descarta en silencio — ni el
publisher ni el broker devuelven un error visible, por eso el mensaje simplemente
desaparecía sin dejar rastro en ningún lado. Mismo patrón que el gotcha de EMQX ya
documentado (fuente `file` con `{allow,all}`), pero en la dirección opuesta: acá la
regla que faltaba era la que debía *permitir*.

Corregido agregando las reglas faltantes a `bootstrap-emqx.sh` (publish de software
para `panel`, subscribe de `software_estado` para `worker`).

**Segundo bug, encontrado al aplicar el fix anterior contra el EMQX real**: el
endpoint que usaba el script para sembrar ACLs
(`POST .../authorization/sources/built_in_database/rules/users`) **no es idempotente**
— es un import de una sola vez; si el usuario ya tenía reglas cargadas (como acá,
porque el script ya se había corrido antes), devuelve `409 ALREADY_EXISTS` y no
actualiza nada. El comentario original en el script decía lo contrario (que era
seguro re-correrlo); era una suposición nunca puesta a prueba, hasta que hubo un
motivo real para correrlo dos veces. Corregido reemplazando el `POST` masivo por un
`PUT` a `.../rules/users/{username}` (uno por usuario) — ese endpoint sí es un "set"
real: crea si no existe, reemplaza si ya había reglas.

**Tercer bug, mismo diagnóstico en caliente**: el `PUT` por sí solo no alcanzaba —
EMQX devolvía `400 BAD_REQUEST` (`required_field: root.username`) porque el body
necesita `"username"` explícito además de `"rules"`, no alcanza con que vaya en la
URL. Corregido agregándolo al body. Verificado armando el JSON con un `curl` de
mentira (sin pegarle a la red real) y confirmando que cada payload parsea bien y
lleva ambos campos.

**Validado**: `bootstrap-emqx.sh` corrido en el servidor real con los tres fixes
aplicados (ACLs de software + endpoint idempotente + `username` en el body) — las
tres credenciales (`saidsof_agente`, `saidsof_worker`, `saidsof_panel`) quedaron
definidas sin error. Una solicitud de instalación de Firefox nueva, bien dirigida a
ML016-A, llegó de punta a punta: recibido → descargado → hash_verificado →
instalando → instalado, y la versión nueva quedó confirmada en la estación (tras
cerrar y reabrir Firefox, que estaba corriendo durante la instalación silenciosa).

**Pendiente de verificar en general**: si hay más tópicos que quedaron sin ACL desde
que se agregaron software/scripts (esta sesión encontró el gap en software porque se
probó por primera vez hoy; scripts sí tenía sus reglas completas desde antes — no se
auditaron sistemáticamente todos los tópicos del protocolo contra
las ACLs sembradas, esto fue reactivo a un síntoma puntual).

**M. Comandos `consultar_info`/`reiniciar` implementados en el agente, y paquete de
instalación "un clic" (11-ago-2026) — 🟢 hecho:**

Instalando una segunda estación real (ML006-A, primer despliegue en Windows 10 22H2 —
sin problemas de compatibilidad, es una build reciente y parchada), se notó que el
botón "Actualizar ahora" del panel no hacía nada: el agente Python solo tenía
implementado `ejecutar_script`, el resto de los comandos caían al log "no
implementado" (`consultar_info` y `reiniciar`, heredado del alcance original de
"agente de prueba"). Ambos quedaron implementados con el mismo esquema de firma HMAC
que ya usaba `ejecutar_script`:

- **`consultar_info`**: un solo script de PowerShell arma hostname/procesador/RAM/
  almacenamiento (vía CIM) + BitLocker del volumen `C:` como JSON, que el agente
  parsea y publica a `/saidsof/agente/{codigo}/info_equipo/`. Si BitLocker no está
  disponible, esos campos quedan vacíos sin romper el resto. Verificado corriendo el
  script real en esta máquina (devolvió hardware real) y comparando la firma HMAC que
  arma el agente contra `apps.catalogo.services.firmar_payload` del servidor (mismo
  `COMANDO_HMAC_SECRET`) — coinciden.
- **`reiniciar`**: reinicia el equipo Windows completo (no el servicio del agente) con
  `shutdown /r /t 10`, fire-and-forget, sin canal de confirmación de vuelta — coherente
  con que el botón del panel ya avisa "esto interrumpe cualquier venta en curso". No se
  ejecutó el shutdown real en ninguna máquina de prueba (por motivos obvios).

**Paquete "un clic"** (`agente-prueba/Instalar.bat` + `config.ejemplo.txt`), mismo
patrón que ya existía para el agente C# (`deploy/docs/prueba-agente/paquete-
instalacion/`), para no tener que escribir el comando de `instalar-servicio.ps1` a
mano en cada estación nueva. Encontrados y corregidos dos bugs reales probándolo:

1. **`setlocal enabledelayedexpansion` (copiado del patrón viejo) hacía perder
   cualquier `!` dentro de `MqttPassword`/`ComandoHmacSecret` al leer `config.txt`**
   — silencioso, sin error. El script no usa `!variable!` en ningún lado, así que la
   delayed expansion no hacía falta; sacarla lo resuelve. Las contraseñas que genera
   este proyecto sí pueden traer `!` (ver la del `svc_saidsoft` de más arriba en este
   mismo documento), así que era un bug real, no teórico.
2. **Una tilde o raya larga en un comentario `rem` rompe el parseo de `cmd.exe`** —
   problema de codepage: el archivo se escribe en UTF-8 pero `cmd.exe` lo lee con la
   página de códigos OEM/ANSI activa, y los bytes multibyte de un caracter acentuado
   corrompen el `rem` de esa línea (y a veces de líneas vecinas), haciendo que
   fragmentos del comentario se intenten ejecutar como comandos — aunque el script
   igual termina funcionando bien después. Corregido quitando toda tilde/raya larga de
   `Instalar.bat` (queda en ASCII puro). Verificado reproduciendo el problema aislado
   (una línea con "—" o con "ñ" alcanza para romperlo) y confirmando que, sin
   caracteres no-ASCII, el archivo corre limpio de punta a punta con un
   `instalar-servicio.ps1` de mentira que solo imprime los parámetros recibidos
   (incluyendo secretos con `!`/`$`/`#`, que llegaron intactos).

**N. Exposición accidental de `MQTT_PASSWORD_AGENTE`/`COMANDO_HMAC_SECRET` en un
commit público (11-ago-2026) — 🟢 rotados:** al completar el `config.ejemplo.txt` de
la plantilla del paquete de un clic (§10-M), quedaron pegados ahí los valores reales
en vez de en `config.txt` (que sí está en `.gitignore`) — y como
`saidsoft-core` es un repo **público**, el commit los dejó visibles en el historial de
GitHub. Corregido el archivo de inmediato, pero como el historial de un repo público
no se puede considerar "borrado" con reescribirlo (GitHub cachea, puede haber clones),
se rotaron los dos secretos de verdad: valores nuevos generados, aplicados en
`deploy/.env`, en EMQX (contraseña del usuario `saidsof_agente`) y reinstalados en
las dos estaciones activas (ML016-A, ML006-A). De paso se encontró y corrigió que
`bootstrap-emqx.sh` tampoco sabía *actualizar* la contraseña de un usuario MQTT ya
existente (el `POST` de creación con `409 ALREADY_EXISTS` no hacía nada más) — ahora
hace `PUT` a `/users/{user_id}` en ese caso. **Lección**: `config.ejemplo.txt` es la
plantilla versionada — nunca debe llevar valores reales, ni siquiera "para probar
rápido"; los reales van solo en `config.txt`, gitignoreado.

**O. Comentarios `{# ... #}` multilínea se renderizaban como texto visible en el panel
(11-ago-2026) — 🟢 corregido:** el usuario reportó ver texto de comentarios de
desarrollo (explicaciones de por qué existe tal `hx-preserve` o tal pausa de polling)
como contenido visible arriba de la tabla de `/estaciones/` y en el modal de info de
estación. El código fuente parecía correcto a simple vista (`{# comentario #}`, la
sintaxis válida de Django) y el archivo desplegado en el contenedor coincidía exacto
con el repo — descartando problemas de despliegue. La causa real: **Django no permite
que un comentario `{# #}` ocupe más de una línea** (documentado, fácil de no saber);
si lo hace, el parser no lo reconoce como comentario y el texto completo —incluidos
los delimitadores— se imprime tal cual. Los seis casos del proyecto que usaban esta
sintaxis multilínea (introducidos en distintos commits a lo largo del desarrollo,
todos con la misma buena intención de documentar el "por qué" de un workaround)
quedaron convertidos a `{% comment %}...{% endcomment %}`, que sí soporta varias
líneas. Verificado con un test que confirma que el texto ya no aparece en
`/estaciones/`, más la suite completa. **A tener en cuenta a futuro**: cualquier
comentario Django de más de una línea debe ir en `{% comment %}`, nunca en `{# #}`.

**P. Formularios de destino (despliegue/solicitud de instalación/promoción de anillo)
rediseñados con checklist real + mostrar/ocultar por tipo de destino (11-ago-2026) —
🟢 cerrado:** el usuario reportó que `/solicitudes-instalacion/nueva/` (y por
extensión los otros dos formularios con el mismo patrón de destino) eran "muy
básicos": al elegir "Toda la cadena" seguían mostrándose los campos de
grupos/farmacias/estaciones (sin sentido, ya que ese destino no los usa), y elegir
farmacias o estaciones específicas usaba un `<select multiple>` nativo (mala
usabilidad con listas largas, sin buscador). Se aplicó el mismo arreglo a los tres
formularios que comparten el patrón `destino_tipo` + `grupos`/`farmacias`/
`estaciones`:
- `DespliegueForm`, `SolicitudInstalacionForm` (`apps/software/forms.py`) y
  `PromoverDespliegueForm` — los tres widgets pasaron de `SelectMultiple`/
  `forms.SelectMultiple(size=6)` a `forms.CheckboxSelectMultiple`.
- Plantillas dedicadas (`despliegue_form.html`, `solicitud_instalacion_form.html`
  nueva, `despliegue_promover_form.html` nueva) con JS que oculta/muestra el bloque
  de grupos/farmacias/estaciones según `destino_tipo` (nada se muestra para
  "cadena"), y un buscador de texto por checklist (`data-checklist-buscar` filtra las
  `<label>` del checklist por coincidencia de texto).
- De paso, `activo_crear` (`apps/panel/views/activos.py`) y
  `solicitud_instalacion_crear` dejaron de usar el `accion_form.html` genérico y
  pasaron a plantillas propias; `activo_form.html` nueva además solo muestra
  Procesador/RAM/Almacenamiento cuando `tipo` es de cómputo (`LAP`/`DSK`/`SRV`/`TAB`),
  corrigiendo que esos campos aparecieran también para Impresora/UPS/etc.
- Verificado con la suite completa de `apps.panel`/`apps.despliegues`/`apps.software`
  y, para cada formulario, una captura headless (Edge `--headless --screenshot`) del
  HTML renderizado vía el `Client` de pruebas (login real + vista real, no
  `render_to_string` suelto — este último no pasa por `AuthenticationMiddleware` y
  termina renderizando el `{% block content_anon %}` vacío de `base.html`).

**Q. El "bug de caché de Docker" que quedó pendiente de §10-O/P en realidad eran dos
binarios `docker compose` distintos peleándose por el mismo `docker-compose.yml`
(11-ago-2026) — 🟢 causa raíz encontrada y documentada:** tras el fix de §P, un rebuild
en el NUC de producción parecía no tomar el código nuevo (`docker exec deploy_web_1
grep ...` seguía dando 0 con el commit correcto ya en el checkout del host). Se
descartó despliegue incompleto (`git log`/`git status` limpios) y se armó un
diagnóstico: `docker images` mostró **dos familias de imágenes para los mismos
servicios** — `deploy_web`/`deploy_worker`/... (guion bajo, 11-ago, recién construidas)
y `deploy-web`/`deploy-worker`/... (guion, 6-ago, viejas). Causa real: el NUC tiene
instalados **tanto `docker-compose` v1 (el binario legado, sin espacio) como el plugin
`docker compose` v2 (con espacio)** — `deploy/README-produccion.md` decía que v2 "no
está instalado por default" (cierto cuando se escribió esa nota, dejó de serlo en algún
momento) y mezclaba ejemplos de ambos comandos en distintas secciones. Los contenedores
que sirven tráfico real (`deploy_web_1`, con el puerto 8080 publicado) los administra
v1; en algún momento se corrió un `docker compose build`/`up` (v2) siguiendo la sección
de instalación inicial del README, que construyó/gestionó un stack paralelo con
namespace de imagen y contenedor distinto (`deploy-web-1`, guion) — completamente
invisible para quien mira `deploy_web_1`. No era caché stale, eran dos stacks
coexistiendo. Fix: `deploy/README-produccion.md` reescrito para usar `docker-compose`
(v1, sin espacio) de forma consistente en **todos** los comandos de ejemplo, con una
sección nueva "NUNCA usar `docker compose` v2" explicando el porqué. **Pendiente de
limpieza no urgente**: borrar las imágenes `deploy-*` (guion) huérfanas del NUC con
`docker rmi` para que dejen de aparecer en `docker images` y confundir a futuro.

**R. Alta masiva de farmacias desde CSV (11-ago-2026) — 🟢 cerrado:** el usuario
necesitaba dar de alta ~29 farmacias de una vez; el pedido inicial sonaba a un rango
secuencial simple (ML001..ML029), pero el archivo real que maneja es un inventario de
red por sitio (columnas Ciudad/Id de sitio/Tipo de Enlace/Backup/NODO, códigos
irregulares por ciudad como MALU1, MB001, MBAL1) — un comando de "rango" no alcanzaba.
Se armó `python manage.py importar_farmacias <csv>` (`apps/catalogo/management/
commands/importar_farmacias.py`): detecta columnas de código/ciudad/nodo por
coincidencia parcial de encabezado, usa el nodo de red como código de `Grupo`
(autocreado si no existe) y deduce la `UnidadNegocio` de la primera letra del código
con un mapeo confirmado con el usuario (`M`→MIA, `G`→SG, dígito→7DIAS). Decisión
explícita: la unidad de negocio **no** se autocrea si falta (es el límite de
aislamiento multi-tenant, ver README "Multi-tenancy") — esa fila se reporta como
error, a diferencia del Grupo (solo un canal de versión de POS, sin riesgo). Soporta
`--dry-run` (previsualizar sin escribir) y `--actualizar` (sobreescribir
ubicación/grupo de las que ya existen; por defecto se omiten intactas — reentrante,
se puede correr el mismo CSV varias veces sin duplicar). **Ampliado el mismo día**
al ver una hoja más completa del inventario real (columnas Provincia/Segmento de
Red/Tipo de Enlace/Login/Backup además de las anteriores): se suma detección
opcional de columna Provincia, combinada con Ciudad en la ubicación
(`"Pasaje, El Oro"`); "Login" (usuario del circuito ante el proveedor de internet)
se ignora por ser dato del proveedor, no de SAIDSOFT. **Segundo agregado, mismo
día**: el usuario notó que Segmento de Red/Tipo de Enlace/Backup sí son datos de
infraestructura útiles para un proyecto de IT Ops (diagnosticar conectividad sin
volver al Excel; priorizar farmacias sin enlace de respaldo como punto único de
falla) — se agregaron como campos nuevos del modelo `Farmacia` (`segmento_red`,
`tipo_enlace`, `tiene_backup`, migración `0012_farmacia_segmento_red_...`), visibles
y filtrables en `FarmaciaAdmin`, y el importador los captura si esas columnas están
presentes (`tiene_backup` interpreta vacío/"NO"/"INACTIVO"/"0" como sin backup,
cualquier otro valor no vacío como con backup). 7 tests en
`apps/catalogo/tests.py::ImportarFarmaciasTests` cubren: creación con deducción de
unidad/grupo, re-corrida idempotente, `--actualizar`, `--dry-run`, prefijo de
código sin mapeo conocido (falla explícito, no adivina), combinación
ciudad+provincia, y captura de segmento de red/tipo de enlace/backup. **Tercer
agregado, mismo día**: el usuario buscaba un botón "Importar" en la pantalla del
admin y no había ninguno — el import solo existía como comando de SSH, inaccesible
para quien no tiene acceso al servidor. Se refactorizó la lógica del comando a
`apps.catalogo.services.importar_farmacias_desde_csv` (mismo shape de resultado,
reusable) y se agregó un botón "Importar CSV" en `/admin/catalogo/farmacia/` (junto
a "Añadir farmacia", vía `FarmaciaAdmin.get_urls()` + `change_list_template`) que
sube un CSV, previsualiza (mismo `--dry-run`) y aplica, protegido por el permiso de
alta sobre `Farmacia`. El comando de management quedó como wrapper delgado sobre el
mismo servicio (mismo patrón que `marcar_estaciones_offline.py`) — dos formas de
disparar la misma lógica, sin duplicarla. 5 tests nuevos en
`FarmaciaAdminImportarViewTests` (formulario, 403 sin permiso, dry-run no escribe,
creación real redirige al listado, error sin archivo). Verificado visualmente
(botón en el listado, formulario, y resultado de una previsualización con error de
prefijo desconocido). Documentado en README.md §"Multi-tenancy".

**S. Bug real en producción: importar_farmacias tiraba 500 en vez de reportar un
error de fila (12-ago-2026) — 🟢 corregido:** primer uso real del botón "Importar
CSV" contra el servidor de producción — `Server Error (500)` en
`/admin/catalogo/farmacia/importar/`. El traceback (`docker-compose logs --tail=80
web`, ya que `docker-compose logs web --tail=80` no funciona en v1 — orden de
opciones) mostró `django.db.utils.DataError: value too long for type character
varying(10)` reventando dentro de `grupo.save()`: un valor de columna NODO real más
largo que `Grupo.codigo` (`max_length=10`) llegaba sin validar hasta el INSERT, y
Postgres lo rechazaba con una excepción sin capturar — 500 crudo en vez de un error
de fila prolijo. Causa raíz más general: el importador nunca validaba longitudes
contra los `max_length` reales de los campos antes de escribir (ni en modo real ni
en `--dry-run`, así que la previsualización tampoco lo detectaba de antemano). Fix
en `importar_farmacias_desde_csv`: valida código/nodo/ubicación/segmento de
red/tipo de enlace contra el `max_length` de su campo antes de tocar la base,
reportando la fila como error con el detalle (valor, largo, máximo permitido) en
vez de escribir y dejar que la base tire la excepción. Nuevo test
`test_nodo_mas_largo_que_el_campo_se_reporta_como_error_sin_reventar`. Pendiente:
no se conoce todavía el valor real de NODO que disparó el error (el usuario no lo
tenía a mano) — si vuelve a aparecer en el reporte de errores del importador, decidir
ahí si conviene ampliar `Grupo.codigo` más allá de 10 caracteres en vez de solo
reportarlo.

**T. Bug real en producción, sin cerrar: agente de `ML006-A` falla intermitente con
"Bad user name or password" contra EMQX (14-ago-2026):** encontrado de paso mientras
se validaba en vivo el monitoreo cruzado MQTT × MeshCentral (ver fila "Monitoreo
cruzado" en §9) — al instalar MeshCentral en `ML006-A` para esa prueba, se notó que su
agente propio (`SaidsoftAgente`, Windows) llevaba desde el 11-ago sin heartbeat pese a
estar `Running`. Diagnosticado hasta acá:
- `C:\ProgramData\Saidsoft\agente_prueba.log` muestra un ciclo repetido cada ~2 min:
  1-2 heartbeats enviados OK, después `Falló la conexión al broker: Bad user name or
  password`, desconecta, paho reconecta solo, y el ciclo se repite — no es una caída
  total, es intermitente.
- Descartado que sean dos instancias del agente compitiendo por la credencial: los dos
  PID que aparecen (`Get-Process`) son el bootloader + hijo normal de un ejecutable
  PyInstaller `--onefile`, no un duplicado real (confirmado con
  `Get-CimInstance Win32_Process` — `ParentProcessId` de uno es el otro).
- Descartada la feature de credenciales rotativas por estación
  (`apps.mqtt_worker.emqx_admin`) como causa: `EMQX_API_*` no está configurado en
  `deploy/.env` de este servidor, así que sigue desactivada — el agente usa la
  credencial compartida estática (`saidsof_agente`) de `config.json`, la misma que
  usan el resto de las estaciones sin problema.
- **Sin diagnosticar todavía**: por qué esa credencial estática falla de forma
  intermitente (no constante) solo en esta estación. Falta revisar del lado de EMQX
  (logs del broker, `GET .../authentication/.../users/saidsof_agente`, o si hay algún
  límite de conexiones/rate-limit que esté rechazando reconexiones muy seguidas).

**U. Mismo arreglo de §10-P aplicado a los formularios de scripts (20-ago-2026) — 🟢
cerrado:** el usuario reportó que `/scripts/ejecutar-adhoc/` "no le gustaba" — al venir
precargado desde el hand-off de "Instalar agente ahora" (`?estacion=5`, ver §9 "Monitoreo
cruzado") con destino ya resuelto a una sola estación, el formulario seguía mostrando los
tres `<select multiple size=6>` de grupos/farmacias/estaciones siempre visibles, sin
ocultar los que no aplicaban — el mismo defecto que §10-P ya había corregido en
`DespliegueForm`/`SolicitudInstalacionForm`/`PromoverDespliegueForm`, pero que se había
quedado sin aplicar en `apps/scripts/forms.py` (`EjecutarScriptForm`, del que heredan
`EjecutarScriptAdhocForm` y de cuyo mismo patrón de campos es `ScriptProgramadoForm`) —
las tres vistas de esa app seguían en el `accion_form.html` genérico. Mismo arreglo, a
las tres de una vez (comparten exactamente el mismo patrón `destino_tipo` +
`grupos`/`farmacias`/`estaciones`, igual que la fila de §10-P agrupó sus tres):
widgets a `CheckboxSelectMultiple`, plantillas dedicadas nuevas
(`script_ejecutar_form.html`, `script_ejecutar_adhoc_form.html`,
`script_programado_form.html`) con el mismo JS de mostrar/ocultar por destino + buscador
de texto. Verificado con la suite completa de `apps.panel`/`apps.scripts` (122 tests OK)

**V. `meshagent.exe` corrido sin argumentos nunca instala de verdad desde una sesión no
interactiva (21-ago-2026) — 🟢 cerrado, validado contra hardware real (`MC001-C`):**
al probar la instalación en una estación nueva se vio la ejecución quedarse "en progreso"
sin resolver. Primer diagnóstico (parcial, incompleto): se asumió que `meshagent.exe`, al
convertirse en el agente persistente, nunca terminaba el proceso, así que se sacó el
`-Wait` de `Start-Process` (commit `fc00cad`) — corrigió el cuelgue de la ejecución, pero
`Get-CimInstance Win32_Service` confirmó que **en realidad nunca se instalaba nada**: dos
procesos `meshagent` sueltos corriendo desde `C:\WINDOWS\TEMP`, sin servicio "Mesh Agent"
registrado ni copia en Program Files. Causa real: corrido sin argumentos, el instalador de
MeshCentral no completa su rutina de auto-instalación (copiarse a Program Files, registrar
el servicio, arrancarlo) cuando lo lanza un proceso de Session 0 (el servicio de Windows
`SaidsoftAgente`, sin sesión interactiva) — confirmado corriendo el mismo `.exe` a mano
como Administrador (sesión interactiva normal), donde sí instaló bien. `meshagent.exe
--help` reveló el flag correcto: `-fullinstall` (copia a Program Files, instala e inicia el
servicio, y termina el proceso al completar — a diferencia de correrlo sin argumentos).
Con `-fullinstall`, `-Wait` es correcto y necesario (el proceso sí vuelve). Fix final:
`Start-Process -FilePath $ruta -ArgumentList "-fullinstall" -Wait` en
`generar_comando_instalacion_meshcentral` — confirmado en MC001-C: `Get-CimInstance
Win32_Service` mostró `Mesh Agent | Running | C:\Program Files\Mesh Agent\MeshAgent.exe`
después de correrlo. **Lección**: un proceso que queda corriendo no prueba que una
instalación silenciosa funcionó — hay que confirmar el efecto real (servicio registrado,
ubicación final), no solo que el `.exe` no crasheó

**W. Auto-vínculo Activo/MeshCentral↔panel por nombre del agente (21-ago-2026) — 🟢
cerrado:** surgió de paso al validar el fix de §10-V — el usuario pidió no tener que abrir
la consola de MeshCentral y copiar el `node_id` a mano cada vez que instala un agente
nuevo. `generar_comando_instalacion_meshcentral` ahora instala con
`--agentName=<código de la estación>` (flag de `meshagent.exe -fullinstall`, revelado por
`meshagent.exe --help` durante el diagnóstico de §10-V) — el nodo aparece en MeshCentral
con el mismo nombre que `Estacion.codigo`. `apps.monitoreo.adapters.meshcentral.
AdaptadorMeshCentral._vincular_por_nombre` usa ese match para completar
`Estacion.meshcentral_node_id` sola la primera vez que ve el nodo (snapshot inicial o
`nodeconnect` si el evento trae el nombre), sin pisar nunca un vínculo ya existente. Nueva
tarea de Celery Beat `sincronizar-meshcentral` (cada 15 min, completa
`AdaptadorMeshCentral.sincronizar_todo()`, que ya existía pero solo se llamaba al
(re)conectar el worker de larga duración) como red de seguridad para el caso en que el
evento en vivo no traiga el nombre. Ya no hace falta el paso manual salvo para estaciones
instaladas antes de este cambio. Verificado con la suite completa (462 tests OK)

**X. Mismo arreglo de §10-P/§10-U aplicado a "Nueva ventana de mantenimiento"
(21-ago-2026) — 🟢 cerrado:** el usuario reportó el mismo defecto en
`/monitoreo/mantenimiento/nueva/` (M2) — `VentanaMantenimientoForm` compartía
exactamente el mismo patrón `destino_tipo` + `grupos`/`farmacias`/`estaciones` que ya se
había corregido en despliegue/solicitud de instalación/scripts, pero se había quedado
afuera de esas dos rondas. Mismo arreglo: widgets a `CheckboxSelectMultiple`, plantilla
dedicada nueva `ventana_mantenimiento_form.html` con el mismo JS de mostrar/ocultar por
destino + buscador de texto, reemplazando el `accion_form.html` genérico. Verificado con
`apps.panel`/`apps.monitoreo` (202 tests OK)

**Y. Auditoría de gobernanza ITIL/ISO 27001 (21-ago-2026) — remediación en curso:** el
usuario pidió una revisión completa del proyecto de cara a gobernanza ITIL e ISO/IEC
27001. Se armó un informe (Artifact) con 16 hallazgos priorizados y su mapeo contra el
Anexo A de ISO 27001 y las prácticas de ITIL 4, con un orden de remediación acordado con
el usuario. Se van cerrando en ese orden, cada uno como entrada propia en esta sección:

- **OPS-1 — 🟢 cerrado y validado en producción (22-ago-2026):** `deploy/backup.sh` usaba
  `docker compose` (sintaxis v2, con espacio) para el `pg_dump`/`tar` del respaldo diario,
  pero el servidor de producción corre Compose **v1** (todo el resto de
  `README-produccion.md` usa `docker-compose` con guion) — en v1 ese subcomando no existe.
  Corregidas las dos invocaciones y las mismas dos referencias sueltas en
  `README-produccion.md` (líneas 227/237, instrucciones de MeshCentral). **Verificado
  contra el servidor real (`glpi@glpi-NUC11TNKv5`)**: resultó ser peor de lo asumido — no
  era que el cron fallara en silencio, **nunca existió ningún cron de respaldo** (crontab
  de `glpi` vacío, `/etc/cron.d/` sin entrada propia, `root` sin crontab, ningún
  `.sql.gz` en el filesystem). De paso se confirmó que `glpi` es miembro del grupo
  `docker` (no hace falta `sudo` para `docker-compose`, se venía usando por costumbre).
  Corrida manual del script ya arreglado: generó `db_20260822_103238.sql.gz` (84K) y
  `media_20260822_103238.tar.gz` (644M), ambos con integridad `gzip -t` OK — el único
  aviso fue de `pg_dump` sobre FKs circulares en las tablas internas de TimescaleDB
  (`hypertable`/`chunk`/`continuous_agg`), conocido y no bloqueante, pero a tener en
  cuenta si algún día se restaura con `--disable-triggers`. Instalado el cron en el
  crontab propio de `glpi` (sin root): `0 2 * * * cd
  /home/glpi/Documentos/Said/saidsoft-core && sh deploy/backup.sh
  /home/glpi/backups/saidsoft >> /home/glpi/backups/saidsoft-backup.log 2>&1`, con el
  daemon `cron` confirmado `active`. **Pendiente, no bloqueante**: probar una
  restauración real de `db_*.sql.gz` sobre una base vacía (ver OPS-2) y mover los
  respaldos fuera del servidor.

- **SEC-2 — 🟢 cerrado (22-ago-2026):** el panel se servía por HTTP plano
  (`http://10.111.6.20:8080`), con `SECURE_SSL_REDIRECT`/`COOKIES_SOLO_HTTPS` en `False`
  en el `.env` real del servidor — confirmado (no solo inferido) leyendo el `.env` real.
  Sin dominio propio, se agregó un servicio `nginx` (`deploy/nginx/nginx.conf`) que
  termina TLS con el mismo certificado autofirmado que ya usa EMQX
  (`deploy/certs/cert.pem`, ya tenía `IP:10.111.6.20` en el SAN — no hizo falta
  regenerarlo). `web` (gunicorn) dejó de publicar puerto al host (`expose: 8000` en vez
  de `ports:`), solo nginx lo alcanza por red interna de Docker. HTTPS en el puerto
  **8084** (dentro del rango 8080-8085 ya abierto en firewall, cero cambios de red);
  8080 sigue respondiendo pero solo redirige a HTTPS.
  **Hallazgo real encontrado al implementarlo** (no en la auditoría original): `/media/`
  (de donde los ~1.935 agentes bajan paquetes de despliegue e instaladores, sin
  autenticación por diseño — ver `config/urls.py`) se resuelve con `urllib` puro del
  lado del agente, que no confía en el certificado autofirmado del panel. Redirigir
  ciegamente todo a HTTPS habría roto la descarga de despliegues de toda la flota en
  producción. Se ajustó nginx para servir `/media/` sin cifrar y sin redirect (no hay
  credenciales en ese path) directo desde el volumen `media_data`, resolviendo de paso
  la deuda de performance que `config/urls.py` ya señalaba (cada descarga ocupaba un
  worker de gunicorn) — `ARCHIVOS_BASE_URL` no necesitó cambiar. En el `.env` real:
  `CSRF_TRUSTED_ORIGINS` a `https://10.111.6.20:8084`, `SECURE_SSL_REDIRECT`/
  `COOKIES_SOLO_HTTPS` a `True`. **Validado contra el servidor real**: `docker-compose
  config` limpio con el `.env` real, `down`+`up -d` completo de los 10 contenedores
  (9 + `nginx` nuevo) sin errores, `worker` reconectó limpio a EMQX
  (`[MQTT] Conectado al broker`) tras el reinicio. `http://10.111.6.20:8080/` → 301 a
  `https://.../`; `https://10.111.6.20:8084/login/` → 200, con `Strict-Transport-
  Security` presente y la cookie `csrftoken` con flag `Secure`; `http://10.111.6.20:
  8080/media/...` → sirve directo sin redirect, confirmando que las descargas de los
  agentes no se rompieron.

- **AC-1/AC-2 — 🟢 cerrado (22-ago-2026):** de las 114 vistas del panel, 97 solo pedían
  sesión iniciada — cualquier usuario autenticado podía leer la bitácora completa de
  auditoría, aprobar/publicar despliegues a toda la flota, dar de baja activos, correr
  scripts, etc. **AC-2** primero: nuevo permiso `despliegues.aprobar_despliegue`
  (`Meta.permissions`, migración `0006`), separado de `change_despliegue` — la regla de
  cuatro ojos verificaba que el aprobador no fuera el autor pero no exigía ningún rol,
  así que cualquier segundo usuario autenticado contaba como "los cuatro ojos"; ahora
  hace falta el permiso, sin otorgarlo a ningún rol operativo por defecto. **AC-1**
  después, módulo por módulo (`auditoria.py`, `reportes.py`, `despliegues.py`,
  `activos.py`, `mantenimiento.py`, `software.py`, `alertas.py`, `cumplimiento.py`):
  `@permission_required` con los permisos `view`/`add`/`change` que Django ya crea
  automáticamente por modelo, siguiendo el mismo mapeo que `seed_permisos.py` ya usa
  para Técnico/Bodeguero/Auditor donde existía, y sin inventar ningún permiso custom
  nuevo salvo el de AC-2. Quedaron a propósito solo con `login_required` las vistas de
  datos estrictamente personales (`notificaciones_lista`/`notificacion_marcar_leida`,
  filtradas por `usuario=request.user`). **Verificado que no rompe nada hoy**: los dos
  únicos usuarios reales de producción (`romo`, `prueba`) son superusuarios sin grupo —
  `has_perm()` siempre es `True` para superusuarios, así que ningún acceso actual se ve
  afectado; el valor se activa el día que se creen usuarios reales con roles limitados
  (Mesa de Ayuda, Bodeguero, etc.). Suite completa verificada en verde en cada módulo
  (474 tests OK al cierre) — varias docenas de tests existentes necesitaron que su
  `setUp` otorgara explícitamente el permiso nuevo al usuario de prueba (antes pasaban
  solo por el scoping de tenant, que seguía siendo necesario pero ya no suficiente).

- **AC-1 — corrección (22-ago-2026):** el cierre anterior no fue completo — al planear
  AC-3 se releyó `scripts.py` y aparecieron 6 de sus 10 vistas sin `@permission_required`
  (`scripts_lista`, `script_detalle`, `ejecuciones_lista`, `ejecucion_detalle`,
  `ejecucion_progreso_partial`, `scripts_programados_lista`), y un `awk` sobre todos los
  módulos de `apps/panel/views/*.py` (buscando `@login_required` sin
  `@permission_required` a continuación) confirmó que el barrido original también se
  había saltado `estaciones.py` (`estaciones_lista`, `estaciones_pendientes_partial`,
  `estacion_info_modal`) y `monitoreo.py` (`monitoreo_lista`, `monitoreo_detalle`,
  `monitoreo_detalle_partial`, `ventanas_mantenimiento_lista`, `tendencia_flota`,
  `red_farmacias_lista`). Mismo tratamiento que el resto de AC-1: `view_<modelo>` de
  Django en cada una (`scripts.view_script`, `scripts.view_ejecucionscript`,
  `scripts.view_scriptprogramado`, `catalogo.view_estacion`, `monitoreo.view_muestrametrica`,
  `monitoreo.view_ventanamantenimiento`, `monitoreo.view_alerta`,
  `monitoreo.view_muestraredfarmacia`), sin permisos nuevos. Un caso no obvio: como
  `estacion_aprobar`/`estacion_rechazar` reusan `estaciones_pendientes_partial(request)`
  como llamada directa a función (no vía `redirect`), el nuevo `@permission_required` de
  esa vista se evalúa también dentro de ellas — un rol de Soporte Técnico con
  `aprobar_estacion` pero sin `view_estacion` quedaba bloqueado al aprobar/rechazar una
  estación pendiente. Esto expuso que `seed_permisos.py` (los Groups reales que se
  usarán el día que se creen usuarios no-superusuario) tampoco otorgaba `view_estacion`
  a ningún rol operativo del piloto RMM — se corrigió ahí mismo, no solo en los tests:
  `catalogo.estacion.view` sumado a Mesa de Ayuda, Soporte Técnico y Operador RMM (este
  último también ganó `monitoreo.muestrametrica/alerta/muestraredfarmacia.view`, sin los
  cuales su propio rol no podría ver el tablero de monitoreo de flota que administra).
  Suite completa verificada en verde (481 tests OK) + `check`/`makemigrations --check
  --dry-run` limpios tras el ajuste.

- **SEC-3 — 🟢 cerrado (22-ago-2026):** `/login/` y `/admin/` aceptaban fuerza bruta
  ilimitada, sin bloqueo ni alerta — sin `django-axes` ni control equivalente en
  `requirements.txt`. Se agregó `django-axes` (`axes` en `INSTALLED_APPS`,
  `AxesBackend` primero en `AUTHENTICATION_BACKENDS` con `ModelBackend` como
  fallback — sin él ningún login válido funcionaría, `AxesBackend` no verifica
  contraseña, solo bloqueo—, y `AxesMiddleware` último en `MIDDLEWARE`, como pide su
  documentación oficial). `AXES_FAILURE_LIMIT = 5`, `AXES_COOLOFF_TIME = 1` (hora).
  **Decisión deliberada, no el default de la librería**: `AXES_LOCKOUT_PARAMETERS =
  ['username']` (sin `'ip_address'`, con el warning `axes.W006` silenciado a
  propósito) — varias estaciones de una misma farmacia salen a internet por la misma
  IP (NAT), así que bloquear también por IP dejaría afuera a toda la farmacia por un
  solo usuario con la contraseña mal; bloquear solo por cuenta protege exactamente lo
  que hay que proteger sin ese efecto colateral. De paso, `MinimumLengthValidator` subió
  de 8 (default de Django) a 12 caracteres. Verificado con un login real de punta a
  punta (no `force_login`, que no pasa por los backends de autenticación): tras
  `AXES_FAILURE_LIMIT` intentos fallidos, ni la contraseña correcta entra —
  `django-axes` corta con `HTTP 429` antes de evaluarla. Un administrador desbloquea
  una cuenta desde `/admin/axes/accessattempt/` (django-axes se auto-registra ahí) o
  con `python manage.py axes_reset_username <usuario>`.

- **BUG-1/BUG-2 — 🟢 cerrado (22-ago-2026):** `apps/activos/services.py` descontaba
  stock con leer-modificar-escribir en Python (`stock.cantidad -= cantidad;
  stock.save()`, sin `select_for_update` ni `F()`) en `registrar_consumible_entregado`,
  `registrar_salida_stock` y `registrar_traslado_bodega` — dos entregas/salidas
  simultáneas de la misma bodega podían leer el mismo saldo antes de que cualquiera
  escribiera, pasando ambas la validación de "stock suficiente" y dejando el inventario
  por debajo de cero sin ningún error visible (**BUG-1**). Ninguna de las transiciones
  de `Activo` corría en `transaction.atomic()`, así que si el `save()` del activo tenía
  éxito pero el `EventoActivo.objects.create()` posterior fallaba, el activo cambiaba de
  estado sin que quedara registro en el historial que la propia app promete como
  "auditoría permanente" (**BUG-2**). Fix: nuevo `_obtener_y_bloquear_stock` (crea la
  fila si no existe + `select_for_update`) combinado con `F('cantidad') ± cantidad` en
  el `UPDATE` — el patrón estándar de Django/Postgres para esta clase de problema — en
  las tres funciones de stock; `registrar_traslado_bodega` además bloquea las dos filas
  (origen y destino) en un orden fijo por `bodega_id` (no por origen/destino de cada
  llamada) para que dos traslados concurrentes en sentidos opuestos nunca formen un
  ciclo de espera entre sí (deadlock). Las 9 funciones de transición/servicio con
  múltiples escrituras (`registrar_ingreso`, `registrar_baja_recomendada`,
  `registrar_asignacion`, `registrar_consumible_entregado`, `registrar_devolucion`,
  `registrar_envio_reparacion`, `registrar_retorno_reparacion`, `registrar_baja`,
  `registrar_recepcion_lote`) quedaron con `@transaction.atomic`. **Sin probar con
  threads reales**: el motor de los tests (SQLite) no soporta bloqueo de fila — dos
  escrituras concurrentes de verdad chocan con "database is locked" en vez de
  serializarse (confirmado empíricamente: un test con `threading.Barrier` fallaba ~7 de
  8 corridas con hilos muriendo en silencio por `OperationalError`, no por la lógica) —
  la garantía real depende de Postgres en producción, que sí bloquea la fila. Se prueba
  en cambio lo que sí es determinístico en cualquier motor: que el `UPDATE` calcula
  siempre sobre el valor real en la base, nunca sobre uno ya leído en Python.

- **AC-3 — 🟢 cerrado (22-ago-2026):** `EjecucionScript` (código arbitrario contra la
  flota) no tenía ninguna aprobación de un segundo usuario para destinos amplios —
  cualquiera con `scripts.add_ejecucionscript` podía correr un script contra toda la
  cadena de un solo clic, a diferencia de `Despliegue` al mismo destino (que sí exige
  `aprobar_despliegue`, ver AC-2). Mismo patrón exacto: nuevo estado
  `EjecucionScript.Estado.PENDIENTE_APROBACION`, FK `aprobado_por`, permiso
  `scripts.aprobar_ejecucionscript` (migración `0005`), y `apps.scripts.services` separa
  la resolución de destino/envío MQTT en `_publicar_ejecucion()` para poder llamarla
  tanto al crear una ejecución que no necesita aprobación como desde la nueva
  `aprobar_ejecucion_script()` una vez aprobada — que aplica la misma regla de cuatro
  ojos que `despliegue_aprobar` (creador ≠ aprobador). `DESTINOS_QUE_REQUIEREN_APROBACION
  = {CADENA, GRUPOS, FARMACIAS}`: `ESTACIONES` queda afuera por ser ya un destino
  angosto (p.ej. instalar el agente en una sola estación). Dos casos que **no** deben
  pasar por la aprobación, ambos ya cubiertos antes de tocar código de producción real:
  las ejecuciones que genera un `ScriptProgramado` vencido (`programado is not None` —
  esa política ya pasó su propio control al crearse, exigir aprobación en cada disparo
  automático rompería la recurrencia), y el comando de management `cambiar_nodo_pos`
  (nuevo parámetro `omitir_aprobacion=True` en `registrar_ejecucion_script`) — ya
  requiere acceso de shell al servidor de producción, un control más fuerte que el
  permiso de panel que esta aprobación reemplaza; sin este segundo caso, el comando
  (usado en vivo esta misma sesión para cambios de nodo POS) habría dejado de publicar
  sus ejecuciones sin ningún aviso. `aprobar_ejecucionscript` no se otorga a ningún rol
  operativo en `seed_permisos.py` por defecto, mismo criterio que `aprobar_despliegue`
  (solo Administrador vía permisos totales) — quién aprueba qué es una decisión de
  gobernanza persona por persona, no de rol. Suite completa verificada en verde (491
  tests OK) + `check`/`makemigrations --check --dry-run` limpios.

- **SEC-1 — 🟡 código listo, DESPLIEGUE PENDIENTE (22-ago-2026):** la firma HMAC de los comandos del panel al
  agente (`apps.catalogo.services.firmar_payload`) no ataba estación ni timestamp — para
  los comandos sin parámetros (`reiniciar`, `consultar_info`, `escanear_actualizaciones`,
  `consultar_software_instalado`) el mensaje firmado era un string **constante**
  ("reiniciar", etc.), así que la firma era la misma para siempre y para cualquier
  estación: capturar un solo mensaje MQTT alcanzaba para reproducirlo indefinidamente.
  Al revisar el fix se encontró algo peor y más amplio que el hallazgo original: los
  mensajes de **Despliegue** (`apps/despliegues/services.py`) y **SolicitudInstalacion**
  de software (`apps/software/services.py`) — los que le dicen a un agente qué paquete
  descargar e instalar en el POS — **no tenían ninguna firma**, ni siquiera la débil de
  los comandos. Solo los protegía la ACL del broker EMQX, y el `sha256` del payload lo
  pone quien arma el mensaje, así que no probaba autenticidad, solo detectaba corrupción
  de transporte.

  **Fix, mismo esquema en los tres:** `estacion` + `timestamp` (epoch) entran a la firma
  de `enviar_comando`/`enviar_script` (mensajes dirigidos a una sola estación, vía su
  propio tópico); `despliegue`/`instalar_software` suman `timestamp` a la firma pero
  **no** `estacion` — sus mensajes son legítimamente broadcast a un tópico de
  grupo/farmacia/cadena (varias estaciones a la vez), así que atar una sola estación no
  aplica ahí; el resto de los campos del payload (url, sha256, versión, modo de
  aplicación...) sí entran a la firma, cerrando la manipulación de contenido. El agente
  (`agente-prueba/agente_prueba.py`, único agente vigente — no hay agente C# en este
  repo ni en ningún otro lado accesible) gana un helper común `_firma_valida()` usado en
  los 6 tipos de mensaje: reconstruye la firma con los mismos campos, y además rechaza
  si (a) el campo `estacion` del mensaje no coincide con el código de esta estación
  (defensa en profundidad — el tópico ya lo acota, pero la credencial MQTT compartida
  con ACL `/saidsof/#` que usan las estaciones aún no migradas la haría insuficiente por
  sí sola, ver `deploy/bootstrap-emqx.sh`) o (b) el `timestamp` cae fuera de una ventana
  de `VENTANA_TIMESTAMP_SEGUNDOS = 120` segundos.

  **Riesgo residual documentado, no resuelto acá:** `COMANDO_HMAC_SECRET` es una única
  clave **global** compartida por las ~1.800 estaciones (no hay secreto por estación
  para esto, a diferencia de las credenciales MQTT que sí son por estación desde
  `emqx_admin.py`). Atar estación+timestamp cierra el replay y la manipulación de
  contenido, pero no evita que un agente comprometido (que por diseño conoce el secreto
  global para poder validar sus propios comandos) fabrique un mensaje válido "para otra
  estación" con solo poner el código que quiera — para cerrar eso haría falta una clave
  HMAC por estación, un cambio más grande que se deja para una ronda aparte si se decide
  priorizarlo. Tampoco se tocó acá el otro hallazgo relacionado que quedó anotado
  durante la investigación: la migración a ACL por estación en EMQX está completa en
  código pero el corte real de la credencial compartida (`deploy/emqx-narrow-acl-agente.sh`)
  es un paso manual que el propio repo documenta como pendiente hasta confirmar que toda
  la flota migró — mientras tanto, esa credencial compartida sigue con ACL `/saidsof/#`
  ("todo"). Verificado con un script que importa `firmar_payload` del servidor y
  `firmar()` del agente por separado y confirma que producen el mismo hex para los 4
  tipos de mensaje (comando, ejecutar_script, desplegar, instalar_software) con los
  mismos campos — la firma es un contrato entre dos implementaciones independientes, así
  que esto es la única forma real de probar que no se rompió sin una estación física.
  Suite completa verificada en verde (499 tests OK) + `check`/`makemigrations --check
  --dry-run` limpios. Sin migración: es código puro, sin cambios de modelo.

  **⚠️ NO DESPLEGAR EN EL SERVIDOR TODAVÍA.** A diferencia de todos los ítems anteriores
  de esta auditoría, este cambio no es solo del lado servidor: cambia el protocolo entre
  servidor y agente. Si se reconstruye/reinicia el `web`/`worker` de producción con este
  código, el servidor empieza a firmar con `estacion`+`timestamp`, pero **todos los
  agentes ya instalados siguen validando con el esquema viejo** — la firma no va a
  coincidir y esos agentes van a rechazar *todo* comando (reiniciar, scripts,
  despliegues, instalación de software) hasta que cada estación reciba el agente nuevo.
  Aclarado con el usuario (22-ago-2026): el piloto real hoy son solo **5 estaciones**
  con el agente instalado (no las ~1.800 de la capacidad de diseño), así que una
  actualización manual puntual es perfectamente viable — pero el usuario pidió no
  depender de eso hacia adelante y en cambio construir la actualización remota desde el
  panel (ver ítem siguiente). Este commit (`9742064`) queda **código completo,
  commiteado y pusheado, pero sin desplegar** hasta que las 5 estaciones tengan el
  agente nuevo (ver secuencia de despliegue en el ítem "Actualización remota del agente
  desde el panel", más abajo).

- **Actualización remota del agente desde el panel — 🟡 código listo, pendiente de
  secuenciar con SEC-1 (22-ago-2026):** no era un hallazgo de la auditoría original,
  surgió como bloqueante directo de SEC-1: para desplegar la firma HMAC nueva hacía
  falta alguna forma de llevar el agente de las estaciones reales al mismo esquema, y
  hoy no existía ninguna (el GPO Computer Startup Script,
  `deploy/docs/desplegar-agente-gpo.ps1`, es idempotente a propósito — sale sin hacer
  nada si el servicio ya existe, así que solo cubre altas nuevas, no reemplazar el
  binario de una estación que ya lo tiene corriendo). Con el piloto en 5 estaciones el
  usuario decidió construir el mecanismo real en vez de resolverlo a mano una vez más:

  - **Modelo** `apps.catalogo.models.VersionAgente` (mismo patrón que
    `apps.software.models.VersionAplicacion`: el `.exe` compilado con
    `agente-prueba/build.ps1` se sube a mano por el admin, el sha256 se calcula acá al
    guardar, nunca se confía en uno que llegue por otro lado).
  - **Comando nuevo** `actualizar_agente` (`apps.catalogo.services.enviar_actualizacion_agente`),
    mismo esquema de firma que el resto de SEC-1 (`estacion`+`timestamp`).
  - **Botón "Actualizar agente"** en el modal de la estación, permiso propio
    `catalogo.actualizar_agente_estacion` (mismo nivel de riesgo que
    `reiniciar_estacion`/`aprobar_estacion`, otorgado a Soporte Técnico en
    `seed_permisos.py`) — visible solo si hay una `VersionAgente` más nueva que la que
    la estación reportó (`Estacion.version_agente`, ya existía y ya se actualiza en cada
    heartbeat, no hizo falta ningún campo nuevo para saber si la actualización aplicó).
  - **Lado agente**: nuevo handler que descarga el `.exe`, verifica el sha256, y lanza un
    script PowerShell **separado y desatachado** que hace lo que el propio proceso no
    puede hacerse a sí mismo (hasta soltar el lock de su propio ejecutable): espera a que
    el servicio de Windows quede `Stopped` (lo detiene él, vía `Stop-Service` — un
    servicio no puede pararse solo desde adentro más que devolviendo el control a
    `SvcDoRun`, que es justo lo que `Stop-Service` dispara), reemplaza el binario
    (guardando un `.bak` para poder revertir a mano si algo sale mal) y vuelve a
    arrancarlo. `VERSION_AGENTE_PRUEBA` subió a `agente-prueba-0.2` para este build (SEC-1
    + esta función, empaquetadas juntas).
  - **Bootstrap con un huevo y la gallina, resuelto a mano una única vez**: un agente
    anterior a este cambio no entiende el comando `actualizar_agente` (ni el esquema de
    firma de SEC-1) — no hay forma de que el panel lo actualice a sí mismo la primera
    vez. Secuencia real de despliegue, en orden estricto:
    1. Compilar el nuevo `.exe` (`build.ps1`) e instalarlo a mano en las 5 estaciones del
       piloto (reemplazo directo del servicio, sin pasar por el panel — es la única vez
       que hace falta).
    2. Recién ahí desplegar el servidor (`git pull` + migrar + reconstruir
       `web`/`worker`/etc. + reiniciar) — para entonces las 5 estaciones ya entienden el
       esquema nuevo, así que no se rompe nada.
    3. Subir ese mismo `.exe` como la primera `VersionAgente` en el admin, para que de
       ahí en adelante cualquier actualización futura salga por el botón del panel.
  - Suite completa verificada en verde (506 tests OK) + `check`/`makemigrations --check
    --dry-run` limpios. Migración `0019` (nuevo modelo + permiso).
