# Propuesta de Gestión de Mantenimiento de TI — CRESIO (SG / MIA / 7DIAS)

**Alcance de esta propuesta:** departamento de soporte TI corporativo con ~600
sucursales, ~1.800 equipos de punto de venta/administrativos, 9 técnicos de campo + 2
supervisores regionales, servicio de mesa de ayuda de primera línea, y una plataforma
propia (SAIDSOFT) que ya cubre buena parte de este proceso.

**Nota de método:** esta no es una propuesta desde cero. SAIDSOFT ya tiene un módulo de
Mantenimiento y uno de Activos en producción (`apps.mantenimiento`, `apps.activos`), con
checklist, firma digital, fotos, repuestos, informe PDF y una API móvil para técnicos.
Cada sección marca explícitamente **qué ya existe**, **qué existe parcial** y **qué
falta construir** — para que esto sea un plan accionable, no un documento de referencia
genérico que alguien tiene que traducir a trabajo real después.

---

## 1. Objetivo

Estandarizar cómo CRESIO planifica, ejecuta, registra y controla el mantenimiento de
sus equipos de TI (PDV y administrativos) en todas sus sucursales, de forma que:

- Todo mantenimiento quede con evidencia trazable (quién, cuándo, qué se hizo, qué se
  reemplazó, quién lo validó).
- El estado real de cada equipo sea consultable en cualquier momento, no solo al
  momento de una falla.
- El costo y la carga de trabajo del área de soporte sean medibles con indicadores
  objetivos, no percepción.
- El proceso funcione igual de bien para el técnico que visita una farmacia en Loja que
  para el que atiende oficinas administrativas en Guayaquil.

## 2. Alcance

| Tipo de equipo | Estado en SAIDSOFT hoy |
|---|---|
| Computador de escritorio | ✅ `Activo.Tipo.DESKTOP` |
| Laptop | ✅ `Activo.Tipo.LAPTOP` |
| Mini PC / NUC | ❌ no existe como tipo propio — hoy se registraría como `DESKTOP` a secas, sin distinguir el factor de forma |
| Monitor | ❌ no existe como tipo propio |
| Impresoras y periféricos | ✅ `Activo.Tipo.IMPRESORA`; "periféricos" (teclado/mouse) no tienen tipo propio — hoy no se individualizan como activo, son accesorios implícitos del equipo |
| Equipos de punto de venta (PDV) | ✅ es el mismo `Activo` (LAPTOP/DESKTOP) de una farmacia, vinculado además a una `Estacion` RMM por número de serie — dos vistas del mismo equipo físico |
| Equipos administrativos | ✅ igual que PDV, sin vínculo a `Estacion` si no corren el agente RMM |
| Servidor / Red / Tablet / UPS / Teléfono / Cámara | ✅ ya existen como tipos (`SRV`, `NET`, `TAB`, `UPS`, `TEL`, `CAM`) |
| Otros dispositivos futuros | 🔶 `Activo.Tipo` es un `TextChoices` cerrado en código — agregar un tipo nuevo hoy requiere una migración, no es autoservicio |

**Recomendación de alcance inmediata**: agregar `MINI_PC` y `MONITOR` a `Activo.Tipo`
(cambio de una línea + migración, bajo riesgo) antes de lanzar el proceso, para no
forzar a los técnicos a registrar un monitor como "desktop" por falta de opción.

### Tipos de mantenimiento — brecha real más importante de esta sección

El sistema hoy **no distingue** preventivo / correctivo / por falla / por actualización
/ por obsolescencia como un dato estructurado. Lo que existe:

- `Mantenimiento.tipo_origen`: solo 3 valores — `manual`, `programado`, `odoo_helpdesk`.
  Esto describe **quién disparó** el mantenimiento, no **por qué**.
- `Mantenimiento.tipo_mantenimiento`: `CharField(30)` de texto libre, sin catálogo — hoy
  cualquiera escribe lo que quiera ("preventivo", "Preventivo", "PREV", "revisión"...),
  lo que hace **imposible** filtrar o reportar por tipo de forma confiable.

**Esto es exactamente el problema que el punto 11 del pedido ya identifica** ("los
checklists y tipos de mantenimiento deben ser configurables, no hardcodeados"). La
solución no es agregar un `TextChoices` más en Python (eso sería hardcodear de nuevo) —
ver punto 14 (modelo de datos) para la propuesta real: un catálogo `TipoMantenimiento`
en base de datos, administrable, con los 5 valores de este pedido como semilla inicial.

## 3. Tipos de mantenimiento (definición conceptual)

| Tipo | Cuándo aplica | Dispara la creación de un `Mantenimiento` |
|---|---|---|
| **Preventivo** | Programado por frecuencia, sin falla reportada | `MantenimientoProgramado` vencido (ya automatizado, `generar_mantenimientos_vencidos`) |
| **Correctivo** | El equipo funciona pero con una falla puntual reportada | Manual, por mesa de ayuda o el técnico |
| **Por falla crítica** | El equipo dejó de operar (PDV caído, no enciende) | Manual, prioridad urgente — hoy `PrioridadActividad.URGENTE` existe para `ActividadPlanificada`, no está enlazada a `Mantenimiento` directamente |
| **Por actualización tecnológica** | Cambio de SO, upgrade de RAM/disco, migración de versión de POS | Manual, casi siempre planificado en anillos (mismo patrón que `Despliegue` de software) |
| **Por obsolescencia** | El equipo ya cumplió su vida útil o quedó fuera de soporte | Debe originarse desde la clasificación de estado (punto 5), no desde una falla |

## 4. Información del activo — campos y su estado real

| Campo pedido | ¿Existe hoy? | Dónde | Obligatorio |
|---|---|---|---|
| Código de activo | ✅ | `Activo.codigo` (autogenerado `CR-TIPO-NNNN`) | Sí (autogenerado) |
| Número de serie | ✅ | `Activo.numero_serie` | Recomendado — sin él no hay vínculo automático con RMM |
| Marca | ✅ | `Activo.marca` (FK) | No |
| Modelo | ✅ | `Activo.modelo` | No |
| Tipo de equipo | ✅ | `Activo.tipo` | Sí |
| Usuario asignado | ✅ | `Activo.colaborador_actual` | No (solo si está `ASIGNADO`) |
| Área / Departamento | 🔶 | Indirecto vía `Colaborador.cargo.departamento` | — |
| Sucursal | 🔶 | Indirecto: `Colaborador.sucursal` (texto libre) o, si es un PDV, `Activo.estacion.farmacia` | — |
| Ubicación física | 🔶 | `Ubicacion` existe como catálogo, pero cuelga de `Colaborador`, no directo de `Activo` — un activo en bodega no tiene "ubicación física" más allá de `bodega_actual` | — |
| Sistema operativo | 🔶 | Solo si el equipo tiene agente RMM (`Activo.estacion.so_nombre`); un monitor, impresora o PC administrativa sin agente no lo tiene | No |
| Procesador / RAM / Almacenamiento | ✅ | `Activo.procesador/ram_gb/almacenamiento_gb` | No (solo aplica a equipos de cómputo) |
| Estado del equipo | ✅ | `Activo.estado` (en_bodega/asignado/en_reparacion/dado_de_baja) — **es un estado de ciclo de vida, no de salud**, ver punto 5 | Sí |
| Fecha de adquisición | ✅ | `Activo.fecha_compra` | No |
| Garantía | ✅ | `Activo.vencimiento_garantia` | No |
| Fecha del último mantenimiento | 🔶 | Solo si tiene un `MantenimientoProgramado` (`fecha_ultimo`); un equipo sin plan preventivo no tiene este dato consolidado — hay que calcularlo del historial | — |
| Fecha del próximo mantenimiento | 🔶 | Mismo caso — solo vía `MantenimientoProgramado.fecha_proximo` | — |
| Responsable técnico | ✅ | `Mantenimiento.tecnico` (por intervención) — no existe un "técnico titular" fijo por activo | No |
| Observaciones | ❌ | No existe un campo persistente en `Activo` — solo detalle puntual dentro de cada `EventoActivo`/`Mantenimiento` | — |

**Campos obligatorios recomendados para el alta de cualquier activo**: código
(automático), tipo, marca, modelo, número de serie (si aplica), unidad de negocio,
estado. El resto es opcional y se completa según el tipo de equipo — un monitor no
necesita procesador/RAM, un servidor no necesita "usuario asignado".

## 5. Checklist de mantenimiento preventivo

**Ya está resuelto como arquitectura**: `ActividadChecklist` es exactamente el catálogo
configurable que pide el punto 11 — cada ítem tiene nombre, orden, si está activo, y a
qué categorías de equipo aplica (`categorias` M2M a `CategoriaEquipo`). No hace falta
código nuevo para esto, hace falta **cargar los ítems** (ver semilla abajo) y, si se
quiere ir más allá, agregar una sección (hardware/software/seguridad) como campo del
ítem para agrupar visualmente en el checklist — hoy todos los ítems se listan planos.

### Hardware

| Ítem | Aplica a |
|---|---|
| Inspección física general (golpes, deformaciones) | Todos |
| Limpieza externa (carcasa, pantalla, teclado) | Todos |
| Limpieza interna (aire comprimido, disipadores) | Desktop, Laptop, Mini PC, Servidor |
| Revisión de ventiladores / disipación | Desktop, Laptop, Mini PC, Servidor |
| Medición de temperatura en carga | Desktop, Laptop, Mini PC, Servidor |
| Estado de cables de poder/datos (sin daño, bien conectados) | Todos |
| Estado de la fuente de alimentación | Desktop, Servidor |
| Revisión de memoria RAM (asiento, test básico) | Desktop, Laptop, Mini PC, Servidor |
| Estado del disco (HDD/SSD/NVMe — S.M.A.R.T.) | Desktop, Laptop, Mini PC, Servidor |
| Prueba de puertos USB | Desktop, Laptop, Mini PC |
| Prueba de puerto de red / conector RJ45 | Todos los que llevan cable de red |
| Estado del monitor (píxeles, brillo, cable) | Monitor, Desktop, Laptop |
| Prueba de teclado (todas las teclas) | Desktop, Laptop |
| Prueba de mouse | Desktop |
| Estado de UPS/regulador (batería, tiempo de respaldo) | PDV, Servidor |

### Software

| Ítem | Aplica a |
|---|---|
| Estado general del sistema operativo (arranque, estabilidad) | Todos con SO |
| Actualizaciones de Windows pendientes | Todos con SO — ya automatizable vía `escanear_actualizaciones` (RMM) |
| Controladores desactualizados o con error | Todos con SO |
| Antivirus/ESET activo y actualizado | Todos con SO |
| Escaneo de malware/amenazas | Todos con SO |
| Revisión de aplicaciones instaladas vs. autorizadas | Todos con SO — ya cubierto por `apps.software.SoftwareInstaladoDetectado` en equipos con agente |
| Aplicaciones obsoletas o sin soporte | Todos con SO — ya cubierto por "software desactualizado" (RMM) |
| Espacio disponible en disco | Todos con SO |
| Revisión de programas de inicio | Todos con SO |
| Servicios innecesarios corriendo | Todos con SO |
| Sincronización de fecha/hora | Todos con SO |
| Conectividad de red (LAN/Wi-Fi) | Todos con SO |
| Acceso a recursos corporativos (unidades de red, VPN) | Equipos administrativos |
| Funcionamiento del POS / aplicación crítica | PDV |

### Seguridad

| Ítem | Aplica a |
|---|---|
| Antivirus activo (verificación, no solo instalado) | Todos con SO |
| Equipo integrado al dominio (si corresponde) | Equipos administrativos |
| Políticas de seguridad aplicadas (GPO) | Equipos administrativos |
| Usuario correctamente asignado en el sistema | Todos |
| Bloqueo automático de pantalla configurado | Todos con SO |
| Actualizaciones de seguridad al día | Todos con SO |
| MFA/2FA activo donde corresponda | Cuentas de usuario, no el equipo en sí |
| BitLocker activo (si la política lo exige) | Todos con SO — ya monitoreado en tiempo real por RMM, no requiere checklist manual |
| Software no autorizado instalado | Todos con SO |
| Puertos/configuraciones inseguras (RDP abierto, compartidos sin contraseña) | Todos con SO |

> Nota importante: varios ítems de esta lista (actualizaciones de Windows, software
> instalado/desactualizado, BitLocker, antivirus) **ya se monitorean automáticamente**
> en los equipos con el agente RMM instalado — no tiene sentido pedirle al técnico que
> los revise a mano en esos casos. El checklist manual debería aplicar completo solo a
> equipos **sin agente** (impresoras, monitores, equipos legacy); en equipos con agente,
> el checklist puede pre-marcar esos ítems con el dato ya conocido del sistema y dejar
> solo lo que de verdad requiere inspección física.

## 6. Flujo de mantenimiento correctivo

| Paso | Ya implementado como | Responsable |
|---|---|---|
| 1. Identificación del problema | Reporte de usuario/mesa de ayuda o hallazgo del técnico | Usuario final / Mesa de Ayuda |
| 2. Registro del incidente | `crear_mantenimiento_manual` (o vía Helpdesk externo, `tipo_origen=odoo_helpdesk`) | Mesa de Ayuda / Técnico |
| 3. Diagnóstico | 🔶 hoy es texto libre en `descripcion` — no hay un campo estructurado de diagnóstico | Técnico |
| 4. Clasificación de la falla | ❌ no existe (ver tipo de mantenimiento, punto 3) | Técnico |
| 5. Determinación de causa raíz | ❌ no existe campo propio — se mezclaría hoy en `descripcion` o en el `detalle_motivo` del evento | Técnico |
| 6. Reparación | Checklist + repuestos (`registrar_repuesto_utilizado`) | Técnico |
| 7. Pruebas | 🔶 implícito, sin checklist de "prueba post-reparación" distinto del preventivo | Técnico |
| 8. Validación con el usuario | `FirmaMantenimiento(tipo_firma=custodio)` — ya existe | Usuario final / Custodio |
| 9. Registro de componentes reemplazados | `RepuestoUtilizado` — ya existe, con costo | Técnico |
| 10. Cierre del mantenimiento | `cerrar_mantenimiento` (ya construido esta misma sesión: si el resultado es exitoso, el activo vuelve a bodega automáticamente) | Técnico / Supervisor |

**Brecha real**: pasos 3-5 (diagnóstico, clasificación, causa raíz) no tienen campos
propios — hoy todo cae en un `descripcion` de texto libre. Para reportar "fallas
recurrentes" (KPI pedido en el punto 8) de forma confiable, hace falta al menos un
campo de **categoría de falla** (catálogo: fuente de poder, disco, RAM, software,
red, pantalla, periférico, otro) y un campo de **causa raíz** en el cierre.

### Criterios de escalamiento a proveedor externo

Escalar quando **cualquiera** de estas condiciones se cumpla:
- La falla requiere un repuesto que CRESIO no mantiene en stock y el proveedor ofrece
  garantía vigente sobre esa pieza.
- El diagnóstico requiere herramienta/certificación que el técnico de campo no tiene
  (ej. soldadura de placa, recuperación de datos).
- El equipo sigue en garantía del fabricante — reparar internamente la anularía.

`ResultadoTecnico.ESCALADO_A_PROVEEDOR` ya existe en el modelo — falta solo el criterio
documentado (arriba) para que todos los técnicos lo apliquen igual.

### Criterios de recomendación de reemplazo (`REQUIERE_BAJA` / `IRREPARABLE`)

Recomendar baja cuando **dos o más** de estas condiciones se cumplan:
- El costo de reparación supera el 50% del valor de un equipo equivalente nuevo.
- El equipo ya tuvo 3 o más correctivos en los últimos 12 meses (falla recurrente).
- El equipo superó su vida útil de referencia (ver tabla de frecuencias, punto 8) y
  además presenta la falla actual.
- El fabricante ya no distribuye repuestos para ese modelo.

Esto **ya está automatizado en código** (`cerrar_mantenimiento` con `resultado_tecnico
in (REQUIERE_BAJA, IRREPARABLE)` marca `Activo.baja_recomendada=True`) — falta
solamente que el criterio de negocio (arriba) quede documentado para que el técnico lo
aplique de forma consistente, no a criterio personal.

## 7. Clasificación del estado del equipo

| Estado | Criterio objetivo |
|---|---|
| 🟢 **Óptimo** | Sin fallas en los últimos 6 meses; último preventivo dentro del plazo; sin software/SO desactualizado |
| 🟡 **Requiere atención** | 1 correctivo menor en los últimos 6 meses, o preventivo vencido por menos de 30 días, o software desactualizado sin impacto funcional |
| 🟠 **Requiere intervención** | 2 correctivos en los últimos 6 meses, o preventivo vencido por más de 30 días, o hardware con síntomas (ruido, sobrecalentamiento, reinicios) sin falla total todavía |
| 🔴 **Crítico** | Equipo caído (no enciende / no procesa ventas si es PDV) o 3+ correctivos en 6 meses |
| ⚫ **Requiere reemplazo** | Cumple los criterios de baja recomendada del punto 6, o superó su vida útil de referencia |

**Brecha real**: esta clasificación de 5 niveles **no existe como campo persistente**
en el modelo actual. `Activo.estado` es ciclo de vida (bodega/asignado/reparación/baja),
no salud; `Mantenimiento.estado_general` es solo un snapshot de 3 valores tomado en el
momento de una intervención puntual, no un estado vivo del equipo. Se necesita un campo
nuevo — ver modelo de datos (punto 14) — calculado, no editado a mano, a partir del
historial real (heartbeat RMM si aplica, conteo de correctivos, vencimiento de
preventivo).

## 8. Frecuencia de mantenimiento

| Criticidad | Tipo de equipo | Frecuencia | Justificación |
|---|---|---|---|
| **Alta** | PDV (POS activo en farmacia) | Trimestral (90 días) | Downtime = venta perdida en el momento; polvo/uso intensivo en ambiente de mostrador |
| **Alta** | Servidores / infraestructura de red | Trimestral (90 días) | Afecta a toda una sucursal o a la cadena si es central |
| **Media** | Equipos administrativos (oficina) | Semestral (180 días) | Uso de oficina, ambiente controlado, impacto limitado a una persona |
| **Media** | Laptops de técnicos/gerencia | Semestral (180 días) | Se mueven físicamente más, pero no paran una venta |
| **Baja** | Impresoras, monitores, periféricos | Anual (365 días) | Menor complejidad interna, falla de forma más predecible (consumibles) |
| **Baja** | Equipos de respaldo/bodega sin uso activo | Anual, o al momento de asignarse | No genera desgaste mientras está en bodega |

Estos valores alimentan directo `MantenimientoProgramado.frecuencia_dias` (ya soporta
cualquier número de días) — no requiere cambio de código, solo la política de negocio
documentada arriba al momento de crear cada plan.

## 9. Evidencia

| Evidencia pedida | Estado |
|---|---|
| Checklist completado | ✅ `ActividadRealizada` |
| Fotografías antes/después | ✅ `ImagenMantenimiento` (no distingue "antes" de "después" explícitamente — recomendación: usar el nombre de archivo o un campo `momento` para etiquetarlas) |
| Diagnóstico | 🔶 texto libre en `descripcion` (ver punto 6) |
| Componentes reemplazados | ✅ `RepuestoUtilizado` |
| Software actualizado | 🔶 implícito si se corrió `escanear_actualizaciones`/instalación desde RMM, no queda un registro explícito dentro del propio `Mantenimiento` |
| Problemas encontrados | 🔶 mezclado en `descripcion` |
| Recomendaciones | ❌ no hay campo propio — hoy solo existe la recomendación implícita de baja (`baja_recomendada`) |
| Nombre del técnico | ✅ `Mantenimiento.tecnico` |
| Fecha y hora | ✅ `fecha_creacion`, `fecha_cierre`, cada `EventoMantenimiento.timestamp` |
| Firma o aprobación | ✅ `FirmaMantenimiento` (custodio y/o técnico) |
| Estado final del equipo | ✅ `resultado_tecnico` + `estado_general` (y desde esta sesión, el `Activo.estado` real refleja el retorno a bodega automáticamente) |

**Ya existe además** un informe PDF generado automáticamente (`generar_informe_pdf`) que
consolida checklist, firmas, fotos y repuestos — la evidencia no vive dispersa, se puede
entregar como un solo documento por intervención.

## 10. Indicadores KPI

Ninguno de estos KPIs está calculado ni expuesto en un dashboard hoy — es la brecha más
grande de todo el proceso actual. Fórmulas propuestas:

| KPI | Fórmula |
|---|---|
| % de mantenimientos ejecutados | (Mantenimientos programados cerrados en el período / Mantenimientos programados que vencían en el período) × 100 |
| % de mantenimientos atrasados | (Planes con `fecha_proximo` < hoy y sin `Mantenimiento` generado o sin cerrar / Total de planes activos) × 100 |
| % de equipos en estado óptimo | (Activos en 🟢 / Total de activos activos) × 100 |
| % de equipos con problemas | (Activos en 🟠 o 🔴 / Total de activos activos) × 100 |
| Número de fallas por sucursal | COUNT(Mantenimiento correctivo) agrupado por `Activo.estacion.farmacia` (o `Colaborador.sucursal` si es administrativo), por período |
| Número de fallas recurrentes | COUNT(Activos con ≥2 `Mantenimiento` correctivo en los últimos 6 meses) |
| MTTR (tiempo medio de reparación) | AVG(`fecha_cierre` − `fecha_creacion`) de mantenimientos correctivos cerrados en el período |
| MTBF (tiempo medio entre fallas) | AVG(días entre el cierre de un correctivo y el inicio del siguiente) por activo, promediado |
| Número de equipos obsoletos | COUNT(Activos que superaron su vida útil de referencia, punto 8) |
| Número de equipos que requieren reemplazo | COUNT(`Activo.baja_recomendada=True` y `estado != dado_de_baja`) — **calculable hoy mismo, sin cambios de modelo** |
| Cumplimiento del mantenimiento preventivo | 1 − (% de mantenimientos atrasados) |
| Costo de mantenimiento por equipo | SUM(`RepuestoUtilizado.costo_total` de todos los mantenimientos de ese activo) / vida útil transcurrida (o por período) |
| Costo de repuestos | SUM(`RepuestoUtilizado.costo_total`) por período, por sucursal o por tipo de equipo |

Todos son calculables con los datos que **ya se están capturando** (`Mantenimiento`,
`EventoMantenimiento`, `RepuestoUtilizado`) — lo que falta es la capa de agregación y un
dashboard, no más captura de datos.

## 11. Flujo del proceso

```
Planificación → Asignación → Diagnóstico → Mantenimiento → Pruebas → Validación → Evidencia → Cierre → Indicadores
```

| Etapa | Qué sucede | Responsable | Soporte en SAIDSOFT |
|---|---|---|---|
| Planificación | Se define qué equipo necesita mantenimiento (por plan preventivo vencido, o por reporte de falla) | Coordinador de TI / Supervisor | `MantenimientoProgramado` + `generar_mantenimientos_vencidos` (automático) |
| Asignación | Se asigna un técnico según zona/disponibilidad | Supervisor de soporte | `Mantenimiento.tecnico` |
| Diagnóstico | El técnico revisa el equipo y determina el alcance real | Técnico | Campo `descripcion` (a estructurar, punto 6) |
| Mantenimiento | Ejecución del checklist, reemplazo de repuestos si aplica | Técnico | `ActividadChecklist`/`RepuestoUtilizado` |
| Pruebas | Verificación de que el equipo quedó operativo | Técnico | Falta un checklist de "prueba final" distinto del preventivo |
| Validación | El usuario/custodio confirma que el equipo funciona | Usuario final | `FirmaMantenimiento(custodio)` |
| Evidencia | Fotos, checklist, firma, repuestos quedan consolidados | Técnico | `generar_informe_pdf` |
| Cierre | Se registra resultado técnico, el activo vuelve a bodega si corresponde | Técnico / Supervisor | `cerrar_mantenimiento` |
| Indicadores | Se recalculan KPIs para seguimiento gerencial | Coordinador de TI | ❌ pendiente (punto 10) |

## 12. Roles y responsabilidades

| Rol | Responsabilidad | Rol/grupo en SAIDSOFT hoy |
|---|---|---|
| **Coordinador de TI** | Define políticas (frecuencias, catálogos, criterios de escalamiento), revisa KPIs | `Administrador` |
| **Supervisor de soporte** (regional) | Asigna técnicos, aprueba resultados críticos, revisa mantenimientos de su equipo | `Soporte Técnico` + permiso individual de aprobación (ya implementado esta sesión) |
| **Técnico de soporte** | Ejecuta el mantenimiento, llena checklist, registra evidencia | `Soporte Técnico` / `Técnico`, con acceso a `apps.mantenimiento` (ya habilitado esta sesión) + app móvil |
| **Usuario final** | Reporta fallas, valida que el equipo quedó operativo (firma) | ❌ sin acceso al sistema hoy — la firma se captura en el dispositivo del técnico, no desde un portal propio del usuario |
| **Responsable de sucursal** | Da seguimiento a los equipos de su local, prioriza urgencias | ❌ sin rol propio hoy — hoy esa función la cubre el técnico asignado a la zona (`Farmacia.tecnico_asignado`) |
| **Proveedor externo** | Repara lo escalado, informa tiempos y garantía | ❌ sin acceso al sistema — la interacción es manual/fuera de SAIDSOFT, solo queda registrado el resultado (`ESCALADO_A_PROVEEDOR`) |

## 13. Requerimientos funcionales para la plataforma — estado real

| Requerimiento pedido | Estado |
|---|---|
| Módulo de activos | ✅ `apps.activos` |
| Módulo de mantenimiento | ✅ `apps.mantenimiento` |
| Checklist configurable | ✅ `ActividadChecklist` (falta seedear contenido + agrupar por sección) |
| Programación automática | ✅ `MantenimientoProgramado` + tarea periódica |
| Asignación de técnicos | ✅ `Mantenimiento.tecnico` |
| Alertas de mantenimientos próximos | ❌ no existe — `Notificacion` existe como modelo pero nada la dispara para "vence en N días" |
| Alertas de mantenimientos vencidos | ❌ no existe — mismo caso |
| Historial por activo | ✅ `Activo.mantenimientos` + `EventoActivo` |
| Historial por sucursal | 🔶 posible por consulta, pero no hay una vista/reporte dedicada agrupando por farmacia/sucursal |
| Evidencias | ✅ fotos, firma, informe PDF |
| Repuestos | ✅ `RepuestoUtilizado`, con descuento real de stock si se indica bodega |
| Costos | 🔶 se capturan por repuesto, no hay agregación por equipo/sucursal/período expuesta |
| Dashboard | ❌ no existe un dashboard propio de mantenimiento (el dashboard general del panel no lo incluye) |
| Reportes | 🔶 hay reportes CSV para otros módulos (activos, alertas, facturación) pero no uno de mantenimiento |
| KPIs | ❌ ver punto 10 |
| Auditoría de cambios | ✅ `apps.auditoria` + `EventoMantenimiento` inmutable |

**Prioridad de cierre de brechas recomendada** (de mayor a menor impacto/esfuerzo):
1. Alertas de próximo/vencido (reutiliza `Notificacion` ya existente + una tarea Celery diaria).
2. Dashboard + reportes CSV de mantenimiento (mismo patrón ya usado en `apps.panel.reportes` para otros módulos).
3. Catálogo `TipoMantenimiento` configurable (reemplaza el texto libre actual).
4. Clasificación de estado 🟢🟡🟠🔴⚫ persistente por activo.
5. Campos estructurados de diagnóstico/causa raíz/recomendación en el cierre.

## 14. Modelo de datos recomendado

**Base**: todo lo que ya existe en `apps.mantenimiento`/`apps.activos` se mantiene tal
cual — es sólido y ya está probado en producción. Se agrega:

```
TipoMantenimiento (nuevo catálogo, reemplaza Mantenimiento.tipo_mantenimiento de texto libre)
  - codigo, nombre (preventivo/correctivo/falla_critica/actualizacion/obsolescencia)
  - activo: bool

CategoriaFalla (nuevo catálogo, para clasificar correctivos)
  - nombre (fuente de poder, disco, RAM, software, red, pantalla, periférico, otro)

Mantenimiento (campos nuevos sobre el modelo ya existente)
  - tipo_mantenimiento: FK a TipoMantenimiento (en vez de CharField libre)
  - categoria_falla: FK a CategoriaFalla, null=True (solo aplica a correctivos)
  - diagnostico: TextField (qué se encontró)
  - causa_raiz: TextField, blank (qué lo originó)
  - recomendacion: TextField, blank (qué se sugiere a futuro)

Activo (campos nuevos)
  - estado_salud: CharField, choices (optimo/atencion/intervencion/critico/reemplazo)
    -- calculado por una tarea periódica a partir del historial, no editable a mano
  - fecha_ultimo_mantenimiento / fecha_proximo_mantenimiento: denormalizados desde
    Mantenimiento/MantenimientoProgramado, para no recalcular en cada consulta de listado
  - observaciones: TextField, blank

AlertaMantenimiento (nuevo, mismo patrón que apps.monitoreo.Alerta)
  - mantenimiento_programado: FK
  - tipo: proximo_a_vencer / vencido
  - estado: abierta/reconocida/resuelta
  - abierta_en, reconocida_por, resuelta_en
```

No se propone tocar `EventoMantenimiento`, `FirmaMantenimiento`, `ImagenMantenimiento`,
`RepuestoUtilizado` — ya cumplen su función tal como están.

## 15. Ejemplo de orden de mantenimiento completa

> Datos de ejemplo, no corresponden a una intervención real.

```
Mantenimiento #482
Tipo:              Correctivo — Falla crítica
Categoría de falla: Fuente de poder
Cliente:           Farmacia ML014 (San Gregorio)
Equipo principal:  CR-DSK-0231 — HP ProDesk 400 G6, N/S HP4X920211
Técnico:           [nombre del técnico de zona]
Origen:            Manual (reporte telefónico del administrador de local)
Prioridad:         Urgente (PDV caído)

--- Diagnóstico ---
El equipo no enciende. Se descarta corte de energía (otros equipos del local
funcionan). Se abre el gabinete: fuente de poder no responde al test de encendido.

Causa raíz: fuente de poder quemada, posible pico de tensión — el local reporta
cortes de luz frecuentes en la última semana.

--- Checklist ejecutado ---
[x] Inspección física          [x] Estado de cables         [x] Fuente de alimentación
[x] Memoria RAM (test)         [x] Disco (S.M.A.R.T.)       [x] Puertos USB
[x] Puerto de red              [ ] Monitor (no aplicaba, se probó con otro)
[x] Prueba final tras reparación

--- Repuestos utilizados ---
1x Fuente de poder 500W — Bodega Central Machala — $28.50

--- Recomendación ---
Instalar UPS/regulador en este punto — segundo caso de fuente quemada en ML014
en 4 meses (ver Mantenimiento #398). Candidato a revisión de instalación eléctrica
del local, no solo del equipo.

--- Resultado ---
Resultado técnico:   Reparado
Estado general:      Operativo
Estado físico final: Bueno
El activo vuelve a "En bodega" → se reasigna al administrador del local.

--- Evidencia ---
- 3 fotografías (antes: fuente dañada / durante: reemplazo / después: equipo encendido)
- Firma del custodio (administrador de local): registrada
- Firma del técnico: registrada
- Informe PDF: generado, adjunto al correo de cierre

Tiempo real de intervención: 55 minutos
Cerrado por: [supervisor regional] — 2026-08-23 16:40
```

## 16. Recomendaciones para implementar en una organización multi-sucursal

1. **No dupliques lo que ya tienes.** El proceso descrito arriba ya vive en un 70-80%
   dentro de SAIDSOFT — la tentación en este tipo de proyectos es comprar/adoptar una
   herramienta externa de gestión de mantenimiento; eso significaría mantener dos
   sistemas de activos desincronizados (el RMM ya sabe qué equipo es cuál).
2. **Arranca por un piloto de una región**, no las ~600 sucursales de una vez —
   igual que se hizo con el rollout del agente RMM. Usa la zona de un solo técnico
   para validar frecuencias y checklist antes de generalizar.
3. **Prioriza cerrar las alertas de vencimiento antes que el dashboard.** Sin alertas,
   ningún plan preventivo se ejecuta a tiempo por más bonito que sea el reporte
   después — es la brecha de mayor impacto operativo inmediato (punto 13).
4. **Conecta el checklist a si el equipo tiene agente RMM o no.** Pedirle a un técnico
   que revise a mano algo que el sistema ya sabe (Windows Update, antivirus, software
   instalado) es trabajo duplicado y genera checklists llenados "por cumplir" sin
   mirar de verdad.
5. **La app móvil de técnicos ya existe** (`apps.mantenimiento.api_urls`) — antes de
   evaluar cualquier app de terceros para el trabajo de campo, valida si ya cubre el
   flujo real (ver/iniciar/cerrar mantenimiento, checklist, firma, fotos).
6. **Los criterios objetivos de este documento (escalamiento, baja, clasificación de
   estado) deben quedar como configuración**, no como conocimiento tácito de cada
   técnico — es exactamente el motivo por el que este documento los deja explícitos.
7. **Mide antes de prometer un SLA.** Sin MTTR/MTBF calculados hoy, cualquier acuerdo
   de tiempo de respuesta con las sucursales sería una suposición — cierra el punto 10
   antes de comprometer tiempos hacia el negocio.
