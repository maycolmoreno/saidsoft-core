"""Diagnostica, paso a paso, qué puede y qué no puede leer SAIDSOFT de una impresora.

Existe por el mismo motivo que `probar_snmp_farmacia`: "no se lee la impresora" tiene
varias causas que se ven iguales desde el panel (un dato que no aparece), y cada una se
arregla en un lugar distinto — la red, el SNMP del equipo, la community, o que el modelo
simplemente no publique esa métrica. Este comando las separa.

    python manage.py probar_snmp_impresora CR-IMP-0004
    python manage.py probar_snmp_impresora 10.111.9.66
    python manage.py probar_snmp_impresora CR-IMP-0004 --comunidad otra

Acepta un código de activo o una IP suelta. La IP sirve para una impresora que todavía no
está inventariada, que es el caso de descubrimiento: primero se averigua si habla SNMP y
después se decide si se carga.

**La community NO sigue la convención de las farmacias.** `apps.monitoreo.mikrotik._comunidad_para`
devuelve el código de la farmacia en minúscula, que es una convención de los Mikrotik; las
impresoras vienen casi siempre en `public`. Por eso acá el default es `public` y no se
reusa esa función.

No escribe nada: es solo diagnóstico. Lo que sí hace es comparar lo que dice el equipo
contra lo que dice el inventario — si la IP cargada apunta a otra impresora, todo lo que
se monitoree de ese activo es de otro equipo (mismo chequeo que `EquipoBordeFarmacia.nombre_coincide`).

Medido contra una RICOH MP C2503 real el 8-oct-2026 (ver `docs/auditoria-snmp.md` §0.1).
Dos cosas que ese equipo obligó a hacer distinto de lo que parecía razonable en papel:

- **El índice de la tabla de suministros NO es CMYK.** En esa Ricoh `.1.2` es el tóner
  RESIDUAL, no el cian. Hay que caminar la tabla y leer `prtMarkerSuppliesType` y
  `prtMarkerColorantValue`; cablear `.9.1.1` a `.9.1.4` habría leído el residual como
  "cian al 100%".
- **`hrPrinterDetectedErrorState` puede devolver 0x00 con la impresora sin papel.** Pasó.
  Lo que sí avisa es `hrDeviceStatus`, el texto de la consola y `prtInputCurrentLevel` por
  bandeja — el último es el único numérico, o sea el único umbralizable.

Las constantes de OID viven acá por ahora. La FASE 2 las mueve a un catálogo declarativo
(ver el plan en `docs/auditoria-snmp.md` §15); mientras no exista, un comando de
diagnóstico autosuficiente es mejor que media abstracción.
"""
import asyncio
import ipaddress
import subprocess

from django.core.management.base import BaseCommand, CommandError

from apps.activos.models import Activo
from apps.monitoreo.mikrotik import _motor_snmp, _texto_snmp

# --- Estándar. Printer-MIB = RFC 3805, HOST-RESOURCES-MIB = RFC 2790 ---
_OID_SYS_NAME = '1.3.6.1.2.1.1.5.0'
_OID_SYS_DESCR = '1.3.6.1.2.1.1.1.0'
_OID_SYS_OBJECT_ID = '1.3.6.1.2.1.1.2.0'
_OID_SERIE = '1.3.6.1.2.1.43.5.1.1.17.1'
_OID_CONTADOR = '1.3.6.1.2.1.43.10.2.1.4.1.1'
_OID_CONTADOR_UNIDAD = '1.3.6.1.2.1.43.10.2.1.3.1.1'
_OID_CONSOLA = '1.3.6.1.2.1.43.16.5.1.2.1.1'
_OID_DEVICE_STATUS = '1.3.6.1.2.1.25.3.2.1.5.1'
_OID_PRINTER_STATUS = '1.3.6.1.2.1.25.3.5.1.1.1'
_OID_ERROR_STATE = '1.3.6.1.2.1.25.3.5.1.2.1'

# Tablas: hay que recorrerlas, el índice no es fijo.
_OID_SUP_TIPO = '1.3.6.1.2.1.43.11.1.1.5'
_OID_SUP_DESCR = '1.3.6.1.2.1.43.11.1.1.6'
_OID_SUP_UNIDAD = '1.3.6.1.2.1.43.11.1.1.7'
_OID_SUP_MAXIMO = '1.3.6.1.2.1.43.11.1.1.8'
_OID_SUP_NIVEL = '1.3.6.1.2.1.43.11.1.1.9'
_OID_COLORANTE = '1.3.6.1.2.1.43.12.1.1.4'
_OID_BANDEJA_NIVEL = '1.3.6.1.2.1.43.8.2.1.10'
_OID_ALERTA_DESCR = '1.3.6.1.2.1.43.18.1.1.8'

# prtMarkerSuppliesType (RFC 3805). Solo los que aparecen en la práctica.
_TIPOS_SUMINISTRO = {
    '1': 'otro', '2': 'desconocido', '3': 'tóner', '4': 'tóner residual', '5': 'tinta',
    '6': 'cartucho de tinta', '9': 'revelador', '10': 'aceite de fusor',
    '15': 'cinta', '21': 'kit de mantenimiento',
}
_UNIDADES = {'7': 'impresiones', '8': 'hojas', '13': 'décimas de gramo', '19': 'porcentaje'}
_ESTADO_IMPRESORA = {'1': 'other', '2': 'unknown', '3': 'inactiva', '4': 'imprimiendo', '5': 'calentando'}
_ESTADO_DISPOSITIVO = {'1': 'unknown', '2': 'funcionando', '3': 'ADVERTENCIA', '4': 'en prueba', '5': 'CAÍDO'}
# El enterprise de sysObjectID identifica al fabricante sin depender de parsear sysDescr.
_FABRICANTES = {
    '11': 'HP', '253': 'Xerox', '367': 'Ricoh', '1248': 'Epson', '1347': 'Kyocera',
    '641': 'Lexmark', '2435': 'Brother', '1602': 'Canon', '236': 'Samsung',
}
# Centinelas de prtMarkerSuppliesLevel / MaxCapacity (RFC 3805).
_CENTINELAS = {-1: 'other', -2: 'desconocido', -3: 'queda algo (cantidad indeterminada)'}

_COMUNIDAD_POR_DEFECTO = 'public'


def _enmascarar(valor: str) -> str:
    """`"public"` -> `"pub****"`. La community viaja en texto plano por la red y es
    adivinable; la salida de este comando termina pegada en un ticket."""
    if not valor:
        return '(vacía)'
    return valor[:3] + '****'


class Command(BaseCommand):
    help = 'Diagnostica qué puede leer SAIDSOFT de una impresora por SNMP, paso a paso.'

    def add_arguments(self, parser):
        parser.add_argument('objetivo', help='Código de activo (ej. CR-IMP-0004) o IP.')
        parser.add_argument(
            '--comunidad', default=_COMUNIDAD_POR_DEFECTO,
            help='Community de lectura. Default: "%s", que es lo que traen casi todas.'
                 % _COMUNIDAD_POR_DEFECTO,
        )
        parser.add_argument('--puerto', type=int, default=161)

    def handle(self, *args, **options):
        activo, ip = self._resolver(options['objetivo'])
        comunidad, puerto = options['comunidad'], options['puerto']

        etiqueta = '%s (%s)' % (activo.codigo, ip) if activo else ip
        self.stdout.write('%s — community %s, puerto %d\n' % (
            etiqueta, _enmascarar(comunidad), puerto,
        ))

        if not self._paso_ping(ip):
            return
        datos = self._paso_snmp(ip, comunidad, puerto)
        if datos is None:
            return

        self._paso_identidad(datos, activo)
        self._paso_suministros(ip, comunidad, puerto)
        self._paso_estado(ip, comunidad, puerto, datos)

    # --- resolución del objetivo -------------------------------------------------

    def _resolver(self, objetivo):
        """`(activo|None, ip)`. Un argumento que parsea como IP se usa tal cual."""
        try:
            ipaddress.ip_address(objetivo)
            return None, objetivo
        except ValueError:
            pass

        activo = Activo.objects.filter(codigo__iexact=objetivo).first()
        if activo is None:
            raise CommandError(
                'No existe el activo "%s" ni es una IP válida. Se puede pasar la IP suelta '
                'para una impresora que todavía no está inventariada.' % objetivo,
            )
        ip, origen = activo.ip_efectiva
        if ip is None:
            raise CommandError(
                '%s no tiene IP cargada. Se carga con la vista de datos técnicos o por el '
                'admin.' % activo.codigo,
            )
        if origen == 'agente':
            self.stdout.write(self.style.WARNING(
                'Ojo: la IP de %s la reporta el agente de su estación, no está cargada a '
                'mano. Un activo con estación RMM no suele ser una impresora de red.\n'
                % activo.codigo,
            ))
        return activo, str(ip)

    # --- pasos --------------------------------------------------------------------

    def _paso_ping(self, ip):
        """Sin ruta no tiene sentido mirar nada más: el SNMP viaja por la misma red."""
        try:
            alcanzable = subprocess.run(
                ['ping', '-c', '2', '-W', '2', ip], capture_output=True, timeout=10,
            ).returncode == 0
        except (subprocess.TimeoutExpired, OSError):
            alcanzable = False
        if alcanzable:
            self._ok('1. Responde al ping — hay ruta hasta el equipo.')
            return True
        self._falla(
            '1. No responde al ping.',
            'Sin ruta el SNMP tampoco va a llegar. Puede ser el equipo apagado, la IP',
            'desactualizada en el inventario, o que este host no alcance esa red.',
        )
        return False

    def _paso_snmp(self, ip, comunidad, puerto):
        """Los escalares de una sola consulta, o None si no contesta."""
        oids = [_OID_SYS_NAME, _OID_SYS_DESCR, _OID_SYS_OBJECT_ID, _OID_SERIE,
                _OID_CONTADOR, _OID_CONTADOR_UNIDAD, _OID_CONSOLA,
                _OID_DEVICE_STATUS, _OID_PRINTER_STATUS]
        valores = asyncio.run(self._get(ip, comunidad, puerto, oids))
        if valores is not None:
            self._ok('2. Responde SNMP — el equipo dice llamarse "%s".' % (valores[0] or '(sin nombre)'))
            return dict(zip(oids, valores))

        # Distinguir "SNMP apagado" de "otra community" es la mitad del diagnóstico.
        if comunidad != _COMUNIDAD_POR_DEFECTO:
            con_default = asyncio.run(self._get(ip, _COMUNIDAD_POR_DEFECTO, puerto, [_OID_SYS_NAME]))
            if con_default is not None:
                self._falla(
                    '2. SNMP está PRENDIDO pero con otra community.',
                    'Con "%s" sí responde (se llama "%s").'
                    % (_COMUNIDAD_POR_DEFECTO, con_default[0]),
                    'Volvé a correr sin --comunidad, o corregí la community del equipo.',
                )
                return None

        self._falla(
            '2. No responde SNMP, pero sí responde al ping.',
            'El paquete llega, así que no es la red: o el agente SNMP está apagado en la',
            'impresora, o usa otra community. Se habilita en su interfaz web, en',
            'Configuración de red > SNMP. Dejar la community de SOLO LECTURA: SNMP v2c la',
            'manda en texto plano y es adivinable.',
            'Para descartar la community, probar con --comunidad <otra>.',
        )
        return None

    def _paso_identidad(self, datos, activo):
        self.stdout.write('')
        self.stdout.write('   IDENTIDAD')
        descr = datos[_OID_SYS_DESCR]
        self.stdout.write('     modelo (sysDescr)  %s' % (descr or '—'))
        self.stdout.write('     fabricante         %s' % self._fabricante(datos[_OID_SYS_OBJECT_ID]))
        serie = datos[_OID_SERIE]
        self.stdout.write('     número de serie    %s' % (serie or 'NO LO PUBLICA'))

        if activo is None:
            self.stdout.write('')
            self.stdout.write(self.style.WARNING(
                '     Esta IP no está inventariada. Si el equipo es de la empresa, darlo de '
                'alta como Activo con la serie y el modelo de arriba.',
            ))
            return

        # El mismo chequeo de integridad gratis que hace EquipoBordeFarmacia.nombre_coincide:
        # si la IP cargada apunta a otro equipo, todo lo que se monitoree es de otro.
        if serie and activo.numero_serie and serie.strip() != activo.numero_serie.strip():
            self._falla(
                '   La serie del equipo NO coincide con la del inventario.',
                'equipo: %s   inventario: %s' % (serie, activo.numero_serie),
                'O la IP de %s apunta a otra impresora, o se cambió el equipo y nadie lo'
                % activo.codigo,
                'registró. Hasta resolverlo, lo que se monitoree de ese activo es de otro.',
            )
        elif serie and not activo.numero_serie:
            self.stdout.write(self.style.WARNING(
                '     %s no tiene serie cargada: la de arriba se puede copiar tal cual.'
                % activo.codigo,
            ))

    def _paso_suministros(self, ip, comunidad, puerto):
        """Camina la tabla. NO asume que el índice sea CMYK — ver el docstring del módulo."""
        self.stdout.write('')
        self.stdout.write('   CONSUMIBLES')
        tablas = asyncio.run(self._walks(ip, comunidad, puerto, [
            _OID_SUP_TIPO, _OID_SUP_DESCR, _OID_SUP_UNIDAD, _OID_SUP_MAXIMO,
            _OID_SUP_NIVEL, _OID_COLORANTE,
        ]))
        if tablas is None:
            self.stdout.write(self.style.ERROR('     No se pudo recorrer la tabla.'))
            return
        tipos, descrs, unidades, maximos, niveles, colorantes = tablas
        if not niveles:
            self.stdout.write(self.style.WARNING(
                '     Este equipo NO publica prtMarkerSuppliesTable: no hay monitoreo de '
                'tóner por MIB estándar.',
            ))
            return

        for indice in sorted(niveles):
            tipo = _TIPOS_SUMINISTRO.get(tipos.get(indice), tipos.get(indice, '?'))
            color = colorantes.get(indice, '')
            self.stdout.write('     %-26s %-14s %s%s' % (
                descrs.get(indice, '(sin descripción)'),
                self._porcentaje(niveles.get(indice), maximos.get(indice), unidades.get(indice)),
                tipo,
                '' if color in ('', 'other') else ' / %s' % color,
            ))

    def _paso_estado(self, ip, comunidad, puerto, datos):
        self.stdout.write('')
        self.stdout.write('   ESTADO Y VOLUMEN')
        unidad = _UNIDADES.get(datos[_OID_CONTADOR_UNIDAD], datos[_OID_CONTADOR_UNIDAD] or '?')
        self.stdout.write('     contador           %s %s' % (datos[_OID_CONTADOR] or '—', unidad))
        if datos[_OID_CONTADOR_UNIDAD] == '8':
            self.stdout.write(self.style.WARNING(
                '                        son HOJAS, no impresiones: a doble faz una hoja '
                'son dos impresiones. Importa si se factura por clic.',
            ))
        self.stdout.write('     impresora          %s' % _ESTADO_IMPRESORA.get(
            datos[_OID_PRINTER_STATUS], datos[_OID_PRINTER_STATUS] or '—'))
        dispositivo = _ESTADO_DISPOSITIVO.get(
            datos[_OID_DEVICE_STATUS], datos[_OID_DEVICE_STATUS] or '—')
        self.stdout.write('     dispositivo        %s' % dispositivo)
        if datos[_OID_CONSOLA]:
            self.stdout.write('     pantalla           %s' % datos[_OID_CONSOLA])

        bandejas = asyncio.run(self._walks(ip, comunidad, puerto, [_OID_BANDEJA_NIVEL]))
        if bandejas and bandejas[0]:
            for indice, valor in sorted(bandejas[0].items()):
                vacia = valor == '0'
                linea = '     bandeja %-10s %s hojas' % (indice, valor)
                self.stdout.write(self.style.ERROR(linea + '   <- VACÍA') if vacia else linea)

        alertas = asyncio.run(self._walks(ip, comunidad, puerto, [_OID_ALERTA_DESCR]))
        if alertas and alertas[0]:
            self.stdout.write('')
            self.stdout.write('   ALERTAS DEL EQUIPO')
            for _indice, texto in sorted(alertas[0].items()):
                self.stdout.write(self.style.WARNING('     %s' % texto))

        # El bitmask estándar no es confiable: una Ricoh MP C2503 real devolvió 0x00
        # estando sin papel. Se informa para que nadie lo tome como "sin problemas".
        crudo = asyncio.run(self._get_bytes(ip, comunidad, puerto, _OID_ERROR_STATE))
        if crudo is not None and not any(crudo):
            self.stdout.write('')
            self.stdout.write(self.style.WARNING(
                '   hrPrinterDetectedErrorState viene en 0x00 (ningún bit). En varios '
                'equipos eso NO significa "sin problemas": mirar el estado del '
                'dispositivo, la pantalla y las bandejas de arriba.',
            ))

    # --- SNMP ---------------------------------------------------------------------

    async def _get(self, ip, comunidad, puerto, oids):
        """Lista de valores como texto, o None si el equipo no contestó. Nunca lanza."""
        from pysnmp.hlapi.v3arch.asyncio import (
            CommunityData, ContextData, ObjectIdentity, ObjectType, UdpTransportTarget, get_cmd,
        )

        with _motor_snmp() as engine:
            try:
                target = await UdpTransportTarget.create((ip, puerto), timeout=3, retries=1)
                error_ind, error_est, _idx, enlaces = await get_cmd(
                    engine, CommunityData(comunidad), target, ContextData(),
                    *(ObjectType(ObjectIdentity(o)) for o in oids),
                )
            except Exception:
                return None
            if error_ind or error_est or not enlaces:
                return None
            return [_texto_snmp(valor) for _nombre, valor in enlaces]

    async def _get_bytes(self, ip, comunidad, puerto, oid):
        """Los bytes crudos de un OCTET STRING, para el bitmask de errores."""
        from pysnmp.hlapi.v3arch.asyncio import (
            CommunityData, ContextData, ObjectIdentity, ObjectType, UdpTransportTarget, get_cmd,
        )

        with _motor_snmp() as engine:
            try:
                target = await UdpTransportTarget.create((ip, puerto), timeout=3, retries=1)
                error_ind, error_est, _idx, enlaces = await get_cmd(
                    engine, CommunityData(comunidad), target, ContextData(),
                    ObjectType(ObjectIdentity(oid)),
                )
                if error_ind or error_est or not enlaces:
                    return None
                return bytes(enlaces[0][1].asOctets())
            except Exception:
                return None

    async def _walks(self, ip, comunidad, puerto, bases):
        """Una lista de `{indice: valor}`, en el orden de `bases`. None si falló."""
        from pysnmp.hlapi.v3arch.asyncio import (
            CommunityData, ContextData, ObjectIdentity, ObjectType, UdpTransportTarget,
            bulk_walk_cmd,
        )

        resultado = []
        with _motor_snmp() as engine:
            try:
                target = await UdpTransportTarget.create((ip, puerto), timeout=4, retries=1)
                for base in bases:
                    filas = {}
                    async for error_ind, error_est, _idx, enlaces in bulk_walk_cmd(
                        engine, CommunityData(comunidad), target, ContextData(),
                        0, 20, ObjectType(ObjectIdentity(base)), lexicographicMode=False,
                    ):
                        if error_ind or error_est:
                            break
                        for nombre, valor in enlaces:
                            texto = _texto_snmp(valor)
                            if texto:
                                filas[str(nombre)[len(base) + 1:]] = texto
                    resultado.append(filas)
            except Exception:
                return None
        return resultado

    # --- presentación -------------------------------------------------------------

    def _fabricante(self, object_id):
        """El enterprise de sysObjectID: `1.3.6.1.4.1.<enterprise>.…`."""
        partes = (object_id or '').split('.')
        if len(partes) > 6 and partes[4] == '4' and partes[5] == '1':
            return _FABRICANTES.get(partes[6], 'enterprise %s (no reconocido)' % partes[6])
        return '— (sysObjectID: %s)' % (object_id or 'sin dato')

    def _porcentaje(self, nivel, maximo, unidad):
        """El nivel como lo publica el equipo, no como uno querría que fuera.

        `prtMarkerSuppliesLevel` NO es un porcentaje: los negativos son centinelas y la
        unidad puede no ser percent. Se calcula `nivel/maximo` solo cuando hay con qué.
        """
        try:
            n = int(nivel)
        except (TypeError, ValueError):
            return '"%s" (no numérico)' % nivel
        if n < 0:
            return _CENTINELAS.get(n, 'centinela %d' % n)
        if unidad == '19':
            return '%d%%' % n
        try:
            m = int(maximo)
        except (TypeError, ValueError):
            m = 0
        if m > 0:
            return '%s%% (%d/%d)' % (round(n / m * 100, 1), n, m)
        return '%d %s' % (n, _UNIDADES.get(unidad, unidad or 'sin unidad'))

    def _ok(self, texto):
        self.stdout.write(self.style.SUCCESS('  OK   %s' % texto))

    def _falla(self, titulo, *lineas):
        self.stdout.write(self.style.ERROR('  FALLA  %s' % titulo))
        for linea in lineas:
            self.stdout.write('         %s' % linea)
