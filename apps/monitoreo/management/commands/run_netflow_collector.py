"""Colector NetFlow de larga duración: escucha UDP, acumula en memoria y cada 5 minutos
vuelca un resumen por farmacia — ver apps.monitoreo.netflow. PILOTO (§10-CQ).

Mismo patrón que run_meshcentral_worker: un proceso propio en su contenedor, apagado
limpio con SIGTERM/SIGINT. Al apagarse vuelca lo que tenía, para no perder la ventana en
curso en cada despliegue.

Los datagramas se identifican por la IP de origen contra `Farmacia.ip_router`. Si un
router sale hacia el servidor con otra dirección, se mapea a mano:

    NETFLOW_EXPORTADORES="10.20.0.5=ML001,10.20.0.9=MH001"

Las IP que exportan y no se reconocen se listan en el log en cada volcado: es la forma
de descubrir cuál es la dirección real de un router nuevo.

Uso: python manage.py run_netflow_collector [--puerto 2055]
"""
import logging
import os
import signal
import socket
import threading
import time

from django.core.management.base import BaseCommand
from django.utils import timezone

from apps.catalogo.db import cerrar_conexiones_viejas
from apps.monitoreo.netflow import (
    PUERTO_COLECTOR, VENTANA_SEGUNDOS, AgregadorNetflow, contexto_de_farmacias, farmacias_por_ip_exportador,
    purgar_netflow_antiguo, volcar,
)

logger = logging.getLogger(__name__)

PURGA_CADA_SEGUNDOS = 3600


def _exportadores_extra(valor: str) -> dict[str, str]:
    """`"ip=CODIGO,ip=CODIGO"` -> {ip: CODIGO}. Lo mal formado se ignora con aviso."""
    extra = {}
    for par in filter(None, (p.strip() for p in valor.split(','))):
        ip, _, codigo = par.partition('=')
        if ip.strip() and codigo.strip():
            extra[ip.strip()] = codigo.strip().upper()
        else:
            logger.warning('NETFLOW_EXPORTADORES: se ignora "%s" (formato ip=CODIGO).', par)
    return extra


class Command(BaseCommand):
    help = 'Recibe NetFlow v5/v9/IPFIX de los Mikrotik y guarda un resumen por farmacia cada 5 minutos.'

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._detener = threading.Event()

    def add_arguments(self, parser):
        parser.add_argument('--puerto', type=int, default=PUERTO_COLECTOR)

    def handle(self, *args, **options):
        signal.signal(signal.SIGTERM, lambda *_: self._detener.set())
        signal.signal(signal.SIGINT, lambda *_: self._detener.set())

        extra = _exportadores_extra(os.environ.get('NETFLOW_EXPORTADORES', ''))
        mapa = farmacias_por_ip_exportador(extra)
        agregador = AgregadorNetflow(mapa, contexto_de_farmacias(mapa))

        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.bind(('0.0.0.0', options['puerto']))
        # Corto para que el bucle vuelva a mirar el reloj y la señal de apagado aunque no
        # llegue ningún datagrama.
        sock.settimeout(1.0)
        self.stdout.write(self.style.SUCCESS(
            f'Colector NetFlow escuchando UDP {options["puerto"]} '
            f'({len(agregador.farmacia_por_ip)} routers conocidos).',
        ))

        inicio_ventana = timezone.now()
        proximo_volcado = time.monotonic() + VENTANA_SEGUNDOS
        proxima_purga = time.monotonic()
        try:
            while not self._detener.is_set():
                try:
                    datos, (ip, _puerto) = sock.recvfrom(65535)
                    agregador.procesar(ip, datos)
                except socket.timeout:
                    pass

                if time.monotonic() >= proximo_volcado:
                    inicio_ventana = self._volcar(agregador, inicio_ventana)
                    # El mapa se relee en cada ventana: una farmacia a la que se le carga
                    # ip_router empieza a contar sin reiniciar el colector.
                    # Igual el contexto: una caja nueva con ip_lan suma su red a la LAN.
                    agregador.farmacia_por_ip = farmacias_por_ip_exportador(extra)
                    agregador.contexto = contexto_de_farmacias(agregador.farmacia_por_ip)
                    proximo_volcado += VENTANA_SEGUNDOS
                if time.monotonic() >= proxima_purga:
                    cerrar_conexiones_viejas()
                    borradas = purgar_netflow_antiguo()
                    if borradas:
                        self.stdout.write(f'Purga NetFlow: {borradas} filas de más de 14 días.')
                    proxima_purga += PURGA_CADA_SEGUNDOS
        finally:
            self._volcar(agregador, inicio_ventana)
            sock.close()
            self.stdout.write(self.style.NOTICE('Colector NetFlow detenido.'))

    def _volcar(self, agregador, inicio):
        fin = timezone.now()
        cerrar_conexiones_viejas()
        resumen = {
            farmacia_id: (a.datagramas, a.bytes_exportacion, a.flujos, a.datagramas_sin_plantilla)
            for farmacia_id, a in agregador.por_farmacia.items()
        }
        desconocidos = dict(agregador.exportadores_desconocidos)
        escritas = volcar(agregador, inicio, fin)
        self.stdout.write(
            f'[{fin:%H:%M:%S}] {escritas} ventana(s). '
            + ' '.join(
                f'farmacia#{fid}: {d} datagramas / {b} B exportados / {f} flujos / {s} sin plantilla.'
                for fid, (d, b, f, s) in resumen.items()
            ),
        )
        if desconocidos:
            self.stdout.write(self.style.WARNING(
                'Exportan pero no se reconocen (¿falta ip_router o NETFLOW_EXPORTADORES?): '
                + ', '.join(f'{ip} ({n})' for ip, n in sorted(desconocidos.items())),
            ))
        return fin
