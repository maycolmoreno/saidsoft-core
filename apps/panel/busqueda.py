"""Búsqueda libre compartida de los listados del panel.

Hermana de `apps.panel.paginacion` y existe por el mismo motivo. De los 33 listados del
panel, el 4-oct-2026 solo tres tenían buscador: a los demás solo se llegaba con
desplegables, y con tablas de miles de filas eso termina en que alguien busca por el admin
de Django en vez de por el panel. Escribir el filtro en cada vista habría dejado 28
variantes del mismo `Q(...)`, que es justo como se llega a "tres filtros implementados de
tres formas distintas" — el hallazgo que ya dejó anotado `docs/auditoria-arquitectura.md`.

Lo que resuelve una vez y para todas:

1. **Las columnas `inet`.** Un `__icontains` sobre un GenericIPAddressField funciona en
   SQLite —donde es texto— y REVIENTA en PostgreSQL, que es producción. Hay ocho campos
   así en cinco apps, así que la trampa estaba garantizada. Acá se detectan por el tipo
   real del campo y se convierten solas: quien use este helper no tiene que saberlo.
2. **Los espacios sobrantes.** Pegar un código desde un chat o un correo arrastra
   espacios, y sin el `strip()` la búsqueda no encuentra nada y parece que no funciona.
3. **Conservar el término.** `paginar()` ya devuelve `query_filtros` sin el parámetro de
   página, así que el término sobrevive al cambiar de página sin que la vista haga nada.

No reemplaza un buscador de texto completo ni pretende serlo: es `LIKE` sobre las columnas
que la vista elija. Para las tablas de esta escala alcanza, y lo que no alcanza se
resuelve con los filtros que cada pantalla ya tiene.
"""
from django.db.models import Q, TextField
from django.db.models.functions import Cast

# Prefijo de las anotaciones que se crean para las columnas `inet`. Distintivo a propósito:
# si colisionara con una anotación de la vista, el error sería silencioso y raro.
_PREFIJO = '_busqueda_txt_'

# Tipos de columna que PostgreSQL no deja comparar con LIKE sin convertir primero.
_TIPOS_QUE_NECESITAN_TEXTO = {'GenericIPAddressField'}


def _necesita_cast(modelo, ruta: str) -> bool:
    """Si `ruta` (ej. 'ip_lan', 'farmacia__ip_router') termina en una columna que hay que
    convertir a texto antes de compararla.

    Camina la ruta por los modelos relacionados en vez de pedirle el campo al modelo de
    arranque: sin eso, un lookup con `__` se interpretaría como el nombre de un campo
    inexistente y la detección fallaría justo en los casos que cruzan tablas.
    """
    actual = modelo
    partes = ruta.split('__')
    for i, parte in enumerate(partes):
        try:
            campo = actual._meta.get_field(parte)
        except Exception:
            # Una ruta que el ORM no resuelve no es trabajo de este helper: que falle
            # después, en el filtro, con el error de Django que explica qué pasa.
            return False
        if i == len(partes) - 1:
            return campo.get_internal_type() in _TIPOS_QUE_NECESITAN_TEXTO
        relacionado = getattr(campo, 'related_model', None)
        if relacionado is None:
            return False
        actual = relacionado
    return False


def buscar(queryset, request, campos, *, parametro='q'):
    """Filtra `queryset` por el término libre de la URL. Devuelve `(queryset, termino)`.

    `campos` son rutas ORM de texto, en el orden en que importan para quien busca — ej.
    `('codigo', 'farmacia__codigo', 'ip_lan')`. Se buscan con OR entre todas.

    Sin término devuelve el queryset intacto, no uno filtrado por cadena vacía: un
    `?q=` es lo que manda un formulario con el campo en blanco y tiene que significar
    "todo", igual que la opción "todos" de un `<select>`.

    El término se devuelve ya limpio para que la vista lo ponga en el contexto y el campo
    del formulario lo muestre de vuelta. Si no se devolviera al formulario, al buscar el
    campo aparecería vacío y no se vería qué se buscó.
    """
    termino = (request.GET.get(parametro) or '').strip()
    if not termino:
        return queryset, ''

    anotaciones = {}
    condicion = Q()
    for ruta in campos:
        if _necesita_cast(queryset.model, ruta):
            alias = _PREFIJO + ruta.replace('__', '_')
            anotaciones[alias] = Cast(ruta, TextField())
            condicion |= Q(**{f'{alias}__icontains': termino})
        else:
            condicion |= Q(**{f'{ruta}__icontains': termino})

    if anotaciones:
        queryset = queryset.annotate(**anotaciones)
    return queryset.filter(condicion), termino
