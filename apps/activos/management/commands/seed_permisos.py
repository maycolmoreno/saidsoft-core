from django.contrib.auth.models import Group, Permission
from django.core.management.base import BaseCommand
from django.db import transaction

# Roles del sistema, mapeados a Group + permisos de Django en vez de un modelo Rol/Modulo
# paralelo. Administrador/Técnico/Bodeguero/Auditor/Operador RMM vienen de InvTICS (RolesJpa);
# Mesa de Ayuda/Soporte Técnico son nuevos, del modelo de soporte del piloto RMM. Cada tupla
# de ROLES es (app_label, nombre_modelo, [acciones]).
ROLES = {
    'Administrador': None,  # None = todos los permisos existentes
    'Técnico': [
        ('activos', 'activo', ['view', 'change']),
        ('activos', 'eventoactivo', ['view', 'add']),
        ('activos', 'ubicacion', ['view']),
        ('activos', 'colaborador', ['view']),
    ],
    'Bodeguero': [
        ('activos', 'activo', ['view', 'add', 'change']),
        ('activos', 'bodega', ['view', 'change']),
        ('activos', 'stockbodega', ['view', 'add', 'change']),
        ('activos', 'ordencompra', ['view', 'add', 'change']),
    ],
    'Auditor': [
        ('activos', 'activo', ['view']),
        ('activos', 'eventoactivo', ['view']),
        ('auditoria', 'eventoauditoria', ['view']),
        ('despliegues', 'despliegue', ['view']),
    ],
    # Ejecutar scripts es una superficie de riesgo distinta a los demás roles operativos
    # (código arbitrario en la flota): grupo propio, no se agrega a Técnico/Bodeguero por defecto.
    'Operador RMM': [
        ('scripts', 'script', ['view', 'add', 'change']),
        ('scripts', 'ejecucionscript', ['view', 'add']),
        ('monitoreo', 'ventanamantenimiento', ['view', 'add', 'change']),
    ],
    # Modelo de soporte del piloto RMM (no viene de InvTICS): mesa de ayuda = primera línea,
    # solo diagnóstico sobre el PDV; soporte técnico = segunda línea, con las acciones de
    # riesgo (reiniciar, aprobar enrolamiento, parchar, correr scripts). Cada agente real
    # tiene su propio usuario (auth.User) en vez de compartir credenciales — todas las
    # acciones quedan atribuidas vía apps.auditoria.registrar_evento.
    'Mesa de Ayuda': [],
    'Soporte Técnico': [
        ('scripts', 'script', ['view', 'add']),
        ('scripts', 'ejecucionscript', ['view', 'add']),
        ('scripts', 'scriptprogramado', ['view', 'add', 'change']),
    ],
}

# Permisos custom que no calzan en el patrón CRUD de ROLES (su codename no es
# "{accion}_{model_name}"), por rol que los necesite además de Administrador
# (que ya los tiene todos vía `None` arriba). codename literal: (app_label, codename).
PERMISOS_LITERALES = {
    'Auditor': [
        ('catalogo', 'supervision_auditoria_estacion'),  # grabaciones de sesión, no soporte en vivo
    ],
    'Mesa de Ayuda': [
        ('catalogo', 'acceso_remoto_estacion'),  # guiar al usuario del PDV por escritorio remoto
        ('catalogo', 'consultar_info_estacion'),  # pedir refresco de hardware/BitLocker
    ],
    'Soporte Técnico': [
        ('catalogo', 'acceso_remoto_estacion'),
        ('catalogo', 'consultar_info_estacion'),
        ('catalogo', 'aprobar_estacion'),
        ('catalogo', 'reiniciar_estacion'),
        ('catalogo', 'escanear_actualizaciones_estacion'),
        # ver_clave_bitlocker y supervision_auditoria_estacion quedan fuera a propósito,
        # mismo criterio que el resto del proyecto (ver comentarios en catalogo/models.py):
        # son más sensibles que el resto y se otorgan persona por persona, no por grupo.
    ],
}


class Command(BaseCommand):
    help = (
        'Crea los Groups de roles (los heredados de InvTICS — Administrador/Técnico/Bodeguero/'
        'Auditor/Operador RMM — más Mesa de Ayuda/Soporte Técnico del modelo de soporte del piloto '
        'RMM) y les asigna los permisos de Django correspondientes. No hay modelo de Rol propio, '
        'se usa el sistema de permisos estándar de Django.'
    )

    @transaction.atomic
    def handle(self, *args, **options):
        for nombre_rol, reglas in ROLES.items():
            grupo, creado = Group.objects.get_or_create(name=nombre_rol)
            if reglas is None:
                grupo.permissions.set(Permission.objects.all())
            else:
                permisos = []
                for app_label, model_name, acciones in reglas:
                    for accion in acciones:
                        # Asume el patrón CRUD estándar de Django (view_x/add_x/...).
                        codename = f'{accion}_{model_name}'
                        try:
                            permisos.append(
                                Permission.objects.get(content_type__app_label=app_label, codename=codename),
                            )
                        except Permission.DoesNotExist:
                            self.stderr.write(self.style.WARNING(
                                f'Permiso {app_label}.{codename} no existe (¿falta una migración?), se omite.',
                            ))
                for app_label, codename in PERMISOS_LITERALES.get(nombre_rol, []):
                    try:
                        permisos.append(
                            Permission.objects.get(content_type__app_label=app_label, codename=codename),
                        )
                    except Permission.DoesNotExist:
                        self.stderr.write(self.style.WARNING(
                            f'Permiso {app_label}.{codename} no existe (¿falta una migración?), se omite.',
                        ))
                grupo.permissions.set(permisos)
            verbo = 'creado' if creado else 'actualizado'
            self.stdout.write(self.style.SUCCESS(f'Group "{nombre_rol}" {verbo} con {grupo.permissions.count()} permiso(s).'))
