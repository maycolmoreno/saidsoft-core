import django.db.models.deletion
from django.db import migrations, models


def backfill_unidad_negocio(apps, schema_editor):
    """Infiere la unidad_negocio de cada Despliegue existente a partir de su destino
    ya guardado (grupos/farmacias/estaciones). Si el destino resuelve a una sola
    unidad de negocio, se usa esa. Si es ambiguo (destino "cadena", varias unidades
    mezcladas, o sin datos resolubles) se asigna la primera unidad de negocio como
    valor de arranque para no romper la migración, dejando constancia en el log de
    migración de que requiere revisión manual antes de operar con clientes reales.
    """
    Despliegue = apps.get_model('despliegues', 'Despliegue')
    Farmacia = apps.get_model('catalogo', 'Farmacia')
    UnidadNegocio = apps.get_model('catalogo', 'UnidadNegocio')

    fallback = UnidadNegocio.objects.order_by('codigo').first()
    if fallback is None:
        return

    ambiguos = []
    for despliegue in Despliegue.objects.all():
        if despliegue.destino_tipo == 'grupos':
            farmacias = Farmacia.objects.filter(grupo__in=despliegue.grupos.all())
        elif despliegue.destino_tipo == 'farmacias':
            farmacias = despliegue.farmacias.all()
        elif despliegue.destino_tipo == 'estaciones':
            farmacias = Farmacia.objects.filter(estaciones__in=despliegue.estaciones.all()).distinct()
        else:  # 'cadena': por definición abarca toda la base, no resoluble a una unidad
            farmacias = Farmacia.objects.none()

        unidades_ids = set(
            farmacias.exclude(unidad_negocio__isnull=True).values_list('unidad_negocio_id', flat=True)
        )
        if len(unidades_ids) == 1:
            despliegue.unidad_negocio_id = unidades_ids.pop()
        else:
            despliegue.unidad_negocio_id = fallback.id
            ambiguos.append(despliegue.pk)
        despliegue.save(update_fields=['unidad_negocio_id'])

    if ambiguos:
        print(
            f'\n  ADVERTENCIA: {len(ambiguos)} despliegue(s) con tenant ambiguo se '
            f'asignaron a "{fallback.codigo}" por defecto — revisar manualmente: {ambiguos}\n'
        )


class Migration(migrations.Migration):

    dependencies = [
        ('despliegues', '0003_despliegue_freno_omitido'),
        ('catalogo', '0009_alter_farmacia_unidad_negocio'),
    ]

    operations = [
        migrations.AddField(
            model_name='despliegue',
            name='unidad_negocio',
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT, related_name='despliegues',
                to='catalogo.unidadnegocio', null=True,
                help_text='Cliente al que se dirige este despliegue. "Toda la cadena" significa toda '
                          'la cadena de esta unidad de negocio, nunca de otras.',
            ),
        ),
        migrations.RunPython(backfill_unidad_negocio, migrations.RunPython.noop),
        migrations.AlterField(
            model_name='despliegue',
            name='unidad_negocio',
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT, related_name='despliegues',
                to='catalogo.unidadnegocio',
                help_text='Cliente al que se dirige este despliegue. "Toda la cadena" significa toda '
                          'la cadena de esta unidad de negocio, nunca de otras.',
            ),
        ),
    ]
