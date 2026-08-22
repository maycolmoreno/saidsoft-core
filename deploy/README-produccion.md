# Despliegue en producción

Stack en `deploy/`: panel web (Django + gunicorn), worker MQTT, TimescaleDB y EMQX,
orquestados con Docker Compose. Reemplaza el SQLite y el broker `amqtt` de desarrollo.

> **Estado**: la infraestructura está escrita y validada en lo que se puede sin levantar
> el stack completo (la config de producción de Django carga con PostgreSQL,
> `collectstatic` con WhiteNoise funciona, la WSGI app importa, el `docker-compose.yml`
> es YAML válido, la migración del hypertable es un no-op seguro fuera de TimescaleDB).
> El servicio `meshcentral` sí se probó suelto (imagen oficial, fuera de este
> `docker-compose.yml`): levanta, genera certificados, sirve la consola HTTPS y el
> endpoint `/meshagents` responde (401 con un `meshid` inventado — se comporta como se
> espera). **No se ejecutó `docker compose up` del stack completo** (db+emqx+web+worker
> juntos) — en el equipo donde se escribió esto, el puerto 5433 que usa `db` ya lo ocupa
> otro proyecto corriendo en Docker; revisar puertos libres antes de probarlo aquí mismo.
> El primer arranque completo debe hacerse en el servidor destino siguiendo estos pasos,
> y ahí conviene una prueba piloto antes del rollout.

## Requisitos del servidor

- Docker Engine + Docker Compose (v1 o v2 — ver nota más abajo sobre diferencias reales
  encontradas con v1).
- Specs sugeridas para ~1.800 equipos: 8 vCPU / 16 GB RAM / SSD (ver PLAN_MODERNIZACION.md).

El proxy TLS delante del panel (servicio `nginx`, 22-ago-2026 — ver PLAN_MODERNIZACION.md
§10-Y/SEC-2) ya viene incluido en `docker-compose.yml`, no hay que traer uno propio.
Publica el panel en **HTTPS por el puerto 8084** con el mismo certificado autofirmado que
usa EMQX (`deploy/certs/cert.pem`) — sin dominio, no hay otra opción real. El puerto 8080
sigue respondiendo pero solo redirige a HTTPS, salvo `/media/` (paquetes de despliegue e
instaladores), que se sirve sin cifrar a propósito porque no requiere autenticación y así
ningún agente de la flota necesita confiar en el certificado autofirmado solo para bajar
un archivo.

## Pasos

```bash
# 1. Variables y secretos
cp deploy/.env.prod.example deploy/.env
#    Editar deploy/.env: SECRET_KEY, ALLOWED_HOSTS, contraseñas de BD/EMQX, etc.
python -c "from django.core.management.utils import get_random_secret_key as k; print(k())"  # para SECRET_KEY

# 2. Certificado TLS — lo comparten EMQX (8883) y el proxy `nginx` delante del panel
#    (8084, ver PLAN_MODERNIZACION.md §10-Y/SEC-2). Pasarle la IP o dominio real del
#    servidor en el SAN, si no los agentes/navegadores rechazan el cert por
#    "certificate is not valid for '<ip>'":
sh deploy/certs/generar-certs-dev.sh IP:x.x.x.x   # o DNS:tu-dominio, self-signed sin dominio
#    …en producción con dominio real, reemplazar deploy/certs/{cert,key}.pem por
#    certificados de una CA real (Let's Encrypt u otra) y distribuir el CA a los agentes.

# 3. Levantar el stack (migraciones y collectstatic corren solos en el arranque del web)
docker-compose --env-file deploy/.env -f deploy/docker-compose.yml up -d --build

# 4. Crear el superusuario del panel — acceder después en https://<IP-o-dominio>:8084
#    (certificado autofirmado sin dominio: aceptar la advertencia del navegador la
#    primera vez, igual que con MeshCentral en el 8083)
docker-compose -f deploy/docker-compose.yml exec web python manage.py createsuperuser

# 5. Sembrar usuarios MQTT y definir ACLs en EMQX (siembra usuarios y reglas por
#    tópico en un solo paso; docker-compose.yml ya fija no_match=deny)
sh deploy/bootstrap-emqx.sh

# 6. Las tareas periódicas ya NO necesitan cron externo: el servicio `celery_beat`
#    (ver docker-compose.yml) las dispara solo según CELERY_BEAT_SCHEDULE en
#    config/settings/base.py — marcar estaciones offline (cada minuto), purgar métricas
#    viejas, generar ejecuciones/mantenimientos programados vencidos (diario).
#    Los comandos manuales se conservan por si hace falta correrlos a mano:
#    docker-compose -f deploy/docker-compose.yml exec web python manage.py marcar_estaciones_offline
#    docker-compose -f deploy/docker-compose.yml exec web python manage.py purgar_metricas --dias 30

# 7. Backups: sí van por cron del host (necesitan orquestar `docker compose` desde
#    afuera del stack) — agregar al crontab:
#    0 2 * * * cd /ruta/al/proyecto && sh deploy/backup.sh /ruta/a/backups >> /var/log/saidsoft-backup.log 2>&1

# 8. (Opcional) Acceso remoto — configurar MeshCentral, ver sección abajo.
```

## Actualizar un despliegue existente

```bash
cd /ruta/al/proyecto
git pull
cd deploy
sudo docker-compose down                              # NO usar -v: borra los volúmenes (db, emqx, etc.)
sudo docker-compose --env-file .env up -d --build
sudo docker-compose ps
```

`down` antes de `up` no es opcional si usás `docker-compose` v1 (ver más abajo) — evita
el bug de "recrear" contenedores. Si solo cambió código Python (no `docker-compose.yml`
ni las plantillas de `deploy/certs`/`deploy/meshcentral`), `--build` es suficiente sin
tocar `certs`/`meshcentral/config.json`, que son archivos aparte del código y sobreviven
al rebuild.

## Problemas conocidos con `docker-compose` v1 (Ubuntu trae esta versión por defecto)

`docker-compose` (el binario Python, v1.29.2, sin guion — distinto del plugin `docker
compose` v2) está sin mantenimiento y tiene un bug conocido al **recrear** contenedores
contra versiones nuevas de Docker Engine:

```
ERROR: for <servicio>  'ContainerConfig'
...
KeyError: 'ContainerConfig'
```

Pasa cada vez que corrés `up -d` y el contenedor de ese servicio **ya existe** (cambió
una imagen, una variable de entorno, o un volumen). La solución que funcionó siempre en
este despliegue: bajar el contenedor primero para que `up` solo tenga que *crearlo*, no
*recrearlo* (el código de "recrear" es el que tiene el bug):

```bash
# Para todo el stack:
sudo docker-compose down
sudo docker-compose --env-file .env up -d --build

# Para un solo servicio (más rápido si no cambió nada más):
sudo docker-compose rm -sf <servicio>
sudo docker-compose --env-file .env up -d <servicio>
```

Si un `down`/`rm` deja contenedores "huérfanos" con nombres raros (ej.
`156311d1f0d7_deploy_web_1`, un hash en vez de `deploy_web_1`) — son restos de un
intento de recreate que falló a mitad de camino, se pueden borrar con
`sudo docker rm -f <nombre>` sin miedo, son solo contenedores parados.

Otras diferencias de v1 encontradas en este despliegue: `docker-compose logs` exige las
opciones **antes** del nombre del servicio (`logs --tail=50 web`, no
`logs web --tail=50`).

### 🔴 NUNCA usar `docker compose` (v2, con espacio) en este servidor

A pesar de lo que decía antes esta sección, el plugin `docker compose` v2 **sí está
instalado** en el NUC de producción (junto al binario legado `docker-compose` v1, el
que de verdad corre el stack). Son dos herramientas independientes con **namespaces de
imagen y contenedor distintos** — mismo `docker-compose.yml`, resultados completamente
separados:

| | `docker-compose` (v1, guion) | `docker compose` (v2, espacio) |
|---|---|---|
| Contenedores | `deploy_web_1` (guion bajo) | `deploy-web-1` (guion) |
| Imágenes | `deploy_web:latest` | `deploy-web:latest` |
| Caché de build | propio | propio, no comparte nada con v1 |

**Esto causó un incidente real (11-ago-2026):** en algún momento se corrió `docker
compose build ...` (v2) pensando que actualizaba el stack en vivo. Como el stack en vivo
lo administra `docker-compose` (v1) — son los contenedores `deploy_web_1` los que tienen
el puerto 8080 publicado —, esa reconstrucción v2 generó una imagen `deploy-web` (guion)
que nadie usa, y el contenedor real (`deploy_web_1`) siguió sirviendo código viejo.
Visto desde afuera parecía "el rebuild no hace nada" (un supuesto bug de caché de
Docker); la causa real era simplemente estar mirando el stack equivocado.

**Regla fija: todos los comandos de este proyecto usan `docker-compose`, sin espacio,
con guion.** Si algún día se decide migrar a v2 de una vez (v2 no tiene el bug de
"recrear" de más arriba), hay que primero `docker-compose down` el stack v1 completo y
recién ahí levantar con v2 — nunca mezclar los dos comandos contra el mismo despliegue.
Para confirmar cuál está sirviendo tráfico ahora: `docker ps` — si los nombres tienen
guion bajo (`deploy_web_1`), es v1.

## Sin proxy TLS todavía (piloto en LAN)

`SECURE_SSL_REDIRECT`/`COOKIES_SOLO_HTTPS` (ambos `True` por defecto en
`config/settings/produccion.py`) asumen que hay un proxy TLS real delante del panel. Sin
uno (típico en un piloto dentro de la LAN, accediendo por IP), Django redirige
`http://` → `https://` hacia un puerto donde nadie sirve HTTPS, y el navegador se queda
esperando indefinidamente ("conexión caducada" o similar) — no es un problema de red ni
de firewall, aunque lo parezca. Mientras no haya proxy TLS real, poné en `deploy/.env`:

```
SECURE_SSL_REDIRECT=False
COOKIES_SOLO_HTTPS=False
```

Volver a `True` apenas haya HTTPS real delante — dejarlo en `False` permanentemente es
un riesgo real (cookies de sesión viajando en texto plano).

## Servicios

| Servicio | Imagen | Rol |
|---|---|---|
| `db` | timescale/timescaledb:pg16 | PostgreSQL + TimescaleDB (métricas como hypertable) |
| `emqx` | emqx:5.8 | Broker MQTT con TLS (8883) y auth built-in; TCP plano desactivado |
| `web` | (build local) | Panel Django con gunicorn; corre migraciones/collectstatic al arrancar |
| `worker` | (build local) | Worker MQTT (`run_mqtt_worker`) |
| `redis` | redis:7-alpine | Broker/backend de Celery (fundación async, ver config/celery.py) |
| `celery_worker` | (build local) | Ejecuta las tareas async (hoy: las 4 tareas periódicas; a futuro: PDF, notificaciones, sync Odoo) |
| `celery_beat` | (build local) | Dispara las tareas periódicas según CELERY_BEAT_SCHEDULE — reemplaza el cron externo |
| `meshcentral` | ghcr.io/ylianst/meshcentral | Acceso remoto interactivo (opcional, ver abajo) |

## TimescaleDB — nota sobre el hypertable (intentado, revertido, ver detalle 31-jul-2026)

La migración `monitoreo.0002` intenta convertir `muestra_metrica` en hypertable, pero
TimescaleDB rechaza hacerlo si un índice único excluye la columna de tiempo — y el PK `id`
que Django agrega por defecto lo hace. Por eso la migración **captura ese error y sigue**:
la tabla queda como PostgreSQL normal (funcional, solo sin las ventajas de particionado).

Se intentó cerrar esto con una PK compuesta `(timestamp, id)` (Django 5.2
`CompositePrimaryKey`) y se encontraron tres problemas reales, verificados corriendo la
migración de verdad contra la base de desarrollo (con datos existentes):

1. Django 5.2 no soporta `CompositePrimaryKey` combinada con `AutoField`/`BigAutoField`
   (`AutoFields must set primary_key=True`, sin forma documentada de evitarlo) — obliga
   a cambiar `id` a `UUIDField`, perdiendo el autoincremento simple.
2. Un modelo con PK compuesta no se puede registrar en el Django admin
   (`ImproperlyConfigured`) — pérdida menor (ese registro ya era de solo lectura), pero
   real.
3. **El más serio**: un `AlterField` directo de `id` entero a `UUIDField` no sabe cómo
   convertir los valores existentes — corrompió silenciosamente las filas ya
   guardadas (quedaron con enteros donde Django ahora espera UUIDs; cualquier lectura
   posterior truena con `ValueError: badly formed hexadecimal UUID string`). Una
   migración correcta necesita un paso `RunPython` que genere un UUID nuevo por fila
   existente antes de cambiar el tipo de columna — sin poder probarlo contra un
   Postgres/TimescaleDB real, no se implementó a ciegas.

**Se revirtió el intento.** La tabla sigue como PostgreSQL normal (funcional, sin
particionado). Opciones para retomarlo, en orden de preferencia:
1. Clave primaria compuesta `(timestamp, id-como-UUID)` con backfill correcto vía
   `RunPython` — la solución completa, probarla primero contra un Postgres/TimescaleDB
   de prueba (no producción) antes de aplicarla.
2. Ejecutar `create_hypertable` manualmente tras ajustar los índices, sin pasar por el
   ORM de Django.

No es urgente para el piloto (la tabla funciona igual sin hypertable, solo sin
particionado/compresión nativa); sí conviene resolverlo antes del rollout completo a
~1.800 equipos, cuando el volumen de `muestra_metrica` empiece a pesar de verdad.

## MeshCentral (acceso remoto) — validado end-to-end (6-ago-2026)

El servicio `meshcentral` en `docker-compose.yml` usa `config.json` como fuente de
verdad (no variables de entorno — ver por qué en el comentario del volumen en
`docker-compose.yml`). Antes de levantarlo:

```bash
cp deploy/meshcentral/config.json.example deploy/meshcentral/config.json
#    Editar deploy/meshcentral/config.json: "cert" con la IP o dominio público del
#    servidor (mismo criterio que ALLOWED_HOSTS — sin esquema, sin puerto).
```

Pasos (probados de punta a punta contra un despliegue real, no solo compilados):

1. Levantar: `docker-compose -f deploy/docker-compose.yml up -d meshcentral`. La
   primera vez genera certificados propios, tarda uno o dos minutos.
2. Entrar a `https://<IP-o-dominio>:8083` (certificado self-signed — aceptar la
   advertencia del navegador) y crear la primera cuenta: **queda como administrador
   del sitio automáticamente**. Apenas la crees, poné `"NewAccounts": false` en
   `config.json`, bajá y volvé a levantar `meshcentral` para que nadie más se registre.
3. En la consola, crear un device group único (ej. "Estaciones SAIDSOFT"). Al agregar
   un agente ("Add Agent" → cualquier link de descarga), el Mesh ID queda en la URL
   (parámetro `meshid=`, una cadena larga) — copiarlo.
4. Poner ese Mesh ID en `MESHCENTRAL_MESH_ID` de `deploy/.env` y reiniciar `web`
   (`docker-compose -f deploy/docker-compose.yml up -d web`).
5. Instalar el agente en una estación piloto desde el panel ("Instalar agente ahora")
   y verificar que aparece en el device group de la consola de MeshCentral. Copiar su
   Node ID (visible en la URL al entrar a su ficha) y pegarlo en la sección "Acceso
   remoto (MeshCentral)" del detalle de la estación en el panel, para vincularla.
6. Probar "Abrir escritorio remoto"/"Abrir terminal remota" desde el panel — los
   defaults de `MESHCENTRAL_VIEWMODE_ESCRITORIO`/`_TERMINAL` (`10`/`12`) ya están
   verificados contra una instancia real, no deberían necesitar ajuste salvo que
   cambien de versión de MeshCentral.
7. A partir de ~50 estaciones vinculadas, sumar MongoDB (`USE_MONGODB`/`MONGO_URL`,
   no incluido en este stack) — NeDB (archivo, el default) alcanza para el piloto pero
   no está pensado para la escala completa (~1.800 equipos) del plan.

**Dos bugs reales encontrados en el primer piloto**, ya corregidos en
`config.json.example` — mencionados acá porque son fáciles de reintroducir editando
`config.json` a mano sin saber por qué existen:
- `"agentTimeStampServer": false` — por defecto, MeshCentral firma los ejecutables del
  agente contra un servidor de timestamping público (`timestamp.comodoca.com`). Si la
  red del servidor no permite esa salida (común en redes de farmacia con salida
  restringida), el arranque se cuelga indefinidamente a mitad del firmado — sin error,
  sin timeout visible, `docker logs` simplemente deja de avanzar. Se ve con
  `docker stats <container>` en `0.00% CPU` (bloqueado, no lento) y confirmando con
  `docker exec <container> cat /proc/<pid>/net/tcp` una conexión saliente con
  retransmisiones sin respuesta.
- `"aliasPort": 8083` — sin esto, MeshCentral no sabe que Docker le remapea el puerto
  443 interno al 8083 externo, y genera los links de instalación de agente
  (`meshServer=wss://...`) apuntando al 443 interno. El instalador corre sin ningún
  error y el servicio de Windows queda "Running", pero el agente nunca logra conectarse
  de vuelta — hay que revisar `C:\Program Files\Mesh Agent\MeshAgent.msh` (línea
  `MeshServer=`) para notar el puerto equivocado.

Es un bolt-on completamente aparte del canal MQTT/HMAC del agente SAIDSOFT: si algo
falla acá, no afecta despliegues, heartbeats ni Scripts RMM.

### Auditoría por grabación (no en vivo)

Distinto del acceso remoto en vivo de arriba: es el botón "Ver grabaciones" del panel
(permiso `catalogo.supervision_auditoria_estacion`, separado de `acceso_remoto_estacion`
a propósito — ver `apps/catalogo/models.py`). No graba nada por sí solo: hay que
habilitar la grabación de sesión en el servidor MeshCentral.

**Probado** (contenedor suelto, fuera de este `docker-compose.yml`): el bloque de abajo,
agregado a `domains[""]` en `meshcentral-data/config.json` y reiniciando el contenedor,
lo acepta sin error — instala solo el módulo `image-size` que necesita para indexar, y
el servidor vuelve a arrancar normal.

```json
"domains": {
  "": {
    "sessionRecording": {
      "onlySelectedDeviceGroups": true,
      "filepath": "records",
      "index": true,
      "maxRecordings": 100,
      "maxRecordingDays": 90,
      "maxRecordingSizeMegabytes": 500,
      "protocols": [2]
    }
  }
}
```

`protocols: [2]` = solo escritorio (no terminal ni transferencia de archivos).
`onlySelectedDeviceGroups: true` porque no se quiere grabar todo por defecto: además de
este bloque, hay que marcar el device group "Estaciones SAIDSOFT" para grabación desde
su configuración en la consola de MeshCentral (checkbox de grabación del grupo).

**No probado**: que la grabación ocurra de verdad al conectarse a un agente real, ni el
checkbox exacto del device group — falta correrlo con una estación piloto real para
confirmar el flujo completo (grabar → listar en "Recordings" → reproducir).

## Notas

- **Los agentes** apuntan a `emqx:8883` con `MqttUsarTls=true` y las credenciales del
  usuario `saidsof_agente`. Deben confiar en el CA que firmó el cert de EMQX. Se
  instalan con `deploy/instalar-agente.ps1` (repo `saidsoft-agente`), que además
  necesita `COMANDO_HMAC_SECRET` (idéntico al de este `.env`, si no coincide el agente
  descarta comandos remotos en silencio) y los datos del POS real — ver
  PLAN_MODERNIZACION.md §10-C.
- **`.dockerignore`**: existe desde 31-jul-2026 — sin él, `COPY . .` del `Dockerfile`
  horneaba `.env`/`deploy/.env`/`deploy/certs/key.pem` dentro de la imagen aunque
  estuvieran en `.gitignore` (que no protege el build context de Docker). Si alguna vez
  se elimina por error, revisar que `COMANDO_HMAC_SECRET` siga llegando por
  `docker-compose.yml` y no por un `.env` de desarrollo horneado por accidente. **Ojo**:
  solo excluye `key.pem` (la llave privada) — `cert.pem` (público) SÍ debe quedar en la
  imagen, `web`/`worker` lo necesitan en tiempo de ejecución
  (`MQTT_CA_CERT=/app/deploy/certs/cert.pem`); excluirlo también rompe el worker en un
  crash-loop (encontrado al levantar el stack real).
- **Todo proceso bajo `config.settings.produccion` necesita `COMANDO_HMAC_SECRET`** (sin
  default) — no solo `web`/`worker`, también `celery_worker`/`celery_beat`. Si agregas
  un servicio nuevo que corra `manage.py` con esas settings, declara este var en su
  `environment` o va a tronar en bucle con `ImproperlyConfigured` (encontrado con el
  scheduler previo a Celery al levantar el stack real 31-jul-2026).
- **`ARCHIVOS_BASE_URL`** debe ser la URL pública del panel (los agentes descargan de
  `/media/despliegues/...`). Con la distribución en cascada, la mayoría descargará del
  caché de su farmacia, pero el central debe ser alcanzable como fallback. **Sin barra
  final** (`http://ip:8080`, no `http://ip:8080/`): el código ya la recorta, pero el
  valor correcto evita confusiones al leer la config.
- **Volumen de media con dueño equivocado (despliegues creados antes del 6-ago-2026)**:
  `deploy/Dockerfile` ahora crea `/app/media` con dueño `appuser`, pero un volumen
  `media_data` que ya existía conserva el dueño `root` con el que Docker lo creó, y
  subir un paquete falla con `PermissionError: '/app/media/despliegues'` (HTTP 500).
  Se arregla una sola vez, sin recrear el volumen:
  ```bash
  sudo docker exec -u root deploy_web_1 chown appuser:appuser /app/media
  ```
- **Subidas grandes por VPN**: `web` corre con `--timeout 600` justamente porque subir
  un `.zip` de decenas de MB desde una farmacia tarda varios minutos; con el default
  anterior (120s) gunicorn mataba al worker a mitad de la subida y el navegador quedaba
  "cargando" sin error. Si aparece algo así, `docker-compose logs web` ahora tiene el
  access log de gunicorn (`--access-logfile -`) para ver el código de respuesta real.
- **ACLs de EMQX**: `bootstrap-emqx.sh` las siembra automáticamente y quedó verificado
  end-to-end contra una instancia real (creación de las 3 reglas confirmada vía la API
  de EMQX, y una prueba real de publish/subscribe con `paho-mqtt` mostró que un tópico
  permitido entrega el mensaje y uno sin regla nunca llega al suscriptor, aunque el
  publicador reciba el PUBACK igual — eso solo confirma que el broker recibió el
  paquete, no que lo autorizó). Requiere que `docker-compose.yml` declare
  `EMQX_AUTHORIZATION__SOURCES__1__TYPE=built_in_database` como fuente — sin eso, EMQX
  usa por defecto una fuente de tipo `file` cuya última regla es `{allow, all}`, y
  `no_match: deny` nunca llega a aplicarse porque esa regla siempre matchea primero.
  Quedan por rol (agente/worker/panel), no por unidad de negocio — la credencial del
  agente sigue siendo compartida por las ~1.800 estaciones; segmentar por tenant es un
  gap aparte, ver PLAN_MODERNIZACION.md §9.
- **Pendiente en el próximo despliegue: volver a correr `bootstrap-emqx.sh`** (16-ago-2026,
  fase R7 — inventario de software instalado, ver PLAN_MODERNIZACION.md §9). Se agregó
  la regla de `subscribe` de `/saidsof/agente/+/software_instalado/` para `worker`; sin
  volver a correr el script en el servidor real, el escaneo de software instalado va a
  fallar en silencio exactamente como describe el bug de más arriba (mensaje publicado,
  nunca entregado, sin error visible ni en el agente ni en el panel). El script es
  reentrante (ver el fix de idempotencia arriba) — correrlo de nuevo no rompe las reglas
  ya sembradas.
- **Misma pendiente, segunda regla agregada la misma sesión (16-ago-2026)**: monitoreo
  de errores del POS (ver PLAN_MODERNIZACION.md §9) agregó también `subscribe` de
  `/saidsof/agente/+/pos_errores/` para `worker` — una sola corrida de
  `bootstrap-emqx.sh` cubre las dos reglas nuevas (software_instalado + pos_errores),
  no hace falta correrlo dos veces.
- **Respaldos**: `deploy/backup.sh` (pg_dump + media, con retención de 14 días
  locales), pensado para el cron del host — ver paso 7 arriba. No reemplaza una copia
  fuera del servidor.
- **Bug real encontrado en producción (20-ago-2026): `docker-compose down` + `up`
  "pierde" las credenciales MQTT de EMQX aunque el volumen `emqx_data` sea con
  nombre y sobreviva.** El servicio `emqx` no tenía `hostname` fijo — cada
  recreación del contenedor le daba una identidad interna nueva, y la base Mnesia
  de EMQX (donde vive `built_in_database`: los usuarios/ACLs que siembra
  `bootstrap-emqx.sh`) queda atada a esa identidad, así que un nodo con hostname
  distinto arranca "en blanco" pese a que los archivos del volumen siguen intactos.
  Reproducido dos veces seguidas en el mismo despliegue: cada `down`+`up`/rebuild
  dejaba a `worker` y a los agentes en loop de `[MQTT] Falló la conexión: Not
  authorized` hasta volver a correr `bootstrap-emqx.sh`. Fix: `hostname: emqx`
  fijo en el servicio (`docker-compose.yml`), le da continuidad al nodo entre
  recreaciones. **Confirmado en producción (20-ago-2026)**: tras el fix, un
  `docker-compose down` + `up -d` completo (recreó los 9 contenedores de cero,
  red nueva) dejó a `worker` conectado sin `Not authorized` — ya no hace falta
  volver a correr `bootstrap-emqx.sh` en cada recreación. **Si de todas formas
  vuelve a pasar** (ej. tras un `docker-compose down -v`, que sí borra el
  volumen), el remedio sigue siendo el mismo: volver a correr
  `sh bootstrap-emqx.sh` — es reentrante, seguro de repetir.
