"""Rollout del agente: publica la orden de actualizar a muchas estaciones a la vez.

    python manage.py actualizar_agente_flota                        # simula: dice a quién le llegaría
    python manage.py actualizar_agente_flota --limite 3 --aplicar   # CANARIO: tres equipos primero
    python manage.py actualizar_agente_flota --aplicar              # el resto
    python manage.py actualizar_agente_flota --agente agente-prueba-0.33 --aplicar
    python manage.py actualizar_agente_flota --unidad SG --aplicar
    python manage.py actualizar_agente_flota --estaciones ML001-A,ML001-B --aplicar

**Por qué un comando y no el botón del panel.** El botón existe y está bien para una
estación (`/estaciones/<pk>/agente/actualizar/`), pero el rollout de una versión nueva son
66 estaciones hoy y ~1.800 proyectadas: con el botón son 66 clics, y nadie los da. Mismo
argumento que `pausar_flota`, y el mismo molde.

**Empezar con `--limite`, siempre.** El agente se actualiza reemplazando su propio
ejecutable, así que un build malo no deja una estación con una versión vieja: la deja **sin
agente**, y recuperarla es ir al equipo (queda un `.bak` al lado del ejecutable para poder
revertir a mano). Tres estaciones primero, confirmar que reportan la versión nueva en su
latido, y después el resto. Esto no lo puede imponer el comando —a veces el rollout urgente
es justificado— pero sí puede decirlo cada vez que corre.

**No exige que la estación esté en línea, a diferencia del botón.** La orden va RETENIDA:
EMQX la guarda y la entrega apenas la estación reconecta, que es exactamente para lo que el
retenido está ahí (ver `enviar_actualizacion_agente`). El botón exige ONLINE porque responde
en un modal y necesita decir algo inmediato; un rollout no. Las farmacias apagan los equipos
al cerrar, así que "todas encendidas a la vez" no pasa nunca.

**Publicar no es aplicar.** Lo único que confirma la actualización es que la estación reporte
la versión nueva en su latido, y `manejar_heartbeat` limpia el retenido cuando eso pasa. El
resumen final separa las dos cosas, igual que `pausar_flota` con la pausa.

**No registra eventos de auditoría**, por el mismo criterio que `pausar_flota`: la acción
masiva deja su rastro en la salida del comando y en los latidos que confirman. El botón del
panel sí audita, porque ahí hay una persona y una estación concretas.
"""
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from apps.catalogo.models import Estacion, UnidadNegocio, VersionAgente
from apps.catalogo.services import enviar_actualizacion_agente
from apps.monitoreo.umbrales import ONLINE_UMBRAL_MINUTOS


class Command(BaseCommand):
    help = 'Publica la actualización del agente a las estaciones aprobadas. Simula salvo --aplicar.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--aplicar', action='store_true',
            help='Publica de verdad. Sin esto solo muestra a quién le llegaría.',
        )
        # `--version` no se puede: BaseCommand lo reserva para imprimir la versión de
        # Django y argparse rechaza el duplicado. `--agente` es el nombre que ya usa
        # `armar_paquete_agente` para lo mismo, por el mismo motivo.
        parser.add_argument(
            '--agente', default='',
            help='Versión a desplegar (ej. agente-prueba-0.33). Vacío = la última cargada.',
        )
        parser.add_argument(
            '--limite', type=int, default=0,
            help='Máximo de estaciones a las que publicar, para hacer canario. 0 = sin límite.',
        )
        parser.add_argument(
            '--unidad', default='',
            help='Código de unidad de negocio (SG/MIA/7DIAS). Vacío = todas.',
        )
        parser.add_argument(
            '--estaciones', default='',
            help='Códigos separados por coma, para alcanzar solo unas pocas.',
        )

    def handle(self, *args, **opciones):
        aplicar = opciones['aplicar']

        if opciones['agente']:
            version = VersionAgente.objects.filter(version=opciones['agente']).first()
            if version is None:
                cargadas = list(
                    VersionAgente.objects.order_by('-fecha_creacion').values_list('version', flat=True)[:5],
                )
                self.stderr.write(
                    f'No existe la versión {opciones["agente"]!r}. '
                    f'Últimas cargadas: {", ".join(cargadas) or "ninguna"}.',
                )
                return
        else:
            version = VersionAgente.objects.order_by('-fecha_creacion').first()
            if version is None:
                self.stderr.write(
                    'No hay ninguna VersionAgente cargada. Subí el ejecutable primero '
                    '(Admin → Versiones de agente).',
                )
                return

        qs = Estacion.objects.filter(
            estado_aprobacion=Estacion.EstadoAprobacion.APROBADA,
        ).select_related('farmacia__unidad_negocio').order_by('codigo')

        if opciones['unidad']:
            unidad = UnidadNegocio.objects.filter(codigo=opciones['unidad'].upper()).first()
            if unidad is None:
                self.stderr.write(f'No existe la unidad de negocio {opciones["unidad"]!r}.')
                return
            qs = qs.filter(farmacia__unidad_negocio=unidad)

        if opciones['estaciones']:
            codigos = [c.strip().upper() for c in opciones['estaciones'].split(',') if c.strip()]
            qs = qs.filter(codigo__in=codigos)

        # Las que ya reportan esta versión no se tocan: republicarles el retenido no cambia
        # nada, les haría reemplazar el ejecutable por el mismo y ensucia el resumen de lo
        # que de verdad se movió. Mismo criterio que `pausar_flota`.
        ya_estaban = qs.filter(version_agente=version.version).count()
        objetivo = list(qs.exclude(version_agente=version.version))

        if opciones['limite'] > 0:
            objetivo = objetivo[:opciones['limite']]

        self.stdout.write(f'Versión a desplegar: {version.version} (sha256 {version.sha256[:12]}…)')
        self.stdout.write(f'Alcance: {len(objetivo)} estación(es); {ya_estaban} ya la reportan.')
        if not objetivo:
            self.stdout.write('Nada que hacer.')
            return

        limite = timezone.now() - timedelta(minutes=ONLINE_UMBRAL_MINUTOS)
        en_linea = sum(1 for e in objetivo if e.ultimo_heartbeat and e.ultimo_heartbeat >= limite)
        apagadas = len(objetivo) - en_linea

        if not aplicar:
            self.stdout.write(self.style.WARNING('SIMULACIÓN — no se publica nada. Repetí con --aplicar.'))
            for e in objetivo[:20]:
                viva = e.ultimo_heartbeat and e.ultimo_heartbeat >= limite
                estado = 'reportando' if viva else 'sin latido (le llegará al encender)'
                self.stdout.write(f'  {e.codigo:<14} {e.version_agente or "(sin versión)":<22} {estado}')
            if len(objetivo) > 20:
                self.stdout.write(f'  ... y {len(objetivo) - 20} más')
            if opciones['limite'] == 0 and len(objetivo) > 5:
                self.stdout.write(self.style.WARNING(
                    f'\nSon {len(objetivo)} estaciones de una sola vez. El agente se reemplaza a sí '
                    'mismo: si el build está mal, quedan SIN agente y hay que ir al equipo.\n'
                    'Conviene empezar con --limite 3, confirmar que reportan la versión nueva en su '
                    'latido, y después el resto.',
                ))
            return

        publicadas, fallaron = 0, []
        for estacion in objetivo:
            if enviar_actualizacion_agente(estacion, version):
                publicadas += 1
            else:
                fallaron.append(estacion.codigo)

        self.stdout.write(self.style.SUCCESS(f'Órdenes publicadas: {publicadas}.'))
        if fallaron:
            self.stderr.write(
                f'NO se pudo publicar a {len(fallaron)}: {", ".join(fallaron)}. '
                'Revisá la conexión al broker antes de dar el rollout por hecho.',
            )

        self.stdout.write(
            f'\nLa orden va retenida: {en_linea} estación(es) la están recibiendo ahora y '
            f'{apagadas} la recibirán al encender.\n'
            'Publicar no es aplicar: lo confirma la columna de versión del agente en el panel, '
            'que sale del latido de cada estación. Revisala antes de seguir con el resto.',
        )
