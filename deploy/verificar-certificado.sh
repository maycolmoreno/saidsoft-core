#!/bin/sh
# ¿El certificado que sirve el servidor sigue siendo el que confía la app de campo?
#
# POR QUÉ EXISTE
#
# `deploy/certs/cert.pem` no lo usa una sola cosa. Lo consumen, hoy:
#
#   - nginx, para el panel en HTTPS (puerto 8084)
#   - EMQX, como certificado de su listener TLS (puerto 8883)
#   - el worker MQTT y las tres tareas de Celery, como CA (MQTT_CA_CERT)
#   - el instalador del agente (deploy/docs/instalar-agente.ps1), que lo copia a cada
#     estación
#   - la app de campo, que empaqueta UNA COPIA en movil-campo/assets/certs/cert.pem
#
# Los primeros cuatro se arreglan redesplegando. El quinto no: la copia vive DENTRO del
# APK que ya está instalado en los teléfonos. Si el servidor rota su certificado, la app
# deja de conectar con "el certificado del servidor no coincide" y la única salida es
# compilar y distribuir un APK nuevo, a mano, teléfono por teléfono.
#
# El problema que esto resuelve no es el de rotar: es que nadie se entera hasta que un
# técnico, parado en una farmacia, no puede cerrar un mantenimiento. Esto lo convierte
# en algo que se puede comprobar antes.
#
# USO
#
#   ./verificar-certificado.sh                    # contra el servidor por defecto
#   ./verificar-certificado.sh 10.111.6.20:8084
#   ./verificar-certificado.sh /ruta/a/cert.pem   # contra un certificado ya obtenido
#
# La forma con archivo sirve cuando el servidor no es alcanzable desde donde estás —el
# NUC vive en la red del sitio, no se llega desde cualquier máquina— y alguien te pasó
# el certificado. Es la misma comparación.
#
# Sale 0 si coinciden, 1 si no. Pensado para correrlo a mano después de cualquier cambio
# en el stack, y sobre todo ANTES de dar por terminada una rotación.
set -eu

ORIGEN="${1:-10.111.6.20:8084}"
DIR="$(cd "$(dirname "$0")" && pwd)"
CERT_APP="$DIR/../movil-campo/assets/certs/cert.pem"

# Margen con el que se avisa que el certificado está por vencer. 90 días es lo que tarda
# de verdad el camino completo: rotar, recompilar el APK, firmarlo y recorrer los
# teléfonos a mano. Enterarse la semana anterior no alcanza.
DIAS_AVISO=90

morir() {
    echo "ERROR: $1" >&2
    exit 1
}

command -v openssl >/dev/null 2>&1 || morir "no se encontró openssl."
[ -f "$CERT_APP" ] || morir "no se encontró el certificado de la app en $CERT_APP."

huella_de_archivo() {
    openssl x509 -in "$1" -noout -fingerprint -sha256 2>/dev/null | cut -d= -f2
}

if [ -f "$ORIGEN" ]; then
    echo "Origen:    $ORIGEN (archivo)"
else
    echo "Servidor:  $ORIGEN"
fi
echo "App:       movil-campo/assets/certs/cert.pem"
echo

HUELLA_APP="$(huella_de_archivo "$CERT_APP")"
[ -n "$HUELLA_APP" ] || morir "no se pudo leer el certificado de la app."

TEMPORAL="$(mktemp)"
trap 'rm -f "$TEMPORAL"' EXIT INT TERM

if [ -f "$ORIGEN" ]; then
    cp "$ORIGEN" "$TEMPORAL"
elif ! echo | openssl s_client -connect "$ORIGEN" 2>/dev/null | openssl x509 > "$TEMPORAL" 2>/dev/null; then
    morir "no se pudo obtener el certificado de $ORIGEN (¿está levantado? ¿hay ruta de red?)."
fi
HUELLA_SERVIDOR="$(huella_de_archivo "$TEMPORAL")"
[ -n "$HUELLA_SERVIDOR" ] || morir "lo que devolvió $ORIGEN no es un certificado legible."

# --- Vencimiento del que empaqueta la app ---------------------------------------
VENCE="$(openssl x509 -in "$CERT_APP" -noout -enddate | cut -d= -f2)"
if openssl x509 -in "$CERT_APP" -noout -checkend $((DIAS_AVISO * 86400)) >/dev/null 2>&1; then
    echo "Vence:     $VENCE  (más de $DIAS_AVISO días)"
else
    echo "Vence:     $VENCE" >&2
    echo >&2
    echo "AVISO: al certificado que empaqueta la app le quedan menos de $DIAS_AVISO dias." >&2
    echo "Cuando venza, las apps instaladas dejan de conectar aunque el servidor siga bien," >&2
    echo "y la unica salida es un APK nuevo distribuido a mano. Empeza el recambio ahora." >&2
fi
echo

# --- Lo que importa: ¿son el mismo? ---------------------------------------------
if [ "$HUELLA_APP" = "$HUELLA_SERVIDOR" ]; then
    echo "OK: el servidor sirve el mismo certificado que confia la app."
    echo "  $HUELLA_APP"
    exit 0
fi

echo "DESFASE: el servidor NO sirve el certificado que confia la app." >&2
echo "  app:      $HUELLA_APP" >&2
echo "  servidor: $HUELLA_SERVIDOR" >&2
echo >&2
echo "Las apps YA INSTALADAS no pueden conectar contra este servidor. Que hacer:" >&2
echo >&2
# En modo archivo, $ORIGEN es una ruta y no sirve para -connect: se muestra el
# servidor por defecto como referencia.
case "$ORIGEN" in
    *:*) REFERENCIA="$ORIGEN" ;;
    *) REFERENCIA="10.111.6.20:8084" ;;
esac
echo "  1. Traer el certificado del servidor al repo:" >&2
echo "       echo | openssl s_client -connect $REFERENCIA 2>/dev/null \\" >&2
echo "         | openssl x509 > movil-campo/assets/certs/cert.pem" >&2
echo "  2. Compilar y FIRMAR el APK (movil-campo/README.md):" >&2
echo "       flutter build apk --release --split-per-abi" >&2
echo "  3. Publicarlo y avisar (ver 'publicar_apk' y la rotacion en" >&2
echo "     deploy/README-produccion.md)." >&2
echo "  4. Volver a correr esto hasta que de OK." >&2
echo >&2
echo "Hasta el paso 3 inclusive, cada telefono sin actualizar esta fuera de servicio." >&2
exit 1
