import django.db.models.deletion
from django.db import migrations, models


def backfill_unidad_negocio(apps, schema_editor):
    """Mismo criterio que apps/despliegues/migrations/0004_despliegue_unidad_negocio.py:
    infiere la unidad_negocio de cada EjecucionScript existente a partir de su destino
    ya guardado; si es ambiguo, cae a la primera unidad de negocio y lo deja anotado
    para revisión manual."""
    EjecucionScript = apps.get_model('scripts', 'EjecucionScript')
    Farmacia = apps.get_model('catalogo', 'Farmacia')
    UnidadNegocio = apps.get_model('catalogo', 'UnidadNegocio')

    fallback = UnidadNegocio.objects.order_by('codigo').first()
    if fallback is None:
        return

    ambiguos = []
    for ejecucion in EjecucionScript.objects.all():
        if ejecucion.destino_tipo == 'grupos':
            farmacias = Farmacia.objects.filter(grupo__in=ejecucion.grupos.all())
        elif ejecucion.destino_tipo == 'farmacias':
            farmacias = ejecucion.farmacias.all()
        elif ejecucion.destino_tipo == 'estaciones':
            farmacias = Farmacia.objects.filter(estaciones__in=ejecucion.estaciones.all()).distinct()
        else:  # 'cadena'
            farmacias = Farmacia.objects.none()

        unidades_ids = set(
            farmacias.exclude(unidad_negocio__isnull=True).values_list('unidad_negocio_id', flat=True)
        )
        if len(unidades_ids) == 1:
            ejecucion.unidad_negocio_id = unidades_ids.pop()
        else:
            ejecucion.unidad_negocio_id = fallback.id
            ambiguos.append(ejecucion.pk)
        ejecucion.save(update_fields=['unidad_negocio_id'])

    if ambiguos:
        print(
            f'\n  ADVERTENCIA: {len(ambiguos)} ejecución(es) de script con tenant ambiguo se '
            f'asignaron a "{fallback.codigo}" por defecto — revisar manualmente: {ambiguos}\n'
        )


class Migration(migrations.Migration):

    dependencies = [
        ('scripts', '0001_initial'),
        ('catalogo', '0009_alter_farmacia_unidad_negocio'),
    ]

    operations = [
        migrations.AddField(
            model_name='script',
            name='unidad_negocio',
            field=models.ForeignKey(
                blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='scripts',
                to='catalogo.unidadnegocio',
                help_text='Vacío = script compartido, visible y ejecutable por cualquier cliente. '
                          'Con valor = script privado de esa unidad de negocio.',
            ),
        ),
        migrations.AddField(
            model_name='ejecucionscript',
            name='unidad_negocio',
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT, related_name='ejecuciones_script',
                to='catalogo.unidadnegocio', null=True,
                help_text='Cliente al que se dirige esta ejecución. "Toda la cadena" significa toda '
                          'la cadena de esta unidad de negocio, nunca de otras.',
            ),
        ),
        migrations.RunPython(backfill_unidad_negocio, migrations.RunPython.noop),
        migrations.AlterField(
            model_name='ejecucionscript',
            name='unidad_negocio',
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT, related_name='ejecuciones_script',
                to='catalogo.unidadnegocio',
                help_text='Cliente al que se dirige esta ejecución. "Toda la cadena" significa toda '
                          'la cadena de esta unidad de negocio, nunca de otras.',
            ),
        ),
    ]
