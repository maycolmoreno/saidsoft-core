"""Contexto compartido por las plantillas del panel."""


def salud_plataforma_contexto(request):
    """Estado real de la plataforma para la barra superior, en UNA consulta.

    Reemplaza al `<div class="sys-status">` fijo que decía "Todos los sistemas operativos"
    con un LED verde en todas las pantallas, sin comprobar nada (hallazgo D-06 de
    docs/auditoria/centro-monitoreo/). El cálculo vive en `apps.monitoreo.salud` y no acá:
    lo consumen la barra y la Zona 0 del tablero, así que no es de ninguno de los dos
    —mismo criterio que `apps.monitoreo.umbrales`— y además el dominio no puede depender
    de la presentación.

    **Corre en todas las páginas del panel, así que el presupuesto es una consulta.** Se
    cumple: `estado_latidos()` trae las tres filas de `WorkerHeartbeat` de golpe y compara
    en Python. Lo fija `SaludEnLaBarraSuperiorTests.test_cuesta_una_sola_consulta`.

    Sin sesión no se consulta nada: `base.html` solo dibuja la barra para un usuario
    autenticado, así que la pantalla de login no debe pagar una consulta por un elemento
    que no se dibuja.
    """
    if not getattr(request, 'user', None) or not request.user.is_authenticated:
        return {}

    from apps.monitoreo.salud import estado_latidos, resumen_salud

    return {'salud_plataforma': resumen_salud(estado_latidos())}


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
