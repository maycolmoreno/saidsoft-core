"""Una firma por tipo y por mantenimiento (BUG-4, ver docs/modulos.md).

La constraint sola no se puede aplicar a ciegas: si en producción ya quedaron firmas
duplicadas por reintentos de la cola offline, `AddConstraint` falla y el despliegue se
corta a mitad de camino. Por eso primero se limpia.

Criterio de limpieza: se conserva la MÁS RECIENTE de cada par
(mantenimiento, tipo_firma) y se borran las anteriores. Un duplicado acá es siempre el
mismo hecho registrado dos veces —el técnico firmó una vez y la acción se reintentó—,
así que no se pierde información. Se imprime lo que se borra antes de borrarlo: el
proyecto ya se quemó con un inventario de "huérfanos" que marcó como sobrante el agente
vigente (ver CLAUDE.md), y una migración es justo donde eso no se puede deshacer.
"""
from django.conf import settings
from django.db import migrations, models


def quitar_firmas_duplicadas(apps, schema_editor):
    FirmaMantenimiento = apps.get_model('mantenimiento', 'FirmaMantenimiento')

    vistos = set()
    a_borrar = []
    # Más reciente primero: el primero de cada par es el que se conserva.
    for firma in FirmaMantenimiento.objects.order_by('-firmado_en', '-id').iterator():
        clave = (firma.mantenimiento_id, firma.tipo_firma)
        if clave in vistos:
            a_borrar.append(firma.pk)
        else:
            vistos.add(clave)

    if not a_borrar:
        return

    print(
        f'\n  Firmas duplicadas a borrar: {len(a_borrar)} '
        f'(se conserva la mas reciente de cada mantenimiento/tipo).',
    )
    for firma in FirmaMantenimiento.objects.filter(pk__in=a_borrar):
        print(
            f'    - id={firma.pk} mantenimiento={firma.mantenimiento_id} '
            f'tipo={firma.tipo_firma} firmado_en={firma.firmado_en:%Y-%m-%d %H:%M:%S}',
        )
    FirmaMantenimiento.objects.filter(pk__in=a_borrar).delete()


def sin_reversa(apps, schema_editor):
    """Las filas borradas no vuelven. Quitar la constraint sí se puede (lo hace
    `AddConstraint` al revertirse); recrear los duplicados no tendría sentido."""


class Migration(migrations.Migration):

    dependencies = [
        ('mantenimiento', '0021_hora_real_de_acciones_offline'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.RunPython(quitar_firmas_duplicadas, sin_reversa),
        migrations.AddConstraint(
            model_name='firmamantenimiento',
            constraint=models.UniqueConstraint(fields=('mantenimiento', 'tipo_firma'), name='firma_unica_por_tipo'),
        ),
    ]
