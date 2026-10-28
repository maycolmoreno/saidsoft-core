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
docker compose -f docker-compose.yml exec -T db \
    pg_dump -U "$POSTGRES_USER" "$POSTGRES_DB" | gzip > "$DB_SIN_CIFRAR"
printf '%s' "$BACKUP_ENCRYPTION_PASSPHRASE" | gpg --batch --yes --passphrase-fd 0 \
    --symmetric --cipher-algo AES256 -o "$DB_SIN_CIFRAR.gpg" "$DB_SIN_CIFRAR"
rm -f "$DB_SIN_CIFRAR"

echo "Respaldando media (paquetes de despliegue, imágenes de mantenimiento, etc.)..."
docker compose -f docker-compose.yml exec -T web \
    tar czf - -C /app/media . > "$MEDIA_SIN_CIFRAR"
printf '%s' "$BACKUP_ENCRYPTION_PASSPHRASE" | gpg --batch --yes --passphrase-fd 0 \
    --symmetric --cipher-algo AES256 -o "$MEDIA_SIN_CIFRAR.gpg" "$MEDIA_SIN_CIFRAR"
rm -f "$MEDIA_SIN_CIFRAR"

# EMQX guarda en su propia base (Mnesia) los usuarios y las ACL, incluida la credencial
# MQTT de CADA estación, que se crea al enrolarla (apps.mqtt_worker.emqx_admin). Nada de
# eso está en la base de Django: si se pierde el disco, `bootstrap-emqx.sh` recrea el
# panel y el worker, pero las estaciones quedan sin poder conectarse hasta reinstalar el
# agente en cada una. Por eso este paso es parte del respaldo y no un extra: si falla,
# el script termina sin registrar el latido y el panel lo marca.
echo "Respaldando EMQX (usuarios, credenciales de las estaciones y ACL)..."
EMQX_SIN_CIFRAR="$DESTINO/emqx_$FECHA.tar.gz"
EXPORT_EMQX=$(docker compose -f docker-compose.yml exec -T emqx emqx ctl data export \
    | grep -o 'emqx-export-[^" ]*\.tar\.gz' | tail -1 || true)
if [ -z "$EXPORT_EMQX" ]; then
    # Si el formato del mensaje cambia en otra versión de EMQX, el export igual se hizo:
    # se toma el más reciente de la carpeta (nombres `emqx-export-AAAA-MM-DD-...`).
    EXPORT_EMQX=$(docker compose -f docker-compose.yml exec -T emqx \
        sh -c 'ls -t /opt/emqx/data/backup/ | grep "^emqx-export-.*\.tar\.gz$" | head -1' || true)
fi
if [ -z "$EXPORT_EMQX" ]; then
    echo "ERROR: 'emqx ctl data export' no dejó ningún archivo en /opt/emqx/data/backup/." >&2
    exit 1
fi
docker compose -f docker-compose.yml exec -T emqx \
    cat "/opt/emqx/data/backup/$EXPORT_EMQX" > "$EMQX_SIN_CIFRAR"
# El export queda dentro del volumen de EMQX: se borra para que no se acumule uno por día.
docker compose -f docker-compose.yml exec -T emqx rm -f "/opt/emqx/data/backup/$EXPORT_EMQX"
printf '%s' "$BACKUP_ENCRYPTION_PASSPHRASE" | gpg --batch --yes --passphrase-fd 0 \
    --symmetric --cipher-algo AES256 -o "$EMQX_SIN_CIFRAR.gpg" "$EMQX_SIN_CIFRAR"
rm -f "$EMQX_SIN_CIFRAR"

echo "Retención: borrando respaldos locales de más de 14 días en $DESTINO..."
find "$DESTINO" -name "db_*.sql.gz.gpg" -mtime +14 -delete
find "$DESTINO" -name "media_*.tar.gz.gpg" -mtime +14 -delete
find "$DESTINO" -name "emqx_*.tar.gz.gpg" -mtime +14 -delete

echo "Listo: $DB_SIN_CIFRAR.gpg, $MEDIA_SIN_CIFRAR.gpg y $EMQX_SIN_CIFRAR.gpg"

# Latido en el panel. Va DESPUÉS de que los dos archivos existen y no antes: la fila
# responde "¿cuándo funcionó por última vez?", y escribirla al empezar mentiría justo
# en el caso que importa. `|| true` porque un respaldo bueno no debe reportarse como
# fallido solo porque el panel no atendió.
echo "Registrando el latido del respaldo en el panel..."
docker compose -f docker-compose.yml exec -T web     python manage.py registrar_latido respaldo ||     echo "AVISO: el respaldo quedó bien pero no se pudo registrar el latido en el panel." >&2

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
    if rsync -az --partial --timeout=120             -e "ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new"             "$DESTINO"/db_*.sql.gz.gpg "$DESTINO"/media_*.tar.gz.gpg "$DESTINO"/emqx_*.tar.gz.gpg             "$BACKUP_OFFSITE_DESTINO"; then
        echo "Copia externa completada."
    else
        # No se propaga el error: el respaldo local YA está hecho y es válido. Que el
        # destino remoto esté caído no debe hacer fracasar la unidad de systemd ni
        # perder el latido que ya se registró.
        echo "ERROR: falló la copia a $BACKUP_OFFSITE_DESTINO — el respaldo local sí quedó bien." >&2
    fi
fi

# Copia a OneDrive con rclone. BACKUP_ONEDRIVE_REMOTO es "remoto:carpeta" de rclone (ej.
# "onedrive:SaidSoft/respaldos"); el remoto se configura una vez con `rclone config`
# como glpi (ver deploy/README-produccion.md). Lo que sube ya está cifrado con GPG: en
# OneDrive no se puede leer sin la passphrase, que NO se sube.
#
# Qué sube y cada cuánto:
#   - base y EMQX, todos los días: son chicos y es lo que cambia;
#   - media (~2 GB), solo si en OneDrive no hay una de los últimos 7 días: casi no cambia
#     de un día a otro y subirla a diario es 60 GB al mes para lo mismo. Mirarlo en
#     OneDrive y no por día de la semana hace que una subida fallida se reintente al día
#     siguiente en vez de esperar una semana.
#
# Retención: BACKUP_ONEDRIVE_RETENCION_DIAS (30 por defecto), y solo se borra después de
# una subida completa: si OneDrive lleva días fallando, no se vacía lo que ya hay allá.
#
# Como el rsync de arriba, un fallo acá no hace fracasar el respaldo local. Lo que lo
# hace visible es el latido `respaldo_externo`, que solo se registra si todo subió: el
# panel lo marca y `verificar_salud` lo informa cuando se atrasa.
if [ -z "${BACKUP_ONEDRIVE_REMOTO:-}" ]; then
    echo "AVISO: BACKUP_ONEDRIVE_REMOTO no está definido en .env — no se copia a OneDrive." >&2
elif ! command -v rclone >/dev/null 2>&1; then
    echo "ERROR: BACKUP_ONEDRIVE_REMOTO está definido pero rclone no está instalado." >&2
else
    REMOTO="$BACKUP_ONEDRIVE_REMOTO"
    RETENCION="${BACKUP_ONEDRIVE_RETENCION_DIAS:-30}"
    A_SUBIR="$DB_SIN_CIFRAR.gpg $EMQX_SIN_CIFRAR.gpg"
    MEDIA_RECIENTE=$(rclone lsf "$REMOTO" --files-only --include 'media_*.tar.gz.gpg' --max-age 7d 2>/dev/null || true)
    if [ -z "$MEDIA_RECIENTE" ]; then
        A_SUBIR="$A_SUBIR $MEDIA_SIN_CIFRAR.gpg"
    fi

    SUBIO_TODO=1
    for ARCHIVO in $A_SUBIR; do
        echo "Subiendo $(basename "$ARCHIVO") a $REMOTO..."
        if ! rclone copy "$ARCHIVO" "$REMOTO" --retries 3 --low-level-retries 10 --stats-one-line; then
            echo "ERROR: no se pudo subir $(basename "$ARCHIVO") a OneDrive." >&2
            SUBIO_TODO=0
        fi
    done

    if [ "$SUBIO_TODO" = 1 ]; then
        echo "Retención en OneDrive: borrando respaldos de más de $RETENCION días..."
        rclone delete "$REMOTO" --min-age "${RETENCION}d" \
            --include 'db_*.sql.gz.gpg' --include 'media_*.tar.gz.gpg' --include 'emqx_*.tar.gz.gpg' \
            || echo "AVISO: no se pudo aplicar la retención en OneDrive (la copia de hoy sí quedó)." >&2
        docker compose -f docker-compose.yml exec -T web python manage.py registrar_latido respaldo_externo \
            || echo "AVISO: la copia a OneDrive quedó bien pero no se pudo registrar su latido." >&2
        echo "Copia a OneDrive completada."
    else
        echo "ERROR: la copia a OneDrive quedó incompleta — el respaldo local sí quedó bien." >&2
    fi
fi
