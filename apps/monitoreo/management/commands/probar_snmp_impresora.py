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
contra lo que dice el inventario — si la IP cargada apunta a otra impresora, todo lo que se
monitoree de ese activo es de otro equipo (mismo chequeo que `EquipoBordeFarmacia.nombre_coincide`).

**Desde la FASE 2 este comando no tiene OIDs propios**: pide el catálogo `IMPRESORA` a
`apps.monitoreo.snmp` y solo presenta el resultado. Tener dos listas de OIDs —una acá y una
en el motor— es la deuda #3 de `docs/auditoria-snmp.md` en miniatura: se desincronizan y
nadie se entera hasta que una impresora deja de reportar algo que la otra sí pedía.
"""
import asyncio
import ipaddress
import subprocess

from django.core.management.base import BaseCommand, CommandError

from apps.activos.models import Activo
from apps.monitoreo import snmp
from apps.monitoreo.snmp import catalogo as cat
from apps.monitoreo.snmp.impresoras import (
    ESTADO_DISPOSITIVO, ESTADO_IMPRESORA, bitmask_sin_informacion,
)
from apps.monitoreo.snmp.lector import por_clave
from apps.monitoreo.snmp.normalizar import UNIDAD_HOJAS, describir_crudo, nombre_de_unidad

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
        lecturas = self._paso_snmp(ip, comunidad, puerto)
        if lecturas is None:
            return

        indice = por_clave(lecturas)
        self._mostrar_identidad(indice, activo)
        self._mostrar_consumibles(lecturas)
        self._mostrar_estado(indice, lecturas)
        self._mostrar_alertas(lecturas)
        self._avisar_bitmask(ip, comunidad, puerto)

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
        """Las lecturas del catálogo IMPRESORA, o None si el equipo no contestó."""
        lecturas = asyncio.run(snmp.leer(ip, comunidad, snmp.IMPRESORA, puerto))
        if lecturas is not None:
            nombre = por_clave(lecturas).get('sistema.nombre')
            self._ok('2. Responde SNMP — el equipo dice llamarse "%s".' % (
                (nombre.texto if nombre else '') or '(sin nombre)',
            ))
            return lecturas

        # Distinguir "SNMP apagado" de "otra community" es la mitad del diagnóstico.
        if comunidad != _COMUNIDAD_POR_DEFECTO:
            con_default = asyncio.run(
                snmp.leer(ip, _COMUNIDAD_POR_DEFECTO, snmp.GENERICO, puerto),
            )
            if con_default is not None:
                nombre = por_clave(con_default).get('sistema.nombre')
                self._falla(
                    '2. SNMP está PRENDIDO pero con otra community.',
                    'Con "%s" sí responde (se llama "%s").'
                    % (_COMUNIDAD_POR_DEFECTO, nombre.texto if nombre else '?'),
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

    # --- presentación -------------------------------------------------------------

    def _mostrar_identidad(self, indice, activo):
        self.stdout.write('')
        self.stdout.write('   IDENTIDAD')
        descr = self._texto(indice, 'sistema.descripcion')
        self.stdout.write('     modelo (sysDescr)  %s' % (descr or '—'))
        fabricante = cat.fabricante_desde_object_id(self._texto(indice, 'sistema.object_id'))
        self.stdout.write('     fabricante         %s' % (fabricante or '— (sin sysObjectID)'))
        serie = self._texto(indice, 'equipo.serie')
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

    def _mostrar_consumibles(self, lecturas):
        """Las claves ya vienen armadas con el TIPO y el COLOR, no con la posición.

        Es lo que evita la trampa que una RICOH MP C2503 real destapó: su índice `.1.2` es
        el tóner residual, así que leer `.9.1.1` a `.9.1.4` como CMYK habría mostrado el
        residual como "cian al 100 %".
        """
        self.stdout.write('')
        self.stdout.write('   CONSUMIBLES')
        niveles = [l for l in lecturas if l.clave.endswith('.nivel') and not l.clave.startswith('bandeja.')]
        clases = {l.clave: l.texto for l in lecturas if l.clave.endswith('.clase')}
        if not niveles:
            self.stdout.write(self.style.WARNING(
                '     Este equipo NO publica prtMarkerSuppliesTable: no hay monitoreo de '
                'tóner por MIB estándar.',
            ))
            return
        for lectura in niveles:
            se_llena = clases.get(lectura.clave[: -len('nivel')] + 'clase') == 'se_llena'
            self.stdout.write('     %-26s %-14s %s%s' % (
                lectura.texto or lectura.clave,
                self._nivel(lectura),
                lectura.clave.rsplit('.', 1)[0],
                '  (se llena: 100 % es el problema)' if se_llena else '',
            ))

    def _mostrar_estado(self, indice, lecturas):
        self.stdout.write('')
        self.stdout.write('   ESTADO Y VOLUMEN')
        contador = indice.get('paginas.total')
        unidad_cruda = self._texto(indice, 'paginas.unidad')
        self.stdout.write('     contador           %s %s' % (
            int(contador.valor) if contador and contador.medido else '—',
            nombre_de_unidad(unidad_cruda),
        ))
        if unidad_cruda == UNIDAD_HOJAS:
            self.stdout.write(self.style.WARNING(
                '                        son HOJAS, no impresiones: a doble faz una hoja '
                'son dos impresiones. Importa si se factura por clic.',
            ))
        self.stdout.write('     impresora          %s' % ESTADO_IMPRESORA.get(
            self._texto(indice, 'estado.impresora'), '—'))
        self.stdout.write('     dispositivo        %s' % ESTADO_DISPOSITIVO.get(
            self._texto(indice, 'estado.dispositivo'), '—').upper())
        consola = self._texto(indice, 'equipo.consola')
        if consola:
            self.stdout.write('     pantalla           %s' % consola)

        for lectura in [l for l in lecturas if l.clave.startswith('bandeja.')]:
            etiqueta = lectura.clave.split('.', 1)[1].rsplit('.', 1)[0]
            vacia = lectura.medido and lectura.valor == 0
            linea = '     bandeja %-10s %s hojas' % (
                etiqueta, int(lectura.valor) if lectura.medido else describir_crudo(lectura.crudo) or '?',
            )
            self.stdout.write(self.style.ERROR(linea + '   <- VACÍA') if vacia else linea)

    def _mostrar_alertas(self, lecturas):
        alertas = [l for l in lecturas if l.clave.startswith('alerta.')]
        if not alertas:
            return
        self.stdout.write('')
        self.stdout.write('   ALERTAS DEL EQUIPO')
        for lectura in alertas:
            self.stdout.write(self.style.WARNING('     %s' % lectura.texto))

    def _avisar_bitmask(self, ip, comunidad, puerto):
        """`hrPrinterDetectedErrorState` en 0x00 NO prueba que no haya problemas.

        Medido: una RICOH MP C2503 lo devolvió todo en ceros estando sin papel. Se avisa
        para que nadie lea ese cero como "sin novedades".
        """
        crudo = asyncio.run(snmp.leer_octetos(ip, comunidad, cat.OID_HR_ERROR_STATE, puerto))
        if crudo is None or not bitmask_sin_informacion(crudo):
            return
        self.stdout.write('')
        self.stdout.write(self.style.WARNING(
            '   hrPrinterDetectedErrorState viene en 0x00 (ningún bit). En varios equipos '
            'eso NO significa "sin problemas": mirar el estado del dispositivo, la '
            'pantalla y las bandejas de arriba.',
        ))

    # --- utilidades ---------------------------------------------------------------

    def _texto(self, indice, clave) -> str:
        lectura = indice.get(clave)
        return lectura.texto if lectura else ''

    def _nivel(self, lectura) -> str:
        """El nivel como lo publica el equipo, no como uno querría que fuera."""
        if lectura.medido:
            return '%g%%' % lectura.valor if lectura.unidad == 'porcentaje' else '%g %s' % (
                lectura.valor, lectura.unidad,
            )
        descripcion = describir_crudo(lectura.crudo)
        return descripcion or 'sin dato'

    def _ok(self, texto):
        self.stdout.write(self.style.SUCCESS('  OK   %s' % texto))

    def _falla(self, titulo, *lineas):
        self.stdout.write(self.style.ERROR('  FALLA  %s' % titulo))
        for linea in lineas:
            self.stdout.write('         %s' % linea)
