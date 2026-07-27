"""Migración de datos: Activo.marca (texto libre) -> Activo.marca_nueva (FK a Marca).

Por cada valor distinto ya sembrado en el CharField `marca`, crea (o reutiliza)
el catálogo `Marca` correspondiente y enlaza los Activo existentes. El campo
CharField viejo se elimina y `marca_nueva` se renombra a `marca` en la
migración 0005, una vez que los datos ya están del lado seguro.
"""
from django.db import migrations


def migrar_marca_a_fk(apps, schema_editor):
    Activo = apps.get_model('activos', 'Activo')
    Marca = apps.get_model('activos', 'Marca')

    valores = (
        Activo.objects.exclude(marca='').values_list('marca', flat=True).distinct()
    )
    for texto in valores:
        marca, _ = Marca.objects.get_or_create(nombre=texto.strip())
        Activo.objects.filter(marca=texto).update(marca_nueva=marca)


def revertir(apps, schema_editor):
    Activo = apps.get_model('activos', 'Activo')
    for activo in Activo.objects.exclude(marca_nueva__isnull=True).select_related('marca_nueva'):
        activo.marca = activo.marca_nueva.nombre
        activo.save(update_fields=['marca'])


class Migration(migrations.Migration):

    dependencies = [
        ('activos', '0003_activo_almacenamiento_gb_activo_baja_recomendada_and_more'),
    ]

    operations = [
        migrations.RunPython(migrar_marca_a_fk, revertir),
    ]
