"""Entrega de archivos protegidos (imagenes e informes de mantenimiento).

Separado porque no es una pantalla: es control de acceso a un archivo. La logica
de si lo sirve nginx o Django, y de quien puede verlo, no tiene nada que ver con
el flujo de un mantenimiento.
"""
from django.conf import settings
from django.contrib.auth.decorators import login_required, permission_required
from django.http import FileResponse, Http404, HttpResponse
from django.shortcuts import get_object_or_404
from apps.cuentas.services import verificar_acceso
from apps.mantenimiento.models import ImagenMantenimiento, Mantenimiento


def _servir_archivo_protegido(archivo):
    """Entrega un archivo de media que NO se sirve sin sesión.

    En producción delega en nginx con `X-Accel-Redirect`: Django decide el permiso y
    nginx manda los bytes. Importa a esta escala — un informe con fotos pesa varios MB, y
    servirlo desde Django ocuparía un worker de gunicorn todo lo que dure la descarga (el
    mismo problema que `config/urls.py` ya documentaba para /media/, y por el que existe
    el proxy).

    Fuera de nginx (runserver, pruebas) responde el archivo directo, así el flujo se puede
    probar sin levantar el proxy.
    """
    if not archivo:
        raise Http404('El archivo no existe.')

    if settings.SERVIR_MEDIA_CON_NGINX:
        respuesta = HttpResponse(status=200)
        # La ruta tiene que caer dentro de la `location ... internal` de nginx, que es
        # inalcanzable desde afuera: es lo que impide que alguien pida el archivo directo
        # salteándose esta vista.
        respuesta['X-Accel-Redirect'] = f'{settings.MEDIA_URL}{archivo.name}'
        # Se borra para que el Content-Type lo resuelva nginx por la extensión real; si
        # Django manda text/html, el navegador intenta renderizar un JPEG como página.
        del respuesta['Content-Type']
        return respuesta

    return FileResponse(archivo.open('rb'))


@login_required
@permission_required('mantenimiento.view_mantenimiento', raise_exception=True)
def mantenimiento_imagen(request, pk):
    """Foto de evidencia de un mantenimiento, detrás de sesión y de alcance por cliente.

    Antes estas fotos se servían desde `/media/mantenimiento/imagenes/` sin ninguna
    autenticación: cualquiera con acceso a la red o a la VPN podía bajarlas conociendo la
    ruta. Son fotos tomadas dentro de las farmacias, no paquetes de despliegue.
    """
    imagen = get_object_or_404(ImagenMantenimiento.objects.select_related('mantenimiento'), pk=pk)
    verificar_acceso(request.user, imagen.mantenimiento.unidad_negocio)
    return _servir_archivo_protegido(imagen.imagen)


@login_required
@permission_required('mantenimiento.view_mantenimiento', raise_exception=True)
def mantenimiento_informe(request, pk):
    """Informe PDF firmado de un mantenimiento, mismo criterio que las fotos."""
    mantenimiento = get_object_or_404(Mantenimiento, pk=pk)
    verificar_acceso(request.user, mantenimiento.unidad_negocio)
    return _servir_archivo_protegido(mantenimiento.informe_pdf)
