from django.db import migrations

# Valores iniciales, pensados para una operación de farmacias donde una caja parada
# es venta perdida. Son un punto de partida configurable desde /admin/ (mismo criterio
# que TipoMantenimiento): el área de TI los ajusta sin tocar código.
#
# NORMAL queda en 72h de resolución a propósito: es exactamente el umbral global que
# usaba mantenimientos_atrasados() antes de esto (DIAS_GRACIA_ATRASADO = 3), así que
# los mantenimientos sin prioridad explícita se siguen comportando igual que antes.
ACUERDOS = [
    # (prioridad, horas_respuesta, horas_resolucion)
    ('critica', 1, 4),
    ('alta', 4, 24),
    ('normal', 24, 72),
    ('baja', 72, 168),
]


def sembrar(apps, schema_editor):
    AcuerdoNivelServicio = apps.get_model('mantenimiento', 'AcuerdoNivelServicio')
    for prioridad, respuesta, resolucion in ACUERDOS:
        AcuerdoNivelServicio.objects.get_or_create(
            prioridad=prioridad,
            defaults={'horas_respuesta': respuesta, 'horas_resolucion': resolucion, 'activo': True},
        )


def borrar(apps, schema_editor):
    AcuerdoNivelServicio = apps.get_model('mantenimiento', 'AcuerdoNivelServicio')
    AcuerdoNivelServicio.objects.filter(prioridad__in=[p for p, _, _ in ACUERDOS]).delete()


class Migration(migrations.Migration):

    dependencies = [
        ('mantenimiento', '0012_acuerdonivelservicio_mantenimiento_prioridad'),
    ]

    operations = [
        migrations.RunPython(sembrar, borrar),
    ]
