# Imagen del app Django (la usan tanto el servicio web como el worker MQTT).
# Python 3.13 (estable en el ecosistema Docker; Django 5.2 soporta 3.10–3.14).
FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DJANGO_SETTINGS_MODULE=config.settings.produccion

WORKDIR /app

# Dependencias del sistema para psycopg (binario) — mínimas.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libpq5 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Usuario no-root
RUN useradd --create-home appuser && chown -R appuser:appuser /app
USER appuser

COPY --chown=appuser:appuser deploy/entrypoint.sh /entrypoint.sh
# Defensivo: el bit de ejecución se pierde fácil al editar en Windows (ver
# .gitattributes) — asegurarlo acá también para que el build nunca dependa de
# que el checkout lo haya preservado bien. chown ya deja a appuser como dueño,
# así que no hace falta volver a root para esto.
RUN chmod +x /entrypoint.sh

EXPOSE 8000
ENTRYPOINT ["/entrypoint.sh"]
# Por defecto arranca el servidor web; el worker sobreescribe el comando en compose.
CMD ["gunicorn", "config.wsgi:application", "--bind", "0.0.0.0:8000", "--workers", "3", "--timeout", "120"]
