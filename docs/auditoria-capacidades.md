# Radiografía de SAIDSOFT: qué tiene, qué datos maneja, qué puede responder

**Fecha:** 27-sep-2026
**Alcance:** toda la plataforma. Solo lectura — no se modificó nada.
**Estado del repo al auditar:** `HEAD = 0034f18`, rama `master`.

> **Documento hermano de** [`capacidad-2030.md`](capacidad-2030.md) (¿aguanta 1.300
> farmacias?) y [`prioridad-2030.md`](prioridad-2030.md) (qué atender primero). Para
> orientarse en el código del día a día, [`modulos.md`](modulos.md).

---

## El límite de esta auditoría, por delante

**Cuando se escribió este documento no se pudo contar una sola fila real**: el servidor
(`10.111.6.20`) no respondía ni por SSH ni por HTTPS.

Eso tiene una consecuencia que atraviesa todo el documento: cuando acá dice que una
capacidad **existe**, significa que el código que la implementa está escrito y cubierto
por pruebas. **No significa que esté en uso, ni que las tablas tengan datos.**

> **Actualización del 28-sep-2026: esa segunda mitad ya se midió.** Ver
> [Uso real](#uso-real--medido-el-28-sep-2026) al final. El resumen en una línea: 701
> farmacias modeladas, **42 estaciones y 29 agentes vivos**, 21 activos, 3 mantenimientos.
> **Lo construido sigue muy por delante de lo usado.**

---

## 1. Resumen

| | |
|---|---|
| Apps Django | 15 |
| Modelos / tablas | 97 |
| Migraciones | 150 |
| Vistas de panel | 176 (166 rutas, 91 plantillas) |
| Tareas Celery | 26 definidas, 23 en Beat |
| Contenedores | 11 |

**SAIDSOFT no es un ITAM al que se le pegó monitoreo. Es un RMM con agente propio** que
además lleva el inventario, los mantenimientos y los procesos de campo de la cadena. Esa
es la diferencia más importante frente a lo que el nombre sugiere.

Lo sólido: el agente Windows (MQTT sobre TLS, comandos firmados con HMAC), el monitoreo
de estaciones y del enlace de cada farmacia (incluido SNMP real contra los MikroTik), el
inventario con ciclo de vida completo, los mantenimientos con SLA y evidencia, y una
auditoría transversal con 133 puntos de registro.

Lo que **no existe**, y conviene no confundirlo con "está a medias":

- **Zabbix: cero.** Ni una línea en todo el repositorio.
- **Active Directory / LDAP: cero.** Solo un paso manual de checklist.
- **ESET: cero como integración.** Solo la cadena `"ESET"` buscada en el inventario de
  software.
- **Odoo: cero como cliente.** Existe `TipoOrigenMantenimiento.ODOO_HELPDESK` y **ningún
  código lo asigna nunca**.
- **Tickets: no hay modelo.** Lo más cercano es `Mantenimiento`, que es una orden de
  trabajo sobre equipos.
- **Topología entre activos: no existe.**
- **Wake-on-LAN: cero.**

> **Hallazgo estructural.** La app `integraciones` —el registro de conectores donde iban
> a vivir Odoo, AD y ESET— **se dio de baja el 26-sep** porque en toda su vida nunca se
> llamó desde ningún lado (ver `catalogo/0033_baja_de_integraciones`). El lugar donde uno
> esperaría encontrar las integraciones no está a medias: fue retirado por no usarse.

---

## 2. Arquitectura

| Componente | Existe | Estado | Evidencia |
|---|---|---|---|
| Backend | Sí | Completo | Django 5.2, 15 apps |
| Frontend | Sí | Completo | Plantillas + HTMX, sin SPA |
| API REST | Sí | **Solo para la app móvil** | `apps/mantenimiento/api_urls.py` |
| Base de datos | Sí | Completo | TimescaleDB 2.17 / PG16, 3 hypertables |
| MQTT | Sí | Completo | EMQX 5.8.3 sobre TLS |
| Worker MQTT | Sí | **Monohilo** | `manage.py run_mqtt_worker` |
| Tareas programadas | Sí | Completo | Celery Beat, 23 entradas |
| Broker de cola | Sí | Completo | Redis 7 |
| Acceso remoto | Sí | Completo | MeshCentral (escritorio + terminal) |
| WebSockets | Parcial | Interno, no de UI | Solo el canal con MeshCentral |
| Autenticación | Sí | Completo | Sesión + MFA TOTP; token DRF; axes |
| Autorización | Sí | Completo | Permisos Django + 8 roles + multi-tenant |
| Auditoría | Sí | Completo | 133 puntos de registro |
| Notificaciones | Sí | Completo | Correo + Teams + Telegram |
| Registro de conectores | **No** | Retirado 26-sep | `catalogo/0033` |

**Los 11 contenedores:** `db`, `emqx`, `web` (gunicorn x3), `nginx`, `worker` (MQTT),
`meshcentral_worker`, `telegram_bot`, `redis`, `celery_worker`, `celery_beat`,
`meshcentral`.

---

## 3. Modelo de datos

```text
UNIDAD_NEGOCIO  (SG - MIA - 7DIAS; nulo = CRESIO corporativo)
   └── GRUPO  (nodo POS: servidor, puerto, BDD, clave cifrada)
          └── FARMACIA  (ciudad, provincia, lat/lon, segmento de red, tipo de enlace,
               │         ancho contratado, circuito, IP del router, coordinadores,
               │         técnico asignado)
               ├── ESTACION  (1:1 opcional con ACTIVO)
               │      ├── hostname, SO, build, serie, procesador, RAM, disco
               │      ├── versión de agente y de POS
               │      ├── BitLocker, Windows Update, plan de energía
               │      ├── heartbeat, estado de conexión, desfase de reloj
               │      ├── pausa remota, HMAC propio, hardware_id
               │      └── meshcentral_node_id  ->  escritorio y terminal remotos
               ├── ACTIVO  (12 tipos; ip, mac, serie, garantía, responsable)
               ├── EQUIPO_BORDE  (1:1 — el MikroTik: modelo, serie, RouterOS, uptime)
               ├── ESTADO_ENLACE  (latencia, pérdida, ancho, proveedor)
               └── DISPOSITIVO_DETECTADO  (mac + ip vistas en la tabla ARP)
```

Los históricos inmutables siguen todos el mismo patrón —estado actual mutable + tabla de
eventos con `PROTECT`—: despliegues, instalaciones, mantenimientos, aperturas y enlaces.

---

## 4. Activos

12 tipos en enumeración cerrada, no extensible sin migración.

| Tipo | Existe | Cómo |
|---|---|---|
| Computadores / NUC | Sí | `DSK`. Un NUC entra acá, sin tipo propio |
| Laptops · Servidores | Sí | `LAP` · `SRV` |
| MikroTik / routers | Sí | `NET` **más** el modelo dedicado `EquipoBordeFarmacia` (1:1 con la farmacia), que es el que se sondea por SNMP |
| Switches · APs | Parcial | Caen en `NET`, sin distinguir. Sin puertos |
| Impresoras · PINPAD · Teléfonos IP | Sí | `IMP` · `PIN` · `TEL` |
| UPS · Cámaras · Biométricos · Alarma | Sí | `UPS` · `CAM` · `BIO` · `ALM` |
| Dispositivos móviles | Parcial | `TAB`. Sin MDM: se inventarían, no se gestionan |
| Monitores | **No** | Sin tipo propio |
| Periféricos | Parcial | No son activos. Existe `PerifericoDetectado` y consumibles con stock |

**Guarda:** código interno, serie, código SAP, modelo, marca, categoría, procesador, RAM,
disco, IP, MAC, farmacia, bodega, responsable, estación, ubicación interna, slot, estado,
estado físico, condición al recibir, fecha de compra, vencimiento de garantía, orden de
compra e historial completo.

**No guarda:** costo, proveedor de garantía, contrato, licencias.

---

## 5. Jerarquía de sucursales

| Nivel | Existe | Cómo |
|---|---|---|
| CRESIO | Sí | `UnidadNegocio` (nulo = corporativo) |
| Región | **Parcial** | No es entidad: `provincia` y `coordinador_regional` son texto libre |
| Ciudad | **Parcial** | Campo de texto, no tabla |
| Farmacia | Sí | Entidad central con geolocalización |
| Enlace | Sí | Tipo, ancho contratado, circuito, backup + `EstadoEnlaceFarmacia` medido |
| Router | Sí | `ip_router` + `EquipoBordeFarmacia` |
| Estaciones / POS | Sí | `Estacion` |

> **Región y ciudad son texto, no estructura.** Se puede filtrar y agrupar por ellas, pero
> no colgarles responsables, metas ni SLA propios, ni garantizar que "Guayaquil" y
> "GUAYAQUIL" sean el mismo lugar. Es el primer límite que se topa al reportar por región.

---

## 6. Topología: no existe

**Se buscó explícitamente en todos los `models.py`: no hay ninguna relación padre/hijo
entre activos, ni autoreferencia, ni campo de puerto, ni VLAN, ni "conectado a".**

Lo que sí existe:

- **Pertenencia a la farmacia.** Contención, no conectividad.
- **El equipo de borde individualizado** (`EquipoBordeFarmacia`, 1:1).
- **Descubrimiento por ARP.** `DispositivoDetectado` guarda MAC, IP y —esto es lo
  relevante— **`interfaz_indice`**: por qué interfaz del MikroTik se vio cada dispositivo.
  **Es el dato crudo del que se podría derivar la topología, y hoy nadie lo interpreta
  como tal.**

El sistema sabe *qué hay* en cada farmacia y *por qué puerta del router se lo vio*, pero
no *qué cuelga de qué*. Consecuencia práctica: **no puede calcular impacto**. Si cae el
switch, no hay forma de decir qué cajas se quedan sin servicio.

---

## 7. Monitoreo

| Capacidad | Existe | Cómo |
|---|---|---|
| Disponibilidad de estación | Sí | Heartbeat; tarea cada minuto marca offline a los 5 min |
| CPU, RAM, disco | Sí | El agente reporta a `MuestraMetrica` (hypertable) |
| Ping / ICMP | Sí | Sonda de enlaces **y** `EstadoRedActivo`: una estación con agente sondea a los activos sin agente de su farmacia |
| Latencia y pérdida | Sí | `EstadoEnlaceFarmacia` + `MuestraRedFarmacia` |
| Tráfico e interfaces | Sí | SNMP: `ifHCInOctets/OutOctets` con cálculo de tasa |
| SNMP | Sí | 696 líneas en `mikrotik.py`: identidad, rutas, máscaras, ARP |
| Servicios del POS | Sí | El agente lee el `.exe.Config` real y prueba los dos PostgreSQL con `SELECT 1`, Odoo y el SOAP de recargas — **medido desde la estación** |
| Eventos de Windows | Sí | `EventoSistemaVigilado` define qué IDs observar |
| Errores del POS | Sí | El agente lee el log y reporta |
| Procesos / servicios Windows | Parcial | Sin monitoreo continuo; solo por script ad-hoc |
| Temperatura | **No** | — |
| Alertas e históricos | Sí | `ReglaAlerta` con umbral, duración y severidad; escalamiento a 30 min; ventanas de mantenimiento |
| Webhooks entrantes | **No** | Solo salientes |

**Diagnóstico con IA:** existe y está acotado con cuidado — solo alertas críticas, una vez
por incidente, resultado en campo propio (`Alerta.diagnostico_ia`), rotulado como
hipótesis sin verificar. Requiere `ANTHROPIC_API_KEY`.

---

## 8. RMM y agente

**La capacidad más madura de la plataforma.**

| | |
|---|---|
| Nombre | `agente-prueba-0.32` |
| Tecnología | Python con PyInstaller. **No es C#** |
| Comunicación | MQTT sobre TLS contra EMQX |
| Autenticación | Usuario/clave MQTT + **HMAC por comando**, ventana anti-replay de 120 s |
| Hilos | 6 bucles: heartbeat, métricas, log POS, servicios POS, eventos de sistema, reloj |

**Frecuencias por defecto** (`CAMPOS_CONFIG`, configurables por estación): heartbeat 60 s ·
métricas 300 s · log POS 300 s · servicios POS 300 s · eventos de Windows 900 s · reloj
3600 s.

**Los 10 comandos:** `ejecutar_script`, `consultar_info`, `reiniciar`,
`escanear_actualizaciones`, `consultar_software_instalado`, `consultar_perifericos`,
`consultar_red_farmacia`, `consultar_activos_farmacia`, `configurar_nodo_pos`,
`actualizar_agente`.

| Capacidad | Estado |
|---|---|
| Heartbeat, inventario de hardware y software | Sí |
| Ejecución remota PowerShell/CMD | Sí, con aprobación, timeout y motivo de no-respuesta |
| Parches de Windows | **Escanea y reporta; no instala.** Es la v1 declarada |
| Reinicio remoto | Sí |
| Apagado remoto · Wake-on-LAN | **No** (y falta la MAC en `Estacion`) |
| Acceso remoto | Sí, MeshCentral con grabación supervisada |
| Instalación / desinstalación de software | Sí, por estación, farmacia o grupo |
| Autoactualización por olas | Sí, con aprobación y sin downgrade automático |
| Pausa remota (kill-switch) | Sí, en tópico retenido propio |
| Autocorrección de reloj | Sí, con cooldown y reporte |
| BitLocker | Sí, estado y custodia de la clave |

---

## 9. ITAM y CMDB

**ITAM: completo.** Identificación (código, serie, SAP, hostname), propiedad (unidad,
farmacia, bodega, responsable), ciclo de vida completo con pantallas propias (compra →
recepción → asignación → traslado → devolución → reparación → retorno → baja) e historial
vía `EventoActivo`.

**Falta:** costo y depreciación, contratos y proveedor de garantía, licencias, y la
distinción responsable/custodio.

**CMDB: acá está más lejos de lo que el nombre sugiere.**

*Lo que sí es CMDB:* elementos con identidad estable, atributos ricos, jerarquía de
contención de cuatro niveles, descubrimiento automático (agente, ARP, SNMP),
reconciliación por número de serie (tarea nocturna) e historial inmutable.

*Lo que no:* **sin relaciones entre CIs** (ni "depende de", ni "conectado a"), **sin
servicios de negocio**, **sin análisis de impacto**, sin línea base ni detección de
desvío.

> Hay un inventario vivo y bien descubierto, que es la mitad difícil de una CMDB. Falta la
> capa de relaciones, que es la que convierte el inventario en un modelo.

---

## 10. Helpdesk / ITSM

**No existe un modelo de ticket.** Se verificó: no hay clase `Ticket`, `Incidente`,
`Requerimiento` ni `Problema` en ningún `models.py`.

Lo que cumple ese papel es `Mantenimiento`, que es una **orden de trabajo sobre equipos**,
no un ticket de usuario. La diferencia es real: **exige al menos un equipo**
(`services.py` lanza `ValueError` si no lo hay), así que no puede representar "la
impresora de la farmacia no imprime" antes de saber cuál es.

| Pieza | Existe |
|---|---|
| SLA, prioridades, categorías, asignación, estados | Sí |
| Evidencias (fotos, firma, repuestos, PDF), historial y tiempos | Sí |
| Incidentes | Parcial (como mantenimiento correctivo) |
| Requerimientos | Parcial (`SolicitudInstalacion`, solo software) |
| Problemas · Cambios · Portal · Correo entrante | **No** |

La cadena que **sí** está cerrada: Alerta → Mantenimiento → Técnico → Visita → Firma →
PDF, con botón manual en la lista de alertas y apertura automática cuando la regla lo
pide.

---

## 11. Integraciones: el estado real

| Integración | Estado | Evidencia |
|---|---|---|
| MQTT / EMQX | **Existe** | Broker propio, TLS, ACL sembradas por `bootstrap-emqx.sh` |
| SNMP / MikroTik | **Existe** | 696 líneas: tráfico, identidad, rutas, ARP |
| MeshCentral | **Existe** | Contenedor, worker, adaptador y botones en el panel |
| Telegram | **Existe** | Bot propio, canal de notificación, resumen diario |
| Microsoft Teams | **Existe** | `CanalNotificacion` tipo `webhook_teams` |
| Correo (SMTP) | Parcial | Implementado, pero `EMAIL_HOST_USER` vacío en producción |
| Anthropic (diagnóstico IA) | Parcial | Requiere `ANTHROPIC_API_KEY` |
| **Odoo** | **No existe** | Solo `ODOO_HELPDESK`, que ningún código asigna. Sí se monitorea el *servicio* Odoo que usa el POS |
| **Zabbix** | **No existe** | Cero coincidencias en todo el repositorio |
| **Active Directory** | **No existe** | Solo un paso manual de checklist de apertura |
| **ESET** | **No existe** | Solo la cadena `"ESET"` buscada en el inventario de software |

### El escenario "ticket de Odoo → activo en SAIDSOFT"

De la cadena completa, SAIDSOFT tiene **todo el lado derecho** y **nada del puente**:

- **Falta** recibir el ticket: no hay endpoint de entrada ni cliente saliente.
- **Existe** resolver la farmacia por código, el activo por código o serie, el usuario
  responsable, la IP, la MAC, el técnico asignado y el historial del equipo.
- **Existe** el campo donde marcar el origen, listo y sin usar.

El trabajo real no es el modelo de datos —está hecho— sino el transporte.

### Dónde entraría Zabbix

`apps/monitoreo/adapters/base.py` define `FuenteMonitoreo`, un puerto abstracto para
fuentes de tipo pull, hoy con una implementación viva (`AdaptadorMeshCentral`). Un
`AdaptadorZabbix` sería el segundo implementador del mismo puerto.

**Antes de integrarlo conviene medir el solapamiento:** SAIDSOFT ya hace ICMP, latencia,
pérdida, tráfico SNMP y disponibilidad, **desde dentro de cada farmacia** — algo que un
Zabbix central no puede replicar.

---

## 12. Automatizaciones

23 entradas en Celery Beat. Las que producen una acción visible:

| Disparador | Acción | Frecuencia |
|---|---|---|
| 5 min sin heartbeat | Estación pasa a offline | cada 1 min |
| Métrica cruza umbral por N minutos | Abre alerta; puede abrir mantenimiento | continuo |
| Alerta abierta 30 min sin atender | Reenvía con prefijo "SIN ATENDER" | cada 10 min |
| Alerta crítica nueva | Diagnóstico con IA, una vez por incidente | al abrir |
| Enlace cambia de estado | Notifica | cada 5 min |
| Resultado de script sin respuesta | Caduca a los 10 min con motivo | cada 10 min |
| Cierre offline en conflicto | Escala para revisión manual | cada 15 min |
| Planes programados | Genera mantenimientos, scripts y escaneos | 06:00–06:30 |
| Serie coincidente | Vincula activo con estación | 04:00 |
| Retención | Purga métricas, eventos, muestras, ubicaciones | 03:00–03:40 |

> **No hay motor de automatización configurable.** Todas estas reglas están en código. Lo
> único parametrizable desde el panel es `ReglaAlerta`.

**De los casos típicos:** equipo offline → alerta OK · disco >90% → alerta OK ·
problema de Zabbix → incidente NO (no hay Zabbix) · endpoint sin antivirus → alerta NO
(el dato existe en el inventario, pero ninguna `ReglaAlerta` lo evalúa: las reglas
trabajan sobre métricas numéricas).

---

## 13. Reportes y auditoría

**Nueve exportaciones CSV** con índice propio, todas filtradas por unidad de negocio:
activos, alertas, mantenimiento, software instalado, cumplimiento, auditoría, facturación,
despliegue y viáticos consolidado. Más un resumen por cliente en pantalla.

**Dashboards:** principal, centro de monitoreo con auto-refresco, tendencia de flota,
enlaces por farmacia, errores del POS en la flota, avisos de activos (garantías, stock
bajo, movidos sin registro) y progreso en vivo de despliegues e instalaciones.

**No hay:** MTTR, disponibilidad histórica, cumplimiento de SLA, costos, reportes
programados por correo, ni exportación a Excel o PDF (salvo el informe de mantenimiento).

**Auditoría:** `EventoAuditoria` con usuario, unidad, acción, modelo, objeto, **detalle
JSON**, IP y timestamp. **133 puntos de registro.** Valor anterior/nuevo no tiene columnas
propias: depende de qué meta cada llamada en el JSON. Los cambios de permisos se hacen por
el admin de Django, que no pasa por esta bitácora.

---

## 14. Qué puede responder hoy

> Recordar el límite del encabezado: se verificó que **el código para responder existe**,
> no que haya filas cargadas.

### Responde

Cuántas farmacias y activos hay · qué activos tiene cada farmacia · qué equipo tiene cada
usuario · qué equipos están fuera de servicio · qué activos están en garantía · qué
activos no tienen responsable o ubicación · qué IP tiene un equipo · qué MikroTik
pertenece a una farmacia · qué dispositivos se ven en una farmacia (ARP) · cuánto ancho de
banda consume · qué equipos están offline y desde cuándo · qué farmacias tienen problemas
de enlace · qué alertas están activas · qué servicio del POS está caído · cuántos
mantenimientos tiene una farmacia · qué activo genera más órdenes · qué equipos tienen
ESET instalado · qué equipos tienen BitLocker y dónde está la clave · qué actualizaciones
faltan · qué usuarios tienen qué permisos.

### Responde parcialmente

| Pregunta | Por qué |
|---|---|
| Disponibilidad de una farmacia | Los datos crudos están; **ningún reporte la calcula** y la retención es de 30 días |
| MTTR | Están todos los tiempos; **no existe el reporte** |
| Qué proveedor tuvo más interrupciones | Existe `circuito_proveedor`; no hay ranking |
| Qué equipos renovar | Hay `baja_recomendada` y fecha de compra; sin política ni costo |
| Qué endpoints están desprotegidos | Se sabe si ESET **no está instalado**; no si está activo o actualizado |
| Qué software no autorizado hay | Está el inventario; falta la lista negra y la regla |

### No puede responder

| Pregunta | Por qué |
|---|---|
| Cuántos tickets/incidentes hay abiertos | **No existe el modelo** |
| Qué equipos dependen de un switch | No hay topología |
| Qué MAC tiene una estación | `Estacion` no tiene campo MAC (`Activo` sí) |
| Disponibilidad del año pasado | Retención de 30 días: **el dato ya no existe** |
| Cuánto cuesta el parque instalado | No se registran costos |
| Si se cumplió el SLA | Se define y ordena, pero no se mide |
| Qué licencias vencen | Hay catálogo de aplicaciones, no de licencias |

> **El patrón detrás de la columna del medio:** casi todas las "parciales" fallan por lo
> mismo — *el dato se está capturando y nadie escribió la consulta que lo resume*. Salvo
> un detalle que sí es estructural: **la retención de 30 días borra cualquier pregunta
> histórica más larga que un mes.**

---

## 15. Matriz de capacidades

| Capacidad | Estado | Limitación |
|---|---|---|
| Inventario | Completo | 12 tipos fijos; sin monitores |
| ITAM | Completo | Sin costos, contratos ni licencias |
| RMM | Completo | Parches se escanean, no se instalan. Sin WoL ni apagado |
| Monitoreo | Completo | Sin temperatura ni procesos en continuo |
| Acceso remoto | Completo | Dependencia de MeshCentral |
| Auditoría | Completo | Valor anterior/nuevo dentro del JSON |
| Reportes | Parcial | Sin MTTR, disponibilidad, SLA ni envío programado |
| SLA | Parcial | Se define y ordena; no se mide |
| Automatización | Parcial | Solo `ReglaAlerta` configurable |
| NOC | Parcial | Sin vista geográfica ni mural; refresco por polling |
| Helpdesk | Parcial | Exige equipo; sin portal ni correo entrante |
| ITSM | Parcial | Sin problemas, cambios ni catálogo de servicios |
| **CMDB** | Parcial | **Sin relaciones entre CIs ni análisis de impacto** |
| Zabbix · Odoo · AD · ESET · MDM | **No existe** | Ver sección 11 |

---

## 16. Estado por categoría

**A. Ya implementado.** Agente RMM completo, monitoreo de estación y enlace, SNMP,
inventario con ciclo de vida, mantenimientos con SLA y evidencia, app móvil con cola
offline, despliegues por olas, gestión de software, aperturas, cumplimiento, viáticos,
auditoría, MFA, multi-tenant, acceso remoto, notificaciones por tres canales.

**B. Implementado parcialmente.** Windows Update (escanea, no instala) · SLA (se define,
no se mide) · región y ciudad como texto · switches y APs como activo genérico.

**C. Existe en backend pero no en la UI.**

- **API REST**: viva pero exclusiva de la app móvil. No hay API pública ni documentada
  (pendiente declarado del roadmap).
- **Mantenimientos generados por una visita**: el FK `Mantenimiento.visita` existe desde
  la migración 0016 y la pantalla de visitas no los muestra.
- **`DispositivoDetectado.interfaz_indice`**: se guarda y no se usa en ninguna vista.
- **Varios modelos solo en el admin**: `CanalNotificacion`, `ConfiguracionMonitoreo`,
  `EventoSistemaVigilado`, `ServicioPosMonitoreado`, `AcuerdoNivelServicio`.

**D. Existe en UI sin backend completo.** No se encontró ningún caso.

**E. Preparado pero no implementado.** `ODOO_HELPDESK` (enumeración nunca asignada) ·
`adapters.FuenteMonitoreo` (puerto real con un solo implementador, deliberado) ·
*(ya resuelto)* la app `integraciones`.

**F. No existe.** Zabbix · AD/LDAP/SSO · Odoo como integración · ESET como integración ·
MDM · tickets y portal · problemas y cambios · topología y análisis de impacto ·
Wake-on-LAN · apagado remoto · instalación de parches · temperatura · motor de reglas
configurable · base de conocimiento · licencias · costos · catálogo de servicios ·
reportes de MTTR, disponibilidad y SLA · API pública documentada.

---

## 17. Riesgos técnicos

| Riesgo | Gravedad | Detalle |
|---|---|---|
| Límite de conexiones de EMQX | **Bloqueante** | Techo de 1024 contra ~1.800 estaciones. **Re-verificado el 28-sep:** son dos techos, `max_conns` del listener y `ulimit -n` del contenedor, ambos en 1024 |
| Worker MQTT monohilo | Alto | Único consumidor de la flota; si se atasca, **falla en silencio** |
| EMQX autoriza por lista blanca y niega mudo | Alto | El PUBACK confirma recepción, no autorización. Ya produjo cinco incidentes del mismo tipo |
| `EMAIL_HOST_USER` vacío | Alto | Los destinatarios de alerta por correo no reciben nada. **Confirmado el 28-sep** |
| `ANTHROPIC_API_KEY` vacía | Medio | **Confirmado el 28-sep:** el diagnóstico con IA nunca se ejecutó en producción |
| Restauración de respaldo sin probar | Alto | **Peor de lo estimado.** El respaldo corre a diario y funciona, pero `pg_dump` avisa en cada corrida de `circular foreign-key constraints` en `hypertable`, `chunk` y `continuous_agg`: *"might not be able to restore without --disable-triggers"*. `BACKUP_OFFSITE_DESTINO` sigue sin definir, así que la copia es única y está en la máquina que protege |
| Dependencias sin fijar | Medio | `requirements.txt` sin versiones ancladas |
| Escritura por latido en facturación | Medio | `get_or_create` en cada latido para una fila mensual |
| Desfase de reloj | Mitigado | La autocorrección de la 0.31/0.32 lo ataca |

---

## 18. Observaciones de arquitectura

1. **El hueco con más consecuencias es el de relaciones, no el de integraciones.** Cada
   integración agrega una fuente de datos; la ausencia de relaciones entre CIs bloquea
   análisis de impacto, priorización por servicio afectado y correlación de alertas. Y el
   dato crudo para empezar —`DispositivoDetectado.interfaz_indice`— **ya se está guardando
   sin usar**.
2. **Antes de integrar Zabbix conviene medir el solapamiento.** La pregunta útil no es
   "¿integramos Zabbix?" sino "¿qué mide Zabbix que nosotros no, y desde dónde lo mide?".
3. **El puerto de integración ya existe y conviene respetarlo.** `FuenteMonitoreo` tiene
   una implementación viva que lo mantiene honesto — mejor precedente que el registro
   genérico que se retiró.
4. **La decisión de fondo: ¿ticket o mantenimiento?** Integrar el helpdesk de Odoo obliga
   a elegir entre relajar la regla de "al menos un equipo" —que toca nueve modelos, el
   conflicto de "ya hay uno abierto" y el snapshot— o introducir una entidad nueva por
   encima. Es una decisión de modelo, no de transporte.
5. **Dos deudas operativas valen más que cualquier función nueva.** El techo de EMQX
   bloquea el despliegue a la cadena completa, y la restauración nunca se probó contra
   hypertables.

---

## Uso real — medido el 28-sep-2026

Con acceso al servidor, esto es lo que hay cargado. **La foto del código estaba completa;
esta es la del uso.**

| | Medido | Contexto |
|---|---|---|
| Farmacias activas | **701** | El modelo está poblado |
| Estaciones | **42** (40 aprobadas) | de ~1.800 objetivo |
| **Agentes vivos** | **29** con heartbeat < 15 min | Eran 8 al 7-sep: subió 3,6× |
| Versiones en la flota | 0.32 → 38 · 0.29 → 3 · 0.31 → 1 | El despliegue por olas funciona |
| Activos | **21** | El ITAM está construido y vacío |
| Colaboradores | **10** | |
| Mantenimientos · visitas | **3 · 1** | |
| Alertas históricas / abiertas | **184 / 3** | El motor de alertas sí funciona |
| `ActividadPlanificada` | **0** | Resuelve la duda de [`tres-agendas.md`](tres-agendas.md): sus tres capacidades propias son teóricas |
| Zonas de viáticos | **0** | La alerta "fuera de zona" no puede dispararse |
| Tamaño de la base | **136 MB** | La tabla más grande es `farmacia` con 1 MB |

### La conclusión que faltaba

**El desafío no es técnico, es de adopción.** La plataforma tiene 97 modelos, 176 vistas y
1.806 pruebas para sostener 21 activos, 10 colaboradores y 3 mantenimientos. Las 184
alertas y los 29 agentes vivos prueban que el núcleo RMM **funciona de verdad** — es el
resto lo que está esperando que alguien lo use.
