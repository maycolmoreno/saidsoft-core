"""Cierra las caídas de enlace congeladas de las farmacias dadas de baja (`activa=False`).

Por qué hace falta: `activa=False` frena el SONDEO (`sondear_enlaces_farmacias` filtra
por ese flag), pero no toca lo que el último sondeo ya dejó escrito. Una farmacia que
estaba caída cuando se la dio de baja queda con un `EventoEnlaceFarmacia` abierto
(`fin=None`) y un `EstadoEnlaceFarmacia` en `alcanzable=False` — y como ya nadie la
sondea, **nada la recupera nunca**: la caída se congela y el sitio aparece caído para
siempre en el Centro de Monitoreo, en el KPI "enlaces caídos", en `/enlaces` del bot y
en la tabla de enlaces.

Eso es exactamente el síntoma que se reportó con GP063 el 2-oct-2026: "la deshabilité
en el admin para no recibir más la alerta de caída de enlace y me sigue saliendo".

Desde ese día el admin de `Farmacia` limpia solo al destildar la casilla (ver
`FarmaciaAdmin.save_model`), y el aviso y las pantallas filtran por `activa=True`. Este
comando es para las bajas que ya estaban hechas ANTES de ese arreglo: el flag ya está en
False y el evento colgado sigue ahí, porque nadie va a volver a guardar esa farmacia en
el admin solo para disparar la limpieza.

    python manage.py limpiar_enlaces_de_baja                    # informa qué haría
    python manage.py limpiar_enlaces_de_baja --aplicar          # escribe
    python manage.py limpiar_enlaces_de_baja --farmacia GP063   # una sola

Simula por defecto y exige `--aplicar`, como el resto de los comandos que escriben en
masa. La caída se CIERRA y no se borra: el historial de lo que de verdad pasó mientras
el sitio estaba en operación es lo que sostiene cualquier reclamo de SLA hacia atrás.

Idempotente: correrlo dos veces no cambia nada la segunda.
"""
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.db.models import Q

from apps.catalogo.models import Farmacia
from apps.monitoreo.enlaces import dar_de_baja_enlace


class Command(BaseCommand):
    help = 'Cierra las caídas de enlace congeladas de las farmacias con activa=False.'

    def add_arguments(self, parser):
        parser.add_argument('--farmacia', default='', help='Limpiar solo esta farmacia (código).')
        parser.add_argument(
            '--aplicar', action='store_true',
            help='Escribe los cambios. Sin esto solo informa qué haría.',
        )

    def handle(self, *args, **options):
        # Solo las dadas de baja QUE TIENEN algo colgado. Las demás ya están limpias y
        # listarlas haría que el informe no se pueda leer: son cientos.
        sucias = Q(eventos_enlace__fin__isnull=True) | Q(estado_enlace__alcanzable__isnull=False)
        candidatas = Farmacia.objects.filter(activa=False).filter(sucias).distinct()

        if options['farmacia']:
            codigo = options['farmacia'].upper()
            farmacia = Farmacia.objects.filter(codigo=codigo).first()
            if farmacia is None:
                raise CommandError(f'No existe la farmacia {codigo}.')
            if farmacia.activa:
                raise CommandError(
                    f'{codigo} está ACTIVA. Este comando solo limpia farmacias dadas de baja: '
                    'si lo que querés es que deje de avisar, destildá `activa` en el admin '
                    '(eso ya hace la limpieza solo).',
                )
            candidatas = candidatas.filter(codigo=codigo)

        candidatas = list(candidatas.order_by('codigo'))
        if not candidatas:
            self.stdout.write(self.style.SUCCESS(
                'Nada que limpiar: ninguna farmacia dada de baja tiene caídas de enlace abiertas '
                'ni estado de enlace colgado.',
            ))
            return

        self.stdout.write(f'{len(candidatas)} farmacia(s) dada(s) de baja con rastro de monitoreo:')
        for farmacia in candidatas:
            abiertas = farmacia.eventos_enlace.filter(fin__isnull=True).count()
            self.stdout.write(f'  {farmacia.codigo:<10} {abiertas} caída(s) abierta(s)')

        if not options['aplicar']:
            self.stdout.write(self.style.WARNING(
                '\nSimulación: no se escribió nada. Volvé a correrlo con --aplicar.',
            ))
            return

        with transaction.atomic():
            totales = {'caidas_cerradas': 0, 'estados_limpiados': 0}
            for farmacia in candidatas:
                resultado = dar_de_baja_enlace(farmacia)
                for clave, valor in resultado.items():
                    totales[clave] += valor

        self.stdout.write(self.style.SUCCESS(
            f'\nListo: {totales["caidas_cerradas"]} caída(s) cerrada(s) y '
            f'{totales["estados_limpiados"]} estado(s) limpiado(s) en {len(candidatas)} farmacia(s).',
        ))
