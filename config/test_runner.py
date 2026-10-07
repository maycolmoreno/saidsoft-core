"""Runner de pruebas: apaga los jobs de TimescaleDB en la base de pruebas.

Existe por un fallo intermitente encontrado el 6-oct-2026, el mismo día que se encendió la
compresión (migración 0040):

    django.db.utils.OperationalError: deadlock detected
    DETAIL: Process 36492 waits for ShareRowExclusiveLock on relation 1542733;
            blocked by process 36530.

**El segundo proceso no era otra prueba: era el planificador de TimescaleDB.** Las
migraciones 0035 y 0040 crean políticas nativas —4 de retención (cada 24 h) y 4 de
compresión (cada 12 h)— y esas políticas son filas en la base, así que la base de pruebas
las hereda y el *background worker* de TimescaleDB las corre ahí también. `compress_chunk`
toma locks fuertes sobre la hypertable, y una prueba insertando en esa misma tabla al mismo
tiempo se traba contra él.

Una base de pruebas efímera no necesita mantenimiento: vive minutos y se tira. Así que acá
se desagendan los 8 jobs en cuanto la base queda creada. Producción no se entera — esto
corre solo cuando corre la suite.

**Por qué acá y no en la migración:** la migración describe el esquema que producción tiene
que tener, y ese esquema incluye las políticas. Hacerla preguntar "¿soy una base de
pruebas?" sería doblar la definición del esquema por conveniencia de la suite. El runner,
en cambio, ya es código que solo existe para correr pruebas.

Lo que esto NO apaga es la compresión en sí: las cuatro tablas siguen con
`compression_enabled`, y `RetencionDeSeriesPorChunksTests` sigue comprobándolo. Lo único
que deja de pasar es que un job se despierte en medio de la suite.
"""
import logging

from django.test.runner import DiscoverRunner

logger = logging.getLogger(__name__)


def _desagendar_jobs_de_timescale(conexion):
    """Pone `scheduled => false` en todos los jobs de políticas de la base conectada."""
    if conexion.vendor != 'postgresql':
        return 0
    with conexion.cursor() as cursor:
        # Se pregunta por la extensión antes de tocar `timescaledb_information`: consultar
        # un esquema inexistente aborta la transacción en curso (mismo cuidado que toma
        # apps.monitoreo.services.es_hypertable).
        cursor.execute("SELECT 1 FROM pg_extension WHERE extname = 'timescaledb'")
        if cursor.fetchone() is None:
            return 0
        cursor.execute(
            'SELECT job_id FROM timescaledb_information.jobs WHERE scheduled '
            "AND proc_name IN ('policy_retention', 'policy_compression')",
        )
        ids = [fila[0] for fila in cursor.fetchall()]
        for job_id in ids:
            cursor.execute('SELECT alter_job(%s, scheduled => false)', [job_id])
    return len(ids)


class RunnerSinJobsDeTimescale(DiscoverRunner):
    """`DiscoverRunner` + desagendar los jobs de TimescaleDB de la base de pruebas."""

    def setup_databases(self, **kwargs):
        configuracion = super().setup_databases(**kwargs)
        from django.db import connections

        for conexion in connections.all():
            cuantos = _desagendar_jobs_de_timescale(conexion)
            if cuantos:
                logger.info(
                    'Base de pruebas %s: %d job(s) de TimescaleDB desagendado(s).',
                    conexion.settings_dict['NAME'], cuantos,
                )
        return configuracion
