"""Convierte de verdad las cuatro series en hypertables de TimescaleDB.

Las migraciones 0002 (`muestra_metrica`), 0006 (`evento_monitoreo`) y 0021
(`muestra_red_farmacia`) intentan esto mismo desde el principio y **fallan siempre**,
también en producción: `cannot create a unique index without the column timestamp`. La
clave primaria que Django crea sola es `id`, y TimescaleDB no acepta un índice único que
no incluya la columna de particionado. Como el fallo queda aislado en un savepoint y
solo deja un `logger.warning`, el despliegue pasa y nadie se entera — verificado el
16-sep-2026: `timescaledb_information.hypertables` devolvía cero filas.

Acá se ataca la causa: la PK pasa a ser `(id, timestamp)` ANTES de convertir. El `id` lo
sigue generando la misma secuencia y el ORM lo sigue usando igual (un `WHERE id = X`
ataca el prefijo del índice compuesto), así que ningún modelo cambia y esta migración no
necesita `state_operations`: para Django el esquema es el mismo de antes.

Se suma `muestra_servicio_pos`, que nunca tuvo intento y es la que más crece: una fila
por servicio del POS que responde, cuatro por estación cada 5 minutos — a 1.500
estaciones son ~1,7 millones de filas por día.

**Qué cambia en la práctica.** La retención deja de ser un DELETE de millones de filas
—que deja bloat y le da trabajo al autovacuum justo en la tabla más caliente— y pasa a
ser `drop_chunks`, que desengancha archivos enteros. Las purgas de Celery
(`apps.monitoreo.tasks.purgar_*`) se quedan donde están y con la MISMA ventana de 30
días: son las que corren donde no hay TimescaleDB (SQLite en desarrollo, o un PostgreSQL
pelado) y acá no estorban, porque borrar lo que la política ya se llevó no cuesta nada.
Si alguna vez hay que cambiar la retención, hay que cambiarla en los dos lados.

**Momento.** La base entera pesa 31 MB (medido el 19-sep-2026). `migrate_data => true`
copia las filas que ya existen: hoy es instantáneo, con 50 millones de filas es una
ventana de mantenimiento. Por eso va ahora y no cuando haga falta.

No se activa compresión todavía. Un chunk comprimido cambia el comportamiento de los
DELETE que las purgas siguen haciendo, y eso se prueba contra datos reales antes de
encenderlo, no de entrada.
"""
import logging

from django.db import migrations, transaction

logger = logging.getLogger(__name__)

# (tabla, intervalo de chunk). Un día en las dos series de alto volumen para que cada
# chunk entre holgado en memoria; el default de 7 días en las otras dos, que escriben
# un orden de magnitud menos.
TABLAS = (
    ('muestra_metrica', '1 day'),
    ('muestra_servicio_pos', '1 day'),
    ('evento_monitoreo', '7 days'),
    ('muestra_red_farmacia', '7 days'),
)

DIAS_RETENCION = 30  # el mismo número que usan las purgas de apps.monitoreo.tasks


def _es_hypertable(cursor, tabla):
    cursor.execute(
        'SELECT 1 FROM timescaledb_information.hypertables WHERE hypertable_name = %s', [tabla],
    )
    return cursor.fetchone() is not None


def _pk_incluye_timestamp(cursor, tabla):
    """True si la PK actual ya es compuesta con `timestamp` (esta migración ya corrió)."""
    cursor.execute(
        """
        SELECT a.attname
        FROM pg_constraint c
        JOIN unnest(c.conkey) AS k(attnum) ON true
        JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = k.attnum
        WHERE c.conrelid = %s::regclass AND c.contype = 'p'
        """,
        [tabla],
    )
    return 'timestamp' in {fila[0] for fila in cursor.fetchall()}


def _nombre_pk(cursor, tabla):
    cursor.execute(
        "SELECT conname FROM pg_constraint WHERE conrelid = %s::regclass AND contype = 'p'", [tabla],
    )
    fila = cursor.fetchone()
    return fila[0] if fila else None


def convertir(apps, schema_editor):
    conexion = schema_editor.connection
    if conexion.vendor != 'postgresql':
        return  # SQLite (desarrollo): la tabla normal alcanza, y las purgas ya la acotan
    with conexion.cursor() as cursor:
        cursor.execute("SELECT 1 FROM pg_extension WHERE extname = 'timescaledb'")
        if cursor.fetchone() is None:
            return  # PostgreSQL sin la extensión: sigue siendo una tabla normal

        for tabla, intervalo_chunk in TABLAS:
            try:
                # Un savepoint por tabla: si una falla, Postgres aborta la transacción en
                # curso y hasta el INSERT en django_migrations reventaría. Aislar el fallo
                # deja que las otras tres se conviertan igual (mismo criterio que 0002).
                with transaction.atomic(using=conexion.alias):
                    if not _pk_incluye_timestamp(cursor, tabla):
                        nombre = _nombre_pk(cursor, tabla)
                        if nombre:
                            cursor.execute('ALTER TABLE %s DROP CONSTRAINT "%s"' % (tabla, nombre))
                        cursor.execute('ALTER TABLE %s ADD PRIMARY KEY (id, "timestamp")' % tabla)

                    if not _es_hypertable(cursor, tabla):
                        cursor.execute(
                            "SELECT create_hypertable(%s, 'timestamp', "
                            'chunk_time_interval => INTERVAL %s, '
                            'migrate_data => true, if_not_exists => true)',
                            [tabla, intervalo_chunk],
                        )

                    cursor.execute(
                        'SELECT add_retention_policy(%s, INTERVAL %s, if_not_exists => true)',
                        [tabla, '%d days' % DIAS_RETENCION],
                    )
                logger.info('%s: hypertable + retención de %d días.', tabla, DIAS_RETENCION)
            except Exception as exc:
                # No es fatal: la tabla sigue siendo PostgreSQL normal y las purgas de
                # Celery la siguen acotando, que es exactamente el estado de hoy.
                logger.warning('No se pudo convertir %s en hypertable: %s', tabla, exc)


def revertir(apps, schema_editor):
    # Volver atrás exigiría recrear cada tabla desde sus chunks. No se hace: la PK
    # compuesta y los chunks no molestan a ninguna migración posterior.
    pass


class Migration(migrations.Migration):
    dependencies = [
        ('monitoreo', '0034_sembrar_eventos_vigilados'),
    ]

    operations = [
        migrations.RunPython(convertir, revertir),
    ]
