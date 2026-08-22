#!/bin/sh
# Respaldo de la base de datos y de media (paquetes de despliegue, imágenes de
# mantenimiento, etc.) del stack de producción.
#
# A diferencia de deploy/scheduler.sh (tareas de la aplicación, corren dentro del
# stack), esto es una tarea de infraestructura que necesita orquestar `docker compose`
# desde afuera — pensado para el cron del HOST, no para un servicio del stack:
#
#   0 2 * * * cd /ruta/al/proyecto && sh deploy/backup.sh /ruta/a/backups >> /var/log/saidsoft-backup.log 2>&1
#
# No reemplaza una copia fuera del servidor (a otra máquina/almacenamiento externo) —
# eso es responsabilidad de operaciones, un script no puede resolverlo solo.
set -eu

DESTINO="${1:-./backups}"
case "$DESTINO" in
    /*) ;;
    *) DESTINO="$(pwd)/$DESTINO" ;;
esac
mkdir -p "$DESTINO"

DIR="$(dirname "$0")"
cd "$DIR"
. ./.env

FECHA=$(date +%Y%m%d_%H%M%S)

echo "Respaldando base de datos..."
docker-compose -f docker-compose.yml exec -T db \
    pg_dump -U "$POSTGRES_USER" "$POSTGRES_DB" | gzip > "$DESTINO/db_$FECHA.sql.gz"

echo "Respaldando media (paquetes de despliegue, imágenes de mantenimiento, etc.)..."
docker-compose -f docker-compose.yml exec -T web \
    tar czf - -C /app/media . > "$DESTINO/media_$FECHA.tar.gz"

echo "Retención: borrando respaldos locales de más de 14 días en $DESTINO..."
find "$DESTINO" -name "db_*.sql.gz" -mtime +14 -delete
find "$DESTINO" -name "media_*.tar.gz" -mtime +14 -delete

echo "Listo: $DESTINO/db_$FECHA.sql.gz y $DESTINO/media_$FECHA.tar.gz"
