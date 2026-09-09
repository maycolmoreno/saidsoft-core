"""Utilidades de presentación del panel que no encajan en `form_extras`.

`hay_filtros` existe para que `panel/_estado_vacio.html` pueda distinguir dos cosas
que se veían iguales y significan lo opuesto: una lista vacía porque el módulo no
tiene datos cargados (hay que cargar el primero) y una lista vacía porque el filtro
que puso el usuario no devolvió nada (hay datos, el filtro los tapa). Ofrecer
"cargar el primero" en el segundo caso es engañoso.
"""
from django.template import Library

register = Library()

# Parámetros que acompañan a cualquier lista sin acotarla. `pagina` es el que usa el
# panel; `page`/`p` van por si alguna vista futura usa el nombre por defecto de Django.
_NO_ACOTAN = {'pagina', 'page', 'p'}


@register.simple_tag(takes_context=True)
def hay_filtros(context):
    """True si la petición trae algún parámetro que de verdad acote la lista.

    Un valor vacío (`?estado=`, que es lo que manda un `<select>` en su opción
    "todos") no cuenta como filtro: la lista que devuelve es la completa.
    """
    peticion = context.get('request')
    if peticion is None:
        return False
    return any(
        clave not in _NO_ACOTAN and any(v.strip() for v in valores)
        for clave, valores in peticion.GET.lists()
    )
