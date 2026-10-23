# Propuesta: agente por estación con comunicación HTTPS

**Fecha:** 20-oct-2026 · **Base:** `master` en `c89310b`, agente `agente-prueba-0.36` ·
**Modo:** evaluación en solo lectura. No se modificó código, configuración ni el agente.

Marcas:
- **[VERIFICADO]**: leído en el código, con archivo y línea, o medido en producción con fecha.
- **[PROPUESTA]**: diseño de este documento; no existe.
- **[HIPÓTESIS]**: razonable, sin comprobar.
- **[ESTIMACIÓN]**: cálculo, no prueba de carga.

---

## 0. Conclusión anticipada

1. **El cuello de botella para llegar a 5.000 agentes no es MQTT.** El techo actual de 1024
   conexiones es una configuración de EMQX (`max_conns` y `ulimit`), no un límite del protocolo:
   un nodo de EMQX sostiene 5.000 conexiones si se configura. Por escala, cambiar de transporte
   **no es necesario**.
2. **Lo que sí falta, con cualquier transporte, es una cola de órdenes persistente en la base**,
   con identificador por orden, caducidad, acuse y resultado. Hoy ese papel lo cumplen:
   - los mensajes retenidos de EMQX;
   - cuatro modelos de resultado distintos;
   - comandos sin seguimiento.

   De ahí salen E-01, E-07 y el defecto de la pausa (`AUDITORIA_SEGURIDAD_ESTABILIDAD.md`).
3. **HTTPS sí aporta ventajas reales:**
   - el alta de estaciones deja de exponerse a la credencial compartida, porque la respuesta va
     solo a quien la pidió (cierra P0-2);
   - sin conexiones permanentes ni ACL ni API Keys de EMQX;
   - un solo puerto y protocolo para atravesar firewalls;
   - la lógica de órdenes queda explícita en la base.

   **Y cuesta:**
   - latencia de las órdenes, igual al intervalo de consulta;
   - más peticiones a Django y PostgreSQL;
   - reescribir el transporte del agente y migrar la flota.
4. **Recomendación:** construir primero la **cola persistente, independiente del transporte**, y
   entregarla por MQTT (fase 1 de la §10). Después agregar HTTPS como **segundo transporte** y
   decidir con datos del piloto si se retira EMQX.

---

## 1. Auditoría del agente actual [VERIFICADO]

| Aspecto | Cómo es hoy | Evidencia |
|---|---|---|
| Lenguaje y estructura | Python, **un solo archivo de 3.312 líneas** (`AgentePrueba`); empaquetado con PyInstaller | `agente-prueba/agente_prueba.py`, `agente_prueba.spec`, `build.ps1` |
| Ejecución | Servicio de Windows `SaidsoftAgente` (pywin32) como **LocalSystem**. Si se cae, el servicio se reinicia a los 30, 60 y 120 s | `servicio_windows.py:65`; `instalar-servicio.ps1:231` |
| Instalación | `instalar-servicio.ps1`: binario en `C:\Program Files\Saidsoft\Agente`, `config.json` al lado; datos en `C:\ProgramData\Saidsoft` | `instalar-servicio.ps1:95,213`; `servicio_windows.py:47` |
| Identidad del equipo | Código de estación configurado a mano + `hardware_id` (MachineGuid) + número de serie | `leer_machine_guid` (`:128`), `leer_numero_serie` (`:238`) |
| Autenticación con el servidor | 1) Credencial MQTT propia por estación (antes, una compartida); 2) `token_enrolamiento` dentro de cada payload; 3) `hmac_secret` propio para verificar las órdenes | `__init__` (`:400-416`), `_respuesta_aceptado` en el servidor |
| Persistencia local | `identidad.json`: token, `hmac_secret`, credencial MQTT, farmacia, grupo, órdenes procesadas, pausa. Escritura atómica; si está corrupto, se aparta y se re-enrola | `_cargar_identidad` / `_guardar_identidad` (`:427-466`) |
| Latido | Cada 60 s, **sin variación aleatoria**: la fase depende de cuándo arrancó el agente | `bucle_heartbeat` (`:1773`); `CAMPOS_CONFIG` (`:3205`) |
| Otros reportes periódicos | Métricas cada 300 s (solo servidores); log del POS, 300 s; servicios del POS, 300 s; eventos de Windows, 900 s; chequeo de reloj, 3600 s, este sí con fase aleatoria | `:1877`, `:1989`, `:1726`, `:1533`, `:1455` |
| Inventario | **Bajo pedido**, por comando: hardware, BitLocker, software, periféricos, Windows Update. No envía cambios solo | `_consultar_info_equipo`, `_listar_software_instalado`, `_listar_perifericos_usb` |
| Transporte | paho-mqtt, TLS, `keepalive=60`, `loop_forever(retry_first_connection=True)`, reconexión automática | `correr` (`:3160`) |
| Publicación | **QoS 0, sin reintento**: si no hay conexión, el mensaje se pierde y queda una línea en el log | `_publicar` (`:942`) |
| Tópicos suscritos | `comando/`, `actualizar_agente/`, `pausa/`, `software/`, `despliegue/` propios; los dos catálogos; respuesta de enrolamiento; los agregados `global/grupo/farmacia`, **obsoletos** | `_al_conectar` (`:663-696`) |
| Comandos | `reiniciar`, `consultar_info`, `consultar_perifericos`, `consultar_software_instalado`, `escanear_actualizaciones`, `consultar_activos_farmacia`, `consultar_red_farmacia`, `configurar_nodo_pos`, `ejecutar_script`, `actualizar_agente` | `_manejar_comando` (`:2080`) |
| Validación de órdenes | HMAC-SHA256 por estación (`firmar`, `:265`); ventana de **120 s** en comandos; sin ventana en las retenidas, que usan id + timestamp procesado como nonce | `_firma_valida` (`:471`), `_ya_procesado` (`:856`) |
| Duplicados | Marca la orden como procesada **antes de ejecutarla** y guarda las últimas 200 por tipo | `_marcar_procesado` (`:908`) |
| Confirmaciones | Por **pasos de estado** (`recibido`, `descargado`, …, `ok`/`error`) publicados con QoS 0 | `_reportar_instalacion`, `_reportar_despliegue`, `_reportar_script` |
| Descargas | `urllib`, 6 intentos con reanudación, timeout de lectura de 60 s; caché de farmacia opcional; **integridad por SHA-256** | `bajar_archivo` (`:327`), `_descargar` (`:2882`) |
| Actualización del POS | Retenida por estación; tres modos (inmediato, ventana, cierre del POS); respaldo y rollback | `_manejar_despliegue` (`:2918`) a `_rollback` (`:3128`) |
| Actualización del agente | Orden firmada con URL y SHA-256, descarga y un `.bat` que reemplaza el ejecutable y reinicia el servicio. **Sin firma de código (Authenticode)** | `_aplicar_actualizacion_agente` (`:2657`), `_generar_script_swap` (`:2725`) |
| Reloj | El servidor mide el desfase en el latido; el agente se corrige contra el servidor | `_corregir_reloj` (`:597`) |

**Debilidades del agente actual que esta propuesta debe cubrir:**

| # | Debilidad | Marca |
|---|---|---|
| A-1 | Los reportes van con QoS 0 y sin reintento: un corte de red pierde resultados | [VERIFICADO] |
| A-2 | Latido sin variación aleatoria: tras un corte de luz masivo, las cajas laten sincronizadas | [VERIFICADO] |
| A-3 | `identidad.json` en `C:\ProgramData\Saidsoft` y el instalador **no restringe sus permisos** (no hay `icacls`). Por defecto, `ProgramData` deja leer a los usuarios locales, así que **cualquier usuario de la caja podría leer el token, el secreto HMAC y la contraseña MQTT** | Ausencia de restricción [VERIFICADO]; permisos efectivos en una caja real [HIPÓTESIS] |
| A-4 | Los catálogos se aceptan sin firma (P0-1) | [VERIFICADO] |
| A-5 | Los 4 defectos del despliegue del POS | [VERIFICADO] |
| A-6 | El ejecutable del agente no tiene firma de código | [VERIFICADO] |

### Qué se reutiliza y qué depende de MQTT

| Pieza del agente | ¿Depende de MQTT? | Con HTTPS |
|---|---|---|
| Lectura del equipo (hardware, software, periféricos, POS, servicios, eventos, reloj) | No | **Se reutiliza sin cambios** |
| Ejecución: scripts, instalación, despliegue con rollback, actualización del agente | No | Se reutiliza; el despliegue necesita las correcciones de A-5 |
| Validación HMAC (`firmar`, `_firma_valida`) y duplicados (`_ya_procesado`) | No | **Se reutiliza** para firmar órdenes |
| Descargas (`bajar_archivo`) | No: ya es HTTP | Se reutiliza |
| `identidad.json` | Guarda la credencial MQTT | Se reutiliza, cambiando la credencial y sus permisos (A-3) |
| `_on_connect`, `_on_message`, suscripciones, `_publicar`, `loop_forever` | **Sí** | Se reemplazan por un ciclo de sincronización HTTPS |
| Enrolamiento por tópico | **Sí** | Pasa a una petición HTTPS |
| Recepción de catálogos retenidos | **Sí** | Pasa a una configuración con versión (ETag) |

Del lado del servidor **[VERIFICADO]**: los 15 manejadores de `apps/mqtt_worker/services.py`
reciben `(codigo_estacion, payload)` y no saben nada de MQTT. Una vista HTTPS puede llamarlos
igual que hoy lo hace `run_mqtt_worker`. **Es la pieza de mayor reutilización.**

---

## 2. Arquitectura propuesta [PROPUESTA]

```
 Estación (agente, servicio de Windows)            NUC / servidor
 ┌──────────────────────────────┐   HTTPS 443    ┌───────────┐   ┌────────────────────┐
 │ ciclo de sincronización      │ ─────────────▶ │  nginx    │──▶│ Django "api_agente" │
 │  (cada 60 s ± 20 %)          │ ◀───────────── │ (TLS, 429)│   │  (gunicorn aparte)  │
 │ ejecutor de órdenes (hilos)  │                └───────────┘   └─────────┬──────────┘
 │ identidad.json (ACL SYSTEM)  │   HTTP /media/ (SHA-256)                 │
 └──────────────────────────────┘ ─────────────▶ nginx /media/            ▼
                                                                ┌────────────────────┐
  Panel (web) ── crea órdenes ─────────────────────────────────▶│ PostgreSQL         │
  Celery ── caduca, reintenta, alerta ─────────────────────────▶│  OrdenAgente, …    │
                                                                └────────────────────┘
```

| Responsable | Hace |
|---|---|
| **Agente** | Sincroniza periódicamente; ejecuta órdenes **a lo sumo una vez**; persiste su estado; reintenta reportes con espera exponencial y variación aleatoria |
| **Django (`api_agente`)** | Autentica cada petición; registra el latido; entrega órdenes pendientes; aplica transiciones de estado idempotentes; reutiliza los manejadores de dominio actuales |
| **PostgreSQL** | **Fuente de verdad** de las órdenes y sus estados; restricciones de unicidad que impiden duplicados |
| **Celery** | Caduca órdenes; detecta las entregadas sin acuse y las reprograma; alerta de estaciones mudas; métricas |
| **Panel** | Crea órdenes con la misma autorización de hoy (cuatro ojos donde ya existe) |

---

## 3. Flujo de comunicación [PROPUESTA]

1. **Alta:** `POST /enrolar` → la respuesta trae, **solo a quien la pidió**, la credencial
   propia (token y secreto). Las reglas son las mismas de `manejar_enrolamiento`: TOFU por
   `hardware_id`, aprobación manual y token de apertura.
2. **Ciclo normal**, cada 60 s ± 20 %:
   - el agente envía en **una sola petición**: estado (latido), acuses, eventos de órdenes
     pendientes de reportar y reportes periódicos vencidos;
   - el servidor responde con: órdenes nuevas firmadas, versión de la configuración, intervalo
     sugerido y hora del servidor.
3. **Orden urgente:** si hay órdenes en curso, el servidor sugiere un intervalo corto (10-15 s)
   hasta que terminen. Sin MQTT no hay "empuje": la latencia es la del intervalo.
4. **Resultado de una operación larga:** el agente lo reporta en el ciclo siguiente o, si es
   final, de inmediato con `POST /ordenes/{uid}/eventos`. Si falla, lo guarda en una cola local y
   lo reintenta.
5. **Descargas:** por `/media/` como hoy, verificadas con el SHA-256 de la orden firmada.

---

## 4. Contrato de la API [PROPUESTA]

Prefijo `/api/agente/v1/`. JSON, comprimido con gzip, TLS obligatorio. Las peticiones
autenticadas firman el cuerpo (§6).

| Operaciones del pedido | Endpoint |
|---|---|
| A. Registrar o identificar | `POST /enrolar` |
| B, C, D, E, F, H. Latido, inventario, consultar, acusar, progreso, errores y recuperación | `POST /sincronizar` |
| F. Resultado final inmediato | `POST /ordenes/{uid}/eventos` |
| G. Campañas de actualización y configuración | Las campañas viajan como **órdenes** en `/sincronizar`; los catálogos, en `GET /configuracion` con ETag |

### `POST /sincronizar`

Petición, resumida:
```json
{
  "estado": {"version_agente": "1.0", "version_pos": "4.3.0", "pausado": false,
             "reloj_epoch": 1792540000, "ip_lan": "…", "mac_lan": "…"},
  "inventario": {"hardware_hash": "ab12…", "software_hash": "cd34…", "perifericos_hash": "ef56…"},
  "acuses": ["0f6c…uuid"],
  "eventos_ordenes": [
    {"uid": "0f6c…", "seq": 3, "estado": "en_ejecucion", "detalle": "descargado"},
    {"uid": "9a1b…", "seq": 7, "estado": "completada", "resultado": {"version_nueva": "4.3.1"}}
  ],
  "reportes": {"servicios_pos": [...], "eventos_sistema": [...], "pos_errores": [...]},
  "configuracion_etag": "W/\"cat-17\""
}
```

Respuesta:
```json
{
  "hora_servidor": 1792540003,
  "ordenes": [
    {"uid": "…", "tipo": "instalar_software", "payload": {...}, "expira_en": 1792626400,
     "firma": "hmac…"}
  ],
  "inventario_pedido": ["software"],
  "configuracion_cambiada": true,
  "proximo_intervalo_s": 60
}
```

- **Inventario por hash:** el agente envía resúmenes; el servidor pide el detalle completo solo
  cuando cambió (`inventario_pedido`). Así "C. Enviar cambios de inventario" no carga cada ciclo.
- **`seq` por orden:** número creciente por orden; el servidor ignora eventos con `seq` igual o
  menor al último guardado. Es la base de la idempotencia de los reportes.
- **Códigos:**
  - `200`: sincronizado;
  - `401`: firma inválida o fuera de ventana; incluye `hora_servidor` para que el agente corrija
    su reloj;
  - `403`: estación rechazada o revocada;
  - `409`: transición inválida, que se registra y no se aplica;
  - `429` + `Retry-After`: el servidor pide bajar el ritmo;
  - `503`: mantenimiento, con `Retry-After`.

**Un `200` solo dice que el servidor guardó lo que llegó.** No significa que una orden se ejecutó:
eso lo dice únicamente el evento `completada` de esa orden.

---

## 5. Cola de órdenes y máquina de estados [PROPUESTA]

### Modelo (uno nuevo, justificado)

Hoy no existe un registro único de "qué se le pidió a esta estación y en qué quedó":
- los comandos de consulta y `reiniciar` no se registran;
- software, despliegues y scripts tienen cada uno su modelo de resultado;
- lo pendiente vive en mensajes retenidos de EMQX [VERIFICADO].

Para entregar órdenes por consulta HTTPS hace falta guardarlas en la base. Se propone **un solo
modelo**, `OrdenAgente`. Los `Resultado*` actuales se **conservan** y se actualizan desde los
eventos de la orden: un adaptador, no una reescritura.

| Campo | Para qué |
|---|---|
| `uid` (UUID, único) | Identificador que viaja firmado; base de la idempotencia |
| `estacion` (FK) | Destinataria única: una orden, una estación |
| `tipo`, `payload` (JSON) | Qué hacer |
| `origen` | Solicitud, despliegue, ejecución de script o comando del panel |
| `clave_idempotencia` | Única por `(estacion, clave)`: impide crear la misma orden dos veces |
| `estado` | Ver la máquina de estados |
| `creada_en`, `no_antes_de`, `expira_en` | Caducidad explícita (hoy no existe: E-07) |
| `entregas`, `ultima_entrega_en` | Reintentos de entrega |
| `recibida_en`, `iniciada_en`, `finalizada_en` | Hitos |
| `ultimo_seq`, `resultado`, `detalle_error` | Progreso y resultado |
| `creada_por`, `aprobada_por` | Autorización y auditoría |
| `destructiva` (bool) | Reinicio, despliegue, script, desinstalación: reglas más estrictas |

Índice parcial sobre `(estacion, estado)` solo para los estados pendientes: la consulta de cada
ciclo toca únicamente las filas vivas.

### Máquina de estados

```
           ┌──────────────── cancelada ◀─────────────┐ (solo antes de RECIBIDA)
           │                                          │
 creada ─▶ pendiente_aprobacion ─▶ pendiente ─▶ entregada ─▶ recibida ─▶ en_ejecucion ─┬▶ completada
           (si requiere 4 ojos)       ▲            │                                   ├▶ fallida
                                      │            │ sin acuse en T                    └▶ interrumpida
                                      └────────────┘ (vuelve, entregas < N)
 pendiente / entregada ─▶ caducada   (expira_en vencido sin RECIBIDA)
 entregada ─▶ no_entregable          (entregas ≥ N)
```

| Regla | Por qué |
|---|---|
| `entregada` **no es** `recibida` | La respuesta HTTP puede perderse. Solo el acuse del agente pasa a `recibida` |
| Volver a entregar está permitido **solo antes de `recibida`** | El agente descarta un `uid` ya visto, así que reentregar es seguro |
| **Después de `recibida` el servidor nunca la reenvía** | Garantiza que una operación destructiva se ejecute a lo sumo una vez |
| El agente **persiste el `uid` antes de ejecutar**, como hoy `_marcar_procesado` | Un reinicio a mitad no la repite |
| Agente reinicia con una orden en `en_ejecucion` → la reporta `interrumpida`, **no la reintenta** | Una persona decide; vale para el caso de una caja que se quedó "instalando" para siempre |
| Transiciones idempotentes: el mismo `(uid, seq)` dos veces no hace nada; una transición hacia atrás es `409` | Un acuse duplicado no tiene efecto |
| `cancelada` solo antes de `recibida`. Después, "cancelación solicitada" viaja como orden y el agente la aplica si la operación todavía no empezó | Corrige que hoy la pausa no detiene lo ya entregado (A-5) |
| Una orden caducada nunca se entrega | Una orden vieja no puede ejecutarse por haber quedado guardada |

**Auditoría:** cada transición genera un evento de máquina (tabla `EventoOrden` o la existente
`auditoria` con usuario nulo y `origen`). Crear, aprobar y cancelar quedan con el usuario y la IP,
como hoy.

---

## 6. Seguridad [PROPUESTA salvo lo marcado]

| Control | Propuesta |
|---|---|
| **Identidad única** | `codigo` + `hardware_id`, como hoy [VERIFICADO], con TOFU solo en el primer contacto |
| **Credencial individual** | Un secreto por estación, entregado en el enrolamiento **por la misma conexión TLS**. No hay secreto global: la variable compartida actual se retira cuando no quede ninguna estación en MQTT |
| **Autenticación de cada petición** | Cabeceras `X-Estacion`, `X-Timestamp`, `X-Nonce` y `X-Firma` = HMAC-SHA256(secreto, método + ruta + SHA-256 del cuerpo + timestamp + nonce). Ventana de ±300 s; nonce guardado en Redis durante esa ventana. Fallar responde `401` con la hora del servidor |
| **Alternativa más fuerte** | mTLS con un certificado por estación emitido por una CA propia. Más robusto, pero exige emisión, renovación y revocación de certificados: dejarlo para una segunda etapa |
| **Revocación** | Estación rechazada o revocada → `403` en el siguiente ciclo. Rotar la credencial desde el panel, como hoy `aprovisionar_credencial_estacion` |
| **TLS** | Certificado del servidor **fijado en el agente**, como hoy con `cert.pem` en MQTT [VERIFICADO]; TLS 1.2 o superior |
| **Autorización por operación** | El servidor solo entrega órdenes creadas por usuarios con el permiso de esa operación; las destructivas pasan por cuatro ojos donde ya existe |
| **Validación de órdenes en el agente** | Firma HMAC por estación sobre `uid`, `tipo`, `payload` y `expira_en`, reutilizando `firmar` y `_firma_valida` [VERIFICADO]. Esto **es independiente de TLS**: una orden alterada en el servidor intermedio, en un respaldo o en la base no se ejecuta |
| **Repetición** | `uid` + `expira_en` firmados + registro de `uid` procesados en el agente. Sin la ventana de 120 s para las órdenes, porque la caducidad la reemplaza |
| **Secretos en la estación** | `identidad.json` con permisos solo para SYSTEM y Administradores (`icacls` en el instalador), o el secreto cifrado con **DPAPI de ámbito máquina**. Corrige A-3 |
| **Actualizaciones verificadas** | Orden firmada + SHA-256 [VERIFICADO], más **firma Authenticode** del ejecutable, verificada antes del reemplazo (A-6) |
| **Auditoría** | §5: cada orden con quién la creó, quién la aprobó, cuándo se entregó, se recibió y terminó, y su resultado |
| **Limitación** | nginx `limit_req` por estación e IP; `429` con `Retry-After` |

**Mejora concreta sobre hoy:** la respuesta de enrolamiento viaja por la conexión TLS de quien la
pidió. Desaparece la posibilidad de leer respuestas ajenas con la credencial compartida (P0-2).

---

## 7. Estimación de carga [ESTIMACIÓN]

Peticiones por segundo del ciclo de sincronización = agentes ÷ intervalo:

| Agentes | Cada 15 s | Cada 30 s | Cada 60 s |
|---|---|---|---|
| 1.000 | 67 | 33 | 17 |
| 3.000 | 200 | 100 | 50 |
| 5.000 | **333** | **167** | **83** |

**Reportes periódicos** (servicios del POS cada 300 s, log del POS cada 300 s, eventos cada 900 s):
- **viajando dentro de la sincronización:** no suman peticiones, solo tamaño;
- **como peticiones aparte:** sumarían ~0,0078 × agentes, unas 39/s a 5.000. Por eso se agrupan.

**Base de datos:**
- Hoy un latido cuesta **unas 11 consultas** [VERIFICADO, medido el 5-oct en `capacidad-2030.md`].
- A 5.000 agentes cada 60 s serían **unas 920 consultas/s solo de latidos**, demasiado para el NUC
  actual [ESTIMACIÓN].
- **Con HTTPS habría que bajarlo a 2 o 3 consultas por ciclo**:
  - un solo `UPDATE` con `update_fields`;
  - reglas de reloj evaluadas solo si cambió el desfase;
  - facturación y estado de dispositivo solo al cambiar de estado.

  Esa optimización conviene **también para MQTT**.

**Django:**
- Hoy: 3 workers síncronos (`docker-compose.yml`) [VERIFICADO].
- A 40 ms por petición rinden ~75 peticiones/s en teoría [ESTIMACIÓN].
- **5.000 cada 60 s ya lo superan**: hace falta un servicio de API de agentes separado, con más
  workers o hilos (gunicorn `gthread`), sin competir con el panel.

**Picos de reconexión:**
- Tras un corte de luz o un reinicio del servidor, sin variación aleatoria, **5.000 peticiones
  llegarían en 1 o 2 segundos**.
- Con espera exponencial con variación aleatoria completa (base 5 s, tope 300 s) y la variación de
  ±20 % del ciclo, el pico se reparte en ~1 minuto: **~80-100 peticiones/s extra durante ese
  minuto**.
- A-2 muestra que el agente actual **no** tiene esa variación en el latido.

**Mitigaciones:**
- intervalo dictado por el servidor: más largo cuando hay carga, corto solo con órdenes activas;
- `429` con `Retry-After`;
- conexiones persistentes (keep-alive) para no negociar TLS en cada ciclo;
- gzip;
- inventario por hash.

**Celery:**
- caducar órdenes y reprogramar las entregadas sin acuse: una tarea cada minuto, por lotes;
- alertas de estaciones mudas: la de hoy (`marcar_estaciones_offline`) sirve igual.

**Monitoreo y alertas:**
- peticiones/s y latencia (p95) por endpoint;
- proporción de `401`, `429` y `5xx`;
- antigüedad de la orden pendiente más vieja;
- órdenes en `entregada` sin acuse;
- estaciones sin sincronizar;
- todo como componentes de la barra de salud que ya existe.

**Recomendación de intervalo:** 60 s ± 20 % por defecto, y 10-15 s solo mientras la estación tenga
órdenes en curso.

**La alternativa MQTT a 5.000:** ampliar `max_conns` y `ulimit` de EMQX y repartir el worker con
suscripciones compartidas (fase 3 del roadmap). La carga de escritura en la base es la misma en
los dos transportes.

---

## 8. Compatibilidad con SaidSoft

| Componente | Efecto | Se conserva / se adapta |
|---|---|---|
| `mqtt_worker` | Sus 15 manejadores se reutilizan desde la API HTTPS. A mediano plazo convendría moverlos a un módulo neutral (`apps/catalogo/ingesta.py` o similar). `run_mqtt_worker` sigue sirviendo a las estaciones en MQTT | **Se conserva** durante toda la migración |
| EMQX | Sigue para las estaciones que no migraron. Se retira solo en la fase 7 | **Se conserva** |
| `monitoreo` | Recibe los mismos datos por la misma vía de manejadores; alertas, enlaces y salud no cambian. Componentes nuevos de salud para la API | Se adapta poco |
| `software`, `despliegues`, `scripts` | Publicar pasa a "crear órdenes"; la entrega la decide el transporte de cada estación. Los modelos `Resultado*` se conservan y se actualizan desde los eventos de la orden. Mejoras de paso: caducidad, cancelación real y pausa que sí detiene lo no iniciado | Se adaptan los servicios de publicación |
| `catalogo` | `enviar_comando`, `enviar_actualizacion_agente` y pausa → órdenes. `Estacion` gana un campo de transporte (`mqtt`/`https`) y la credencial de transporte | Se adapta |
| `auditoria` | Eventos de ciclo de vida de las órdenes | Se agrega |
| Agente | Nuevo transporte HTTPS dentro del mismo agente, elegido por configuración; todo lo de la §1 que no depende de MQTT se reutiliza | Se adapta |
| Docker Compose | Opcional: servicio `api_agente` (otro gunicorn sobre la misma imagen), y Redis para nonces y limitación, que ya existe | Se agrega |
| PostgreSQL | Tablas `OrdenAgente` (y `EventoOrden`), índices parciales. Sin cambios en las tablas existentes, salvo el campo de transporte en `Estacion` | Migraciones aditivas |
| Celery | Tareas de caducidad, reentrega y métricas de la cola | Se agrega |

**Lo que no cambia:** MQTT y sus flujos actuales, mientras haya estaciones en MQTT.

---

## 9. Riesgos

| # | Riesgo | Mitigación |
|---|---|---|
| R-1 | **Latencia de las órdenes**: un reinicio o una consulta tarda hasta un intervalo | Intervalo corto mientras hay órdenes activas; que el panel muestre "entregada / recibida" |
| R-2 | **Carga sobre Django y PostgreSQL** mayor que con MQTT (cada latido es una petición completa) | Optimizar el latido (§7); servicio de API separado; medir antes de crecer |
| R-3 | **Reescribir el transporte del agente** en una flota con versiones de 0.29 a 0.36 | Doble transporte en el mismo agente; migración por tandas con canario |
| R-4 | **Ejecución duplicada** de una operación destructiva | Reglas de §5; pruebas específicas de duplicado y de reinicio a mitad |
| R-5 | **Desfase de reloj** de las cajas contra la ventana de la firma | `401` con la hora del servidor y corrección automática, que el agente ya hace |
| R-6 | **El NUC sigue siendo el punto único de falla** | El transporte no lo resuelve; es la fase 3 del roadmap |
| R-7 | **Dos transportes en paralelo** durante meses | Una sola cola de órdenes; el transporte es solo la entrega |
| R-8 | **Proxies o firewalls de farmacia** que inspeccionen TLS | [HIPÓTESIS] probarlo en el piloto; el certificado fijado lo detectaría |

---

## 10. Plan de migración

Cada fase tiene criterios para avanzar y una forma de volver atrás. **Ninguna se ejecuta sin
aprobación.**

| Fase | Qué | Criterio de aceptación | Reversión |
|---|---|---|---|
| **1. Cola de órdenes, transporte MQTT** *(implementada en la rama `fase1/cola-ordenes`, sin desplegar: `apps/catalogo/ordenes.py`, modelo `OrdenAgente`, migración `catalogo/0039`)* | `OrdenAgente` y la máquina de estados; software, despliegues, scripts y comandos crean órdenes; se entregan por MQTT como hoy | Mismas pruebas de hoy en verde; duplicados y reinicios a mitad probados; caducidad y cancelación funcionan | Las órdenes se pueden seguir publicando por el camino viejo: bandera de configuración |
| **2. Prototipo HTTPS independiente** | API `/api/agente/v1/` y un agente de prueba, en laboratorio con la imagen y la base de producción (sin datos reales) | Contrato cubierto por pruebas; firma, ventana, `409` y `429` verificados | Se borra: no toca producción |
| **3. Una estación** | Una caja de la oficina, con doble transporte y HTTPS activo | 7 días sin pérdida de latidos ni de resultados; un despliegue del POS y una instalación completos | Configuración `transporte=mqtt` y reinicio del servicio |
| **4. Piloto controlado** | 10 a 20 estaciones de distintas farmacias | Igual que la fase 3, con latencia de las órdenes medida | Ídem, por estación |
| **5. Desconexión y recuperación** | Cortes de red y de energía, servidor reiniciado y reloj desfasado, sobre el piloto y en laboratorio | Ninguna orden destructiva duplicada; ningún resultado perdido; pico de reconexión dentro de lo estimado | — |
| **6. Miles de agentes simulados** | Simulador en laboratorio (no en producción) con 5.000 agentes a 60 s y picos de reconexión | p95 de `/sincronizar` < 300 ms; sin `5xx`; base por debajo del 60 % de CPU | — |
| **7. Migración gradual** | Por tandas, con el mecanismo de actualización del agente que ya existe | Cada tanda pasa los criterios de la fase 3 antes de la siguiente | Bajar la tanda a MQTT por configuración |
| **8. Retirar el broker** | Solo cuando **ninguna** estación use MQTT durante 30 días y los respaldos no dependan de EMQX | `revisar_credenciales_mqtt` sin clientes; panel sin componentes de MQTT | Volver a levantar EMQX desde su respaldo (`emqx ctl data import`) |

---

## 11. Criterios de aceptación globales

1. **Ninguna orden destructiva se ejecuta dos veces**, demostrado con pruebas de acuse duplicado,
   respuesta perdida y reinicio a mitad.
2. **Ningún resultado se pierde** ante cortes de red de hasta 24 h: la cola local del agente
   reintenta.
3. **Una orden vencida nunca se ejecuta.**
4. **Cada orden tiene trazabilidad completa** en el panel: quién, cuándo, a quién, estado y
   resultado.
5. **No hay un secreto compartido por la flota.**
6. **5.000 agentes simulados** a 60 s con p95 < 300 ms en laboratorio, antes de pasar de 1.000
   estaciones reales.
7. **MQTT sigue funcionando** para todas las estaciones no migradas durante toda la transición.

---

## 12. Resumen de clasificación

| Afirmación | Marca |
|---|---|
| La lógica de dominio del servidor es independiente del transporte | [VERIFICADO] |
| El agente reutiliza sin cambios la lectura del equipo, la ejecución, las firmas y las descargas | [VERIFICADO] |
| El agente pierde reportes ante cortes (QoS 0) y late sin variación aleatoria | [VERIFICADO] |
| `identidad.json` es legible por usuarios locales | [HIPÓTESIS]: falta comprobar los permisos en una caja real |
| 5.000 agentes cada 60 s ≈ 83 peticiones/s; ~920 consultas/s con el latido actual | [ESTIMACIÓN] |
| MQTT puede escalar a 5.000 con configuración | [HIPÓTESIS] razonable; probar en laboratorio |
| La cola persistente mejora la plataforma con cualquier transporte | [PROPUESTA] |
