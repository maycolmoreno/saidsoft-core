# Checklist del piloto en producción — cola de órdenes al agente

**Alcance autorizado:** desplegar la versión candidata en el NUC de producción y probarla
con **una sola PC de prueba**, que no sea una caja de farmacia. **No están autorizadas**
campañas de software ni despliegues del POS a muchas farmacias: eso necesita otra
autorización, con el resultado de este piloto a la vista.

**Por qué un piloto y no staging:** hoy no hay un entorno de staging con agente real y
EMQX con TLS. Si se arma uno, se usa primero `GUIA_STAGING_COLA.md`. Este checklist es la
alternativa limitada.

Marcar cada casilla al completarla. Un paso que no se pueda hacer se marca **NO PROBADO**,
con el motivo; no se da por bueno.

Convención de comandos: «PC» es tu computadora (Windows, Git Bash o PowerShell); «NUC» es el
servidor (`glpi@10.111.6.20`), desde la carpeta `~/Documentos/Said/saidsoft-core/deploy`.
Las consultas a la base se hacen con:

```sh
docker compose --env-file .env exec -T web python manage.py shell -c "<código>"
```

## 0. Versión candidata

Rama `fase1/cola-ordenes`, sobre `master` = `c89310b`, con exactamente dos commits:

| Commit | Contenido |
|---|---|
| `8c42efc` | Cola de órdenes: transporte MQTT 5, estado `publicando`, resolución manual, pruebas, guía de staging |
| (el siguiente) | Corrección del procedimiento de reversión y este checklist |

- [ ] En la PC: `git log --oneline c89310b..fase1/cola-ordenes` muestra **solo** esos dos commits.
- [ ] En la PC: `git status` no muestra nada preparado. Lo que quedó fuera (`.claude/`,
      `docs/auditoria/`, los dos documentos de consolidación y `PROPUESTA_AGENTE_HTTPS.md`)
      **no** viaja: no está en esos commits.
- [ ] Se hereda de la validación local: 2447 pruebas OK (13 dependen de variables de entorno
      y se corrieron aparte) e integración con EMQX real de laboratorio, **sin TLS**. El
      agente real y TLS se prueban recién en este piloto.

## 1. Requisitos antes del día del piloto

- [ ] **PC de prueba** enrolada y **aprobada** en producción, con el agente
      `agente-prueba-0.36`, encendida y con latido reciente. Anotar su código: `__________`.
      No puede ser una caja que atienda público.
- [ ] Un **MSI chico** en el catálogo de software (por ejemplo 7-Zip), para instalar en esa PC.
- [ ] (Opcional) Si la PC de prueba tiene el POS: un despliegue de prueba **aprobado** solo
      para ella. Si no lo tiene, el paso 6.3 queda NO PROBADO.
- [ ] Un usuario con el permiso «Puede resolver a mano órdenes inciertas al agente»
      (`catalogo.resolver_ordenagente`). Se asigna en el admin, después del despliegue
      (el permiso lo crea la migración).
- [ ] Horario acordado de poco movimiento, con 2 horas libres para probar y, si hace falta,
      volver atrás.

## 2. Antes de desplegar (NUC)

- [ ] **Sin publicaciones en curso.** Anotar los dos números:
  ```sh
  docker compose --env-file .env exec -T web python manage.py shell -c "from apps.software.models import SolicitudInstalacion as S; from apps.despliegues.models import Despliegue as D; print('solicitudes publicando:', S.objects.filter(estado='publicando').count()); print('despliegues publicando/pausados:', D.objects.filter(estado__in=['publicando','pausado']).count())"
  ```
  No bloquean (lo publicado antes sigue funcionando sin cola), pero mejor no lanzar nada
  nuevo durante el piloto.
- [ ] **Respaldo nuevo**, con el mismo servicio del respaldo nocturno:
  ```sh
  sudo systemctl start saidsoft-respaldo.service && journalctl -u saidsoft-respaldo -n 40 --no-pager
  ls -lht /home/glpi/backups/saidsoft/ | head -5
  ```
  Tienen que aparecer `db_`, `media_` y `emqx_` con la hora de recién y tamaño razonable
  (el `db_` parecido al de ayer).
- [ ] **Respaldo verificado**: se restaura en la base de PRUEBA `restore_test`, nunca en la real.
  ```sh
  cd ~/Documentos/Said/saidsoft-core
  sh deploy/restaurar-backup.sh /home/glpi/backups/saidsoft/db_<FECHA>.sql.gz.gpg
  ```
  Tiene que terminar sin error. Anotar el archivo verificado: `__________`.
- [ ] Anotar el commit actual del NUC: `git log --oneline -1` (debería ser `c89310b`).

## 3. Desplegar

- [ ] En la PC, llevar la rama a `master` (avance directo, sin merge) y subirla:
  ```sh
  git checkout master
  git merge --ff-only fase1/cola-ordenes
  git push origin master
  ```
  Si `--ff-only` falla, **parar**: `master` cambió y hay que revisar antes de seguir.
- [ ] En el NUC:
  ```sh
  cd ~/Documentos/Said/saidsoft-core && sh deploy/desplegar.sh
  ```
  Esperado:
  - en el paso 4/4, `Applying catalogo.0039_orden_agente... OK`;
  - la verificación termina en «Todo aplicado» y muestra el commit nuevo.

## 4. Verificación inmediata (NUC)

- [ ] La migración está aplicada:
  ```sh
  docker compose --env-file .env exec -T web python manage.py showmigrations catalogo | tail -2
  ```
  Esperado: `[X] 0039_orden_agente`.
- [ ] La tabla existe y está vacía:
  ```sh
  docker compose --env-file .env exec -T web python manage.py shell -c "from apps.catalogo.models import OrdenAgente; print(OrdenAgente.objects.count())"
  ```
- [ ] La tarea periódica corre cada minuto (esperar 2 minutos):
  ```sh
  docker compose --env-file .env logs --since 3m celery_beat | grep procesar-ordenes-agente
  docker compose --env-file .env logs --since 3m celery_worker | grep procesar_ordenes_task
  ```
  Esperado: «Sending due task procesar-ordenes-agente» y la tarea con `succeeded`, sin
  `error_transitorio` ni tracebacks.
- [ ] El panel y el admin abren normalmente. En Admin → Catálogo → «Órdenes al agente»
      aparece la lista vacía.
- [ ] Asignar el permiso de resolver al usuario elegido (Admin → Usuarios → permisos).

## 5. Qué mirar durante el piloto

En otra terminal del NUC:

```sh
docker compose --env-file .env logs -f celery_worker worker web 2>&1 | grep -iE "orden|EMQX|rechaz|SIN CONFIRMAR|Traceback"
```

Y el dashboard de EMQX (por el túnel SSH de siempre al `127.0.0.1:8082`): Retained
messages, filtrando por el código de la PC de prueba.

## 6. Pruebas con la PC de prueba

### 6.1 Instalación de software

- [ ] Crear una solicitud con el MSI chico, destino: **solo** la PC de prueba. Publicarla
      desde el panel.
- [ ] El panel muestra un mensaje **verde** «Solicitud en curso…: 1 confirmada(s) por el
      broker». Si sale **amarillo** («sin confirmar» o «RECHAZADA»), parar y revisar
      (ver §7).
- [ ] En Admin → Órdenes al agente, la orden pasa por `publicando → entregada`, y el
      historial dice «aceptada por el broker».
- [ ] El agente la recibe (log de la PC); la orden avanza a `recibida → en_ejecucion →
      completada`.
- [ ] El resultado de la solicitud queda «Instalado», con **un solo** evento «Publicado».
- [ ] En EMQX, el retenido del tópico de software de la PC se retira después del acuse.

### 6.2 Recepción al reconectar

- [ ] Desconectar la red de la PC de prueba (o detener el servicio del agente).
- [ ] Publicar otra solicitud chica solo a esa PC. La orden queda `entregada` (EMQX la
      retiene).
- [ ] Publicar una **segunda** solicitud. Esa queda `pendiente`, porque la ranura está ocupada.
- [ ] Reconectar la PC. La primera llega y se ejecuta; al acusarla, la segunda pasa a
      `entregada` en segundos y también llega. Las dos terminan `completada`.
- [ ] En el log del agente, cada instalación se ejecutó **una sola vez**.

### 6.3 Despliegue del POS (solo si la PC tiene POS)

- [ ] Publicar el despliegue de prueba aprobado, solo a la PC de prueba.
- [ ] Mensaje verde; la orden `entregada → recibida → en_ejecucion → completada` y el
      resultado «Aplicado».
- [ ] El POS de la PC sigue funcionando.
- Si no hay POS: **NO PROBADO** — motivo: `__________`.

### 6.4 Script (diagnóstico)

- [ ] Ejecutar un script inocuo (por ejemplo, `Get-Date`) solo a la PC de prueba.
- [ ] El resultado pasa a «Enviado» y luego a «Completado». La orden termina `completada`.
- [ ] El script se ejecutó **una sola vez** en el log del agente.

### 6.5 Lo que NO se prueba en producción

Rechazo de la ACL, corte en medio de una publicación, órdenes interrumpidas y resolución
manual: están cubiertos en las pruebas y en el laboratorio, y provocarlos en producción
exigiría tocar la ACL o el broker real. Quedan para staging. Si en el piloto aparece una
orden incierta de verdad, se resuelve con el admin («Resolver…») y se anota.

## 7. Cuándo parar y volver atrás

Volver atrás (§8) si pasa **cualquiera** de estas cosas:

- `desplegar.sh` termina con AVISO, o la migración no se aplica;
- el panel da error 500 al publicar;
- la publicación a la PC de prueba sale **rechazada** por EMQX (credencial o ACL del panel);
- una orden pasa a `entregada` sin «aceptada por el broker» en el historial;
- el agente ejecuta dos veces la misma instalación, despliegue o script;
- `procesar_ordenes_task` falla de forma repetida en los logs de celery.

Una publicación «sin confirmar» aislada **no** es motivo para volver atrás: se espera 3
minutos y se mira si la recuperación la llevó a `entregada`. Si se repite, sí.

## 8. Volver atrás

**Primero, siempre:** anotar las órdenes que todavía no salieron. El código anterior no las
despacha, así que esas estaciones no las recibirían:

```sh
docker compose --env-file .env exec -T web python manage.py shell -c "from apps.catalogo.models import OrdenAgente as O; [print(o.estacion.codigo, o.tipo, o.origen_id, o.estado) for o in O.objects.filter(estado__in=['pendiente','publicando'])]"
```

Hay dos caminos. **El orden de los pasos importa:** una vez desplegado el código anterior,
Django ya no puede quitar la migración `0039`, porque su archivo no está.

### 8.A Rápida, sin pérdida de datos (para una emergencia)

1. En la PC, revertir los dos commits en `master` y subir:
   ```sh
   git checkout master
   git revert --no-edit <hash del commit de la corrección> 8c42efc
   git push origin master
   ```
2. En el NUC: `sh deploy/desplegar.sh`. Queda el código anterior, y la tabla `orden_agente`
   sigue en la base sin uso.
3. Limitación mientras la tabla siga ahí: **no se puede borrar** una estación ni un usuario
   que tengan órdenes (las claves foráneas lo impiden). Para quitarla después, volver a
   desplegar la versión nueva y seguir 8.B, o pedir un procedimiento de limpieza aparte.

### 8.B Completa (quita la tabla)

Con el código **nuevo** todavía desplegado:

1. Detener lo que escribe en la cola:
   ```sh
   docker compose --env-file .env stop celery_beat celery_worker worker
   ```
2. Respaldar la tabla:
   ```sh
   docker compose --env-file .env exec -T web python manage.py dumpdata catalogo.OrdenAgente > ~/orden_agente_$(date +%Y%m%d_%H%M).json
   ```
3. Revertir la migración:
   ```sh
   docker compose --env-file .env exec -T web python manage.py migrate catalogo 0038_estacion_mac_lan
   docker compose --env-file .env exec -T web python manage.py showmigrations catalogo | tail -2
   ```
   Esperado: `[ ] 0039_orden_agente`.
4. Recién ahora, revertir el código (8.A pasos 1 y 2). `desplegar.sh` vuelve a levantar
   todos los servicios.
5. Verificar que los conteos de resultados de software, despliegues y scripts no cambiaron,
   y que el panel publica normalmente.

En los dos caminos, quedan inofensivos los retenidos que la cola publicó en EMQX (el código
anterior los maneja como siempre), el permiso `resolver_ordenagente` y los eventos de
auditoría `orden_agente.*`.

## 9. Resultado del piloto

| Punto | Resultado (OK / FALLA / NO PROBADO) | Nota |
|---|---|---|
| Respaldo verificado (§2) | | |
| Despliegue y migración (§3, §4) | | |
| Tarea periódica (§4) | | |
| Software (§6.1) | | |
| Reconexión (§6.2) | | |
| Despliegue del POS (§6.3) | | |
| Script (§6.4) | | |
| Sin ejecuciones duplicadas | | |

Dejar el piloto **24 a 48 horas** con la PC de prueba encendida, mirando §5 de vez en
cuando y el filtro «requiere revisión» del admin. Con todo OK, el paso siguiente
(una campaña a pocas farmacias) se pide como una autorización nueva.
