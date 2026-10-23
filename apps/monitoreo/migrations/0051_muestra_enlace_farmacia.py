"""Tabla del historial de latencia y pérdida de enlaces. La conversión a hypertable va
en 0052: Django crea los índices de esta tabla al FINAL de la migración, y con la
compresión ya encendida TimescaleDB rechaza cualquier cambio de esquema."""
import django.db.models.deletion
import django.utils.timezone
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('catalogo', '0041_fabricante_y_modelo_del_equipo'),
        ('monitoreo', '0050_una_alerta_activa_por_regla_y_estacion'),
    ]

    operations = [
        migrations.CreateModel(
            name='MuestraEnlaceFarmacia',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('latencia_ms', models.FloatField(blank=True, help_text='Promedio de los paquetes que volvieron.', null=True)),
                ('latencia_max_ms', models.FloatField(blank=True, null=True)),
                ('perdida_pct', models.FloatField(blank=True, help_text='Paquetes perdidos sobre los enviados en el sondeo. null = no se pudo saber.', null=True)),
                ('timestamp', models.DateTimeField(db_index=True, default=django.utils.timezone.now, editable=False)),
                ('farmacia', models.ForeignKey(db_index=False, on_delete=django.db.models.deletion.CASCADE, related_name='muestras_enlace', to='catalogo.farmacia')),
            ],
            options={
                'verbose_name': 'Muestra de enlace de farmacia',
                'verbose_name_plural': 'Muestras de enlace de farmacia',
                'db_table': 'muestra_enlace_farmacia',
                'ordering': ['-timestamp'],
                'indexes': [models.Index(fields=['farmacia', '-timestamp'], name='muestra_enl_farmaci_a5a651_idx')],
            },
        ),
    ]
