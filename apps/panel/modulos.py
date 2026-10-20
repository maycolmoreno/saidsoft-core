"""Módulos del panel que quedan FUERA del alcance de SaidSoft RMM 1.0.

Ver docs/producto/SAIDSOFT_RMM_1_0_ALCANCE.md. RMM 1.0 se concentra en el monitoreo de
infraestructura y en la gestión centralizada de software; estos módulos se difieren: su
código, sus modelos, sus tareas y sus servicios SIGUEN donde están (otros procesos los
usan, p. ej. el enrolamiento con token de apertura o el bot de Telegram con los scripts),
pero sus pantallas no se exponen.

Mecanismo, el mínimo que hace falta:
- `settings.PANEL_MODULOS_DIFERIDOS` dice qué módulos están diferidos. En producción es la
  lista completa (config/settings/produccion.py); en desarrollo y CI está vacía, para que
  las pruebas existentes de esos módulos sigan ejercitando su código.
- `diferido(modulo, vista)` envuelve cada ruta del módulo en `apps/panel/urls.py` y
  devuelve 404 si el módulo está diferido. Los nombres de ruta no cambian, así que ningún
  `{% url %}` se rompe.
- `modulos_diferidos_contexto` expone la lista a las plantillas para ocultar el menú y los
  enlaces cruzados.

No se usaron los permisos porque el rol Administrador los tiene todos (seguiría viendo los
módulos) y quitarlos de los roles es un cambio de datos en producción.
"""
from functools import wraps

from django.conf import settings
from django.http import Http404

# Los módulos que pueden diferirse. Una clave fuera de esta lista es un error de
# programación (un typo dejaría la ruta expuesta), por eso `diferido` la valida.
MODULOS_DIFERIBLES = frozenset({
    'viaticos', 'aperturas', 'cumplimiento', 'netflow', 'mesa', 'despliegues', 'scripts',
})


def modulos_diferidos() -> frozenset:
    return frozenset(getattr(settings, 'PANEL_MODULOS_DIFERIDOS', ()) or ())


def diferido(modulo, vista):
    """Envuelve `vista`: 404 mientras `modulo` esté en `PANEL_MODULOS_DIFERIDOS`."""
    if modulo not in MODULOS_DIFERIBLES:
        raise ValueError(f'Módulo desconocido: {modulo!r}')

    @wraps(vista)
    def envoltura(request, *args, **kwargs):
        if modulo in modulos_diferidos():
            raise Http404('Este módulo no forma parte de SaidSoft RMM 1.0.')
        return vista(request, *args, **kwargs)

    envoltura.modulo_diferible = modulo
    return envoltura


def modulos_diferidos_contexto(request):
    """Para las plantillas: `{% if 'viaticos' not in modulos_diferidos %}`."""
    return {'modulos_diferidos': modulos_diferidos()}
