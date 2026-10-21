#!/bin/sh
# EMQX de LABORATORIO para las pruebas de integración de la cola de órdenes
# (apps/catalogo/tests_integracion_emqx.py).
#
# - Misma versión y misma autenticación/autorización que producción
#   (deploy/docker-compose.yml): built_in_database, no_match=deny, retainer en disco.
# - Solo en loopback (127.0.0.1), sin TLS, con un volumen propio y desechable.
# - NO usa credenciales ni datos de producción, y no se conecta a ninguna estación real:
#   los usuarios y las ACL los crea la propia prueba con claves de laboratorio.
#
# Uso:
#   sh deploy/lab/emqx-cola-lab.sh arrancar   # crea y espera a que responda
#   sh deploy/lab/emqx-cola-lab.sh reiniciar  # docker restart (prueba de persistencia)
#   sh deploy/lab/emqx-cola-lab.sh detener    # borra el contenedor y su volumen de lab
#
# Después de arrancar, correr las pruebas con estas variables (las imprime el script):
#   EMQX_LAB_HOST, EMQX_LAB_PUERTO, EMQX_LAB_API, EMQX_LAB_ADMIN_PASSWORD, EMQX_LAB_CONTENEDOR
set -eu

NOMBRE=saidsoft-lab-emqx
VOLUMEN=saidsoft-lab-emqx-data
PUERTO_MQTT=21883
PUERTO_API=28083
# Clave del dashboard DEL LABORATORIO (loopback, desechable). No es la de producción.
CLAVE_ADMIN=saidsoft-lab-admin-1

esperar() {
    i=0
    until docker exec "$NOMBRE" emqx ctl status >/dev/null 2>&1; do
        i=$((i + 1))
        if [ "$i" -gt 60 ]; then
            echo "EMQX de laboratorio no respondió en 60 s." >&2
            exit 1
        fi
        sleep 1
    done
}

case "${1:-}" in
    arrancar)
        docker run -d --name "$NOMBRE" --hostname emqx \
            -e EMQX_NODE__NAME=emqx@127.0.0.1 \
            -e EMQX_NODE__COOKIE=saidsoft-lab \
            -e EMQX_DASHBOARD__DEFAULT_PASSWORD="$CLAVE_ADMIN" \
            -e EMQX_AUTHENTICATION__1__MECHANISM=password_based \
            -e EMQX_AUTHENTICATION__1__BACKEND=built_in_database \
            -e EMQX_AUTHENTICATION__1__ENABLE=true \
            -e EMQX_AUTHORIZATION__SOURCES__1__TYPE=built_in_database \
            -e EMQX_AUTHORIZATION__SOURCES__1__ENABLE=true \
            -e EMQX_AUTHORIZATION__NO_MATCH=deny \
            -e EMQX_RETAINER__BACKEND__STORAGE_TYPE=disc \
            -e EMQX_LISTENERS__WSS__DEFAULT__ENABLE=false \
            -v "$VOLUMEN":/opt/emqx/data \
            -p "127.0.0.1:$PUERTO_MQTT:1883" -p "127.0.0.1:$PUERTO_API:18083" \
            emqx/emqx:5.8.3 >/dev/null
        esperar
        echo "EMQX de laboratorio listo. Variables para las pruebas:"
        echo "  EMQX_LAB_HOST=127.0.0.1 EMQX_LAB_PUERTO=$PUERTO_MQTT EMQX_LAB_API=http://127.0.0.1:$PUERTO_API/api/v5 \\"
        echo "  EMQX_LAB_ADMIN_PASSWORD=$CLAVE_ADMIN EMQX_LAB_CONTENEDOR=$NOMBRE"
        ;;
    reiniciar)
        docker restart "$NOMBRE" >/dev/null
        esperar
        ;;
    detener)
        docker rm -f "$NOMBRE" >/dev/null 2>&1 || true
        docker volume rm "$VOLUMEN" >/dev/null 2>&1 || true
        echo "Laboratorio eliminado (contenedor y volumen $VOLUMEN)."
        ;;
    *)
        echo "Uso: sh deploy/lab/emqx-cola-lab.sh arrancar|reiniciar|detener" >&2
        exit 2
        ;;
esac
