from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('cuentas', '0001_initial'),
        ('catalogo', '0009_alter_farmacia_unidad_negocio'),
    ]

    operations = [
        migrations.AddField(
            model_name='perfilusuario',
            name='acceso_todas_unidades',
            field=models.BooleanField(
                default=False,
                help_text='Para personal interno (soporte/operaciones) que necesita ver todos los '
                          'clientes. `unidades_negocio` se ignora si esto está activo.',
            ),
        ),
        migrations.AddField(
            model_name='perfilusuario',
            name='unidades_negocio',
            field=models.ManyToManyField(
                blank=True, related_name='usuarios', to='catalogo.unidadnegocio',
                help_text='Clientes que este usuario puede ver y accionar. Vacío + '
                          'acceso_todas_unidades=False equivale a no ver ningún dato con tenant.',
            ),
        ),
    ]
