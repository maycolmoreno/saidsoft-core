"""Le pone código a los activos que quedaron sin uno, reusando la numeración del sistema.

Por qué existe: `Activo.codigo` no lo genera el modelo, lo genera
`apps.activos.services.generar_codigo_activo` y lo asigna `registrar_ingreso`. Durante un
tiempo el admin de Django —donde `codigo` es `readonly_fields`, a propósito— daba de alta
sin pasar por ahí, y como `unique=True` acepta UN string vacío, la primera alta por ese
camino entraba con `codigo=''` y la segunda rompía contra el índice único. El hueco se
cerró el 16-sep-2026 con `ActivoAdmin.save_model`; este comando limpia lo que quedó de
antes.

**El huérfano no era solo cosmético.** `generar_codigo_activo` tomaba el código más alto
del tipo y le hacía `int(codigo.rsplit('-', 1)[-1])`, así que un código vacío como ÚNICO
activo de su tipo era `int('')` y un `ValueError`: no se podía dar de alta ningún activo
más de ese tipo. Eso **ya está arreglado** — la función ahora ignora los códigos que no
tienen la forma esperada— pero el dato sigue mereciendo corrección: un activo sin código
no tiene etiqueta con la que buscarlo.

Simula por defecto y escribe solo con `--aplicar`, igual que `completar_topologia` e
`importar_planilla_ips`: el mismo criterio de que una corrección de datos se mira antes de
hacerse.

    python manage.py corregir_activos_sin_codigo
    python manage.py corregir_activos_sin_codigo --aplicar
    python manage.py corregir_activos_sin_codigo --aplicar --usuario mi.usuario

En producción, con el nombre real del contenedor:

    docker exec deploy-web-1 python manage.py corregir_activos_sin_codigo
"""
from django.contrib.auth.models import User
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.db.models import Q

from apps.activos.models import Activo, EventoActivo
from apps.activos.services import generar_codigo_activo


class Command(BaseCommand):
    help = 'Asigna código a los activos que quedaron con el código vacío. Simula por defecto.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--aplicar', action='store_true',
            help='Escribe los cambios. Sin esto solo informa qué haría.',
        )
        parser.add_argument(
            '--usuario',
            help='Usuario a atribuir en el historial del activo. Default: el primer superusuario.',
        )

    def handle(self, *args, **options):
        pendientes = self._pendientes()

        if not pendientes:
            self.stdout.write(self.style.SUCCESS(
                'No hay activos sin código: nada que corregir.',
            ))
            return

        self.stdout.write('Activos sin código: %d' % len(pendientes))
        self.stdout.write('')
        self.stdout.write('  %-5s %-5s %-14s %-14s %-14s %s' % (
            'id', 'tipo', 'código actual', 'código nuevo', 'estado', 'ip',
        ))

        propuestos = {}
        bloqueados = []
        for activo in pendientes:
            nuevo, motivo = self._codigo_propuesto(activo)
            if nuevo is None:
                bloqueados.append((activo, motivo))
                self.stdout.write('  %-5s %-5s %-14s %-14s %-14s %s' % (
                    activo.pk, activo.tipo, repr(activo.codigo), 'BLOQUEADO',
                    activo.estado, activo.ip or '—',
                ))
                continue
            # En simulación nada se guarda, así que `generar_codigo_activo` devuelve el
            # mismo número para dos activos del mismo tipo. No se inventa acá una segunda
            # numeración: se avisa, y al aplicar cada save deja ver el siguiente al que
            # sigue (ver el bucle de abajo).
            repetido = nuevo in propuestos.values()
            propuestos[activo.pk] = nuevo
            self.stdout.write('  %-5s %-5s %-14s %-14s %-14s %s%s' % (
                activo.pk, activo.tipo, repr(activo.codigo), nuevo, activo.estado,
                activo.ip or '—', '   <- se recalcula al aplicar' if repetido else '',
            ))

        if bloqueados:
            self._abortar_por_bloqueados(bloqueados)

        self._verificar_conflictos(pendientes, propuestos)

        if not options['aplicar']:
            self.stdout.write('')
            self.stdout.write(self.style.WARNING(
                'Simulación: no se escribió nada. Repetí con --aplicar.',
            ))
            return

        usuario = self._resolver_usuario(options['usuario'])
        corregidos = self._aplicar(pendientes, usuario)

        self.stdout.write('')
        for codigo_viejo, activo in corregidos:
            self.stdout.write('  id=%s: %s -> %s' % (activo.pk, repr(codigo_viejo), activo.codigo))
        self.stdout.write('')
        self.stdout.write(self.style.SUCCESS(
            'Corregidos: %d activo(s). Quedan sin código: %d.'
            % (len(corregidos), len(self._pendientes())),
        ))

    def _pendientes(self):
        """Los activos sin código, en orden estable.

        `codigo__isnull=True` va por las dudas y no porque pueda pasar: la columna es
        `CharField(unique=True)` SIN `null=True`, o sea `NOT NULL` en la base, así que un
        NULL es imposible hoy. Si alguien algún día la hiciera nullable, este comando ya
        los encuentra en vez de dejarlos invisibles.
        """
        return list(
            Activo.objects.filter(Q(codigo='') | Q(codigo__isnull=True)).order_by('tipo', 'pk'),
        )

    def _codigo_propuesto(self, activo):
        """`(codigo, None)`, o `(None, motivo)` si la numeración no puede resolverlo.

        Red de seguridad, hoy inalcanzable a propósito. `generar_codigo_activo` ignora los
        códigos que no tienen la forma `CR-TIPO-NNNN`, así que ya no puede fallar con los
        datos que este comando viene a corregir. Antes sí: tomaba el más alto del tipo y le
        hacía `int(...)`, y con un código vacío como único del tipo eso era `int('')`.

        Se conserva porque el costo es nulo y la alternativa es confiar en que la
        numeración nunca vuelva a lanzar: si alguna vez lo hiciera, lo que importa es que
        este comando lo informe con el id en vez de morir a mitad de una corrección.
        """
        try:
            return generar_codigo_activo(activo.tipo), None
        except ValueError:
            return None, (
                '`generar_codigo_activo(%r)` lanzó ValueError. Ya ignora los códigos sin '
                'la forma CR-TIPO-NNNN, así que la causa es otra y hay que mirarla'
                % activo.tipo
            )

    def _abortar_por_bloqueados(self, bloqueados):
        """Todo o nada, igual que `completar_datos_topologia`.

        Se podría corregir el resto y saltear los bloqueados, pero media corrección
        aplicada deja a alguien sin saber desde dónde retomar — mismo criterio que la
        carga de planillas. Y abortar fuerza a resolver la causa en vez de arrastrarla.
        """
        self.stdout.write('')
        self.stdout.write(self.style.ERROR('No se puede calcular el código de:'))
        for activo, motivo in bloqueados:
            self.stdout.write(self.style.ERROR('  id=%s: %s' % (activo.pk, motivo)))
        raise CommandError(
            'La numeración no puede resolver %d activo(s). No se escribió nada. '
            '`apps.activos.services.generar_codigo_activo` ya ignora los códigos sin la '
            'forma CR-TIPO-NNNN, así que llegar acá significa que falló por otro motivo: '
            'hay que mirarlo antes de insistir.' % len(bloqueados),
        )

    def _verificar_conflictos(self, pendientes, propuestos):
        """Aborta si un código propuesto ya lo tiene OTRO activo.

        No puede pasar con `generar_codigo_activo` funcionando bien, y justamente por eso
        se comprueba: si pasara, el `unique=True` convertiría el save en un IntegrityError
        a mitad de la corrida, y lo que hay que evitar es tocar el activo equivocado.
        """
        propios = {a.pk for a in pendientes}
        for activo in pendientes:
            nuevo = propuestos[activo.pk]
            choque = Activo.objects.filter(codigo=nuevo).exclude(pk__in=propios).first()
            if choque is not None:
                raise CommandError(
                    'El código "%s" que le tocaría al activo id=%s ya lo tiene el activo '
                    'id=%s (%s). No se escribió nada. Revisar `generar_codigo_activo` '
                    'antes de insistir.' % (nuevo, activo.pk, choque.pk, choque.codigo),
                )

    def _resolver_usuario(self, nombre):
        """Mismo criterio que `completar_topologia`: el primer superusuario si no se indica."""
        if nombre:
            usuario = User.objects.filter(username=nombre).first()
            if usuario is None:
                raise CommandError('No existe el usuario "%s".' % nombre)
            return usuario
        usuario = User.objects.filter(is_superuser=True).order_by('id').first()
        if usuario is None:
            raise CommandError('No hay superusuarios; indicá uno con --usuario.')
        return usuario

    def _aplicar(self, pendientes, usuario):
        """Asigna y guarda, todo o nada.

        El código se recalcula ACÁ y no se reusa el de la simulación: al guardar uno,
        `generar_codigo_activo` ya ve el anterior y devuelve el siguiente, que es lo que
        hace que dos activos del mismo tipo no colisionen. Es el mismo motivo por el que
        el save va dentro del bucle y no en un `bulk_update`.
        """
        corregidos = []
        with transaction.atomic():
            for activo in pendientes:
                codigo_viejo = activo.codigo
                activo.codigo = generar_codigo_activo(activo.tipo)
                activo.save(update_fields=['codigo'])
                # DATOS_RED_CARGADOS es la única choice que hoy usa una corrección de
                # datos por comando (ver `completar_datos_topologia`) y no una transición
                # del ciclo de vida. La etiqueta habla de IP/MAC/serie, así que `detalle`
                # dice sin ambigüedad qué se corrigió. Agregar una choice propia exigiría
                # una migración, que esta fase no toca.
                EventoActivo.objects.create(
                    activo=activo, tipo_evento=EventoActivo.TipoEvento.DATOS_RED_CARGADOS,
                    usuario=usuario,
                    detalle={
                        'correccion': 'codigo_vacio',
                        'codigo_anterior': codigo_viejo,
                        'codigo_nuevo': activo.codigo,
                        'comando': 'corregir_activos_sin_codigo',
                    },
                )
                corregidos.append((codigo_viejo, activo))
        return corregidos
