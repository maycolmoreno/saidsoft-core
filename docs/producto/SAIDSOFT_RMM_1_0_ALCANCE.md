# SaidSoft RMM 1.0 — Alcance del producto

**Fecha:** 20-oct-2026 · **Rama:** `producto/rmm-1.0-consolidacion` (sobre `6e0df91`).
**Para qué sirve:** decidir qué código se conserva, qué se oculta y qué se retira, y en qué
orden. No es una auditoría de seguridad: lo de seguridad está en `docs/auditoria/`.

**Vocabulario:**

| Categoría | Significado |
|---|---|
| `CORE` | Imprescindible para RMM 1.0 |
| `SECUNDARIO` | Necesario en algún contexto, no es el objetivo principal |
| `DIFERIDO` | El código se conserva; no se expone en 1.0 |
| `RETIRADA_CANDIDATA` | Hay indicios suficientes para eliminarlo en una tanda posterior |
| `DEPENDENCIA_COMPARTIDA` | No se retira sin antes separar lo que usa el núcleo |

---

## 1. Objetivo del producto

SaidSoft RMM 1.0 hace dos cosas para la red de farmacias:

1. **Monitoreo de infraestructura:**
   - disponibilidad de estaciones, enlaces y servicios del POS;
   - estado y comunicación de los agentes;
   - alertas, incidentes e historial;
   - identificación de farmacia, estación y versión.
2. **Gestión centralizada de software:**
   - inventario de versiones;
   - instalación y actualización controlada de aplicaciones autorizadas;
   - seguimiento del resultado;
   - actualización controlada del POS Farmamia/Elipsys (`Zabyca.Pos.Desktop.exe`);
   - actualización controlada del agente SaidSoft.

Todo lo demás se difiere o se retira.

---

## 2. Funcionalidades incluidas (`CORE`)

| Funcionalidad | Dónde vive |
|---|---|
| Sitios, estaciones, unidad de negocio, enrolamiento y aprobación | `catalogo` (`Farmacia`, `Estacion`, `UnidadNegocio`, `aprobar_estacion`) |
| Comandos firmados al agente, pausa remota, catálogos del agente | `catalogo/services.py`, `monitoreo/servicios_pos.py` |
| Ingesta MQTT de la flota (latidos, métricas, eventos, POS, estados de operaciones) | `mqtt_worker` |
| Credenciales MQTT por estación y ACL | `mqtt_worker/emqx_admin.py`, `deploy/bootstrap-emqx.sh` |
| Enlaces: sondeo, caídas, saturación, historial y disponibilidad | `monitoreo/enlaces.py`, `saturacion.py`, `historial_enlaces.py` |
| Alertas, reglas, escalamiento, notificación (correo, Teams, Telegram) | `monitoreo/services.py`, `ReglaAlerta`, `Alerta`, `CanalNotificacion` |
| Centro de Monitoreo, tablero, salud de la plataforma | `panel/views/monitoreo.py`, `dashboard.py`, `monitoreo/salud.py` |
| Servicios del POS y eventos de Windows vistos desde la caja | `monitoreo` (`EstadoServicioPos`, `EventoSistemaDetectado`) |
| SNMP del equipo de borde (ancho de banda, identidad, reinicios) | `monitoreo/mikrotik.py` |
| Actualizaciones de Windows **pendientes** (solo escaneo) | Agente `escanear_actualizaciones`, ficha de estación |
| Catálogo de software, versiones, instalaciones e inventario de lo instalado | `software` |
| Despliegues del POS Farmamia/Elipsys: paquete, aprobación de cuatro ojos, publicación por MQTT, progreso por estación, freno por % de error, pausa, promoción y rollback | `despliegues`, `panel/views/despliegues.py`, agente `_manejar_despliegue`. Riesgos pendientes en la sección 11 |
| Actualización del agente por tandas con canario; instalador de un solo uso | `panel/views/agente_flota.py`, `instalador.py`, `catalogo.VersionAgente` |
| Usuarios, roles, MFA, alcance por unidad de negocio | `cuentas`, `activos/management/commands/seed_permisos.py` |
| Auditoría de acciones | `auditoria` |
| Tareas periódicas del núcleo | `CELERY_BEAT_SCHEDULE` (sondeos, alertas, purgas, offline) |
| Agente Windows | `agente-prueba/` (fuera de esta consolidación) |

## 3. Funcionalidades secundarias (`SECUNDARIO`)

| Funcionalidad | Por qué se conserva |
|---|---|
| Bot de Telegram (consultas y avisos) | Hoy es el único canal de aviso que funciona. Usa `scripts` para sincronizar la hora |
| MeshCentral (escritorio remoto y cruce de presencia) | Produce la alerta `agente_caido_red_viva`; es la vía de recuperación de estaciones mudas |
| SNMP genérico (impresoras, UPS) | Solo visibilidad; no molesta, pero no se amplía en 1.0 |
| Reportes CSV de enlaces, estaciones, cumplimiento de versión del POS | `reporte_cumplimiento_csv` mide la **versión del POS** (`catalogo`), no las campañas de `cumplimiento` |
| `facturacion` (endpoints activos por mes) | La escribe cada latido (`mqtt_worker/services.py`). Su utilidad de negocio no está confirmada (sección 9) |
| Inventario básico de activos (`Activo` ↔ `Estacion`) | Asocia el equipo físico con la estación |

## 4. Funcionalidades diferidas (`DIFERIDO`)

**En este primer corte** (pantallas con 404 en producción y sin menú; código, modelos y servicios intactos):

| Módulo | Rutas | Por qué se difiere |
|---|---|---|
| Viáticos | 11 (`viaticos/*`) | Finanzas, no operación de TI. 0 zonas cargadas [auditoría] |
| Aperturas | 10 (`aperturas/*`) | Nunca se usó (0 aperturas). Su servicio de token **sigue activo** en el enrolamiento |
| Campañas de cumplimiento | 4 (`cumplimiento/*`) | Atestación manual sin uso |
| NetFlow | 1 (`monitoreo/netflow/`) | Piloto; el contenedor `netflow_collector` sigue |
| Mesa de ayuda por WhatsApp | 2 (`mesa/*`) | Duplica Odoo; el chatbot está apagado (`WHATSAPP_MODO`) |
| Scripts PowerShell/BAT | 11 (`scripts/*`, `ejecuciones/*`) | Ejecución genérica fuera de 1.0 |

Despliegues del POS figuraba en esta lista en la primera versión del corte. Se pasó a `CORE` antes de publicarlo (sección 2): es la actualización del POS Farmamia/Elipsys, el software principal de las farmacias, y su ciclo completo ya llegó a "Confirmado OK" con el POS real en la estación piloto ML016-A.

**Diferidos sin cambios en este corte:**

| Funcionalidad | Estado |
|---|---|
| Diagnóstico con IA de alertas críticas | Sin interfaz propia; inactivo si no hay `ANTHROPIC_API_KEY` |
| Descubrimiento ARP (`DispositivoDetectado`) | Solo comando manual |
| Aplicación de parches de Windows | **No existe** en el código: el agente solo escanea |

## 5. Candidatos a retirada (`RETIRADA_CANDIDATA`)

| Elemento | Evidencia |
|---|---|
| `viaticos` (app completa) | Nadie la importa; 0 zonas; ya diferida |
| `cumplimiento` (app completa) | Sin uso; solo la referencia un permiso de un CSV (sección 6) |
| `mantenimiento.ActividadPlanificada` | 0 filas [`docs/prioridad-2030.md`]; redundante con `VisitaTecnica` |
| `probar_apertura.py` (raíz, versionado) | Script de demostración |
| `deploy/sonda-enlaces/` | Redundante: el servidor sondea directo |
| Webhook público de WhatsApp (`whatsapp/webhook/`) | Chatbot apagado, sin webhook configurado en Meta [plan] |

## 6. Dependencias compartidas (`DEPENDENCIA_COMPARTIDA`)

| App | Qué usa el núcleo | Qué es prescindible |
|---|---|---|
| `aperturas` | `consumir_token` / `enrolar_estacion_de_apertura` en `mqtt_worker/services.py` (enrolamiento con token) | Pantallas, plantillas, pasos |
| `scripts` | El bot de Telegram ("sincronizar hora"), `cambiar_nodo_pos`, el worker (`script_estado`), `aperturas` | Pantallas, biblioteca, ad-hoc y programados |
| `whatsapp` | `monitoreo/telegram_bot.py` (respuestas de la mesa) y `historial_enlaces.py` (causa "luz" fuera del SLA) importan sus modelos | Bandeja `/mesa/`, chatbot, webhook |
| `activos` | `Farmacia.tecnico_asignado` → `Colaborador`; `Activo.estacion`; FK desde `monitoreo` y `mantenimiento` | Compras, bodegas, movimientos, consumibles (ITAM avanzado) |
| `mantenimiento` | `Alerta.mantenimiento` (FK), botón "abrir mantenimiento" en alertas, bandeja `Notificacion` (la usan otras apps), **API de la app móvil de los técnicos** | Planificación, programados, PDF |
| `cumplimiento` | El permiso `cumplimiento.view_actividadcumplimiento` protege `reporte_cumplimiento_csv` (`panel/views/reportes.py:83`) | Todo lo demás |
| `facturacion` | Escritura en cada latido y dos reportes | Si no hay cobro, todo |

## 7. Aplicaciones Django afectadas

| App | Clasificación | En este corte |
|---|---|---|
| `catalogo`, `mqtt_worker`, `cuentas`, `auditoria`, `software`, `panel` | CORE | Sin cambios de comportamiento (el panel gana el mecanismo de módulos diferidos) |
| `despliegues` | CORE | Sin cambios: no se puede diferir |
| `monitoreo` | CORE (NetFlow e IA diferidos dentro) | Pantalla NetFlow diferida |
| `activos`, `mantenimiento` | DEPENDENCIA_COMPARTIDA | Sin cambios (sección 9) |
| `scripts`, `aperturas`, `whatsapp` | DEPENDENCIA_COMPARTIDA, UI DIFERIDA | Pantallas diferidas |
| `viaticos`, `cumplimiento` | DIFERIDO → RETIRADA_CANDIDATA | Pantallas diferidas |
| `facturacion` | SECUNDARIO | Sin cambios |

## 8. Riesgos de eliminar cada componente

| Componente | Riesgo |
|---|---|
| `aperturas` (app) | Rompe el enrolamiento con token de apertura (`mqtt_worker`). Separar antes `consumir_token` |
| `scripts` (app) | Se cae "sincronizar hora" del bot de Telegram y `cambiar_nodo_pos` |
| `whatsapp` (app) | Rompen imports en `telegram_bot.py` y `historial_enlaces.py`; el SLA de enlaces deja de excluir cortes de luz |
| `mantenimiento` (app) | Rompe `Alerta.mantenimiento`, la bandeja de notificaciones y **la app móvil de los técnicos** |
| `activos` (app) | FK desde `catalogo`, `monitoreo`, `mantenimiento`, `viaticos`, `cuentas` |
| `facturacion` (app) | Hay que quitar su escritura del latido y dos reportes |
| `viaticos`, `cumplimiento` (apps) | Bajo. Migraciones que borran tablas: exportar datos antes y cortar el despliegue si hay filas inesperadas (el patrón de `catalogo/0033_baja_de_integraciones`) |
| Contenedor `netflow_collector` | Bajo; se pierde la telemetría del piloto |

**Regla para todas:** el código y las tablas se retiran recién cuando:
1. el módulo lleva una versión entera diferido sin que nadie lo reclame;
2. sus dependencias compartidas ya se separaron;
3. sus datos se exportaron.

## 9. Orden recomendado de las siguientes retiradas

1. **Tanda 2, bajo riesgo:**
   - ocultar también en `/admin/` los modelos de los módulos diferidos;
   - borrar `probar_apertura.py` y `deploy/sonda-enlaces/`;
   - retirar `ActividadPlanificada`;
   - mover el permiso de `reporte_cumplimiento_csv` a uno de `catalogo`, para desacoplar `cumplimiento`.
2. **Tanda 3:** retirar las apps `viaticos` y `cumplimiento`, con exportación de datos y migraciones que se cortan si encuentran filas.
3. **Tanda 4:**
   - `whatsapp`: llevar a `monitoreo` lo que usan el bot y el historial; luego retirar el webhook, la bandeja y la app;
   - NetFlow: pantalla, comando y contenedor;
   - IA: si no se va a usar, quitar `diagnostico_ia.py` y la dependencia `anthropic`.
4. **Tanda 5:**
   - `activos` y `mantenimiento`, con la app móvil, pasan a otro proyecto (sección 10);
   - `facturacion`: ¿hay cobro entre unidades? Necesita decisión.
5. **Tanda 6:** separar de `scripts` y `aperturas` lo que usa el núcleo (enrolamiento con token, estados en el worker, hora del bot) y después retirar el resto.

---

## 10. Inventario y mantenimiento como proyecto aparte

**Decisión:** el inventario (`activos`) y el mantenimiento (`mantenimiento` y la app móvil de los técnicos) se van a un proyecto propio. Ese proyecto le consulta a SaidSoft lo que el agente ya reporta de cada estación —serial, procesador, RAM, disco, periféricos, software— en lugar de pedírselo otra vez a los equipos o copiar tablas.

**Vínculo entre los dos:** el código de la estación (único y estable) y el número de serie. Nunca el id de la base de datos.

### Lo que hoy impide separarlo

| Dependencia | Dónde | Cómo se resuelve |
|---|---|---|
| Ping y SNMP de impresoras y routers | `EstadoRedActivo` y `ObjetivoSnmp` → `activos.Activo` | Paso 2: un modelo propio en `monitoreo`, porque monitorear esos equipos es parte del producto |
| Técnico asignado a una farmacia | `Farmacia.tecnico_asignado` → `activos.Colaborador` | Paso 2: pasa a apuntar a un usuario o se va con el otro proyecto |
| Perfil de usuario | `PerfilUsuario.colaborador` → `activos.Colaborador` | Paso 2 |
| Abrir un mantenimiento desde una alerta | `Alerta.mantenimiento`, `abrir_mantenimiento_desde_alerta` | Paso 3: el otro proyecto lee `/api/v1/rmm/alertas/` y lo abre él |
| Cierres en conflicto en la salud | `contar_cierres_en_conflicto` | Paso 3: el indicador se va con mantenimiento |
| App móvil | `/api/v1/` de `mantenimiento` | Paso 4: la app apunta al servidor del otro proyecto |

En sentido contrario no hay bloqueo: ni `activos` ni `mantenimiento` necesitan vivir dentro de SaidSoft. Solo necesitan leer estaciones, farmacias y alertas.

### Pasos

1. **API de integración de solo lectura en SaidSoft. Hecho en esta rama, sin desplegar.** Rutas bajo `/api/v1/rmm/`:
   - `estaciones/`: identidad, serial, hardware, Windows, estado de conexión y último contacto. Filtros: `farmacia`, `unidad_negocio`, `numero_serie`, `hostname`, `estado_conexion`;
   - `estaciones/<código>/`: lo mismo, más periféricos USB y software instalado;
   - `farmacias/`. Filtros: `unidad_negocio`, `activa`;
   - `alertas/`. Filtros: `desde_id` (para consultar solo las nuevas), `estado`, `estacion`, `farmacia`.

   Todas comparten estas reglas:
   - token de DRF de un usuario propio para la integración, y los mismos permisos (`catalogo.view_estacion`, `catalogo.view_farmacia`, `monitoreo.view_alerta`) y alcance por unidad de negocio que el panel;
   - responden 200 por página, hasta 500 con `por_pagina`;
   - lo que el usuario no puede ver devuelve 404;
   - no exponen token de enrolamiento, secreto HMAC, id de MeshCentral, conexión al POS ni clave de BitLocker.

   Código: `apps/catalogo/api_views.py`, `apps/catalogo/serializers.py`, `apps/catalogo/api_urls.py` y `apps/monitoreo/api_rmm.py`. Pruebas: `apps/catalogo/tests_api_rmm.py`. Para no atar el núcleo a mantenimiento, `PermisoDeclarado` se movió a `apps/cuentas/api_permissions.py`; mantenimiento lo reexporta.
2. **Sacar del inventario lo que es monitoreo.**
   - Los equipos de red que se vigilan por ping o SNMP pasan a un modelo propio en `monitoreo`, con IP, tipo y farmacia.
   - El técnico asignado deja de ser un `Colaborador`.
   - Es una migración con traspaso de datos y se hace en su propia tanda.
3. **Cambiar la llamada directa por la API.** SaidSoft deja de importar `apps.mantenimiento`. El otro proyecto consulta `alertas/?desde_id=…` cada pocos minutos y abre el mantenimiento si la regla tiene `abre_mantenimiento`. Si hace falta inmediatez, se agrega un aviso desde SaidSoft, pero no antes.
4. **Crear el proyecto nuevo** con `activos`, `mantenimiento` y la app móvil:
   - exportar sus datos;
   - apuntar la app al nuevo servidor;
   - retirar las dos apps de SaidSoft, con migraciones que se cortan si encuentran filas sin exportar.

**Pendiente de decidir:** si el ping y el SNMP de impresoras y routers se quedan en SaidSoft (recomendado, porque es monitoreo) o se van con el inventario.

---

## 11. Despliegues del POS: riesgos pendientes antes del despliegue masivo

Despliegues entra al núcleo **sin cambios en el agente ni en MQTT**. Está validado en una sola estación (ML016-A). Antes de publicar una versión a muchas farmacias hay que corregir estos defectos del agente (hallazgo C-13 de `docs/auditoria/gestion-remota-software.md`). Corregirlos implica una versión nueva del agente:

| # | Defecto | Dónde | Consecuencia |
|---|---|---|---|
| 1 | El POS se detiene con `taskkill /F` | `agente_prueba.py`, `_detener_pos` | Si hay una venta en curso, se interrumpe sin cierre ordenado |
| 2 | El rollback copia el respaldo encima de la carpeta y no borra lo que el paquete agregó | `_restaurar_carpeta` (`copytree(..., dirs_exist_ok=True)`) | Después de un rollback pueden quedar archivos de la versión fallida junto a los de la anterior |
| 3 | La extracción de un `.zip` con carpeta raíz copia cada entrada sin validar que quede dentro de la carpeta del POS | `_extraer_paquete`, rama del prefijo | Una entrada con `..` podría escribir fuera de la carpeta. Lo mitigan la verificación SHA-256 y la firma HMAC del paquete, que solo emite el servidor |
| 4 | Pausar solo cambia el estado en el servidor | `despliegues/services.py`, `pausar_despliegue` | Las estaciones que ya recibieron el mensaje retenido siguen instalando |

**Mientras tanto:**
- usar el modo "Aplicar cuando el POS se cierre". Evita el cierre forzado en horario de venta, pero no reemplaza la corrección 1;
- avanzar por anillos: una estación piloto, unas pocas farmacias, un grupo y por último la cadena, promoviendo solo con el anillo anterior en "Confirmado OK";
- dejar el freno por porcentaje de error activo;
- no contar con la pausa para detener estaciones que ya recibieron el paquete.

---

## Anexo — Mecanismo del primer corte

- `apps/panel/modulos.py`:
  - `MODULOS_DIFERIBLES`;
  - `diferido(modulo, vista)`, que devuelve 404 si el módulo está diferido;
  - un *context processor* que expone `modulos_diferidos` a las plantillas.
- `config/settings/base.py`: `PANEL_MODULOS_DIFERIDOS` vacío (desarrollo y CI).
- `config/settings/produccion.py`: la lista de los 6 módulos, sobrescribible por variable de entorno sin tocar código.
- `apps/panel/urls.py`: 39 rutas envueltas; los nombres no cambian.
- Plantillas:
  - **menú** (`base.html`);
  - **botón NetFlow** en Enlaces;
  - **"Instalar agente ahora (vía Scripts RMM)"** en la ficha de la estación. El texto que explica cómo instalar MeshCentral a mano sigue visible.
- Despliegues no es diferible: sus 10 rutas, el bloque "Despliegues en curso" del tablero y el CSV de despliegues en Reportes quedan como estaban antes del corte. Si el `.env` del servidor todavía nombra `despliegues` en `PANEL_MODULOS_DIFERIDOS`, no tiene efecto.
- Pruebas: `apps/panel/tests_alcance_rmm.py`.

**Por qué no permisos:** el rol Administrador los tiene todos (seguiría viendo los módulos), y quitarlos de los roles es un cambio de datos en producción.
