"""Paginación compartida del panel.

Existe desde la segunda vista paginada (estaciones, 12-sep-2026). La primera —enlaces—
lo resolvió en su propia vista, y la auditoría de arquitectura señaló que si aparecía
una segunda había que extraerlo antes de que cada pantalla inventara su variante: es
exactamente así como terminan tres filtros implementados de tres formas distintas.

`pagina` y no `page`: es el nombre que `apps.panel.templatetags.panel_extras.hay_filtros`
ya reconoce como parámetro que NO acota la lista. Con `page`, el estado vacío de la
página 2 diría "no coincide nada con este filtro" cuando no hay ningún filtro puesto.
"""
from django.core.paginator import Paginator

# 25 filas: entra en una pantalla sin scroll interno y mantiene la respuesta liviana a
# 700 farmacias / ~1.800 estaciones.
POR_PAGINA = 25


def paginar(queryset, request, por_pagina=POR_PAGINA):
    """Devuelve `(pagina, query_filtros)`.

    `query_filtros` son los parámetros de la URL SIN `pagina`, para que los enlaces del
    paginador conserven los filtros activos sin arrastrar la página vieja. Sin eso,
    pasar de página descarta el filtro y el operador vuelve al listado completo.

    El queryset tiene que llegar **ordenado**: paginar sobre un orden indefinido
    devuelve filas repetidas o faltantes entre páginas, y en PostgreSQL —a diferencia de
    SQLite— el orden sin `ORDER BY` no es estable (§10 del plan documenta esta misma
    familia de diferencia entre motores).
    """
    pagina = Paginator(queryset, por_pagina).get_page(request.GET.get('pagina'))
    query = request.GET.copy()
    query.pop('pagina', None)
    return pagina, query.urlencode()
