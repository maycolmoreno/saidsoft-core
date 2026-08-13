#!/bin/sh
# NO CORRER hasta confirmar que TODA la flota de estaciones ya migró a una credencial
# MQTT propia (ver apps.mqtt_worker.emqx_admin y PLAN_MODERNIZACION.md §9).
#
# Angosta la ACL del usuario compartido "agente" de "/saidsof/#" (todo, usado hoy como
# credencial de bootstrap para estaciones que aún no tienen credencial propia) a solo
# los tópicos de enrolamiento. Correrlo antes de tiempo corta el heartbeat/tráfico de
# CUALQUIER estación que todavía dependa de la credencial compartida — en la práctica,
# toda la flota que no haya pasado por el script de reprovisionamiento
# (apps/scripts/management/commands/seed_scripts_migracion_mqtt.py) con el agente nuevo
# ya instalado.
#
# Cómo confirmar que es seguro correrlo: en el panel, ninguna estación debería seguir
# conectándose con el client_id "agente-*" bajo el usuario MQTT_USERNAME_AGENTE — EMQX
# expone las sesiones activas por usuario en GET /api/v5/clients?username=<agente>.
#
# Uso: sh deploy/emqx-narrow-acl-agente.sh
set -e

. "$(dirname "$0")/.env"

EMQX_API="${EMQX_API:-http://localhost:8082/api/v5}"

TOKEN=$(curl -s -X POST "$EMQX_API/login" -H "Content-Type: application/json" \
    -d "{\"username\":\"admin\",\"password\":\"${EMQX_DASHBOARD_PASSWORD}\"}" \
    | sed -n 's/.*"token":"\([^"]*\)".*/\1/p')
if [ -z "$TOKEN" ]; then
    echo "No se pudo obtener token del dashboard de EMQX (revisa EMQX_DASHBOARD_PASSWORD)." >&2
    exit 1
fi

echo "Estaciones actualmente conectadas con la credencial compartida ($MQTT_USERNAME_AGENTE):"
curl -s -X GET "$EMQX_API/clients?username=$MQTT_USERNAME_AGENTE&limit=1" \
    -H "Authorization: Bearer $TOKEN" | sed -n 's/.*"count":\([0-9]*\).*/  \1 cliente(s) conectado(s) ahora mismo./p'
echo "(esto es solo un snapshot de conexiones activas en este momento, no confirma que"
echo "todas las estaciones ya migraron — una estación offline no aparece aquí aunque"
echo "siga configurada con la credencial compartida)"
echo
printf "¿Confirmás que ya se migró TODA la flota y querés angostar la ACL? (escribir 'si'): "
read -r confirmacion
if [ "$confirmacion" != "si" ]; then
    echo "Cancelado."
    exit 0
fi

resp_body=$(mktemp)
resp=$(curl -s -o "$resp_body" -w "%{http_code}" -X PUT \
    "$EMQX_API/authorization/sources/built_in_database/rules/users/$MQTT_USERNAME_AGENTE" \
    -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
    -d "{\"username\": \"$MQTT_USERNAME_AGENTE\", \"rules\": [
        {\"topic\": \"/saidsof/enrolamiento/solicitar/\", \"permission\": \"allow\", \"action\": \"publish\"},
        {\"topic\": \"/saidsof/enrolamiento/respuesta/+/\", \"permission\": \"allow\", \"action\": \"subscribe\"}
    ]}")
case "$resp" in
    200|201|204) echo "ACL de $MQTT_USERNAME_AGENTE angostada a solo enrolamiento." ;;
    *)
        echo "ERROR (HTTP $resp) angostando la ACL:" >&2
        cat "$resp_body" >&2
        exit 1
        ;;
esac
rm -f "$resp_body"
