#!/bin/sh
# Genera un certificado self-signed para el listener TLS de EMQX (solo para pruebas /
# arranque). En producción, reemplazar cert.pem/key.pem por certificados reales de tu CA
# (o Let's Encrypt), y distribuir el CA a los agentes para que validen la conexión.
#
# Uso: sh generar-certs-dev.sh [SAN extra]
#   Sin argumento: SAN = emqx/localhost/127.0.0.1 (solo sirve para probar contra la
#   propia máquina — cualquier agente que conecte por la IP/hostname real del servidor
#   rechaza el cert por "certificate is not valid for '<ip>'", encontrado probando el
#   agente de prueba contra un despliegue real). Pasar la IP o dominio público del
#   servidor para que los agentes remotos puedan validar la conexión, ej.:
#     sh generar-certs-dev.sh IP:10.111.6.20
#     sh generar-certs-dev.sh DNS:mqtt.midominio.com
set -e
cd "$(dirname "$0")"

SAN="DNS:emqx,DNS:localhost,IP:127.0.0.1"
if [ -n "$1" ]; then
    SAN="$SAN,$1"
fi

openssl req -x509 -newkey rsa:2048 -nodes \
    -keyout key.pem -out cert.pem -days 825 \
    -subj "/CN=saidsof-mqtt" \
    -addext "subjectAltName=$SAN"

echo "Generados cert.pem y key.pem (self-signed). Reemplazar por certs reales en producción."
echo "SAN: $SAN"
