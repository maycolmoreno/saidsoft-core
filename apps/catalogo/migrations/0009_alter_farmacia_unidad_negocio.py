# R1 (multi-tenancy): Farmacia.unidad_negocio pasa de opcional a obligatoria.
#
# IMPORTANTE: si esta migración se corre contra una base con farmacias sin
# unidad_negocio asignada, fallará con IntegrityError (a propósito — la
# asignación de tenant es una decisión de negocio, no algo que se pueda
# inferir). Antes de desplegar esto en un entorno con datos reales, correr:
#   python manage.py backfill_unidad_negocio
# y completar manualmente lo que reporte como pendiente.
import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('catalogo', '0008_unidadnegocio_farmacia_fecha_apertura_and_more'),
    ]

    operations = [
        migrations.AlterField(
            model_name='farmacia',
            name='unidad_negocio',
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT, related_name='farmacias', to='catalogo.unidadnegocio',
                help_text='Cliente/cadena dueña de esta farmacia. Define el aislamiento de datos (tenant): '
                          'una farmacia siempre pertenece a una única unidad de negocio.',
            ),
        ),
    ]
