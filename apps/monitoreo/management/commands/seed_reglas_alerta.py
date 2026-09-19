"""Crea el juego inicial de reglas de alerta. Sin esto, el monitoreo detecta y no avisa.

Por qué hace falta: todo el motor de alertas está construido y probado —condición
sostenida, escalamiento, ventanas de mantenimiento, canal de Teams— y en producción hay
CERO reglas cargadas. La capacidad existe y no está en uso: hoy alguien tiene que estar
mirando una pantalla para enterarse de algo.

    python manage.py seed_reglas_alerta            # simula
    python manage.py seed_reglas_alerta --aplicar

## La trampa que define qué se siembra y qué no

Las farmacias cierran y apagan los equipos. Una regla que alerte por ausencia dispararía
todas las noches, en cada local, y el resultado conocido de cientos de falsos positivos
por noche es que se terminan ignorando todas — incluidas las verdaderas.

Por eso el reparto no es arbitrario:

- Las reglas de **métrica** (CPU, RAM, disco, errores y servicios del POS) se evalúan
  solo cuando llega un reporte de la estación. Una estación apagada no manda nada,
  así que no pueden dispararse de noche. Son seguras y se siembran ACTIVAS.

- `sin_heartbeat` y `agente_caido_red_viva` se disparan por AUSENCIA. Se siembran
  DESACTIVADAS a propósito: hay que decidir primero cómo se distingue "cerró la farmacia"
  de "se cayó el equipo" —horario de atención, o una VentanaMantenimiento recurrente— y
  recién ahí activarlas desde el panel.

Ninguna regla abre mantenimiento automático. Mismo criterio que el default del modelo:
que activar una regla no empiece a generar órdenes de trabajo sin que nadie lo decida.

## Por qué estos umbrales

Salen de lo que rompe la operación de una farmacia, no de números redondos:

- **Disco**: un disco lleno frena el POS y corrompe su base local. 90% avisa con tiempo,
  95% ya es urgente.
- **RAM y CPU**: sostenidos 15 minutos, no un pico. Un pico al abrir el POS es normal.
- **Errores del POS**: es la única métrica que habla del negocio y no del equipo. Diez
  errores en una ventana de reporte no es ruido: es una caja que no está vendiendo bien.
- **BitLocker**: no es una falla, es cumplimiento. Warning, no crítica.
- **Servicios del POS**: dos reglas para la misma métrica, una crítica y una de
  advertencia. `evaluar_regla_servicio_pos` elige según el campo `critico` del
  servicio: sin la base local la caja no vende, sin Odoo sigue vendiendo.
"""
from django.contrib.auth.models import User
from django.core.management.base import BaseCommand, CommandError

from apps.monitoreo.models import Metrica, ReglaAlerta

# (nombre, metrica, operador, umbral, duracion_minutos, severidad, activa, por_que)
REGLAS = [
    (
        'Disco casi lleno (90%)', Metrica.DISCO_USADO_PCT, ReglaAlerta.Operador.GTE, 90, 15,
        ReglaAlerta.Severidad.WARNING, True,
        'Da margen para liberar espacio antes de que el POS empiece a fallar.',
    ),
    (
        'Disco lleno (95%)', Metrica.DISCO_USADO_PCT, ReglaAlerta.Operador.GTE, 95, 5,
        ReglaAlerta.Severidad.CRITICAL, True,
        'A esta altura el POS ya puede no poder escribir su base local.',
    ),
    (
        'RAM al limite (92%)', Metrica.RAM_USADA_PCT, ReglaAlerta.Operador.GTE, 92, 15,
        ReglaAlerta.Severidad.WARNING, True,
        'Sostenido 15 min: un pico al abrir el POS es normal y no amerita alerta.',
    ),
    (
        'CPU saturada (90%)', Metrica.CPU_CARGA_PCT, ReglaAlerta.Operador.GTE, 90, 15,
        ReglaAlerta.Severidad.WARNING, True,
        'Mismo criterio que RAM: 15 minutos separan un problema de un pico.',
    ),
    (
        'Errores del POS en una ventana', Metrica.POS_ERRORES, ReglaAlerta.Operador.GTE, 10, 0,
        ReglaAlerta.Severidad.WARNING, True,
        'La unica metrica que habla del negocio: una caja que no vende bien.',
    ),
    (
        'BitLocker deshabilitado', Metrica.BITLOCKER_DESHABILITADO, ReglaAlerta.Operador.GTE, 0, 0,
        ReglaAlerta.Severidad.WARNING, True,
        'Cumplimiento, no falla: el equipo funciona, el disco no esta cifrado.',
    ),
    # Dos reglas para la misma metrica, y no es redundancia: `evaluar_regla_servicio_pos`
    # elige cual aplicar segun el campo `critico` del servicio. Sin la base local la caja
    # no vende; sin Odoo sigue vendiendo y sincroniza despues. Si hubiera una sola regla,
    # las dos caidas abririan la misma alerta y "critica" dejaria de significar "anda a la
    # farmacia".
    (
        'Servicio critico del POS sin responder', Metrica.SERVICIO_POS_CAIDO,
        ReglaAlerta.Operador.GTE, 0, 0, ReglaAlerta.Severidad.CRITICAL, True,
        'La base local del POS: sin ella la caja no puede vender.',
    ),
    (
        'Servicio del POS sin responder (no critico)', Metrica.SERVICIO_POS_CAIDO,
        ReglaAlerta.Operador.GTE, 0, 0, ReglaAlerta.Severidad.WARNING, True,
        'Base central, Odoo y recargas: la caja sigue vendiendo y sincroniza despues.',
    ),
    # Dos niveles, y la diferencia entre ellos es la que decide si hace falta un viaje.
    # A los 120 s el agente descarta TODO mensaje firmado (VENTANA_TIMESTAMP_SEGUNDOS),
    # incluido el script que le arreglaria el reloj: pasada esa marca la estacion solo se
    # recupera en el local. Se siembran ACTIVAS, a diferencia de sin_heartbeat: se evaluan
    # con el latido, no por ausencia, asi que una farmacia cerrada no las dispara.
    (
        'Reloj corrido (30 s)', Metrica.DESFASE_RELOJ, ReglaAlerta.Operador.GTE, 30, 0,
        ReglaAlerta.Severidad.WARNING, True,
        'Mismo umbral que ya usa el panel. Todavia obedece comandos: se arregla en remoto.',
    ),
    (
        'Reloj por quedar incomunicado (90 s)', Metrica.DESFASE_RELOJ, ReglaAlerta.Operador.GTE, 90, 0,
        ReglaAlerta.Severidad.CRITICAL, True,
        'Ultima llamada: quedan 30 s antes de los 120 s en que deja de obedecer y hay que ir al local.',
    ),
    (
        'Sin heartbeat (30 min)', Metrica.SIN_HEARTBEAT, ReglaAlerta.Operador.GTE, 30, 0,
        ReglaAlerta.Severidad.CRITICAL, False,
        'DESACTIVADA: se dispara por ausencia y las farmacias apagan equipos al cerrar. '
        'Activar recien cuando se decida como distinguir cierre de caida.',
    ),
    (
        'Agente sin reportar con red viva (30 min)', Metrica.AGENTE_CAIDO_RED_VIVA,
        ReglaAlerta.Operador.GTE, 30, 0, ReglaAlerta.Severidad.CRITICAL, False,
        'DESACTIVADA por el mismo motivo, aunque es mas precisa: exige que MeshCentral vea '
        'el equipo en linea, o sea que descarta el caso "se corto internet".',
    ),
]


class Command(BaseCommand):
    help = 'Crea el juego inicial de reglas de alerta (globales, para todas las unidades).'

    def add_arguments(self, parser):
        parser.add_argument(
            '--aplicar', action='store_true',
            help='Crea de verdad. Sin esto, solo muestra lo que haria.',
        )
        parser.add_argument(
            '--actualizar', action='store_true',
            help='Ajusta umbral, duracion y severidad de las reglas que ya existen. Sin '
                 'esto se dejan intactas: una regla afinada a mano no se pisa.',
        )

    def handle(self, *args, **options):
        aplicar = options['aplicar']
        actualizar = options['actualizar']

        admin = User.objects.filter(is_superuser=True).order_by('id').first()
        if admin is None:
            raise CommandError(
                'No hay ningun superusuario: ReglaAlerta.creado_por es obligatorio. '
                'Crea uno con `manage.py createsuperuser`.',
            )

        existentes = {r.nombre: r for r in ReglaAlerta.objects.all()}
        creadas, actualizadas, intactas = [], [], []

        for nombre, metrica, operador, umbral, duracion, severidad, activa, por_que in REGLAS:
            regla = existentes.get(nombre)
            if regla is None:
                creadas.append((nombre, activa, por_que))
                if aplicar:
                    ReglaAlerta.objects.create(
                        nombre=nombre, metrica=metrica, operador=operador, umbral=umbral,
                        duracion_minutos=duracion, severidad=severidad, activo=activa,
                        creado_por=admin,
                    )
            elif actualizar:
                actualizadas.append(nombre)
                if aplicar:
                    regla.metrica = metrica
                    regla.operador = operador
                    regla.umbral = umbral
                    regla.duracion_minutos = duracion
                    regla.severidad = severidad
                    # `activo` NO se toca al actualizar: si alguien activo sin_heartbeat
                    # despues de resolver el tema de los horarios, volver a apagarla seria
                    # deshacer una decision tomada.
                    regla.save(update_fields=[
                        'metrica', 'operador', 'umbral', 'duracion_minutos', 'severidad',
                    ])
            else:
                intactas.append(nombre)

        for nombre, activa, por_que in creadas:
            estado = self.style.SUCCESS('ACTIVA  ') if activa else self.style.WARNING('apagada ')
            self.stdout.write('  %s %s' % (estado, nombre))
            self.stdout.write('           %s' % por_que)

        self.stdout.write('')
        if intactas:
            self.stdout.write(
                '%d regla(s) ya existian y se dejaron intactas: %s'
                % (len(intactas), ', '.join(intactas)),
            )
            self.stdout.write('Para ajustarles umbral o severidad, repeti con --actualizar.')
        if actualizadas:
            self.stdout.write('%d regla(s) actualizada(s): %s' % (len(actualizadas), ', '.join(actualizadas)))

        if not aplicar:
            self.stdout.write(self.style.WARNING(
                'Simulacion: se crearian %d regla(s). Repeti con --aplicar.' % len(creadas),
            ))
            return

        self.stdout.write(self.style.SUCCESS('%d regla(s) creada(s).' % len(creadas)))
        self.stdout.write('')
        self.stdout.write(
            'Las dos que quedaron apagadas se disparan por AUSENCIA de reporte, y las '
            'farmacias apagan equipos al cerrar. Activarlas antes de resolver eso llenaria '
            'de falsos positivos cada noche — y el resultado conocido es que se terminan '
            'ignorando todas, incluidas las verdaderas.',
        )
