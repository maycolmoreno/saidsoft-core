"""Índice para el patrón "aprobadas + último latido", el más consultado del sistema.

Lo usan `estado_flota` (la fuente única del KPI de flota: tablero, Centro y bot),
`marcar_estaciones_offline` (cada 60 segundos, siempre), la lista de estaciones calladas
del tablero y los contadores del listado. Ver el comentario del índice en
`apps.catalogo.models.Estacion.Meta`.

**`AddIndex` normal y no `AddIndexConcurrently`, a propósito.** La auditoría recomendaba la
variante concurrente porque `AddIndex` toma un lock de escritura y el entrypoint corre las
migraciones solo en cada despliegue. A este tamaño no se justifica: la tabla tiene decenas
de filas hoy y el parque proyectado son ~1.800, así que el `CREATE INDEX` son
milisegundos. La variante concurrente obliga a `atomic = False` y a una operación
exclusiva de PostgreSQL, que rompería el desarrollo local sobre SQLite a cambio de nada
medible. Si esta tabla llegara a cientos de miles de filas, el cambio es de una línea.
"""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('catalogo', '0035_corp_admin_y_tipo_tienda'),
    ]

    operations = [
        migrations.AddIndex(
            model_name='estacion',
            index=models.Index(fields=['estado_aprobacion', 'ultimo_heartbeat'], name='estacion_aprobacion_latido'),
        ),
    ]
