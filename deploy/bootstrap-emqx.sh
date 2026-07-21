#!/bin/sh
# Siembra en EMQX los usuarios MQTT (panel, worker, agentes) y las ACLs por tópico.
# Correr UNA vez tras levantar el stack, desde el host: sh deploy/bootstrap-emqx.sh
# Requiere que deploy/.env esté completo. Usa la API HTTP de EMQX (dashboard).
set -e

. "$(dirname "$0")/.env"

EMQX_API="${EMQX_API:-http://localhost:18083/api/v5}"
AUTH="admin:${EMQX_DASHBOARD_PASSWORD}"

crear_usuario() {
    user="$1"; pass="$2"
    echo "Creando usuario MQTT: $user"
    curl -s -u "$AUTH" -X POST "$EMQX_API/authentication/password_based:built_in_database/users" \
        -H "Content-Type: application/json" \
        -d "{\"user_id\":\"$user\",\"password\":\"$pass\"}" >/dev/null || true
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
