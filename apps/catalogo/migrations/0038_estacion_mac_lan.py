from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('catalogo', '0037_enlace_instalador'),
    ]

    operations = [
        migrations.AddField(
            model_name='estacion',
            name='mac_lan',
            field=models.CharField(
                blank=True, default='', max_length=17,
                help_text='MAC de la placa de red con la que sale la estación (AA:BB:CC:DD:EE:FF), reportada por el agente.',
            ),
            preserve_default=False,
        ),
    ]
