# Despliegue en producción

Stack en `deploy/`: panel web (Django + gunicorn), worker MQTT, TimescaleDB y EMQX,
orquestados con Docker Compose. Reemplaza el SQLite y el broker `amqtt` de desarrollo.

> **Estado**: la infraestructura está escrita y validada en lo que se puede sin Docker
> (la config de producción de Django carga con PostgreSQL, `collectstatic` con WhiteNoise
> funciona, la WSGI app importa, el `docker-compose.yml` es YAML válido, la migración del
> hypertable es un no-op seguro fuera de TimescaleDB). **No se ejecutó `docker compose up`
> en el equipo de desarrollo** (no tiene Docker). El primer arranque debe hacerse en el
> servidor destino siguiendo estos pasos, y ahí conviene una prueba piloto antes del rollout.

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

# 5. Sembrar usuarios MQTT y definir ACLs en EMQX
sh deploy/bootstrap-emqx.sh
#    Luego, en el dashboard de EMQX (http://host:18083), definir las ACLs por tópico.

# 6. Tareas periódicas (cron del host o del contenedor web):
#    - marcar estaciones offline (cada minuto)
#    - purgar métricas viejas (diario)
#    docker compose -f deploy/docker-compose.yml exec web python manage.py marcar_estaciones_offline
#    docker compose -f deploy/docker-compose.yml exec web python manage.py purgar_metricas --dias 30
```

## Servicios

| Servicio | Imagen | Rol |
|---|---|---|
| `db` | timescale/timescaledb:pg16 | PostgreSQL + TimescaleDB (métricas como hypertable) |
| `emqx` | emqx:5.8 | Broker MQTT con TLS (8883) y auth built-in; TCP plano desactivado |
| `web` | (build local) | Panel Django con gunicorn; corre migraciones/collectstatic al arrancar |
| `worker` | (build local) | Worker MQTT (`run_mqtt_worker`) |

## TimescaleDB — nota sobre el hypertable

La migración `monitoreo.0002` intenta convertir `muestra_metrica` en hypertable, pero
TimescaleDB rechaza hacerlo si un índice único excluye la columna de tiempo — y el PK `id`
que Django agrega por defecto lo hace. Por eso la migración **captura ese error y sigue**:
la tabla queda como PostgreSQL normal (funcional, solo sin las ventajas de particionado).

Para activar el hypertable de verdad a escala real, hay que quitar ese PK simple. Opciones:
1. Clave primaria compuesta `(timestamp, id)` en el modelo (Django 5.2 `CompositePrimaryKey`), o
2. Ejecutar el `create_hypertable` manualmente tras ajustar los índices.

No es urgente para el piloto; sí conviene resolverlo antes del rollout completo.

## Notas

- **Los agentes** apuntan a `emqx:8883` con `MqttUsarTls=true` y las credenciales del
  usuario `saidsof_agente`. Deben confiar en el CA que firmó el cert de EMQX.
- **`ARCHIVOS_BASE_URL`** debe ser la URL pública del panel (los agentes descargan de
  `/media/despliegues/...`). Con la distribución en cascada, la mayoría descargará del
  caché de su farmacia, pero el central debe ser alcanzable como fallback.
- **Respaldos**: programar `pg_dump` del volumen `db_data` y respaldar `media_data`
  (los paquetes de despliegue).
