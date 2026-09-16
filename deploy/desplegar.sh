#!/bin/sh
# Despliegue de SAIDSOFT en el servidor. Ejecutar desde cualquier lado:
#
#     sh ~/Documentos/Said/saidsoft-core/deploy/desplegar.sh
#
# Existe porque saltearse el `build` NO falla: simplemente deja el servidor corriendo el
# código anterior, sin ningún aviso. Pasó el 15-sep-2026 — el `git pull` llegó, el commit
# estaba en el servidor, y los contenedores siguieron con la imagen vieja. `git log` decía
# una cosa y la aplicación hacía otra, y las dos eran ciertas: hablan de lugares distintos.
#
# Ese día las migraciones nuevas tampoco se aplicaron, y no porque falte correr `migrate`
# —el entrypoint lo hace solo— sino porque la imagen vieja no las contenía: el entrypoint
# corrió las migraciones que conocía, que ya estaban todas puestas.
#
# El código vive en dos lados. `git pull` actualiza el DISCO; los contenedores arrancan
# desde una IMAGEN, que es una copia del código sacada al construirla. Sin `build`, la
# copia sigue siendo la de antes.
set -e

REPO="$(cd "$(dirname "$0")/.." && pwd)"

# La ubicación del repo sale de dónde está ESTE archivo, así que el script tiene que
# vivir en deploy/. Copiarlo a otro lado (p.ej. /tmp) hace que $REPO apunte a cualquier
# cosa y el `git pull` falle con un mensaje que no explica nada.
if [ ! -d "$REPO/.git" ]; then
    echo "ERROR: '$REPO' no es el repositorio. Este script tiene que ejecutarse desde" >&2
    echo "       su lugar en el repo: sh ~/Documentos/Said/saidsoft-core/deploy/desplegar.sh" >&2
    exit 1
fi

cd "$REPO"

echo "==> 1/4  git pull"
git pull

cd "$REPO/deploy"

# `build` SIN argumento a propósito: este proyecto tiene CINCO imágenes (web, worker,
# celery_worker, celery_beat, meshcentral_worker). Con `build web` la parte web queda
# actualizada y los procesos de fondo —el que habla por MQTT con los agentes, el que
# corre las tareas programadas— siguen con el código viejo. Ya pasó el 11-sep-2026.
echo "==> 2/4  build (las cinco imágenes)"
docker compose --env-file .env build

# `|| true` a propósito: `up -d` puede fallar con "the container name ... is already in
# use" y aun así dejar todo corriendo. Pasa porque cuatro contenedores de este servidor
# (db, emqx, redis, meshcentral) quedaron con nombres prefijados por un hash de un
# conflicto viejo, y Compose reintenta el renombrado en cada despliegue. Con `set -e` el
# script abortaba ahí y nunca llegaba a la verificación — o sea que fallaba justo antes
# del paso que podía decir si el despliegue había servido o no.
#
# No se ignora el problema: si de verdad quedó algo mal, la verificación del final lo
# detecta y el script sale con error igual. Lo que cambia es QUÉ decide el resultado —
# el estado real del servidor, no el código de salida de un comando que miente.
echo "==> 3/4  up -d"
docker compose --env-file .env up -d || echo "   (up -d devolvió error; se sigue y lo decide la verificación del final)"

# El entrypoint YA corre `migrate` al arrancar cada contenedor (ver entrypoint.sh).
# Se repite acá solo para VER el resultado en la salida del despliegue: sin esto, que una
# migración se aplique o no queda enterrado en los logs del contenedor. Es idempotente,
# así que repetirlo no cuesta nada.
echo "==> 4/4  migrate (el entrypoint ya lo corrió; esto lo deja a la vista)"
docker compose exec -T web python manage.py migrate || true

# Comprobación final: que no quede ninguna migración sin aplicar. Es lo que convierte
# este script en algo más que un atajo — si un paso no tuvo efecto, se entera acá y no
# tres días después, cuando algo no funcione por un motivo que parece no tener relación.
echo
echo "==> Verificación"
PENDIENTES=$(docker compose exec -T web python manage.py showmigrations --plan 2>/dev/null | grep -c '^\[ \]' || true)
if [ "$PENDIENTES" -gt 0 ]; then
    echo "AVISO: quedan $PENDIENTES migración(es) sin aplicar:"
    docker compose exec -T web python manage.py showmigrations --plan 2>/dev/null | grep '^\[ \]'
    exit 1
fi

# Se verifica por SERVICIO y no por nombre de contenedor: cuatro de este servidor
# arrastran un prefijo de hash (ej. "1abca43cf424_deploy-db-1") de un conflicto viejo, y
# buscar el nombre exacto daría falsos negativos.
FALTAN=""
for SERVICIO in db redis emqx nginx web worker celery_worker celery_beat meshcentral meshcentral_worker; do
    ESTADO=$(docker compose ps --format '{{.Service}} {{.State}}' 2>/dev/null | awk -v s="$SERVICIO" '$1==s {print $2}')
    if [ "$ESTADO" != "running" ]; then
        FALTAN="$FALTAN $SERVICIO(${ESTADO:-ausente})"
    fi
done
if [ -n "$FALTAN" ]; then
    echo "AVISO: estos servicios no están corriendo:$FALTAN"
    exit 1
fi

# Que la web conteste es lo único que prueba que el despliegue sirvió de verdad: los
# contenedores pueden estar "running" y la aplicación caída por un error de arranque.
#
# El host sale de ALLOWED_HOSTS del .env y no se escribe "localhost": Django responde
# 400 a cualquier Host que no esté en esa lista, así que pedirle a localhost daba un
# falso fallo en cada despliegue aunque la aplicación estuviera perfecta.
HOST=$(grep -E '^ALLOWED_HOSTS=' .env | head -1 | cut -d= -f2- | cut -d, -f1 | tr -d ' "')
if [ -z "$HOST" ]; then
    echo "AVISO: no se pudo leer ALLOWED_HOSTS del .env; se omite el chequeo web."
else
    # Con reintentos: el contenedor recién recreado tarda en levantar gunicorn, y
    # mientras tanto nginx contesta 502. Una sola consulta inmediata daba "la web no
    # responde" sobre un despliegue perfectamente sano (16-sep-2026) — un falso fallo que
    # cuesta caro, porque enseña a desconfiar de la verificación y entonces deja de
    # servir para detectar una caída real.
    CODIGO=000
    for INTENTO in 1 2 3 4 5 6 7 8 9 10; do
        CODIGO=$(curl -sk -o /dev/null -w '%{http_code}' "https://$HOST:8084/login/" || echo 000)
        if [ "$CODIGO" = "200" ]; then
            break
        fi
        echo "   esperando a que la web levante (intento $INTENTO/10, HTTP $CODIGO)..."
        sleep 3
    done
    if [ "$CODIGO" != "200" ]; then
        echo "AVISO: la web no responde 200 en https://$HOST:8084/login/ tras 30s (devolvió $CODIGO)."
        echo "       Revisar: docker compose logs --tail 60 web"
        exit 1
    fi
    echo "Web respondiendo (HTTP $CODIGO)."
fi

echo "Todo aplicado. Commit desplegado:"
cd "$REPO" && git log --oneline -1
