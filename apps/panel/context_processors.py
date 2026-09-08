"""Contexto compartido por las plantillas del panel."""


def htmx_contexto(request):
    """Decide si la respuesta se está armando para una ventana emergente.

    Es lo que permite que un mismo formulario se sirva como página completa o
    dentro de un modal sin duplicar la plantilla: las 31 vistas de alta siguen
    haciendo `render(request, 'panel/accion_form.html', ...)` sin enterarse, y
    `plantilla_base` decide de qué hereda esa plantilla en cada caso.

    HX-History-Restore-Request llega cuando htmx repinta una página desde su
    caché de historial. Es una navegación normal, no una carga dentro del modal:
    tratarla como modal dejaría al usuario mirando un formulario suelto, sin
    barra lateral, después de tocar "atrás" en el navegador.
    """
    es_htmx = request.headers.get('HX-Request') == 'true'
    restaura_historial = request.headers.get('HX-History-Restore-Request') == 'true'
    en_modal = es_htmx and not restaura_historial
    return {
        'en_modal': en_modal,
        'plantilla_base': 'panel/_base_modal.html' if en_modal else 'panel/base.html',
    }
