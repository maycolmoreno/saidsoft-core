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

- Docker Engine + Docker Compose v2.
- Un proxy TLS delante (traefik/nginx/caddy) que termine HTTPS para el panel y reenvíe
  a `web:8000` con la cabecera `X-Forwarded-Proto: https` (Django ya la respeta).
- Specs sugeridas para ~1.800 equipos: 8 vCPU / 16 GB RAM / SSD (ver PLAN_MODERNIZACION.md).

## Pasos

```bash
# 1. Variables y secretos
cp deploy/.env.prod.example deploy/.env
#    Editar deploy/.env: SECRET_KEY, ALLOWED_HOSTS, contraseñas de BD/EMQX, etc.
python -c "from django.core.management.utils import get_random_secret_key as k; print(k())"  # para SECRET_KEY

# 2. Certificado TLS para EMQX
sh deploy/certs/generar-certs-dev.sh        # self-signed para arrancar…
#    …en producción, reemplazar deploy/certs/{cert,key}.pem por certificados reales
#    y distribuir el CA a los agentes.

# 3. Levantar el stack (migraciones y collectstatic corren solos en el arranque del web)
docker compose --env-file deploy/.env -f deploy/docker-compose.yml up -d --build

# 4. Crear el superusuario del panel
docker compose -f deploy/docker-compose.yml exec web python manage.py createsuperuser

# 5. Sembrar usuarios MQTT y definir ACLs en EMQX (siembra usuarios y reglas por
#    tópico en un solo paso; docker-compose.yml ya fija no_match=deny)
sh deploy/bootstrap-emqx.sh

# 6. Las tareas periódicas ya NO necesitan cron externo: el servicio `celery_beat`
#    (ver docker-compose.yml) las dispara solo según CELERY_BEAT_SCHEDULE en
#    config/settings/base.py — marcar estaciones offline (cada minuto), purgar métricas
#    viejas, generar ejecuciones/mantenimientos programados vencidos (diario).
#    Los comandos manuales se conservan por si hace falta correrlos a mano:
#    docker compose -f deploy/docker-compose.yml exec web python manage.py marcar_estaciones_offline
#    docker compose -f deploy/docker-compose.yml exec web python manage.py purgar_metricas --dias 30

# 7. Backups: sí van por cron del host (necesitan orquestar `docker compose` desde
#    afuera del stack) — agregar al crontab:
#    0 2 * * * cd /ruta/al/proyecto && sh deploy/backup.sh /ruta/a/backups >> /var/log/saidsoft-backup.log 2>&1

# 8. (Opcional) Acceso remoto — configurar MeshCentral, ver sección abajo.
```

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

## MeshCentral (acceso remoto) — opcional, no validado end-to-end

El servicio `meshcentral` en `docker-compose.yml` es **nuevo y sin correr todavía**: se
armó siguiendo la guía oficial (imagen `ghcr.io/ylianst/meshcentral`, variables
`HOSTNAME`/`NODE_ENV`/`ALLOW_NEW_ACCOUNTS`, volúmenes `meshcentral-data`/`meshcentral-files`),
pero nadie lo levantó contra un servidor real todavía. Antes de depender de él en
producción:

1. Completar `MESHCENTRAL_HOSTNAME`/`MESHCENTRAL_SERVER_URL` en `deploy/.env` (mismo
   dominio, con y sin esquema) y levantarlo: `docker compose -f deploy/docker-compose.yml up -d meshcentral`.
2. Entrar a `https://<MESHCENTRAL_HOSTNAME>:8083` (o el puerto que exponga el proxy TLS)
   con `MESHCENTRAL_ALLOW_NEW_ACCOUNTS=true` — la primera cuenta creada queda como admin.
   **Volver `ALLOW_NEW_ACCOUNTS` a `false` y reiniciar el contenedor** apenas se crea esa cuenta.
3. Crear un device group único (ej. "Estaciones SAIDSOFT"), copiar su Mesh ID a
   `MESHCENTRAL_MESH_ID` en `deploy/.env` y reiniciar `web` (`docker compose -f deploy/docker-compose.yml up -d web`).
4. Instalar el agente en una estación piloto desde el panel ("Instalar agente ahora")
   y verificar que aparece en la consola de MeshCentral.
5. Abrir el link `?node=<id>` una vez a mano, navegar a las pestañas de escritorio y
   terminal, y ajustar `MESHCENTRAL_VIEWMODE_ESCRITORIO`/`_TERMINAL` en `deploy/.env`
   con los valores que MeshCentral use en su propia URL (los `.env.prod.example` traen
   `11`/`12` sin verificar).
6. A partir de ~50 estaciones vinculadas, sumar MongoDB (`USE_MONGODB`/`MONGO_URL`,
   no incluido en este stack) — NeDB (archivo, el default) alcanza para el piloto pero
   no está pensado para la escala completa (~1.800 equipos) del plan.

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
  caché de su farmacia, pero el central debe ser alcanzable como fallback.
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
- **Respaldos**: `deploy/backup.sh` (pg_dump + media, con retención de 14 días
  locales), pensado para el cron del host — ver paso 7 arriba. No reemplaza una copia
  fuera del servidor.
