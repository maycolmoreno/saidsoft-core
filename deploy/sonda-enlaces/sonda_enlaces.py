#!/usr/bin/env python3
"""Sonda de enlaces: hace ping a las farmacias y reporta a SAIDSOFT por HTTP.

Para el caso en que el host que SÍ tiene ruta hacia las farmacias no pueda alcanzar la
base de datos de SAIDSOFT. Si sí puede, no hace falta esto: se corre directamente
`python manage.py sondear_enlaces --intervalo 60` con el repo completo.

**Sin dependencias fuera de la biblioteca estándar.** A propósito: esto va a correr en
una máquina de oficina donde instalar cosas puede ser un trámite, y cuanto menos pida,
antes está funcionando. Usa el `ping` del sistema y `urllib`.

    python sonda_enlaces.py --url https://10.111.6.20:8084 --token <token> --intervalo 60
    python sonda_enlaces.py --url ... --token ... --solo-probar

El token se emite en el panel de administración de SAIDSOFT (Tokens de DRF) para un
usuario que tenga **solo** el permiso `monitoreo.registrar_sondeo_enlace`, y nada más:
este archivo y ese token viven fuera del servidor.

La lista de farmacias la entrega el propio SAIDSOFT, así que no hay que mantener acá
ninguna copia de las IP: se piden al arrancar cada barrido.
"""
import argparse
import json
import platform
import re
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

TIMEOUT_PING = 2
MAX_CONCURRENTES = 50
# 3 y no 1, igual que el servidor: medido contra la flota real, la misma IP responde en
# un sondeo y falla en el siguiente. Un paquete perdido en un enlace WAN de farmacia es
# normal y no significa que el sitio esté caído.
PAQUETES_POR_SONDEO = 3
# Igual que el servidor: la unidad se pide como "m" y no "ms" porque Windows en español
# imprime "tiempo<1m" cuando la respuesta es submilisegundo.
RE_LATENCIA = re.compile(r'(?:time|tiempo)[=<]\s*([\d.,]+)\s*m', re.IGNORECASE)


def sondear(ip):
    """Un ping. Devuelve (alcanzable, latencia_ms)."""
    if platform.system() == 'Windows':
        comando = ['ping', '-n', str(PAQUETES_POR_SONDEO), '-w', str(TIMEOUT_PING * 1000), ip]
    else:
        comando = ['ping', '-c', str(PAQUETES_POR_SONDEO), '-W', str(TIMEOUT_PING), ip]
    try:
        proceso = subprocess.run(
            comando, capture_output=True, text=True, timeout=TIMEOUT_PING * PAQUETES_POR_SONDEO + 5, errors='replace',
        )
    except (subprocess.TimeoutExpired, OSError):
        return False, None
    if proceso.returncode != 0:
        return False, None
    salida = proceso.stdout or ''
    m = RE_LATENCIA.search(salida)
    if m is None:
        # returncode 0 no alcanza en Windows: "Host de destino inaccesible" es un ICMP de
        # otro equipo de la ruta, no del destino.
        if 'inaccesible' in salida.lower() or 'unreachable' in salida.lower():
            return False, None
        return True, None
    return True, float(m.group(1).replace(',', '.'))


def _contexto_ssl(inseguro):
    if not inseguro:
        return None
    # El panel del piloto usa un certificado propio; --inseguro existe para no obligar a
    # instalar la CA en la máquina de la sonda solo para poder reportar mediciones.
    contexto = ssl.create_default_context()
    contexto.check_hostname = False
    contexto.verify_mode = ssl.CERT_NONE
    return contexto


def pedir(url, token, inseguro, datos=None):
    cuerpo = json.dumps(datos).encode('utf-8') if datos is not None else None
    peticion = urllib.request.Request(
        url, data=cuerpo, method='POST' if datos is not None else 'GET',
        headers={'Authorization': f'Token {token}', 'Content-Type': 'application/json'},
    )
    try:
        with urllib.request.urlopen(peticion, timeout=30, context=_contexto_ssl(inseguro)) as resp:
            return resp.status, json.loads(resp.read().decode('utf-8'))
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.loads(exc.read().decode('utf-8'))
        except Exception:
            return exc.code, {'detail': exc.reason}
    except urllib.error.URLError as exc:
        return 0, {'detail': f'No se pudo conectar a SAIDSOFT: {exc.reason}'}


def un_barrido(args, farmacias):
    with ThreadPoolExecutor(max_workers=MAX_CONCURRENTES) as pool:
        medidos = list(pool.map(lambda f: (f, *sondear(f['ip'])), farmacias))

    resultados = [
        {'farmacia': f['codigo'], 'alcanzable': ok, **({'latencia_ms': lat} if lat is not None else {})}
        for f, ok, lat in medidos
    ]
    if args.solo_probar:
        vivos = sum(1 for _f, ok, _l in medidos if ok)
        for f, ok, lat in medidos[:20]:
            detalle = f'{lat:.0f} ms' if lat is not None else ('responde' if ok else 'sin respuesta')
            print(f'  {f["codigo"]:<10} {f["ip"]:<16} {detalle}')
        print(f'\n{vivos}/{len(medidos)} respondieron.')
        if vivos == 0:
            print('Ninguna respondió: esta máquina NO tiene ruta hacia las farmacias.')
        return

    estado, cuerpo = pedir(
        f'{args.url.rstrip("/")}/api/v1/monitoreo/enlaces/sondeo/', args.token, args.inseguro,
        {'resultados': resultados},
    )
    marca = time.strftime('%H:%M:%S')
    if estado == 200:
        print(f'[{marca}] {cuerpo["activas"]} activo(s), {cuerpo["caidas"]} caído(s).')
    elif estado == 409:
        # El servidor rechazó el barrido entero: casi todo falló. Es la sonda, no la flota.
        print(f'[{marca}] RECHAZADO: {cuerpo.get("detail")}', file=sys.stderr)
    else:
        print(f'[{marca}] Error {estado}: {cuerpo.get("detail", cuerpo)}', file=sys.stderr)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--url', required=True, help='Base de SAIDSOFT, ej. https://10.111.6.20:8084')
    parser.add_argument('--token', required=True, help='Token de DRF de la sonda.')
    parser.add_argument('--intervalo', type=int, default=0, help='Segundos entre barridos. 0 = uno solo y sale.')
    parser.add_argument('--solo-probar', action='store_true', help='Mide y muestra, sin reportar nada.')
    parser.add_argument('--inseguro', action='store_true', help='No validar el certificado TLS del panel.')
    parser.add_argument(
        '--farmacias', default='',
        help='Archivo CSV "codigo,ip" por línea. Por defecto la lista se le pide a SAIDSOFT.',
    )
    args = parser.parse_args()

    if args.farmacias:
        with open(args.farmacias, encoding='utf-8') as fh:
            farmacias = [
                {'codigo': p[0].strip(), 'ip': p[1].strip()}
                for p in (l.split(',') for l in fh if l.strip() and not l.startswith('#'))
                if len(p) >= 2
            ]
    else:
        estado, cuerpo = pedir(
            f'{args.url.rstrip("/")}/api/v1/monitoreo/enlaces/farmacias/', args.token, args.inseguro,
        )
        if estado != 200:
            print(f'No se pudo pedir la lista de farmacias ({estado}): '
                  f'{cuerpo.get("detail", cuerpo)}', file=sys.stderr)
            return 2
        farmacias = cuerpo['farmacias']
    if not farmacias:
        print('No hay farmacias que sondear.', file=sys.stderr)
        return 2

    print(f'{len(farmacias)} enlace(s) a sondear.')
    if not args.intervalo:
        un_barrido(args, farmacias)
        return 0
    print(f'Sondeando cada {args.intervalo}s. Ctrl+C para terminar.')
    try:
        while True:
            un_barrido(args, farmacias)
            time.sleep(args.intervalo)
    except KeyboardInterrupt:
        print('\nSondeo detenido.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
