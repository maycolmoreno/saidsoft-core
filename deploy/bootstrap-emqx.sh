#!/bin/sh
# Siembra en EMQX los usuarios MQTT (panel, worker, agentes) y las ACLs por tópico.
# Correr UNA vez tras levantar el stack, desde el host: sh deploy/bootstrap-emqx.sh
# Requiere que deploy/.env esté completo. Usa la API HTTP de EMQX (dashboard).
#
# docker-compose.yml fija EMQX_AUTHORIZATION__NO_MATCH=deny — sin las reglas que
# siembra este script, ningún cliente podría publicar/suscribirse a nada (antes de
# esto, el comportamiento por defecto de EMQX sin NO_MATCH=deny era permitir todo a
# cualquier cliente autenticado, así que las 3 credenciales podían leer/escribir
# cualquier tópico, no solo los suyos).
#
# Nota: el usuario "agente" es una sola credencial compartida por las ~1.800
# estaciones (el agente deriva su código de tópico del hostname, no de una identidad
# MQTT propia) — su regla es amplia (todo bajo /saidsof/) porque EMQX no tiene forma
# de restringirlo a "solo su propia estación" sin credenciales por agente o un
# prefijo de tópico por unidad de negocio. Ver PLAN_MODERNIZACION.md §9 ("ACLs MQTT/
# EMQX por tenant") — este script no resuelve ese gap, solo cierra el más grave
# (cualquiera podía tocar cualquier tópico, no solo los de agentes).
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
echo "Definiendo ACLs por tópico..."

# PUT a .../rules/users/{username} (no el POST bulk a .../rules/users): el bulk POST
# es de importación única — si el usuario ya tiene reglas cargadas (ej. correr este
# script una segunda vez para agregar tópicos nuevos, como pasó al sumar el catálogo
# de software) devuelve 409 ALREADY_EXISTS y no actualiza nada. El PUT por-usuario es
# un "set" real (crea si no existe, reemplaza si ya existía) — encontrado corriendo
# este script una segunda vez contra una instancia EMQX real, 10-ago-2026.
definir_acl() {
    user="$1"; reglas_json="$2"
    resp_body=$(mktemp)
    # El body necesita "username" además de las "rules" — no alcanza con que vaya en
    # la URL; sin él, EMQX responde 400 BAD_REQUEST "required_field: root.username"
    # (encontrado corriendo esto contra un EMQX real, 10-ago-2026).
    resp=$(curl -s -o "$resp_body" -w "%{http_code}" -X PUT \
        "$EMQX_API/authorization/sources/built_in_database/rules/users/$user" \
        -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
        -d "{\"username\": \"$user\", \"rules\": $reglas_json}")
    case "$resp" in
        200|201|204) echo "  ACLs de $user definidas." ;;
        *)
            echo "  ERROR (HTTP $resp) definiendo ACLs de $user:" >&2
            cat "$resp_body" >&2
            rm -f "$resp_body"
            exit 1
            ;;
    esac
    rm -f "$resp_body"
}

definir_acl "$MQTT_USERNAME_AGENTE" '[
    {"topic": "/saidsof/#", "permission": "allow", "action": "all"}
]'

definir_acl "$MQTT_USERNAME_WORKER" '[
    {"topic": "/saidsof/enrolamiento/solicitar/", "permission": "allow", "action": "subscribe"},
    {"topic": "/saidsof/agente/+/heartbeat/", "permission": "allow", "action": "subscribe"},
    {"topic": "/saidsof/agente/+/despliegue_estado/", "permission": "allow", "action": "subscribe"},
    {"topic": "/saidsof/agente/+/software_estado/", "permission": "allow", "action": "subscribe"},
    {"topic": "/saidsof/agente/+/metricas/", "permission": "allow", "action": "subscribe"},
    {"topic": "/saidsof/agente/+/info_equipo/", "permission": "allow", "action": "subscribe"},
    {"topic": "/saidsof/agente/+/script_estado/", "permission": "allow", "action": "subscribe"},
    {"topic": "/saidsof/enrolamiento/respuesta/+/", "permission": "allow", "action": "publish"}
]'

definir_acl "$MQTT_USERNAME_PANEL" '[
    {"topic": "/saidsof/despliegue/global/", "permission": "allow", "action": "publish"},
    {"topic": "/saidsof/despliegue/grupo/+/", "permission": "allow", "action": "publish"},
    {"topic": "/saidsof/despliegue/farmacia/+/", "permission": "allow", "action": "publish"},
    {"topic": "/saidsof/agente/+/despliegue/", "permission": "allow", "action": "publish"},
    {"topic": "/saidsof/software/global/", "permission": "allow", "action": "publish"},
    {"topic": "/saidsof/software/grupo/+/", "permission": "allow", "action": "publish"},
    {"topic": "/saidsof/software/farmacia/+/", "permission": "allow", "action": "publish"},
    {"topic": "/saidsof/agente/+/software/", "permission": "allow", "action": "publish"},
    {"topic": "/saidsof/agente/+/comando/", "permission": "allow", "action": "publish"}
]'

echo
echo "IMPORTANTE: en el agente .NET, MqttUsuario/MqttPassword deben ser las del agente,"
echo "y MqttUsarTls=true apuntando al puerto 8883 con el CA que firma deploy/certs/cert.pem."
echo
echo "Verificado contra una instancia EMQX real (31-jul-2026): las 3 reglas quedan"
echo "confirmadas vía la API (GET .../authorization/sources/built_in_database/rules/users)"
echo "y una prueba de publish/subscribe con paho-mqtt mostró que un tópico permitido"
echo "entrega el mensaje y uno sin regla nunca llega al suscriptor (aunque el publicador"
echo "reciba el PUBACK igual — eso solo confirma recepción del paquete, no autorización)."
echo "Requiere que docker-compose.yml declare built_in_database como fuente de"
echo "autorización; sin eso EMQX usa por defecto una fuente 'file' que termina en"
echo "{allow, all} y el no_match=deny nunca se alcanza. Sigue sin resolver la"
echo "segmentación por tenant (la credencial del agente es compartida por las ~1.800"
echo "estaciones) — ver PLAN_MODERNIZACION.md §9."
