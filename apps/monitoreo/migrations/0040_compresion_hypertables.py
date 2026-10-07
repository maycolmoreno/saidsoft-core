"""Enciende la compresión en las cuatro hypertables, con la retención ya adaptada.

La migración 0035 las convirtió en hypertables y dejó la compresión explícitamente
afuera: «Un chunk comprimido cambia el comportamiento de los DELETE que las purgas
siguen haciendo, y eso se prueba contra datos reales antes de encenderlo». Esto es esa
prueba, hecha el 6-oct-2026 contra TimescaleDB 2.17.2 en una base aparte, con 665.280
filas con la forma de `muestra_metrica`:

1. **La PK `(id, timestamp)` no impide comprimir.** `ALTER TABLE ... SET
   (timescaledb.compress)` avisa «column "id" should be used for segmenting or ordering»
   y sigue adelante. Se acepta el aviso a propósito: poner `id` en `segmentby` haría un
   segmento por fila —o sea, no comprimir— y `id` sale de una secuencia, así que no hay
   duplicados que el índice único tenga que atrapar.
2. **Un DELETE sobre chunks comprimidos falla**: «tuple decompression limit exceeded by
   operation — current limit: 100000, tuples decompressed: 210210». Y la transacción
   abortada deja como tuplas muertas lo que llegó a descomprimir: la tabla pasó de 64 MB
   a **106 MB por un solo DELETE fallido**. Por debajo de las 100.000 filas el DELETE sí
   entra, y es peor: descomprime en silencio lo que se acababa de comprimir. Por eso las
   purgas de Celery dejaron de borrar donde hay retención nativa (ver
   `services._purgar_serie`), y por eso este orden —purgas primero, compresión
   después— no es una preferencia.
3. **Un INSERT atrasado NO descomprime.** Importaba porque el agente tiene cola offline:
   una estación que estuvo caída manda muestras viejas al volver. Probado con una fila y
   con una tanda de 2.000 sobre un chunk comprimido: entran, los chunks siguen
   comprimidos, y lo insertado se lee igual.

**`segmentby` se eligió midiendo, no por analogía.** La segunda columna de agrupación
cambia el resultado por un factor de 4 a 7:

    muestra_servicio_pos   estacion_id            11,6x
                           estacion_id, servicio  42,5x   <- elegido
    evento_monitoreo       estacion_id            16,7x
                           estacion_id, fuente    110x    <- elegido

El motivo es el mismo en las dos: con la segunda columna cada segmento queda con UNA
serie (un servicio de una estación, una fuente de una estación) en vez de varias
entreveradas, y además ese valor repetido deja de almacenarse por fila y pasa a ser la
clave del segmento. `muestra_metrica` y `muestra_red_farmacia` no tienen segunda columna
que agrupe: una fila por estación/farmacia y timestamp.

Sobre `muestra_metrica`, con datos realistas (series que se mueven poco, enteros
repetidos, dos columnas en NULL): **10,7x** — 72 MB de chunks pasaron a 6.888 kB. Los
índices eran 59 de los 129 MB sin comprimir, y eso también se va.

**Comprime a los 7 días**, no antes: la semana caliente —que es lo que miran los
gráficos del panel— se queda sin comprimir, y los chunks de 1 día de las dos series
grandes entran comprimidos de a uno. La retención sigue en 30 días y no se toca: con
compresión el espacio deja de ser el argumento para recortarla, que es justo lo que el
informe pedía no hacer.

**Al desplegar**, conviene no hacerlo sobre la ventana de las purgas (03:00): entre que
esta migración corre y que `celery_beat` levanta con el código nuevo, un `beat` viejo
todavía podría disparar un DELETE sobre chunks ya comprimidos.
"""
import logging

from django.db import migrations, transaction

logger = logging.getLogger(__name__)

# (tabla, segmentby). El `orderby` es `"timestamp" DESC` en las cuatro: es la columna de
# particionado y el orden en que se lee siempre (lo último primero).
TABLAS = (
    ('muestra_metrica', 'estacion_id'),
    ('muestra_servicio_pos', 'estacion_id, servicio'),
    ('evento_monitoreo', 'estacion_id, fuente'),
    ('muestra_red_farmacia', 'farmacia_id'),
)

DIAS_COMPRESION = 7


def _es_hypertable(cursor, tabla):
    cursor.execute(
        'SELECT 1 FROM timescaledb_information.hypertables WHERE hypertable_name = %s', [tabla],
    )
    return cursor.fetchone() is not None


def encender(apps, schema_editor):
    conexion = schema_editor.connection
    if conexion.vendor != 'postgresql':
        return  # SQLite (desarrollo): no hay hypertables que comprimir
    with conexion.cursor() as cursor:
        cursor.execute("SELECT 1 FROM pg_extension WHERE extname = 'timescaledb'")
        if cursor.fetchone() is None:
            return  # PostgreSQL pelado: las tablas son normales y las purgas las acotan

        for tabla, segmentby in TABLAS:
            try:
                # Un savepoint por tabla, igual que 0035 y por lo mismo: si una falla,
                # Postgres aborta la transacción entera y hasta el INSERT en
                # django_migrations reventaría. Aislado, las otras tres se encienden.
                with transaction.atomic(using=conexion.alias):
                    if not _es_hypertable(cursor, tabla):
                        logger.warning(
                            '%s no es hypertable: no se enciende compresión. La purga de '
                            'Celery sigue siendo su retención.', tabla,
                        )
                        continue
                    # `compress_segmentby` no admite parámetros: es una opción de
                    # ALTER TABLE, no un valor. Los nombres salen de TABLAS, acá arriba,
                    # no de ninguna entrada.
                    cursor.execute(
                        'ALTER TABLE %s SET ('
                        'timescaledb.compress, '
                        "timescaledb.compress_segmentby = '%s', "
                        'timescaledb.compress_orderby = \'"timestamp" DESC\')'
                        % (tabla, segmentby),
                    )
                    cursor.execute(
                        'SELECT add_compression_policy(%s, INTERVAL %s, if_not_exists => true)',
                        [tabla, '%d days' % DIAS_COMPRESION],
                    )
                logger.info(
                    '%s: compresión encendida (segmentby %s) a los %d días.',
                    tabla, segmentby, DIAS_COMPRESION,
                )
            except Exception as exc:
                # No es fatal: sin compresión la tabla sigue funcionando igual, solo
                # ocupa lo que ocupaba ayer. Lo que NO puede pasar es que la migración
                # falle a medias y deje la compresión encendida sin que las purgas se
                # hayan adaptado — pero eso ya está resuelto, porque las purgas de este
                # mismo commit no borran en cuanto hay retención nativa, comprima o no.
                logger.warning('No se pudo encender compresión en %s: %s', tabla, exc)


def apagar(apps, schema_editor):
    """Revertir SÍ se puede acá, al contrario que en 0035: se quita la política y se
    descomprimen los chunks. Con datos reales es una operación larga y que necesita el
    espacio en disco de la versión sin comprimir — no es un `migrate` más."""
    conexion = schema_editor.connection
    if conexion.vendor != 'postgresql':
        return
    with conexion.cursor() as cursor:
        cursor.execute("SELECT 1 FROM pg_extension WHERE extname = 'timescaledb'")
        if cursor.fetchone() is None:
            return
        for tabla, _segmentby in TABLAS:
            try:
                with transaction.atomic(using=conexion.alias):
                    if not _es_hypertable(cursor, tabla):
                        continue
                    cursor.execute(
                        'SELECT remove_compression_policy(%s, if_exists => true)', [tabla],
                    )
                    cursor.execute(
                        'SELECT decompress_chunk(ch, if_compressed => true) '
                        'FROM show_chunks(%s) ch', [tabla],
                    )
                    cursor.execute('ALTER TABLE %s SET (timescaledb.compress = false)' % tabla)
            except Exception as exc:
                logger.warning('No se pudo apagar compresión en %s: %s', tabla, exc)


class Migration(migrations.Migration):
    dependencies = [
        ('monitoreo', '0039_indice_alerta_estado_abierta'),
    ]

    operations = [
        migrations.RunPython(encender, apagar),
    ]
