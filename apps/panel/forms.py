from django import forms

from apps.despliegues.models import Despliegue

INPUT_CLASS = 'w-full rounded-md border border-slate-300 px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-slate-900'


class DespliegueForm(forms.ModelForm):
    class Meta:
        model = Despliegue
        fields = [
            'version', 'archivo', 'descripcion',
            'modo_aplicacion', 'ventana_fecha_hora',
            'destino_tipo', 'grupos', 'farmacias', 'estaciones',
            'umbral_error_pct',
        ]
        widgets = {
            'version': forms.TextInput(attrs={'class': INPUT_CLASS, 'placeholder': 'ej. 4.2.1'}),
            'archivo': forms.ClearableFileInput(attrs={'class': INPUT_CLASS}),
            'descripcion': forms.Textarea(attrs={'class': INPUT_CLASS, 'rows': 3}),
            'modo_aplicacion': forms.Select(attrs={'class': INPUT_CLASS}),
            'ventana_fecha_hora': forms.DateTimeInput(attrs={'class': INPUT_CLASS, 'type': 'datetime-local'}),
            'destino_tipo': forms.Select(attrs={'class': INPUT_CLASS}),
            'grupos': forms.SelectMultiple(attrs={'class': INPUT_CLASS, 'size': 6}),
            'farmacias': forms.SelectMultiple(attrs={'class': INPUT_CLASS, 'size': 6}),
            'estaciones': forms.SelectMultiple(attrs={'class': INPUT_CLASS, 'size': 6}),
            'umbral_error_pct': forms.NumberInput(attrs={'class': INPUT_CLASS, 'step': '0.5'}),
        }


class PromoverDespliegueForm(forms.ModelForm):
    """Crea el siguiente anillo de un despliegue ya completado: mismo paquete y
    versión, pero con un destino más amplio."""

    class Meta:
        model = Despliegue
        fields = ['destino_tipo', 'grupos', 'farmacias', 'estaciones', 'umbral_error_pct']
        widgets = {
            'destino_tipo': forms.Select(attrs={'class': INPUT_CLASS}),
            'grupos': forms.SelectMultiple(attrs={'class': INPUT_CLASS, 'size': 6}),
            'farmacias': forms.SelectMultiple(attrs={'class': INPUT_CLASS, 'size': 6}),
            'estaciones': forms.SelectMultiple(attrs={'class': INPUT_CLASS, 'size': 6}),
            'umbral_error_pct': forms.NumberInput(attrs={'class': INPUT_CLASS, 'step': '0.5'}),
        }
