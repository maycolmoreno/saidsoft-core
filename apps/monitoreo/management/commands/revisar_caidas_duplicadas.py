"""Busca farmacias con MÁS DE UNA caída de enlace abierta al mismo tiempo.

De solo lectura: informa y sale. No cierra nada y no toca el historial.

Por qué existe. `EventoEnlaceFarmacia` no tiene constraint que impida dos filas abiertas
para la misma farmacia; lo único que lo evita es la bandera `estaba_caida` de
`registrar_sondeo`. Si por cualquier camino quedaran dos, hasta el 10-oct-2026 la
recuperación cerraba solo la más reciente y la otra se volvía un fantasma: una caída
abierta para siempre, en un sitio que responde, que ninguna pantalla podía hacer
desaparecer (hallazgo D-04 de docs/auditoria/centro-monitoreo/).

Ese modo de fallo ya está cerrado —la recuperación cierra todas— pero eso solo arregla de
aquí en adelante. Este comando responde la otra mitad: **si quedó alguno de antes.**

No se resuelven solos a propósito. Cerrar un evento es escribirle un `fin` que nadie
midió, y esta tabla es la línea base de disponibilidad con la que se discute un SLA con el
proveedor. Decidir qué hacer con un duplicado es una decisión humana con el dato delante.

    python manage.py revisar_caidas_duplicadas

Código de salida 1 si encontró alguno, para poder encadenarlo en una verificación.
"""
from django.core.management.base import BaseCommand
from django.db.models import Count


class Command(BaseCommand):
    help = 'Informa farmacias con más de una caída de enlace abierta. Solo lectura.'

    def handle(self, *args, **options):
        from apps.monitoreo.models import EventoEnlaceFarmacia

        duplicadas = (
            EventoEnlaceFarmacia.objects
            .filter(fin__isnull=True)
            .values('farmacia_id', 'farmacia__codigo')
            .annotate(abiertas=Count('pk'))
            .filter(abiertas__gt=1)
            .order_by('-abiertas', 'farmacia__codigo')
        )

        if not duplicadas:
            self.stdout.write(self.style.SUCCESS(
                'Ninguna farmacia tiene más de una caída abierta.',
            ))
            return

        self.stdout.write(self.style.WARNING(
            '%d farmacia(s) con más de una caída abierta:' % len(duplicadas),
        ))
        for fila in duplicadas:
            eventos = (
                EventoEnlaceFarmacia.objects
                .filter(farmacia_id=fila['farmacia_id'], fin__isnull=True)
                .order_by('inicio')
            )
            self.stdout.write('  %-10s %d abiertas' % (fila['farmacia__codigo'], fila['abiertas']))
            for evento in eventos:
                # La más antigua es la que tiene el `inicio` real de la caída; las demás
                # son las sospechosas. Se imprimen las dos cosas para poder decidir.
                self.stdout.write('      #%-8s inicio %s  circuito %s' % (
                    evento.pk, evento.inicio.strftime('%Y-%m-%d %H:%M'),
                    evento.circuito_proveedor or '(sin cargar)',
                ))

        self.stdout.write('')
        self.stdout.write(
            'La más antigua de cada grupo es la que tiene el inicio real. Cerrar un '
            'duplicado es escribirle un "fin" que nadie midió, así que la decisión es '
            'humana: este comando no toca nada.',
        )
        raise SystemExit(1)
