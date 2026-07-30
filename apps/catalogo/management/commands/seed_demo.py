from django.contrib.auth.models import Group
from django.core.management.base import BaseCommand
from django.db import transaction

from apps.catalogo.models import Estacion, Farmacia, Grupo, UnidadNegocio


class Command(BaseCommand):
    help = 'Carga datos de ejemplo: TRX001/ML001 y TRX004/MAM01, con sus estaciones.'

    @transaction.atomic
    def handle(self, *args, **options):
        Group.objects.get_or_create(name='tecnicos')

        trx001, _ = Grupo.objects.update_or_create(
            codigo='TRX001', defaults={'nombre': 'Canal TRX001', 'version_objetivo': '4.2.1'},
        )
        trx004, _ = Grupo.objects.update_or_create(
            codigo='TRX004', defaults={'nombre': 'Canal TRX004', 'version_objetivo': '4.1.9'},
        )

        # SG/MIA las siembra la migración de datos de catalogo (0008) — reutilizarlas.
        mia = UnidadNegocio.objects.get(codigo='MIA')
        sg = UnidadNegocio.objects.get(codigo='SG')

        ml001, _ = Farmacia.objects.update_or_create(
            codigo='ML001',
            defaults={'nombre': 'Farmacia Milagro 001', 'grupo': trx001, 'unidad_negocio': mia, 'ubicacion': 'Milagro'},
        )
        mam01, _ = Farmacia.objects.update_or_create(
            codigo='MAM01',
            defaults={'nombre': 'Farmacia Mamá 01', 'grupo': trx004, 'unidad_negocio': sg, 'ubicacion': 'Guayaquil'},
        )

        estaciones_ml001 = ['ML001-ADM', 'ML001-A']
        estaciones_mam01 = ['MAM01-ADM', 'MAM01-A', 'MAM01-B', 'MAM01-C']

        creadas = 0
        for codigo in estaciones_ml001:
            _, creada = Estacion.objects.get_or_create(
                codigo=codigo,
                defaults={
                    'farmacia': ml001,
                    'estado_aprobacion': Estacion.EstadoAprobacion.APROBADA,
                    'version_pos': '4.2.1',
                    'so_nombre': 'Windows 11',
                    'so_build': '23H2',
                    'version_agente': '1.0.0',
                },
            )
            creadas += int(creada)

        for codigo in estaciones_mam01:
            so_nombre, so_build = ('Windows 10', '1909') if codigo.endswith('C') else ('Windows 10', '22H2')
            _, creada = Estacion.objects.get_or_create(
                codigo=codigo,
                defaults={
                    'farmacia': mam01,
                    'estado_aprobacion': Estacion.EstadoAprobacion.APROBADA,
                    'version_pos': '4.1.8',  # a propósito distinta a la objetivo, para ver la alerta de desactualización
                    'so_nombre': so_nombre,
                    'so_build': so_build,
                    'version_agente': '1.0.0',
                },
            )
            creadas += int(creada)

        self.stdout.write(self.style.SUCCESS(
            f'Listo. Grupos: TRX001, TRX004 · Farmacias: ML001, MAM01 · '
            f'Estaciones nuevas creadas: {creadas} (total {len(estaciones_ml001) + len(estaciones_mam01)}).',
        ))
