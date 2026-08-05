#!/bin/sh
# Siembra en EMQX los usuarios MQTT (panel, worker, agentes) y las ACLs por tópico.
# Correr UNA vez tras levantar el stack, desde el host: sh deploy/bootstrap-emqx.sh
# Requiere que deploy/.env esté completo. Usa la API HTTP de EMQX (dashboard).
set -e

. "$(dirname "$0")/.env"

# El dashboard de EMQX corre en el puerto 18083 DENTRO del contenedor, pero
# docker-compose.yml lo publica remapeado a 8082 en el host (rango 8080-8085
# abierto en firewall, ver el comentario junto a "dashboard" en ese archivo).
EMQX_API="${EMQX_API:-http://localhost:8082/api/v5}"

# La API de EMQX 5.x no acepta Basic Auth con el usuario del dashboard en estos
# endpoints; hay que loguearse primero y usar el token Bearer.
TOKEN=$(curl -s -X POST "$EMQX_API/login" -H "Content-Type: application/json" \
    -d "{\"username\":\"admin\",\"password\":\"${EMQX_DASHBOARD_PASSWORD}\"}" \
    | sed -n 's/.*"token":"\([^"]*\)".*/\1/p')
if [ -z "$TOKEN" ]; then
    echo "No se pudo obtener token del dashboard de EMQX (revisa EMQX_DASHBOARD_PASSWORD)." >&2
    exit 1
fi

crear_usuario() {
    user="$1"; pass="$2"
    echo "Creando usuario MQTT: $user"
    resp=$(curl -s -o /dev/null -w "%{http_code}" -X POST \
        "$EMQX_API/authentication/password_based:built_in_database/users" \
        -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
        -d "{\"user_id\":\"$user\",\"password\":\"$pass\"}")
    case "$resp" in
        200|201) ;;
        409) echo "  (ya existía)" ;;
        *) echo "  ERROR (HTTP $resp) creando $user" >&2 ;;
    esac
}

crear_usuario "$MQTT_USERNAME_PANEL"  "$MQTT_PASSWORD_PANEL"
crear_usuario "$MQTT_USERNAME_WORKER" "$MQTT_PASSWORD_WORKER"
crear_usuario "$MQTT_USERNAME_AGENTE" "$MQTT_PASSWORD_AGENTE"

echo
echo "Usuarios creados. Falta definir las ACLs por tópico desde el dashboard de EMQX"
echo "(http://<host>:18083), o vía API de authorization. Reglas recomendadas:"
echo "  - $MQTT_USERNAME_AGENTE : publish/subscribe solo bajo /saidsof/..."
echo "  - $MQTT_USERNAME_WORKER : subscribe a los tópicos de agentes; publish a respuestas"
echo "  - $MQTT_USERNAME_PANEL  : publish a /saidsof/despliegue/... y /saidsof/enrolamiento/..."
echo
echo "IMPORTANTE: en el agente .NET, MqttUsuario/MqttPassword deben ser las del agente,"
echo "y MqttUsarTls=true apuntando al puerto 8883 con el CA que firma deploy/certs/cert.pem."
