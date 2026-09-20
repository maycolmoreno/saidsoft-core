"""Pasa los cuatro `ServicioPos` que vivían en código a filas del catálogo.

Explícita y reversible a propósito, en vez de un `choices -> FK` automático: los
`EstadoServicioPos` y `MuestraServicioPos` que ya existen guardan la clave en texto
(`pg_local`, `pg_central`, `odoo`, `recargas_soap`) y esta migración crea exactamente
esas cuatro claves. Ninguna fila de historial se toca ni queda huérfana — antes y
después de correr esto, `EstadoServicioPos.servicio` dice lo mismo.

Los cuatro nacen con `origen=config_pos` porque NO tienen destino fijo: cada estación
los descubre leyendo el `.exe.Config` real de su POS (el servidor y la base cambian de
farmacia en farmacia). La fila del catálogo existe para nombrarlos, fijar su criticidad,
poder darlos de baja y dejar la nota para quien atiende la alerta — no para decirle al
agente a dónde apuntar.

La criticidad replica la que el agente ya venía mandando (ver `_leer_servicios_pos`):
solo `pg_local` es crítico, porque sin la base local la caja no vende; sin la central
sigue vendiendo y sincroniza después.

`odoo` se siembra DESACTIVADO. Está de baja en la operación real y no había respondido
ni una vez en ninguna de las 9 estaciones con agente — el 18-sep-2026 cada una abría su
alerta por un servicio que nadie iba a arreglar. Sembrarlo activo repetiría eso el día
que alguien mire; sembrarlo desactivado conserva el historial y deja de pedirlo.
"""
from django.db import migrations

# (clave, nombre, tipo, critico, activo, nota)
SERVICIOS = [
    (
        'pg_local', 'PostgreSQL local', 'postgres', True, True,
        'La base del propio POS. Si no responde, esa caja no puede vender: es la falla '
        'mas grave de esta lista y amerita atencion inmediata.',
    ),
    (
        'pg_central', 'PostgreSQL central', 'postgres', False, True,
        'El nodo central. La caja sigue vendiendo contra su base local y sincroniza '
        'cuando vuelve, asi que no frena la operacion — pero si queda caido mucho tiempo, '
        'la farmacia trabaja con datos desactualizados.',
    ),
    (
        'odoo', 'Odoo', 'http', False, False,
        'DESACTIVADO el 19-sep-2026: el servicio esta de baja y no habia respondido ni '
        'una vez desde que se lo monitorea. Seguia figurando en el .exe.Config del POS de '
        'las 9 estaciones con agente y abria una alerta por estacion que nadie iba a '
        'atender. Reactivar solo si Odoo vuelve a la operacion.',
    ),
    (
        'recargas_soap', 'Web service de recargas', 'http', False, True,
        'Recargas de celular. Si no responde, la caja vende todo lo demas con normalidad '
        'y solo falla ese producto.',
    ),
]


def sembrar(apps, schema_editor):
    ServicioPosMonitoreado = apps.get_model('monitoreo', 'ServicioPosMonitoreado')
    for clave, nombre, tipo, critico, activo, nota in SERVICIOS:
        # get_or_create y no create: la migracion tiene que poder correrse sobre una base
        # donde alguien ya cargo la entrada a mano desde el admin.
        ServicioPosMonitoreado.objects.get_or_create(
            clave=clave,
            defaults={
                'nombre': nombre, 'tipo': tipo, 'origen': 'config_pos',
                'destino': '', 'critico': critico, 'activo': activo, 'nota': nota,
            },
        )


def revertir(apps, schema_editor):
    """Borra SOLO las cuatro sembradas acá, y solo si nadie las edito.

    No se borra el catalogo entero: para cuando alguien revierta esto puede haber
    entradas agregadas desde el admin, que son justamente lo que este trabajo vino a
    habilitar. Tampoco se toca ningun EstadoServicioPos: su `servicio` es texto libre y
    sigue siendo valido sin la fila del catalogo (solo deja de aceptarse en reportes
    nuevos, que es el comportamiento correcto al revertir).
    """
    ServicioPosMonitoreado = apps.get_model('monitoreo', 'ServicioPosMonitoreado')
    ServicioPosMonitoreado.objects.filter(
        clave__in=[s[0] for s in SERVICIOS], origen='config_pos',
    ).delete()


class Migration(migrations.Migration):

    dependencies = [
        ('monitoreo', '0031_catalogo_servicios_pos'),
    ]

    operations = [
        migrations.RunPython(sembrar, revertir),
    ]
