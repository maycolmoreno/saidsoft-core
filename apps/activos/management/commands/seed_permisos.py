from django.contrib.auth.models import Group, Permission
from django.core.management.base import BaseCommand
from django.db import transaction

# Roles observados en InvTICS (RolesJpa), mapeados a Group + permisos de Django
# en vez de un modelo Rol/Modulo paralelo. Cada tupla es (nombre_modelo_app, [acciones]).
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
}


class Command(BaseCommand):
    help = (
        'Crea los Groups equivalentes a los roles de InvTICS (Administrador/Técnico/Bodeguero/Auditor) '
        'y les asigna los permisos de Django correspondientes. Reemplaza a RolesJpa/ModuloJpa: no hay '
        'modelo de Rol propio, se usa el sistema de permisos estándar de Django.'
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
                        codename = f'{accion}_{model_name}'
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
