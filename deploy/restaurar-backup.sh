#!/bin/sh
# Restaura un backup de base de datos (deploy/backup.sh) contra una base de PRUEBA —
# nunca contra la base real sin que sea explícito y a propósito. Pensado tanto para
# validar que un backup puntual sirve (OPS-2, auditoría 22-ago-2026 — nunca se había
# probado una restauración real, solo `gzip -t`/integridad del archivo) como para una
# restauración de emergencia real, pasándole el nombre de la base real como segundo
# argumento a sabiendas de que la pisa.
#
# Uso: sh deploy/restaurar-backup.sh /ruta/a/db_AAAAMMDD_HHMMSS.sql.gz.gpg [nombre_bd_destino]
# Por default restaura contra "restore_test" (se borra y se recrea en cada corrida).
set -eu

ARCHIVO="${1:?Uso: sh restaurar-backup.sh /ruta/al/db_FECHA.sql.gz.gpg [nombre_bd_destino]}"
BD_DESTINO="${2:-restore_test}"

DIR="$(dirname "$0")"
cd "$DIR"
. ./.env

if [ -z "${BACKUP_ENCRYPTION_PASSPHRASE:-}" ]; then
    echo "ERROR: falta BACKUP_ENCRYPTION_PASSPHRASE en .env." >&2
    exit 1
fi
if [ ! -f "$ARCHIVO" ]; then
    echo "ERROR: no existe '$ARCHIVO'." >&2
    exit 1
fi
if [ "$BD_DESTINO" = "$POSTGRES_DB" ]; then
    echo "AVISO: vas a restaurar sobre '$POSTGRES_DB', la base REAL — se borra todo lo que tiene hoy." >&2
    printf 'Escribí exactamente "%s" para confirmar: ' "$POSTGRES_DB" >&2
    read -r confirmacion
    if [ "$confirmacion" != "$POSTGRES_DB" ]; then
        echo "Cancelado." >&2
        exit 1
    fi
fi

SQL_TMP="$(mktemp)"
trap 'rm -f "$SQL_TMP"' EXIT INT TERM

echo "Descifrando y descomprimiendo $ARCHIVO..."
printf '%s' "$BACKUP_ENCRYPTION_PASSPHRASE" | gpg --batch --yes --passphrase-fd 0 -d "$ARCHIVO" | gunzip > "$SQL_TMP"

echo "Recreando la base '$BD_DESTINO' en el contenedor db..."
docker-compose -f docker-compose.yml exec -T db psql -U "$POSTGRES_USER" -d postgres \
    -c "DROP DATABASE IF EXISTS \"$BD_DESTINO\";" \
    -c "CREATE DATABASE \"$BD_DESTINO\" OWNER $POSTGRES_USER;"

echo "Restaurando (esto puede tardar varios minutos con una base grande)..."
docker-compose -f docker-compose.yml exec -T db psql -U "$POSTGRES_USER" -d "$BD_DESTINO" < "$SQL_TMP"

echo ""
echo "Listo. Verificar cantidad de tablas y alguna fila real, por ejemplo:"
echo "  docker-compose exec db psql -U $POSTGRES_USER -d $BD_DESTINO -c '\\dt' | wc -l"
