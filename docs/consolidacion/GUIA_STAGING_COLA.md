# Guía de validación en staging — cola de órdenes al agente (fase 1)

Rama: `fase1/cola-ordenes`. Diseño: `apps/catalogo/ordenes.py` y `PROPUESTA_AGENTE_HTTPS.md` §5.

Esta guía es el procedimiento para validar la cola **con un agente real y EMQX con TLS** antes
de autorizar el despliegue. Nada de lo que sigue se corrió todavía con un agente real ni con
TLS. Las pruebas automáticas cubren el broker real **sin TLS** (`tests_integracion_emqx.py`,
laboratorio en loopback) y simulan el agente. Cada paso indica qué registrar. Un paso que no se
pueda ejecutar se anota como **no probado**; no se da por bueno.

## 0. Qué significa cada estado

| Estado | Qué afirma | Qué NO afirma |
|---|---|---|
| `pendiente` | Autorizada; espera que se libere la ranura de la estación. | Que salió. |
| `publicando` | La ranura quedó reservada en la base y se intentó publicar. | Que EMQX la tenga. Puede estar o no. |
| `entregada` | EMQX la **aceptó** (PUBACK de MQTT 5 con código de éxito). | Que la estación la recibió. |
| `recibida` / `en_ejecucion` | El agente reportó `recibido` / un paso intermedio. | Que terminó. |
| `completada` / `fallida` | El agente reportó el final, o una persona lo verificó y la resolvió. | — |
| `interrumpida` / `no_entregable` | Inferencia del servidor: no se sabe si se ejecutó. Requiere revisión. | Que falló. |

Ranura: como máximo una orden `publicando` o `entregada` por estación y canal (software,
despliegue). Lo garantiza un índice único parcial de PostgreSQL.

### Respuesta del broker y qué hace la cola

Las reglas están en el encabezado de `apps/catalogo/ordenes.py`.

| Caso | Software y despliegue | Script |
|---|---|---|
| Aceptada (PUBACK de éxito) | `entregada`. Recién ahí el resultado registra el evento «publicado». | `entregada`; el resultado queda `enviado`. |
| Rechazo confirmado (ACL, credencial), si nunca pudo llegar | `cancelada` y sin reintento automático. El resultado de esa estación queda en `error` con el motivo «EMQX rechazó». Si se rechazan **todas**, `exitoso=False` y nada queda en curso. | `cancelada`; el resultado queda `error` («No se envió»). |
| Rechazo de un reintento cuando el anterior quedó incierto | `no_entregable`, requiere revisión. El resultado no cambia. | — |
| Sin conexión al publicar (pedido de una persona) | `exitoso=False`; todo queda `cancelado`. | `cancelada`; `error`. |
| Sin conexión en el despacho diferido | Vuelve a `pendiente` sin contar el intento. | — |
| Sin PUBACK (timeout o corte) | Sigue `publicando`. **No** se registra «publicado». Se republica el **mismo** mensaje tras 2 min, hasta 3 veces; después, `no_entregable` y se retira. | `publicando`, marcada; **nunca** se reenvía. El resultado queda `pendiente`, y el barrido de scripts lo cierra como `timeout` («nunca supimos nada»), no como `error`. |

En el panel, una publicación con algo sin confirmar o rechazado se muestra como
**advertencia**, con los conteos, nunca como «publicada». La auditoría
`*.publicar` guarda `aceptadas`, `sin_confirmar`, `rechazadas`, `en_espera` y `omitidas`.

## 1. Preparación del entorno de staging

1. Servidor de staging con la rama desplegada (`git checkout fase1/cola-ordenes`). **No es el
   NUC de producción.**
2. Base de staging restaurada desde un respaldo reciente, o vacía con `seed`. Aplicar
   migraciones: `python manage.py migrate` (aplica `catalogo.0039_orden_agente`).
3. EMQX 5.8.3 con la misma configuración que producción: `deploy/docker-compose.yml`, y
   `deploy/bootstrap-emqx.sh` con credenciales **de staging**, no las de producción.
4. Listener TLS 8883 con el certificado de staging. En el `.env` de staging:
   `MQTT_USE_TLS=True`, `MQTT_PORT=8883`, `MQTT_CA_CERT=<ruta al CA>`.
5. `celery_worker`, `celery_beat` y `mqtt_worker` corriendo. La tarea
   `procesar-ordenes-agente` corre cada 60 s.
6. Una PC de prueba (no una caja de farmacia) con el agente actual (`agente-prueba-0.36`),
   enrolada y aprobada en staging. En esta guía su código es `STG-A`.
7. Un usuario de staging con el permiso **"Puede resolver a mano órdenes inciertas al agente"**
   (`catalogo.resolver_ordenagente`), con acceso a la unidad de `STG-A`. Otro usuario sin ese
   permiso, para el paso 7.

Para observar, en todos los pasos:

- Admin → Catálogo → Órdenes al agente. Filtrar por estación, estado y "requiere revisión";
  el historial está en el detalle.
- Log del agente en la PC de prueba.
- `docker compose logs -f celery_worker mqtt_worker`.
- EMQX dashboard → Retained messages, tópico `/saidsof/agente/STG-A/#`.

## 2. Agente real con una orden de software

1. Con `STG-A` conectada, publicar desde el panel una instalación de un MSI pequeño solo a
   `STG-A`.
2. Esperado en orden:
   - el panel muestra "publicada";
   - la orden pasa `pendiente → publicando → entregada`, y el historial dice "aceptada por el
     broker";
   - el agente reporta `recibido`, y la orden queda `recibida`;
   - después pasa por `en_ejecucion` (descargado, hash_verificado, instalando) y termina en
     `completada` con `instalado`;
   - `ResultadoInstalacion` queda en `instalado`;
   - el retenido de `/saidsof/agente/STG-A/software/` se retira.
3. Registrar los tiempos entre estados y una captura del historial.

## 3. Agente real con un despliegue del POS

1. En la PC de prueba, el POS en una versión anterior. Crear un despliegue en modo
   `inmediato` solo a `STG-A`, aprobarlo (con otro usuario) y publicarlo.
2. Esperado:
   - `entregada → recibida → en_ejecucion` (pos_cerrado, aplicado, pos_relanzado);
   - termina en `completada` con `ok`;
   - `ResultadoDespliegue` queda `aplicado`.
3. Repetir con un paquete de hash incorrecto. Esperado: `fallida` (error o rollback), y el POS
   de la PC sigue funcionando.
4. Opcional: el modo `cierre_pos`. La orden debe quedar `recibida` sin pasar a `interrumpida`
   antes de 72 h.

## 4. Publicación aceptada y rechazo real de la ACL

1. **Aceptada:** ya la cubren los pasos 2 y 3. Confirmar que ninguna orden pasó a `entregada`
   sin la entrada "aceptada por el broker".
2. **Rechazo de la ACL.** No tocar las ACL de producción. En el EMQX **de staging**, crear un
   usuario `stg_sin_permiso` sin reglas (con `no_match = deny`, todo publish se rechaza).
   Arrancar un shell con esa credencial:
   ```sh
   MQTT_USERNAME=stg_sin_permiso MQTT_PASSWORD=<clave de staging> python manage.py shell
   ```
   Dentro, publicar una solicitud a `STG-A` con `apps.software.services.publicar_solicitud`.
3. Esperado:
   - `exitoso=False`;
   - la orden queda `cancelada` y su historial no tiene `entregada`;
   - no hay evento «publicado»;
   - el log dice "rechazado(s) por EMQX";
   - la PC no recibe nada (log del agente);
   - en Retained messages no aparece el mensaje.
4. Con la credencial correcta, volver a publicar la misma solicitud. Debe salir normalmente.
5. **Rechazo parcial.** Con un despliegue a dos PCs, donde una tiene la ACL de su tópico de
   despliegue mal a propósito (solo en staging). Esperado:
   - en el panel, una advertencia con «1 confirmada, 1 RECHAZADA»;
   - la orden rechazada queda `cancelada` y su resultado en `error` con el motivo;
   - el freno automático puede pausar el despliegue (cuenta como error);
   - corregida la ACL, «Reanudar» crea una orden nueva solo para esa PC.

## 5. TLS

1. Repetir el paso 2 con `MQTT_USE_TLS=True` (puerto 8883) en el servidor y el agente
   conectado por TLS.
2. Verificar:
   - en el EMQX dashboard, el cliente del panel (`CLIENT_ID_PANEL-xxxxxxxx`) entra por el
     listener `ssl:default`;
   - la orden llega a `entregada` (PUBACK sobre TLS);
   - con un `MQTT_CA_CERT` incorrecto, la publicación da `exitoso=False` y la orden queda
     `cancelada` (falla el handshake y nada se publicó).

## 6. Recepción al reconectar

1. Desconectar la red de la PC de prueba (o detener el servicio del agente).
2. Publicar una instalación. Esperado: `entregada` (EMQX la aceptó y la retiene).
3. Publicar una **segunda** instalación a `STG-A`. Esperado: queda `pendiente`, porque la
   ranura está ocupada; no pisa a la primera.
4. Reconectar la PC. Esperado:
   - el agente recibe la primera (retenida);
   - al reportar `recibido`, la segunda pasa a `entregada` en segundos (despacho al liberar la
     ranura) y también llega;
   - ambas terminan `completada`.

## 7. Orden incierta, resolución manual y reintento

1. **Interrumpida.** Iniciar un despliegue en la PC y, durante `pos_cerrado`, detener el
   servicio del agente sin apagar la PC (los latidos siguen si el servicio de latido es otro;
   si no, mantenerla viva con el agente reiniciado). Para acelerar en staging se puede bajar
   `PLAZO_SIN_AVANCE` en el shell, o esperar 6 h. Esperado: la orden pasa a `interrumpida`
   con "requiere revisión".
2. **Reintento bloqueado.** "Reanudar/reintentar" el despliegue. Esperado: `STG-A` aparece
   omitida y no se crea otra orden.
3. **Resolución:**
   - con el usuario **sin** permiso: Admin → la orden → `…/resolver/` debe dar 403;
   - con el usuario con permiso: abrir "Resolver…" y probar las tres decisiones en órdenes
     distintas:
     - **completada**: la orden y el resultado pasan a éxito (el despliegue queda
       `aplicado`), con la nota como detalle. No cambia la versión del POS de la estación;
     - **fallida**: la orden queda `fallida` y el resultado en `error`. El reintento queda
       habilitado;
     - **no se pudo determinar**: la orden sigue incierta y marcada, el resultado **no**
       cambia y el reintento sigue bloqueado. Solo queda registrada la revisión;
   - comprobar que una nota vacía o de menos de 10 caracteres no resuelve;
   - si la orden todavía puede estar retenida en el broker, la resolución la retira
     primero. Con EMQX detenido, la resolución debe negarse.
4. Auditoría: el evento `orden_agente.resolver` registra usuario, fecha, decisión, nota,
   estado anterior e IP.
5. **Reintento.** Si se resolvió como `fallida`, el reintento ahora sí crea una orden nueva
   para `STG-A`.
6. **Script incierto.** Ejecutar un script con la PC desconectada. Esperado: la orden queda
   `entregada` y luego `no_entregable` con revisión, y **nunca se reenvía**. Resolverla a mano
   después de mirar la PC.

## 8. Recuperación tras reiniciar el broker

1. Con la PC desconectada, publicar una instalación (`entregada`).
2. Reiniciar EMQX de staging: `docker compose restart emqx`.
3. Reconectar la PC. Esperado: la orden retenida sobrevive (retainer en disco) y llega.
4. **Corte en medio de una publicación.** Durante una publicación a muchas estaciones, o con
   `docker pause` del contenedor EMQX justo antes de publicar, la orden debe quedar
   `publicando`, nunca `entregada` sin PUBACK. Después de `docker unpause`, en la pasada
   periódica siguiente (más de 2 min después) la orden se republica con **el mismo payload**
   y pasa a `entregada`. El agente, si ya la tenía, la descarta (`_ya_procesado`). Verificar en
   el log del agente que no la ejecutó dos veces.
5. **Reinicio del servidor** (celery y web) en medio de una pasada: no deben quedar órdenes
   colgadas. Las `publicando` se recuperan, y el cerrojo de la pasada se libera solo al morir
   la conexión.

## 9. Reversión (si staging falla)

Ensayada en la base de pruebas con `ENSAYO_REVERSION=1 python manage.py test
apps.catalogo.tests_reversion_0039`. En staging:

1. Respaldo previo:
   ```sh
   pg_dump -t orden_agente
   python manage.py dumpdata catalogo.OrdenAgente > orden_agente.json
   ```
2. Volver el código a la rama anterior **antes** o junto con la migración. El código viejo no
   usa la tabla; el código nuevo sin la tabla falla.
3. `python manage.py migrate catalogo 0038_estacion_mac_lan`. Borra solo `orden_agente`, con
   sus índices y restricciones. No toca resultados, estaciones ni eventos.
4. Verificar:
   - siguen iguales los conteos y estados de `ResultadoInstalacion`, `ResultadoDespliegue`,
     `ResultadoEjecucionScript` y `Estacion`;
   - en `showmigrations catalogo`, `0039` aparece sin aplicar.
5. Quedan, inofensivos: el permiso `resolver_ordenagente` y el content type de `ordenagente`
   (Django no los borra) y los eventos de auditoría `orden_agente.*` (texto, sin FK).
6. Los retenidos que la cola publicó siguen en EMQX. El código anterior los maneja como
   siempre (`retirar_retenido_*` al recibir el reporte).

## 10. Criterios para autorizar el despliegue

- Pasos 2, 3, 4, 5, 6, 7 y 8 ejecutados con el resultado esperado, o el desvío documentado y
  aceptado.
- Ninguna orden `entregada` sin "aceptada por el broker" en el historial.
- Ninguna ejecución duplicada en el log del agente.
- Ningún script reenviado automáticamente.
