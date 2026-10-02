"""Elimina el esquema de auditoría de sincronización con RRHH, que nunca tuvo conector.

`SyncEjecucion`/`SyncCambio` se crearon como esquema puro, por adelantado, para auditar
las corridas de un sync de colaboradores con un sistema de RRHH externo. Ese sistema
nunca se definió, así que las tablas vivieron vacías y su propio docstring lo decía: "no
hay conector real todavía […] hoy nada crea filas aquí". El 2-oct-2026 se decidió no
hacer la integración.

Los campos COMPAÑEROS de `Colaborador` (`origen_sync`, `sincronizado_en`,
`cargo_directorio`, `departamento_directorio`) NO se tocan: los llena
`crear_tecnicos_soporte`, que es un alta puntual con datos hardcodeados, y distinguen a
quien vino de una planilla de RRHH de quien se cargó a mano.
"""
from django.db import migrations


def exigir_tablas_vacias(apps, schema_editor):
    """Aborta la migración si alguien cargó filas, en vez de borrarlas en silencio.

    Las dos tablas deberían estar vacías —nada en el código las escribe— pero estaban
    registradas en el admin de Django, así que una carga a mano era posible. Un
    `DeleteModel` sobre una tabla con datos los destruye sin preguntar y sin dejar rastro.

    Esta guarda convierte esa pérdida irreversible en un despliegue que falla y dice qué
    encontró. Si salta, la decisión vuelve a una persona: si esas filas no importan, se
    borran a mano y se repite el despliegue; si importan, hay que exportarlas antes.

    No usa los modelos reales sino `apps.get_model`: en una migración histórica los
    modelos del código pueden ya no existir, y de hecho acá no existen.
    """
    SyncEjecucion = apps.get_model('activos', 'SyncEjecucion')
    SyncCambio = apps.get_model('activos', 'SyncCambio')

    ejecuciones = SyncEjecucion.objects.count()
    cambios = SyncCambio.objects.count()
    if ejecuciones or cambios:
        raise RuntimeError(
            'No se eliminan las tablas de auditoría de sync RRHH porque tienen datos: '
            '%d SyncEjecucion y %d SyncCambio. Se esperaban vacías (nada en el código las '
            'escribe). Revisá qué son antes de seguir: si no hacen falta, borralas a mano '
            'y repetí el despliegue; si hacen falta, exportalas primero.'
            % (ejecuciones, cambios),
        )


def sin_vuelta_atras(apps, schema_editor):
    """La reversa recrea las tablas vacías, que es todo lo que había. No hay datos que
    restaurar porque no había datos."""


class Migration(migrations.Migration):

    dependencies = [
        ('activos', '0025_convencion_unidad_corporativa'),
    ]

    operations = [
        # Primero la guarda: tiene que correr ANTES de cualquier DROP, o no sirve de nada.
        migrations.RunPython(exigir_tablas_vacias, sin_vuelta_atras),
        migrations.RemoveField(
            model_name='syncejecucion',
            name='ejecutado_por',
        ),
        migrations.DeleteModel(
            name='SyncCambio',
        ),
        migrations.DeleteModel(
            name='SyncEjecucion',
        ),
    ]
