"""Siembra el set inicial de eventos de Windows a vigilar.

Los diez salen de qué predice o explica que una caja deje de vender, no de una lista
genérica de "eventos importantes de Windows". Cada uno tiene su nota escrita para que
quien atiende la alerta a las 3 de la mañana sepa qué mirar primero.

Solo DOS abren alerta —disco y apagón— y es deliberado. Medido sobre una máquina real
en 30 días: 260 eventos de `Ntfs 55`, 15 de `Application Error 1000`. Alertar por todo
es exactamente cómo se consigue que dejen de mirarse las alertas; el resto se guarda y
se consulta cuando hay un problema que explicar.

Explícita y reversible: la reversa borra solo estas diez y solo si nadie las edito.
"""
from django.db import migrations

# (log, proveedor, id, nombre, severidad, abre_alerta, nota)
#
# El PROVEEDOR no es opcional. Comprobado contra un visor real el 20-sep-2026: pidiendo
# solo por ID, `System 55` devolvio 260 avisos de "Microsoft-Windows-Kernel-Processor-Power"
# (administracion de energia del procesador) cuando se esperaba corrupcion de NTFS, y
# `System 153` devolvio "Microsoft-Windows-Kernel-Boot" en vez de reintentos de disco.
# Sin el proveedor esto recolecta otra cosa y nadie se entera.
EVENTOS = [
    (
        'System', 'Microsoft-Windows-Kernel-Power', 41, 'Apagón inesperado', 'critical', True,
        'El equipo se apago sin apagado limpio: corte de luz, cuelgue duro, o alguien '
        'desenchufo. Es lo mas parecido que hay a saber si una caida fue de ENERGIA y no '
        'del enlace -- no se sabe durante el corte (el equipo esta muerto), se sabe cuando '
        'vuelve. Si la farmacia estuvo caida y aparece esto, el ticket va a electricidad, '
        'no al proveedor de internet.',
    ),
    (
        'System', 'disk', 7, 'Error de disco (bloque defectuoso)', 'critical', True,
        'El disco reporto un bloque que no pudo leer. Es de los pocos avisos que llegan '
        'ANTES de la falla: conviene reemplazar antes de que la caja se quede sin sistema '
        'a mitad de una venta.',
    ),
    (
        'System', 'disk', 51, 'Error de paginación en el disco', 'critical', True,
        'Windows no pudo escribir en el disco. Suele preceder a la corrupcion del sistema '
        'de archivos y a que el POS empiece a fallar de formas raras.',
    ),
    (
        'System', 'disk', 52, 'El disco avisa que va a fallar (SMART)', 'critical', True,
        'El propio disco predice su falla. Es el aviso mas claro que existe: programar el '
        'reemplazo ya, no esperar.',
    ),
    (
        'System', 'disk', 153, 'Reintento de E/S en el disco', 'warning', False,
        'El disco tardo tanto que Windows reintento. Uno suelto no dice nada; que crezca '
        'la cuenta si. Se guarda para ver la tendencia.',
    ),
    (
        'System', 'Ntfs', 55, 'Corrupción en el sistema de archivos (NTFS)', 'warning', False,
        'NTFS encontro inconsistencias. Muy ruidoso -- 260 en 30 dias en una sola maquina '
        'medidos el 20-sep-2026 -- asi que NO abre alerta. Sirve como contexto cuando ya '
        'hay un problema, y para decidir si conviene un chkdsk.',
    ),
    (
        'System', 'Service Control Manager', 7031, 'Un servicio se murió inesperadamente', 'warning', False,
        'Un servicio de Windows termino solo. Importa cual: si es el del POS o el del '
        'agente, explica por que esa caja dejo de reportar.',
    ),
    (
        'System', 'Service Control Manager', 7034, 'Un servicio murió varias veces', 'warning', False,
        'El mismo servicio viene cayendose repetido. Ya no es un incidente, es algo '
        'sistematico que hay que corregir.',
    ),
    (
        'Application', 'Application Error', 1000, 'Una aplicación crasheó', 'warning', False,
        'Crash de una aplicacion. Cuando el nombre es el del POS, esto es la causa real '
        'detras de "se me cerro el sistema" -- con el modulo exacto que fallo.',
    ),
    (
        'System', 'Microsoft-Windows-WHEA-Logger', 17, 'Error de hardware corregido (WHEA)', 'warning', False,
        'El hardware tuvo un error y lo corrigio solo. Aislado no amerita nada; repetido '
        'suele anteceder a una falla de memoria o de CPU.',
    ),
]


def sembrar(apps, schema_editor):
    Vigilado = apps.get_model('monitoreo', 'EventoSistemaVigilado')
    for log, proveedor, identificador, nombre, severidad, abre_alerta, nota in EVENTOS:
        # get_or_create: la migracion tiene que poder correr sobre una base donde alguien
        # ya cargo el evento a mano desde el admin.
        Vigilado.objects.get_or_create(
            log=log, proveedor=proveedor, identificador=identificador,
            defaults={
                'nombre': nombre, 'severidad': severidad,
                'abre_alerta': abre_alerta, 'activo': True, 'nota': nota,
            },
        )


def revertir(apps, schema_editor):
    """Borra solo estos diez. No se toca `EventoSistemaDetectado`: su clave es texto y
    numero, asi que el historial sigue siendo valido sin la fila del catalogo."""
    Vigilado = apps.get_model('monitoreo', 'EventoSistemaVigilado')
    for log, proveedor, identificador, *_ in EVENTOS:
        Vigilado.objects.filter(log=log, proveedor=proveedor, identificador=identificador).delete()


class Migration(migrations.Migration):

    dependencies = [
        ('monitoreo', '0033_eventos_windows'),
    ]

    operations = [
        migrations.RunPython(sembrar, revertir),
    ]
