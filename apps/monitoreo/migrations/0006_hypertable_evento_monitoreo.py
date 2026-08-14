"""Convierte evento_monitoreo en hypertable de TimescaleDB, si está disponible.

Mismo criterio que 0002_hypertable_timescaledb.py para muestra_metrica: no-op en SQLite
y en PostgreSQL sin la extensión, así la misma migración sirve en todos los entornos.
"""
from django.db import migrations, transaction


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
            with transaction.atomic(using=schema_editor.connection.alias):
                cursor.execute(
                    "SELECT create_hypertable('evento_monitoreo', 'timestamp', "
                    "migrate_data => true, if_not_exists => true)"
                )
            logger.info('evento_monitoreo convertida en hypertable de TimescaleDB.')
        except Exception as exc:
            # Mismo hueco documentado para muestra_metrica: el PK `id` de Django excluye
            # la columna de tiempo, TimescaleDB puede rechazar la conversión. No es fatal.
            logger.warning('No se pudo convertir evento_monitoreo en hypertable: %s', exc)


def revertir(apps, schema_editor):
    pass


class Migration(migrations.Migration):
    dependencies = [
        ('monitoreo', '0005_alter_reglaalerta_metrica_alter_reglaalerta_operador_and_more'),
    ]

    operations = [
        migrations.RunPython(crear_hypertable, revertir),
    ]
