"""Qué estaciones tienen aplicado el último catálogo (agente 0.38 en adelante).

    python manage.py estado_catalogos

Por catálogo (servicios del POS y eventos de Windows): cuántas estaciones aprobadas están
al día, cuántas tienen una versión vieja (con la lista) y cuántas no informan versión
(agente anterior a 0.38). Es la medición que en H-03 hubo que hacer a mano leyendo el log
del worker.
"""
from django.core.management.base import BaseCommand

from apps.catalogo.models import Estacion
from apps.monitoreo.servicios_pos import versiones_vigentes

CATALOGOS = (
    ('Servicios del POS', 'servicios', 'catalogo_servicios_version'),
    ('Eventos de Windows', 'eventos', 'catalogo_eventos_version'),
)


class Command(BaseCommand):
    help = 'Muestra qué estaciones tienen aplicado el último catálogo de servicios y de eventos.'

    def handle(self, *args, **options):
        vigentes = versiones_vigentes()
        estaciones = Estacion.objects.filter(estado_aprobacion=Estacion.EstadoAprobacion.APROBADA)
        for nombre, clave, campo in CATALOGOS:
            vigente = vigentes[clave]
            sin_dato = estaciones.filter(**{campo: ''})
            viejas = estaciones.exclude(**{campo: ''}).exclude(**{campo: vigente})
            al_dia = estaciones.filter(**{campo: vigente}).count()
            self.stdout.write(f'{nombre} (versión vigente {vigente}):')
            self.stdout.write(f'  al día: {al_dia}')
            self.stdout.write(f'  sin dato (agente anterior a 0.38): {sin_dato.count()}')
            codigos = list(viejas.order_by('codigo').values_list('codigo', campo))
            estilo = self.style.WARNING if codigos else self.style.SUCCESS
            self.stdout.write(estilo(f'  desactualizadas: {len(codigos)}'))
            for codigo, version in codigos:
                self.stdout.write(f'    {codigo} (tiene {version})')
