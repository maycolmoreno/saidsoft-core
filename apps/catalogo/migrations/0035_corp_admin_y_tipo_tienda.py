"""Deja el terreno listo para los sitios administrativos, sin tocar lo que ya funciona.

Tres cosas, todas idempotentes (`get_or_create` / `update` acotado), mismo criterio que
la 0008 que sembró SG y MIA: correr `migrate` de nuevo no cambia nada la segunda vez.

1. Las tiendas de 7DIAS pasan a `tipo='tienda'`. Ya estaban ahí —la tabla nunca fue
   exclusiva de farmacias— pero el tipo lo decía solo el nombre ("TIENDAS 7DM001").
   El resto queda en `farmacia`, que es el default del campo: cero cambio de
   comportamiento para las 700 existentes.

2. `UnidadNegocio` CORP. Administración no es "de" San Gregorio ni de MIA ni de 7DIAS, y
   `Farmacia.unidad_negocio` no acepta nulo. Una cuarta unidad es la forma que el modelo
   ya tiene de decir "esto es de la empresa, no de una cadena".

   Ojo con el efecto inmediato: los 9 técnicos reales tienen `acceso_todas_unidades=True`
   (ver crear_tecnicos_soporte), así que van a VER los sitios CORP apenas existan. Es lo
   buscado; no es que queden ocultos.

3. `Grupo` ADMIN. `Farmacia.grupo` es obligatorio y significa "canal de versión del POS
   y nodo al que apunta el sitio". Una oficina no tiene POS, así que necesita un grupo
   propio que NUNCA reciba un despliegue: separarlo es lo que hace imposible mandarle
   una versión de POS a una PC administrativa por un filtro mal puesto.

Se imprime lo que se toca antes de tocarlo: en producción 7DIAS existe y en desarrollo
no, así que el conteo dice de una si la migración hizo lo que se esperaba.
"""
from django.db import migrations


def preparar(apps, schema_editor):
    Farmacia = apps.get_model('catalogo', 'Farmacia')
    Grupo = apps.get_model('catalogo', 'Grupo')
    UnidadNegocio = apps.get_model('catalogo', 'UnidadNegocio')

    # 1. Tiendas de 7DIAS.
    tiendas = Farmacia.objects.filter(unidad_negocio__codigo='7DIAS')
    cuantas = tiendas.count()
    if cuantas:
        tiendas.update(tipo='tienda')
    print(f'\n  Sitios marcados como tienda (unidad 7DIAS): {cuantas}')

    # 2. Unidad de negocio de la empresa.
    corp, creada = UnidadNegocio.objects.get_or_create(
        codigo='CORP', defaults={'nombre': 'Cresio — administración'},
    )
    print(f'  UnidadNegocio CORP: {"creada" if creada else "ya existía"}')

    # 3. Grupo sin POS.
    _, creado = Grupo.objects.get_or_create(
        codigo='ADMIN',
        defaults={'nombre': 'Administración (sin POS: nunca recibe despliegues)'},
    )
    print(f'  Grupo ADMIN: {"creado" if creado else "ya existía"}')


def revertir(apps, schema_editor):
    """Solo se deshace lo que no puede tener nada colgando.

    CORP y ADMIN se borran únicamente si ningún sitio los usa: si alguien ya cargó los
    departamentos, borrarlos fallaría por la FK PROTECT, y forzarlo sería peor. Las
    tiendas vuelven a `farmacia`, que es el default del campo.
    """
    Farmacia = apps.get_model('catalogo', 'Farmacia')
    Grupo = apps.get_model('catalogo', 'Grupo')
    UnidadNegocio = apps.get_model('catalogo', 'UnidadNegocio')

    Farmacia.objects.filter(tipo='tienda').update(tipo='farmacia')
    UnidadNegocio.objects.filter(codigo='CORP', farmacias__isnull=True).delete()
    Grupo.objects.filter(codigo='ADMIN', farmacias__isnull=True).delete()


class Migration(migrations.Migration):

    dependencies = [
        ('catalogo', '0034_sitios_administrativos'),
    ]

    operations = [
        migrations.RunPython(preparar, revertir),
    ]
