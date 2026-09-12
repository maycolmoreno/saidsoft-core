"""Carga las IPs de una farmacia desde la hoja de la planilla, sin transcribir nada.

La planilla de direccionamiento tiene una hoja por farmacia con dos columnas: etiqueta y
dirección. Este comando la lee tal cual y traduce las etiquetas a los slots del catálogo
(`IMPRESORA-A` -> `impresora_A`, `GATEWAY` -> `mikrotik`, `SIPAO CAMARAS` ->
`camaras_sipao`).

Existe para evitar el paso de transcribir la hoja a otro formato, que es donde aparece el
error más difícil de detectar: una IP mal copiada tiene forma de IP válida y nadie la
vuelve a mirar.

Por defecto SIMULA, igual que `completar_topologia`, del que reusa toda la validación:
todo o nada, y nunca pisa un valor ya cargado.

    python manage.py importar_planilla_ips --farmacia GMI04 --datos gmi04.csv
    python manage.py importar_planilla_ips --farmacia GMI04 --datos gmi04.csv --aplicar

El CSV es la hoja exportada, sin cabecera, dos columnas:

    GMI04,
    LOGIN,sangregorio-gmi04
    RED,10.201.7.224/27
    GATEWAY,10.201.7.225
    BASE,10.201.7.226
    GMI04-ADM,10.201.7.227
    IMPRESORA-ADM,10.201.7.232
    MEDIANET-ADM,10.201.7.237
    VoIP,10.201.7.242

Los equipos tienen que existir antes: creálos con `crear_topologia_farmacia --aplicar`.
"""
import csv

from django.contrib.auth.models import User
from django.core.management.base import BaseCommand, CommandError

from apps.activos.services import ErroresDeCarga, completar_datos_topologia, traducir_planilla_ips
from apps.catalogo.models import Farmacia


class Command(BaseCommand):
    help = 'Carga las IPs de una farmacia desde la hoja de la planilla de direccionamiento.'

    def add_arguments(self, parser):
        parser.add_argument('--farmacia', required=True, help='Código de la farmacia, ej. GMI04.')
        parser.add_argument('--datos', required=True, help='CSV de la hoja: etiqueta,dirección (sin cabecera).')
        parser.add_argument(
            '--aplicar', action='store_true',
            help='Escribe los cambios. Sin esto solo informa qué haría.',
        )
        parser.add_argument(
            '--usuario',
            help='Usuario a atribuir en el historial del activo. Default: el primer superusuario.',
        )

    def handle(self, *args, **options):
        farmacia = Farmacia.objects.filter(codigo=options['farmacia']).first()
        if farmacia is None:
            raise CommandError('No existe la farmacia "%s".' % options['farmacia'])

        try:
            with open(options['datos'], newline='', encoding='utf-8-sig') as archivo:
                crudas = [(f[0], f[1] if len(f) > 1 else '') for f in csv.reader(archivo) if f]
        except OSError as exc:
            raise CommandError('No se pudo leer %s: %s' % (options['datos'], exc))
        if not crudas:
            raise CommandError('%s está vacío.' % options['datos'])

        filas, omitidas, errores = traducir_planilla_ips(
            codigo_farmacia=farmacia.codigo, filas=crudas,
        )

        if errores:
            for error in errores:
                self.stdout.write(self.style.ERROR('  %s' % error))
            raise CommandError(
                'Hay %d etiqueta(s) que no supe traducir. No se escribió nada: una fila que '
                'nadie mira es una dirección que queda sin cargar sin que nadie se entere.'
                % len(errores),
            )

        if options['usuario']:
            usuario = User.objects.filter(username=options['usuario']).first()
            if usuario is None:
                raise CommandError('No existe el usuario "%s".' % options['usuario'])
        else:
            usuario = User.objects.filter(is_superuser=True).order_by('id').first()
            if usuario is None:
                raise CommandError('No hay superusuarios; indicá uno con --usuario.')

        try:
            resumen = completar_datos_topologia(
                filas=filas, usuario=usuario, aplicar=options['aplicar'],
            )
        except ErroresDeCarga as exc:
            for error in exc.errores:
                self.stdout.write(self.style.ERROR('  %s' % error))
            raise CommandError(
                '%d problema(s) al aplicar la hoja. No se escribió nada.' % len(exc.errores),
            )

        for linea in resumen['actualizados']:
            self.stdout.write('  %s' % linea)
        self.stdout.write('')

        verbo = 'Actualizados' if options['aplicar'] else 'Se actualizarían'
        self.stdout.write(self.style.SUCCESS(
            '%s: %d activo(s) de %s. Sin cambios: %d.'
            % (verbo, len(resumen['actualizados']), farmacia.codigo, resumen['sin_cambios']),
        ))

        if omitidas:
            self.stdout.write('')
            self.stdout.write(self.style.WARNING('Filas omitidas de la hoja:'))
            for linea in omitidas:
                self.stdout.write(self.style.WARNING('  %s' % linea))

        if resumen['incompletos']:
            self.stdout.write('')
            self.stdout.write(self.style.WARNING('Siguen sin MAC ni número de serie (la hoja no los trae):'))
            for linea in resumen['incompletos']:
                self.stdout.write(self.style.WARNING('  %s' % linea))

        if not options['aplicar']:
            self.stdout.write('')
            self.stdout.write(self.style.WARNING('Simulación: no se escribió nada. Repetí con --aplicar.'))
