from django.db import migrations

# Checklist base de un mantenimiento preventivo de estación de farmacia. Se siembra
# por migración (mismo criterio que TipoMantenimiento y los SLA) porque sin ítems la
# pantalla de checklist de la app móvil queda vacía y el técnico no tiene qué marcar:
# el catálogo estaba en 0 en producción.
#
# Es un punto de partida editable desde /admin/, no una lista cerrada.
ACTIVIDADES = [
    (10, 'Limpieza física del equipo (gabinete, ventilación)'),
    (20, 'Limpieza de periféricos (teclado, mouse, lector)'),
    (30, 'Revisión de conexiones y cableado'),
    (40, 'Verificación de encendido y arranque'),
    (50, 'Revisión de espacio en disco'),
    (60, 'Verificación de antivirus activo y actualizado'),
    (70, 'Verificación de actualizaciones de Windows'),
    (80, 'Prueba de conectividad de red'),
    (90, 'Prueba de impresión (facturación / tickets)'),
    (100, 'Verificación de apertura del POS'),
    (110, 'Prueba de lector de código de barras'),
    (120, 'Verificación de respaldo / sincronización'),
    (130, 'Revisión de UPS y energía'),
    (140, 'Verificación de fecha y hora del equipo'),
]


def sembrar(apps, schema_editor):
    ActividadChecklist = apps.get_model('mantenimiento', 'ActividadChecklist')
    for orden, nombre in ACTIVIDADES:
        ActividadChecklist.objects.get_or_create(
            nombre=nombre, defaults={'orden': orden, 'activo': True},
        )


def borrar(apps, schema_editor):
    ActividadChecklist = apps.get_model('mantenimiento', 'ActividadChecklist')
    ActividadChecklist.objects.filter(nombre__in=[n for _, n in ACTIVIDADES]).delete()


class Migration(migrations.Migration):

    dependencies = [
        ('mantenimiento', '0016_visitatecnica_mantenimiento_visita'),
    ]

    operations = [
        migrations.RunPython(sembrar, borrar),
    ]
