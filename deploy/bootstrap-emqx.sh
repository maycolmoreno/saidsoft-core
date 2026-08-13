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
# Nota: el usuario "agente" sigue siendo una credencial compartida, usada como
# "bootstrap" por cualquier estación que todavía no tenga su propia credencial MQTT.
# Desde apps.mqtt_worker.emqx_admin, Django aprovisiona una credencial propia por
# estación (username = codigo_estacion) en cada enrolamiento, vía la API Key que este
# script crea más abajo — pero eso requiere que el agente instalado en cada estación
# sepa usarla (el agente hoy es agente-prueba/agente_prueba.py, Python — ver
# PLAN_MODERNIZACION.md §10-K sobre por qué reemplazó al agente C# original), y ese
# rollout es gradual (no hay autoactualización del agente, ver
# apps/scripts/management/commands/seed_scripts_migracion_mqtt.py). Hasta que la flota
# completa haya migrado, esta credencial compartida debe seguir funcionando para TODO
# el tráfico, así que su regla se mantiene amplia (todo bajo /saidsof/). Angostarla es
# un paso manual aparte, ver deploy/emqx-narrow-acl-agente.sh — no lo hace este script.
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
        409)
            # Si ya existe, el POST de creación no actualiza nada — hay que hacer PUT
            # a /users/{user_id} para rotar la contraseña. Sin esto, correr este script
            # de nuevo después de cambiar una contraseña en .env deja a EMQX con la
            # vieja (encontrado rotando MQTT_PASSWORD_AGENTE/COMANDO_HMAC_SECRET tras
            # una exposición accidental en el repo, 11-ago-2026).
            resp_put=$(curl -s -o /dev/null -w "%{http_code}" -X PUT \
                "$EMQX_API/authentication/password_based:built_in_database/users/$user" \
                -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
                -d "{\"user_id\":\"$user\",\"password\":\"$pass\"}")
            case "$resp_put" in
                200|201|204) echo "  (ya existía, contraseña actualizada)" ;;
                *) echo "  ERROR (HTTP $resp_put) actualizando contraseña de $user" >&2 ;;
            esac
            ;;
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
    {"topic": "/saidsof/agente/+/windows_update/", "permission": "allow", "action": "subscribe"},
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
echo "Verificando API Key administrativa para apps.mqtt_worker.emqx_admin..."

API_KEY_NAME="saidsof_django_admin"

ya_existe=$(curl -s -X GET "$EMQX_API/api_key" -H "Authorization: Bearer $TOKEN" \
    | grep -c "\"name\":\"$API_KEY_NAME\"" || true)
if [ "$ya_existe" -gt 0 ]; then
    echo "  Ya existe una API Key \"$API_KEY_NAME\". EMQX no vuelve a mostrar el secret:"
    echo "  si se perdió (EMQX_API_KEY/EMQX_API_SECRET no están en deploy/.env), bórrala"
    echo "  primero (DELETE $EMQX_API/api_key/$API_KEY_NAME) y corre este script de nuevo."
else
    resp_body=$(mktemp)
    resp=$(curl -s -o "$resp_body" -w "%{http_code}" -X POST "$EMQX_API/api_key" \
        -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
        -d "{\"name\":\"$API_KEY_NAME\",\"enable\":true,\"expired_at\":\"infinity\",\"desc\":\"Aprovisionamiento de credenciales MQTT por estacion (apps.mqtt_worker.emqx_admin)\"}")
    case "$resp" in
        200|201)
            api_key=$(sed -n 's/.*"api_key":"\([^"]*\)".*/\1/p' "$resp_body")
            api_secret=$(sed -n 's/.*"api_secret":"\([^"]*\)".*/\1/p' "$resp_body")
            echo "  API Key creada. EMQX NO vuelve a mostrar el secret — pégalos AHORA en deploy/.env:"
            echo "    EMQX_API_URL=$EMQX_API"
            echo "    EMQX_API_KEY=$api_key"
            echo "    EMQX_API_SECRET=$api_secret"
            echo "  Mientras deploy/.env no tenga estas 3 variables, el aprovisionamiento por"
            echo "  estación queda desactivado sin romper nada (el enrolamiento sigue usando"
            echo "  solo la credencial compartida, como hasta ahora)."
            ;;
        *)
            echo "  ERROR (HTTP $resp) creando la API Key:" >&2
            cat "$resp_body" >&2
            ;;
    esac
    rm -f "$resp_body"
fi

echo
echo "IMPORTANTE: en config.json del agente (agente-prueba/servicio_windows.py), usuario/"
echo "password deben ser las del agente, y tls=true apuntando al puerto 8883 con el CA que"
echo "firma deploy/certs/cert.pem. (Corregido 13-ago-2026: esto decía \"agente .NET\" desde"
echo "antes de que ese agente se reemplazara por el de Python, ver PLAN_MODERNIZACION.md §10-K.)"
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
