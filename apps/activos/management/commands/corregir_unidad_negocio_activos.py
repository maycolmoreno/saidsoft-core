"""Pone al día la unidad de negocio de los activos que están en una farmacia.

    python manage.py corregir_unidad_negocio_activos              # simula: dice a quién tocaría
    python manage.py corregir_unidad_negocio_activos --aplicar

POR QUÉ EXISTE

`INV-05` de la auditoría por agentes. Las rutas que ubican un activo en una farmacia ya
heredan su unidad de negocio desde el 12-oct-2026, pero eso **no arregla las filas que
quedaron mal antes**: un activo con `unidad_negocio` vacío en la farmacia de un cliente es
visible —y asignable, reubicable, dable de baja— por un usuario de otro, porque
`usuario_puede_ver(user, None)` devuelve True siempre. Y al mismo tiempo desaparece del bloque
"sin sondeo" del Centro de Monitoreo, que filtra con un `IN` estricto: el mismo activo se ve
en el listado y no se ve en el tablero.

Medido en producción el 12-oct-2026: **3 activos** en esa situación —dos en farmacias de MIA y
uno de 7DIAS, los tres con unidad vacía—.

**Qué toca y qué no.** Solo activos que están en una farmacia, porque ahí la unidad no es una
opinión: es el cliente dueño de esa farmacia. Un activo sin farmacia (en bodega, en oficinas)
puede tener unidad vacía con todo derecho y no se toca.

**El caso de la unidad DISTINTA se informa pero no se cambia sin `--forzar`.** Que un activo
en una farmacia de MIA esté marcado como de SG puede ser el defecto de `INV-05`, o puede ser
alguien que lo marcó a propósito por un motivo que este comando no conoce. Pisar eso en
silencio sería reemplazar un dato dudoso por otro. El caso vacío sí se completa directo:
vacío no es una decisión, es la ausencia de una.

Deja `EventoActivo` por cada activo tocado (`CORRECCION_DATOS`, que existe justamente para
"alguien arreglando un dato que quedó mal" y no para una transición del ciclo de vida).
"""
from django.core.management.base import BaseCommand

from apps.activos.models import Activo, EventoActivo


class Command(BaseCommand):
    help = 'Hereda la unidad de negocio de la farmacia en los activos que la tienen vacía. Simula salvo --aplicar.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--aplicar', action='store_true',
            help='Corrige de verdad. Sin esto solo muestra qué cambiaría.',
        )
        parser.add_argument(
            '--forzar', action='store_true',
            help='Además, pisa las unidades DISTINTAS a la de su farmacia. Sin esto solo se '
                 'informan: una unidad distinta puede ser una decisión que este comando no conoce.',
        )

    def handle(self, *args, **opciones):
        aplicar, forzar = opciones['aplicar'], opciones['forzar']

        en_farmacia = (
            Activo.objects.filter(farmacia__isnull=False)
            .select_related('farmacia__unidad_negocio', 'unidad_negocio')
            .order_by('codigo')
        )
        vacios = [a for a in en_farmacia if a.unidad_negocio_id is None]
        distintos = [
            a for a in en_farmacia
            if a.unidad_negocio_id is not None
            and a.unidad_negocio_id != a.farmacia.unidad_negocio_id
        ]

        self.stdout.write(f'Activos en una farmacia: {en_farmacia.count()}')
        self.stdout.write(f'  con unidad VACÍA (se completan) : {len(vacios)}')
        self.stdout.write(f'  con unidad DISTINTA             : {len(distintos)}')

        for activo in vacios:
            self.stdout.write(
                f'  {activo.codigo:<14} {activo.farmacia.codigo:<8} (vacía) -> '
                f'{activo.farmacia.unidad_negocio.codigo}',
            )
        for activo in distintos:
            marca = '-> ' + activo.farmacia.unidad_negocio.codigo if forzar else 'SE DEJA COMO ESTÁ'
            self.stdout.write(self.style.WARNING(
                f'  {activo.codigo:<14} {activo.farmacia.codigo:<8} '
                f'{activo.unidad_negocio.codigo} vs {activo.farmacia.unidad_negocio.codigo} '
                f'de su farmacia: {marca}',
            ))

        objetivo = vacios + (distintos if forzar else [])
        if not objetivo:
            self.stdout.write('\nNada que corregir.')
            if distintos and not forzar:
                self.stdout.write(
                    'Las de unidad distinta necesitan una decisión: si de verdad hay que '
                    'alinearlas con su farmacia, repetí con --forzar.',
                )
            return

        if not aplicar:
            self.stdout.write(self.style.WARNING(
                f'\nSIMULACIÓN — no se tocó nada. {len(objetivo)} activo(s) cambiarían. '
                'Repetí con --aplicar.',
            ))
            return

        for activo in objetivo:
            antes = activo.unidad_negocio.codigo if activo.unidad_negocio_id else None
            activo.unidad_negocio_id = activo.farmacia.unidad_negocio_id
            activo.save(update_fields=['unidad_negocio'])
            EventoActivo.objects.create(
                activo=activo, tipo_evento=EventoActivo.TipoEvento.CORRECCION_DATOS, usuario=None,
                detalle={
                    'campo': 'unidad_negocio',
                    'anterior': antes,
                    'nuevo': activo.farmacia.unidad_negocio.codigo,
                    'motivo': f'heredada de la farmacia {activo.farmacia.codigo} (INV-05)',
                },
            )

        self.stdout.write(self.style.SUCCESS(f'\n{len(objetivo)} activo(s) corregido(s).'))
        self.stdout.write(
            'Ojo con el efecto visible: esos activos dejan de verse para los usuarios de otros '
            'clientes —que es el punto— y empiezan a aparecer en el bloque "sin sondeo" del '
            'Centro de Monitoreo, del que estaban ausentes por tener la unidad vacía.',
        )
