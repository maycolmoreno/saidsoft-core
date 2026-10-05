# Actualización del POS en la cadena

**Fecha:** 5-oct-2026
**Para:** dirección, área comercial y área administrativa.
**Qué es:** la propuesta de una iniciativa, no documentación técnica. Todo lo que afirma
sobre lo que el sistema ya hace está verificado contra el código, no supuesto.

SAIDSOFT ya despliega versiones del POS con aprobación de dos personas, verificación de
integridad y freno automático ante fallas. El proyecto no construye ese mecanismo: lo
extiende a toda la cadena —área comercial y área administrativa— y cierra los huecos de
cobertura que hoy obligan a instalar a mano.

---

## 1. Alcance

El sistema ya distingue los tres tipos de sitio que la cadena tiene cargados: farmacia,
tienda y sitio administrativo. El proyecto cubre los tres con el mismo mecanismo, no con
dos soluciones distintas.

| Entra | Área | Qué significa |
| --- | --- | --- |
| Versiones del POS | Comercial | Instalar y actualizar el POS en las estaciones de farmacias y tiendas |
| Versiones del POS | Administrativa | La MISMA versión que el área comercial: la regla es una sola versión en toda la cadena |
| Aplicaciones del catálogo | Ambas | Software que no es el POS y ya se vigila por versión |
| Instalación inicial | Ambas | Una estación nueva queda en la versión vigente sin visita |

No entra: cambios al POS en sí, migraciones de base de datos del POS, ni equipos sin
agente instalado. Un equipo sin agente no se puede alcanzar de forma remota, y ponerle
agente es requisito del proyecto, no parte de su alcance.

La diferencia entre las dos áreas no es técnica sino de **cuándo se puede tocar el
equipo**. Una caja de farmacia no se interrumpe en horario de venta; una PC de oficina sí.
Eso ya está previsto: el despliegue tiene tres modos de aplicación —inmediato, por ventana
y al cierre del POS— y se eligen por destino.

---

## 2. Lo construido y lo que falta

El mecanismo de despliegue está construido y en uso. Lo que falta es cobertura y
operación, no plataforma —y eso cambia el tamaño del proyecto.

| Capacidad | Estado | Detalle |
| --- | --- | --- |
| Publicar una versión a un destino | Construido | Por cadena, por canal, por farmacia o por estación puntual |
| Aprobación de dos personas | Construido | Quien crea el despliegue no puede aprobarlo |
| Verificación de integridad | Construido | SHA-256 del paquete, verificado por el agente antes de aplicar |
| Momento de aplicación | Construido | Inmediato, por ventana horaria, o al cerrar el POS |
| Despliegue por anillos | Construido | Un despliegue completado se promueve a un destino más amplio conservando versión y hash |
| Freno automático ante fallas | Construido | Pausa solo sobre las estaciones que ya reportaron, no sobre las que aún no descargaron |
| Reversión | Construido | El agente reporta `rollback` como un paso propio de la línea de tiempo |
| Rastro por estación | Construido | Diez pasos registrados, de `publicado` a `ok`, `error` o `rollback` |
| **Agente en toda la cadena** | **Falta** | Sin agente no hay despliegue remoto. Es el requisito que condiciona todo lo demás |
| **Inventario de versión vigente por sitio** | **Parcial** | Se sabe qué versión corre cada estación; falta declarar la versión única de la cadena contra la cual compararla |
| **Ventanas horarias por área** | **Falta definir** | El mecanismo existe; quién fija el horario de cada área, no |
| **Quién aprueba** | **Falta definir** | El sistema exige dos personas; qué roles las ocupan es decisión de las áreas |

Las cuatro filas de abajo son el proyecto. Las ocho de arriba son el punto de partida: ya
existen, ya tienen pruebas y ya se usaron en producción.

---

## 3. Cómo viaja una versión

Ninguna versión se aplica sin dos aprobaciones y un hash verificado. Arriba, lo que hacen
las personas; abajo, lo que hace el agente en la estación.

```
PERSONAS
  Se crea el despliegue
        ↓
  Aprueba un segundo          ← nunca quien lo creó
        ↓
  Se publica al destino elegido
        ↓
AGENTE EN LA ESTACIÓN
  El agente descarga
        ↓
  ¿Hash correcto? ──── no ──→ Error o rollback  (frena el despliegue;
        │                                         no se aplica nada)
        sí
        ↓
  Cierra el POS y aplica      ← en su ventana, y lo relanza
        ↓
  Reporta OK
```

Cada estación deja rastro de los diez pasos, así que una falla se ubica en el punto exacto
donde ocurrió: no se descargó, no coincidió el hash, no cerró el POS. Si el hash no
coincide, el agente no aplica nada.

---

## 4. Fases y puertas

Cada fase tiene una puerta: ninguna avanza sin cerrar la anterior. Sin fechas todavía —las
ponen las áreas al definir ventanas y responsables.

| # | Fase | Qué se hace | Puerta de salida |
| --- | --- | --- | --- |
| 1 | Cobertura de agente | Instalar el agente donde falta, en las dos áreas. Sin agente no hay despliegue remoto | Toda estación aprobada reporta |
| 2 | Línea base de versiones | Declarar la versión única de la cadena y medir cuántas estaciones están fuera de ella | La versión única está declarada y cada área firmó su ventana |
| 3 | Piloto por anillos | Un canal chico primero; se promueve al siguiente solo si el anterior cerró sin frenos | Un anillo cierra completo sin freno automático |
| 4 | Cadena completa y operación | El resto de la cadena, y cada versión nueva siguiendo el mismo camino como rutina | — |

Las puertas no son hitos de calendario sino condiciones medibles: si no se cumplen, la
fase siguiente no arranca. La fase 3 es la que protege a la cadena —un anillo que cierra
limpio es la evidencia de que la versión sirve, y el freno automático impide que un anillo
malo siga avanzando.

---

## 5. Riesgos y controles

Los cuatro primeros ya tienen control construido. Los dos últimos no, y son los que hay
que decidir.

| Riesgo | Control | ¿Existe hoy? |
| --- | --- | --- |
| Una versión mala llega a toda la cadena | Anillos: se promueve solo si el anterior cerró limpio | Sí |
| Muchas estaciones fallan y nadie frena | Freno automático por porcentaje de error | Sí |
| El paquete llega corrupto o alterado | SHA-256 verificado antes de aplicar; si no coincide, no se aplica nada | Sí |
| Alguien publica sin control | Aprobación de una segunda persona, con registro de quién | Sí |
| Se interrumpe una venta | Aplicación por ventana u orden de cierre del POS | El mecanismo sí; el horario de cada área falta definirlo |
| Una estación queda atrás sin que nadie lo note | Comparar versión vigente contra la versión única de la cadena | No todavía: falta declarar cuál es esa versión (fase 2) |

El riesgo que no tiene control técnico es el último, y no se arregla con código: hasta que
cada área no declare qué versión debe correr, el sistema puede decir qué hay instalado
pero no si está bien.

---

## 6. Indicadores

Cuatro números, y los cuatro salen de datos que el sistema ya registra. Ninguno necesita
que alguien lleve una planilla aparte.

| Indicador | De dónde sale | Para qué sirve |
| --- | --- | --- |
| Cobertura de agente | Estaciones aprobadas que reportan, sobre el total | Dice si la fase 1 cerró. Es el que condiciona todo lo demás |
| Estaciones en la versión única | Versión vigente de cada estación contra la declarada para la cadena | Es la meta del proyecto. Un solo número para toda la cadena |
| Despliegues que terminan sin freno | Despliegues completados sobre publicados | Dice si las versiones llegan sanas o si el problema es el paquete |
| Tiempo hasta quedar al día | Entre la publicación y el último `ok` de ese destino | Dice cuánto tarda la cadena en absorber una versión |

El tercero es el que conviene mirar primero cuando algo sale mal: si los despliegues se
frenan seguido, el problema no es la logística del proyecto sino la calidad de lo que se
está publicando.

---

## 7. Decisiones pendientes

Ninguna es técnica. El mecanismo ya espera estas respuestas y no puede inventarlas.

- [ ] **Ventana horaria de cada área.** En qué franja se puede tocar una caja de farmacia y
  en cuál una PC administrativa. El sistema aplica por ventana o al cerrar el POS; alguien
  tiene que decir cuál y a qué hora.
- [ ] **Quién aprueba.** El sistema exige dos personas y no deja que la misma cree y
  apruebe. Falta decir qué rol ocupa cada lugar en cada área.
- [x] **Versión objetivo por área — DECIDIDO (5-oct-2026).** Una sola versión del POS para
  toda la cadena, comercial y administrativa. No hay versión distinta por área. En
  consecuencia el indicador principal es **uno solo**, y cualquier estación fuera de esa
  versión es un desvío, no una variante aceptada.
- [ ] **Orden de los anillos.** Qué canal va primero en el piloto. Conviene uno con
  operación real pero bajo impacto si hay que revertir.
- [ ] **Qué hacer con equipos sin agente.** Si se instala en todos, o si queda un grupo que
  se atiende a mano —y entonces ese grupo queda fuera de los indicadores y hay que decirlo
  desde el principio.

Las dos primeras bloquean la fase 1. Las otras tres se pueden definir mientras esa fase
avanza.

---

## Dónde está cada cosa en el código

Para quien tenga que verificar lo que este documento afirma.

| Afirmación | Dónde comprobarla |
| --- | --- |
| Estados del despliegue y pasos por estación | `apps/despliegues/models.py` — `Despliegue.Estado`, `EventoDespliegue.Paso` |
| Aprobación de dos personas | `apps/panel/views/despliegues.py` — `despliegue_aprobar`, que rechaza explícitamente a quien lo creó |
| Freno automático | `apps/despliegues/services.py` — `evaluar_freno_automatico` |
| Promoción por anillos | `apps/panel/views/despliegues.py` — `despliegue_promover` |
| Modos de aplicación y destinos | `apps/despliegues/models.py` — `modo_aplicacion`, `destino_tipo` |
| Tipos de sitio | `apps/catalogo/models.py` — `Farmacia.Tipo` |
| Ingesta del reporte de cada estación | `apps/despliegues/services.py` — `registrar_estado_de_estacion` |
