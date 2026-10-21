# Inventario técnico de SaidSoft

**Fecha:** 20-oct-2026 · **Commit auditado:** `68d0045` (`master` = `origin/master`) · **Modo:** solo lectura.

## Para qué sirve y qué no repite

Es la foto **verificada contra el código actual** de lo que existe. No reemplaza a:

| Documento | Qué tiene que este no repite |
|---|---|
| `docs/auditoria/fase1-descubrimiento.md` | Stack con versiones, convenciones, entorno de pruebas (línea base `fe2819f`, 95 modelos) |
| `docs/modulos.md` | Qué mirar cuando falla cada módulo (guía de mesa de ayuda) |
| `docs/producto/SAIDSOFT_RMM_1_0_ALCANCE.md` | Qué entra en RMM 1.0 y qué se ocultó en producción |

Se escribió aparte, y no como actualización de `fase1-descubrimiento.md`, porque ese documento está
fechado y aprobado como línea base histórica ("no se reescribe", §0.1). Este corrige sus cifras.

## Cómo se midió

- **[CÓDIGO]**: introspección de Django sin consultar ninguna base (`apps.get_models()`,
  `get_resolver()`, registro de Celery, `get_commands()`), más búsqueda en el código.
- **[PROD fecha]**: medido en producción por solo lectura en sesiones anteriores, con la fecha.
- **[DOC]**: afirmado por la documentación del repositorio, no verificado ahora.
- **NO VERIFICADO**: no hay evidencia suficiente.

## Cifras

| Qué | Valor | Fuente |
|---|---|---|
| Apps Django propias | **16** (15 con modelos + `panel`) | [CÓDIGO] |
| Modelos | **107** (no 95: la línea base de la fase 1 quedó atrás) | [CÓDIGO] |
| Rutas totales | 732: 506 del admin de Django, 176 del panel y 50 fuera de ambos | [CÓDIGO] |
| Rutas de la API REST | 48 bajo `api/`, incluidas las variantes de sufijo de formato de DRF; las otras 2 son el webhook de WhatsApp y `/media/` | [CÓDIGO] |
| Tareas de Celery | 34; 28 programadas en `CELERY_BEAT_SCHEDULE` | [CÓDIGO] |
| Comandos de management | 65 | [CÓDIGO] |
| Contenedores en producción | 12 | `deploy/docker-compose.yml` + [PROD 20-oct] |
| Farmacias modeladas | 701 | [PROD 28-sep] |
| Estaciones con agente | 85 registradas, 56 con latido en 15 min | [PROD 19-oct] |
| Conexiones MQTT de estaciones | 71 | [PROD 20-oct] |

## Aplicaciones

Clasificación funcional: **A** implementada y verificada · **B** implementada, sin verificar en
ejecución · **C** parcial · **D** duplicada · **E** sin uso demostrado · **F** obsoleta con
evidencia · **G** no verificada.

| App | Responsabilidad comprobada | Modelos | Clasificación | Evidencia de uso |
|---|---|---|---|---|
| `catalogo` | Identidad de la red: unidad de negocio, grupo, farmacia, estación, versión del agente, instalador de un solo uso, clave BitLocker, periféricos. Enrolamiento, comandos firmados, pausa, publicación MQTT del servidor | 8 | **A** | 85 estaciones, 50 actualizadas a 0.35 por `actualizar_agente` [PROD 19-oct] |
| `mqtt_worker` | Único consumidor de lo que publica la flota: 15 tópicos. Latidos de procesos, mensajes fallidos, enrolamientos rechazados, administración de EMQX | 3 | **A** | 71 conexiones, latido del worker en el panel [PROD 20-oct] |
| `monitoreo` | Métricas, enlaces, alertas y reglas, notificación, SNMP, servicios del POS, eventos de Windows, ventanas de mantenimiento, NetFlow, ARP, bot de Telegram, MeshCentral, salud de la plataforma | 29 | **A** en alertas, enlaces, salud y catálogos · **B** en SNMP genérico · **E** en IA (`ANTHROPIC_API_KEY` vacía [PROD 28-sep]) | 184 alertas históricas [PROD 28-sep]; catálogos publicados el 20-oct |
| `software` | Catálogo de aplicaciones, solicitudes de instalación, inventario de software instalado, escaneos programados | 7 | **A** | Solicitudes 11 a 14 (Firefox, LibreOffice), 63 resultados terminales [PROD 19-oct] |
| `despliegues` | Actualización del POS Farmamia/Elipsys: paquete, aprobación de cuatro ojos, publicación, freno, promoción, rollback | 3 | **B** | Una sola estación piloto, ML016-A, en "Confirmado OK" [DOC: `PLAN_MODERNIZACION.md`]; 0 retenidos [PROD 19-oct] |
| `cuentas` | Perfil de usuario, alcance por unidad de negocio, MFA, permiso de la API | 1 | **A** | Uso diario del panel |
| `auditoria` | Registro de acciones humanas (`registrar_evento`) | 1 | **A** | La importan 8 apps [CÓDIGO] |
| `panel` | Presentación HTMX de todo el sistema; sin modelos | 0 | **A** | — |
| `scripts` | Biblioteca de PowerShell/BAT, ejecución ad hoc y programada con aprobación, resultados. Incluye el parcheo de terceros con `winget` (`seed_scripts_parcheo`) | 4 | **B**. Pantallas ocultas en producción desde `9634c68` | Lo usa el bot de Telegram ("sincronizar hora") y `cambiar_nodo_pos` [CÓDIGO]. Uso por pantallas: **NO VERIFICADO** |
| `activos` | Inventario físico (ITAM): activo, colaborador, bodega, compras, movimientos, consumibles | 15 | **B** en lo básico · **E** en compras y bodegas | 22 activos, 10 colaboradores [PROD 5-oct] |
| `mantenimiento` | Servicio en campo: mantenimiento, checklist, firmas, repuestos, visitas, notificaciones, ubicación del técnico; API de la app móvil | 18 | **B** · `ActividadPlanificada` **E** | 3 mantenimientos, 1 visita [PROD 28-sep]; `ActividadPlanificada` 0 filas [PROD 28-sep] |
| `aperturas` | Alta "cero-touch" de una farmacia con plantilla y token | 7 | **B** en pantallas · **A** en `consumir_token` (enrolamiento) | 0 aperturas [DOC] |
| `cumplimiento` | Campañas de cumplimiento por estación, farmacia o colaborador | 4 | **E** | Sin uso [DOC: producto-v1] |
| `viaticos` | Gastos de campo por zona | 3 | **E** | 0 zonas [PROD 28-sep] |
| `whatsapp` | Webhook de Meta, mesa de novedades, chatbot | 3 | **C** | Chatbot apagado por `WHATSAPP_MODO` [DOC]; `Novedad` la usan el bot y el historial de enlaces [CÓDIGO] |
| `facturacion` | Actividad mensual por estación, escrita en cada latido | 1 | **B** | Se escribe en cada latido [CÓDIGO]; uso de negocio **NO VERIFICADO** |

La lista completa de modelos y tablas está en el anexo.

## Procesos en ejecución (`deploy/docker-compose.yml`)

| Servicio | Qué corre | Imagen o comando |
|---|---|---|
| `db` | PostgreSQL + TimescaleDB, publicado solo en `127.0.0.1:5433` | `timescale/timescaledb:2.17.2-pg16` |
| `emqx` | Broker MQTT; TLS en 8081 y dashboard en 8082 | `emqx/emqx:5.8.3` |
| `web` | Panel y API | `gunicorn`, 3 workers, timeout 600 s |
| `nginx` | TLS en 8084 y `/media/` en HTTP 8080 | `nginx:1.27-alpine` |
| `worker` | Consumidor MQTT de la flota | `manage.py run_mqtt_worker` |
| `celery_worker` | Tareas asíncronas, con la credencial MQTT del panel | `celery worker --concurrency=2` |
| `celery_beat` | Planificador de las 28 tareas periódicas | `celery beat` |
| `redis` | Broker de Celery | `redis:7-alpine` |
| `meshcentral` | Escritorio remoto, publicado en 8083 | `ghcr.io/ylianst/meshcentral:latest` (sin versión fija) |
| `meshcentral_worker` | Cruce de presencia con MeshCentral | `manage.py run_meshcentral_worker` |
| `telegram_bot` | Bot de consultas y avisos | `manage.py run_telegram_bot` |
| `netflow_collector` | Piloto NetFlow, UDP 8085 | `manage.py run_netflow_collector` |

Imagen de la aplicación: `python:3.13-slim` (`deploy/Dockerfile:3`). **`CLAUDE.md` dice "Python 3.14"**:
esa es la versión del entorno local, no la de producción.

## Tareas de Celery

- **28 periódicas** [CÓDIGO]. Las más frecuentes:
  - `marcar_estaciones_offline` y `latido_beat`, cada 60 s;
  - `sondear_enlaces_farmacias`, cada 120 s;
  - SNMP, red vía agente, ancho de banda, notificaciones de enlaces y WhatsApp, cada 300 s.
  - Diarias: purgas de 03:00 a 03:40, escaneos y programados de 06:00 a 07:00, resumen de Telegram a las 08:00.
- **6 encoladas desde el código, sin programación:**
  - `limpiar_actualizacion_pendiente`, `retirar_retenido_despliegue` y `retirar_retenido_instalacion`, desde el worker MQTT;
  - `notificar_alerta` y `diagnosticar_alerta`, desde `abrir_o_mantener_alerta`;
  - `procesar_mensaje_whatsapp`, desde el webhook.
- **2 encoladas por otra tarea:** `sondear_lote_snmp` (desde `repartir_sondeo_snmp`) y `generar_informe_pdf` (mantenimiento).

## Configuración de producción

`deploy/docker-compose.yml` usa **52 variables**. **22 no están documentadas en
`deploy/.env.prod.example`** [CÓDIGO]:

- `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_IDS_AUTORIZADOS`, `ENLACES_TELEGRAM_CHAT_ID`,
  `MESA_AYUDA_TELEGRAM_CHAT_ID`, `ENLACES_NOTIFICAR_A`;
- `EMAIL_HOST`, `EMAIL_PORT`, `EMAIL_HOST_USER`, `EMAIL_HOST_PASSWORD`;
- 10 de WhatsApp (`WHATSAPP_*`);
- `ANTHROPIC_API_KEY`, `NETFLOW_EXPORTADORES`, `SECURE_SSL_REDIRECT`, `COOKIES_SOLO_HTTPS`.

Es un **requisito de recuperación**: un `.env` reconstruido desde la plantilla perdería el canal
de Telegram, que es hoy el único aviso que funciona (`EMAIL_HOST_USER` vacía [PROD 28-sep]).

Al revés, la plantilla documenta `MESHCENTRAL_API_CA_CERT` y el `.env` de producción no la define:
`docker compose` avisa "variable is not set" en cada comando [PROD 20-oct].

**Dependencias de Python:** `requirements.txt` usa rangos (`Django>=5.2.8,<6.0`,
`djangorestframework>=3.15`, …) y no hay archivo de bloqueo. Dos builds del mismo commit pueden
instalar versiones distintas [CÓDIGO].

## Componentes fuera de Django

| Componente | Dónde | Estado |
|---|---|---|
| Agente Windows 0.36 (servicio LocalSystem) | `agente-prueba/agente_prueba.py` | **A**. En la flota conviven 0.29 a 0.35; la 0.36 no estaba registrada como `VersionAgente` [PROD 19-oct] |
| App móvil Flutter del técnico | `movil-campo/` | **B**. Distribución manual del APK, con seis cambios sin distribuir [DOC: `CLAUDE.md`] |
| Sonda externa de enlaces | `deploy/sonda-enlaces/` | **F**: el servidor sondea directo [DOC: `CLAUDE.md`] |
| Scripts del host | `deploy/*.sh`, timers de systemd de respaldo y salud | **A** en respaldo y salud [PROD 20-oct] |

## Anexo: modelos por aplicación [CÓDIGO]

- **catalogo:** UnidadNegocio, Grupo, Farmacia, Estacion, VersionAgente, EnlaceInstalador, ClaveRecuperacionBitLocker, PerifericoDetectado
- **mqtt_worker:** WorkerHeartbeat, MensajeMqttFallido, EnrolamientoRechazado
- **monitoreo:** MuestraMetrica, MuestraRedFarmacia, VentanaNetflowFarmacia, FlujoNetflowAgregado, EstadoDispositivo, EventoMonitoreo, ReglaAlerta, Alerta, PosErrorDetectado, VentanaMantenimiento, CanalNotificacion, EquipoBordeFarmacia, DispositivoDetectado, EstadoEnlaceFarmacia, ReinicioEquipoBorde, EventoEnlaceFarmacia, EventoSaturacionEnlace, ReanudacionMonitoreo, BarridoEnlaces, EstadoRedActivo, EventoSistemaVigilado, EventoSistemaDetectado, ServicioPosMonitoreado, EstadoServicioPos, MuestraServicioPos, ConfiguracionMonitoreo, PerfilSnmp, ObjetivoSnmp, LecturaSnmpActual
- **software:** AplicacionCatalogo, VersionAplicacion, SolicitudInstalacion, ResultadoInstalacion, EventoInstalacion, SoftwareInstaladoDetectado, InventarioProgramado
- **despliegues:** Despliegue, ResultadoDespliegue, EventoDespliegue
- **cuentas:** PerfilUsuario · **auditoria:** EventoAuditoria · **facturacion:** ActividadMensualEstacion
- **scripts:** Script, EjecucionScript, ResultadoEjecucionScript, ScriptProgramado
- **activos:** CategoriaEquipo, Marca, Departamento, Cargo, Ubicacion, Bodega, Colaborador, TipoConsumible, StockBodega, OrdenCompra, OrdenCompraDetalle, RecepcionLote, MovimientoInventario, Activo, EventoActivo
- **mantenimiento:** MantenimientoProgramado, TipoMantenimiento, Mantenimiento, MantenimientoEquipo, ActividadChecklist, ActividadRealizada, EventoMantenimiento, AcuerdoNivelServicio, FirmaMantenimiento, ImagenMantenimiento, RepuestoUtilizado, ActividadPlanificada, Notificacion, ConsentimientoMonitoreo, UbicacionTecnico, VisitaTecnica, AccionOfflineAplicada, CierreEnConflicto
- **aperturas:** PlantillaApertura, PerfilEstacionPlantilla, PasoPlantilla, Apertura, PasoApertura, EventoApertura, TokenApertura
- **cumplimiento:** ActividadCumplimiento, ResultadoCumplimientoEstacion, ResultadoCumplimientoFarmacia, ResultadoCumplimientoColaborador
- **viaticos:** ColaboradorZona, ReporteViatico, AlertaViatico
- **whatsapp:** Novedad, MensajeWhatsApp, ConversacionWhatsApp

Las tablas usan el nombre del modelo en minúsculas con guiones bajos (`db_table` explícito en
todos), salvo `Novedad` → `novedad_farmacia` y `PasoPlantilla` → `paso_plantilla_apertura`.
