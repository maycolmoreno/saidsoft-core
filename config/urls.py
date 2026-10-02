from django.contrib import admin
from django.conf import settings
from django.http import Http404
from django.urls import include, path, re_path
from django.views.static import serve
from rest_framework.authtoken.views import obtain_auth_token

urlpatterns = [
    path('admin/', admin.site.urls),
    path('api/v1/auth/token/', obtain_auth_token, name='api-auth-token'),
    path('api/v1/', include('apps.mantenimiento.api_urls')),
    path('api/v1/monitoreo/', include('apps.monitoreo.api_urls')),
    path('', include('apps.panel.urls')),
]

# Subárboles de MEDIA_ROOT que NUNCA se sirven sin sesión. Los entrega
# apps.panel.views.archivos, que comprueba permiso Y unidad de negocio antes de
# mandar los bytes (o de delegarlos a nginx con X-Accel-Redirect).
#
# nginx ya los declara `internal` (deploy/nginx/nginx.conf), así que en el stack
# desplegado una petición externa recibe 404 sin llegar hasta acá. Esta lista es la
# segunda mitad de esa defensa, y hace falta porque las dos están en capas distintas
# y no se enteran una de la otra: correr con settings de producción sin nginx
# delante —o perder el bloque `internal` en un cambio de configuración— reabriría la
# ruta cruda sin que nada fallara. Las rutas son adivinables (el pk es secuencial y
# la carpeta es el año/mes), así que "nadie sabe la URL" no es una protección.
PREFIJOS_MEDIA_PROTEGIDOS = ('mantenimiento/',)


def servir_media_publico(request, path, **kwargs):
    """Sirve /media/ salvo los subárboles protegidos, que solo salen por su vista.

    El resto de /media/ es público a propósito: los agentes bajan de ahí los paquetes
    de despliegue, los instaladores y su propio ejecutable sin credenciales (ver
    ARCHIVOS_BASE_URL). La integridad la cubre el SHA-256 que viaja en el comando.
    """
    if path.startswith(PREFIJOS_MEDIA_PROTEGIDOS):
        raise Http404('Ruta protegida: se entrega solo por la vista con control de acceso.')
    return serve(request, path, **kwargs)


# Una sola rama para DEBUG y producción, a propósito. `static()` devuelve [] con
# DEBUG=False, así que en producción nadie servía /media/ y los agentes recibían 404
# al descargar los .zip de despliegues (encontrado en el primer despliegue real del
# piloto, 6-ago-2026). Y usar `static()` en desarrollo dejaba el flujo protegido
# salteable justo donde se prueba, con la diferencia invisible hasta desplegar.
#
# Mientras no haya un proxy (nginx) delante que sirva media directo del volumen, lo
# sirve Django: funciona, pero cada descarga ocupa un worker de gunicorn el tiempo
# que dure — a escala real (~1.800 estaciones) esto necesita nginx o similar, no solo
# la distribución en cascada por caché de farmacia.
urlpatterns += [
    re_path(
        rf'^{settings.MEDIA_URL.lstrip("/")}(?P<path>.*)$',
        servir_media_publico,
        {'document_root': settings.MEDIA_ROOT},
    ),
]
