"""Convierte muestra_red_farmacia en hypertable de TimescaleDB, si está disponible.

Llega tarde a propósito de nadie: `muestra_metrica` (0002) y `evento_monitoreo` (0006)
se convirtieron desde el principio y esta tabla quedó afuera, sin hypertable y sin purga.
Hoy pesa poco porque solo 4 Mikrotiks responden SNMP, pero se escribe una fila por
farmacia cada 5 minutos — con las 700 respondiendo serían ~6 millones de filas por mes,
la tabla más grande del sistema. Convertirla mientras está chica es gratis; hacerlo con
decenas de millones de filas ya no.

Mismo criterio que las otras dos: no-op en SQLite y en PostgreSQL sin la extensión, así
la misma migración sirve en todos los entornos.
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
                    "SELECT create_hypertable('muestra_red_farmacia', 'timestamp', "
                    "migrate_data => true, if_not_exists => true)"
                )
            logger.info('muestra_red_farmacia convertida en hypertable de TimescaleDB.')
        except Exception as exc:
            # Mismo hueco documentado para las otras dos: el PK `id` de Django excluye la
            # columna de tiempo y TimescaleDB puede rechazar la conversión. No es fatal —
            # la tabla sigue funcionando y la purga de Celery cubre la retención igual.
            logger.warning('No se pudo convertir muestra_red_farmacia en hypertable: %s', exc)


def revertir(apps, schema_editor):
    pass


class Migration(migrations.Migration):
    dependencies = [
        ('monitoreo', '0020_reinicioequipoborde'),
    ]

    operations = [
        migrations.RunPython(crear_hypertable, revertir),
    ]
