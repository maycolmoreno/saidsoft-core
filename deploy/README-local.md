# Entorno local de pruebas

Probar en tu máquina antes de tocar producción. La idea es simple: **la misma base de
datos que el servidor**, y Django corriendo nativo para poder iterar rápido.

## Por qué no alcanza con SQLite

Hasta ahora las pruebas locales corrían en SQLite y producción es PostgreSQL con
TimescaleDB. Esa diferencia esconde una familia entera de bugs que solo aparecen en el
servidor:

- Sumar y contar en el mismo `annotate()`: el JOIN duplica filas y los totales salen
  inflados. Pasó de verdad — un reporte de viáticos de $40 con dos alertas sumaba $80.
- Resultados sin `ORDER BY`: SQLite suele devolverlos en un orden estable, Postgres no.
- Semántica de transacciones y de `select_for_update`, que en SQLite es casi un no-op.

Con este entorno, lo que probás local es lo que va a pasar en el servidor.

## Levantarlo

Solo hacen falta la base y Redis; el resto del stack (EMQX, MeshCentral) únicamente si
vas a probar agentes.

```sh
docker compose -f deploy/docker-compose.yml --env-file deploy/.env up -d db redis
```

La base queda en `127.0.0.1:5433`. Las credenciales salen de `deploy/.env` — el mismo
archivo que ya usás, no hay uno nuevo que mantener.

## Apuntar Django a Postgres

Agregá esta línea al `.env` de la raíz del repo (está en `.gitignore`, no se versiona),
reemplazando usuario, clave y base por los de `deploy/.env`:

```
DATABASE_URL=postgres://<POSTGRES_USER>:<POSTGRES_PASSWORD>@127.0.0.1:5433/<POSTGRES_DB>
```

Después:

```sh
python manage.py migrate
python manage.py test apps --noinput
python manage.py runserver
```

**Para volver a SQLite**, borrá o comentá esa línea: `DATABASE_URL` tiene un default a
`db.sqlite3`. Sirve para una prueba rápida y suelta, pero lo que va a producción se
prueba contra Postgres.

## Traer datos de producción

Los respaldos del servidor son `pg_dump` cifrados con GPG (ver `deploy/backup.sh`), así
que se restauran tal cual en el Postgres local. Con el servidor accesible:

```sh
# 1. Traer el más reciente
scp glpi@10.111.6.20:'~/backups/saidsoft/db_*.sql.gz.gpg' /tmp/

# 2. Descifrar (la passphrase es BACKUP_ENCRYPTION_PASSPHRASE del deploy/.env del SERVIDOR)
gpg --decrypt /tmp/db_XXXXXXXX.sql.gz.gpg | gunzip > /tmp/prod.sql

# 3. Restaurar en la base local
docker exec -i deploy-db-1 psql -U <POSTGRES_USER> -d <POSTGRES_DB> < /tmp/prod.sql

# 4. Borrar el descifrado: es la base entera de producción en texto plano
rm -f /tmp/prod.sql /tmp/db_*.sql.gz.gpg
```

El paso 4 no es opcional. Ese archivo tiene los datos de las 700 farmacias, los usuarios
y las claves de BitLocker cifradas.

`deploy/restaurar-backup.sh` hace lo mismo en el servidor con más resguardos (restaura
contra una base `restore_test` salvo que le pases otro nombre explícitamente **dos**
veces); acá conviene el camino manual porque la base local es descartable.

## Cuándo el test de humo no alcanza

Correr las pruebas y el `runserver` cubre la lógica, pero hay cosas que solo se ven
contra el servidor real: el agente hablando por MQTT, MeshCentral, y el sondeo SNMP a
las farmacias. Para eso no hay atajo local — hay que desplegar y mirar.

## Promover a producción

Una vez que anda local:

```sh
# En tu máquina
git push origin master

# En el servidor
cd ~/Documentos/Said/saidsoft-core && git pull --ff-only
cd deploy
docker compose --env-file .env build web
docker compose --env-file .env up -d
```

Las migraciones corren solas al arrancar `web` (`RUN_MIGRATIONS=1` en su `environment`).

**Si tocaste `deploy/nginx/nginx.conf`**, hay que RECREAR el contenedor, no recargarlo:
un bind mount de archivo ata el inode, y `git pull` reemplaza el archivo, así que el
contenedor sigue viendo el viejo y `nginx -t` valida el que no es. Ver
`PLAN_MODERNIZACION.md` §10-AA.

## Cuidado con el nombre del proyecto

Compose deriva el nombre del proyecto del directorio del archivo, así que
`deploy/docker-compose.yml` es siempre el proyecto `deploy`. Un segundo compose en esa
carpeta con un servicio llamado igual **recrea el contenedor existente** y puede
reapuntarlo a otro volumen. Si alguna vez necesitás un stack paralelo, usá
`-p <otro-nombre>`.
