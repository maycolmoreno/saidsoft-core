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

**`INV-01` se reprodujo y se corrigió el mismo día** — es el primer hallazgo de la auditoría
por agentes que pasa por `Reproducción: SÍ` antes de arreglarse, y el §5 cuenta cómo.
**`RIESGO-06`, el primero del orden, está corregido en el código** (§6); se cierra del todo
cuando el agente 0.33 esté instalado en los 66 equipos. Quedan **9 ALTO abiertos**.

---

## 1. Vigencia verificada hoy

| ID | Vigente | Línea verificada hoy | Qué hace falta para reproducirlo |
|---|---|---|---|
| `RIESGO-06` | **CORREGIDO en el código**, pendiente de distribuir el agente | `monitoreo/servicios_pos.py` (`envolver_firmado`) · `agente_prueba.py` (`_catalogo_verificado`) | **Cerrado del lado del servidor** — ver §6 |
| `B-1` ≡ `RIESGO-07` | **SÍ** | `mqtt_worker/services.py:107` (`aprovisionar_credencial_estacion` dentro de `_respuesta_aceptado`) · `panel/views/estaciones.py:199-205` (`estacion_rechazar` solo escribe el campo) · `emqx_admin.py` no tiene ninguna función de baja | Prueba local con EMQX simulado; la confirmación final, contra el broker |
| `B-2` | **SÍ** | `emqx_admin.py:124` — `publish` sobre `/saidsof/enrolamiento/solicitar/`, sin código en la ruta | Broker real (publicar con la credencial de una estación por el código de otra) |
| `A-6` | **CORREGIDO** | `agente_prueba.py` (`leer_machine_guid`) | **Cerrado en el código**; viaja en el agente 0.33 con `RIESGO-06` — ver §8 |
| `A-2` | **SÍ** | `aperturas/management/commands/generar_paquete_apertura.py:106` — `hmac_secret=settings.COMANDO_HMAC_SECRET` | Prueba local del comando |
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

**Alcance:** 7 pruebas nuevas (`CatalogoFirmadoTests`), 835 de `apps.monitoreo` +
`apps.mqtt_worker` en verde. Sin migración. Del lado del agente no hay suite —es un script
aparte—, así que las pruebas reconstruyen la firma exactamente como lo hace él, el mismo
método que usa `apps/despliegues/tests.py` para los tópicos de difusión.

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

[← Fase 4: hallazgos](fase4-hallazgos.md) · [Fase 1: descubrimiento](fase1-descubrimiento.md)
