"""Migración de datos: Colaborador.cargo (texto libre) -> Colaborador.cargo_nueva (FK a Cargo).

Cargo requiere un Departamento (FK obligatoria); los cargos migrados desde
texto libre no traen esa información, así que se agrupan bajo un
Departamento placeholder "Sin clasificar (migrado)" que un humano puede
reasignar después desde el admin.
"""
from django.db import migrations


def migrar_cargo_a_fk(apps, schema_editor):
    Colaborador = apps.get_model('activos', 'Colaborador')
    Cargo = apps.get_model('activos', 'Cargo')
    Departamento = apps.get_model('activos', 'Departamento')

    valores = Colaborador.objects.exclude(cargo='').values_list('cargo', flat=True).distinct()
    if not valores:
        return

    depto_placeholder, _ = Departamento.objects.get_or_create(
        nombre='Sin clasificar (migrado)', defaults={'tipo': 'administrativo'},
    )
    for texto in valores:
        cargo, _ = Cargo.objects.get_or_create(nombre=texto.strip(), departamento=depto_placeholder)
        Colaborador.objects.filter(cargo=texto).update(cargo_nueva=cargo)


def revertir(apps, schema_editor):
    Colaborador = apps.get_model('activos', 'Colaborador')
    for colaborador in Colaborador.objects.exclude(cargo_nueva__isnull=True).select_related('cargo_nueva'):
        colaborador.cargo = colaborador.cargo_nueva.nombre
        colaborador.save(update_fields=['cargo'])


class Migration(migrations.Migration):

    dependencies = [
        ('activos', '0009_colaborador_cargo_directorio_colaborador_cargo_nueva_and_more'),
    ]

    operations = [
        migrations.RunPython(migrar_cargo_a_fk, revertir),
    ]
