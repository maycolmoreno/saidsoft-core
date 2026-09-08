"""Traduce el redirect de una vista de alta al protocolo que entiende htmx."""

import json

from django.http import HttpResponse

# Id del contenedor que vive dentro del <dialog> de base.html. La conversión se
# limita a las respuestas dirigidas ahí a propósito: el panel ya usaba htmx para
# otras cosas (el modal de información de estación, la bandeja de viáticos) y
# convertir CUALQUIER redirect de CUALQUIER petición htmx rompería esos flujos.
DESTINO_MODAL = 'modal-form-content'


class RedirectHtmxMiddleware:
    """Convierte el 302 de un alta guardada en 204 + evento.

    Las vistas de alta terminan en `redirect(...)` cuando el formulario es
    válido. Servidas dentro de la ventana emergente eso no sirve: htmx sigue el
    redirect por su cuenta y termina insertando la página ENTERA de la lista
    dentro del modal (barra lateral incluida, anidada sobre la que ya está).

    Devolviendo 204 y disparando `saidsoft:guardado`, la decisión de qué hacer
    después queda del lado del navegador: cerrar y llevar a la lista, o limpiar
    el formulario para cargar el siguiente registro sin salir del modal. Ninguna
    de las 31 vistas necesita enterarse de que el modal existe.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        respuesta = self.get_response(request)
        if not self._es_alta_en_modal(request, respuesta):
            return respuesta

        destino = respuesta['Location']
        # 204 y no 200 con cuerpo vacío: con 204 htmx no reemplaza nada, así que
        # el formulario que el usuario acaba de enviar sigue en pantalla mientras
        # el evento decide si se cierra o se limpia. Un 200 vacío lo borraría
        # antes de que el manejador pudiera leer la casilla "cargar otro".
        vacia = HttpResponse(status=204)
        vacia['HX-Trigger'] = json.dumps({'saidsoft:guardado': {'url': destino}})
        return vacia

    @staticmethod
    def _es_alta_en_modal(request, respuesta):
        return (
            request.headers.get('HX-Request') == 'true'
            and request.headers.get('HX-Target') == DESTINO_MODAL
            and respuesta.status_code in (301, 302)
            and respuesta.has_header('Location')
        )
