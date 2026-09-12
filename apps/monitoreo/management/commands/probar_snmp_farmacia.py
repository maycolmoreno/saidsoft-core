"""Diagnostica, paso a paso, por qué SAIDSOFT no lee el Mikrotik de una farmacia.

Existe porque "no anda el SNMP" tiene cuatro causas distintas que se ven igual desde el
panel (un dato que no aparece), y cada una se arregla en un lugar diferente: la red, el
`/snmp` del router, la comunidad, o la interfaz WAN. Este comando las separa.

    python manage.py probar_snmp_farmacia ML001
    python manage.py probar_snmp_farmacia ML001 --comunidad public

Sin `--comunidad` usa la que espera el sistema: el código de la farmacia en minúscula
(ver `apps.monitoreo.mikrotik._comunidad_para`). Pasarla sirve para distinguir "SNMP
apagado" de "SNMP prendido con otra comunidad", que es la confusión más común.

No escribe nada: es solo diagnóstico.
"""
import asyncio
import subprocess

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from apps.catalogo.models import Farmacia
from apps.monitoreo.mikrotik import _comunidad_para, _puerto

# sysName: el OID más básico que responde cualquier agente SNMP. Si esto contesta, el
# SNMP del router está prendido y la comunidad es correcta; el resto ya es cuestión de
# qué interfaz leer.
_OID_SYSNAME = '1.3.6.1.2.1.1.5.0'


class Command(BaseCommand):
    help = 'Diagnostica la lectura SNMP del Mikrotik de una farmacia, paso a paso.'

    def add_arguments(self, parser):
        parser.add_argument('farmacia', help='Código de la farmacia, ej. ML001.')
        parser.add_argument(
            '--comunidad', default='',
            help='Probar con esta comunidad en vez de la esperada (el código en minúscula).',
        )

    def handle(self, *args, **options):
        farmacia = Farmacia.objects.filter(codigo=options['farmacia'].upper()).first()
        if farmacia is None:
            raise CommandError(f'No existe la farmacia {options["farmacia"].upper()}.')
        if farmacia.ip_router is None:
            raise CommandError(
                f'{farmacia.codigo} no tiene `ip_router` cargada. Se carga con '
                'importar_red_farmacias_xlsx.',
            )

        ip = str(farmacia.ip_router)
        comunidad = options['comunidad'] or _comunidad_para(farmacia)
        puerto = _puerto()
        self.stdout.write(f'{farmacia.codigo} — {ip}:{puerto}, comunidad "{comunidad}"\n')

        if not self._paso_ping(ip):
            return
        if not self._paso_snmp(ip, puerto, comunidad, farmacia, options['comunidad']):
            return
        self._paso_interfaz(farmacia, puerto)

    def _ok(self, texto):
        self.stdout.write(self.style.SUCCESS(f'  OK   {texto}'))

    def _falla(self, titulo, *lineas):
        self.stdout.write(self.style.ERROR(f'  FALLA  {titulo}'))
        for linea in lineas:
            self.stdout.write(f'         {linea}')

    def _paso_ping(self, ip):
        """Sin ruta no tiene sentido mirar nada más: SNMP viaja por la misma red."""
        try:
            alcanzable = subprocess.run(
                ['ping', '-c', '2', '-W', '2', ip], capture_output=True, timeout=10,
            ).returncode == 0
        except (subprocess.TimeoutExpired, OSError):
            alcanzable = False
        if alcanzable:
            self._ok('1. Responde al ping — hay ruta de red hasta el equipo.')
            return True
        self._falla(
            '1. No responde al ping.',
            'Sin ruta hasta el equipo, el SNMP tampoco va a llegar. Puede ser el enlace',
            'caído, la IP desactualizada en el catálogo, o que este host no tenga ruta.',
        )
        return False

    def _paso_snmp(self, ip, puerto, comunidad, farmacia, comunidad_forzada):
        nombre = asyncio.run(self._consultar(ip, puerto, comunidad, _OID_SYSNAME))
        if nombre is not None:
            self._ok(f'2. Responde SNMP con esa comunidad — el equipo dice llamarse "{nombre}".')
            return True

        # Distinguir "apagado" de "otra comunidad" es la mitad del diagnóstico: con
        # `public` se prueba si hay agente SNMP escuchando pero con otro nombre.
        if not comunidad_forzada and comunidad != 'public':
            con_public = asyncio.run(self._consultar(ip, puerto, 'public', _OID_SYSNAME))
            if con_public is not None:
                self._falla(
                    f'2. SNMP está PRENDIDO pero la comunidad "{comunidad}" no es la correcta.',
                    f'Con "public" sí responde (el equipo dice llamarse "{con_public}").',
                    f'SAIDSOFT espera la comunidad "{comunidad}" — el código de la farmacia en',
                    'minúscula. En el router:',
                    f'  /snmp community set [find name=public] name={comunidad}',
                )
                return False

        self._falla(
            '2. No responde SNMP (ni con esa comunidad ni con "public").',
            'El equipo está en la red pero no tiene agente SNMP escuchando, o un firewall',
            'bloquea el UDP 161. En RouterOS:',
            f'  /snmp community add name={comunidad} addresses={self._red_del_servidor()}',
            '  /snmp set enabled=yes contact="SAIDSOFT"',
            '  /ip firewall filter add chain=input protocol=udp dst-port=161 \\',
            f'      src-address={self._red_del_servidor()} action=accept place-before=0',
        )
        return False

    def _paso_interfaz(self, farmacia, puerto):
        """Lo que el poller real hace: resolver la WAN y leer sus contadores."""
        from apps.monitoreo.mikrotik import _sondear_farmacia

        resultado = asyncio.run(_sondear_farmacia(farmacia, puerto))
        if resultado is not None:
            _f, rx, tx = resultado
            self._ok(f'3. Contadores leídos — recibidos={rx} enviados={tx}.')
            self.stdout.write(self.style.SUCCESS(
                '\nTodo listo: esta farmacia ya se puede leer desde el servidor.',
            ))
            return
        self._falla(
            '3. Responde SNMP pero no se pudo leer la interfaz WAN.',
            'SAIDSOFT resuelve la WAN por la ruta por defecto (ipRouteIfIndex de 0.0.0.0).',
            'Si el router no expone esa tabla, hay que habilitarle el árbol de interfaces:',
            '  /snmp community set [find] read-access=yes',
            'Verificar a mano qué interfaces expone:',
            f'  snmpwalk -v2c -c {_comunidad_para(farmacia)} {farmacia.ip_router} 1.3.6.1.2.1.2.2.1.2',
        )

    def _red_del_servidor(self):
        """Subred del central, para sugerir la regla de acceso. Es una pista, no un dato
        con autoridad: el router puede verlo salir por otra IP si hay NAT de por medio."""
        base = getattr(settings, 'ARCHIVOS_BASE_URL', '')
        host = base.split('://', 1)[-1].split('/', 1)[0].split(':', 1)[0]
        partes = host.split('.')
        if len(partes) == 4 and all(p.isdigit() for p in partes):
            return f'{partes[0]}.{partes[1]}.{partes[2]}.0/24'
        return '<subred-del-servidor>/24'

    async def _consultar(self, ip, puerto, comunidad, oid):
        """Un GET SNMP v2c. Devuelve el valor como texto, o None si no contestó."""
        from pysnmp.hlapi.v3arch.asyncio import (
            CommunityData, ContextData, ObjectIdentity, ObjectType, SnmpEngine,
            UdpTransportTarget, get_cmd,
        )

        try:
            error_ind, error_est, _idx, enlaces = await get_cmd(
                SnmpEngine(), CommunityData(comunidad, mpModel=1),
                await UdpTransportTarget.create((ip, puerto), timeout=3, retries=0),
                ContextData(), ObjectType(ObjectIdentity(oid)),
            )
        except Exception:
            return None
        if error_ind or error_est or not enlaces:
            return None
        return str(enlaces[0][1])
