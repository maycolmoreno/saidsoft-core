import datetime

from django.contrib.auth.models import User
from django.core.management.base import BaseCommand
from django.db import transaction

from apps.activos import services
from apps.activos.models import (
    Activo, Bodega, Cargo, CategoriaEquipo, Colaborador, Departamento, Marca, OrdenCompra, TipoConsumible,
    Ubicacion,
)


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

        marcas = {}
        for nombre in ['Dell', 'HP', 'Epson']:
            marcas[nombre], _ = Marca.objects.get_or_create(nombre=nombre)

        categorias = {}
        for codigo, nombre in [
            ('LAPTOP-CORP', 'Laptop corporativa'), ('DESKTOP-CORP', 'Desktop corporativo'),
            ('IMPRESORA', 'Impresora'),
        ]:
            categorias[codigo], _ = CategoriaEquipo.objects.update_or_create(
                codigo=codigo, defaults={'nombre': nombre},
            )

        services.registrar_ingreso_stock(
            bodega=bodegas['CUENCA'], tipo_consumible=TipoConsumible.objects.get(codigo='MOUSE'), cantidad=15,
        )
        services.registrar_ingreso_stock(
            bodega=bodegas['CUENCA'], tipo_consumible=TipoConsumible.objects.get(codigo='TECLADO'), cantidad=10,
        )

        depto_admin, _ = Departamento.objects.update_or_create(
            nombre='Administración', defaults={'tipo': Departamento.Tipo.ADMINISTRATIVO},
        )
        depto_comercial, _ = Departamento.objects.update_or_create(
            nombre='Comercial', defaults={'tipo': Departamento.Tipo.OPERATIVO},
        )
        cargo_analista, _ = Cargo.objects.get_or_create(nombre='Analista', departamento=depto_admin)
        cargo_vendedor, _ = Cargo.objects.get_or_create(nombre='Vendedor', departamento=depto_comercial)

        ubicacion_cuenca, _ = Ubicacion.objects.update_or_create(
            nombre='Agencia Cuenca', defaults={'ciudad': 'Cuenca', 'departamento': depto_admin},
        )
        ubicacion_machala, _ = Ubicacion.objects.update_or_create(
            nombre='Agencia Machala', defaults={'ciudad': 'Machala', 'departamento': depto_comercial},
        )

        colaborador1, _ = Colaborador.objects.update_or_create(
            cedula='0102030405',
            defaults={
                'nombre': 'María Fernanda Ortiz', 'cargo': cargo_analista, 'ubicacion': ubicacion_cuenca,
                'sucursal': 'Cuenca', 'zona': 'Sierra',
            },
        )
        colaborador2, _ = Colaborador.objects.update_or_create(
            cedula='0203040506',
            defaults={
                'nombre': 'Luis Andrés Cabrera', 'cargo': cargo_vendedor, 'ubicacion': ubicacion_machala,
                'sucursal': 'Machala', 'zona': 'Costa',
            },
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
                tipo=Activo.Tipo.LAPTOP, marca=marcas['Dell'], categoria=categorias['LAPTOP-CORP'],
                modelo='Latitude 5440', numero_serie='SN-LAP-0001',
                procesador='Intel Core i5-1335U', ram_gb=16, almacenamiento_gb=512,
                condicion_al_recibir='nuevo',
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
                tipo=Activo.Tipo.DESKTOP, marca=marcas['HP'], categoria=categorias['DESKTOP-CORP'],
                modelo='ProDesk 400', numero_serie='SN-DSK-0001',
                procesador='Intel Core i5-13500', ram_gb=8, almacenamiento_gb=256,
                condicion_al_recibir='nuevo',
                fecha_compra=datetime.date(2026, 6, 20), vencimiento_garantia=datetime.date(2028, 6, 20),
                orden_compra=oc, bodega=bodegas['MACHALA'], usuario=admin,
            )
            services.registrar_envio_reparacion(
                activo=desktop, motivo='falla_tecnica', detalle_motivo='No enciende, posible fuente dañada',
                usuario=admin,
            )

            services.registrar_ingreso(
                tipo=Activo.Tipo.IMPRESORA, marca=marcas['Epson'], categoria=categorias['IMPRESORA'],
                modelo='L3250', numero_serie='SN-IMP-0001', condicion_al_recibir='nuevo',
                fecha_compra=datetime.date(2026, 6, 20), vencimiento_garantia=datetime.date(2027, 6, 20),
                orden_compra=oc, bodega=bodegas['LOJA'], usuario=admin,
            )

        self.stdout.write(self.style.SUCCESS(
            'Listo: 4 bodegas, 5 tipos de consumible, 2 colaboradores, 1 OC recibida y 3 activos '
            '(1 asignado, 1 en reparación, 1 en bodega).',
        ))
