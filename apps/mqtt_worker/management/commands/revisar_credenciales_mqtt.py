"""¿Qué estaciones tienen credencial MQTT propia en EMQX, y cuáles dependen de la compartida?

    python manage.py revisar_credenciales_mqtt            # solo las aprobadas
    python manage.py revisar_credenciales_mqtt --todas    # también pendientes y rechazadas
    python manage.py revisar_credenciales_mqtt --detalle  # una línea por estación

**Solo lectura.** Dos `GET` por estación a la API de EMQX, ninguna escritura.

POR QUÉ EXISTE

`deploy/emqx-narrow-acl-agente.sh` angosta la ACL de la credencial compartida a solo
enrolamiento, y es lo que cierra el agujero de que cualquiera con esa credencial pueda leer
—y publicar en— los tópicos de toda la flota. Está escrito desde hace semanas y sin correr,
porque su primera línea dice **"NO CORRER hasta confirmar que TODA la flota ya migró a una
credencial MQTT propia"**... y no había forma de confirmarlo.

El script intenta ayudar mostrando las conexiones activas, pero él mismo aclara el límite:
*"esto es solo un snapshot de conexiones activas en este momento, no confirma que todas las
estaciones ya migraron — una estación offline no aparece aquí aunque siga configurada con la
credencial compartida"*. Con equipos que se apagan al cerrar la farmacia, ese snapshot nunca
alcanza.

Esto pregunta **por estación, esté encendida o apagada**: si EMQX tiene su usuario propio, la
estación no depende de la compartida. Con cero dependientes, el script se puede correr; con
una sola, correrlo la deja muda.

Lo que este comando **no** puede decir: qué credencial está escrita en el `config.txt` de cada
equipo. Una estación puede tener usuario propio en EMQX y seguir conectándose con la
compartida si nunca aplicó su credencial nueva — por eso el resumen también separa las que
**están conectadas ahora** (`estado_conexion`), que es donde el riesgo sería inmediato.
"""
from django.core.management.base import BaseCommand

from apps.catalogo.models import Estacion
from apps.mqtt_worker.emqx_admin import consultar_credencial_estacion


class Command(BaseCommand):
    help = 'Informa qué estaciones tienen credencial MQTT propia en EMQX. Solo lectura.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--todas', action='store_true',
            help='Incluye pendientes y rechazadas. Por defecto solo las aprobadas, que son las '
                 'que el angostamiento de ACL dejaría mudas.',
        )
        parser.add_argument(
            '--detalle', action='store_true',
            help='Una línea por estación, en vez del resumen.',
        )

    def handle(self, *args, **opciones):
        qs = Estacion.objects.select_related('farmacia').order_by('codigo')
        if not opciones['todas']:
            qs = qs.filter(estado_aprobacion=Estacion.EstadoAprobacion.APROBADA)

        estaciones = list(qs)
        if not estaciones:
            self.stdout.write('No hay estaciones que revisar.')
            return

        propias, dependientes, indeterminadas, sin_acl = [], [], [], []
        for estacion in estaciones:
            resultado = consultar_credencial_estacion(estacion)
            if resultado is None:
                self.stderr.write(
                    'EMQX_ADMIN_CONFIG no está configurado, así que esta instalación no usa '
                    'credenciales por estación: TODA la flota depende de la compartida y '
                    'deploy/emqx-narrow-acl-agente.sh no se puede correr.',
                )
                return
            if resultado['usuario'] is None:
                indeterminadas.append(estacion)
            elif resultado['usuario']:
                propias.append(estacion)
                if resultado['acl'] is False:
                    sin_acl.append(estacion)
            else:
                dependientes.append(estacion)

            if opciones['detalle']:
                estado = {True: 'propia', False: 'COMPARTIDA', None: '¿?'}[resultado['usuario']]
                acl = {True: 'con acl', False: 'SIN ACL', None: 'acl ¿?'}[resultado['acl']]
                self.stdout.write(
                    f'  {estacion.codigo:<14} {estado:<11} {acl:<8} {estacion.estado_conexion}',
                )

        total = len(estaciones)
        self.stdout.write('')
        self.stdout.write(f'Revisadas {total} estación(es):')
        self.stdout.write(f'  con credencial propia en EMQX : {len(propias)}')
        self.stdout.write(f'  dependen de la compartida     : {len(dependientes)}')
        if indeterminadas:
            self.stdout.write(f'  no se pudo determinar         : {len(indeterminadas)}')

        if sin_acl:
            # Un usuario sin reglas puede conectarse y no publicar nada: el síntoma es una
            # estación que autentica y no reporta, sin error en ningún log (EMQX deniega en
            # silencio). `reaplicar_acls_mqtt --aplicar` lo arregla sin rotar contraseñas.
            self.stdout.write(self.style.WARNING(
                f'\n{len(sin_acl)} con usuario pero SIN reglas de ACL: '
                f'{", ".join(e.codigo for e in sin_acl)}\n'
                'Esas autentican y EMQX les deniega todo en silencio. '
                'Correr `reaplicar_acls_mqtt --aplicar` (no rota contraseñas).',
            ))

        self.stdout.write('')
        if indeterminadas:
            self.stderr.write(
                'VEREDICTO: no concluyente. EMQX no respondió con claridad sobre '
                f'{len(indeterminadas)} estación(es): {", ".join(e.codigo for e in indeterminadas[:10])}. '
                'Repetir cuando el broker responda bien; no correr el angostamiento a ciegas.',
            )
            return

        if dependientes:
            conectadas = [
                e for e in dependientes if e.estado_conexion == Estacion.EstadoConexion.ONLINE
            ]
            self.stderr.write(
                f'VEREDICTO: NO correr deploy/emqx-narrow-acl-agente.sh todavía. '
                f'{len(dependientes)} estación(es) dependen de la credencial compartida y '
                f'quedarían mudas:\n  ' + ', '.join(e.codigo for e in dependientes) + '\n'
                + (f'De esas, {len(conectadas)} están conectadas ahora mismo.\n' if conectadas else '')
                + 'Para migrarlas: `seed_scripts_migracion_mqtt` crea el script que fuerza su '
                're-enrolamiento (borra identidad.json y reinicia el servicio).',
            )
            return

        self.stdout.write(self.style.SUCCESS(
            'VEREDICTO: ninguna estación depende de la credencial compartida.\n'
            'Es la condición que deploy/emqx-narrow-acl-agente.sh pide confirmar antes de '
            'correrse, y este comando la confirma por estación — no por snapshot de '
            'conexiones, que es lo que ese script advierte que no alcanza.',
        ))
        self.stdout.write(
            '\nOjo con lo que esto NO prueba: que el `config.txt` de cada equipo ya use su '
            'credencial propia. Una estación con usuario en EMQX puede seguir conectándose con '
            'la compartida si nunca aplicó la nueva. Antes de angostar, mirar en EMQX cuántos '
            'clientes hay conectados con el usuario compartido (lo hace el propio script).',
        )
