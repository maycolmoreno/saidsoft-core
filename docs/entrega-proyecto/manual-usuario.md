# Manual de Usuario — SAIDSOFT

**Versión del documento:** 23-ago-2026
**Sistema:** SAIDSOFT — Plataforma de gestión remota (RMM) y de activos de TI para
Farmacias San Gregorio (SG), MIA y 7DIAS, unidades de negocio de CRESIO.

---

## 1. Introducción

SAIDSOFT es la plataforma central desde la que el equipo de Tecnologías e Innovación de
CRESIO administra, de forma remota, las ~1.800 estaciones Windows instaladas en las
~600 farmacias del grupo (marcas SG, MIA y 7DIAS), además de llevar el inventario de
activos físicos (equipos, licencias, insumos) asignados a cada sucursal.

Cada estación tiene instalado un **agente** que se conecta al servidor por MQTT (con
TLS) y permite, sin desplazarse físicamente al sitio:

- Ver el estado de conexión y la información de hardware de cada equipo.
- Reiniciar equipos, tomar control remoto y ejecutar scripts o comandos.
- Desplegar software, parches y actualizaciones del propio agente.
- Recibir alertas automáticas cuando algo falla (equipo caído, disco lleno, etc.).
- Medir el consumo de ancho de banda de la red de cada farmacia.

El sistema es **multi-tenant**: un mismo panel sirve a las tres unidades de negocio
(SG/MIA/7DIAS) como si fueran clientes independientes — cada usuario ve solo los datos
de las unidades de negocio a las que tiene acceso, salvo el personal interno de soporte
que ve las tres.

### Arquitectura (resumen)

```
┌─ Servidor central (Docker) ─────────────────────────────────┐
│  Panel web Django 5.2 + PostgreSQL 16 + TimescaleDB          │
│  Worker Python (aiomqtt) + Broker MQTT (EMQX, con TLS)       │
│  MeshCentral (acceso remoto de escritorio)                   │
└───────────────────────────────────────────────────────────────┘
                    ▲ MQTT sobre TLS (VPN)
                    ▼
┌─ Cada estación Windows (farmacia) ─────────────────────────┐
│  Agente SAIDSOFT (Python, empaquetado con PyInstaller)       │
│  corre como servicio de Windows, se auto-enrola y reporta    │
│  hardware/estado; recibe comandos y actualizaciones firmadas │
└───────────────────────────────────────────────────────────────┘
```

### Jerarquía del negocio

```
UnidadNegocio (SG / MIA / 7DIAS)
  └── Grupo (canal de versión POS, ej. TRX001, PENDIENTE)
        └── Farmacia (ej. ML001, MAM01)
              └── Estación (ej. ML001-ADM, ML001-A)
```

---

## 2. Ingreso al sistema

1. Ingresar a la URL del panel con **usuario y contraseña**.
2. Si el usuario activó la **verificación en dos pasos (MFA/TOTP)**, el mismo
   formulario de login pide además el **código de 6 dígitos** de la app autenticadora
   (Google Authenticator, Authy, etc.) — sin ese código, el login se rechaza aunque la
   contraseña sea correcta.
3. Tras **5 intentos fallidos** con el mismo usuario, la cuenta queda bloqueada
   **1 hora** (protección contra fuerza bruta).

### Activar la verificación en dos pasos (opcional, por usuario)

Menú de la cuenta → *Verificación en dos pasos*:

1. Se muestra un código QR (y su equivalente en texto, para cargarlo a mano si no se
   puede escanear) — se agrega a una app autenticadora.
2. Se ingresa el código de 6 dígitos que la app genera para confirmar el enrolamiento.
3. A partir de ahí, cada login pedirá ese código además de la contraseña.
4. Para desactivarla hay que confirmar la contraseña actual.

Hoy es **opcional para todos los roles** — no se exige todavía a ningún grupo en
particular (queda como decisión futura).

---

## 3. Roles y qué puede hacer cada uno

SAIDSOFT no tiene una pantalla de "roles" propia: usa el sistema estándar de grupos y
permisos de Django. Cada usuario pertenece a uno o más de estos grupos:

| Rol (Group) | Para quién es | Qué puede hacer |
|---|---|---|
| **Administrador** | Equipo interno con acceso total | Todos los permisos del sistema, sin excepción. |
| **Soporte Técnico** | Técnicos de campo (segunda línea) | Ver/aprobar/reiniciar estaciones, acceso remoto, consultar info, escanear actualizaciones, actualizar el agente; crear y ver scripts/ejecuciones y tareas programadas; ver/editar activos, registrar eventos de un activo, ver ubicaciones y colaboradores. |
| **Mesa de Ayuda** | Primera línea de soporte (diagnóstico) | Ver estaciones, acceso remoto y consultar info — sin acciones de riesgo (no reinicia, no aprueba, no ejecuta scripts). |
| **Técnico** | Personal de campo de activos/inventario (heredado de InvTICS) | Ver/editar activos, ver/crear eventos de activo, ver ubicaciones y colaboradores. |
| **Bodeguero** | Encargado de bodega/compras | Ver/crear/editar activos, ver/editar bodegas, ver/crear/editar stock de bodega y órdenes de compra. |
| **Auditor** | Control interno | Ver activos, eventos de activo, eventos de auditoría, despliegues; supervisión/auditoría de grabaciones de sesión remota (⚠️ el botón existe pero la grabación real no está activada en producción — ver §4.5). |
| **Operador RMM** | Gestión de scripts y monitoreo de flota, sin activos | Ver/crear/editar scripts, ver/crear ejecuciones, ver/crear/editar ventanas de mantenimiento, ver estaciones, métricas, alertas y red de farmacias. |

### Permisos que NUNCA se otorgan por rol, solo persona por persona

Por decisión de gobernanza (auditoría ISO 27001, cierre de los puntos AC-2/AC-3), estos
permisos son deliberadamente más sensibles y se asignan a personas concretas, nunca a un
grupo completo:

- **Ver la clave de recuperación de BitLocker** de una estación (`ver_clave_bitlocker`).
- **Supervisión de grabaciones de sesión remota** (`supervision_auditoria_estacion`) —
  excepto Auditor, que sí la tiene por rol.
- **Aprobar una ejecución de script pendiente** (`aprobar_ejecucionscript`) — hoy
  otorgado individualmente a los supervisores regionales de Soporte Técnico.
- **Aprobar un despliegue pendiente** (`aprobar_despliegue`) — mismo criterio.

Esto implementa un control de **"cuatro ojos"**: quien crea una ejecución de script o un
despliegue hacia un destino amplio (toda la cadena, un grupo o una farmacia completa)
necesita que **otra persona distinta** lo apruebe antes de que se ejecute — nunca puede
aprobar su propia solicitud.

---

## 4. Módulo de Estaciones y Farmacias

### 4.1 Enrolamiento de una estación nueva

Cuando se instala el agente en un equipo nuevo, este se anuncia solo al servidor
(código de estación con formato `FARMACIA-SUFIJO`, ej. `ML001-A`):

1. Si la farmacia del código no existe en el sistema, el enrolamiento se rechaza.
2. Si existe, se crea la `Estación` en estado **Pendiente** — todavía no procesa
   comandos ni heartbeats.
3. Un usuario con permiso (Soporte Técnico o Administrador) la revisa en
   *Estaciones → Pendientes* y la **aprueba** (o **rechaza**) — individualmente o en
   lote.
4. Solo una estación **Aprobada** queda activa: procesa heartbeats, reporta métricas y
   acepta comandos.

**Reenrolamiento** (si el agente pierde su identidad local): el sistema valida que el
identificador de hardware coincida con el que ya tenía guardado — si no coincide,
rechaza y exige reaprobación manual (protección contra suplantación de un equipo por
otro).

### 4.2 Estado de conexión

Cada estación aprobada envía un heartbeat periódico. Si deja de reportar por más de un
umbral configurable (5 minutos por defecto), pasa a **Offline** automáticamente.

### 4.3 Acciones sobre una estación (ficha de detalle)

| Acción | Qué hace | Quién puede |
|---|---|---|
| Consultar info | Pide al agente un refresco de hardware, BitLocker y plan de energía | Mesa de Ayuda, Soporte Técnico, Administrador |
| Reiniciar | Reinicia el equipo remotamente | Soporte Técnico, Administrador |
| Acceso remoto (escritorio/terminal) | Abre una sesión de control remoto vía MeshCentral | Mesa de Ayuda, Soporte Técnico, Administrador |
| Ver grabaciones de sesión ⚠️ | Auditoría de sesiones remotas pasadas — **hoy no funciona en producción** (ver nota abajo) | Auditor, o persona autorizada puntualmente |
| Ver clave de recuperación BitLocker | Muestra la clave descifrada (queda auditado) | Solo persona autorizada puntualmente |
| Escanear actualizaciones (Windows Update) | Pide un escaneo — solo reporta, nunca instala ni reinicia solo | Soporte Técnico, Administrador |
| Consultar software instalado | Pide un inventario de programas instalados | Mesa de Ayuda, Soporte Técnico, Administrador |
| Actualizar agente | Envía la última versión firmada del agente para que se auto-actualice | Soporte Técnico, Administrador |

### 4.4 BitLocker

El sistema guarda si BitLocker está activo (visible para cualquiera con acceso a la
ficha) y, si el agente la reporta, la **clave de recuperación cifrada** — nunca en
texto plano. Ver esa clave es una acción sensible con permiso propio, otorgado persona
por persona, y queda registrada en la auditoría cada vez que alguien la consulta.

### 4.5 MeshCentral (acceso remoto)

Cada estación puede vincularse a un dispositivo de MeshCentral (copiando su
identificador desde la consola de MeshCentral al panel). Una vez vinculada, el botón de
acceso remoto abre directamente el escritorio o la terminal de esa estación.

> 💡 **Copiar y pegar durante una sesión de escritorio remoto no es automático como en
> TeamViewer/AnyDesk** — verificado contra la versión real en producción (MeshCentral
> 1.2.4). Dentro de la sesión de escritorio remoto hay dos formas de hacerlo:
> - **Manual**: dos íconos en la barra de herramientas del visor — uno sube tu
>   portapapeles a la estación, el otro trae el portapapeles de la estación al tuyo. Hay
>   que hacer clic cada vez.
> - **Automático**: dentro de la configuración del visor (ícono de engranaje ⚙️) hay una
>   casilla **"Automatic Clipboard"** — al activarla, el portapapeles se sincroniza solo
>   en ambas direcciones durante esa sesión, igual que TeamViewer/AnyDesk. Viene
>   **desactivada por defecto** en cada sesión nueva.
>
> Es un comportamiento del propio MeshCentral (software de terceros), no algo que
> SAIDSOFT controle desde el servidor — no existe una opción para dejarlo activado por
> defecto para todos los usuarios.

> ⚠️ **"Ver grabaciones de sesión" no funciona todavía en producción.** El botón y el
> permiso existen en SAIDSOFT, pero solo abren la ficha general del equipo en
> MeshCentral (no hay un link directo a una pestaña de grabaciones por dispositivo). La
> grabación en sí **nunca se activó** en el servidor MeshCentral real: requiere agregar
> el bloque `sessionRecording` a `config.json` y marcar el grupo de dispositivos para
> grabación desde la consola de MeshCentral — algo que solo se probó en un contenedor
> suelto de prueba, nunca en el `docker-compose.yml` que corre hoy en producción. Hasta
> que se haga esa configuración y se valide con una estación real, este botón abre
> MeshCentral pero no hay ninguna grabación que ver.

### 4.6 Windows Update (solo escaneo)

Un botón "Escanear ahora" pide a la estación que busque actualizaciones pendientes de
Windows y reporte cuántas hay y si requiere reinicio — **nunca instala ni reinicia
automáticamente**, por el riesgo de hacerlo en un equipo de farmacia en producción.

### 4.7 Farmacias y Grupos

- Cada **Farmacia** pertenece a un **Grupo** (canal de versión del POS) y a una
  **Unidad de Negocio** (SG/MIA/7DIAS).
- La ficha de una farmacia guarda también sus datos operativos: dirección, ciudad,
  coordenadas, horario, administrador, coordinadores zonal/regional, tipo de sucursal
  (propia/asociado), formato (mostrador, autoservicio, etc.), datos de red (IP del
  router, tipo de enlace, si tiene respaldo), y el **técnico de soporte asignado** a esa
  zona.
- El alta/edición masiva de farmacias y grupos se hace desde el panel de administración
  de Django (no tiene pantalla propia en el panel de usuario todavía).

## 5. Módulo de Scripts, Despliegues y Software

Tres formas distintas de llevar cambios a la flota de estaciones, cada una con su
propio nivel de riesgo y control:

| | Para qué sirve | Control de "cuatro ojos" |
|---|---|---|
| **Scripts** | Ejecutar comandos/PowerShell arbitrarios contra la flota | Solo si el destino es amplio (Cadena, Grupos o Farmacias) — un destino a Estaciones puntuales no lo exige |
| **Despliegues** | Actualizar el POS mismo (paquete versionado) | Siempre, sin excepción, para cualquier destino |
| **Software** | Instalar/actualizar/desinstalar software de terceros desde un catálogo | Nunca — riesgo menor, mismo criterio que Scripts sin la exigencia |

### 5.1 Scripts

- **Biblioteca de scripts**: cada `Script` guardado tiene nombre, tipo (hoy solo
  PowerShell), contenido, categoría, y puede ser compartido entre todas las unidades de
  negocio o privado de una sola.
- **Ejecutar un script**: se elige un script de la biblioteca (o se escribe uno
  "al vuelo", ad-hoc, sin guardarlo) y un destino: **toda la cadena**, **grupos**
  específicos, **farmacias** específicas, o **estaciones** puntuales.
- **Aprobación**: si el destino es Cadena, Grupos o Farmacias, la ejecución queda
  *Pendiente de aprobación* — alguien distinto de quien la creó debe aprobarla antes de
  que se publique. Un destino de Estaciones puntuales no lo requiere (ya es acotado).
  Las ejecuciones generadas automáticamente por una tarea programada tampoco piden
  aprobación de nuevo (la política ya se aprobó una vez al crearse).
- **Scripts programados**: una política recurrente ("correr este script cada N días")
  contra un destino fijo, sin pasar por aprobación en cada disparo.
- **Seguimiento**: cada ejecución muestra el progreso por estación (pendiente, enviado,
  ejecutando, completado, error o timeout), con salida estándar/error y código de
  salida.

### 5.2 Despliegues (actualizaciones del POS)

- Se sube un paquete `.zip` versionado con un modo de aplicación: **inmediato**, en una
  **ventana de fecha/hora**, o al **cierre del POS**.
- Todo despliegue nace en estado *Pendiente de aprobación*, sin excepción — alguien
  distinto de quien lo creó debe aprobarlo.
- Una vez aprobado, se **publica** por MQTT a las estaciones del destino elegido.
- **Freno automático**: si el % de estaciones con error supera un umbral configurable,
  el despliegue se pausa solo; un operador puede reanudarlo a propósito (queda marcado
  que el freno se omitió, para no volver a frenarse por el mismo motivo).
- **Promoción por anillos**: un despliegue ya completado en un destino angosto puede
  "promoverse" al siguiente anillo (destino más amplio), manteniendo el mismo paquete.
- Cada estación pasa por una línea de tiempo de pasos: publicado → recibido → descargado
  → hash verificado → (si aplica) POS cerrado → aplicado → POS relanzado → OK/error —
  esta línea de tiempo es inmutable, no se puede borrar.

### 5.3 Catálogo de software de terceros

- Un **catálogo** (`AplicacionCatalogo`) agrupa versiones (`VersionAplicacion`) de un
  mismo programa (ej. 7-Zip, un antivirus, un lector de PDF).
- Una **solicitud de instalación** elige una versión del catálogo, una acción
  (instalar/actualizar/desinstalar) y un destino — sin necesidad de segunda aprobación.
- El agente reporta periódicamente el software instalado detectado en cada estación
  (comparado contra el registro de Windows), lo que permite ver software desactualizado
  frente a la versión más reciente conocida en el catálogo.
- Una política de **inventario programado** puede pedir ese escaneo cada N días de
  forma automática.

### 5.4 Destinos disponibles

En los tres módulos el destino se elige de la misma forma:

- **Cadena**: todas las estaciones aprobadas de la unidad de negocio.
- **Grupos**: todas las estaciones de las farmacias que pertenecen a esos grupos (canal
  de versión POS).
- **Farmacias**: todas las estaciones de esas farmacias puntuales.
- **Estaciones**: las estaciones elegidas una por una.

El sistema nunca deja que un destino "Cadena"/"Grupos" alcance estaciones de una unidad
de negocio distinta a la que se está operando, aunque un grupo esté compartido entre
varias marcas.

## 6. Módulo de Monitoreo, Cumplimiento y Mantenimiento

### 6.1 Monitoreo y alertas

- El agente reporta métricas periódicas de cada estación (CPU, RAM, disco, temperatura,
  latencia, red) que se guardan como historial.
- Un **motor de reglas** (`Regla de alerta`) compara esas métricas contra un umbral
  configurable (mayor/igual o menor/igual) sostenido durante N minutos, y abre una
  **Alerta** (advertencia o crítica) cuando se cumple. También hay reglas especiales:
  estación sin heartbeat, BitLocker deshabilitado, "agente caído pero la red sigue viva"
  (cruce con MeshCentral), y errores del POS por encima de un umbral.
- Cada alerta se puede **reconocer** (alguien la está atendiendo) y **resolver**
  (se solucionó). Si sigue abierta más de 30 minutos, se **escala** (se vuelve a
  notificar).
- **Notificación**: correo a los usuarios con acceso a esa unidad de negocio (o acceso
  total), y opcionalmente un webhook a un canal de Microsoft Teams configurado por
  unidad de negocio (o global).
- **Ventanas de mantenimiento**: mientras una estación/farmacia/grupo/cadena está dentro
  de una ventana activa (con motivo y horario definidos), las alertas de esa estación se
  silencian — para no generar ruido durante un despliegue o mantenimiento planificado.
- **Ancho de banda por farmacia**: cada 5 minutos se consulta el router Mikrotik de cada
  farmacia (por SNMP) y se guarda la tasa de subida/bajada. Es solo panel de
  visibilidad por ahora, sin alertas automáticas todavía.

### 6.2 Cumplimiento

Módulo genérico para dar seguimiento a **iniciativas con fecha límite** que aplican a
estaciones, farmacias o colaboradores (ej. instalación de antivirus, checklist de
apertura de una sucursal nueva, capacitación de 2FA) — no es solo BitLocker.

- Se crea una **actividad de cumplimiento** con su tipo de objetivo (estaciones,
  farmacias o colaboradores), fecha límite, y a quién aplica.
- El sistema genera automáticamente una fila de resultado pendiente por cada objetivo
  que corresponda.
- Alguien marca cada resultado como **completado** manualmente (es una atestación, no
  una verificación automática en esta primera versión) y queda un % de avance.

### 6.3 Mantenimiento

- Un **mantenimiento** es la atención técnica de uno o más equipos (`Activo`), con
  origen manual, por mesa de ayuda (Odoo Helpdesk) o generado por una programación
  recurrente.
- Tiene checklist de actividades, firma digital (del custodio y del técnico), fotos
  adjuntas, repuestos utilizados (que pueden descontar stock real de una bodega), y al
  cerrarse puede generar un **informe en PDF**.
- Puede programarse una recurrencia por equipo (ej. "mantenimiento preventivo cada 90
  días").
- Además existe una **agenda general** del técnico (actividades planificadas) que no
  necesariamente son un mantenimiento formal.
- **App móvil de técnicos**: existe una API propia (autenticación por token) para que
  los técnicos de campo trabajen desde un celular — ver sus mantenimientos asignados,
  iniciarlos, marcar el checklist, firmar, adjuntar fotos y cerrar — incluyendo registro
  de ubicación GPS del técnico, siempre con su consentimiento explícito previo.

### 6.4 Catálogo de software y detección de software desactualizado

- El catálogo (`apps.software`) mantiene qué versión de cada programa es la más
  reciente conocida; el agente reporta qué versión tiene instalada cada estación
  (comparado contra el registro de Windows).
- La pantalla de "software desactualizado" cruza ambos datos y muestra qué estaciones
  quedaron atrás — solo visibilidad, igual criterio que ancho de banda por farmacia.

## 7. Módulo de Activos e Inventario

### 7.1 Compras (Órdenes de compra)

1. Se crea una **Orden de Compra** (proveedor, fecha, bodega(s) destino) y se le agregan
   líneas (activo o consumible, cantidad solicitada).
2. Al llegar la mercadería se registra una **recepción de lote** por línea — puede ser
   parcial (llegan menos unidades de las pedidas). La orden pasa a *Recepción parcial* o
   *Recibida* según cuánto quede pendiente.
3. Si el ítem es un **consumible**, la cantidad recibida entra directo al stock de la
   bodega destino. Si es un **activo**, cada unidad se da de alta individualmente (con
   su propio código único `CR-TIPO-NNNN`).
4. Una recepción puede **anularse** si se registró por error — revierte la cantidad
   recibida y el stock, sin borrar el historial (queda marcada como anulada).

### 7.2 Ciclo de vida de un Activo

```
En bodega → Asignado → (Devuelto → En bodega, o En reparación → En bodega) → Dado de baja
```

- **Asignar** un activo en bodega a un colaborador.
- **Entregar un consumible** asociado (ej. un cartucho de tóner) a quien tiene asignada
  una impresora — descuenta del stock de la bodega.
- **Devolver**: el colaborador entrega el equipo; si necesita reparación pasa a *En
  reparación*, si no vuelve directo a bodega.
- **Enviar a reparación** / **retorno de reparación**.
- **Dar de baja** (destrucción, obsolescencia, robo/pérdida, donación) — un activo dado
  de baja **nunca se elimina** del sistema, queda con ese estado permanentemente junto a
  todo su historial.

Todo movimiento de un activo (ingreso, asignación, devolución, reparación, baja) queda
en su historial de eventos, que tampoco se puede borrar.

### 7.3 Inventario de consumibles (kardex)

Además del stock por bodega, cada movimiento de consumibles (ingreso por compra,
traslado entre bodegas, ajuste por conteo físico, salida por consumo en un
mantenimiento) queda registrado en un kardex permanente con motivo y responsable.

### 7.4 Vínculo automático con las estaciones RMM

Todos los días, el sistema cruza el número de serie que reporta cada estación RMM
contra el número de serie de los activos registrados. Si encuentra una coincidencia
única, los vincula automáticamente (nunca adivina si hay 0 o más de una coincidencia).
Esto habilita dos detecciones automáticas:

- **Activo dado de baja pero conectado**: una señal de que la baja se hizo mal, o que
  hay un número de serie duplicado.
- **Activo movido sin registro**: el equipo aparece funcionando en una farmacia de una
  unidad de negocio distinta a la que el inventario tiene registrada.

### 7.5 Avisos (garantías y stock bajo)

Una pantalla de avisos agrupa, sin enviar correos todavía (solo visibilidad):

- Activos con garantía **vencida o por vencer** (próximos 30 días).
- Consumibles con **stock por debajo del mínimo** configurado (0 = no vigilado).
- Las dos anomalías de vínculo RMM de la sección anterior.

## 8. Cuentas, Auditoría y Facturación

### 8.1 Perfil de usuario y unidades de negocio

Cada usuario del sistema (`auth.User` de Django) puede tener un **Perfil** que define
qué unidades de negocio (SG/MIA/7DIAS) puede ver y sobre cuáles puede actuar:

- Un usuario normal ve solo las unidades de negocio que tiene asignadas en su perfil.
- El personal interno (con `acceso_todas_unidades` activo, o superusuario) ve las tres
  sin restricción.
- Un recurso sin unidad de negocio asignada (ej. un script marcado como compartido)
  siempre es visible para todos, sin importar el perfil.
- El perfil también puede enlazarse a un registro de `Colaborador` (si el usuario es
  además un empleado con activos asignados), y guarda el token de notificaciones push
  del último dispositivo móvil usado (sin envío real implementado todavía).
- La gestión de perfiles/unidades de negocio se hace hoy desde el panel de
  administración de Django, no desde el panel de usuario.

### 8.2 Auditoría

Cada acción relevante del sistema (aprobar una estación, aprobar un despliegue, ver una
clave BitLocker, activar/desactivar MFA, etc.) queda registrada de forma **inmutable**
(no se puede editar ni borrar un evento de auditoría): quién, qué acción, sobre qué
objeto, cuándo, desde qué IP, y detalles adicionales. Hay una pantalla de consulta (los
últimos 200 eventos, filtrable) y un reporte CSV descargable por rango de fechas.

### 8.3 Facturación por endpoint

Cada mes, se considera "endpoint activo" (facturable) a toda estación que envió al
menos un heartbeat ese mes calendario — sin importar cuántos. Este dato se guarda de
forma permanente (a diferencia de las métricas, que se purgan a los 30 días), así que
sirve como base histórica de facturación mes a mes. Hay un reporte CSV y un resumen
dentro del reporte ejecutivo por cliente.

### 8.4 Reportes

Desde *Reportes* se puede exportar en CSV: auditoría, cumplimiento, un despliegue
puntual, activos, alertas, facturación y software instalado — todos acotados a las
unidades de negocio visibles para quien los descarga. También existe un **resumen
ejecutivo por cliente** (una sola pantalla con cumplimiento, despliegues, alertas,
activos y facturación de un rango de fechas).

### 8.5 Panel principal (dashboard)

Accesible para cualquier usuario autenticado (sin permiso especial, pero con los datos
acotados a lo que puede ver): matriz de cumplimiento de versión de POS por grupo,
despliegues en curso, cantidad de estaciones pendientes de aprobar, alertas abiertas,
total de estaciones online/offline, y el estado de salud del propio servicio de
mensajería (worker MQTT).
