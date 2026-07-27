"""OrdenCompra.Estado gana BORRADOR/RECEPCION_PARCIAL/CANCELADA y pierde PENDIENTE.

Las OC existentes con estado='pendiente' (el único valor viejo que no tiene
equivalente exacto en el nuevo catálogo) pasan a 'emitida', que es el estado
más cercano: ya fueron emitidas y están a la espera de recepción. 'recibida'
no cambia de valor, así que esas filas no necesitan tocarse.
"""
from django.db import migrations


def migrar_pendiente_a_emitida(apps, schema_editor):
    OrdenCompra = apps.get_model('activos', 'OrdenCompra')
    OrdenCompra.objects.filter(estado='pendiente').update(estado='emitida')


def revertir(apps, schema_editor):
    OrdenCompra = apps.get_model('activos', 'OrdenCompra')
    OrdenCompra.objects.filter(estado='emitida').update(estado='pendiente')


class Migration(migrations.Migration):

    dependencies = [
        ('activos', '0007_ordencompra_version_alter_ordencompra_estado_and_more'),
    ]

    operations = [
        migrations.RunPython(migrar_pendiente_a_emitida, revertir),
    ]
