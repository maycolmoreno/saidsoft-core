# Requerimiento: control centralizado de versiones del POS

**Fecha:** 5-oct-2026
**Solicita:** Tecnología / Soporte
**Dirigido a:** dirección, área comercial y área administrativa
**Qué se pide:** aprobación de la necesidad y tres definiciones. No se pide presupuesto ni
fecha todavía.

---

## La necesidad

**La regla es que toda la cadena corra una sola versión del POS. Hoy no hay forma de
garantizarla ni de verificarla.**

Cada versión se copia a mano, carpeta por carpeta, en unas 2.115 estaciones. Lo hacen 9
técnicos. Son 235 estaciones por técnico, por versión — y mientras ese recorrido dura, la
cadena está, por definición, corriendo dos versiones a la vez.

| Dato | Valor |
| --- | --- |
| Farmacias en la cadena | 705 |
| Estaciones por farmacia | ~3 |
| Estaciones a intervenir por versión | **~2.115** |
| Técnicos disponibles | 9 |
| Estaciones por técnico, por versión | **~235** |

Y ese número es el piso, no el techo: **toda estación o farmacia que esté apagada cuando
el técnico llega obliga a volver.** Esa segunda vuelta no agrega ninguna estación nueva al
resultado — es trabajo repetido sobre la misma.

De ahí se desprenden tres consecuencias que ya se están pagando:

1. **La regla de una sola versión no se puede sostener.** Mientras el recorrido manual
   dura, conviven la versión vieja y la nueva; y cualquier estación que quede afuera
   prolonga esa convivencia sin que nadie lo sepa.
2. **El esfuerzo crece con la cadena.** Lo que hoy ocupa a 9 técnicos por versión, a 1.300
   farmacias no es viable con el mismo equipo.
3. **Un equipo que queda atrás no se nota.** Se descubre cuando falla, no antes.

No es una proyección. En la última semana hubo dos casos que exigieron intervención manual
equipo por equipo: una estación a la que el antivirus le borró el agente y otra que quedó
incomunicada tras reinstalarle el sistema operativo. Ninguna de las dos se podía resolver
a distancia sin que alguien tocara el equipo.

## Qué pasa si no se hace

- Cada versión nueva del POS se sigue coordinando a mano, sitio por sitio.
- Las actualizaciones ocurren en horarios que decide quien las ejecuta, no el área dueña
  de la operación — con riesgo de interrumpir una venta.
- Una falla masiva por una versión mala se detecta por los reclamos, no por el sistema.
- La diferencia de versiones entre sitios se acumula en silencio y vuelve más difícil cada
  actualización siguiente.

## Lo que ya existe (y por eso esto no es un desarrollo desde cero)

El mecanismo de distribución **ya está construido, probado y usado en producción**:
publicación a un destino elegido, aprobación obligatoria de una segunda persona,
verificación de integridad del paquete antes de aplicarlo, despliegue por etapas, freno
automático si las fallas superan un umbral, y reversión.

**Y resuelve específicamente el problema del equipo apagado.** La orden de actualización
queda retenida en el servidor: una estación que estaba apagada la recibe sola al
encenderse, sin que nadie vuelva a pasar por ahí. El retrabajo que hoy duplica la
actividad desaparece, no se reduce.

Con eso, publicar una versión a las 2.115 estaciones pasa de ser una campaña de 9 técnicos
a una acción que se aprueba una vez y se ejecuta sola, estación por estación, en la ventana
horaria que cada área defina.

**Lo que falta no es tecnología: es cobertura y acuerdos de operación.** Esa es la razón
de este requerimiento.

## Qué se solicita aprobar

| # | Solicitud | A quién | Por qué es necesario |
| --- | --- | --- | --- |
| 1 | Instalar el agente de gestión en el parque que hoy no lo tiene, en ambas áreas | Dirección | Sin agente, el equipo no se puede actualizar de forma remota. Es la condición de todo lo demás |
| 2 | Designar quién aprueba una actualización en cada área | Comercial y Administrativa | El sistema exige dos personas y no permite que quien la crea la apruebe |
| 3 | Definir la ventana horaria en que se puede intervenir cada tipo de sitio | Comercial y Administrativa | Para que las actualizaciones ocurran cuando el área lo decide, no cuando se ejecutan |

Con esas tres definiciones el trabajo puede empezar. Sin ellas, no.

## Qué NO se pide ahora

- No se pide presupuesto: el mecanismo ya está desarrollado.
- No se pide una fecha de finalización: depende de la cobertura de agente, que recién se
  puede dimensionar una vez aprobado el punto 1.
- No se pide cambiar el POS ni sus procesos de negocio. Esto es cómo llega una versión al
  equipo, no qué hace esa versión.

## Beneficio esperado

**Que la regla de una sola versión en toda la cadena deje de ser una intención y pase a ser
algo medible y exigible.**

En concreto: que una versión llegue a las dos áreas de forma controlada, con aprobación, en
la ventana que cada una defina, y que en cualquier momento se pueda responder con un número
cuántas estaciones están en la versión vigente y cuáles no. Hoy esa pregunta no tiene
respuesta; con esto, tiene una sola.

---

## Anexo

El detalle de cómo se ejecutaría —fases, puertas de avance, riesgos, indicadores y
decisiones operativas— está en [proyecto-despliegue-pos.md](proyecto-despliegue-pos.md).
Ese documento es para después de aprobada la necesidad.

## Origen de las cifras

| Cifra | Origen |
| --- | --- |
| 705 farmacias, ~3 estaciones por farmacia, 9 técnicos | Datos operativos del área de Soporte |
| Mensaje retenido para equipos apagados | Verificado en el código: los despliegues se publican con `retain`, igual que las órdenes de pausa remota |
| Capacidades ya construidas | Verificadas en el código; el detalle por función está en el anexo |

**Falta una cifra que haría el caso todavía más claro:** cuántas versiones distintas del
POS conviven hoy en la cadena. Si son varias, ese solo número muestra el problema mejor
que todo este documento. Se obtiene del sistema y conviene agregarla antes de presentar.
