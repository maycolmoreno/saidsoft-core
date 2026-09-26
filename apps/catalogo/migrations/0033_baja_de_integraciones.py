"""Da de baja apps.integraciones (26-sep-2026).

La app se construyó como andamiaje para conectores externos (Odoo, AD, ESET) que nunca
se escribieron. `registrar_sync_pendiente` —único creador de filas— jamás se llamó desde
fuera de la app: `git log -S` muestra un solo commit tocándolo, el que creó la app.
El admin además define `has_add_permission = False`. Ver docs/modulos.md.

El drop vive acá y no en la propia app porque al sacarla de INSTALLED_APPS sus migraciones
dejan de correr. `catalogo` es el destino natural: `sincronizacion_externa` tenía una FK a
`catalogo.unidad_negocio`.

**No borra a ciegas.** Si alguna tabla trae filas, la migración corta el deploy con un
error en vez de tirar datos que nadie esperaba que existieran.
"""
from django.db import migrations

TABLAS = ['evento_sync_externo', 'sincronizacion_externa']  # hija primero: FK CASCADE


def bajar(apps, schema_editor):
    con = schema_editor.connection
    existentes = set(con.introspection.table_names())
    with con.cursor() as cur:
        for tabla in TABLAS:
            if tabla not in existentes:
                continue  # base nueva: la app ya no está, la tabla nunca se creó
            cur.execute(f'SELECT count(*) FROM "{tabla}"')
            filas = cur.fetchone()[0]
            if filas:
                raise RuntimeError(
                    f'"{tabla}" tiene {filas} fila(s) y la baja de apps.integraciones '
                    f'asumía cero. Se corta el deploy a propósito: revisá de dónde salieron '
                    f'antes de borrarlas. Para continuar, respaldá la tabla y vaciala a mano.'
                )
            cur.execute(f'DROP TABLE "{tabla}"')
        # Sin esto, django_migrations queda con filas de una app que ya no existe y
        # `showmigrations` no las lista, así que nadie las encuentra.
        cur.execute("DELETE FROM django_migrations WHERE app = 'integraciones'")


def revertir(apps, schema_editor):
    raise RuntimeError(
        'Para volver atrás: `git revert` del commit que sacó apps.integraciones, y luego '
        '`migrate integraciones` recrea las tablas desde su 0001_initial. Esta migración no '
        'las puede recrear sola porque los modelos ya no están en el proyecto.'
    )


class Migration(migrations.Migration):

    dependencies = [('catalogo', '0032_autocorreccion_del_reloj')]

    operations = [migrations.RunPython(bajar, revertir)]
