# Auditoría SAIDSOFT — FASE 5: triage de los 12 hallazgos ALTO

Hecho el **11-oct-2026** sobre `master` @ `d19bf38`, 7 commits después de la línea base de
la Fase 4 (`0515773`). Cada hallazgo se volvió a leer en el código de hoy: la Fase 4 midió
sobre un árbol que ya no es el actual, y entre medio entraron el trabajo de permisos del
panel (`083c3d1`…`9bd7b35`) y las cuatro fases del Centro de Monitoreo (`60415b8`).

**Esta fase no corrige nada todavía y no ejecutó ninguna prueba.** Lo que agrega sobre la
Fase 4 es: (1) si el hallazgo **sigue vigente** hoy, con la línea exacta; (2) qué hace falta
para reproducirlo —y cuáles se pueden reproducir **en local**, sin tocar producción ni el
broker—; (3) un **orden** con el motivo, que es lo que §10-BH dejó pedido.

De los 12 ALTO, **11 seguían vigentes** y 1 ya estaba corregido (`BUG-01`, en `ecc9e70`).
Ninguno fue tocado por accidente por el trabajo de estos días.

## Estado al cierre del 11-oct-2026

**Diez de los 12 ALTO están cerrados** —uno antes del triage, cuatro el día del triage y
cinco el 12-oct— y **2 siguen abiertos**: `INV-07`, que necesita mirar dos equipos físicos, y
`C-1`, que viaja con la tanda de la app móvil.

**Y los 94 hallazgos resultaron ser 92:** al triar los MEDIO/BAJO (§10) aparecieron dos pares
de duplicados que la deduplicación original no detectó. De esos 92 quedan **77 abiertos**: 2
ALTO y 75 MEDIO/BAJO, con el orden propuesto en §10.

Cerrados además, fuera de los 94: el **trust-on-first-use** (§10-BÑ) y el **angostamiento de
la ACL de la credencial compartida** (§10-BQ), que llevaba desde septiembre esperando una
verificación que nadie podía hacer.

| ID | Estado | Dónde está documentado |
|---|---|---|
| `BUG-01` | **CERRADO** antes del triage, en `ecc9e70` | §10-BH |
| `INV-01` | **CERRADO** — reproducido primero, el primer `Reproducción: SÍ` de la auditoría | §5 · §10-BJ |
| `RIESGO-06` | **CERRADO** en el código, el servidor y los catálogos del broker. En la flota: **14 de 66** estaciones con el agente 0.33 (§10-BP) | §6 · §10-BK |
| `B-1` | **CERRADO** | §7 · §10-BL |
| `B-2` | **CERRADO el daño** (la expulsión del broker). El hueco de trust-on-first-use resultó **teórico**: 0 estaciones sin identidad — ver §9 | §7 · §10-BL |
| `A-6` | **CERRADO** en el código; en la flota, las mismas **14 de 66** del agente 0.33 | §8 · §10-BM |
| `A-2` | **CERRADO** — el paquete de apertura ya no escribe el HMAC compartido | §10-BR |
| `C-1` | abierto — conviene que entre con la tanda de la app móvil | §3 |
| `INV-02` | **CERRADO** — ya no roba el vínculo ni elige al azar entre series duplicadas | §10-BS |
| `INV-04` | **CERRADO** — la tarea diaria resincroniza la farmacia desde la estación | §10-BS |
| `INV-05` | **CERRADO** — las tres rutas heredan la unidad; 3 filas viejas se corrigen con un comando | §10-BS |
| `INV-07` | abierto — necesita mirar la etiqueta física de dos equipos | §9 |

**Las dos cosas que hay que hacer para que lo cerrado llegue a la flota:** compilar y
distribuir el **agente 0.33** (lleva `RIESGO-06` y `A-6`; se actualiza solo por MQTT, lo
manual es el build) y **desplegar el backend**. Hasta entonces, el freno de `RIESGO-06` que
no depende de nada es `deploy/emqx-narrow-acl-agente.sh`.

**Lo que estas correcciones dejaron pendiente y comparte una sola pasada de solo lectura
contra producción:** cuántos activos estaban congelados (`INV-01`), si hay series duplicadas
(`INV-07`), cuántas estaciones tienen `hardware_id` vacío (el hueco de `B-2`, que `A-6` no
cierra) y cuántas entraron al denominador del KPI al dejar de excluir el grupo `PENDIENTE`
(`L-1`, del informe del tablero).

Las cuatro están escritas y listas para correr en **`docs/auditoria-fase5-verificacion.sql`**,
con el comando de `psql` dentro del contenedor que ya usan las otras dos baterías. Solo
`SELECT`, ni un `UPDATE`. **Dos de ellas deciden cómo se arregla un hallazgo que sigue
abierto**: si hay series duplicadas, `INV-07` empieza por resolverlas a mano y no por un
constraint —una migración que imponga unicidad sobre datos sucios falla en el despliegue, la
misma trampa que hizo descartar el constraint de `EventoEnlaceFarmacia`—; y cuántas
estaciones están sin identidad de hardware decide si el hueco de trust-on-first-use se cierra
a mano o necesita un camino en el panel.

---

## 1. Vigencia verificada hoy

| ID | Vigente | Línea verificada hoy | Qué hace falta para reproducirlo |
|---|---|---|---|
| `RIESGO-06` | **CORREGIDO en el código**, pendiente de distribuir el agente | `monitoreo/servicios_pos.py` (`envolver_firmado`) · `agente_prueba.py` (`_catalogo_verificado`) | **Cerrado del lado del servidor** — ver §6 |
| `B-1` ≡ `RIESGO-07` | **CORREGIDO** | `emqx_admin.py` (`revocar_credencial_estacion`) · `catalogo/services.py` (`rechazar_estacion`) · `catalogo/admin.py` | **Cerrado** — ver §7 |
| `B-2` | **CORREGIDO en el daño**, hueco de TOFU abierto | `emqx_admin.py:124` · `mqtt_worker/services.py` (`_tiene_latido_fresco`) | **Cerrado para estaciones vivas** — ver §7 |
| `A-6` | **CORREGIDO** | `agente_prueba.py` (`leer_machine_guid`) | **Cerrado en el código**; viaja en el agente 0.33 con `RIESGO-06` — ver §8 |
| `A-2` | **CORREGIDO** | `generar_paquete_apertura` (`_hmac_secret_para_el_config`) | **Cerrado** — ver §10-BR |
| `C-1` ≡ `RIESGO-11` | **SÍ** | `mantenimiento/api_views.py:75` filtra por `(usuario, origen_id)` · `models.py:803-806` el constraint es el mismo par · `sincronizador.dart:91` manda `origen_id = accion.id` | **Prueba local**, sin teléfono |
| `INV-01` | **CORREGIDO** | `activos/models.py:592-640` · `admin.py:213-215` | **Reproducido y cerrado** — ver §5 |
| `INV-02` ≡ `HALLAZGO-19` | **SÍ** | `activos/services.py:788` — `filter(numero_serie__iexact=serie).first()`, sin `estacion__isnull=True` y sin orden, contra la guarda correcta de `:669-676` | **Prueba local** |
| `INV-04` | **SÍ** | `activos/services.py:674` escribe `farmacia` al vincular y nada la resincroniza después | **Prueba local** |
| `INV-05` | **SÍ** | `activos/services.py:153-154` — la herencia de `unidad_negocio` ocurre solo en `registrar_ingreso` | **Prueba local** |
| `INV-07` | **SÍ** | `activos/models.py:452` — `numero_serie = CharField(blank=True)`, sin unicidad ni constraint | **Consulta de solo lectura en producción**: saber si hoy hay duplicados |
| `BUG-01` | corregido | `ecc9e70` | — |

**Lo que cambió respecto de la Fase 4:** nada en los hallazgos, pero sí en el contexto de
dos de ellos. `R-3` del informe del tablero agregó `catalogo/0036`, un índice sobre
`(estado_aprobacion, ultimo_heartbeat)` de `Estacion` — la misma columna que `B-1` dice que
el enrolamiento no mira. Y la Fase D del tablero reescribió `run_mqtt_worker`, vecino de
`manejar_enrolamiento`, sin tocarlo.

---

## 2. El criterio del orden

No es por severidad: los 12 son ALTO. Es por **daño que ocurre hoy ÷ costo de arreglarlo**,
con dos desempates que este proyecto ya aprendió a mirar:

- **Lo que se arregla antes de que el módulo se use, se arregla una vez.** Los cinco `INV-*`
  son de integridad del inventario, y hoy hay **22 activos** contra 701 farmacias
  (`CLAUDE.md`). El daño actual es casi nulo y el costo de arreglarlos nunca va a ser más
  bajo que ahora: cuando haya 1.800, cada uno de estos deja filas sucias que hay que
  reconciliar a mano. La excepción es `INV-01`, que molesta hoy.
- **Lo que cruza al agente o al teléfono paga el peaje de la distribución.** Un arreglo en
  `agente-prueba/` o en `movil-campo/` no está hecho cuando está commiteado: hay que
  distribuirlo, y la app ya tiene **seis cambios acumulados sin distribuir**. Eso no los
  baja de prioridad, pero sí dice que conviene agruparlos en una tanda, no sueltos.

---

## 3. Orden propuesto

### Primero — `RIESGO-06`: los dos mensajes que el agente obedece sin verificar

Los catálogos de servicios POS y de eventos de Windows son los **únicos dos** mensajes
dirigidos al agente que viajan sin firma HMAC, y el agente los aplica directo. El catálogo
dirige actividad de red del agente (`urlopen`, `psycopg2.connect`, `ping`) contra destinos
que vienen en el propio mensaje, y devuelve el resultado —incluido el texto del error— por
`/saidsof/agente/{codigo}/servicios_pos/`. Es decir: quien pueda publicar ahí consigue 66
equipos dentro de las redes de las farmacias haciendo peticiones y contándole qué pasó. Y
va con `retain=True`, así que el mensaje sobrevive y lo recibe cada agente que reconecta.

Va primero porque **es el único de los once que convierte una credencial filtrada en control
de la flota**, y porque la credencial compartida sigue viva: dos estaciones apagadas la usan
todavía y `deploy/emqx-narrow-acl-agente.sh` no se corrió. Los otros diez necesitan algo
más que una credencial.

El arreglo es simétrico con lo que ya existe: `firmar_payload` del lado del servidor y
`_firma_valida` del lado del agente, que ya protegen los otros siete tipos de mensaje. La
parte delicada es la compatibilidad, y el proyecto ya tiene el patrón escrito para esto
(`hmac_propio_confirmado`, `VERSION_AGENTE_CON_HMAC_PROPIO`): el servidor firma desde ya, el
agente nuevo exige firma, y los agentes viejos siguen aceptando sin firma hasta que se
actualicen. Firmar primero y exigir después, nunca al revés.

### Segundo — `INV-01`: el 500 que congela un activo

Un activo con estación vinculada **y** `ip`/`mac` cargadas de antes no se puede guardar
desde el admin, que es el único lugar del sistema donde se edita un activo existente. El
operador recibe un 500 y el síntoma ("no puedo guardar este equipo") no apunta a la causa.
El mecanismo está confirmado por lectura: `get_readonly_fields` saca `ip` y `mac` del
formulario cuando hay estación, `Model.clean()` devuelve el error **dirigido a `ip`**, y
`BaseForm.add_error` sobre un campo que no está en el form lanza `ValueError`.

Va segundo porque es el único de los once que un operador puede estar sufriendo hoy, y
porque la fila que lo dispara **la produce el flujo normal**: se inventaría el equipo con la
IP de la planilla, se le instala el agente, y el Beat de las 04:00 lo vincula por serie. Se
reproduce con una prueba local —un POST al admin— y el arreglo es de pocas líneas.

### Tercero — `B-1` y `B-2` juntos: la baja que no existe

Van juntos porque viven en las mismas dos funciones y porque el arreglo de uno abre el del
otro. `B-1`: rechazar una estación en el panel no le quita el acceso al broker, y no hay
ninguna función que dé de baja un usuario de EMQX, así que una estación rechazada conserva
credencial válida y recibe una nueva cada vez que reintenta. `B-2`: como el tópico de
enrolamiento es global y `_respuesta_aceptado` rota la contraseña **como efecto colateral de
responder**, una estación comprometida puede expulsar del broker a cualquier otra de la
cadena pidiendo su re-enrolamiento en un bucle.

Son tres cambios acotados, todos del lado del servidor —no hay que distribuir nada—:
no aprovisionar a una estación `RECHAZADA`; dar de baja el usuario y sus reglas al rechazar
y al borrar la `Estacion` (un `DELETE` sobre las dos rutas que `_peticion` ya sabe hablar);
y rotar la credencial solo después de pasar todas las validaciones.

### Cuarto — `A-6` y `A-2`: dos arreglos chicos que viajan con el agente

`A-6` es un `except` que devuelve una constante: dos equipos donde falle la lectura del
`MachineGuid` comparten `hardware_id`, y eso anula la única defensa del servidor contra la
suplantación en el re-enrolamiento — la misma que `B-2` necesita que funcione. `A-2` es el
`config.txt` de las aperturas, que sigue volcando el `COMANDO_HMAC_SECRET` de toda la flota
cuando el fan-out por estación ya lo volvió innecesario; el criterio correcto ya está escrito
al lado, en `armar_paquete_agente` (`PASO_1_CON_HMAC_PROPIO` vs. `PASO_1_CON_HMAC_COMPARTIDO`),
y **depende de la versión del binario que se empaqueta**: vaciarlo con un agente anterior a
0.21 produce el peor resultado posible —instalación correcta y comandos descartados en
silencio—, así que la corrección es copiar ese criterio, no vaciar el campo.

Van en cuarto lugar porque el arreglo de `A-6` no sirve de nada hasta que el agente se
actualice en los equipos, y conviene que viaje con `RIESGO-06`, que también toca el agente.

### Quinto — `C-1`: la clave de idempotencia que compara el id de otro teléfono

La clave es `(usuario, origen_id)` y `origen_id` es el autoincremento de SQLite del teléfono.
Un técnico con dos teléfonos, o que reinstala la app —lo que reinicia el contador—, manda una
acción nº 5 que el servidor considera ya aplicada y le devuelve **la respuesta guardada del
hecho ajeno**. El trabajo de campo se pierde sin que nada falle, que es la peor forma.
`tipo` se guarda en la fila pero no participa ni de la búsqueda ni del constraint, así que
un "cerrar visita" puede quedar absorbido por un "registrar viático".

Va quinto, y no antes, por una razón de oportunidad y no de gravedad: el arreglo correcto
—un identificador de instalación generado por la app, que hoy no existe— cruza Flutter,
backend y una migración, y los teléfonos que están en la calle **no tienen el aviso de
versión nueva**. Conviene que entre con la tanda de los seis cambios sin distribuir, en una
sola vuelta de instalación teléfono por teléfono. Mientras no esté, el daño es silencioso:
vale mirar cuántos usuarios tienen más de un dispositivo antes de decidir cuánto esperar.

### Sexto — `INV-02`, `INV-04`, `INV-05`, `INV-07`: integridad antes de cargar el inventario

Los cuatro describen la misma clase de problema: el cruce declarado ↔ observado funciona
cuando los datos están limpios y falla **en silencio** cuando no. `INV-02` es el peor de los
cuatro, porque no solo falla: `crear_activos_desde_estaciones` le **roba** el vínculo a otra
estación (`.first()` sin `estacion__isnull=True` y sin orden), donde la función hermana a 100
líneas de distancia lo hace bien. `INV-07` es su causa de fondo —`numero_serie` sin
unicidad— y es el único de los once que **necesita medir producción primero**: una consulta
de solo lectura que diga si hoy hay series duplicadas, antes de decidir si el arreglo es un
constraint o una reconciliación a mano. `INV-04` (la farmacia que queda vieja cuando la
estación se muda) e `INV-05` (el activo que queda `unidad_negocio=None`, o sea compartido
entre clientes, salvo que entre por `registrar_ingreso`) son del mismo tenor.

Van al final por el primer desempate del §2, no porque sean menores: con 22 activos cargados
el daño de hoy es casi nulo, los cuatro se reproducen con pruebas locales, y este es el
momento más barato en que van a poder arreglarse.

---

## 4. Lo que este triage NO decide

- **Nada está reproducido.** Los siete marcados "prueba local" se pueden reproducir sin
  tocar producción ni el broker, y eso es lo único que puede moverlos a `Reproducción: SÍ`.
  Hace falta la autorización para ejecutar pruebas que §10-BH dejó pendiente.
- **Los 81 hallazgos de severidad MEDIA y BAJA siguen sin triar.** Entre ellos está `BD-11`,
  que propone tocar índices y cuyo propio informe pide medir antes.
- **`INV-07` y `L-1` comparten una consulta de producción** que nadie corrió todavía. Vale
  juntarlas en una sola pasada de solo lectura cuando haya acceso.

---

## 5. `INV-01`, cerrado el 11-oct-2026

Entrada del plan: **§10-BJ**.

**Reproducido primero, y eso es la novedad.** Cinco pruebas nuevas en
`apps/activos/tests.py` (`AdminEdicionConEstacionVinculadaTests`), tres de ellas en rojo
antes de tocar el modelo, con exactamente el error que la ficha predijo por lectura:

```
ValueError: 'ActivoForm' has no field named 'ip'.
```

Es el primer hallazgo de la Fase 4 que llega a `Reproducción: SÍ`. La ficha había
reconstruido el mecanismo entero —`get_readonly_fields` → `exclude` del ModelForm →
`Model.full_clean()` que no filtra los errores de `Model.clean()` → `add_error` sobre un
campo ausente— sin ejecutar nada, y acertó en todo.

**El arreglo fue hacer lo que el docstring ya decía.** `Activo.clean()` prometía *"lo que se
evita es que una persona los EDITE cuando ya hay una fuente automática, no que la fila
exista"*, y rechazaba por el estado de la fila. Ahora compara contra lo que hay en base:

- un dato **heredado** (ya estaba antes de esta edición) deja guardar — `ip_efectiva` ya
  resuelve cuál de las dos fuentes manda, así que no hay ambigüedad que proteger;
- un dato **introducido o cambiado** en esta edición se rechaza, igual que antes;
- y cada error va al campo que la persona tocó —`ip` a `ip`, `mac` a `mac`—, que por
  construcción es uno que sí está en el formulario. Antes el error iba siempre a `ip`,
  incluso cuando el dato cargado era la MAC: además del 500, señalaba el campo equivocado.

No se borra la IP manual al guardar, y hay una prueba que lo fija: es el único rastro de lo
que decía la planilla, y `ip_efectiva` ya la relega sola.

**Lo que NO se hizo:** la opción (b) de la ficha —dirigir el error a `NON_FIELD_ERRORS`
cuando el campo no está en el formulario— se descartó. Es un parche sobre el síntoma: deja
la regla rechazando por estado y solo cambia dónde aparece el error, así que el activo
seguiría sin poder editarse, ahora con un mensaje arriba en vez de un 500. Con la opción (a)
el 500 es imposible por construcción: un error dirigido a `ip` solo se emite si `ip` cambió,
y solo puede cambiar si está en el formulario.

**Alcance:** 216 pruebas de `apps.activos` en verde, incluidas las 9 de `TopologiaActivoTests`
que defienden la regla original. Sin migración: `clean()` no es un constraint.

**Queda pendiente la consulta de verificación de la ficha**, que dice cuántos activos hay hoy
en ese estado —cada uno era un equipo congelado— y que conviene correr en la misma pasada de
solo lectura que `L-1` e `INV-07`:

```sql
SELECT a.id, a.codigo, e.codigo AS estacion, a.ip, a.mac
FROM activo a
JOIN estacion e ON e.id = a.estacion_id
WHERE a.estacion_id IS NOT NULL
  AND (a.ip IS NOT NULL OR a.mac <> '');
```

---

## 6. `RIESGO-06`, firmado el 11-oct-2026

Entrada del plan: **§10-BK**.

Los catálogos de servicios del POS y de eventos de Windows eran los **dos únicos** mensajes
dirigidos al agente que viajaban sin firma HMAC, y el agente los aplicaba sin pasar por
`_firma_valida`, a diferencia de los otros siete tipos. Lo que un catálogo decide es contra
qué host hace `urlopen`, `psycopg2.connect` o `ping` cada agente, y el resultado —incluido
el texto del error— vuelve publicado a `/saidsof/agente/{codigo}/servicios_pos/`. Van
retenidos y el agente los persiste en `identidad.json`, así que un catálogo envenenado
alcanzaba a las estaciones apagadas y sobrevivía al reinicio del servicio.

**Lo que se firma es un string, no un objeto.** El contenido viaja serializado en la clave
`catalogo` y la firma se calcula sobre ese string exacto. No es un detalle de estilo: el
docstring de `firmar_payload` existe porque el proyecto decidió **no** depender de que
`json.dumps` en Python y `System.Text.Json` en el agente canonicalicen igual. Firmar el
objeto habría obligado al agente a re-serializarlo para verificar, que es justamente la
trampa que ese docstring advierte. El agente verifica contra el string tal como llegó y
**lee de ahí**, no de las claves de primer nivel: si leyera de las claves, alcanzaría con
reenviar una firma legítima acompañada de unas claves cambiadas.

El secreto es el **compartido** de la flota, como en los tópicos de difusión de despliegue y
software, por la razón que ya está escrita en `firmar_payload`: un solo payload que verifican
muchas estaciones. El `comando` entra en la firma y acá hace un trabajo extra —impedir que
un catálogo de servicios válido se reenvíe por el tópico de eventos, que lleva la misma
ACL—, y hay una prueba para eso. El `timestamp` se firma pero no se valida contra ventana
(`verificar_ventana=False`), igual que `actualizar_agente`: el mensaje puede quedar días en
el broker.

**La compatibilidad es la parte delicada, y va en el orden que este proyecto ya aprendió:
firmar primero, exigir después.** Las claves de primer nivel se siguen mandando duplicadas,
así que un agente anterior a 0.33 entiende el mensaje igual; el 0.33 exige firma válida y
descarta lo que no la tenga. Un servidor nuevo con agentes viejos no rompe nada, y un agente
0.33 contra un servidor viejo —sin firma— aplica el catálogo dejando un `WARNING` en su log
en vez de quedarse con la lista vieja.

**Lo que esto NO cierra todavía.** Mientras haya agentes anteriores a 0.33 en la flota,
ellos siguen aceptando un catálogo forjado: el arreglo completo es tener el 0.33 corriendo,
no el commit. La buena noticia es que el agente Windows **se actualiza solo**: se compila el
ejecutable, se carga una `VersionAgente` con su sha256 y se dispara
`enviar_actualizacion_agente`, retenida, así que alcanza también a las apagadas. Lo manual
es el build, no la instalación equipo por equipo (eso es la app móvil).

El freno que no depende de nada de eso es `deploy/emqx-narrow-acl-agente.sh` —con la ACL
angosta ninguna estación puede publicar en estos dos tópicos—, que a su vez espera a que las
2 estaciones apagadas migren a credencial propia. Es la misma dependencia que ya bloquea la
rotación de `MQTT_PASSWORD_AGENTE` y el paquete del instalador completo.

**Un paso operativo que el commit no resuelve y es fácil de olvidar:** los catálogos viajan
**retenidos**, así que el mensaje que hoy está guardado en EMQX se publicó sin firma y ahí se
queda hasta que alguien publique uno nuevo. Después de desplegar hay que republicar los dos.
El de servicios del POS ya tenía comando; **el de eventos de Windows no lo tenía y se
escribió acá** (`publicar_catalogo_eventos_sistema`, simula por defecto como su gemelo),
porque hasta entonces la única forma de republicarlo era entrar al admin y guardar una fila
cualquiera para disparar su `save_model`. El runbook está en `deploy/README-produccion.md`,
sección "Desplegar las correcciones de la auditoría por agentes".

**Alcance:** 7 pruebas nuevas (`CatalogoFirmadoTests`), 835 de `apps.monitoreo` +
`apps.mqtt_worker` en verde. Sin migración. Del lado del agente no hay suite —es un script
aparte—, así que las pruebas reconstruyen la firma exactamente como lo hace él, el mismo
método que usa `apps/despliegues/tests.py` para los tópicos de difusión.

---

---

## 7. `B-1` y `B-2`, cerrados el 11-oct-2026

Entrada del plan: **§10-BL**. (La sección 6 documenta `RIESGO-06`, que viajó en su propia
rama; esta entrada y esa se escribieron el mismo día, en ramas separadas.)

Fueron juntos porque viven en las mismas dos funciones. `B-1`: `emqx_admin` sabía dar de
alta y no dar de baja, así que **rechazar una estación no le quitaba el acceso al broker** —
conservaba usuario MQTT con ACL sobre sus tópicos y sobre los dos catálogos globales, y cada
reintento de enrolamiento le entregaba contraseña nueva; borrar la fila dejaba el usuario
huérfano en EMQX para siempre. `B-2`: el tópico de enrolamiento es global y no lleva el
código en la ruta, y responder **rotaba la contraseña de la víctima** como efecto colateral,
así que una estación comprometida podía expulsar del broker a toda la cadena en un bucle.

Lo que se hizo está en §10-BL. Acá va lo que el triage no podía anticipar y apareció al
implementar:

**La ficha recomendaba negar el re-enrolamiento de una estación con latido fresco. Se
descartó, y la evidencia estaba en el agente.** Dos cosas:

1. **El re-enrolamiento legítimo de una estación viva existe**: es el de `_on_connect`
   cuando el agente tiene token pero no su secreto HMAC propio, puesto deliberadamente tras
   el incidente del 16-sep-2026 que dejó mudas a dos estaciones del piloto. Negarlo habría
   roto esa auto-reparación.
2. **El agente solo pide enrolarse al conectar y no reintenta.** Un rechazo lo habría dejado
   mudo hasta la próxima reconexión al broker.

En su lugar se **dejó de rotar** la credencial cuando la estación late. Elimina el daño de
`B-2` —la expulsión— sin pedirle nada al agente y sin tocar la auto-reparación, porque lo
que ese camino busca es `hmac_secret`, que se entrega siempre. Y la asimetría de costos
queda del lado seguro: si el criterio se equivoca, la estación sigue usando la credencial
que tiene y lo reintenta cuando el latido envejezca; negarle el enrolamiento la habría
callado.

**Otro hallazgo del camino:** la revocación necesitaba **tres** resultados y no dos. `False`
("EMQX no confirmó, sigue autenticando") y `None` ("esta instalación no usa credenciales por
estación") piden mensajes distintos: sin la diferencia, el panel avisaba de un fallo que no
había ocurrido en toda instalación sin `EMQX_ADMIN_CONFIG` — es decir, en desarrollo y en
cada prueba.

**El repo corrigió el primer intento.** La lógica del rechazo —marcar la fila y revocar—
había quedado en la vista, y `DivisionDeVistasTests` la rebotó: hay un techo de 550 líneas
por módulo de vistas y `estaciones.py` estaba a cinco de alcanzarlo. La prueba dice "la
respuesta es dividirlo, no subir el límite", y acá la respuesta era una tercera: eso no iba
en la vista. Quedó en `catalogo.services.rechazar_estacion`, como una sola operación —un
rechazo que no revoca es el defecto que se estaba arreglando, así que separarlos otra vez en
dos llamadas es cómo volvería—. Vale anotarlo porque la prueba de arquitectura hizo
exactamente lo que se le pide: avisar antes, no después.

**Y una decisión de arquitectura que el repo ya tenía tomada:** el borrado se engancha en
`EstacionAdmin.delete_model`/`delete_queryset`, no en una señal `post_delete`.
`FarmaciaAdmin.save_model` ya dejó escrito por qué —este proyecto no usa señales en ninguna
app, y esconder un efecto así hace mucho más difícil entender después por qué una credencial
dejó de funcionar—, así que se siguió ese criterio en vez de abrir el primer caso.

**Lo que queda abierto:** el hueco de **trust-on-first-use**. Una estación con `hardware_id`
vacío puede ser suplantada por quien acierte su código, y si está **apagada**, la rotación
forzada todavía la expulsa: la no-rotación solo protege a las vivas. Dimensionarlo es una
consulta de solo lectura —cuántas estaciones tienen hoy `hardware_id` vacío— que va en la
misma pasada que `L-1`, `INV-07` y la de `INV-01`. `A-6` es la otra mitad del mismo hueco.

**Alcance:** 16 pruebas nuevas en `apps.mqtt_worker`, `apps.catalogo` y `apps.panel`. Sin
migración.

---

## 8. `A-6`, cerrado el 11-oct-2026

Entrada del plan: **§10-BM**. Se hizo **junto a `RIESGO-06`, en el mismo agente 0.33**, y no
en el orden del §3: los dos son del agente, y separarlos obligaba a compilar y rotar el
ejecutable en los 66 equipos dos veces. Cuando el orden de un triage choca con el costo de
distribuir, manda el costo de distribuir.

`leer_machine_guid()` caía a una constante de reserva cuando no podía leer el registro, así
que dos equipos donde la lectura fallara presentaban la **misma** identidad de hardware — y
esa identidad es la única defensa del servidor contra la suplantación en el re-enrolamiento.
El valor, además, estaba escrito en el repo.

Ahora devuelve vacío. Las alternativas se descartaron con un motivo cada una: un UUID
generado y guardado se pierde junto con `identidad.json`, que es el único caso que hay que
cubrir; y algo derivado del hostname o del número de serie vuelve a ser adivinable por quien
ya conoce el código de la estación, que es el atacante de `B-2`. Vacío es lo honesto, y el
servidor ya lo trata del lado seguro: no lo fija como identidad y niega el re-enrolamiento
de una estación que sí tenía una. Las dos cosas quedaron con prueba, porque son lo que hace
seguro devolver vacío.

**Un detalle del camino que vale anotar:** la primera versión del arreglo dejó la constante
nombrada en el docstring que explicaba el defecto, y la prueba que vigila el fuente del
agente la encontró ahí. Tenía razón: el valor no tiene por qué seguir estando en el repo ni
como ejemplo. Quedó descrito sin escribirlo.

**Lo que no cierra:** el trust-on-first-use. `A-6` quita el valor compartido y conocido, pero
una estación con `hardware_id` vacío sigue aceptando el primero que llegue. Cuántas están en
ese estado es una de las cuatro consultas pendientes.

---

## 9. Las cuatro mediciones, corridas el 11-oct-2026

Desplegado `5604833` en el NUC y corrido `docs/auditoria-fase5-verificacion.sql` contra
produccion. Las cuatro respuestas, y lo que cada una decide:

| Bloque | Resultado | Consecuencia |
|---|---|---|
| `A` · `INV-01` | **0 activos congelados** | El bug era real —se reprodujo con prueba— pero no habia alcanzado a nadie |
| `B` · `INV-07` | **1 duplicado**: `BB3VJD3` en `CR-DSK-0002` y `CR-LAP-0001` | El constraint **es viable**: hay una sola fila que resolver |
| `C` · `B-2` | **0 estaciones sin `hardware_id`**, 66 con | El hueco de trust-on-first-use **es teorico hoy** |
| `D` · `L-1` | 66 = 66, 35 en linea con los dos umbrales, 0 entran | **El KPI no cambio** |

### `INV-01`: se atrapo antes de que doliera

Cero filas. El 500 era real y reproducible —tres pruebas en rojo antes del arreglo— pero
ningun activo de produccion estaba en ese estado todavia. Vale anotarlo porque es la
respuesta a "¿cuanto importaba?": importaba por lo que iba a pasar, no por lo que ya habia
pasado. La fila que lo dispara la produce el flujo normal, asi que era cuestion de tiempo.

### `INV-07`: un duplicado, y no es un duplicado legitimo

`BB3VJD3` aparece en **un desktop y un laptop**. Un desktop y un laptop no comparten numero
de serie: es un error de carga, no dos equipos con el mismo serial de fabrica. Y no es una
serie de relleno del BIOS (el bloque B2 dio cero), asi que no se explica por la captura
automatica.

**Esto decide el orden del arreglo de `INV-07`:** primero averiguar cual de los dos equipos
tiene el serial correcto —cosa que necesita mirar las etiquetas fisicas, no la base— y
corregir esa fila; despues el constraint de unicidad, que con una sola fila sucia deja de ser
el riesgo que era. La alternativa que se descarto en §10-BI (imponer unicidad sobre datos
sucios y que falle el despliegue) ya no aplica a esta escala.

Tambien explica con que se equivocaba `INV-02`: el robo de vinculo necesita exactamente esto,
dos activos con la misma serie.

### `B-2`: el hueco de trust-on-first-use no existe en la practica

Las **66** estaciones tienen `hardware_id` fijado; ninguna esta en TOFU. La guarda
anti-suplantacion esta activa en toda la flota, asi que hoy no hay ninguna estacion
suplantable por ese camino.

**Esto invierte lo que este documento decia.** Dos cosas cambian:

1. No hace falta un camino en el panel para fijar identidades: no hay ninguna que fijar.
2. **Se puede endurecer la regla ahora sin romper nada** —negar el trust-on-first-use para
   una estacion que ya existe— precisamente porque hoy ninguna lo necesita. Conviene hacerlo
   mientras el numero es cero: despues, con estaciones nuevas en el medio, el mismo cambio
   deja a alguna afuera.

Queda como el siguiente paso natural de este tema, y es chico.

### `L-1`: la advertencia no se materializo

Cero farmacias aprobadas en el grupo `PENDIENTE`, asi que el denominador nuevo y el viejo dan
lo mismo (66), y los 35 en linea son los mismos con 5 o con 10 minutos. **No hay ningun
numero que cambie en el panel y no hay nada que avisarle a la mesa de ayuda.**

La advertencia estaba bien puesta —el cambio de denominador y de umbral podia mover el KPI— y
lo que la medicion dice es que en esta flota no lo mueve. De paso: 35 de 66 en linea, y
ninguna estacion que nunca haya reportado.

---

## 10. Triage de los 81 MEDIO/BAJO (12-oct-2026)

El bloque que nadie había leído. Al revisarlo aparecieron tres cosas antes de entrar a
priorizar, y las tres cambian el número:

### Los 94 hallazgos son en realidad 92

La Fase 4 dedujo seis pares de duplicados. Se le pasaron **dos más**, y por el mismo motivo
en los dos casos: los IDs viven en informes de agentes distintos y describen lo mismo con
palabras distintas.

| Par | Qué es |
|---|---|
| `B-5` ≡ `DOC-01` | El §4 del descubrimiento dice que la credencial MQTT la da la **aprobación** humana; la da el **enrolamiento** |
| `B-6` ≡ `DOC-02` | `servicios_pos.py:31` remite a `emqx_admin.reglas_acl_estacion`, que no existe (es `_reglas_para`) |

Vale anotarlo junto a la lección que §10-BH ya dejó escrita: deduplicar por `archivo:línea`
sin leer las fichas pierde pares. Acá los dos pares **sí** compartían archivo y línea, y aun
así se escaparon — porque uno de los dos informes los agrupaba en una tabla de "desvíos
menores" sin ficha propia.

### Dos quedaron cerrados por el trabajo de estos días, sin que nadie los estuviera mirando

- **`B-4`** — *"2 de 3 rechazos de enrolamiento no llegan a la bandeja de triage"*. Hoy los
  tres llegan: farmacia inexistente (ya iba), identidad de hardware distinta (§10-BT) y sin
  identidad con historia de uso (§10-BÑ, que no existía cuando se auditó). Los otros dos
  rechazos que hay ahora —freno de emergencia y rechazo humano— **no van a propósito**: el
  primero es esperado y masivo, y el segundo ya se ve en el panel porque alguien acaba de
  hacerlo.
- **`HALLAZGO-19`** ≡ `INV-02`, cerrado en §10-BS.

### Las cuatro afirmaciones falsas, corregidas

Los cuatro hallazgos de "documentación desactualizada" se cierran en esta misma pasada,
porque son el tipo de defecto que este repo trata como de primera clase: **documentación que
afirma lo que uno querría que el código hiciera**.

| | Qué decía | Qué hace |
|---|---|---|
| `B-5`≡`DOC-01` | La credencial MQTT la entrega la aprobación humana | La entrega el enrolamiento, antes y con independencia de la aprobación |
| `DOC-03` | El servidor central **no** alcanza las IP de las farmacias ("100% de pérdida de ping") | Desmentido el 11-sep-2026: responden 19 de 25 gateways |
| `B-6`≡`DOC-02` | Remite a `emqx_admin.reglas_acl_estacion` | Esa función no existe; es `_reglas_para` |
| `F-1` | "27 módulos de vistas", "services.py 1.466 ln" | 26 módulos; 1.629 líneas (medido hoy, no copiado) |

**`B-5` es el que más importa de los cuatro, y no es el más grave: es el más instructivo.** El
documento describía el flujo que uno espera —un humano aprueba y *entonces* se entrega la
credencial— y el código nunca hizo eso. Mientras la documentación dijera lo correcto, `B-1`
era invisible: nadie va a buscar un agujero donde el mapa dice que no hay puerta.

### Lo que queda, agrupado por cómo se arregla

De los 79 MEDIO/BAJO únicos que quedan, el orden que propongo no es por severidad —son todos
MEDIO o BAJO— sino por **cuántos cierra un mismo cambio**:

> **`D-5` está CORREGIDO** (§10-BV y §10-BW). Sus cinco transiciones viven en servicios: las
> dos de estaciones, las tres de despliegues y la de aperturas. De paso quedó **un escritor
> menos** del estado `PAUSADO` —`evaluar_freno_automatico` delega la escritura y conserva su
> decisión— y `apps/panel/views/estaciones.py` bajó a 485 líneas al dividirse.
>
> Un cambio de comportamiento observable: **`despliegue_pausar` ya no falla en silencio**.
> Pendientes anotados: `despliegue_reanudar` sí sigue redirigiendo sin avisar si el despliegue
> no está pausado, y dos cancelaciones simultáneas de una apertura registrarían dos eventos
> (no hay `select_for_update`; el estado y los tokens quedan correctos).

1. **Duplicación de transiciones de dominio** (`D-1`, `D-2`, `D-5`, `D-3`): el bloque de
   publicación MQTT escrito 5 veces, la transición "alerta resuelta" otras 5 —una dentro de
   una vista—, aprobar/rechazar estación como lógica en la vista, y 14 manejadores MQTT
   repitiendo el preámbulo de autenticación. Son cuatro hallazgos y **un solo refactor**, y
   `B-1` nació exactamente de este patrón: lógica de dominio en una vista que nadie revisa.
2. **Paginación inestable** (`BD-01`, `BD-02`): cinco listados ordenan por fecha no única sin
   desempate, y la vista de auditoría **anula** con su `order_by` el desempate que el modelo
   había agregado. Barato y con síntoma real: filas repetidas o perdidas al paginar.
3. **Integridad de datos observados** (`BD-07`≡`INV-08`, `BD-18`, `INV-13`, `INV-10`,
   `INV-14`): MAC sin normalizar que rompe el cruce declarado×observado, el centinela
   `0.0.0.0` guardado como medición, `ip_sondeada` que puede hablar de otro destino. Todos en
   la misma cadena que `INV-02` y `INV-07`.
4. **Herencia de tenant que falta en SNMP** (`INV-11`, `E-2`): el admin de SNMP sin scoping y
   `PerfilSnmp.unidad_negocio` declarado y nunca aplicado. Es el mismo defecto que `INV-05`,
   que ya se cerró, en el módulo nuevo.
5. **Concurrencia en la ruta caliente** (`BD-04`, `INV-18`, `RIESGO-12`): el latido guarda la
   fila completa de `Estacion` y pisa ediciones del panel. Los tres apuntan al mismo
   `save()`; `R-7` del informe del tablero lo declaró fuera de alcance en su momento.
6. **Rendimiento medible** (`BD-12`…`BD-16`, `E-4`): N+1 en dos listados, upsert en lote para
   ARP, el `Cast` innecesario en seis `count()`. **`BD-11` va aparte y primero**: propone
   tocar índices y su propio informe pide medir antes.
7. **El resto** (`A-1`, `A-3`…`A-5`, `A-7`, `C-2`…`C-6`, `D-4`, `D-6`, `D-7`, `E-1`, `E-3`,
   `INV-03`, `INV-06`, `INV-09`, `INV-12`, `INV-15`…`INV-20`, `BUG-02`, `BUG-03`, `BUG-10`,
   `RIESGO-04`, `RIESGO-05`, `RIESGO-08`, `RIESGO-09`, `HALLAZGO-13`…`HALLAZGO-27`): cada uno
   es independiente. Los cuatro `BUG CONFIRMADO` restantes (`BUG-02`, `BUG-03`, `BUG-10`)
   merecen leerse antes que el resto de este grupo, porque su evidencia ya es inequívoca.

**`INV-03` quedó a medias y conviene cerrarlo con el grupo 7**: la resincronización de §10-BS
deja `EventoActivo`, pero la **vinculación inicial** sigue cambiando la farmacia sin dejar
rastro. Es el mismo hallazgo partido en dos caminos, y ahora solo uno lo cumple.

---

[← Fase 4: hallazgos](fase4-hallazgos.md) · [Fase 1: descubrimiento](fase1-descubrimiento.md)
