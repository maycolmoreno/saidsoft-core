"""Convierte muestra_metrica en hypertable de TimescaleDB, si está disponible.

A escala de 1.800 equipos, esta tabla crece mucho; TimescaleDB la particiona por tiempo
y permite compresión/retención nativas. La conversión es un no-op en SQLite (desarrollo)
y en PostgreSQL sin la extensión — así la misma migración sirve en todos los entornos.
"""
from django.db import migrations


def crear_hypertable(apps, schema_editor):
    import logging
    logger = logging.getLogger(__name__)

    if schema_editor.connection.vendor != 'postgresql':
        return
    with schema_editor.connection.cursor() as cursor:
        cursor.execute("SELECT 1 FROM pg_extension WHERE extname = 'timescaledb'")
        if cursor.fetchone() is None:
            return  # PostgreSQL sin TimescaleDB: se queda como tabla normal
        try:
            # migrate_data mueve las filas existentes a chunks; if_not_exists lo hace idempotente.
            cursor.execute(
                "SELECT create_hypertable('muestra_metrica', 'timestamp', "
                "migrate_data => true, if_not_exists => true)"
            )
            logger.info('muestra_metrica convertida en hypertable de TimescaleDB.')
        except Exception as exc:
            # TimescaleDB exige que ningún índice único excluya la columna de tiempo; el PK
            # `id` de Django lo hace, así que la conversión puede rechazarse. No es fatal: la
            # tabla sigue funcionando como PostgreSQL normal. Para activar el hypertable a
            # escala real, ver deploy/README-produccion.md (ajuste del PK a compuesto).
            logger.warning('No se pudo convertir muestra_metrica en hypertable: %s', exc)


def revertir(apps, schema_editor):
    # No se puede "des-hipertabla" sin recrear la tabla; se deja como está.
    pass


class Migration(migrations.Migration):
    dependencies = [
        ('monitoreo', '0001_initial'),
    ]

    operations = [
        migrations.RunPython(crear_hypertable, revertir),
    ]
