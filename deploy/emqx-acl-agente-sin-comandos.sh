#!/bin/sh
# Le quita al usuario MQTT compartido ("agente") la capacidad de PUBLICAR en los tópicos
# servidor→agente, dejándole intacto todo lo que una estación legítima necesita.
#
# Por qué: hasta ahora ese usuario tenía ACL `/saidsof/#` allow-all. Una estación real
# nunca publica en los tópicos por los que el panel le manda trabajo — solo se suscribe.
# Pero cualquiera con esa credencial sí podía publicar ahí, y con el secreto HMAC firmar
# comandos válidos: ejecutar scripts o reiniciar cualquier estación de la flota.
# (4-sep-2026: ese par de secretos estuvo publicado sin autenticación en
# /media/agente-instalador/agente-instalador.zip — ver PLAN_MODERNIZACION.md §10.)
#
# Diferencia con emqx-narrow-acl-agente.sh: aquel deja la credencial compartida SOLO
# para enrolamiento, y por eso exige que TODA la flota haya migrado antes — si se corre
# antes de tiempo, deja mudas a las estaciones que aún dependen de ella. Este script es
# el paso intermedio, seguro de correr en cualquier momento: mata el vector de ataque
# (publicar comandos) sin cortarle el heartbeat ni la recepción de trabajo a nadie.
# Cuando la flota entera tenga credencial propia, correr igual el otro y terminar de
# cerrar.
#
# Uso: sh deploy/emqx-acl-agente-sin-comandos.sh
set -e

. "$(dirname "$0")/.env"

EMQX_API_LOCAL="${EMQX_API_LOCAL:-http://localhost:8082/api/v5}"

if [ -z "$EMQX_API_KEY" ] || [ -z "$EMQX_API_SECRET" ]; then
    echo "Faltan EMQX_API_KEY/EMQX_API_SECRET en deploy/.env." >&2
    exit 1
fi

# Las reglas se evalúan EN ORDEN y gana la primera que coincide, así que los deny van
# primero: el allow-all de abajo es lo que mantiene funcionando a las estaciones que
# todavía usan esta credencial (heartbeat, métricas, resultados y sus suscripciones).
REGLAS='[
    {"topic": "/saidsof/agente/+/comando/",           "permission": "deny",  "action": "publish"},
    {"topic": "/saidsof/agente/+/actualizar_agente/", "permission": "deny",  "action": "publish"},
    {"topic": "/saidsof/agente/+/software/",          "permission": "deny",  "action": "publish"},
    {"topic": "/saidsof/agente/+/despliegue/",        "permission": "deny",  "action": "publish"},
    {"topic": "/saidsof/enrolamiento/respuesta/+/",   "permission": "deny",  "action": "publish"},
    {"topic": "/saidsof/software/#",                  "permission": "deny",  "action": "publish"},
    {"topic": "/saidsof/despliegue/#",                "permission": "deny",  "action": "publish"},
    {"topic": "/saidsof/#",                           "permission": "allow", "action": "all"}
]'

echo "Aplicando ACL sin-comandos a $MQTT_USERNAME_AGENTE..."
resp=$(curl -s -o /tmp/acl_resp.json -w '%{http_code}' -X PUT \
    "$EMQX_API_LOCAL/authorization/sources/built_in_database/rules/users/$MQTT_USERNAME_AGENTE" \
    -u "$EMQX_API_KEY:$EMQX_API_SECRET" \
    -H "Content-Type: application/json" \
    -d "{\"username\": \"$MQTT_USERNAME_AGENTE\", \"rules\": $REGLAS}")

case "$resp" in
    200|201|204)
        echo "  Listo. La credencial compartida ya no puede publicar comandos a ninguna estación."
        ;;
    *)
        echo "  ERROR (HTTP $resp):" >&2
        cat /tmp/acl_resp.json >&2
        rm -f /tmp/acl_resp.json
        exit 1
        ;;
esac
rm -f /tmp/acl_resp.json

echo
echo "Reglas efectivas ahora:"
curl -s -X GET \
    "$EMQX_API_LOCAL/authorization/sources/built_in_database/rules/users/$MQTT_USERNAME_AGENTE" \
    -u "$EMQX_API_KEY:$EMQX_API_SECRET" \
    | python3 -c 'import sys,json; [print("  %-6s %-9s %s" % (r["permission"], r["action"], r["topic"])) for r in json.load(sys.stdin)["rules"]]'
