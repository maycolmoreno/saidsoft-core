"""Temperatura del SERVIDOR que corre SAIDSOFT (no de las estaciones de las farmacias).

Existe por lo que se midió el 20-oct-2026 en el NUC de producción: CPU a 97 °C con carga
0,6, 43.629 eventos de throttling en tres horas y ocho reinicios bruscos entre el 30-sep y
el 20-oct, sin secuencia de apagado. Cada corte reinicia EMQX, que guarda los mensajes
retenidos en RAM. Un problema de refrigeración tiene que verse en el panel antes de que
apague el equipo.

Se lee de sysfs, que los contenedores ven tal cual el del host (comprobado ese día desde
`deploy-web-1`): `/sys/class/thermal` (zonas `x86_pkg_temp` y `acpitz`) y `/sys/class/hwmon`
(`coretemp` y `nvme`). Son unas pocas lecturas de archivo, sin consultas a la base: se
pueden hacer en cada render del panel.

Nada acá lanza. Un sensor ilegible, ausente o que reporta basura —en el NUC hay zonas que
dicen -274 °C— se omite, y si no queda ninguno el resultado es un dict vacío, que la salud
muestra como "sin dato" y no como "bien".
"""
import os

BASE_SYSFS = '/sys/class'

# Lo que esté fuera de esto no es una temperatura real de un equipo encendido.
_MINIMO_C = 1
_MAXIMO_C = 150


def _leer_texto(ruta):
    try:
        with open(ruta, encoding='ascii', errors='replace') as archivo:
            return archivo.read().strip()
    except OSError:
        return None


def _leer_celsius(ruta):
    """Grados Celsius de un archivo de sysfs (que guarda milésimas), o None."""
    texto = _leer_texto(ruta)
    try:
        grados = int(texto) / 1000
    except (TypeError, ValueError):
        return None
    return grados if _MINIMO_C <= grados <= _MAXIMO_C else None


def _subdirectorios(ruta, prefijo):
    try:
        nombres = sorted(os.listdir(ruta))
    except OSError:
        return []
    return [os.path.join(ruta, nombre) for nombre in nombres if nombre.startswith(prefijo)]


def leer_temperaturas(base=None) -> dict:
    """`{'cpu', 'placa', 'disco', 'disco_max', 'disco_crit'}` con lo que se pudo leer.

    - cpu: la zona `x86_pkg_temp` (el encapsulado entero); si no existe, `coretemp`
      temp1, que en Intel es "Package id 0".
    - placa: la zona `acpitz`.
    - disco: el NVMe, temp1 ("Composite", la que usa el propio disco para sus umbrales), con
      `disco_max`/`disco_crit` si el disco los informa.
    """
    base = base or BASE_SYSFS
    lecturas = {}
    for zona in _subdirectorios(os.path.join(base, 'thermal'), 'thermal_zone'):
        tipo = _leer_texto(os.path.join(zona, 'type'))
        clave = {'x86_pkg_temp': 'cpu', 'acpitz': 'placa'}.get(tipo)
        if clave and clave not in lecturas:
            grados = _leer_celsius(os.path.join(zona, 'temp'))
            if grados is not None:
                lecturas[clave] = grados
    for sensor in _subdirectorios(os.path.join(base, 'hwmon'), 'hwmon'):
        nombre = _leer_texto(os.path.join(sensor, 'name'))
        if nombre == 'coretemp' and 'cpu' not in lecturas:
            grados = _leer_celsius(os.path.join(sensor, 'temp1_input'))
            if grados is not None:
                lecturas['cpu'] = grados
        elif nombre == 'nvme' and 'disco' not in lecturas:
            grados = _leer_celsius(os.path.join(sensor, 'temp1_input'))
            if grados is not None:
                lecturas['disco'] = grados
                for sufijo in ('max', 'crit'):
                    umbral = _leer_celsius(os.path.join(sensor, f'temp1_{sufijo}'))
                    if umbral is not None:
                        lecturas[f'disco_{sufijo}'] = umbral
    return lecturas
