"""Lo que juntó el piloto de NetFlow para una farmacia: el costo de la exportación y quién
consume — ver apps.monitoreo.netflow (§10-CQ).

El costo va primero porque es la pregunta del piloto: qué porcentaje del tráfico del
enlace agrega la exportación misma, y cuántas filas hacen falta por día. Con eso se decide
si escala a la flota.

    python manage.py reporte_netflow --farmacia ML001
    python manage.py reporte_netflow --farmacia MH001 --horas 2 --top 20
"""
from datetime import timedelta

from django.core.management.base import BaseCommand, CommandError
from django.db.models import Count, F, Sum
from django.utils import timezone

from apps.catalogo.models import Estacion, Farmacia
from apps.monitoreo.models import FlujoNetflowAgregado, VentanaNetflowFarmacia

_PROTOCOLOS = {1: 'icmp', 6: 'tcp', 17: 'udp'}


def _legible(cant_bytes) -> str:
    cant = float(cant_bytes or 0)
    for unidad in ('B', 'KB', 'MB', 'GB'):
        if cant < 1024:
            return f'{cant:.1f} {unidad}'
        cant /= 1024
    return f'{cant:.1f} TB'


class Command(BaseCommand):
    help = 'Resumen del piloto de NetFlow para una farmacia: costo de la exportación y top de consumo.'

    def add_arguments(self, parser):
        parser.add_argument('--farmacia', required=True)
        parser.add_argument('--horas', type=int, default=24)
        parser.add_argument('--top', type=int, default=10)

    def handle(self, *args, **options):
        farmacia = Farmacia.objects.filter(codigo=options['farmacia'].upper()).first()
        if farmacia is None:
            raise CommandError(f'No existe la farmacia {options["farmacia"]}.')
        desde = timezone.now() - timedelta(hours=options['horas'])
        ventanas = VentanaNetflowFarmacia.objects.filter(farmacia=farmacia, inicio__gte=desde)
        totales = ventanas.aggregate(
            n=Count('pk'), datagramas=Sum('datagramas'), exportacion=Sum('bytes_exportacion'),
            medidos=Sum('bytes_medidos'), flujos=Sum('flujos'), sin_plantilla=Sum('datagramas_sin_plantilla'),
            descartados=Sum('flujos_descartados'), del_router=Sum('flujos_del_router'),
        )
        if not totales['n']:
            self.stdout.write(self.style.WARNING(
                f'{farmacia.codigo}: sin ventanas NetFlow en las últimas {options["horas"]} h. '
                '¿El router exporta? Mirá el log de netflow_collector: lista las IP que no reconoce.',
            ))
            return

        segundos = options['horas'] * 3600
        filas = FlujoNetflowAgregado.objects.filter(ventana__in=ventanas)
        self.stdout.write(self.style.MIGRATE_HEADING(f'{farmacia.codigo} — últimas {options["horas"]} h'))
        self.stdout.write('Costo de la exportación')
        self.stdout.write(f'  ventanas:            {totales["n"]}')
        self.stdout.write(
            f'  enviado por NetFlow: {_legible(totales["exportacion"])} '
            f'({totales["datagramas"]} datagramas, {8 * (totales["exportacion"] or 0) / segundos / 1000:.1f} kbps promedio)',
        )
        self.stdout.write(f'  tráfico descrito:    {_legible(totales["medidos"])}')
        if totales['medidos']:
            self.stdout.write(f'  sobrecarga:          {100 * totales["exportacion"] / totales["medidos"]:.2f} %')
        self.stdout.write(
            f'  flujos:              {totales["flujos"]} útiles, {totales["del_router"]} del propio router '
            f'(Winbox, DNS, la exportación), {totales["descartados"]} descartados, '
            f'{totales["sin_plantilla"]} datagramas sin plantilla',
        )
        self.stdout.write(f'  filas guardadas:     {filas.count()} ({filas.count() * 24 // max(options["horas"], 1)} por día)')

        estaciones = {
            str(ip): codigo for ip, codigo in
            Estacion.objects.filter(farmacia=farmacia, ip_lan__isnull=False).values_list('ip_lan', 'codigo')
        }
        consumo = F('bytes_subida') + F('bytes_bajada')

        self.stdout.write('')
        self.stdout.write('Equipos que más consumen')
        por_equipo = (
            filas.exclude(ip_local__isnull=True).values('ip_local')
            .annotate(subida=Sum('bytes_subida'), bajada=Sum('bytes_bajada'), total=Sum(consumo))
            .order_by('-total')[:options['top']]
        )
        for fila in por_equipo:
            nombre = estaciones.get(fila['ip_local'], '')
            self.stdout.write(
                f'  {fila["ip_local"]:<16} {nombre:<12} ↓ {_legible(fila["bajada"]):>10}  ↑ {_legible(fila["subida"]):>10}',
            )

        self.stdout.write('')
        self.stdout.write('Destinos que más consumen')
        por_destino = (
            filas.exclude(ip_remota__isnull=True).values('ip_remota', 'protocolo', 'puerto_remoto')
            .annotate(total=Sum(consumo), equipos=Count('ip_local', distinct=True))
            .order_by('-total')[:options['top']]
        )
        for fila in por_destino:
            protocolo = _PROTOCOLOS.get(fila['protocolo'], str(fila['protocolo']))
            self.stdout.write(
                f'  {fila["ip_remota"]:<16} {protocolo}/{fila["puerto_remoto"]:<6} '
                f'{_legible(fila["total"]):>10}  ({fila["equipos"]} equipo(s))',
            )

        resto = filas.filter(ip_local__isnull=True).aggregate(total=Sum(consumo))['total']
        if resto:
            self.stdout.write('')
            self.stdout.write(f'Fuera del top de cada ventana: {_legible(resto)}')
