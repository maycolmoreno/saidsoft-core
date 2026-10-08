# Preparación del inventario de activos para monitoreo — SAIDSOFT

**Fecha:** 7-oct-2026
**Alcance:** auditar el modelo de activos y determinar el **cambio mínimo** que permita a
Administración registrar equipos y a TI completar después los datos técnicos, sin
implementar todavía SNMP.
**Documento hermano:** `docs/auditoria-snmp.md` (auditoría del monitoreo SNMP y su medición).
**Regla:** no se propone reescritura, ni arquitectura paralela, ni un "inventario SNMP".
El inventario sigue siendo uno.

Todo lo que dice este documento sale de leer el código o de medir la base de producción.
Las mediciones son del 7-oct-2026, por SSH, **solo lectura**.

---

## 1. Resumen ejecutivo

**Lo que esperaba encontrar y no encontré: un modelo que obligue a que un activo sea
monitoreable.** No existe ese problema. `Activo.ip` es `null=True, blank=True`,
`registrar_ingreso` acepta `ip=None`, y en producción hay una impresora sin IP que existe
perfectamente. **La capacidad que pedís ya está.**

**Lo que sí encontré: la separación Administración / TI ya está implementada, y con tu
mismo razonamiento escrito en el código.** El docstring de
`apps/activos/management/commands/completar_topologia.py` dice:

> *"`crear_topologia_farmacia` da de alta los puestos; este comando les pone los datos
> reales cuando aparecen. **Están separados a propósito: crear es una decisión sobre qué
> equipos existen, completar es transcribir una planilla**, y mezclarlos haría que un
> error de tipeo en el CSV pudiera dar de alta equipos fantasma."*

Eso es, palabra por palabra, tu criterio final ("¿qué equipos existen?" ≠ "¿cuáles puedo
monitorear?"). Ya está resuelto, con validación todo-o-nada, sin pisar valores cargados, y
dejando un `EventoActivo` por activo tocado.

**El problema real es otro, y es de datos, no de modelo.** Medido: **22 activos**, y de
esos **0 tienen MAC, 0 tienen código SAP, 3 tienen IP, 4 tienen modelo, 2 tienen custodio,
1 tiene fecha de compra**. El modelo admite todo eso; nadie lo cargó.

**Cinco cosas que sí faltan de verdad**, y son chicas:

| # | Falta | Por qué bloquea |
|---|---|---|
| 1 | **Un activo no puede estar "en servicio en matriz"** | `registrar_ingreso` exige bodega **o** farmacia. Una impresora administrativa no es ninguna de las dos. Hoy hay que mentir |
| 2 | **No hay campo de descripción/observaciones** en `Activo` | Hay una impresora "sin marca" y nadie tuvo dónde escribir qué es |
| 3 | **El reporte de "a qué le falta qué" solo existe como simulación de un comando** | `CAMPOS_COMPLETABLES` y `resumen['incompletos']` ya lo calculan, pero no se ve en el panel |
| 4 | **TI no tiene dónde cargar datos técnicos de un activo suelto** | La carga masiva resuelve por `(farmacia, slot)`, así que no alcanza a un equipo de matriz. El único lugar para editar un activo es el admin de Django |
| 5 | **Una fila huérfana con `codigo=''`** | Diagnosticada por completo en §4 |

**Tablas nuevas necesarias: cero. Campos nuevos: dos.** Detalle en §5.

---

## 2. Inventario actual — medido, no estimado

### 2.1 Completitud de los 22 activos de producción

| Campo | Llenos | | Campo | Llenos |
|---|---|---|---|---|
| `codigo` | **21 / 22** | | `farmacia` | 19 / 22 |
| `marca` | 9 / 22 | | `bodega_actual` | 1 / 22 |
| `categoria` | 15 / 22 | | `ubicacion_interna` | 11 / 22 |
| `modelo` | **4 / 22** | | `slot` | 9 / 22 |
| `numero_serie` | 10 / 22 | | `ip` | **3 / 22** |
| `codigo_sap` ("Código Activo") | **0 / 22** | | `mac` | **0 / 22** |
| `fecha_compra` | **1 / 22** | | `estacion` | 8 / 22 |
| `vencimiento_garantia` | **1 / 22** | | `unidad_negocio` | 16 / 22 |
| `orden_compra` | 1 / 22 | | `colaborador_actual` (custodio) | **2 / 22** |

Por tipo: DSK 8, IMP 3, PIN 3, NET 2, TEL 2, CAM 1, BIO 1, LAP 1, ALM 1.

### 2.2 Catálogos administrativos: ¿hay a qué apuntar?

| Catálogo | Filas |
|---|---|
`categoria_equipo` | 11
`colaborador` | 10
`marca` | 6
`departamento` | 2
`bodega` | 1
`orden_compra` | 1
**`ubicacion`** | **0**

**`ubicacion` está vacía.** Importa para §5: es el modelo que mejor resuelve "ubicación
física + área + responsable", pero apuntar a un catálogo vacío no da información. El campo
se agrega y el catálogo se carga; si no se carga, el campo es decoración.

---

## 3. Modelo actual — lo que ya existe

### 3.1 Campos de `Activo` (`apps/activos/models.py:404`)

```
IDENTIDAD        codigo (unique, editable=False)   tipo (12 choices)
                 marca FK                          categoria FK
                 modelo                            numero_serie
                 codigo_sap  ("Código Activo")

ESPECIFICACIÓN   procesador  ram_gb  almacenamiento_gb
                 condicion_al_recibir              estado_fisico_actual
                 baja_recomendada

ADQUISICIÓN      fecha_compra  vencimiento_garantia  orden_compra FK

UBICACIÓN        farmacia FK (null)                bodega_actual FK (null)
                 colaborador_actual FK (null)      ubicacion_interna (choices)
                 slot                              unidad_negocio FK (null)

TÉCNICO          ip (GenericIPAddressField, inet)  mac
                 estacion OneToOne (null)

CICLO            estado: en_bodega | asignado | en_reparacion | dado_de_baja
                 fecha_creacion
```

### 3.2 Relaciones y reglas que ya están resueltas

- **`delete()` lanza `NotImplementedError`** — un activo nunca se borra, se da de baja.
  El historial es auditoría permanente.
- **`ip_efectiva`** devuelve `(ip, origen)` con `origen ∈ {'agente','manual',None}`. **Es
  el precedente exacto del enfoque derivado**: el dato no se duplica en una columna, se
  resuelve al leerlo y además dice de dónde salió.
- **`clean()`** valida dos cosas: `slot` exige `farmacia`, y `estacion` vinculada prohíbe
  cargar `ip`/`mac` a mano (*"crearía una segunda versión del mismo dato"*). Es validación
  de formulario y **no** un constraint de base, a propósito: una fila existente tiene que
  poder guardarse igual.
- **`EventoActivo`** es el historial por activo, con `tipo_evento` y `detalle` JSON. Cada
  transición del ciclo de vida escribe uno.
- **`Ubicacion`** (`db_table='ubicacion'`) ya tiene `nombre, agencia, direccion, ciudad,
  parroquia, provincia, departamento FK, encargado FK, latitud, longitud`. La apuntan
  `Colaborador` y `apps.mantenimiento`. **`Activo` NO la apunta.**

### 3.3 Caminos que crean o modifican un `Activo` — los cinco, completos

| Camino | Pasa por `generar_codigo_activo`? |
|---|---|
| `apps.activos.services.registrar_ingreso` (único constructor `Activo(...)`) | **Sí**, `services.py:102` |
| Panel: `activo_crear` → `ActivoIngresoForm` → `registrar_ingreso` | **Sí**, vía el servicio |
| `crear_topologia_farmacia` → `registrar_ingreso` | **Sí**, vía el servicio |
| Admin de Django (`ActivoAdmin.save_model`) | **Sí, desde el 16-sep-2026.** Antes, no |
| `apps/viaticos/.../sembrar_escenarios_prueba.py` (`get_or_create`) | No, pero **pasa un código explícito** |

**No hay ningún otro camino.** Ni API, ni sincronización ARP, ni migración de datos crea
activos: verificado buscando `Activo(`, `Activo.objects.create`, `bulk_create`,
`get_or_create` y `update_or_create` en todo el repo.

**El descubrimiento por ARP NO crea activos** — escribe en `DispositivoDetectado`, que es
una tabla aparte. Eso ya está bien y hay que dejarlo así (§8).

### 3.4 El flujo de carga técnica masiva que ya existe

```
crear_topologia_farmacia       →  da de alta los PUESTOS (qué equipos existen)
          ↓
completar_topologia            →  carga ip / mac / numero_serie en los que YA existen
importar_planilla_ips          →  lo mismo, leyendo la planilla de direccionamiento cruda
```

`CAMPOS_COMPLETABLES = ('ip', 'mac', 'numero_serie')` — **ésa es, hoy, la definición de
"datos técnicos" en el código.** Y `completar_datos_topologia` ya implementa:

- **Todo o nada**: valida la planilla entera antes de escribir la primera fila.
- **Nunca pisa un valor cargado**: una IP distinta es un conflicto que resuelve una
  persona, no un `UPDATE` silencioso.
- **Rechaza ip/mac si hay estación vinculada.**
- **Deja un `EventoActivo`** con qué campos se cargaron.
- **Reporta `resumen['incompletos']`**: "a este activo le falta ip, mac, numero_serie".
- **Simula por defecto**; escribe solo con `--aplicar`.

**Esas seis reglas son el contrato que cualquier carga técnica nueva tiene que respetar.**
No hay que diseñarlas: hay que reusarlas.

**Su límite:** resuelve el activo por `(farmacia, slot)`. Un equipo **sin farmacia y sin
slot** —la impresora de matriz— no es direccionable por esta vía. Ése es el hueco 4 del
resumen.

---

## 4. Problemas encontrados

### 4.1 El `Activo id=20` con `codigo=''` — causa raíz, con fechas

**No es un bug abierto. Es el único sobreviviente de un bug que ya se arregló, y el arreglo
llegó 13 minutos tarde para esta fila.**

La secuencia, reconstruida del código y del historial de git:

1. **`Activo.codigo` nunca se generó en el modelo.** No hay `save()` que lo asigne. Lo
   asigna `apps.activos.services.registrar_ingreso` (`services.py:102`), que era "el camino
   del panel".
2. **`Activo` está registrado en el admin de Django** (`apps/activos/admin.py:152`), y
   `codigo` está en `readonly_fields` — a propósito, *"para que nadie lo invente a mano y se
   salte la numeración"*.
3. Un campo readonly no viaja en el formulario, así que un alta por el admin insertaba
   `codigo=''`. **Como `unique=True` acepta exactamente un string vacío, la primera pasó.**
4. El 16-sep-2026 alguien dio de alta esta impresora por el admin. Quedó con `codigo=''`,
   `ip=10.101.49.136`, y `estado='asignado'` sin farmacia — otra señal de que no pasó por el
   servicio, que solo pone `ASIGNADO` cuando hay farmacia.
5. **El siguiente alta rompió** contra el índice único. El docstring del arreglo lo dice:
   *"el alta insertaba `codigo=''` y, de la segunda en adelante, rompía contra el índice
   único con un 500 sin explicación."*
6. Se agregó `ActivoAdmin.save_model`, que genera el código en el alta.

La cronología, al minuto:

```
2026-09-16 23:12:36 UTC   se crea el Activo id=20 con codigo=''
2026-09-16 23:25:47 UTC   commit 35ee395 "admin activos numeracion auto"  (+13 min)
```

**Respuestas a lo que preguntaste:**

| Pregunta | Respuesta |
|---|---|
| ¿Por qué quedó con código vacío? | `codigo` es `readonly` en el admin y el modelo no lo genera; el generador vive en el servicio, que el admin no usaba |
| ¿Qué flujo lo creó? | El admin de Django, el 16-sep-2026, antes de `save_model` |
| ¿Hay otra ruta que lo permita? | **Hoy, no.** Los cinco caminos de §3.3 asignan código. El hueco se cerró |
| ¿Puede afectar otros tipos? | Podía, cualquier tipo dado de alta por el admin. Medido: **1 sola fila afectada en toda la base** |
| ¿Corregir ahora o documentar? | **Corregir** — es una fila, y además destraba un bug latente (§4.2). Pero con un comando idempotente, no con un `UPDATE` a mano |

### 4.2 Bug latente que el huérfano mantiene vivo

```python
# apps/activos/services.py:36
def generar_codigo_activo(tipo: str) -> str:
    ultimo = Activo.objects.filter(tipo=tipo).order_by('-codigo').first()
    ultimo_num = int(ultimo.codigo.rsplit('-', 1)[-1]) if ultimo else 0
```

Si el **único** activo de un tipo tuviera `codigo=''`, entonces `''.rsplit('-',1)[-1]` es
`''` y `int('')` lanza **`ValueError`**: no se podría dar de alta ningún activo de ese tipo.

Hoy no explota porque `IMP` tiene además `CR-IMP-0001` y `CR-IMP-0002`, y el orden
descendente deja el vacío al final. **Es una bomba desactivada por casualidad**, y
corregir la fila la desarma de verdad.

### 4.3 Dos anomalías más que apareció la medición

| id | código | Síntoma |
|---|---|---|
| 20 | *(vacío)* | `estado='asignado'` **sin farmacia** |
| 23 | CR-PIN-0002 | `estado='en_bodega'` **sin `bodega_actual`** |
| 24 | CR-PIN-0003 | `estado='en_bodega'` **sin `bodega_actual`** |

**`clean()` no valida la coherencia entre `estado` y la ubicación.** Un activo puede decir
"en bodega" sin bodega, o "asignado" sin farmacia ni custodio.

**Recomendación: NO agregar una validación dura.** El propio modelo ya decidió este
criterio para el caso `estacion` + `ip`: *"un registro existente con los dos datos tiene
que poder guardarse igual… Lo que se evita es que una persona los EDITE."* Una validación
dura dejaría estas tres filas sin poder guardarse desde el admin, que es justo donde hay
que ir a arreglarlas. **Va como chequeo de calidad de datos en el panel de avisos**, no
como constraint.

### 4.4 A verificar antes de cerrar la FASE A

El arreglo `35ee395` es del 16-sep. **No verifiqué que el código desplegado lo tenga** —
y conviene hacerlo antes de dar el hueco por cerrado:

```bash
docker exec deploy-web-1 grep -c "if not change and not obj.codigo" /app/apps/activos/admin.py
```

---

## 5. Propuesta de modelo

### 5.1 Mapa dato por dato (lo que pediste en la FASE 2)

#### Información administrativa

| Dato | ¿Existe? | Modelo / campo | Reusar | Falta | Recomendación |
|---|---|---|---|---|---|
| Código de activo | Sí | `Activo.codigo` | ✔ | | Autogenerado. No tocar |
| Código contable | Sí | `Activo.codigo_sap` ("Código Activo") | ✔ | | **0/22 cargados.** Es dato, no código |
| Tipo | Sí | `Activo.tipo` (12 choices) | ✔ | | Ya incluye `UPS`, `RED`, `IMP` |
| Categoría | Sí | `Activo.categoria` → `CategoriaEquipo` | ✔ | | 11 filas en catálogo |
| Fabricante | Sí | `Activo.marca` → `Marca` | ✔ | | 6 filas |
| Modelo | Sí | `Activo.modelo` | ✔ | | 4/22 |
| Número de serie | Sí | `Activo.numero_serie` | ✔ | | SNMP lo llenaría solo después |
| **Descripción / observaciones** | **No** | — | | **Sí** | **Agregar `observaciones` TextField** |
| Ubicación (farmacia) | Sí | `Activo.farmacia` | ✔ | | |
| Ubicación (interna) | Sí | `Activo.ubicacion_interna` (choices) | ✔ | | Caja, oficina, etc. |
| **Ubicación física fuera de farmacia** | **Parcial** | existe `Ubicacion`, **no ligada a `Activo`** | | **Sí** | **Agregar `Activo.ubicacion` FK** |
| **Área / departamento** | **Parcial** | `Departamento` existe, llega solo vía `Ubicacion` o `Cargo` | ✔ | | Queda resuelto por el FK de arriba — **no agregar un FK propio** |
| Custodio / responsable | Sí | `Activo.colaborador_actual` | ✔ | | 2/22 |
| Estado | Sí | `Activo.estado` + `estado_fisico_actual` | ✔ | | |
| Fecha de adquisición | Sí | `Activo.fecha_compra` | ✔ | | 1/22 |
| Garantía | Sí | `Activo.vencimiento_garantia` | ✔ | | Ya alimenta el aviso de vencimientos |
| Proveedor | **Parcial** | `OrdenCompra.proveedor` (CharField) | ✔ | — | **NO agregar campo todavía.** 21/22 no tienen ninguna data de compra: el campo faltante no es lo que bloquea |

#### Información técnica

| Dato | ¿Existe? | Modelo / campo | Reusar | Falta | Recomendación |
|---|---|---|---|---|---|
| IP | Sí | `Activo.ip` + `ip_efectiva` (resuelve agente vs manual) | ✔ | | 3/22 |
| MAC | Sí | `Activo.mac` | ✔ | | **0/22**. Identidad estable; la IP cambia |
| **Hostname** | No | `Estacion.codigo` para los que tienen agente | | — | **NO agregar.** Para una impresora no agrega nada que SNMP no dé después (`sysName`) |
| **VLAN** | No | `Farmacia.segmento_red` | | — | **NO agregar.** SNMP necesita IP + community + puerto, no VLAN |
| Método de monitoreo | **Derivable** | `estacion` / `EstadoRedActivo` / (futuro `ObjetivoSnmp`) | ✔ | | **NO agregar columna.** Ver §5.3 |
| SNMP habilitado / versión / credencial | No | sería `ObjetivoSnmp` + `PerfilSnmp` | | después | Fuera de alcance de este documento |
| Estado de monitoreo (ICMP) | Sí | `EstadoRedActivo.responde` | ✔ | | Se recoge y **no se muestra** |
| Última comprobación | Sí | `EstadoRedActivo.ultima_verificacion` + `verificacion_vigente` | ✔ | | |

### 5.2 Los dos campos nuevos, justificados uno por uno

**`Activo.ubicacion = FK('activos.Ubicacion', null=True, blank=True, on_delete=PROTECT)`**

- **Por qué:** `registrar_ingreso` exige `bodega` **o** `farmacia`. Una impresora
  administrativa en matriz no es ninguna. Hoy hay que elegir entre ponerla en una bodega
  (queda `EN_BODEGA`, o sea "almacenada", y es falso) o inventarle una farmacia (peor: el
  equipo aparece en un local donde no está).
- **Por qué reusar `Ubicacion` y no campos nuevos:** ya trae `nombre, agencia, direccion,
  ciudad, parroquia, provincia, departamento FK, encargado FK`. **Un solo FK resuelve de
  una vez "ubicación física", "área/departamento" y "responsable del sitio"** —tres filas
  de tu tabla de FASE 2— sin agregar tres campos.
- **`PROTECT`:** una ubicación con activos no se borra por accidente.
- **Advertencia medida:** `ubicacion` tiene **0 filas**. El campo sin catálogo cargado no
  informa nada. **Cargar las ubicaciones es parte de la FASE A, no un "después".**

**`Activo.observaciones = TextField(blank=True)`**

- **Por qué:** no hay ningún campo de texto libre. Hay una impresora "sin marca" en
  producción: quien la cargó no tuvo dónde escribir qué es ni de dónde salió. Es el campo
  que evita que un dato se pierda por no tener casillero.
- Alternativa descartada: usar `EventoActivo.detalle`. Es historial de transiciones, no una
  nota vigente del equipo; buscar ahí la descripción actual sería leer un log para sacar un
  estado.

**Y nada más.** `hostname`, `vlan`, `proveedor`, `descripcion` separada de observaciones,
`area` como FK propio, `monitoring_method`: ninguno entra.

### 5.3 Ciclo de vida: no agregar estados (FASE 3)

Los estados que propusiste —`REGISTRADO → INVENTARIO COMPLETO → DATOS TÉCNICOS COMPLETOS →
VALIDACIÓN → MONITOREABLE → MONITOREADO`— **no deben vivir en `Activo.estado`.**

Dos motivos, los dos del propio código:

1. **`Activo.estado` es un eje distinto: custodia.** `en_bodega | asignado | en_reparacion |
   dado_de_baja` responde "dónde está y de quién es", no "cuánto sabemos de él". Mezclar
   los dos ejes obliga a estados compuestos: ¿una impresora *asignada* y *monitoreable* qué
   valor lleva?
2. **Este repo ya pagó por un estado que nadie asignaba.** El comentario de `Estado` cuenta
   que `EN_TRANSITO` *"estuvo acá desde siempre sin que ninguna transición lo asignara ni
   ninguna vista lo leyera"*, y se eliminó tras confirmar 0 filas en producción. Agregar
   seis estados que nada asigna repite exactamente ese error, multiplicado por seis.

**Todos son derivables, y el modelo ya tiene el precedente de cómo hacerlo:** `ip_efectiva`
devuelve `(valor, origen)` resolviendo al leer en vez de guardar una copia.

| Pregunta | Se deriva de |
|---|---|
| ¿Inventario administrativo completo? | `marca`, `categoria`, `modelo`, `numero_serie` cargados |
| ¿Datos técnicos completos? | `ip_efectiva` devuelve algo (y `mac` si no tiene estación) |
| ¿Conectividad validada? | `EstadoRedActivo.responde` **y** `verificacion_vigente` |
| ¿Monitoreable? | tiene `estacion` (agente) **o** tiene IP (ICMP/SNMP) |
| ¿Monitoreado? | tiene `estacion`, o `EstadoRedActivo` vigente, o (futuro) `ObjetivoSnmp` habilitado |

Una propiedad `Activo.monitoreo` que devuelva `{'metodos': [...], 'completitud': ...}` lo
resuelve sin una columna, sin migración y **sin poder desincronizarse**. Si algún día hace
falta filtrar por eso en SQL a escala, se resuelve con un índice parcial o una vista
materializada — no cambiando el modelo ahora.

---

## 6. Flujo Administración → TI

```
ADMINISTRACIÓN                              TI
──────────────                              ──
Registra el activo
  tipo, marca, modelo, serie
  ubicación / farmacia / bodega
  custodio, compra, garantía
  observaciones
        │
        │  registrar_ingreso()  →  codigo autogenerado
        │                          EventoActivo INGRESO
        ▼
  El activo EXISTE
  (sin IP, sin SNMP, sin monitoreo)
        │
        │  aparece en el panel de avisos:
        │  "sin datos técnicos"
        ▼
                                    Carga ip / mac
                                      · masivo por planilla  (farmacia + slot)
                                        completar_topologia / importar_planilla_ips
                                      · o por activo         (equipos de matriz)
                                        vista nueva de la FASE C
                                            │
                                            │  nunca pisa un valor cargado
                                            │  rechaza si hay estación vinculada
                                            │  EventoActivo por activo tocado
                                            ▼
                                    Validación de conectividad
                                      EstadoRedActivo (ICMP, ya existe)
                                            │
                                            ▼
                                    Habilita SNMP   ← recién acá empieza
                                      ObjetivoSnmp     docs/auditoria-snmp.md
```

**Lo importante del dibujo: la mitad izquierda y la columna de carga masiva ya existen.**
Lo que falta son las dos cajas marcadas FASE A-C.

Administración nunca ve SNMP, OID, community, MIB, Celery ni MQTT. **Eso hoy ya se cumple**
y no por casualidad: `ActivoIngresoForm` ni siquiera incluye `ip`, `mac`,
`ubicacion_interna` ni `slot`, aunque `registrar_ingreso` los acepta (§7.1).

---

## 7. Formulario propuesto

### 7.1 Lo que ya hay

`apps/activos/forms.py::ActivoIngresoForm` es un `forms.Form` (no un `ModelForm`), así que
**pasa obligatoriamente por el servicio** y no puede saltarse la generación del código.
Incluye: `tipo, marca, categoria, modelo, numero_serie, procesador, ram_gb,
almacenamiento_gb, codigo_sap, condicion_al_recibir, fecha_compra, vencimiento_garantia,
orden_compra, bodega, farmacia`.

**No incluye `ip`, `mac`, `ubicacion_interna` ni `slot`.** O sea: **la separación
administrativo / técnico ya está hecha en el formulario**, aunque nunca se la llamó así.

### 7.2 Lo que hay que cambiar — poco

**Formulario de Administración** (`ActivoIngresoForm`), tres cambios:

```
IDENTIFICACIÓN         tipo*  marca  categoria  modelo  numero_serie  codigo_sap
                       observaciones                                   ← NUEVO

UBICACIÓN              farmacia  |  bodega  |  ubicacion               ← NUEVO (una de las tres)
                       colaborador_actual (custodio)                   ← NUEVO en el form
                       ubicacion_interna      (solo si hay farmacia)

ESPECIFICACIÓN         procesador  ram_gb  almacenamiento_gb  condicion_al_recibir

ADQUISICIÓN            fecha_compra  vencimiento_garantia  orden_compra
```

- `ubicacion` como **tercera opción** del `clean()` que hoy exige bodega-o-farmacia. Con
  `ubicacion` el estado queda `ASIGNADO` (en servicio), que es lo correcto para un equipo
  en uso en matriz.
- `colaborador_actual` en el alta: hoy solo se asigna después, con `AsignacionForm`. Con
  2/22 custodios cargados, pedirlo cuando la persona tiene el equipo delante es más
  efectivo que un paso aparte.
- `ubicacion_interna` condicionado a farmacia, igual que `clean()` ya hace con `slot`.

**Nada de ip/mac/SNMP en este formulario.** Sigue siendo la frontera.

### 7.3 Vista de datos técnicos para TI (FASE C)

Hoy **el único lugar donde se puede editar un activo existente es el admin de Django** — lo
dice su propio docstring: *"El admin es el ÚNICO lugar del sistema donde se puede editar un
Activo existente; el panel solo tiene acciones de ciclo de vida."* Darle a TI acceso al
admin para cargar una IP es demasiado permiso para una tarea chica.

Una vista única, con los campos `ip` y `mac`, que **replique las seis reglas de
`completar_datos_topologia`** (§3.4) en vez de inventar otras:

```
DATOS TÉCNICOS · CR-IMP-0003 · Epson L3250

IP     [                ]   ← bloqueado si hay estación vinculada, con el motivo a la vista
MAC    [                ]

CONECTIVIDAD
  ICMP   sin verificar  ·  EstadoRedActivo (ya existe, hoy no se muestra en ninguna parte)

MONITOREO
  Métodos: ninguno            ← derivado, no editable
  SNMP:    no disponible      ← aparece cuando exista ObjetivoSnmp
```

El bloqueo por estación vinculada se hereda tal cual del admin, que ya lo resuelve bien:
*"Bloquear en vez de ocultar: que el campo siga a la vista, vacío y deshabilitado, le dice
al operador que el dato existe pero viene de otro lado. Ocultarlo lo dejaría buscándolo."*

---

## 8. Descubrimiento de red: qué hacer con ARP

### 8.1 Qué hace hoy, medido

| | |
|---|---|
| Función | `apps.monitoreo.mikrotik.sincronizar_dispositivos_detectados` |
| Qué lee | Tabla ARP del Mikrotik por SNMP (`1.3.6.1.2.1.4.22.1.2`), tope 200 entradas |
| Qué escribe | `DispositivoDetectado`: `farmacia, mac, ip, interfaz_indice, visto_por_primera_vez, visto_por_ultima_vez` |
| Identidad | **la MAC**, no la IP — *"la Epson va por WiFi con DHCP y cambia de dirección, pero su MAC no"* |
| Quién la invoca | **Solo** el comando manual `descubrir_dispositivos_farmacia` |
| ¿Está en Beat? | **No.** Verificado: 0 coincidencias en `CELERY_BEAT_SCHEDULE` |
| Dato en producción | 29 dispositivos, **todos** con `visto_por_ultima_vez = 15-sep-2026** (3 semanas) |
| ¿Crea activos? | **No**, y es correcto |

### 8.2 ¿Sirve al inventario? Sí, pero no como lo pensás

**No sirve para crear activos, y en eso tenés razón.** La ARP da MAC, IP e `ifIndex`. **No
da** fabricante real, modelo, número de serie, función del equipo, ni si es de la empresa.
El OUI de la MAC sugiere un fabricante, y sugerir no es saber.

**Sirve para lo contrario, que es más valioso:** `DispositivoDetectado.activo_declarado`
ya cruza por MAC contra `Activo`, y el docstring explica para qué: *"qué hay enchufado que
nadie inventarió, y qué está inventariado pero no aparece."* Medido hoy: **29 de 29 sin
inventariar.** Es una lista de trabajo para Administración, no una fuente de altas.

### 8.3 Estrategia segura propuesta

1. **Agendarlo en Beat, diario**, no cada 5 minutos. Una tabla ARP no cambia tanto, y el
   costo es un walk SNMP por farmacia.
2. **Que siga sin crear activos nunca.** La separación tabla-aparte es la protección; no
   hay que tocarla.
3. **Exponer el cruce como cola de trabajo**: "29 dispositivos vistos en la red sin activo
   declarado", con farmacia, IP, MAC y OUI, y un botón que **precarga** el formulario de
   alta con MAC e IP para que una persona complete marca, modelo y serie. Alta asistida,
   nunca automática.
4. **Aprovechar el dato de desaparición**: un `DispositivoDetectado` que dejó de verse por
   semanas es un equipo que se fue del local. Hoy esa señal se pierde porque el sync no
   corre.

### 8.4 Riesgos de automatizarlo

| Riesgo | Nivel | Mitigación |
|---|---|---|
| Crear activos fantasma | **Alto si se automatizara el alta** | No automatizarla. Punto 2 y 3 |
| Carga SNMP sobre 700 Mikrotik | Medio | Diario, no cada 5 min. El walk ya tiene tope de 200 entradas |
| Crecimiento de `dispositivo_detectado` | Bajo | No es historial: una fila por MAC por farmacia, se actualiza |
| MAC aleatoria de celulares/visitas | Medio | Ensucia la cola de trabajo con equipos que no son de la empresa. **Hay que verlo con datos frescos antes de decidir filtros** |
| Equipo que cambia de farmacia | Bajo | El UNIQUE es `(farmacia, mac)`: aparecería en las dos. Visible, no silencioso |

---

## 9. Preparación para SNMP: qué tiene que estar listo

De `docs/auditoria-snmp.md` §0.1, la compuerta de la FASE 0 **no se cerró porque no hay
dispositivos**. Este documento prepara justamente eso. Para empezar SNMP hace falta:

1. **Al menos una impresora administrativa cargada como `Activo`** con marca, modelo e IP.
   Hoy: 3 impresoras, cero HP/Ricoh/Xerox, una con IP.
2. **Que esa impresora pueda existir "en matriz"** sin mentirle al estado → campo
   `ubicacion` (§5.2).
3. **SNMP de solo lectura habilitado en el equipo**, y la community anotada. La única
   impresora con IP responde ping a 10,8 ms y **no** responde SNMP en seis communities: el
   paquete llega, el agente SNMP está apagado. **Se arregla en la impresora.**
4. **`EstadoRedActivo` visible en el panel**, para que "responde ICMP pero no SNMP" sea una
   observación que alguien puede hacer sin entrar a la base.
5. **La fila huérfana corregida**, porque desarma el `ValueError` latente de §4.2.

Nada de eso es el módulo SNMP. Todo eso es inventario.

---

## 10. Migraciones necesarias

**Una sola, con dos campos aditivos y nullables.**

```
activos/00XX_activo_ubicacion_observaciones.py
  + Activo.ubicacion     FK → activos.Ubicacion, null=True, blank=True, on_delete=PROTECT
  + Activo.observaciones TextField(blank=True)
```

No hay `ALTER` destructivo, ni `NOT NULL` sobre una tabla con datos, ni renombres, ni
índices nuevos (el FK trae el suyo). **Ninguna hypertable involucrada**, así que no aplica
nada de lo de `config/test_runner.py` ni el patrón de la migración 0035.

**La corrección de la fila huérfana NO va en esta migración.** Va como comando idempotente
(§12, FASE A): una migración de datos corre una vez en cada entorno y no deja rastro
consultable; un comando se puede simular primero —como ya hacen `completar_topologia` e
`importar_planilla_ips`— y deja un `EventoActivo`.

---

## 11. Riesgos

| Cambio | Riesgo | Por qué | Cómo probar que no rompió nada |
|---|---|---|---|
| `Activo.observaciones` | **BAJO** | Aditivo, nullable, nada lo lee todavía | Suite completa |
| `Activo.ubicacion` FK | **BAJO** | Aditivo, nullable. `PROTECT` no afecta filas existentes | Suite + alta con y sin ubicación |
| Corregir la fila huérfana | **BAJO** | 1 fila, comando idempotente con simulación | Correr en simulación; verificar que `generar_codigo_activo('IMP')` sigue dando el siguiente |
| 5º bloque en `activos_avisos` | **BAJO** | Solo lectura. **Vigilar N+1** con `select_related` | `assertNumQueries` |
| `ActivoIngresoForm`: +3 campos y `clean()` con tercera opción | **MEDIO** | Es el camino de alta del panel. Un `clean()` mal armado puede bloquear altas que hoy funcionan | Pruebas de las 3 ramas (bodega / farmacia / ubicación) y de que exigir "al menos una" sigue valiendo |
| `registrar_ingreso`: +1 kwarg `ubicacion` | **MEDIO** | **Único constructor de `Activo`** del sistema; lo usa el panel y `crear_topologia_farmacia` | Kwarg opcional con default: las llamadas existentes no cambian. Probar que la topología sigue creando igual |
| Vista de datos técnicos (FASE C) | **MEDIO** | Camino de escritura nuevo sobre `Activo`. Debe replicar las 6 reglas de §3.4, no inventar otras | Probar: no pisa valor cargado; rechaza con estación vinculada; escribe `EventoActivo` |
| Agendar el sync de ARP en Beat | **MEDIO** | Toca `CELERY_BEAT_SCHEDULE`, o sea `celery_beat`. Suma carga SNMP a 700 Mikrotik | Correr el comando a mano sobre unas farmacias y medir antes de agendar |
| Validación dura `estado` ↔ ubicación | **ALTO — descartado** | Dejaría las 3 filas anómalas sin poder guardarse justo donde hay que arreglarlas | No se hace. Va como aviso |
| Estados de monitoreo en `Activo.estado` | **ALTO — descartado** | Mezcla dos ejes y repite el error de `EN_TRANSITO` | No se hace. Derivado |

### No se toca nada de esto

`Alerta` y el motor de alertas · `apps/monitoreo/mikrotik.py` · MQTT y el agente ·
`apps/mqtt_worker` · Celery global (solo se agrega **una** entrada a Beat, en la FASE E, y
con medición previa) · Redis · las 4 hypertables · `Estacion` · el panel existente salvo
los agregados listados.

**Ninguno de los cambios propuestos requiere aprobación bajo la regla de no regresión**,
excepto agendar ARP en Beat, que toca `celery_beat` y queda en la FASE E justamente para
pedirla por separado.

---

## 12. Plan de implementación

Una fase = un cambio lógico. No se mezcla inventario con SNMP.

### FASE A — Corregir el inventario

**Objetivo:** que no quede ninguna fila inconsistente y que el hueco de `codigo` esté
cerrado de punta a punta.

- **Archivos:** `apps/activos/management/commands/corregir_activos_sin_codigo.py` (nuevo).
- **BD:** ninguna migración. Solo datos: 1 fila.
- **Qué hace:** busca `Activo` con `codigo=''`, asigna el siguiente con
  `generar_codigo_activo(tipo)`, escribe un `EventoActivo`. **Simula por defecto**, escribe
  con `--aplicar`, igual que `completar_topologia`.
- **Además:** verificar en el servidor que el arreglo `35ee395` esté desplegado (§4.4), y
  listar las 3 anomalías de §4.3 para que alguien las resuelva a mano por el admin.
- **Riesgo:** BAJO.
- **Pruebas:** idempotencia (correrlo dos veces no cambia nada); que el huérfano quede con
  código; que `generar_codigo_activo('IMP')` siga devolviendo el siguiente correcto; una
  prueba de regresión del `ValueError` de §4.2.
- **Aceptación:** `select count(*) from activo where codigo=''` devuelve **0**.

### FASE B — Campos nuevos y formulario administrativo

**Objetivo:** que un activo pueda existir "en servicio en matriz" y que haya dónde
describirlo.

- **Archivos:** `apps/activos/models.py` (2 campos), migración nueva,
  `apps/activos/services.py::registrar_ingreso` (+1 kwarg), `apps/activos/forms.py`,
  `templates/panel/activo_form.html`, `apps/activos/admin.py` (que los muestre).
- **BD:** una migración aditiva (§10).
- **Riesgo:** BAJO los campos, **MEDIO** el `clean()` del formulario y el kwarg del
  servicio.
- **Pruebas:** las 3 ramas de ubicación; que las llamadas existentes a `registrar_ingreso`
  no cambien de comportamiento; que `crear_topologia_farmacia` siga creando igual.
- **Aceptación:** se puede dar de alta una impresora en matriz, queda `ASIGNADO`, con
  ubicación y sin IP, y **no** aparece en ninguna bodega.
- **Y además:** cargar el catálogo `ubicacion`, que hoy tiene 0 filas. **Sin esto la fase
  entrega un campo vacío**, que es precisamente el riesgo del proyecto.

### FASE C — Datos técnicos para TI

**Objetivo:** que TI cargue IP/MAC sin acceso al admin de Django.

- **Archivos:** `apps/panel/views/activos.py` (vista nueva), `apps/activos/forms.py`,
  plantilla nueva, `apps/panel/urls.py`.
- **BD:** ninguna.
- **Riesgo:** MEDIO — camino de escritura nuevo.
- **Pruebas:** no pisa un valor cargado; rechaza si hay `estacion` vinculada mostrando el
  motivo; escribe `EventoActivo`; respeta el scoping por unidad de negocio.
- **Aceptación:** la impresora de la FASE B queda con IP cargada por el panel, con su
  evento en el historial, sin que nadie entre al admin.

### FASE D — Validación y visibilidad

**Objetivo:** que "¿qué equipos existen?" y "¿cuáles puedo monitorear?" sean dos preguntas
que el panel contesta.

- **Archivos:** `apps/activos/models.py` (propiedad derivada `monitoreo`),
  `apps/activos/services.py` (queryset de incompletos), `apps/panel/views/activos.py`,
  `templates/panel/activos_avisos.html`, `templates/panel/activo_detalle.html`.
- **BD:** ninguna. **Todo derivado.**
- **Qué agrega:** 5º bloque en el panel de avisos —*"activos sin datos técnicos"* y *"estado
  incoherente con la ubicación"*— y el bloque "Monitoreo" en la ficha del activo, que de
  paso muestra `EstadoRedActivo`, que hoy se recoge y no se ve en ninguna parte.
- **Riesgo:** BAJO. **Vigilar N+1**: la ficha y la lista tienen que traer `estacion` y
  `estado_red` con `select_related`.
- **Pruebas:** `assertNumQueries` estable al crecer la cantidad de activos; la propiedad
  derivada contra los 5 casos de §5.3.
- **Aceptación:** el panel dice, sin consultar la base a mano, cuántos activos les faltan
  datos técnicos y cuáles.

### FASE E — Preparación para SNMP

**Objetivo:** dejar cerrada la compuerta de la FASE 0 de `docs/auditoria-snmp.md`.

- **Archivos:** `config/settings/base.py` (**una** entrada en `CELERY_BEAT_SCHEDULE` para
  el sync de ARP, diario).
- **Riesgo:** MEDIO — toca `celery_beat` y suma carga SNMP. **Requiere aprobación
  explícita** por la regla de no regresión.
- **Antes de agendar:** correr `descubrir_dispositivos_farmacia` a mano sobre unas farmacias
  y medir duración y volumen.
- **Aceptación:** `visto_por_ultima_vez` deja de tener semanas de atraso, y existe al menos
  una impresora administrativa cargada, con IP, con SNMP habilitado en el equipo y
  respondiendo `prtMarkerSuppliesLevel` al probe.

**Recién cuando la FASE E cierre empieza `docs/auditoria-snmp.md` FASE 1.**

---

## 13. Respuestas directas a la FASE 10

**A. ¿Qué ya tenemos?** `Activo` como sujeto único con 24 campos, incluidos `ip`, `mac`,
`slot`, `ubicacion_interna`. `generar_codigo_activo` + los 5 caminos de alta, todos
pasando por él. `registrar_ingreso` como único constructor. `EventoActivo` como historial.
`ip_efectiva` como precedente de dato derivado. `Ubicacion` con departamento y encargado.
`CategoriaEquipo`, `Marca`, `Colaborador`, `Bodega`, `Departamento`. `EstadoRedActivo` para
ICMP. `completar_datos_topologia` con las 6 reglas de carga técnica. `crypto.py` Fernet.
`activos_avisos` como panel de visibilidad con 4 chequeos. El formulario de alta ya
separado de lo técnico.

**B. ¿Qué falta realmente?** Que un activo pueda estar en servicio fuera de farmacia y
bodega. Un campo de texto libre. Que el reporte de incompletos se vea en el panel. Una
superficie de carga técnica que no sea el admin. Y datos: 22 activos con casi todo vacío.

**C. ¿Qué NO debemos crear?** Tabla de dispositivos (es `Activo`). Tabla o campos de
ubicación (es `Ubicacion`). FK de departamento en `Activo` (llega por `Ubicacion`).
`monitoring_method` como columna. Estados de monitoreo en `Activo.estado`. Campos
`hostname`, `vlan`, `proveedor`. Un segundo mecanismo de cifrado. Un "inventario SNMP"
paralelo. Alta automática de activos desde ARP.

**D. ¿Cuál es el cambio mínimo?** Un comando de corrección, **dos campos**, una migración
aditiva, un kwarg opcional en `registrar_ingreso`, tres campos en el formulario de alta,
una vista de datos técnicos, una propiedad derivada y dos bloques de panel. Cero tablas.

**E. ¿Qué riesgos existen?** Tabla completa en §11. Los únicos MEDIO son `registrar_ingreso`
(único constructor), el `clean()` del formulario, la vista de escritura nueva y agendar ARP
en Beat. Los dos ALTO identificados —validación dura de estado y estados de monitoreo en
`Activo.estado`— están **descartados**, no pospuestos.

---

## Apéndice: fuentes

**Código leído:** `apps/activos/{models,services,forms,admin}.py` ·
`apps/activos/management/commands/{completar_topologia,importar_planilla_ips,crear_topologia_farmacia,seed_activos}.py` ·
`apps/activos/migrations/{0022,0024}*.py` · `apps/monitoreo/{models,mikrotik}.py` ·
`apps/monitoreo/management/commands/descubrir_dispositivos_farmacia.py` ·
`apps/catalogo/models.py` · `apps/panel/views/activos.py` ·
`apps/viaticos/management/commands/sembrar_escenarios_prueba.py` ·
`config/settings/base.py` · `templates/panel/activo_detalle.html`

**Git:** `35ee395` (16-sep-2026 18:25:47 -0500) "admin activos numeracion auto".

**Producción, 7-oct-2026, solo lectura:** completitud campo por campo de los 22 activos ·
conteo de catálogos administrativos · activos con `codigo` vacío · activos sin farmacia ni
bodega · antigüedad de `dispositivo_detectado`.
