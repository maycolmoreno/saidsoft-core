#!/bin/sh
set -e

# Espera a que la base de datos acepte conexiones antes de migrar.
python - <<'PY'
import os, time, sys
import psycopg
url = os.environ.get("DATABASE_URL", "")
if url.startswith("postgres"):
    for intento in range(30):
        try:
            psycopg.connect(url, connect_timeout=3).close()
            print("Base de datos lista.")
            break
        except Exception as e:
            print(f"Esperando la base de datos ({intento+1}/30): {e}")
            time.sleep(2)
    else:
        sys.exit("La base de datos no respondió a tiempo.")
PY

# Solo el servicio web corre migraciones y collectstatic (el worker no debe).
if [ "$RUN_MIGRATIONS" = "1" ]; then
    echo "Aplicando migraciones..."
    python manage.py migrate --noinput
    echo "Recolectando estáticos..."
    python manage.py collectstatic --noinput
fi

exec "$@"
