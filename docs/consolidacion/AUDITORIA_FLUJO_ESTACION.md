# Auditoría del flujo de una estación

**Fecha:** 20-oct-2026 · **Commit:** `68d0045` · **Modo:** solo lectura. No se cambió ningún
protocolo ni flujo.

Marcas: **[CÓDIGO]** · **[PROD fecha]** · **[DOC]** · **HIPÓTESIS** · **NO VERIFICADO**.

El recorrido es: identificación → enrolamiento y aprobación → credenciales → latido → estado →
monitoreo → alertas → mantenimiento → comandos → software y despliegues → confirmaciones.

---

## 1. Identificación y asociación con la organización [CÓDIGO]

- **Jerarquía:** `UnidadNegocio` → `Grupo` → `Farmacia` → `Estacion` (`apps/catalogo/models.py`).
  La unidad de negocio es el cliente (SG, MIA, 7DIAS) y define qué ve cada usuario.
- **El código de la estación es la identidad pública:** formato `FARMACIA-SUFIJO`
  (`codigo_estacion_validator`), único. La farmacia se deduce del prefijo
  (`mqtt_worker.services._farmacia_desde_codigo_estacion`).
- **La identidad de hardware es `hardware_id`**, enviado por el agente. Se fija la primera vez
  (TOFU) y protege el re-enrolamiento (§2).
- **Dueño del dato:** `catalogo`.
  - **Lo modifican:** el worker (latido, info de equipo, enrolamiento), el panel (aprobación,
    edición, pausa), el admin y los comandos de importación.

## 2. Enrolamiento y aprobación [CÓDIGO]

`mqtt_worker.services.manejar_enrolamiento`, en `/saidsof/enrolamiento/solicitar/`:

| Caso | Resultado |
|---|---|
| Estación existente y `hardware_id` distinto | Se rechaza y queda en la bandeja `EnrolamientoRechazado` (posible suplantación) |
| Estación existente y RECHAZADA en el panel | Se rechaza |
| Estación existente sin `hardware_id` y con latidos previos | Se rechaza: hay que cargar el `hardware_id` en el admin |
| Estación existente, válida | Se acepta. **No rota la credencial si tiene latido fresco** (protección `B-2`) |
| Altas deshabilitadas (`ConfiguracionMonitoreo.enrolamiento_habilitado`) | Se rechaza (freno de emergencia) |
| Nueva con token de apertura válido | Entra **aprobada** (`aperturas.consumir_token`) |
| Nueva y la farmacia no existe | Se rechaza y queda en la bandeja |
| Nueva y la farmacia existe | Se crea **pendiente de aprobación** |

La aprobación es manual, en el panel (`estaciones_aprobacion`), y exige el permiso `aprobar_estacion`.

**Respuesta** (`_respuesta_aceptado`), publicada por el worker en
`/saidsof/enrolamiento/respuesta/{cod}/`, **sin retener**:
- token de enrolamiento;
- `hmac_secret` propio;
- credencial MQTT propia, aprovisionada por la API de EMQX con ACL `all` sobre
  `/saidsof/agente/{cod}/#`.

**Riesgo abierto [PROD 19-oct, DOC]:** la credencial compartida `saidsof_agente` todavía puede
suscribirse a `/saidsof/enrolamiento/respuesta/+/`. Quien la tenga puede **leer** la respuesta de
enrolamiento de cualquier estación, que lleva token, secreto HMAC y contraseña (P0-2 de
`verificacion-seguridad-p0.md`).

## 3. Latido [CÓDIGO]

`manejar_heartbeat` (`apps/mqtt_worker/services.py:382`), una vez por minuto por estación [DOC]:

1. Busca la estación por `codigo` **y** `token_enrolamiento` del payload. Si no coincide: log y descarte.
2. Si no está APROBADA, la ignora.
3. Si estaba fuera de línea, resuelve las alertas `sin_heartbeat` y `agente_caido_red_viva`.
4. Actualiza versiones, Windows, POS, reloj y desfase calculado por el servidor, IP y MAC
   validadas, autocorrecciones de reloj y estado de pausa declarado. Luego `estado_conexion =
   ONLINE` y `ultimo_heartbeat = now`.
5. **`estacion.save()` sin `update_fields`.**
6. Si cambió la versión del agente, encola el borrado de la actualización retenida.
7. `facturacion.registrar_actividad_mensual`, `registrar_estado_dispositivo` (fuente MQTT) y las
   reglas de reloj.

**Costo:** unas 11 consultas por latido [DOC, medido el 5-oct en `capacidad-2030.md`].

## 4. Estado fuera de línea [CÓDIGO]

- **Detección:** `marcar_estaciones_offline_task` (Beat, cada 60 s) marca OFFLINE a las estaciones
  sin latido por más de **5 minutos** (`ALERTA_SIN_LATIDO_UMBRAL_MINUTOS`) y abre alertas
  `sin_heartbeat` (`evaluar_reglas_sin_heartbeat`).
- Las pantallas usan otro umbral, **10 minutos** (`ONLINE_UMBRAL_MINUTOS`), a propósito.
- **Cruce con MeshCentral:** `evaluar_cruce_monitoreo` (cada 420 s) abre `agente_caido_red_viva`
  cuando MeshCentral ve el equipo vivo y el agente no reporta.

## 5. Monitoreo

| Fuente | Entrada | Dónde queda |
|---|---|---|
| Métricas (solo estaciones con `monitorear_recursos`) | `metricas/` | `MuestraMetrica` (hypertable, 30 días) |
| Servicios del POS | `servicios_pos/`, según el catálogo retenido | `EstadoServicioPos`, `MuestraServicioPos` |
| Eventos de Windows | `eventos_sistema/`, según el catálogo retenido | `EventoSistemaDetectado` |
| Errores del POS | `pos_errores/` | `PosErrorDetectado` |
| Enlace de la farmacia | Ping desde el servidor cada 120 s | `EstadoEnlaceFarmacia`, `EventoEnlaceFarmacia` |
| Router (ancho de banda, identidad) | SNMP desde el servidor, o vía agente | `MuestraRedFarmacia`, `EquipoBordeFarmacia` |
| Equipos sin agente | Ping vía agente cada 900 s | `EstadoRedActivo` (sobre `activos.Activo`) |

Los catálogos de servicios y de eventos se publicaron firmados y retenidos el 20-oct
[PROD 20-oct]. **Que las estaciones los hayan aplicado: NO VERIFICADO todavía.**

## 6. Alertas [CÓDIGO]

`monitoreo.services.abrir_o_mantener_alerta`:

1. Si la estación está en una `VentanaMantenimiento` activa, no abre nada.
2. Si ya hay una alerta activa para esa regla y estación, no abre otra.
3. Crea la `Alerta` y encola `notificar_alerta`.
4. Si es crítica, encola el diagnóstico con IA. **No funciona en producción:** sin API key.
5. Si la regla tiene `abre_mantenimiento`, llama a `mantenimiento.abrir_mantenimiento_desde_alerta`.

Canales de aviso:
- **Telegram:** funciona [DOC].
- **Correo:** no funciona, `EMAIL_HOST_USER` vacía [PROD 28-sep].
- **Teams:** NO VERIFICADO.

**Escalamiento:** `escalar_alertas` cada 600 s.

**Las reglas** son por unidad de negocio, y la condición tiene que sostenerse durante
`duracion_minutos` (`_condicion_sostenida`).

## 7. Mantenimiento [CÓDIGO]

- **Desde una alerta:** con `abre_mantenimiento`, se crea un `Mantenimiento` y la alerta queda
  enlazada (`Alerta.mantenimiento`).
- **Programado:** `generar_mantenimientos_programados` (06:15) y aviso de vencimiento (07:00).
- **En campo:** la app móvil por `/api/v1/mantenimientos/` y `/visitas/`. El permiso es
  `PermisoDeclarado` (`apps/cuentas/api_permissions.py`) y el alcance, `tecnico=request.user`.
- **Uso real:** 3 mantenimientos y 1 visita [PROD 28-sep].

## 8. Comandos remotos [CÓDIGO]

- **Envío:** `catalogo.services.enviar_comando` / `enviar_script` →
  `/saidsof/agente/{cod}/comando/`. **No retenidos** (una estación apagada no los recibe, a
  propósito), firmados con HMAC por estación y con una **ventana de 120 s**
  (`VENTANA_TIMESTAMP_SEGUNDOS`), que es la defensa contra la repetición.
- **Comandos de consulta** (`consultar_info`, `consultar_perifericos`,
  `escanear_actualizaciones`, …): la respuesta llega por su propio tópico. **No hay un estado
  "comando enviado → respondido"** que el panel pueda seguir: si no llega respuesta, no hay aviso.
- **Scripts:**
  - estados `PENDIENTE_APROBACION → PENDIENTE → EN_PROGRESO → COMPLETADO / CON_ERRORES`;
  - por estación, `PENDIENTE → ENVIADO → EJECUTANDO → COMPLETADO / ERROR / TIMEOUT`;
  - `aprobar_ejecucion_script` aplica los cuatro ojos;
  - `caducar_resultados_vencidos` (cada 600 s) cierra los que quedaron sin respuesta.
- **Pausa:** retenida, con estado confirmado por el latido (`pausado`, `pausa_confirmada_en`).

## 9. Software y despliegues del POS [CÓDIGO]

| Paso | Software | Despliegue del POS |
|---|---|---|
| Creación | `SolicitudInstalacion` | `Despliegue` con aprobación de cuatro ojos (`aprobar_despliegue`) |
| Publicación | Retenida por estación, `publish.multiple` (MQTT 3.1.1, QoS 0) | Igual; también reintento y envío a una estación |
| Resultado por estación | `PENDIENTE → DESCARGANDO → … → INSTALADO / ERROR` | `PENDIENTE → … → APLICADO / ERROR / ROLLBACK` |
| Duplicados en el agente | `_ya_procesado(id, timestamp)` en `identidad.json`, últimos 200 | Igual |
| Freno | No | `evaluar_freno_automatico` por % de error |
| Retiro del retenido | Al confirmar la recepción, si no queda otra pendiente (`688e104`) | Igual |
| Caducidad | **No existe**: una solicitud puede quedar en `publicando` para siempre (C-11) [DOC] | Igual |

## 10. Confirmaciones, reintentos y errores

| Aspecto | Comportamiento | Evidencia |
|---|---|---|
| Del agente al servidor | **QoS 0, sin reintento.** Si no hay conexión, el reporte se pierde y solo queda una línea en el log local | [CÓDIGO] `agente_prueba.py:949` |
| Del servidor al agente (caminos nuevos) | MQTT 5 + QoS 1; el PUBACK detecta un rechazo de EMQX | [CÓDIGO] `_publicar_mqtt` |
| Del servidor al agente (software, despliegue) | MQTT 3.1.1, QoS 0: un rechazo pasa como éxito | [CÓDIGO] |
| Error al procesar en el worker | Se guarda en `MensajeMqttFallido` y el panel lo muestra como advertencia | [CÓDIGO] `run_mqtt_worker.py` |
| Mensaje que no es JSON | Igual | [CÓDIGO] |
| Duplicados del agente al servidor | Latido idempotente. Para los pasos de software y despliegue, se crea un evento por cada mensaje recibido | [CÓDIGO] |
| Duplicados del servidor al agente | `solicitud_id`/`despliegue_id` + timestamp en `identidad.json`; comandos por ventana de 120 s | [CÓDIGO] |
| Persistencia de órdenes | Retenidas en disco desde el 20-oct | [PROD 20-oct] |
| Confirmación de entrega de una orden | Un **reporte** de la estación (`recibido` o un final), no un ACK de MQTT | [CÓDIGO] |

## 11. Posibles condiciones de carrera

Todas son **HIPÓTESIS** con base en el código; **ninguna está reproducida**.

| # | Situación | Evidencia | Efecto posible |
|---|---|---|---|
| CR-1 | El latido hace `estacion.save()` completo; un cambio del panel en el mismo instante (aprobación, `monitorear_recursos`, pedido de pausa) se puede perder | `manejar_heartbeat`, `manejar_info_equipo` sin `update_fields` | Una edición del panel revertida en silencio |
| CR-2 | Dos alertas activas para la misma regla y estación | `_alerta_activa` + `create` sin restricción única en `Alerta` | Avisos duplicados. Bajo: el worker es de un hilo, pero Celery evalúa "sin latido" en paralelo |
| CR-3 | `reintentar_despliegue` vuelve los resultados a PENDIENTE **después** de publicar | `despliegues/services.py` | Un `recibido` que llega antes queda pisado; el retenido se conserva más tiempo, sin otro daño |
| CR-4 | Una orden publicada entre el reporte final y la tarea de retiro | `retirar_retenido_*` | Se borra el retenido de una orden que la estación ya recibió en vivo. Riesgo residual mínimo, documentado en `688e104` |

## 12. Puntos únicos de falla del flujo

1. **Worker MQTT:** si se cae, no entra nada. El panel lo detecta por su latido (`mqtt_worker`).
2. **EMQX:** nodo único con techo de 1024 conexiones.
3. **Celery Beat:** sin él no se marca ninguna estación como fuera de línea ni se abren alertas
   `sin_heartbeat`. Tiene latido propio.
4. **`token_enrolamiento`:** es lo único que autentica el payload (además de la credencial MQTT).
   Si `identidad.json` se pierde, hay que re-enrolar.
