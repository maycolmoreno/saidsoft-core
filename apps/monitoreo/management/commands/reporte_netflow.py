"""Lo que juntó el piloto de NetFlow para una farmacia: el costo de la exportación y quién
consume — ver apps.monitoreo.netflow (§10-CQ). Lo mismo que la pantalla Monitoreo →
NetFlow del panel; las dos leen `resumen_netflow`.

El costo va primero porque es la pregunta del piloto: qué porcentaje del tráfico del
enlace agrega la exportación misma, y cuántas filas hacen falta por día. Con eso se decide
si escala a la flota.

    python manage.py reporte_netflow --farmacia ML001
    python manage.py reporte_netflow --farmacia MH001 --horas 2 --top 20
"""
from django.core.management.base import BaseCommand, CommandError

from apps.catalogo.models import Farmacia
from apps.monitoreo.netflow import resumen_netflow


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
        resumen = resumen_netflow(farmacia, horas=options['horas'], top=options['top'])
        if resumen is None:
            self.stdout.write(self.style.WARNING(
                f'{farmacia.codigo}: sin ventanas NetFlow en las últimas {options["horas"]} h. '
                '¿El router exporta? Mirá el log de netflow_collector: lista las IP que no reconoce.',
            ))
            return

        t = resumen['totales']
        self.stdout.write(self.style.MIGRATE_HEADING(f'{farmacia.codigo} — últimas {options["horas"]} h'))
        self.stdout.write('Costo de la exportación')
        self.stdout.write(f'  ventanas:            {t["ventanas"]}')
        self.stdout.write(
            f'  enviado por NetFlow: {_legible(t["exportacion"])} '
            f'({t["datagramas"]} datagramas, {t["kbps_exportacion"]} kbps promedio)',
        )
        self.stdout.write(f'  tráfico descrito:    {_legible(t["medidos"])}')
        if t['sobrecarga_pct'] is not None:
            self.stdout.write(f'  sobrecarga:          {t["sobrecarga_pct"]:.2f} %')
        self.stdout.write(
            f'  flujos:              {t["flujos"]} útiles, {t["del_router"]} del propio router '
            f'(Winbox, DNS, la exportación), {t["descartados"]} descartados, '
            f'{t["sin_plantilla"]} datagramas sin plantilla',
        )
        self.stdout.write(f'  filas guardadas:     {t["filas"]} ({t["filas_por_dia"]} por día)')

        self.stdout.write('')
        self.stdout.write('Equipos que más consumen')
        for fila in resumen['equipos']:
            self.stdout.write(
                f'  {fila["ip_local"]:<16} {fila["estacion"]:<12} '
                f'↓ {_legible(fila["bajada"]):>10}  ↑ {_legible(fila["subida"]):>10}',
            )

        self.stdout.write('')
        self.stdout.write('Destinos que más consumen')
        for fila in resumen['destinos']:
            self.stdout.write(
                f'  {fila["ip_remota"]:<16} {fila["protocolo_nombre"]}/{fila["puerto_remoto"]:<6} '
                f'{_legible(fila["total"]):>10}  ({fila["equipos"]} equipo(s))',
            )

        if resumen['resto']:
            self.stdout.write('')
            self.stdout.write(f'Fuera del top de cada ventana: {_legible(resumen["resto"])}')
