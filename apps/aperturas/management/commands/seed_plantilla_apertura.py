"""Siembra una plantilla de apertura usable para el formato más común (mostrador).

A diferencia de sembrar catálogos, esto define trabajo que va a *ejecutarse solo* en
equipos de una farmacia que está abriendo — por eso es un comando explícito y **simula
por defecto**, como `crear_activos_desde_rmm` e `importar_circuitos_proveedor`.

    python manage.py seed_plantilla_apertura --unidad SG            # simula
    python manage.py seed_plantilla_apertura --unidad SG --aplicar
    python manage.py seed_plantilla_apertura --unidad SG --cajas 3 --aplicar

Qué siembra, y por qué así:

- **Perfiles**: un `-ADM` (servidor de la farmacia: es el que reporta métricas y sirve
  de caché por LAN al resto) y N cajas `-A`, `-B`, … Es la forma real de una farmacia
  de mostrador; `--cajas` la ajusta.
- **Pasos que no dependen de nada más**: las verificaciones leen lo que la estación ya
  reporta, y el alta en ITAM usa el servicio que ya existe. Estos siempre se crean.
- **Pasos de script/software**: solo si el script o la aplicación existen en la base.
  No se inventa un `Script` con contenido de PowerShell adivinado para correrlo en una
  caja real — si falta, el comando lo dice y sigue sin ese paso.
- **Pasos manuales**: AD, 2FA y el circuito del proveedor. No se automatizan (ver el
  docstring de `apps.aperturas.models`); quedan en el checklist con su evidencia en vez
  de fingirse automáticos.
"""
from django.contrib.auth.models import User
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from apps.aperturas.models import (
    PasoPlantilla, PerfilEstacionPlantilla, PlantillaApertura, TipoPaso, TipoVerificacion,
)
from apps.catalogo.models import Farmacia, UnidadNegocio

NOMBRE_PLANTILLA = 'Apertura de mostrador'

# Nombre (o parte) con el que se busca el antivirus en el inventario que reporta el
# agente (R7). No es el nombre exacto del catálogo: en el registro de Windows aparece
# como "ESET Endpoint Security", "ESET Security", etc. — por eso la verificación usa
# `icontains` y no una igualdad.
MARCA_ANTIVIRUS = 'ESET'


class Command(BaseCommand):
    help = 'Siembra una plantilla de apertura para farmacias de formato mostrador.'

    def add_arguments(self, parser):
        parser.add_argument('--unidad', required=True, help='Código de la unidad de negocio (ej. SG, MIA, 7DIAS).')
        parser.add_argument('--cajas', type=int, default=2, help='Cantidad de cajas esperadas (default 2).')
        parser.add_argument(
            '--script-energia', default='',
            help='Nombre exacto de un Script de la biblioteca para configurar el plan de energía. '
                 'Si no se pasa (o no existe), ese paso no se crea.',
        )
        parser.add_argument(
            '--app-antivirus', default='',
            help='Nombre exacto de una AplicacionCatalogo para instalar en la apertura. Se usa su '
                 'versión más reciente. Si no se pasa (o no existe), se instala nada y solo se verifica.',
        )
        parser.add_argument('--aplicar', action='store_true', help='Escribe de verdad. Sin esto, solo simula.')

    def handle(self, *args, **options):
        unidad = UnidadNegocio.objects.filter(codigo=options['unidad'].upper()).first()
        if unidad is None:
            raise CommandError(
                f'No existe la unidad de negocio "{options["unidad"]}". '
                f'Disponibles: {", ".join(UnidadNegocio.objects.values_list("codigo", flat=True)) or "ninguna"}.',
            )
        if options['cajas'] < 1:
            raise CommandError('--cajas tiene que ser al menos 1.')

        autor = User.objects.filter(is_superuser=True).order_by('id').first()
        if autor is None:
            raise CommandError('Necesitás al menos un superusuario antes de correr este seed.')

        if PlantillaApertura.objects.filter(unidad_negocio=unidad, nombre=NOMBRE_PLANTILLA, version=1).exists():
            raise CommandError(
                f'"{NOMBRE_PLANTILLA}" v1 ya existe para {unidad.codigo}. Editala en el admin, o creá '
                'una versión nueva desde ahí — este seed no pisa una plantilla existente.',
            )

        script_energia = self._buscar_script(options['script_energia'], unidad)
        version_app = self._buscar_version_app(options['app_antivirus'], unidad)

        perfiles = self._perfiles(options['cajas'])
        pasos = self._pasos(script_energia, version_app)

        self.stdout.write(f'Plantilla "{NOMBRE_PLANTILLA}" v1 para {unidad.codigo} '
                          f'(formato {Farmacia.FormatoFarmacia.MOSTRADOR}):')
        for sufijo, rol, monitorea, cache in perfiles:
            extras = []
            if monitorea:
                extras.append('reporta métricas')
            if cache:
                extras.append('caché de farmacia')
            self.stdout.write(f'  perfil -{sufijo:<4} {rol}{" — " + ", ".join(extras) if extras else ""}')
        for paso in pasos:
            self.stdout.write(f'  paso {paso["orden"]:>3}. {paso["nombre"]} [{paso["tipo"]}]')

        if not options['aplicar']:
            self.stdout.write(self.style.WARNING(
                '\nSimulación: no se escribió nada. Repetí con --aplicar.',
            ))
            return

        self._escribir(unidad, autor, perfiles, pasos)
        self.stdout.write(self.style.SUCCESS(
            f'\nPlantilla creada. Cargala en una apertura desde /aperturas/nueva/.',
        ))

    def _buscar_script(self, nombre, unidad):
        if not nombre:
            return None
        from apps.scripts.models import Script
        script = Script.objects.filter(nombre=nombre, activo=True).first()
        if script is None:
            self.stderr.write(self.style.WARNING(
                f'No existe un Script activo llamado "{nombre}": el paso de energía no se va a crear.',
            ))
            return None
        if script.unidad_negocio_id not in (None, unidad.pk):
            raise CommandError(
                f'El script "{nombre}" es privado de {script.unidad_negocio.codigo}, no de {unidad.codigo}.',
            )
        return script

    def _buscar_version_app(self, nombre, unidad):
        if not nombre:
            return None
        from apps.software.models import AplicacionCatalogo, VersionAplicacion
        app = AplicacionCatalogo.objects.filter(nombre=nombre).first()
        if app is None:
            self.stderr.write(self.style.WARNING(
                f'No existe la aplicación "{nombre}" en el catálogo: no se va a crear el paso de instalación '
                '(la verificación de antivirus presente sí se crea igual).',
            ))
            return None
        version = VersionAplicacion.objects.filter(aplicacion=app).order_by('-id').first()
        if version is None:
            self.stderr.write(self.style.WARNING(
                f'"{nombre}" no tiene ninguna versión cargada: no se va a crear el paso de instalación.',
            ))
        return version

    def _perfiles(self, cajas):
        """(sufijo, rol, monitorear_recursos, es_cache_farmacia)."""
        perfiles = [('ADM', PerfilEstacionPlantilla.Rol.SERVIDOR, True, True)]
        for i in range(cajas):
            perfiles.append((chr(ord('A') + i), PerfilEstacionPlantilla.Rol.CAJA, False, False))
        return perfiles

    def _pasos(self, script_energia, version_app):
        pasos = []
        orden = 10

        def agregar(**kwargs):
            nonlocal orden
            pasos.append({'orden': orden, **kwargs})
            orden += 10

        if script_energia is not None:
            agregar(
                nombre='Configurar plan de energía', tipo=TipoPaso.SCRIPT, script=script_energia,
                rol_estacion='', obligatorio=True,
            )
        if version_app is not None:
            agregar(
                nombre=f'Instalar {version_app.aplicacion.nombre}', tipo=TipoPaso.SOFTWARE,
                version_aplicacion=version_app, rol_estacion='', obligatorio=True,
            )

        agregar(
            nombre='Verificar antivirus instalado', tipo=TipoPaso.VERIFICACION,
            verificacion=TipoVerificacion.SOFTWARE_PRESENTE, parametro=MARCA_ANTIVIRUS,
            rol_estacion='', obligatorio=True,
        )
        agregar(
            nombre='Verificar cifrado de disco (BitLocker)', tipo=TipoPaso.VERIFICACION,
            verificacion=TipoVerificacion.BITLOCKER, rol_estacion='',
            # No obligatorio: muchas cajas del parque son Windows Home, donde BitLocker
            # no existe. Se reporta como pendiente sin trabar la apertura del local.
            obligatorio=False,
        )
        agregar(
            nombre='Verificar que el POS apunta al nodo correcto', tipo=TipoPaso.VERIFICACION,
            verificacion=TipoVerificacion.NODO_POS_COHERENTE, rol_estacion='', obligatorio=True,
        )
        agregar(
            nombre='Verificar versión del POS', tipo=TipoPaso.VERIFICACION,
            verificacion=TipoVerificacion.VERSION_POS_OBJETIVO, rol_estacion='', obligatorio=True,
        )
        agregar(
            nombre='Escanear Windows Update', tipo=TipoPaso.VERIFICACION,
            verificacion=TipoVerificacion.WINDOWS_UPDATE_ESCANEADO, rol_estacion='',
            # Muchas estaciones no tienen salida a internet por defecto; el escaneo falla
            # con ese mensaje y no puede frenar la apertura de un local.
            obligatorio=False,
        )
        agregar(
            nombre='Dar de alta en el inventario (ITAM)', tipo=TipoPaso.ACTIVO_ITAM,
            rol_estacion='', obligatorio=True,
        )

        for nombre in (
            'Alta de usuarios en Active Directory',
            'Enrolar al personal en 2FA',
            'Confirmar circuito del proveedor de enlace',
        ):
            agregar(nombre=nombre, tipo=TipoPaso.MANUAL, rol_estacion='', obligatorio=True)

        return pasos

    @transaction.atomic
    def _escribir(self, unidad, autor, perfiles, pasos):
        plantilla = PlantillaApertura.objects.create(
            nombre=NOMBRE_PLANTILLA, version=1, unidad_negocio=unidad, creado_por=autor,
            formato_farmacia=Farmacia.FormatoFarmacia.MOSTRADOR,
            descripcion=(
                'Sembrada por seed_plantilla_apertura. Los pasos manuales (AD, 2FA, circuito del '
                'proveedor) no se automatizan: quedan en el checklist con su evidencia.'
            ),
        )
        for sufijo, rol, monitorea, cache in perfiles:
            PerfilEstacionPlantilla.objects.create(
                plantilla=plantilla, sufijo=sufijo, rol=rol,
                monitorear_recursos=monitorea, es_cache_farmacia=cache,
            )
        for paso in pasos:
            PasoPlantilla.objects.create(plantilla=plantilla, **paso)
        return plantilla
