# 02 · Arquitectura encontrada

[← 01 Resumen ejecutivo](01-resumen-ejecutivo.md) · [Índice](README.md) · [Siguiente: Auditoría de indicadores →](03-auditoria-indicadores.md)

El flujo real de los datos, desde el agente hasta el píxel. Todo lo de esta parte está
**CONFIRMADO** por lectura de código; es la base de evidencia del resto del informe.

---

## 2.1 Flujo de un heartbeat

```
Agente Windows  (agente-prueba/)
   |  publica JSON en  /saidsof/agente/<CODIGO>/heartbeat/   (QoS 0)
   v
EMQX 5.8.3
   |  credencial por estacion; ACL en deploy/emqx-narrow-acl-agente.sh
   v
run_mqtt_worker
   |  apps/mqtt_worker/management/commands/run_mqtt_worker.py
   |  _on_message()  ->  _registrar_latido_si_corresponde()        (linea 146)
   v
manejar_heartbeat()
   |  apps/mqtt_worker/services.py:218
   |  . select_related('farmacia__unidad_negocio')               (221)
   |  . descarta si estado_aprobacion != APROBADA                (235)
   |  . resuelve alertas sin_heartbeat / agente_caido_red_viva   (238-241)
   |  . estado_conexion = ONLINE ; ultimo_heartbeat = now()      (324-325)
   |  . estacion.save()          <- sin update_fields (ver R-7)  (326)
   |  . evaluar_regla_reloj / autocorrecciones / registrar_estado_dispositivo
   v
Estacion.ultimo_heartbeat   .  apps/catalogo/models.py:399
Estacion.estado_conexion    .  apps/catalogo/models.py:396
   ^
   |  Celery Beat  'marcar-estaciones-offline'  cada 60 s
   |  config/settings/base.py:378
   |  apps/catalogo/tasks.py:9  ->  marcar_estaciones_offline(minutos=5)
   |  apps/catalogo/services.py:400
   |     . ONLINE y ultimo_heartbeat < now-5min  ->  OFFLINE
   |     . evaluar_reglas_sin_heartbeat(afectadas)   <- ABRE ALERTAS
   v
Vista Django  ->  contexto  ->  plantilla
```

**Lo que importa de este flujo para el tablero:**

- `ultimo_heartbeat` es un **valor persistido**, no una comprobación. Si el worker muere, el
  dato sigue ahí y el dashboard sigue mostrando estaciones en línea durante 5 minutos, y
  después las declara caídas una por una — presentando como fallo de 700 farmacias lo que
  es el fallo de un proceso.
- El mismo umbral de 5 minutos decide **dos cosas distintas**: cómo se pinta la pantalla y
  **cuándo se abre una alerta**. Eso condiciona cualquier cambio de umbral (ver `X-1`).
- Una estación **pausada sigue latiendo** por diseño deliberado y documentado
  (`apps/catalogo/models.py`, comentario del campo `pausado`): *"podría callarse del todo,
  pero entonces el panel la mostraría caída y perderías visibilidad justo en el momento en
  que más la necesitas"*. Correcto como mecanismo; invisible como información (`D-09`).

---

## 2.2 Flujo de una caída de enlace

```
Celery Beat  'sondear-enlaces-farmacias'  cada 2 min
   |  config/settings/base.py:589
   v
sondear_enlaces_farmacias()   .  apps/monitoreo/enlaces.py:290
   |  ICMP a Farmacia.ip_router
   |  ABORTA el barrido completo si falla >=80 %  <- proteccion anti-falso-positivo:
   |    asume que el que perdio la ruta es este host, no que cayeron 704 farmacias
   v
registrar_sondeo()            .  apps/monitoreo/enlaces.py:171
   |  . ignora farmacia.activa=False  (punto unico; GP063, 2-oct-2026)
   |  . fallas_consecutivas >= ConfiguracionMonitoreo.fallas_consecutivas_enlace
   |        defecto 3  ->  ~6 min con el barrido de 2 min
   |  . primer_fallo = instante de la PRIMERA falla, no la tercera
   |  . recuperacion inmediata al primer sondeo exitoso
   +--> EstadoEnlaceFarmacia    estado ACTUAL (alcanzable, latencia, ultima_verificacion)
   +--> EventoEnlaceFarmacia    HISTORIAL  (inicio = primer_fallo, fin = recuperacion)
                                 . PROTECT, no CASCADE: es la evidencia de SLA
                                 . circuito_proveedor denormalizado al momento de la caida
   v
Tres consumidores, DOS fuentes distintas:
   . dashboard            ->  EventoEnlaceFarmacia(fin=null)        apps/panel/views/dashboard.py:122
   . lista del Centro     ->  EventoEnlaceFarmacia(fin=null)        apps/panel/views/monitoreo.py:334
   . KPI del Centro       ->  EstadoEnlaceFarmacia(alcanzable=False) apps/monitoreo/services.py:1483
   . pantalla de Enlaces  ->  EstadoEnlaceFarmacia (select_related)  apps/panel/views/enlaces.py:97
```

Ver `D-04` en [12 · Índice de hallazgos](12-indice-hallazgos.md) y el detalle en
[04 · Riesgo C](04-riesgos-del-encargo.md#c--enlaces-caídos).

La recuperación cierra **el evento abierto más reciente**
(`.order_by('-inicio').first()`, `enlaces.py:238`), y `dar_de_baja_enlace()`
(`enlaces.py:245`) cierra **todos** los abiertos de la farmacia y devuelve el estado a
`alcanzable=None` — "nunca se sondeó", el único valor que ninguna pantalla cuenta como
caída. Esa función es la respuesta correcta a un problema real y está bien resuelta.

---

## 2.3 Salud de la propia plataforma

### Mecanismos que existen

| Componente | Mecanismo real | Archivo |
|---|---|---|
| Worker MQTT | hilo `_latido_periodico` escribe `WorkerHeartbeat` cada *N* s mientras el proceso viva | `run_mqtt_worker.py:107` |
| Worker MeshCentral | mismo mecanismo | `verificar_salud.py:124` |
| Respaldo | `deploy/backup.sh` → `manage.py registrar_latido respaldo` al **terminar bien** | `WorkerHeartbeat` |
| Celery Beat | **inferido** de la frescura de `MuestraRedFarmacia` | `verificar_salud.py:152` |
| Sondeo de enlaces | **inferido** de `EstadoEnlaceFarmacia.ultima_verificacion` | `verificar_salud.py:171` |
| Base de datos | `SELECT 1` | `verificar_salud.py:111` |
| Disco, Telegram | revisiones propias | `verificar_salud.py:189+` |
| **Broker EMQX** | **no existe ninguna comprobación** | — |

`verificar_salud` corre **desde fuera del stack**, por un temporizador de systemd, y su
contrato es el código de salida. La decisión está bien argumentada en su propio docstring:
*"una alerta generada por Celery no sirve para avisar que Celery se cayó. Un proceso
externo sí."* **No debe sustituirse por una vista web**; lo que falta es exponer su
resultado, no reemplazar el mecanismo.

### El problema del latido del worker — `D-05` CONFIRMADO

```python
# apps/mqtt_worker/management/commands/run_mqtt_worker.py:107-109
def _latido_periodico(self):
    while not self._detener.wait(LATIDO_INTERVALO_SEGUNDOS):
        registrar_latido_worker(NOMBRE_WORKER_MQTT)   # no consulta client.is_connected()
```

El hilo escribe mientras el proceso viva. No consulta la conexión al broker ni cuenta
mensajes procesados. El comentario que acompaña al hilo explica por qué se añadió —un
worker sano sin tráfico se veía "sin señal" a los 90 s— y la solución **resolvió el falso
negativo abriendo un falso positivo**: ahora un worker desconectado del broker se ve sano.

Los cuatro estados que un tablero de monitoreo tiene que distinguir, y lo que hoy se puede
afirmar de cada uno:

| Estado | ¿Se puede afirmar hoy? |
|---|---|
| Proceso activo | **Sí** — `WorkerHeartbeat` |
| Comunicación con el broker | **No** |
| Procesamiento efectivo | **No** (`MensajeMqttFallido` existe, no se muestra) |
| Disponibilidad de los datos | **Inferible** por `max(Estacion.ultimo_heartbeat)` — **no se calcula en ningún sitio** |

---

## 2.4 Mapa de umbrales: dónde vive cada uno

Esta tabla es el resumen de la parte más importante de la auditoría. Las primeras seis
filas están bien resueltas; las tres últimas son el mismo concepto escrito tres veces con
dos valores.

| Umbral | Valor | Fuente de verdad | Estado |
|---|---|---|---|
| Worker MQTT sin señal | 90 s | `apps/monitoreo/umbrales.py` | ✅ compartido y documentado |
| Respaldo atrasado | 26 h | `apps/monitoreo/umbrales.py` | ✅ compartido |
| Fallas para declarar enlace caído | 3 | `ConfiguracionMonitoreo` (BD) | ✅ editable sin desplegar |
| Escalamiento de alerta | 30 min | `ConfiguracionMonitoreo` (BD) | ✅ editable |
| CPU / RAM / disco | de la `ReglaAlerta` activa, con defecto | `apps/panel/umbrales.py` | ✅ la regla manda sobre la constante |
| Frescura por fuente | 6 / 15 / 10 / 30 min | `apps/monitoreo/services.py:1401` | ✅ derivada de `CELERY_BEAT_SCHEDULE` |
| **Estación en línea** | **5 min** | `apps/panel/views/dashboard.py:23` | ❌ |
| **Estación en línea** | **10 min** | `services.py:1401` `['estaciones']` | ❌ |
| **Estación → OFFLINE** | **5 min** | `apps/catalogo/tasks.py:9` | ❌ literal en la tarea |

El propio proyecto ya resolvió este problema una vez y dejó escrito el criterio, en
`apps/monitoreo/umbrales.py`:

> *"Un umbral operativo no es un detalle de cómo se pinta una pantalla; que estuviera en la
> vista era el accidente. [...] un valor que necesitan dos consumidores no es de ninguno de
> los dos."*

`ONLINE_UMBRAL_MINUTOS` sigue viviendo en una vista.

---

## 2.5 Cadencias del backend (de `CELERY_BEAT_SCHEDULE`)

Relevante porque **ninguna cadencia de refresco de pantalla debería ser más rápida que la
del dato que muestra** — criterio que el Centro ya aplica y prueba
(`test_el_polling_no_es_mas_rapido_que_el_backend`).

| Tarea | Cadencia | Qué alimenta en el tablero |
|---|---|---|
| `marcar-estaciones-offline` | 60 s | `estado_conexion`, alertas `sin_heartbeat` |
| `sondear-enlaces-farmacias` | 2 min | enlaces caídos |
| `sincronizar-ancho-banda-farmacias` | 5 min | ancho de banda (fuente principal: 252 de 270 farmacias, medido 5-oct-2026) |
| `sondear-red-farmacias-via-agente` | 5 min | respaldo del anterior, solo las que el directo no midió |
| `notificar-cambios-enlaces` | 5 min | correos de caída y recuperación |
| `evaluar-cruce-monitoreo` | 7 min | cruce agente / red |
| `escalar-alertas-abiertas` | 10 min | reenvío de alertas sin reconocer |
| `sondear-activos-sin-agente` / `sondear-snmp-rapido` | 15 min / 5 min | equipos sin agente |
| purgas, programados, resumen diario | crontab 3:00-8:00 | — |

Las diarias usan `crontab` y no un intervalo numérico, por una razón medida y documentada:
el estado de Beat vive dentro del contenedor sin volumen, así que un intervalo de 86 400 s
solo dispara si pasan 24 h sin desplegar. Pasó: `EventoMonitoreo` tenía filas de 33 días
con retención de 30.

---

## 2.6 Infraestructura relevante para el rediseño

| Hecho | Evidencia | Por qué importa |
|---|---|---|
| **No hay `CACHES` configurado** | `grep CACHES config/settings/` sin resultados | Django usa LocMemCache por proceso: inútil con 6 contenedores. Ver `R-5` |
| **Redis ya es dependencia** | `requirements.txt: redis>=5.0`; `CELERY_BROKER_URL` | Configurar caché **no añade dependencia nueva** |
| **`ASGI_APPLICATION` existe** | `config/settings/base.py:103` | WebSockets serían posibles — y no se justifican (ver parte 07) |
| **HTMX servido localmente** | `templates/panel/base.html:8` | El patrón marco + parcial ya está probado en el Centro |
| **Sin índices en `Estacion`** | `Meta` en `catalogo/models.py:533` sin `indexes`; ninguna migración de `catalogo` con `AddIndex` | Ver `R-3`. Contrasta con `Alerta` y `EventoEnlaceFarmacia`, que sí los tienen y documentados |
| **6 imágenes Docker** | `deploy/`, CLAUDE.md | `build web` no alcanza: el worker se queda con código viejo |

---

## Lo que hay que decidir en esta parte

Nada. Esta parte es evidencia, no propuesta. Si algo de aquí es incorrecto, el resto del
informe se cae: **es la parte que conviene refutar primero.**

---

[← 01 Resumen ejecutivo](01-resumen-ejecutivo.md) · [Índice](README.md) · [Siguiente: Auditoría de indicadores →](03-auditoria-indicadores.md)
