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
# OPS-2 (auditoría 22-ago-2026): los .sql.gz/.tar.gz quedan cifrados con GPG (AES256
# simétrico, BACKUP_ENCRYPTION_PASSPHRASE en .env) — sin la passphrase, nadie que
# copie estos archivos (ni siquiera desde un backup robado/filtrado, o el destino
# offsite que todavía no existe) puede leer la base ni los paquetes de despliegue. Ver
# deploy/restaurar-backup.sh para el procedimiento de restauración (ya probado contra
# una base real, ver PLAN_MODERNIZACION.md OPS-2).
#
# Sigue sin reemplazar una copia fuera del servidor (a otra máquina/almacenamiento
# externo) — pendiente, ver PLAN_MODERNIZACION.md OPS-2.
set -eu
umask 077

DESTINO="${1:-./backups}"
case "$DESTINO" in
    /*) ;;
    *) DESTINO="$(pwd)/$DESTINO" ;;
esac
mkdir -p "$DESTINO"

DIR="$(dirname "$0")"
cd "$DIR"
. ./.env

if [ -z "${BACKUP_ENCRYPTION_PASSPHRASE:-}" ]; then
    echo "ERROR: falta BACKUP_ENCRYPTION_PASSPHRASE en .env — sin eso no se puede cifrar el backup." >&2
    exit 1
fi

FECHA=$(date +%Y%m%d_%H%M%S)
DB_SIN_CIFRAR="$DESTINO/db_$FECHA.sql.gz"
MEDIA_SIN_CIFRAR="$DESTINO/media_$FECHA.tar.gz"

echo "Respaldando base de datos..."
docker-compose -f docker-compose.yml exec -T db \
    pg_dump -U "$POSTGRES_USER" "$POSTGRES_DB" | gzip > "$DB_SIN_CIFRAR"
printf '%s' "$BACKUP_ENCRYPTION_PASSPHRASE" | gpg --batch --yes --passphrase-fd 0 \
    --symmetric --cipher-algo AES256 -o "$DB_SIN_CIFRAR.gpg" "$DB_SIN_CIFRAR"
rm -f "$DB_SIN_CIFRAR"

echo "Respaldando media (paquetes de despliegue, imágenes de mantenimiento, etc.)..."
docker-compose -f docker-compose.yml exec -T web \
    tar czf - -C /app/media . > "$MEDIA_SIN_CIFRAR"
printf '%s' "$BACKUP_ENCRYPTION_PASSPHRASE" | gpg --batch --yes --passphrase-fd 0 \
    --symmetric --cipher-algo AES256 -o "$MEDIA_SIN_CIFRAR.gpg" "$MEDIA_SIN_CIFRAR"
rm -f "$MEDIA_SIN_CIFRAR"

echo "Retención: borrando respaldos locales de más de 14 días en $DESTINO..."
find "$DESTINO" -name "db_*.sql.gz.gpg" -mtime +14 -delete
find "$DESTINO" -name "media_*.tar.gz.gpg" -mtime +14 -delete

echo "Listo: $DB_SIN_CIFRAR.gpg y $MEDIA_SIN_CIFRAR.gpg"
