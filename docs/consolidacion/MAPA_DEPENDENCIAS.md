# Mapa de dependencias de SaidSoft

**Fecha:** 20-oct-2026 · **Commit:** `68d0045` · **Modo:** solo lectura.

Marcas: **[CÓDIGO]** verificado en el código · **[PROD fecha]** medido en producción ·
**[DOC]** afirmado por la documentación · **HIPÓTESIS** · **NO VERIFICADO**.

Complementa a `docs/auditoria-arquitectura.md` (acoplamiento y consultas pesadas). Este documento
es el grafo actual, medido.

---

## 1. Importaciones entre apps

Contadas en código de producción: sin pruebas ni migraciones, e incluyendo los imports diferidos
dentro de funciones [CÓDIGO].

| App | Importa a (cantidad de imports) | La importan |
|---|---|---|
| `catalogo` | monitoreo 11, cuentas 3, auditoria 2, mqtt_worker 2, activos 2 | **14 apps** |
| `cuentas` | catalogo 1 | 12 apps |
| `auditoria` | — | 8 apps |
| `activos` | catalogo 11, cuentas 9 | aperturas, catalogo, cumplimiento, mantenimiento, monitoreo, panel, viaticos |
| `monitoreo` | catalogo 53, cuentas 12, mqtt_worker 9, activos 6, scripts 3, whatsapp 2, mantenimiento 2, auditoria 1 | catalogo, mqtt_worker, panel, viaticos, whatsapp |
| `mqtt_worker` | monitoreo 15, catalogo 9, software 3, facturacion 2, despliegues 2, scripts 2, aperturas 1 | catalogo, monitoreo, panel |
| `mantenimiento` | cuentas 10, activos 8, catalogo 3, auditoria 2 | monitoreo, panel, viaticos |
| `scripts` | catalogo 8, cuentas 4 | aperturas, monitoreo, mqtt_worker, panel |
| `software` | catalogo 9, cuentas 4, auditoria 2 | aperturas, mqtt_worker, panel |
| `despliegues` | catalogo 5, auditoria 1, cuentas 1 | aperturas, mqtt_worker, panel |
| `aperturas` | catalogo 5, software 5, scripts 4, despliegues 2, auditoria 1, activos 1 | mqtt_worker, panel |
| `whatsapp` | monitoreo 7, catalogo 3, cuentas 1 | monitoreo, panel |
| `facturacion` | catalogo 2, cuentas 1 | mqtt_worker, panel |
| `viaticos` | mantenimiento 3, cuentas 3, catalogo 2, activos 1, monitoreo 1 | panel |
| `cumplimiento` | catalogo 3, activos 2, cuentas 2, auditoria 1 | panel |
| `panel` | las 15 | ninguna |

### Ciclos [CÓDIGO]

Ninguno rompe el arranque, porque las importaciones de un lado son diferidas, dentro de funciones:

| Ciclo | Por qué existe |
|---|---|
| `catalogo` ↔ `monitoreo` | `catalogo.services` dispara reglas de alerta y estado de dispositivo (`marcar_estaciones_offline` → `evaluar_reglas_sin_heartbeat`) |
| `catalogo` ↔ `mqtt_worker` | Credenciales por estación (`emqx_admin`) y latidos de procesos |
| `catalogo` ↔ `activos` | `Farmacia.tecnico_asignado` → `activos.Colaborador`; `Activo` → `Estacion`/`Farmacia` |
| `catalogo` ↔ `cuentas` | `PerfilUsuario.colaborador` → `activos`; el alcance por unidad de negocio |
| `monitoreo` ↔ `mqtt_worker` | El worker llama a los servicios de monitoreo; la salud lee los latidos del worker |
| `monitoreo` ↔ `whatsapp` | El bot de Telegram y el historial de enlaces leen `Novedad` |

**Lectura:** `catalogo` y `monitoreo` forman un único núcleo de hecho. Separarlos no aporta nada
hoy; lo que sí conviene es no sumar ciclos nuevos.

---

## 2. Claves foráneas entre apps [CÓDIGO]

Solo las que **cruzan** de app. Las que van a `auth.User` se omiten.

| Desde | Hacia | Relevancia |
|---|---|---|
| `catalogo.Farmacia.tecnico_asignado` | `activos.Colaborador` | El núcleo depende del inventario |
| `cuentas.PerfilUsuario.colaborador` | `activos.Colaborador` | Igual |
| `monitoreo.Alerta.mantenimiento` | `mantenimiento.Mantenimiento` | El núcleo depende de mantenimiento |
| `monitoreo.EstadoRedActivo.activo`, `ObjetivoSnmp.activo` | `activos.Activo` | El monitoreo de impresoras y routers vive sobre el inventario |
| `whatsapp.Novedad.evento_enlace` | `monitoreo.EventoEnlaceFarmacia` | — |
| `aperturas.PasoPlantilla` / `PasoApertura` | `despliegues`, `scripts`, `software` | Aperturas orquesta los tres |
| `mantenimiento.*` | `activos.Activo`, `Colaborador`, `Bodega`, `TipoConsumible`, `CategoriaEquipo`, `Ubicacion` | Mantenimiento e inventario son un mismo bloque |
| `viaticos.*`, `cumplimiento.*` | `activos.Colaborador`, `activos.Cargo` | — |
| 13 apps | `catalogo.Estacion` / `Farmacia` / `Grupo` / `UnidadNegocio` | `catalogo` es la raíz de la jerarquía |

Las cuatro primeras filas son las que impiden separar `activos` y `mantenimiento` sin trabajo
previo (ver `docs/producto/SAIDSOFT_RMM_1_0_ALCANCE.md` §10).

---

## 3. MQTT: productores y consumidores

### Del agente al servidor [CÓDIGO]

El agente publica todo con **QoS 0** y sin `retain` (`agente_prueba.py:949`,
`self.client.publish(topico, json.dumps(payload))`). Si no hay conexión, el mensaje se descarta y
solo queda una línea en el log local. El worker se suscribe a tres de estos tópicos con QoS 1, pero
MQTT entrega con el menor de los dos niveles, así que **en la práctica todo llega con QoS 0**.

| Tópico | Contenido | Lo procesa | Autenticación |
|---|---|---|---|
| `/saidsof/enrolamiento/solicitar/` | Código, hardware_id, datos del equipo | `manejar_enrolamiento` | `hardware_id` + estado de aprobación; token de apertura opcional |
| `agente/{cod}/heartbeat/` | Versiones, Windows, POS, reloj, IP/MAC, pausa | `manejar_heartbeat` | `token_enrolamiento` en el payload + estación aprobada |
| `agente/{cod}/metricas/` | CPU, RAM, disco, latencia | `manejar_metricas` | Igual |
| `agente/{cod}/info_equipo/` | Hardware, BitLocker y su clave, plan de energía | `manejar_info_equipo` | Igual |
| `agente/{cod}/despliegue_estado/` | Paso del despliegue del POS | `manejar_estado_despliegue` | Igual |
| `agente/{cod}/software_estado/` | Paso de la instalación | `manejar_estado_instalacion` | Igual |
| `agente/{cod}/script_estado/` | Resultado del script | `manejar_estado_script` | Igual |
| `agente/{cod}/windows_update/` | Actualizaciones pendientes (solo consulta) | `manejar_windows_update` | Igual |
| `agente/{cod}/software_instalado/` | Inventario de software | `manejar_software_instalado` | Igual |
| `agente/{cod}/perifericos/` | Dispositivos USB | `manejar_perifericos` | Igual |
| `agente/{cod}/red_farmacia/` | Bytes del router (sondeo vía agente) | `manejar_red_farmacia` | Igual |
| `agente/{cod}/activos_farmacia/` | Ping a equipos sin agente | `manejar_activos_farmacia` | Igual |
| `agente/{cod}/servicios_pos/` | Estado de PostgreSQL local y central y de recargas | `manejar_servicios_pos` | Igual |
| `agente/{cod}/eventos_sistema/` | Eventos de Windows vigilados | `manejar_eventos_sistema` | Igual |
| `agente/{cod}/pos_errores/` | Errores del log del POS | `manejar_pos_errores` | Igual |

Además de autenticarse con usuario y contraseña propios en EMQX, cada estación tiene `all` sobre
`/saidsof/agente/{cod}/#` (`emqx_admin.py:113`). Solo puede publicar en sus propios tópicos.

### Del servidor al agente [CÓDIGO]

| Tópico | Retenido | Publica (credencial) | Firma | Retiro del retenido |
|---|---|---|---|---|
| `enrolamiento/respuesta/{cod}/` | No | `worker` (`run_mqtt_worker.py:237`) | — (viaja token, hmac_secret y contraseña MQTT) | — |
| `agente/{cod}/comando/` (comandos y scripts) | No | `web` / `celery_worker`, credencial del panel | HMAC por estación; ventana de 120 s | — |
| `agente/{cod}/actualizar_agente/` | **Sí** | Panel | HMAC; sin ventana | Tarea de Celery al reportar la versión (`b99fa53`) |
| `agente/{cod}/pausa/` | **Sí** | Panel | HMAC; sin ventana | Queda: es estado declarativo |
| `agente/{cod}/software/` | **Sí** | Panel (`publish.multiple`, MQTT 3.1.1, QoS 0) | HMAC | Tarea de Celery al confirmar la recepción (`688e104`) |
| `agente/{cod}/despliegue/` | **Sí** | Panel (`publish.multiple`/`single`, MQTT 3.1.1, QoS 0) | HMAC | Igual (`688e104`) |
| `catalogo/eventos_sistema/` | **Sí** | Panel (`_publicar_mqtt`, MQTT 5, QoS 1) | HMAC con el secreto de la flota | Queda: es estado declarativo |
| `catalogo/servicios_pos/` | **Sí** | Panel (MQTT 5, QoS 1) | HMAC | Queda |
| `software/global|grupo/|farmacia/`, `despliegue/global|grupo/|farmacia/` | — | **Nadie**: el servidor publica por estación desde el fan-out | — | **F, obsoletos con evidencia**: el agente sigue suscrito (`agente_prueba.py:683-696`) y la ACL los permite (`emqx_admin.py:125-148`) |

**Dos caminos de publicación conviven** [CÓDIGO]:
- **MQTT 5 con QoS 1**, que detecta un rechazo de EMQX: `catalogo._publicar_mqtt` (`b99fa53`).
- **MQTT 3.1.1 con QoS 0**, que no lo detecta: `paho.mqtt.publish.multiple`/`single` en
  `software.publicar_solicitud`, `despliegues.publicar_despliegue`, `reintentar_despliegue` y
  `publicar_despliegue_a_estacion`. Una ACL incompleta haría fallar esas publicaciones **sin aviso**.

### Persistencia

El retainer de EMQX guarda en **disco** desde el 20-oct-2026 (cambio por la API y
`EMQX_RETAINER__BACKEND__STORAGE_TYPE`, `688e104`). Hay 5 retenidos: 3 de `$SYS` y los 2 catálogos;
sobrevivieron a un reinicio completo del NUC [PROD 20-oct].

---

## 4. Celery: qué dispara qué [CÓDIGO]

| Origen | Tarea | Destino |
|---|---|---|
| Beat, cada 60 s | `marcar_estaciones_offline` | `catalogo` → `monitoreo.evaluar_reglas_sin_heartbeat` → alertas |
| Beat, cada 120 s | `sondear_enlaces_farmacias` | Ping desde el servidor → `EventoEnlaceFarmacia` |
| Beat, cada 300 s | `solicitar_sondeo_red_farmacias_via_agente` | Comando a un agente por farmacia → `red_farmacia` |
| Beat, cada 300 s y 3600 s, y diaria | `repartir_sondeo_snmp` | `sondear_lote_snmp`, con 25 sondeos simultáneos |
| Beat, cada 600 s | `escalar_alertas` | Notificación de escalamiento |
| Beat, cada 900 s | `sincronizar_meshcentral` | API de MeshCentral |
| Worker MQTT | `limpiar_actualizacion_pendiente`, `retirar_retenido_*` | MQTT 5 con la credencial del panel |
| `abrir_o_mantener_alerta` | `notificar_alerta`, `diagnosticar_alerta` | Telegram, Teams, correo; IA |
| Webhook de WhatsApp | `procesar_mensaje_whatsapp` | `whatsapp` |

---

## 5. Servicios externos [CÓDIGO]

| Servicio | Quién lo usa | Credencial | Estado |
|---|---|---|---|
| API REST de EMQX | `mqtt_worker.emqx_admin`: aprovisiona credencial y ACL por estación | `EMQX_API_KEY`/`SECRET`, sin vencimiento [PROD 19-oct] | **A** |
| MeshCentral (WebSocket) | `meshcentral_worker`, `sincronizar_meshcentral` | `MESHCENTRAL_API_*` | **A** [PROD 20-oct: latido del worker] |
| Telegram Bot API | `telegram_bot`, `notificar_alerta`, resumen diario | `TELEGRAM_BOT_TOKEN` | **A**: único canal de aviso que funciona [DOC] |
| Webhook de Teams | `_enviar_webhook_teams` | URL por `CanalNotificacion` | **NO VERIFICADO** |
| SMTP | `notificar_alerta` | `EMAIL_HOST_*` | **No funciona**: `EMAIL_HOST_USER` vacía [PROD 28-sep] |
| WhatsApp Cloud API (Meta) | `whatsapp` | `WHATSAPP_*` | **C**: chatbot apagado [DOC] |
| Anthropic | `diagnostico_ia` | `ANTHROPIC_API_KEY` | **E**: vacía, nunca se ejecutó [PROD 28-sep] |
| SNMP a los MikroTik | `monitoreo.mikrotik`, `monitoreo.snmp` | Comunidad cifrada (Fernet) | **A** en ancho de banda e identidad [DOC] |
| Odoo | — | — | **No existe.** Solo queda la opción de origen `odoo_helpdesk` en `Mantenimiento`; el conector se dio de baja sin haberse escrito (`catalogo/0033`) |
| Firebase (push) | `cuentas.services.enviar_push` | — | **C**: la app no está registrada en Firebase [DOC] |

---

## 6. Dependencias críticas y puntos únicos de falla

Medidos o documentados; el detalle de capacidad está en `docs/capacidad-2030.md`.

| Componente | Si falla | Evidencia |
|---|---|---|
| El NUC (un host, 12 contenedores, salida por WiFi `wlo1`) | Se cae todo | [PROD 28-sep], `capacidad-2030.md` §6 |
| Worker MQTT (un proceso, un hilo, `loop_forever`) | Deja de entrar información; el panel sigue funcionando | [CÓDIGO], `capacidad-2030.md` §5 |
| EMQX (nodo único, `max_conns` 1024, `ulimit -n` 1024) | Toda la flota; el techo bloquea pasar de ~1.000 estaciones | [PROD 19-oct]; **sigue sin ampliarse** en el compose |
| Celery Beat (instancia única) | Se detienen purgas, sondeos, escalamientos y la detección de "sin latido" | [CÓDIGO]; tiene latido propio (`latido_beat`) |
| Redis (`--save 60 1`) | Celery deja de funcionar | [CÓDIGO] |
| `deploy/.env` | Sin él no hay secretos para recuperar | Fuera de git; 22 variables sin documentar en la plantilla |
| Base de datos | Sin réplica | [CÓDIGO]; respaldo diario cifrado y local [PROD 20-oct] |
