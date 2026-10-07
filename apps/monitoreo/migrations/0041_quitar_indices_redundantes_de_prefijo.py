"""Quita los 9 índices de una columna que ya son PREFIJO de otro índice de su tabla.

M-1 del informe de auditoría del 5-oct-2026. Son los índices que Django crea solo por cada
`ForeignKey`, en nueve tablas donde ya existe un índice compuesto que empieza por esa misma
columna. PostgreSQL puede usar un índice compuesto para una búsqueda por su primera
columna, así que el de una columna no aporta nada: solo se mantiene en cada INSERT.

Verificado en el catálogo antes de escribir esto: los nueve existen y cada uno es prefijo
estricto de otro índice de la misma tabla.

| tabla | se va | ya lo cubre |
|---|---|---|
| `muestra_servicio_pos` | `(estacion_id)` | `(estacion_id, servicio, timestamp DESC)` |
| `muestra_red_farmacia` | `(farmacia_id)` | `(farmacia_id, timestamp DESC)` |
| `muestra_metrica` | `(estacion_id)` | `(estacion_id, timestamp DESC)` |
| `evento_monitoreo` | `(estacion_id)` | `(estacion_id, timestamp DESC)` |
| `evento_sistema_detectado` | `(estacion_id)` | `(estacion_id, ultima_vez DESC)` |
| `pos_error_detectado` | `(estacion_id)` | UNIQUE `(estacion_id, mensaje)` |
| `estado_dispositivo` | `(estacion_id)` | UNIQUE `(estacion_id, fuente)` |
| `estado_servicio_pos` | `(estacion_id)` | UNIQUE `(estacion_id, servicio)` |
| `dispositivo_detectado` | `(farmacia_id)` | `(farmacia_id, ip)` y UNIQUE `(farmacia_id, mac)` |

**Lo que de verdad se gana es escritura, no disco, y eso cambió desde que se escribió el
informe.** El informe proyectaba ~2,04 GB de ahorro solo en `muestra_servicio_pos`, contando
24 bytes por fila sobre 84,9 M filas a 1.300 farmacias. Esa cuenta era correcta entonces,
pero la migración 0040 encendió la compresión: un chunk comprimido no conserva los índices
de la tabla, así que el índice redundante hoy solo existe en los chunks de los últimos 7
días. En disco queda una fracción de lo proyectado. Lo que NO cambió es que cada INSERT
mantenía una entrada de índice de más en las tres tablas de más escritura — a 1.300
farmacias son ~1,7 millones de INSERT por día solo en `muestra_servicio_pos`—, y ese es el
motivo por el que vale hacerlo.

**No se tocan los otros 54 índices redundantes del esquema.** El informe los encontró en
tablas chicas y recomendó explícitamente dejarlos: el ahorro es de kilobytes y el riesgo de
equivocarse no se compensa.

## Por qué está escrita a mano

`makemigrations` genera para cada campo un `AlterField` que, además de soltar el índice,
**tira y rehace la constraint de FK**:

    ALTER TABLE "muestra_servicio_pos" DROP CONSTRAINT "..._fk_estacion_id";
    DROP INDEX IF EXISTS "muestra_servicio_pos_estacion_id_163db46e";
    ALTER TABLE "muestra_servicio_pos" ADD CONSTRAINT "..." FOREIGN KEY ...;

Rehacer una FK **valida la tabla entera** y toma un lock fuerte sobre ella. Sobre las
hypertables de producción —las tablas más calientes del sistema, con chunks comprimidos— eso
es exactamente lo que no conviene hacer para un cambio cuyo efecto deseado es una sola línea
de DDL. Así que el estado del modelo se declara con `state_operations` y lo único que corre
contra la base es el `DROP INDEX`.

## La red de seguridad

El índice de una FK no es decorativo: borrar una `Estacion` obliga a PostgreSQL a buscar las
filas que la referencian, y sin ningún índice sobre esa columna eso es un seq scan de la
serie entera. Por eso cada `DROP INDEX` se hace solo después de comprobar **en el catálogo,
en el momento de correr** que existe otro índice cuya primera columna es la misma. Si no
aparece, la migración no borra nada y deja un `warning`. Un nombre de índice hardcodeado
tampoco se usa: se busca el índice real de una columna, y si no hay exactamente uno, no se
toca.
"""
import logging

import django.db.models.deletion
from django.db import migrations, models

logger = logging.getLogger(__name__)

# (tabla, columna). La columna es la del `ForeignKey` que pierde su índice propio.
OBJETIVOS = (
    ('muestra_servicio_pos', 'estacion_id'),
    ('muestra_red_farmacia', 'farmacia_id'),
    ('muestra_metrica', 'estacion_id'),
    ('evento_monitoreo', 'estacion_id'),
    ('evento_sistema_detectado', 'estacion_id'),
    ('pos_error_detectado', 'estacion_id'),
    ('estado_dispositivo', 'estacion_id'),
    ('estado_servicio_pos', 'estacion_id'),
    ('dispositivo_detectado', 'farmacia_id'),
)

# Índices de UNA columna, no únicos, sobre `columna`: los candidatos a irse.
_SQL_CANDIDATOS = """
SELECT i.relname
FROM pg_index idx
JOIN pg_class i ON i.oid = idx.indexrelid
JOIN pg_class t ON t.oid = idx.indrelid
JOIN pg_namespace n ON n.oid = t.relnamespace
JOIN pg_attribute a ON a.attrelid = t.oid AND a.attnum = idx.indkey[0]
WHERE n.nspname = 'public' AND t.relname = %s AND a.attname = %s
  AND idx.indnatts = 1 AND NOT idx.indisunique AND NOT idx.indisprimary
  AND idx.indpred IS NULL
"""

# Índices de VARIAS columnas (o únicos) cuya PRIMERA columna es `columna`: los que cubren.
_SQL_COBERTURA = """
SELECT i.relname
FROM pg_index idx
JOIN pg_class i ON i.oid = idx.indexrelid
JOIN pg_class t ON t.oid = idx.indrelid
JOIN pg_namespace n ON n.oid = t.relnamespace
JOIN pg_attribute a ON a.attrelid = t.oid AND a.attnum = idx.indkey[0]
WHERE n.nspname = 'public' AND t.relname = %s AND a.attname = %s
  AND idx.indpred IS NULL AND i.relname <> %s
  AND (idx.indnatts > 1 OR idx.indisunique)
"""


def quitar(apps, schema_editor):
    conexion = schema_editor.connection
    if conexion.vendor != 'postgresql':
        # SQLite (desarrollo): el índice de más no molesta y no hay catálogo que consultar.
        # El estado del modelo lo fija `state_operations` igual, así que no hay drift.
        return
    with conexion.cursor() as cursor:
        for tabla, columna in OBJETIVOS:
            cursor.execute(_SQL_CANDIDATOS, [tabla, columna])
            candidatos = [fila[0] for fila in cursor.fetchall()]
            if len(candidatos) != 1:
                logger.warning(
                    '%s.%s: se esperaba UN índice de una columna y hay %d (%s). No se toca.',
                    tabla, columna, len(candidatos), ', '.join(candidatos) or 'ninguno',
                )
                continue
            indice = candidatos[0]

            cursor.execute(_SQL_COBERTURA, [tabla, columna, indice])
            cobertura = [fila[0] for fila in cursor.fetchall()]
            if not cobertura:
                # La red de seguridad: sin otro índice que empiece por esta columna, borrar
                # el de la FK dejaría el borrado en cascada haciendo un seq scan de la serie.
                logger.warning(
                    '%s.%s: no hay ningún índice que empiece por esa columna aparte de %s. '
                    'NO se borra: sin índice, borrar la fila padre haría un seq scan.',
                    tabla, columna, indice,
                )
                continue

            cursor.execute('DROP INDEX IF EXISTS "%s"' % indice)
            logger.info('%s: índice %s quitado (lo cubre %s).', tabla, indice, cobertura[0])


def devolver(apps, schema_editor):
    """Recrea el índice de una columna. No usa CONCURRENTLY porque una migración corre
    dentro de una transacción; con datos reales conviene hacerlo a mano y fuera de hora."""
    conexion = schema_editor.connection
    if conexion.vendor != 'postgresql':
        return
    with conexion.cursor() as cursor:
        for tabla, columna in OBJETIVOS:
            cursor.execute(
                'CREATE INDEX IF NOT EXISTS "%s_%s_idx" ON "%s" ("%s")'
                % (tabla, columna, tabla, columna),
            )


class Migration(migrations.Migration):
    dependencies = [
        ('catalogo', '0035_corp_admin_y_tipo_tienda'),
        ('monitoreo', '0040_compresion_hypertables'),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            database_operations=[migrations.RunPython(quitar, devolver)],
            state_operations=[
                migrations.AlterField(
                    model_name='dispositivodetectado',
                    name='farmacia',
                    field=models.ForeignKey(
                        db_index=False, on_delete=django.db.models.deletion.CASCADE,
                        related_name='dispositivos_detectados', to='catalogo.farmacia',
                    ),
                ),
                migrations.AlterField(
                    model_name='estadodispositivo',
                    name='estacion',
                    field=models.ForeignKey(
                        db_index=False, on_delete=django.db.models.deletion.CASCADE,
                        related_name='estados_dispositivo', to='catalogo.estacion',
                    ),
                ),
                migrations.AlterField(
                    model_name='estadoserviciopos',
                    name='estacion',
                    field=models.ForeignKey(
                        db_index=False, on_delete=django.db.models.deletion.CASCADE,
                        related_name='servicios_pos', to='catalogo.estacion',
                    ),
                ),
                migrations.AlterField(
                    model_name='eventomonitoreo',
                    name='estacion',
                    field=models.ForeignKey(
                        db_index=False, on_delete=django.db.models.deletion.CASCADE,
                        related_name='eventos_monitoreo', to='catalogo.estacion',
                    ),
                ),
                migrations.AlterField(
                    model_name='eventosistemadetectado',
                    name='estacion',
                    field=models.ForeignKey(
                        db_index=False, on_delete=django.db.models.deletion.CASCADE,
                        related_name='eventos_sistema', to='catalogo.estacion',
                    ),
                ),
                migrations.AlterField(
                    model_name='muestrametrica',
                    name='estacion',
                    field=models.ForeignKey(
                        db_index=False, on_delete=django.db.models.deletion.CASCADE,
                        related_name='metricas', to='catalogo.estacion',
                    ),
                ),
                migrations.AlterField(
                    model_name='muestraredfarmacia',
                    name='farmacia',
                    field=models.ForeignKey(
                        db_index=False, on_delete=django.db.models.deletion.CASCADE,
                        related_name='muestras_red', to='catalogo.farmacia',
                    ),
                ),
                migrations.AlterField(
                    model_name='muestraserviciopos',
                    name='estacion',
                    field=models.ForeignKey(
                        db_index=False, on_delete=django.db.models.deletion.CASCADE,
                        related_name='muestras_servicios_pos', to='catalogo.estacion',
                    ),
                ),
                migrations.AlterField(
                    model_name='poserrordetectado',
                    name='estacion',
                    field=models.ForeignKey(
                        db_index=False, on_delete=django.db.models.deletion.CASCADE,
                        related_name='pos_errores', to='catalogo.estacion',
                    ),
                ),
            ],
        ),
    ]
