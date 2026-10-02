"""Arma los escenarios para probar A MANO los flujos que no se pueden clickear sin datos.

    python manage.py sembrar_escenarios_prueba              # simula: dice qué crearía
    python manage.py sembrar_escenarios_prueba --aplicar
    python manage.py sembrar_escenarios_prueba --limpiar --aplicar

Existe porque tres flujos del panel necesitan un estado previo que no se llega a armar
clickeando: un viático **ya observado**, un mantenimiento abierto **por otro usuario**, y
una alerta abierta sobre una estación **con equipo vinculado**. Sin eso, verificar a mano
que funcionan exige media hora de preparación cada vez.

**Esto crea usuarios reales con acceso al panel.** No es un seeder de demo: es una
herramienta de verificación, y en producción deja gente que puede entrar. Por eso:

- La contraseña se **genera al azar y se muestra una sola vez**. No hay ninguna clave
  fija en este archivo ni en el repo: una clave versionada que alguien deja puesta en
  producción es exactamente cómo se regalan accesos.
- Todo lo que crea lleva el prefijo `PRUEBA-` / `prueba.` y `--limpiar` lo borra por ese
  prefijo. Sin esa contracara, probar en producción sería dejar basura permanente.
- Simula por defecto y exige `--aplicar`, como el resto de los comandos que escriben en
  masa de este proyecto.

**Después de verificar en producción, corré `--limpiar --aplicar`.** No es opcional.
"""
import secrets

from django.contrib.auth.models import Permission, User
from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from apps.activos.models import Activo, CategoriaEquipo, Colaborador, Marca
from apps.catalogo.models import Estacion, Farmacia, Grupo, UnidadNegocio
from apps.cuentas.models import PerfilUsuario
from apps.mantenimiento.models import (
    EventoMantenimiento, Mantenimiento, MantenimientoProgramado, TipoMantenimiento,
)
from apps.monitoreo.models import Alerta, Metrica, ReglaAlerta
from apps.viaticos.models import EstadoReporteViatico, ReporteViatico, RubroViatico

# Todo lo que este comando crea empieza así. `--limpiar` borra exactamente esto y nada
# más: sin una marca, limpiar en producción sería adivinar qué era de prueba.
PREFIJO_USUARIO = 'prueba.'
PREFIJO_CODIGO = 'PRUEBA-'
CODIGO_GRUPO = 'PRUEBA-TRX'
CODIGO_FARMACIA = 'PRUEBA-ML'
CODIGO_ESTACION = 'PRUEBA-ML-A'
CEDULA_COLABORADOR = 'PRUEBA-0001'
NOMBRE_REGLA = 'PRUEBA - Disco lleno'

USUARIOS = {
    'prueba.tecnico': [
        ('viaticos', 'add_reporteviatico'), ('viaticos', 'view_reporteviatico'),
    ],
    'prueba.coordinador': [
        ('viaticos', 'view_reporteviatico'), ('viaticos', 'change_reporteviatico'),
    ],
    'prueba.mesa': [
        ('monitoreo', 'view_alerta'), ('monitoreo', 'change_alerta'),
        ('mantenimiento', 'add_mantenimiento'), ('mantenimiento', 'view_mantenimiento'),
        ('catalogo', 'view_estacion'),
    ],
}


class Command(BaseCommand):
    help = 'Arma (o limpia) los escenarios para probar a mano viáticos, mantenimiento y alertas.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--aplicar', action='store_true',
            help='Escribe de verdad. Sin esto solo dice qué haría.',
        )
        parser.add_argument(
            '--limpiar', action='store_true',
            help='Borra todo lo que este comando haya creado, en vez de crearlo.',
        )
        parser.add_argument(
            '--unidad', default='SG',
            help='Unidad de negocio donde armar el escenario. SG por defecto.',
        )

    def handle(self, *args, **opciones):
        if opciones['limpiar']:
            return self._limpiar(aplicar=opciones['aplicar'])
        return self._sembrar(codigo_unidad=opciones['unidad'], aplicar=opciones['aplicar'])

    # --- limpieza ---

    def _limpiar(self, *, aplicar):
        usuarios = User.objects.filter(username__startswith=PREFIJO_USUARIO)
        estaciones = Estacion.objects.filter(codigo__startswith=PREFIJO_CODIGO)
        activos = Activo.objects.filter(codigo__startswith=PREFIJO_CODIGO)
        farmacias = Farmacia.objects.filter(codigo__startswith=PREFIJO_CODIGO)
        grupos = Grupo.objects.filter(codigo__startswith=PREFIJO_CODIGO)
        colaboradores = Colaborador.objects.filter(cedula__startswith=PREFIJO_CODIGO)
        reglas = ReglaAlerta.objects.filter(nombre__startswith='PRUEBA')
        # Los mantenimientos que se hayan abierto SOBRE el equipo de prueba, incluidos
        # los que crea el propio flujo que se está verificando (el botón "Abrir
        # mantenimiento" de una alerta). Sin esto la limpieza falla con ProtectedError:
        # `MantenimientoEquipo.equipo` es PROTECT, así que usar la funcionalidad deja
        # una referencia que impide borrar el equipo. Encontrado limpiando de verdad,
        # no leyendo el modelo.
        mantenimientos = Mantenimiento.objects.filter(equipos__equipo__in=activos).distinct()

        self.stdout.write('Se borraría:')
        for etiqueta, qs in [
            ('usuarios', usuarios), ('alertas', Alerta.objects.filter(estacion__in=estaciones)),
            ('mantenimientos sobre el equipo de prueba', mantenimientos),
            ('estaciones', estaciones), ('activos', activos), ('colaboradores', colaboradores),
            ('reglas de alerta', reglas), ('farmacias', farmacias), ('grupos', grupos),
        ]:
            self.stdout.write(f'  {etiqueta}: {qs.count()}')

        if not aplicar:
            self.stdout.write(self.style.WARNING('\nSIMULACIÓN — nada se borró. Repetí con --aplicar.'))
            return

        with transaction.atomic():
            # El orden importa: lo que otros apuntan, al final.
            Alerta.objects.filter(estacion__in=estaciones).delete()
            ReporteViatico.objects.filter(colaborador__in=colaboradores).delete()
            reglas.delete()
            # El orden lo imponen los PROTECT, que están puestos a propósito para que
            # nadie borre el historial de un mantenimiento real. Se desarma de adentro
            # hacia afuera: eventos -> mantenimiento -> equipo.
            EventoMantenimiento.objects.filter(mantenimiento__in=mantenimientos).delete()
            MantenimientoProgramado.objects.filter(equipo__in=activos).delete()
            mantenimientos.delete()
            activos.update(estacion=None)
            activos.delete()
            estaciones.delete()
            colaboradores.delete()
            farmacias.delete()
            grupos.delete()
            usuarios.delete()
        self.stdout.write(self.style.SUCCESS('Listo: no queda nada de prueba.'))

    # --- siembra ---

    def _sembrar(self, *, codigo_unidad, aplicar):
        unidad = UnidadNegocio.objects.filter(codigo=codigo_unidad.upper()).first()
        if unidad is None:
            self.stderr.write(f'No existe la unidad de negocio {codigo_unidad!r}.')
            return

        self.stdout.write(f'Escenarios de prueba en {unidad.codigo}:')
        self.stdout.write(f'  {len(USUARIOS)} usuarios con acceso al panel ({", ".join(USUARIOS)})')
        self.stdout.write(f'  1 farmacia {CODIGO_FARMACIA}, 1 estación {CODIGO_ESTACION}, 1 equipo')
        self.stdout.write('  1 viático YA OBSERVADO (para probar corregir y reenviar)')
        self.stdout.write('  1 alerta abierta con equipo vinculado (para probar abrir mantenimiento)')

        if not aplicar:
            self.stdout.write(self.style.WARNING('\nSIMULACIÓN — nada se creó. Repetí con --aplicar.'))
            return

        clave = secrets.token_urlsafe(12)
        with transaction.atomic():
            creados = self._crear_todo(unidad, clave)

        self.stdout.write(self.style.SUCCESS('\nListo.'))
        self.stdout.write(f"  Viático observado:  #{creados['reporte'].pk}")
        self.stdout.write(f"  Alerta abierta:     #{creados['alerta'].pk} en {CODIGO_ESTACION}")
        self.stdout.write('\n  Usuarios: ' + ', '.join(USUARIOS))
        self.stdout.write(self.style.WARNING(f'  Contraseña (se muestra UNA sola vez): {clave}'))
        self.stdout.write(
            '\nCuando termines de verificar:\n'
            '  python manage.py sembrar_escenarios_prueba --limpiar --aplicar',
        )

    def _crear_todo(self, unidad, clave):
        usuarios = {}
        for username, permisos in USUARIOS.items():
            u, _ = User.objects.get_or_create(username=username)
            u.set_password(clave)
            u.is_active = True
            u.save()
            PerfilUsuario.objects.update_or_create(
                usuario=u, defaults={'acceso_todas_unidades': True},
            )
            for app, code in permisos:
                u.user_permissions.add(
                    Permission.objects.get(content_type__app_label=app, codename=code),
                )
            usuarios[username] = u

        # El colaborador ata el usuario al gasto: sin esto viáticos responde 409.
        colaborador, _ = Colaborador.objects.get_or_create(
            cedula=CEDULA_COLABORADOR,
            defaults={'nombre': 'Técnico de Prueba', 'unidad_negocio': unidad},
        )
        colaborador.usuario = usuarios['prueba.tecnico']
        colaborador.unidad_negocio = unidad
        colaborador.save()

        grupo, _ = Grupo.objects.get_or_create(
            codigo=CODIGO_GRUPO, defaults={'nombre': 'Nodo de prueba'},
        )
        farmacia, _ = Farmacia.objects.get_or_create(
            codigo=CODIGO_FARMACIA,
            defaults={'nombre': 'Farmacia de prueba', 'grupo': grupo, 'unidad_negocio': unidad},
        )

        # Escenario del arreglo 1: un viático que el coordinador ya observó.
        reporte, _ = ReporteViatico.objects.get_or_create(
            colaborador=colaborador, fecha=timezone.localdate(), rubro=RubroViatico.MOVILIZACION,
            defaults={'farmacia_visitada': farmacia, 'monto': '15.50'},
        )
        reporte.estado = EstadoReporteViatico.OBSERVADO
        reporte.comentario_coordinador = (
            'Falta el origen y el destino del viaje. Corregilo y reenvialo.'
        )
        reporte.revisado_por = usuarios['prueba.coordinador']
        reporte.revisado_en = timezone.now()
        reporte.save()

        # Escenario del arreglo 3: alerta abierta sobre una estación CON equipo vinculado.
        # Sin el equipo, el botón avisa que falta vincularlo — que también vale la pena
        # probar, pero desatando el activo a mano.
        TipoMantenimiento.objects.get_or_create(
            codigo='correctivo', defaults={'nombre': 'Correctivo'},
        )
        estacion, _ = Estacion.objects.get_or_create(
            codigo=CODIGO_ESTACION,
            defaults={'farmacia': farmacia, 'estado_aprobacion': Estacion.EstadoAprobacion.APROBADA},
        )
        categoria, _ = CategoriaEquipo.objects.get_or_create(nombre='PC de prueba')
        marca, _ = Marca.objects.get_or_create(nombre='Marca de prueba')
        activo, _ = Activo.objects.get_or_create(
            codigo=f'{PREFIJO_CODIGO}EQ',
            defaults={'categoria': categoria, 'marca': marca, 'unidad_negocio': unidad},
        )
        activo.estacion = estacion
        activo.save()

        # `abre_mantenimiento=False` a propósito: si la regla lo abriera sola, el botón
        # manual —que es lo que se quiere probar— no tendría nada que hacer.
        regla, _ = ReglaAlerta.objects.get_or_create(
            nombre=NOMBRE_REGLA,
            defaults={
                'metrica': Metrica.DISCO_USADO_PCT, 'umbral': 95, 'duracion_minutos': 0,
                'severidad': ReglaAlerta.Severidad.CRITICAL,
                'creado_por': usuarios['prueba.mesa'], 'abre_mantenimiento': False,
            },
        )
        alerta, _ = Alerta.objects.get_or_create(
            regla=regla, estacion=estacion, estado=Alerta.Estado.ABIERTA,
            defaults={'valor_disparador': 99},
        )
        return {'reporte': reporte, 'alerta': alerta}
