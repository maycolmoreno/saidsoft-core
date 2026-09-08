#!/bin/sh
# Espera a que la base del stack esté healthy antes de dejar correr una tarea que
# depende de ella.
#
# Existe por el arranque tras un corte de energía. `Persistent=true` del timer dispara
# la ejecución atrasada apenas systemd arranca la unidad, y `After=docker.service` solo
# garantiza que el DEMONIO de Docker esté activo — no que los contenedores con
# `restart: unless-stopped` ya hayan terminado de levantar. Sin esta espera, el respaldo
# del primer arranque del lunes fallaría con "connection refused" contra una base que
# iba a estar lista treinta segundos después.
set -eu

INTENTOS="${1:-60}"
ESPERA="${2:-5}"

cd "$(dirname "$0")"

i=0
while [ "$i" -lt "$INTENTOS" ]; do
    CONTENEDOR="$(docker-compose -f docker-compose.yml ps -q db 2>/dev/null || true)"
    if [ -n "$CONTENEDOR" ]; then
        ESTADO="$(docker inspect -f '{{.State.Health.Status}}' "$CONTENEDOR" 2>/dev/null || true)"
        if [ "$ESTADO" = 'healthy' ]; then
            echo "La base está healthy."
            exit 0
        fi
    fi
    i=$((i + 1))
    sleep "$ESPERA"
done

echo "ERROR: la base no quedó healthy tras $((INTENTOS * ESPERA))s — se aborta la tarea." >&2
exit 1
