from django import forms

from apps.catalogo.models import Estacion, Farmacia
from apps.catalogo.services import validar_destino_unidad_negocio
from apps.despliegues.models import Despliegue

INPUT_CLASS = 'w-full rounded-md border border-[var(--border)] bg-[var(--bg)] text-[var(--text)] px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-[var(--accent)]'


class DespliegueForm(forms.ModelForm):
    class Meta:
        model = Despliegue
        fields = [
            'unidad_negocio', 'version', 'archivo', 'descripcion',
            'modo_aplicacion', 'ventana_fecha_hora',
            'destino_tipo', 'grupos', 'farmacias', 'estaciones',
            'umbral_error_pct',
        ]
        widgets = {
            'unidad_negocio': forms.Select(attrs={'class': INPUT_CLASS}),
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

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        from apps.cuentas.services import unidades_negocio_visibles
        visibles = unidades_negocio_visibles(user) if user is not None else Farmacia.objects.none()
        self.fields['unidad_negocio'].queryset = visibles
        if visibles.count() == 1:
            self.fields['unidad_negocio'].initial = visibles.first()
        # Las opciones de farmacias/estaciones se acotan a lo que el usuario puede ver en
        # general (puede abarcar más de una unidad si tiene acceso a varias); clean()
        # abajo es quien exige que coincidan con la unidad_negocio elegida en el desplegable.
        self.fields['farmacias'].queryset = Farmacia.objects.filter(
            unidad_negocio__in=visibles,
        ).order_by('codigo')
        self.fields['estaciones'].queryset = Estacion.objects.filter(
            farmacia__unidad_negocio__in=visibles,
        ).order_by('codigo')

    def clean(self):
        cleaned = super().clean()
        unidad = cleaned.get('unidad_negocio')
        if unidad:
            validar_destino_unidad_negocio(
                unidad, farmacias=cleaned.get('farmacias'), estaciones=cleaned.get('estaciones'),
            )
        return cleaned


class PromoverDespliegueForm(forms.ModelForm):
    """Crea el siguiente anillo de un despliegue ya completado: mismo paquete y
    versión, pero con un destino más amplio. La unidad_negocio no se pide aquí — se
    hereda del despliegue de origen (un anillo nunca cambia de cliente), ver
    apps.panel.views.despliegues.despliegue_promover."""

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

    def __init__(self, *args, unidad_negocio=None, **kwargs):
        super().__init__(*args, **kwargs)
        self._unidad_negocio = unidad_negocio
        if unidad_negocio is not None:
            self.fields['farmacias'].queryset = Farmacia.objects.filter(
                unidad_negocio=unidad_negocio,
            ).order_by('codigo')
            self.fields['estaciones'].queryset = Estacion.objects.filter(
                farmacia__unidad_negocio=unidad_negocio,
            ).order_by('codigo')

    def clean(self):
        cleaned = super().clean()
        if self._unidad_negocio:
            validar_destino_unidad_negocio(
                self._unidad_negocio, farmacias=cleaned.get('farmacias'), estaciones=cleaned.get('estaciones'),
            )
        return cleaned
