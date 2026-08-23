import datetime

from django.contrib.auth.models import User
from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from apps.activos.models import Activo, CategoriaEquipo
from apps.mantenimiento import services
from apps.mantenimiento.models import (
    ActividadChecklist, Mantenimiento, MantenimientoProgramado, ResultadoTecnico, TipoMantenimiento,
)


class Command(BaseCommand):
    help = 'Carga datos de ejemplo del módulo de mantenimiento: checklist, un programado y mantenimientos.'

    @transaction.atomic
    def handle(self, *args, **options):
        admin = User.objects.filter(is_superuser=True).order_by('id').first()
        if admin is None:
            self.stderr.write(self.style.ERROR('Necesitas al menos un superusuario antes de correr este seed.'))
            return

        categorias = list(CategoriaEquipo.objects.all()[:3])
        items = []
        for orden, nombre in enumerate([
            'Limpieza de ventiladores y disipadores', 'Actualización de sistema operativo',
            'Revisión de batería/UPS', 'Prueba de puertos y periféricos',
        ], start=1):
            item, _ = ActividadChecklist.objects.update_or_create(nombre=nombre, defaults={'orden': orden})
            if categorias:
                item.categorias.set(categorias)
            items.append(item)

        equipos = list(Activo.objects.exclude(estado=Activo.Estado.DADO_DE_BAJA)[:2])
        if not equipos:
            self.stderr.write(self.style.ERROR('Necesitas al menos un activo (corre seed_activos primero).'))
            return

        equipo_principal = equipos[0]
        cliente = equipo_principal.colaborador_actual

        if not MantenimientoProgramado.objects.filter(equipo=equipo_principal).exists():
            MantenimientoProgramado.objects.create(
                equipo=equipo_principal, tecnico=admin, frecuencia_dias=180,
                fecha_ultimo=datetime.date(2026, 6, 20),
                fecha_proximo=datetime.date(2026, 12, 20),
                observaciones='Mantenimiento preventivo semestral.',
            )

        tipo_preventivo = TipoMantenimiento.objects.filter(codigo='preventivo').first()
        tipo_correctivo = TipoMantenimiento.objects.filter(codigo='correctivo').first()

        if not Mantenimiento.objects.filter(descripcion__startswith='[seed]').exists():
            m1 = services.crear_mantenimiento_manual(
                equipos=[equipo_principal], tecnico=admin, cliente=cliente,
                descripcion='[seed] Limpieza preventiva programada', tipo_mantenimiento=tipo_preventivo,
                fecha_programada=timezone.now(), usuario=admin,
            )
            services.iniciar_mantenimiento(mantenimiento=m1, usuario=admin)
            for item in items[:2]:
                services.registrar_actividad_checklist(
                    mantenimiento=m1, actividad=item, realizada=True, usuario=admin,
                )

            if len(equipos) > 1:
                m2 = services.crear_mantenimiento_manual(
                    equipos=equipos, tecnico=admin, cliente=cliente,
                    descripcion='[seed] Revisión correctiva multi-equipo', tipo_mantenimiento=tipo_correctivo,
                    fecha_programada=timezone.now(), usuario=admin, equipo_principal=equipos[1],
                )
                services.iniciar_mantenimiento(mantenimiento=m2, usuario=admin)
                services.cerrar_mantenimiento(
                    mantenimiento=m2, resultado_tecnico=ResultadoTecnico.REPARADO, usuario=admin,
                )

        self.stdout.write(self.style.SUCCESS(
            '4 ítems de checklist, 1 mantenimiento programado y hasta 2 mantenimientos de ejemplo '
            '(uno en proceso, otro cerrado).',
        ))
