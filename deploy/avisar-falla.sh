#!/bin/sh
# Avisa por Telegram que una unidad de systemd de SAIDSOFT falló.
# Lo invoca saidsoft-aviso@.service, que es el OnFailure= de saidsoft-salud.service y
# saidsoft-respaldo.service.
#
# Por qué existe (hallazgo S-14, 27-oct-2026): hasta hoy la verificación de salud solo
# escribía en el journal. El 25-oct-2026 el panel estuvo 7,5 horas en 502 y nadie se
# enteró. Este script corre en el HOST con curl, sin pasar por ningún contenedor: avisa
# aunque `web`, Celery o la base estén caídos.
#
# Lee de deploy/.env (sin ejecutarlo): TELEGRAM_BOT_TOKEN y el chat destino,
# SALUD_TELEGRAM_CHAT_ID si existe, si no ENLACES_TELEGRAM_CHAT_ID.
#
# Uso: sh deploy/avisar-falla.sh <unidad-que-fallo>
set -u

UNIDAD="${1:-desconocida}"
DIR="$(cd "$(dirname "$0")" && pwd)"
ENV="$DIR/.env"

leer() {
    # Toma la última definición de la variable; quita comillas simples o dobles.
    grep -E "^$1=" "$ENV" 2>/dev/null | tail -n 1 | cut -d= -f2- | sed -e "s/^['\"]//" -e "s/['\"]$//"
}

TOKEN="$(leer TELEGRAM_BOT_TOKEN)"
CHAT="$(leer SALUD_TELEGRAM_CHAT_ID)"
[ -n "$CHAT" ] || CHAT="$(leer ENLACES_TELEGRAM_CHAT_ID)"

if [ -z "$TOKEN" ] || [ -z "$CHAT" ]; then
    echo "avisar-falla: falta TELEGRAM_BOT_TOKEN o el chat en $ENV; no se pudo avisar de $UNIDAD." >&2
    exit 1
fi

# Las últimas líneas del journal de la unidad: lo que dijo verificar_salud o backup.sh.
DETALLE="$(journalctl -u "$UNIDAD" -n 12 --no-pager -o cat 2>/dev/null | tail -c 3000)"
TEXTO="⚠️ SAIDSOFT: falló $UNIDAD en $(hostname) ($(date '+%d/%m %H:%M'))

$DETALLE

Revisar: journalctl -u $UNIDAD --since today"

# El token va por stdin (-K -) y no en la línea de comandos: así no aparece en `ps`.
printf 'url = "https://api.telegram.org/bot%s/sendMessage"\n' "$TOKEN" \
    | curl -sS --max-time 20 -K - \
        --data-urlencode "chat_id=$CHAT" \
        --data-urlencode "text=$TEXTO" \
        -o /dev/null -w "avisar-falla: Telegram respondió HTTP %{http_code}\n"
