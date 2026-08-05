#!/bin/sh
# Genera un certificado self-signed para el listener TLS de EMQX (solo para pruebas /
# arranque). En producción, reemplazar cert.pem/key.pem por certificados reales de tu CA
# (o Let's Encrypt), y distribuir el CA a los agentes para que validen la conexión.
set -e
cd "$(dirname "$0")"

openssl req -x509 -newkey rsa:2048 -nodes \
    -keyout key.pem -out cert.pem -days 825 \
    -subj "/CN=saidsof-mqtt" \
    -addext "subjectAltName=DNS:emqx,DNS:localhost,IP:127.0.0.1"

echo "Generados cert.pem y key.pem (self-signed). Reemplazar por certs reales en producción."
