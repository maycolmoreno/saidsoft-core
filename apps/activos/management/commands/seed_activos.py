import datetime

from django.contrib.auth.models import User
from django.core.management.base import BaseCommand
from django.db import transaction

from apps.activos import services
from apps.activos.models import Activo, Bodega, Colaborador, OrdenCompra, TipoConsumible


class Command(BaseCommand):
    help = 'Carga datos de ejemplo del módulo de activos: bodegas, colaboradores, una OC y algunos activos.'

    @transaction.atomic
    def handle(self, *args, **options):
        admin = User.objects.filter(is_superuser=True).order_by('id').first()
        if admin is None:
            self.stderr.write(self.style.ERROR('Necesitas al menos un superusuario antes de correr este seed.'))
            return

        bodegas = {}
        for codigo, nombre in [
            ('MACHALA', 'Bodega Machala'), ('LOJA', 'Bodega Loja'),
            ('CUENCA', 'Bodega Cuenca'), ('PORTOVIEJO', 'Bodega Portoviejo'),
        ]:
            bodegas[codigo], _ = Bodega.objects.update_or_create(
                codigo=codigo, defaults={'nombre': nombre, 'custodio': admin},
            )

        for codigo, nombre in [
            ('MOUSE', 'Mouse'), ('TECLADO', 'Teclado'), ('AURICULARES', 'Auriculares'),
            ('CABLE-HDMI', 'Cable HDMI'), ('TONER-HP26A', 'Tóner HP 26A'),
        ]:
            TipoConsumible.objects.update_or_create(codigo=codigo, defaults={'nombre': nombre})

        services.registrar_ingreso_stock(
            bodega=bodegas['CUENCA'], tipo_consumible=TipoConsumible.objects.get(codigo='MOUSE'), cantidad=15,
        )
        services.registrar_ingreso_stock(
            bodega=bodegas['CUENCA'], tipo_consumible=TipoConsumible.objects.get(codigo='TECLADO'), cantidad=10,
        )

        colaborador1, _ = Colaborador.objects.update_or_create(
            cedula='0102030405',
            defaults={'nombre': 'María Fernanda Ortiz', 'cargo': 'Analista', 'sucursal': 'Cuenca', 'zona': 'Sierra'},
        )
        colaborador2, _ = Colaborador.objects.update_or_create(
            cedula='0203040506',
            defaults={'nombre': 'Luis Andrés Cabrera', 'cargo': 'Vendedor', 'sucursal': 'Machala', 'zona': 'Costa'},
        )

        oc, _ = OrdenCompra.objects.update_or_create(
            numero_oc='OC-2026-001',
            defaults={
                'proveedor': 'TecnoImport S.A.', 'fecha_emision': datetime.date(2026, 6, 15),
                'estado': OrdenCompra.Estado.RECIBIDA, 'recibido_por': admin,
            },
        )
        oc.bodegas_destino.set([bodegas['CUENCA'], bodegas['MACHALA']])

        if not Activo.objects.filter(orden_compra=oc).exists():
            laptop = services.registrar_ingreso(
                tipo=Activo.Tipo.LAPTOP, marca='Dell', modelo='Latitude 5440', numero_serie='SN-LAP-0001',
                fecha_compra=datetime.date(2026, 6, 20), vencimiento_garantia=datetime.date(2029, 6, 20),
                orden_compra=oc, bodega=bodegas['CUENCA'], usuario=admin,
            )
            services.registrar_asignacion(
                activo=laptop, colaborador=colaborador1,
                estado_fisico_entrega=Activo.EstadoFisico.NUEVO, usuario=admin,
            )
            services.registrar_consumible_entregado(
                activo=laptop, tipo_consumible=TipoConsumible.objects.get(codigo='MOUSE'),
                cantidad=1, usuario=admin,
            )

            desktop = services.registrar_ingreso(
                tipo=Activo.Tipo.DESKTOP, marca='HP', modelo='ProDesk 400', numero_serie='SN-DSK-0001',
                fecha_compra=datetime.date(2026, 6, 20), vencimiento_garantia=datetime.date(2028, 6, 20),
                orden_compra=oc, bodega=bodegas['MACHALA'], usuario=admin,
            )
            services.registrar_envio_reparacion(
                activo=desktop, motivo='falla_tecnica', detalle_motivo='No enciende, posible fuente dañada',
                usuario=admin,
            )

            services.registrar_ingreso(
                tipo=Activo.Tipo.IMPRESORA, marca='Epson', modelo='L3250', numero_serie='SN-IMP-0001',
                fecha_compra=datetime.date(2026, 6, 20), vencimiento_garantia=datetime.date(2027, 6, 20),
                orden_compra=oc, bodega=bodegas['LOJA'], usuario=admin,
            )

        self.stdout.write(self.style.SUCCESS(
            'Listo: 4 bodegas, 5 tipos de consumible, 2 colaboradores, 1 OC recibida y 3 activos '
            '(1 asignado, 1 en reparación, 1 en bodega).',
        ))
