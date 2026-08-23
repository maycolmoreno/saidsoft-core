"""Siembra los 5 tipos conceptuales de docs/proceso-mantenimiento-ti.md (brecha #3,
23-ago-2026) apenas se aplica la migración -- sin esto, el catálogo nace vacío y nadie
podría clasificar un mantenimiento hasta que un administrador entre a /admin/ a
cargarlos a mano. Idempotente (get_or_create) para poder correr `migrate` de nuevo sin
duplicar filas."""
from django.db import migrations

TIPOS = [
    ('preventivo', 'Preventivo', 'Mantenimiento programado por frecuencia, sin falla reportada.'),
    ('correctivo', 'Correctivo', 'El equipo funciona pero con una falla puntual reportada.'),
    ('falla_critica', 'Falla crítica', 'El equipo dejó de operar (PDV caído, no enciende).'),
    ('actualizacion', 'Actualización tecnológica', 'Cambio de SO, upgrade de hardware, migración de versión de POS.'),
    ('obsolescencia', 'Obsolescencia', 'El equipo cumplió su vida útil o quedó fuera de soporte.'),
]


def sembrar(apps, schema_editor):
    TipoMantenimiento = apps.get_model('mantenimiento', 'TipoMantenimiento')
    for codigo, nombre, descripcion in TIPOS:
        TipoMantenimiento.objects.get_or_create(codigo=codigo, defaults={'nombre': nombre, 'descripcion': descripcion})


def revertir(apps, schema_editor):
    TipoMantenimiento = apps.get_model('mantenimiento', 'TipoMantenimiento')
    TipoMantenimiento.objects.filter(codigo__in=[codigo for codigo, _, _ in TIPOS]).delete()


class Migration(migrations.Migration):

    dependencies = [
        ('mantenimiento', '0010_tipomantenimiento_and_more'),
    ]

    operations = [
        migrations.RunPython(sembrar, revertir),
    ]
