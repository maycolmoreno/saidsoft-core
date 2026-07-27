"""Cierra la migración texto->FK de Colaborador.cargo: quita el CharField viejo
y renombra `cargo_nueva` (FK, ya poblado por la migración 0010) a `cargo`.

Escrita a mano por la misma razón que 0005_marca_texto_a_fk_definitivo: evitar
que el autodetector confunda esto con un AlterField de tipo sobre el mismo
nombre de campo.
"""
from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('activos', '0010_migrar_cargo_texto_a_fk'),
    ]

    operations = [
        migrations.RemoveField(
            model_name='colaborador',
            name='cargo',
        ),
        migrations.RenameField(
            model_name='colaborador',
            old_name='cargo_nueva',
            new_name='cargo',
        ),
    ]
