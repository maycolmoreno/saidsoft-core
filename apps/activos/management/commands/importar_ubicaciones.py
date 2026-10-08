"""Carga el catálogo de ubicaciones (matriz, oficinas) desde un CSV, sin transcribir nada.

Por qué existe: `Activo.ubicacion` es lo que permite registrar un equipo en servicio
fuera de una farmacia y fuera de una bodega — la impresora administrativa, el switch de
la oficina. Pero la tabla `ubicacion` está vacía en producción (medido el 7-oct-2026), y
un desplegable vacío hace que el campo sea decoración.

**No es el `seed_activos`.** Ése es un seed de demostración con datos inventados ("Agencia
Cuenca", nombres de fantasía) y no debe correr en producción. Esto lee las ubicaciones
reales de un archivo que provee quien las conoce.

El `nombre` es la identidad: si ya existe una ubicación con ese nombre, **no se toca**. Si
el CSV trae valores distintos de los cargados, eso es un conflicto que resuelve una
persona, no un `UPDATE` silencioso — mismo criterio que `completar_topologia`. Y por el
mismo motivo: una ciudad mal pisada tiene forma de ciudad válida y nadie la vuelve a mirar.

Simula por defecto y escribe solo con `--aplicar`.

    python manage.py importar_ubicaciones --datos ubicaciones.csv
    python manage.py importar_ubicaciones --datos ubicaciones.csv --aplicar

El CSV lleva cabecera. Solo `nombre` es obligatorio:

    nombre,agencia,direccion,ciudad,parroquia,provincia,departamento,encargado
    Matriz,Matriz,Av. 9 de Octubre 100,Guayaquil,Tarqui,Guayas,Tecnologías e Innovación,0912345678
    Oficina Machala,,Calle Guayas 45,Machala,,El Oro,TRANSFORMACIÓN DIGITAL,

- `departamento` se busca por nombre y **tiene que existir**: crear departamentos como
  efecto colateral de importar ubicaciones es la misma trampa que dar de alta equipos
  fantasma desde una planilla. Los departamentos se cargan por el admin.
- `encargado` es la **cédula** de un `Colaborador` que ya exista (es su identidad única).

Para ver qué departamentos hay disponibles:

    python manage.py importar_ubicaciones --listar-departamentos
"""
import csv

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from apps.activos.models import Colaborador, Departamento, Ubicacion

COLUMNAS = ('nombre', 'agencia', 'direccion', 'ciudad', 'parroquia', 'provincia',
            'departamento', 'encargado')
# Las que se copian tal cual del CSV a la fila. `departamento` y `encargado` no están:
# son FK y se resuelven aparte.
CAMPOS_DE_TEXTO = ('agencia', 'direccion', 'ciudad', 'parroquia', 'provincia')


class Command(BaseCommand):
    help = 'Carga el catálogo de ubicaciones desde un CSV. Simula por defecto.'

    def add_arguments(self, parser):
        parser.add_argument('--datos', help='CSV con cabecera: %s.' % ','.join(COLUMNAS))
        parser.add_argument(
            '--aplicar', action='store_true',
            help='Escribe los cambios. Sin esto solo informa qué haría.',
        )
        parser.add_argument(
            '--listar-departamentos', action='store_true',
            help='Muestra los departamentos cargados y termina, para saber qué poner en el CSV.',
        )

    def handle(self, *args, **options):
        if options['listar_departamentos']:
            return self._listar_departamentos()
        if not options['datos']:
            raise CommandError('Falta --datos con el CSV. Ver --help para el formato.')

        filas = self._leer(options['datos'])
        planeado, existentes, errores = self._resolver(filas)

        if errores:
            for error in errores:
                self.stdout.write(self.style.ERROR('  %s' % error))
            raise CommandError(
                '%d problema(s) en la planilla. No se escribió nada: corregí y volvé a '
                'correr.' % len(errores),
            )

        for linea in existentes:
            self.stdout.write('  %s' % linea)
        for datos, _depto, _enc in planeado:
            self.stdout.write(self.style.SUCCESS('  + %s' % datos['nombre']))

        if not options['aplicar']:
            self.stdout.write('')
            self.stdout.write(self.style.WARNING(
                'Simulación: se crearían %d ubicación(es), %d ya existen. '
                'Repetí con --aplicar.' % (len(planeado), len(existentes)),
            ))
            return

        with transaction.atomic():
            for datos, departamento, encargado in planeado:
                Ubicacion.objects.create(
                    departamento=departamento, encargado=encargado,
                    **{c: datos.get(c, '') for c in ('nombre',) + CAMPOS_DE_TEXTO},
                )

        self.stdout.write('')
        self.stdout.write(self.style.SUCCESS(
            'Creadas: %d ubicación(es). Sin cambios (ya existían): %d. Total en catálogo: %d.'
            % (len(planeado), len(existentes), Ubicacion.objects.count()),
        ))

    def _listar_departamentos(self):
        departamentos = Departamento.objects.filter(activo=True).order_by('nombre')
        if not departamentos:
            self.stdout.write(self.style.WARNING(
                'No hay departamentos cargados. Se cargan por el admin antes de importar '
                'ubicaciones que los referencien.',
            ))
            return
        self.stdout.write('Departamentos disponibles para la columna `departamento`:')
        for departamento in departamentos:
            self.stdout.write('  %s  (%s)' % (departamento.nombre, departamento.get_tipo_display()))

    def _leer(self, ruta):
        """`utf-8-sig` y no `utf-8`: un CSV exportado de Excel trae BOM, y sin esto la
        primera columna de la cabecera se llama '\\ufeffnombre' y no coincide con nada."""
        try:
            with open(ruta, newline='', encoding='utf-8-sig') as archivo:
                filas = list(csv.DictReader(archivo))
        except FileNotFoundError:
            raise CommandError('No existe el archivo %s.' % ruta)
        if not filas:
            raise CommandError('El CSV no tiene filas de datos.')
        desconocidas = set(filas[0]) - set(COLUMNAS) - {None}
        if desconocidas:
            raise CommandError(
                'Columnas que no se entienden: %s. Se esperan: %s.'
                % (', '.join(sorted(desconocidas)), ', '.join(COLUMNAS)),
            )
        return filas

    def _resolver(self, filas):
        """Valida la planilla entera antes de escribir la primera fila.

        Todo o nada: media planilla aplicada deja a alguien sin saber desde dónde
        retomar. Devuelve `(a_crear, ya_existentes, errores)`.
        """
        a_crear = []
        ya_existentes = []
        errores = []
        vistos = set()

        for numero, fila in enumerate(filas, start=1):
            datos = {c: (fila.get(c) or '').strip() for c in COLUMNAS}
            nombre = datos['nombre']
            etiqueta = 'fila %d (%s)' % (numero, nombre or '?')

            if not nombre:
                errores.append('%s: falta "nombre", que es la identidad de la ubicación.' % etiqueta)
                continue
            if nombre.casefold() in vistos:
                errores.append('%s: repetida en la planilla.' % etiqueta)
                continue
            vistos.add(nombre.casefold())

            # `Ubicacion.nombre` NO es unique en la base, así que puede haber dos con el
            # mismo nombre de antes. Si pasa, este comando no puede decidir a cuál se
            # refiere la fila: lo dice y para.
            coincidencias = list(Ubicacion.objects.filter(nombre=nombre)[:2])
            if len(coincidencias) > 1:
                errores.append(
                    '%s: ya hay más de una ubicación con ese nombre (ids %s). Hay que '
                    'resolver la duplicación antes de importar.'
                    % (etiqueta, ', '.join(str(u.pk) for u in coincidencias)),
                )
                continue

            departamento, error = self._resolver_departamento(datos['departamento'], etiqueta)
            if error:
                errores.append(error)
                continue
            encargado, error = self._resolver_encargado(datos['encargado'], etiqueta)
            if error:
                errores.append(error)
                continue

            if coincidencias:
                ya_existentes.append(
                    self._describir_existente(coincidencias[0], datos, departamento, encargado),
                )
                continue

            a_crear.append((datos, departamento, encargado))

        return a_crear, ya_existentes, errores

    def _resolver_departamento(self, nombre, etiqueta):
        if not nombre:
            return None, None
        departamento = Departamento.objects.filter(nombre=nombre).first()
        if departamento is None:
            return None, (
                '%s: no existe el departamento "%s". Verlos con '
                '--listar-departamentos; se cargan por el admin.' % (etiqueta, nombre)
            )
        return departamento, None

    def _resolver_encargado(self, cedula, etiqueta):
        if not cedula:
            return None, None
        encargado = Colaborador.objects.filter(cedula=cedula).first()
        if encargado is None:
            return None, (
                '%s: no existe un colaborador con cédula "%s". La columna `encargado` es '
                'la cédula, que es su identidad única.' % (etiqueta, cedula)
            )
        return encargado, None

    def _describir_existente(self, ubicacion, datos, departamento, encargado):
        """Una ubicación que ya está no se toca, pero si el CSV trae otra cosa hay que
        decirlo: puede ser que la planilla esté vieja, o que alguien la haya editado."""
        difieren = [
            '%s: cargado "%s" / planilla "%s"' % (campo, getattr(ubicacion, campo), datos[campo])
            for campo in CAMPOS_DE_TEXTO
            if datos[campo] and datos[campo] != getattr(ubicacion, campo)
        ]
        if departamento is not None and ubicacion.departamento_id != departamento.pk:
            difieren.append(
                'departamento: cargado "%s" / planilla "%s"'
                % (ubicacion.departamento or '—', departamento),
            )
        if encargado is not None and ubicacion.encargado_id != encargado.pk:
            difieren.append(
                'encargado: cargado "%s" / planilla "%s"' % (ubicacion.encargado or '—', encargado),
            )
        if difieren:
            return '= %s: ya existe, NO se toca. Difiere en %s' % (datos['nombre'], '; '.join(difieren))
        return '= %s: ya existe, sin cambios.' % datos['nombre']
