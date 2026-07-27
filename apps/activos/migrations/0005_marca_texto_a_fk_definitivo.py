"""Cierra la migración texto->FK de Activo.marca: quita el CharField viejo
y renombra `marca_nueva` (FK, ya poblado por la migración 0004) a `marca`.

Se escribe a mano en vez de confiar en el autodetector de makemigrations
porque ambos campos comparten temporalmente el nombre final `marca` en el
modelo — el autodetector podría intentar interpretar esto como un cambio de
tipo en un solo AlterField (arriesgado en SQLite) en vez del remove+rename
seguro que se hace aquí.
"""
from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('activos', '0004_migrar_marca_texto_a_fk'),
    ]

    operations = [
        migrations.RemoveField(
            model_name='activo',
            name='marca',
        ),
        migrations.RenameField(
            model_name='activo',
            old_name='marca_nueva',
            new_name='marca',
        ),
    ]
