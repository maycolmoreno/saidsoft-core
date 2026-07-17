# Plan de Modernización SAIDSOFT

**Fecha:** 16 de julio de 2026
**Alcance:** ~600 farmacias · ~1.800 estaciones Windows · servidor propio (LAN/VPN)

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

| Fase | Contenido | Duración |
|---|---|---|
| **0. Preparación** | Backup BD, Git, rotar credenciales, variables de entorno, documentar tópicos | 1 sem |
| **1. Núcleo** | Django 5.2/Py 3.12, modelo nuevo (grupos/farmacias/estaciones/despliegues/auditoría), worker MQTT Python (absorbe Node.js), Celery Beat reemplaza kronos | 2-3 sem |
| **2. Panel despliegues** | HTMX + Tailwind, dashboard tiempo real (Channels), matriz de versiones por TRX, anillos y olas, aprobación cuatro ojos, reportes | 2-3 sem |
| **2b. Módulo activos** | OC, bodegas, activos CR, consumibles, asignaciones, etiquetas, reportes CRESIO | 3-4 sem |
| **3. Agente .NET 10** | Windows Service self-contained, MQTTnet, auto-enrolamiento por token, ciclo descarga/verifica/aplica/rollback, MSI para GPO, auto-actualización | 3-4 sem |
| **4. Integración POS** | Librería Saidsoft.Client, heartbeat con versión/serie, vinculación activo↔estación, flujo cierre-actualiza-relanza | 2 sem |
| **5. Despliegue** | docker-compose (Django+worker+PostgreSQL+EMQX/TLS), piloto 2-3 farmacias (incluir el Win10 más viejo), rollout por anillos, respaldos | 1-2 sem |

**Total: 16-20 semanas.** Prioridad acordada: Módulo A (despliegues) primero; Módulo B (activos) después de la Fase 2.

## 8. Riesgos y mitigaciones

| Riesgo | Mitigación |
|---|---|
| Actualización mala llega a 600 farmacias | Anillos + freno automático por umbral de error + rollback local |
| VPN saturada por descargas masivas | Olas escalonadas + throttling; rol caché en -ADM previsto como plan B |
| Win10 builds viejos fallan con el agente | Self-contained .NET 10; piloto incluye la máquina más antigua; mínimo build 1607 |
| Windows 10 sin soporte (oct-2025) | El inventario de SO del heartbeat alimenta el plan de migración a W11 |
| Instalar agente en 1.800 equipos | MSI + GPO/script, una sola vez; después se auto-actualiza por su propio canal |
