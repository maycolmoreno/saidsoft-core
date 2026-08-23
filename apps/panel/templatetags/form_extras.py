"""Filtros para renderizar formularios con el mismo lenguaje visual que el resto
del panel (field-grid/form-grid, ver static_src/components.css) sin tener que
adivinar el tipo de widget desde el template — Django no expone `input_type` en
Textarea/Select/FileInput (solo en subclases de `Input`), así que se resuelve acá
con `isinstance` una sola vez, en un solo lugar.
"""
from django import forms
from django.template import Library

register = Library()

_AGRUPAR_TEXTAREA_O_ARCHIVO = (
    forms.Textarea, forms.FileInput, forms.ClearableFileInput,
    forms.SelectMultiple, forms.CheckboxSelectMultiple,
)


@register.filter
def widget_ancho_completo(field):
    """True si el campo debe ocupar toda la fila del form-grid (texto largo,
    archivos, selección múltiple) en vez de compartir fila con otro campo."""
    return isinstance(field.field.widget, _AGRUPAR_TEXTAREA_O_ARCHIVO)


@register.filter
def es_checkbox(field):
    return isinstance(field.field.widget, forms.CheckboxInput)


@register.filter
def split_names(nombres_csv):
    """"tipo,marca,categoria" -> ['tipo', 'marca', 'categoria'] -- para poder agrupar
    campos de un Form en secciones (ver activo_form.html) sin reordenar el Form en
    Python solo para el layout."""
    return [n.strip() for n in nombres_csv.split(',') if n.strip()]


@register.filter
def get_field(form, nombre):
    return form[nombre]
