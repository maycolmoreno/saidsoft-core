#!/bin/sh
# Respaldo cifrado del keystore de firma de SAIDSOFT Campo.
#
# POR QUÉ EXISTE
#
# Android identifica una app por `applicationId` + FIRMA. Si se pierde este keystore o
# su contraseña, NO hay forma de publicar una actualización que los teléfonos ya
# instalados acepten: hay que desinstalar y reinstalar en cada equipo, perdiendo los
# datos locales de la app —cola offline incluida, o sea trabajo de campo que todavía no
# llegó al servidor—. Es el único punto de todo el proyecto que ningún parche posterior
# arregla.
#
# Hasta hoy existía en un solo disco. Esto no lo resuelve solo: produce un archivo
# cifrado y verificable, y MOVERLO fuera de esta máquina lo tenés que hacer vos. El
# script no copia nada a ningún lado a propósito — a dónde va una llave de firma es una
# decisión con consecuencias, no un parámetro por defecto.
#
# QUÉ GUARDA, Y POR QUÉ LOS DOS JUNTOS
#
# El keystore Y `key.properties` (que tiene su contraseña). Van en el mismo archivo para
# que una restauración no pueda fallar a medias: "tenemos el keystore pero nadie sabe la
# contraseña" deja exactamente igual de afuera que no tenerlo. El costo es que quien
# consiga el archivo Y su passphrase consigue todo, así que:
#
#   *** LA PASSPHRASE NO PUEDE VIVIR AL LADO DEL ARCHIVO. ***
#
# En un gestor de contraseñas, en otra cuenta, en otro sitio. Guardarla en la misma
# carpeta —o en el mismo disco que el archivo— convierte el cifrado en decoración.
#
# Mismo cifrado que los respaldos del servidor (GPG AES256 simétrico, ver
# deploy/backup.sh), por el mismo criterio: quien copie el archivo no puede leerlo.
#
# USO
#
#   ./respaldar-keystore.sh crear     <carpeta-destino>
#   ./respaldar-keystore.sh verificar <archivo.tar.gz.gpg>
#
# La passphrase se pide de forma interactiva. Para canalizarla desde un gestor de
# contrasenas, pasala por el descriptor 3:
#
#   ./respaldar-keystore.sh verificar copia.tar.gz.gpg 3< <(pass mostrar keystore-campo)
#
# `verificar` descifra en una carpeta temporal y comprueba que la huella sea la de
# producción. Es la mitad que se suele saltear: un respaldo que nunca se restauró no es
# un respaldo, es una suposición. Mismo criterio que deploy/restaurar-backup.sh, que se
# probó contra una base real antes de confiar en él.
set -eu
umask 077

# Huella del certificado de producción. Pública (está en el README), y acá cumple una
# función concreta: si el keystore en disco NO es este, respaldarlo sería peor que no
# hacer nada — crearía confianza en una copia equivocada.
HUELLA_ESPERADA="73:9A:43:B7:70:BE:7C:86:87:77:EA:5D:2A:7F:C1:C7:29:1F:7E:C0:7D:60:60:5B:33:09:E1:F3:E0:7F:0A:9C"

DIR="$(cd "$(dirname "$0")" && pwd)"
KEYSTORE="$DIR/cresio-campo-release.jks"
PROPIEDADES="$DIR/key.properties"

morir() {
    echo "ERROR: $1" >&2
    exit 1
}

# Argumentos de gpg para la passphrase.
#
# Por defecto la pide de forma interactiva, que es lo correcto para una persona. Si el
# descriptor 3 esta abierto, la lee de ahi: eso permite canalizarla desde un gestor de
# contrasenas (`... 3< <(pass mostrar keystore)`) y, sobre todo, permite PROBAR este
# script. Un verificador de respaldos que nunca se probo crea la misma falsa confianza
# que no tener respaldo -- que es justo lo que esto viene a corregir.
#
# La ruta interactiva no cambia: sin fd 3, gpg pregunta como siempre.
gpg_passphrase() {
    if { : >&3; } 2>/dev/null; then
        echo "--batch --pinentry-mode loopback --passphrase-fd 3"
    fi
}

buscar_keytool() {
    if command -v keytool >/dev/null 2>&1; then
        command -v keytool
        return
    fi
    if [ -n "${JAVA_HOME:-}" ] && [ -x "$JAVA_HOME/bin/keytool" ]; then
        echo "$JAVA_HOME/bin/keytool"
        return
    fi
    morir "no se encontró keytool. Viene con el JDK que ya usa Flutter; agregalo al PATH o definí JAVA_HOME."
}

# Huella SHA-256 del keystore que se le pase. La contraseña se lee de key.properties y
# viaja por STDIN, nunca por la línea de comandos: ahí quedaría visible en la lista de
# procesos y en el historial del shell.
huella_de() {
    keystore="$1"
    propiedades="$2"
    kt="$(buscar_keytool)"
    clave="$(grep -E '^storePassword=' "$propiedades" | cut -d= -f2-)"
    alias="$(grep -E '^keyAlias=' "$propiedades" | cut -d= -f2-)"
    [ -n "$clave" ] || morir "key.properties no tiene storePassword."
    [ -n "$alias" ] || morir "key.properties no tiene keyAlias."
    printf '%s\n' "$clave" \
        | "$kt" -list -v -keystore "$keystore" -alias "$alias" 2>/dev/null \
        | sed -n 's/.*SHA256: \([0-9A-F:]*\).*/\1/p' \
        | head -1
}

comprobar_huella() {
    huella="$1"
    origen="$2"
    [ -n "$huella" ] || morir "no se pudo leer la huella de $origen (¿contraseña equivocada en key.properties?)."
    if [ "$huella" != "$HUELLA_ESPERADA" ]; then
        echo "ERROR: la huella de $origen NO es la de produccion." >&2
        echo "  esperada: $HUELLA_ESPERADA" >&2
        echo "  leida:    $huella" >&2
        echo >&2
        echo "Un keystore distinto firma una app que Android trata como OTRA aplicacion:" >&2
        echo "no puede actualizar a las ya instaladas. Averigua que paso ANTES de seguir." >&2
        exit 1
    fi
}

crear() {
    destino="${1:-}"
    [ -n "$destino" ] || morir "falta la carpeta destino. Uso: $0 crear <carpeta-destino>"

    # Dentro del repo no: un archivo con la llave de firma a un `git add -A` de distancia
    # es justo lo que `.gitignore` viene evitando.
    repo="$(cd "$DIR/../.." && pwd)"
    absoluto="$(mkdir -p "$destino" && cd "$destino" && pwd)"
    case "$absoluto/" in
        "$repo"/*) morir "elegiste una carpeta DENTRO del repo ($absoluto). Usa una de afuera: el punto es que la copia no viva donde ya vive el original." ;;
    esac

    [ -f "$KEYSTORE" ] || morir "no existe $KEYSTORE."
    [ -f "$PROPIEDADES" ] || morir "no existe $PROPIEDADES."
    command -v gpg >/dev/null 2>&1 || morir "no se encontró gpg."

    echo "Verificando que el keystore sea el de produccion..."
    comprobar_huella "$(huella_de "$KEYSTORE" "$PROPIEDADES")" "el keystore en disco"
    echo "  OK: $HUELLA_ESPERADA"

    fecha="$(date +%Y%m%d_%H%M%S)"
    salida="$absoluto/keystore-cresio-campo_$fecha.tar.gz.gpg"

    echo
    echo "Se te va a pedir una passphrase (dos veces). Es la que hara falta para"
    echo "restaurar: guardala en un gestor de contrasenas, NO junto al archivo."
    echo

    tar -czf - -C "$DIR" cresio-campo-release.jks key.properties \
        | gpg $(gpg_passphrase) --symmetric --cipher-algo AES256 --output "$salida"

    echo
    echo "Listo: $salida"
    echo
    echo "FALTA LO QUE IMPORTA, y no lo puede hacer este script:"
    echo "  1. Copia ese archivo FUERA de esta maquina (otro disco, otra cuenta, otro sitio)."
    echo "  2. Guarda la passphrase en otro lado distinto del archivo."
    echo "  3. Corre la verificacion sobre la copia ya movida:"
    echo "        $0 verificar <ruta-de-la-copia>"
    echo
    echo "Mientras el archivo siga solo en este disco, el riesgo es el mismo que antes."
}

verificar() {
    archivo="${1:-}"
    [ -n "$archivo" ] || morir "falta el archivo. Uso: $0 verificar <archivo.tar.gz.gpg>"
    [ -f "$archivo" ] || morir "no existe $archivo."
    command -v gpg >/dev/null 2>&1 || morir "no se encontró gpg."

    temporal="$(mktemp -d)"
    # Se borra pase lo que pase: acá adentro queda el keystore en claro.
    trap 'rm -rf "$temporal"' EXIT INT TERM

    echo "Descifrando en una carpeta temporal..."
    gpg $(gpg_passphrase) --decrypt "$archivo" 2>/dev/null | tar -xzf - -C "$temporal" \
        || morir "no se pudo descifrar o desempaquetar (¿passphrase equivocada?)."

    [ -f "$temporal/cresio-campo-release.jks" ] || morir "el archivo no contiene el keystore."
    [ -f "$temporal/key.properties" ] || morir "el archivo no contiene key.properties."

    echo "Comprobando la huella de la copia..."
    comprobar_huella \
        "$(huella_de "$temporal/cresio-campo-release.jks" "$temporal/key.properties")" \
        "la copia de respaldo"

    echo
    echo "OK: esta copia es el keystore de produccion y se puede restaurar."
    echo "  $HUELLA_ESPERADA"
    echo
    echo "Para restaurar de verdad, copia los dos archivos a movil-campo/android/ y"
    echo "corre: flutter build apk --release --split-per-abi"
}

case "${1:-}" in
    crear) shift; crear "$@" ;;
    verificar) shift; verificar "$@" ;;
    *)
        echo "Uso:" >&2
        echo "  $0 crear     <carpeta-destino>        # produce el archivo cifrado" >&2
        echo "  $0 verificar <archivo.tar.gz.gpg>     # comprueba que se puede restaurar" >&2
        exit 2
        ;;
esac
