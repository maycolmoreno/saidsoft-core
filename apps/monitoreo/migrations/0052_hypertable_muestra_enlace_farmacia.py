"""Historial de latencia y pérdida del enlace de cada farmacia (`MuestraEnlaceFarmacia`).

La tabla nace como hypertable con retención y compresión, en el mismo paso: es lo que las
migraciones 0035 y 0040 hicieron con las otras cuatro series, y hacerlo ahora —con la
tabla vacía— es instantáneo. Mismos números que esas dos: chunk de 1 día (es una serie de
alto volumen, una fila por farmacia cada 2 minutos), compresión a los 7 días agrupada por
farmacia, retención de 30.

Como en 0035, la PK pasa a `(id, timestamp)` antes de convertir, y todo va en un
savepoint: sin TimescaleDB (SQLite, PostgreSQL pelado) la tabla queda normal y la purga
de Celery (`purgar_muestras_enlace_antiguas`) la acota.
"""
import logging

from django.db import migrations, transaction

logger = logging.getLogger(__name__)

TABLA = 'muestra_enlace_farmacia'
DIAS_RETENCION = 30
DIAS_COMPRESION = 7


def convertir(apps, schema_editor):
    conexion = schema_editor.connection
    if conexion.vendor != 'postgresql':
        return
    with conexion.cursor() as cursor:
        cursor.execute("SELECT 1 FROM pg_extension WHERE extname = 'timescaledb'")
        if cursor.fetchone() is None:
            return
        try:
            with transaction.atomic(using=conexion.alias):
                cursor.execute(
                    "SELECT conname FROM pg_constraint WHERE conrelid = %s::regclass AND contype = 'p'", [TABLA],
                )
                fila = cursor.fetchone()
                if fila:
                    cursor.execute('ALTER TABLE %s DROP CONSTRAINT "%s"' % (TABLA, fila[0]))
                cursor.execute('ALTER TABLE %s ADD PRIMARY KEY (id, "timestamp")' % TABLA)
                cursor.execute(
                    "SELECT create_hypertable(%s, 'timestamp', chunk_time_interval => INTERVAL '1 day', "
                    'migrate_data => true, if_not_exists => true)',
                    [TABLA],
                )
                cursor.execute(
                    'SELECT add_retention_policy(%s, INTERVAL %s, if_not_exists => true)',
                    [TABLA, '%d days' % DIAS_RETENCION],
                )
                cursor.execute(
                    "ALTER TABLE %s SET (timescaledb.compress, timescaledb.compress_segmentby = 'farmacia_id', "
                    """timescaledb.compress_orderby = '"timestamp" DESC')""" % TABLA,
                )
                cursor.execute(
                    'SELECT add_compression_policy(%s, INTERVAL %s, if_not_exists => true)',
                    [TABLA, '%d days' % DIAS_COMPRESION],
                )
            logger.info('%s: hypertable + retención %d días + compresión a los %d.', TABLA, DIAS_RETENCION, DIAS_COMPRESION)
        except Exception as exc:
            logger.warning('No se pudo convertir %s en hypertable: %s', TABLA, exc)


class Migration(migrations.Migration):

    dependencies = [
        ('monitoreo', '0051_muestra_enlace_farmacia'),
    ]

    operations = [
        migrations.RunPython(convertir, migrations.RunPython.noop),
    ]
