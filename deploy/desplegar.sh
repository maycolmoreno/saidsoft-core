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

echo "==> 3/4  up -d"
docker compose --env-file .env up -d

# El entrypoint YA corre `migrate` al arrancar cada contenedor (ver entrypoint.sh).
# Se repite acá solo para VER el resultado en la salida del despliegue: sin esto, que una
# migración se aplique o no queda enterrado en los logs del contenedor. Es idempotente,
# así que repetirlo no cuesta nada.
echo "==> 4/4  migrate (el entrypoint ya lo corrió; esto lo deja a la vista)"
docker compose exec -T web python manage.py migrate

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

CAIDOS=$(docker compose ps --format '{{.Name}} {{.State}}' 2>/dev/null | grep -v ' running' || true)
if [ -n "$CAIDOS" ]; then
    echo "AVISO: hay contenedores que no están corriendo:"
    echo "$CAIDOS"
    exit 1
fi

echo "Todo aplicado. Commit desplegado:"
cd "$REPO" && git log --oneline -1
