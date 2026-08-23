"""Alta de los técnicos de soporte de campo (Colaborador + login real al panel).

Los datos de estas 9 personas reales (cédula, correo corporativo, cargo) se
recibieron directamente del usuario por chat una sola vez (22-ago-2026, ver
PLAN_MODERNIZACION.md) — no vienen de ningún archivo, así que quedan hardcodeados acá
como fuente de verdad de este alta puntual. Idempotente por cédula: correr de nuevo no
duplica gente ni pisa un login que ya exista.

Cada técnico entra al grupo "Soporte Técnico" (ver apps.activos.management.commands.
seed_permisos) — NO Administrador, aunque el sistema de RRHH de origen los marque
"rol: Admin": ese campo es de otro sistema y no corresponde traducirlo a acceso total
en SAIDSOFT. Soporte Técnico ya tiene exactamente lo que necesitan para su función
(aprobar/reiniciar estaciones, acceso remoto, ejecutar scripts) — mínimo privilegio.
"""
import secrets

from django.contrib.auth.models import Group, User
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from apps.activos.models import Cargo, Colaborador, Departamento

DEPARTAMENTO_NOMBRE = 'Tecnologías e Innovación'

TECNICOS = [
    dict(
        nombre='Carranza Cedeño Jaime Leonerys', cedula='1312655291', correo='jaime.carranza@cresio.com',
        telefono='0939364870', cargo='Asistente de Soporte Técnico', ciudad='', provincia='',
    ),
    dict(
        nombre='Alvarez Mendoza Wellington Mauricio', cedula='1314821941', correo='wellington.alvarez@cresio.com',
        telefono='0991788372', cargo='Asistente de Soporte Técnico', ciudad='', provincia='Manabi',
    ),
    dict(
        nombre='Figueroa Parraga Luis Miguel', cedula='1310909906', correo='luis.figueroa@cresio.com',
        telefono='0986086237', cargo='Supervisor Regional de Soporte Técnico', ciudad='', provincia='',
    ),
    dict(
        nombre='Aguilar Peña Diego Fabricio', cedula='0706884947', correo='diego.aguilar@cresio.com',
        telefono='0999512823', cargo='Supervisor Regional de Soporte Técnico', ciudad='', provincia='El Oro',
    ),
    dict(
        nombre='Villacres Cango Hjalmar Leonel', cedula='0706362621', correo='leonel.villacres@cresio.com',
        telefono='', cargo='Asistente de Soporte Técnico', ciudad='Santa Rosa', provincia='El Oro',
    ),
    dict(
        nombre='Picon Barros Justin Mateo', cedula='0107627846', correo='mateo.picon@cresio.com',
        telefono='0969941945', cargo='Asistente de Soporte Técnico', ciudad='', provincia='Azuay',
    ),
    dict(
        nombre='Lopez Barros Luis Adrian', cedula='0706390663', correo='luis.lopez@cresio.com',
        telefono='0988835971', cargo='Asistente de Soporte Técnico', ciudad='Machala', provincia='El Oro',
    ),
    dict(
        nombre='Lema Simbaña Alex Leonel', cedula='1750887059', correo='alex.lema@cresio.com',
        telefono='0988844091', cargo='Asistente de Soporte Técnico', ciudad='Ambato', provincia='Tungurahua',
    ),
    dict(
        nombre='Dumes Armijos Daniel Josue', cedula='0706009370', correo='daniel.dumes@cresio.com',
        telefono='0989666290', cargo='Asistente de Soporte Técnico', ciudad='', provincia='',
    ),
]

# Mapeo entre el nombre corto usado en la columna "Tecnico" del directorio de
# sucursales (agosto 2026) y la cédula real -- varias personas figuran ahí por su
# segundo nombre (ej. "MAURICIO ALVAREZ" es Wellington Mauricio Alvarez Mendoza), así
# que no alcanza con comparar texto: apps.catalogo.services.importar_directorio_sucursales
# usa este mapeo para vincular Farmacia.tecnico_asignado a la cédula correcta.
NOMBRE_DIRECTORIO_A_CEDULA = {
    'JAIME CARRANZA': '1312655291',
    'MAURICIO ALVAREZ': '1314821941',
    'LUIS FIGUEROA': '1310909906',
    'DIEGO AGUILAR': '0706884947',
    'LEONEL VILLACRES': '0706362621',
    'MATEO PICON': '0107627846',
    'LUIS LOPEZ': '0706390663',
    'ALEX LEMA': '1750887059',
    'DANIEL DUMES': '0706009370',
}


class Command(BaseCommand):
    help = 'Crea/actualiza los Colaborador + login de los 9 técnicos de soporte de campo.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--archivo-passwords', default='credenciales_tecnicos.txt',
            help='Dónde guardar las contraseñas generadas para logins nuevos (nunca se imprimen en pantalla).',
        )
        parser.add_argument('--dry-run', action='store_true', help='No escribe nada, solo muestra qué haría.')

    def handle(self, *args, **options):
        dry_run = options.pop('dry_run')
        with transaction.atomic():
            sp = transaction.savepoint()
            self._crear_todos(dry_run=dry_run, **options)
            if dry_run:
                transaction.savepoint_rollback(sp)
            else:
                transaction.savepoint_commit(sp)

    def _crear_todos(self, *, dry_run, **options):
        prefijo = '[DRY RUN] ' if dry_run else ''
        try:
            grupo_soporte = Group.objects.get(name='Soporte Técnico')
        except Group.DoesNotExist:
            raise CommandError(
                'No existe el grupo "Soporte Técnico" — correr `manage.py seed_permisos` primero.',
            )

        departamento, _ = Departamento.objects.get_or_create(
            nombre=DEPARTAMENTO_NOMBRE, defaults={'tipo': Departamento.Tipo.TECNICO},
        )

        credenciales_nuevas = []
        for datos in TECNICOS:
            cargo, _ = Cargo.objects.get_or_create(nombre=datos['cargo'], departamento=departamento)

            colaborador, creado = Colaborador.objects.get_or_create(
                cedula=datos['cedula'],
                defaults=dict(
                    nombre=datos['nombre'], correo=datos['correo'], telefono=datos['telefono'],
                    cargo=cargo, cargo_directorio=datos['cargo'], departamento_directorio=DEPARTAMENTO_NOMBRE,
                    origen_sync=True, sincronizado_en=timezone.now(),
                ),
            )
            if not creado:
                colaborador.nombre = datos['nombre']
                colaborador.correo = datos['correo']
                colaborador.telefono = datos['telefono']
                colaborador.cargo = cargo
                colaborador.cargo_directorio = datos['cargo']
                colaborador.departamento_directorio = DEPARTAMENTO_NOMBRE
                colaborador.save(update_fields=[
                    'nombre', 'correo', 'telefono', 'cargo', 'cargo_directorio', 'departamento_directorio',
                ])
                self.stdout.write(f'{prefijo}{colaborador.nombre}: Colaborador ya existía, datos actualizados.')
            else:
                self.stdout.write(self.style.SUCCESS(f'{prefijo}{colaborador.nombre}: Colaborador creado.'))

            username = datos['correo'].split('@')[0]
            if colaborador.usuario_id:
                self.stdout.write(f'  {prefijo}{username}: ya tiene login, no se toca.')
                continue

            usuario, creado = User.objects.get_or_create(
                username=username, defaults={'email': datos['correo']},
            )
            if creado:
                password = secrets.token_urlsafe(12)
                usuario.set_password(password)
                usuario.save(update_fields=['password'])
                credenciales_nuevas.append((username, password))
                self.stdout.write(self.style.SUCCESS(f'  {prefijo}{username}: login creado.'))
            else:
                self.stdout.write(
                    f'  {prefijo}{username}: el usuario ya existía en Django, se vincula sin tocar su contraseña.',
                )
            usuario.groups.add(grupo_soporte)
            colaborador.usuario = usuario
            colaborador.save(update_fields=['usuario'])

        if credenciales_nuevas and dry_run:
            self.stdout.write(self.style.WARNING(
                f'{prefijo}{len(credenciales_nuevas)} contraseña(s) se generarían — no se escribe ningún archivo '
                'en modo prueba.',
            ))
        elif credenciales_nuevas:
            ruta = options['archivo_passwords']
            with open(ruta, 'w', encoding='utf-8') as f:
                f.write('usuario,contrasena\n')
                for username, password in credenciales_nuevas:
                    f.write(f'{username},{password}\n')
            self.stdout.write(self.style.WARNING(
                f'{len(credenciales_nuevas)} contraseña(s) nueva(s) escritas en "{ruta}" — '
                'distribuir de forma segura a cada técnico y borrar el archivo después.',
            ))
        else:
            self.stdout.write('Todos los técnicos ya tenían login — no se generó ninguna contraseña nueva.')
