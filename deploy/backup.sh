#!/bin/sh
# Respaldo de la base de datos y de media (paquetes de despliegue, imágenes de
# mantenimiento, etc.) del stack de producción.
#
# A diferencia de deploy/scheduler.sh (tareas de la aplicación, corren dentro del
# stack), esto es una tarea de infraestructura que necesita orquestar `docker compose`
# desde afuera — pensado para el HOST, no para un servicio del stack.
#
# Lo dispara un TIMER DE SYSTEMD, no cron (ver deploy/saidsoft-respaldo.timer y
# deploy/README-produccion.md). El cambio no es cosmético: el 6 y el 7-sep-2026 no hubo
# respaldo porque el NUC estuvo apagado todo el fin de semana y el `0 2 * * *` de cron
# simplemente no existió a esa hora — cron no tiene memoria de lo que se perdió. El
# timer usa `Persistent=true`, que corre la ejecución atrasada en el próximo arranque.
#
# OPS-2 (auditoría 22-ago-2026): los .sql.gz/.tar.gz quedan cifrados con GPG (AES256
# simétrico, BACKUP_ENCRYPTION_PASSPHRASE en .env) — sin la passphrase, nadie que
# copie estos archivos (ni siquiera desde un backup robado/filtrado, o el destino
# offsite que todavía no existe) puede leer la base ni los paquetes de despliegue. Ver
# deploy/restaurar-backup.sh para el procedimiento de restauración (ya probado contra
# una base real, ver PLAN_MODERNIZACION.md OPS-2).
#
# La copia fuera del servidor se hace al final, con rsync sobre SSH, SI
# BACKUP_OFFSITE_DESTINO está definido en .env. Sin esa variable el script avisa y sigue
# — el respaldo local nunca depende de que el destino remoto exista o responda.
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

# Latido en el panel. Va DESPUÉS de que los dos archivos existen y no antes: la fila
# responde "¿cuándo funcionó por última vez?", y escribirla al empezar mentiría justo
# en el caso que importa. `|| true` porque un respaldo bueno no debe reportarse como
# fallido solo porque el panel no atendió.
echo "Registrando el latido del respaldo en el panel..."
docker-compose -f docker-compose.yml exec -T web     python manage.py registrar_latido respaldo ||     echo "AVISO: el respaldo quedó bien pero no se pudo registrar el latido en el panel." >&2

# Copia fuera del servidor. Un respaldo en el mismo disco que respalda no protege del
# caso que más caro sale: el NUC está en sitio, sin UPS, y ya se apagó por corte de
# energía tres veces en septiembre.
#
# IMPORTANTE: la passphrase de GPG NO viaja al destino. Si se pierde el servidor se
# pierden la base y la llave a la vez, así que BACKUP_ENCRYPTION_PASSPHRASE tiene que
# estar guardada aparte (gestor de contraseñas), no solo en este .env.
if [ -z "${BACKUP_OFFSITE_DESTINO:-}" ]; then
    echo "AVISO: BACKUP_OFFSITE_DESTINO no está definido en .env — el respaldo queda SOLO en este servidor." >&2
else
    echo "Copiando a $BACKUP_OFFSITE_DESTINO..."
    # --partial para que un corte de red no obligue a retransmitir el .tar.gz entero
    # (media pasa de 1 GB). Sin --delete: la retención del destino se decide allá, no
    # acá; borrar en remoto lo que la retención local ya limpió anularía el sentido de
    # tener una copia externa.
    if rsync -az --partial --timeout=120             -e "ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new"             "$DESTINO"/db_*.sql.gz.gpg "$DESTINO"/media_*.tar.gz.gpg             "$BACKUP_OFFSITE_DESTINO"; then
        echo "Copia externa completada."
    else
        # No se propaga el error: el respaldo local YA está hecho y es válido. Que el
        # destino remoto esté caído no debe hacer fracasar la unidad de systemd ni
        # perder el latido que ya se registró.
        echo "ERROR: falló la copia a $BACKUP_OFFSITE_DESTINO — el respaldo local sí quedó bien." >&2
    fi
fi
