"""Corrige la `ip_router` de las farmacias cuyo equipo de borde es del proveedor.

El problema: en las redes 192.x la IP cargada terminaba en `.1`, pero el router que
entrega TELCONET es el `.254`. El `.1` es otro equipo de la LAN del sitio.

Por qué importa y no es cosmético (medido el 18-sep-2026 sobre una muestra de 24):

- De las 12 farmacias que el panel daba por CAÍDAS, las 12 respondían en `.254`. Eran
  falsos positivos: el enlace estaba bien y el monitoreo miraba una IP que no existe.
- De las 12 que sí respondían en `.1`, las 12 respondían también en `.254`, y con MENOS
  latencia (19-25 ms contra 23-28 ms) — está más cerca, es el borde de verdad.

O sea que corregirlo no rompe ninguna de las que hoy andan y recupera las que estaban
mal reportadas. Además es condición para que el sondeo SNMP tenga sentido: preguntarle
contadores de tráfico a un equipo que no es el router no mide el enlace.

**Verifica antes de tocar.** Por cada farmacia hace ping al `.254` y solo la cambia si
responde. Cambiar 299 registros a ciegas por un patrón que se cumple "casi siempre" es
como se rompe el monitoreo de un sitio que estaba bien. Con `--forzar` se salta la
verificación, para el caso en que la corrida se haga desde un host sin ruta.

Uso:
    python manage.py corregir_ip_router_proveedor                 # simulacro, no escribe
    python manage.py corregir_ip_router_proveedor --aplicar
    python manage.py corregir_ip_router_proveedor --prefijo 10.120 --aplicar
"""
from concurrent.futures import ThreadPoolExecutor

from django.core.management.base import BaseCommand

from apps.catalogo.models import Farmacia

# Solo las redes del proveedor. Las 10.101.x / 10.201.x son los Mikrotik propios, donde
# la IP cargada ya es la correcta (son las que responden SNMP hoy) — meterlas acá las
# rompería.
PREFIJOS_POR_DEFECTO = ('192.168.', '192.169.', '192.170.')
OCTETO_VIEJO = '1'
OCTETO_NUEVO = '254'
MAX_SONDEOS_CONCURRENTES = 25


class Command(BaseCommand):
    help = 'Corrige la ip_router de .1 a .254 en las farmacias con equipo de borde del proveedor.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--aplicar', action='store_true',
            help='Escribe los cambios. Sin esto solo muestra qué haría.',
        )
        parser.add_argument(
            '--prefijo', action='append', dest='prefijos',
            help='Prefijo de red a corregir (repetible). Por defecto: '
                 + ', '.join(PREFIJOS_POR_DEFECTO),
        )
        parser.add_argument(
            '--forzar', action='store_true',
            help='No verifica por ping antes de cambiar. Solo si se corre desde un host sin ruta.',
        )

    def handle(self, *args, **opciones):
        prefijos = tuple(p if p.endswith('.') else p + '.' for p in (opciones['prefijos'] or PREFIJOS_POR_DEFECTO))
        aplicar = opciones['aplicar']
        forzar = opciones['forzar']

        candidatas = [
            f for f in Farmacia.objects.exclude(ip_router__isnull=True).order_by('codigo')
            if str(f.ip_router).startswith(prefijos) and str(f.ip_router).rsplit('.', 1)[-1] == OCTETO_VIEJO
        ]
        if not candidatas:
            self.stdout.write(self.style.WARNING(
                f'Ninguna farmacia con ip_router terminada en .{OCTETO_VIEJO} '
                f'en {", ".join(prefijos)}.'))
            return

        self.stdout.write(f'{len(candidatas)} farmacia(s) candidata(s) en {", ".join(prefijos)}.')

        if forzar:
            a_cambiar = [(f, self._nueva_ip(f), None) for f in candidatas]
            sin_responder = []
        else:
            self.stdout.write(f'Verificando que el .{OCTETO_NUEVO} responda antes de tocar nada...')
            a_cambiar, sin_responder = self._verificar(candidatas)
            self.stdout.write(
                f'  responden en .{OCTETO_NUEVO}: {len(a_cambiar)} · '
                f'no responden: {len(sin_responder)}')

        for farmacia, nueva, latencia in a_cambiar[:20]:
            detalle = f' ({latencia:.0f} ms)' if latencia is not None else ''
            self.stdout.write(f'  {farmacia.codigo:<9} {farmacia.ip_router} -> {nueva}{detalle}')
        if len(a_cambiar) > 20:
            self.stdout.write(f'  ... y {len(a_cambiar) - 20} más')

        if sin_responder:
            # No se tocan: puede ser una caída real, un sitio de baja o una red con otra
            # numeración. Cambiarles la IP escondería el problema detrás de un dato nuevo.
            self.stdout.write(self.style.WARNING(
                f'\nSe dejan sin tocar {len(sin_responder)} que tampoco responden en '
                f'.{OCTETO_NUEVO} (revisar a mano):'))
            for farmacia in sin_responder[:15]:
                self.stdout.write(f'  {farmacia.codigo:<9} {farmacia.ip_router}')
            if len(sin_responder) > 15:
                self.stdout.write(f'  ... y {len(sin_responder) - 15} más')

        if not aplicar:
            self.stdout.write(self.style.NOTICE(
                f'\nSimulacro: no se escribió nada. Repetir con --aplicar para cambiar '
                f'{len(a_cambiar)} farmacia(s).'))
            return

        for farmacia, nueva, _ in a_cambiar:
            farmacia.ip_router = nueva
            farmacia.save(update_fields=['ip_router'])

        self.stdout.write(self.style.SUCCESS(f'\n{len(a_cambiar)} farmacia(s) actualizada(s).'))
        self.stdout.write(
            'El próximo barrido de enlaces (cada 2 min) sondea la IP nueva y cierra solo '
            'las caídas que eran de la IP vieja.')

    def _nueva_ip(self, farmacia):
        return str(farmacia.ip_router).rsplit('.', 1)[0] + '.' + OCTETO_NUEVO

    def _verificar(self, candidatas):
        from apps.monitoreo.enlaces import sondear_enlace, verificar_ping_disponible

        # Si falta el binario de ping, todas darían "no responde" y el comando no
        # cambiaría nada diciendo que ninguna contesta — un diagnóstico falso.
        verificar_ping_disponible()

        def probar(farmacia):
            nueva = self._nueva_ip(farmacia)
            alcanzable, latencia = sondear_enlace(nueva)
            return farmacia, nueva, latencia, alcanzable

        a_cambiar, sin_responder = [], []
        with ThreadPoolExecutor(max_workers=MAX_SONDEOS_CONCURRENTES) as pool:
            for farmacia, nueva, latencia, alcanzable in pool.map(probar, candidatas):
                if alcanzable:
                    a_cambiar.append((farmacia, nueva, latencia))
                else:
                    sin_responder.append(farmacia)
        return a_cambiar, sin_responder
