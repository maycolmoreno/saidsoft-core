# Matriz de consolidación de SaidSoft

**Fecha:** 20-oct-2026 · **Commit:** `68d0045` · **Modo:** solo lectura. **No se eliminó nada.**

Marcas: **[CÓDIGO]** · **[PROD fecha]** · **[DOC]** · **HIPÓTESIS** · **NO VERIFICADO**.

**Recomendaciones posibles:**
1. CONSERVAR
2. CONSOLIDAR
3. REFACTORIZAR MÁS ADELANTE
4. INVESTIGAR
5. CANDIDATO A RETIRADA
6. RETIRADA APROBADA: no se usa en ninguna fila, porque no hay autorización.

**Relación con lo anterior:** actualiza la clasificación de `docs/producto/SAIDSOFT_RMM_1_0_ALCANCE.md`
(qué se ocultó en producción) y de `docs/auditoria/producto-v1.md`. Las diferencias se señalan
en cada fila.

**No hay señales de Django** (`@receiver`, `post_save.connect`) en ninguna app [CÓDIGO]. Todas las
dependencias son imports o claves foráneas explícitas, y están medidas en `MAPA_DEPENDENCIAS.md`.

---

## 1. Aplicaciones Django

| App | Responsabilidad comprobada | Modelos | Entran | Salen | Rutas, tareas y config | MQTT y externos | Uso real | Riesgo de tocarla | Recomendación |
|---|---|---|---|---|---|---|---|---|---|
| `catalogo` | Identidad de la red, enrolamiento, comandos firmados, publicación MQTT | 8 | 14 apps | monitoreo, cuentas, auditoria, mqtt_worker, activos | Rutas de estaciones, agente, instalador y API RMM; `marcar_estaciones_offline`; 18 comandos | Publica comandos, pausa y actualización; firma HMAC | **Alto** [PROD 19-oct] | Máximo: es la raíz | **1. CONSERVAR** |
| `mqtt_worker` | Único consumidor de la flota (15 tópicos); credenciales EMQX | 3 | catalogo, monitoreo, panel | monitoreo, catalogo, software, despliegues, scripts, facturacion, aperturas | Servicio `worker`; `revisar_credenciales_mqtt` | Suscriptor de todo; API de EMQX | **Alto** [PROD 20-oct] | Máximo | **1. CONSERVAR**; **3. REFACTORIZAR MÁS ADELANTE** el procesamiento en un hilo para crecer |
| `monitoreo` | Alertas, enlaces, salud, servicios del POS, eventos, SNMP, bot, MeshCentral, NetFlow, IA | 29 | catalogo, mqtt_worker, panel, viaticos, whatsapp | catalogo, cuentas, mqtt_worker, activos, scripts, whatsapp, mantenimiento | 18 de las 28 tareas periódicas | Telegram, Teams, SMTP, SNMP, MeshCentral, Anthropic | **Alto** en alertas y enlaces | Alto | **1. CONSERVAR** el núcleo · NetFlow **4. INVESTIGAR** (piloto) · IA **4. INVESTIGAR** (nunca se ejecutó) |
| `software` | Catálogo, instalaciones e inventario de software | 7 | aperturas, mqtt_worker, panel | catalogo, cuentas, auditoria | 10 rutas; escaneos programados | Publica retenido por estación | **Real**: Firefox y LibreOffice, 63 resultados [PROD 19-oct] | Medio | **1. CONSERVAR**; **2. CONSOLIDAR** la publicación en `_publicar_mqtt` (MQTT 5) |
| `despliegues` | Actualización del POS Farmamia/Elipsys | 3 | aperturas, mqtt_worker, panel | catalogo, auditoria, cuentas | 10 rutas; núcleo desde `e2b299b` | Publica retenido; freno | **Piloto**: una estación, ML016-A [DOC] | Alto: toca el POS en producción | **1. CONSERVAR**; **3. REFACTORIZAR MÁS ADELANTE** los 4 defectos del agente y la publicación MQTT 5 |
| `cuentas` | Perfil, alcance por unidad de negocio, MFA, permiso de la API | 1 | 12 apps | catalogo | Context processor de unidad de negocio | Push FCM (sin registrar) | Alto | Máximo: seguridad | **1. CONSERVAR** |
| `auditoria` | Registro de acciones humanas | 1 | 8 apps | — | Rutas de bitácora y CSV | — | Alto | Alto: trazabilidad | **1. CONSERVAR** |
| `panel` | Presentación (HTMX); sin modelos | 0 | ninguna | las 15 | 176 rutas; 4 context processors propios | — | Alto | — | **1. CONSERVAR** |
| `scripts` | Biblioteca y ejecución de PowerShell/BAT con aprobación; parcheo de terceros con `winget` | 4 | aperturas, monitoreo, mqtt_worker, panel | catalogo, cuentas | 11 rutas **ocultas en producción**; programados (06:00); caducidad (600 s) | Por `/comando/`, firmado | Bot "sincronizar hora", `cambiar_nodo_pos` [CÓDIGO]; uso por pantallas **NO VERIFICADO** | Alto: ejecución remota en toda la flota | **1. CONSERVAR** el servicio · **4. INVESTIGAR** si se vuelve a mostrar solo para parcheo con `winget` y diagnóstico |
| `activos` | Inventario físico (ITAM) | 15 | 7 apps | catalogo, cuentas | 12 rutas + compras y bodegas | — | 22 activos y 10 colaboradores [PROD 5-oct]; la organización **no usa** GLPI ni otra herramienta de inventario (20-oct) | Alto: claves foráneas desde el núcleo | **2. CONSOLIDAR** en el proyecto separado (alcance §10), que sigue siendo la opción: no hay herramienta externa con la que integrarse. Ping y SNMP de equipos se quedan en monitoreo |
| `mantenimiento` | Servicio en campo y API de la app móvil | 18 | monitoreo, panel, viaticos | cuentas, activos, catalogo, auditoria | 17 rutas, 39 de API, 4 tareas | Push, app Flutter | 3 mantenimientos y 1 visita [PROD 28-sep]; **la app móvil todavía no se usa** (respuesta del responsable, 20-oct) | Medio: `Alerta.mantenimiento` y la bandeja de notificaciones | **2. CONSOLIDAR** en el proyecto separado de inventario y mantenimiento (alcance §10); sin usuarios de la app que proteger durante el traslado |
| `aperturas` | Alta cero-touch con plantilla | 7 | mqtt_worker, panel | catalogo, software, scripts, despliegues, auditoria, activos | 10 rutas ocultas | `consumir_token` en el enrolamiento | 0 aperturas [DOC] | Medio: está en el camino de enrolamiento | **2. CONSOLIDAR**: llevar `consumir_token` al núcleo · el resto **5. CANDIDATO A RETIRADA** |
| `cumplimiento` | Campañas de cumplimiento | 4 | panel | catalogo, activos, cuentas, auditoria | 4 rutas ocultas | — | Sin uso [DOC] | Bajo, salvo el permiso de `reporte_cumplimiento_csv` | **5. CANDIDATO A RETIRADA** |
| `viaticos` | Gastos de campo | 3 | panel | mantenimiento, cuentas, catalogo, activos, monitoreo | 11 rutas ocultas; comando de escenarios | — | 0 zonas [PROD 28-sep] | Bajo | **5. CANDIDATO A RETIRADA** |
| `whatsapp` | Webhook de Meta, mesa, chatbot | 3 | monitoreo, panel | catalogo, monitoreo, cuentas | Webhook público; `evaluar_whatsapp` cada 300 s | WhatsApp Cloud API | Chatbot apagado [DOC] | Medio: `Novedad` la usan el bot y el historial | **2. CONSOLIDAR** lo que usa monitoreo · después **5. CANDIDATO A RETIRADA** |
| `facturacion` | Actividad mensual por estación | 1 | mqtt_worker, panel | catalogo, cuentas | 2 reportes | Escritura en cada latido | **No se usa**: no hay cobro entre unidades (respuesta del responsable, 20-oct) | Bajo: hay que quitar su escritura del latido y dos reportes | **5. CANDIDATO A RETIRADA** |

---

## 2. Componentes que no son apps

| Componente | Evidencia | Recomendación |
|---|---|---|
| Tópicos `software/` y `despliegue/` `global`, `grupo` y `farmacia` | El servidor ya no publica ahí; el agente se suscribe (`agente_prueba.py:683-696`) y la ACL los permite (`emqx_admin.py:125-148`) [CÓDIGO] | **5. CANDIDATO A RETIRADA**. Exige una versión del agente y ajustar la ACL; mientras tanto no estorba |
| `ActividadPlanificada` | 0 filas [PROD 28-sep]; la usan admin, form, services y `panel/views/actividades.py` [CÓDIGO]; `docs/tres-agendas.md` | **5. CANDIDATO A RETIRADA** |
| `deploy/sonda-enlaces/` + `/api/v1/monitoreo/enlaces/sondeo/` | El servidor sondea directo desde el 11-sep [DOC: `CLAUDE.md`] | **5. CANDIDATO A RETIRADA**, después de confirmar que nadie corre la sonda |
| `probar_apertura.py` (raíz, en git) | Script de demostración [CÓDIGO] | **5. CANDIDATO A RETIRADA** |
| Diagnóstico con IA (`diagnostico_ia.py`, dependencia `anthropic`) | Sin API key en producción [PROD 28-sep] | **4. INVESTIGAR** |
| NetFlow (contenedor, comando y pantalla oculta) | Piloto del 18-oct [DOC] | **4. INVESTIGAR** |
| MeshCentral | Produce `agente_caido_red_viva`; imagen `:latest` [CÓDIGO] | **1. CONSERVAR**, fijando la versión de la imagen |
| Webhook de WhatsApp | Público; protegido por la firma del cuerpo [CÓDIGO] | **5. CANDIDATO A RETIRADA** si el chatbot no se usa |
| Admin de Django (`/admin/`) | Sigue mostrando los modelos de los módulos ocultos [CÓDIGO] | **2. CONSOLIDAR**: ocultar esos modelos |

---

## 3. Duplicaciones verificadas

| # | Duplicación | Evidencia | Recomendación |
|---|---|---|---|
| D-1 | **Dos caminos de publicación MQTT.** `_publicar_mqtt` (MQTT 5, detecta rechazos) y cuatro bloques con `paho.publish.multiple`/`single` que repiten la misma armadura de usuario y TLS (MQTT 3.1.1, no detecta rechazos) | `catalogo/services.py`; `software/services.py:publicar_solicitud`; `despliegues/services.py:publicar_despliegue`, `reintentar_despliegue`, `publicar_despliegue_a_estacion` [CÓDIGO] | **2. CONSOLIDAR** en una función que publique varios mensajes por MQTT 5 en una conexión. Riesgo bajo, alto valor: cierra la última falla silenciosa del lado del servidor |
| D-2 | **Distribución de órdenes por tópico agregado y por estación** | §2 de este documento | Ya consolidado del lado del servidor; falta el agente y la ACL |
| D-3 | **Tres agendas:** `MantenimientoProgramado`, `ActividadPlanificada`, `VisitaTecnica` | `docs/tres-agendas.md` [DOC] | `VisitaTecnica` **no** es un duplicado: es el nivel superior y la usa la app. Solo `ActividadPlanificada` (0 filas) |
| D-4 | **Dos sondas de enlaces:** servidor y sonda externa por API | §2 | Candidato a retirada |
| D-5 | **Mesa de ayuda por WhatsApp y Odoo Helpdesk** | Duplicación funcional con una herramienta externa [DOC: producto-v1] | Retirar la mesa propia e integrar Odoo si hace falta (ver el roadmap) |

**No son duplicaciones**, aunque lo parezcan:
- **`Estacion.estado_conexion` y `EstadoDispositivo`:** son dos fuentes deliberadas, MQTT y
  MeshCentral, y su cruce produce una alerta.
- **`Activo` y `DispositivoDetectado`:** lo declarado contra lo observado (`CLAUDE.md`).

---

## 4. Antes de retirar algo: qué podría romperse

| Candidato | Referencias a cortar antes | Qué se rompe si no |
|---|---|---|
| `viaticos` | `panel/views/viaticos.py`, plantillas, menú (ya oculto), `seed_permisos` | Imports del panel al arrancar; migraciones que borran tablas: exportar antes |
| `cumplimiento` | Permiso `cumplimiento.view_actividadcumplimiento` en `reporte_cumplimiento_csv` (`panel/views/reportes.py`), vistas del panel, FK a `activos.Cargo` | Quien hoy ve ese reporte pierde el acceso |
| `aperturas` | `mqtt_worker.services.manejar_enrolamiento` → `consumir_token` y `enrolar_estacion_de_apertura` | **El enrolamiento con token de apertura deja de funcionar** |
| `whatsapp` | `monitoreo/telegram_bot.py` y `historial_enlaces.py` (`Novedad`); Beat `evaluar-whatsapp`; webhook en `config/urls.py` | Respuestas del bot y la exclusión de cortes de luz en el SLA de enlaces |
| `ActividadPlanificada` | Admin, form, services, `panel/views/actividades.py` y su menú | La pantalla de actividades |
| Tópicos agregados | Suscripciones del agente y ACL | Nada en el servidor; un agente que los espera no se rompe, solo deja de recibir lo que ya no se publica |

La regla del alcance (§8) sigue vigente: retirar código y tablas solo cuando el módulo pasó una
versión entera oculto sin reclamos, sus dependencias compartidas ya se separaron y sus datos se
exportaron.
