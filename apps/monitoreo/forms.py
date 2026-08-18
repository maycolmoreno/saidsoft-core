from django import forms

from apps.catalogo.models import UnidadNegocio
from apps.catalogo.services import validar_destino_unidad_negocio

from .models import ReglaAlerta, VentanaMantenimiento

INPUT_CLASS = 'w-full rounded-md border border-[var(--border)] bg-[var(--bg)] text-[var(--text)] px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-[var(--accent)]'


class ReglaAlertaForm(forms.ModelForm):
    class Meta:
        model = ReglaAlerta
        fields = [
            'nombre', 'metrica', 'operador', 'umbral', 'duracion_minutos', 'severidad',
            'unidad_negocio', 'activo',
        ]
        widgets = {
            'nombre': forms.TextInput(attrs={'class': INPUT_CLASS}),
            'metrica': forms.Select(attrs={'class': INPUT_CLASS}),
            'operador': forms.Select(attrs={'class': INPUT_CLASS}),
            'umbral': forms.NumberInput(attrs={'class': INPUT_CLASS, 'step': '0.1'}),
            'duracion_minutos': forms.NumberInput(attrs={'class': INPUT_CLASS}),
            'severidad': forms.Select(attrs={'class': INPUT_CLASS}),
            'unidad_negocio': forms.Select(attrs={'class': INPUT_CLASS}),
        }
        help_texts = {
            'unidad_negocio': 'Vacío = regla global, aplica a todos los clientes.',
        }

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        from apps.cuentas.services import unidades_negocio_visibles, usuario_tiene_acceso_total
        visibles = unidades_negocio_visibles(user) if user is not None else UnidadNegocio.objects.none()
        self.fields['unidad_negocio'].queryset = visibles
        if user is not None and not usuario_tiene_acceso_total(user):
            self.fields['unidad_negocio'].required = True
            if visibles.count() == 1:
                self.fields['unidad_negocio'].initial = visibles.first()


class VentanaMantenimientoForm(forms.ModelForm):
    class Meta:
        model = VentanaMantenimiento
        fields = [
            'unidad_negocio', 'destino_tipo', 'grupos', 'farmacias', 'estaciones',
            'desde', 'hasta', 'motivo', 'activo',
        ]
        widgets = {
            'unidad_negocio': forms.Select(attrs={'class': INPUT_CLASS}),
            'destino_tipo': forms.Select(attrs={'class': INPUT_CLASS}),
            'grupos': forms.SelectMultiple(attrs={'class': INPUT_CLASS, 'size': 6}),
            'farmacias': forms.SelectMultiple(attrs={'class': INPUT_CLASS, 'size': 6}),
            'estaciones': forms.SelectMultiple(attrs={'class': INPUT_CLASS, 'size': 6}),
            'desde': forms.DateTimeInput(attrs={'class': INPUT_CLASS, 'type': 'datetime-local'}),
            'hasta': forms.DateTimeInput(attrs={'class': INPUT_CLASS, 'type': 'datetime-local'}),
            'motivo': forms.TextInput(attrs={'class': INPUT_CLASS}),
        }

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        from apps.catalogo.models import Estacion, Farmacia, Grupo
        from apps.cuentas.services import unidades_negocio_visibles
        visibles = unidades_negocio_visibles(user) if user is not None else UnidadNegocio.objects.none()
        self.fields['unidad_negocio'].queryset = visibles
        if visibles.count() == 1:
            self.fields['unidad_negocio'].initial = visibles.first()
        self.fields['grupos'].queryset = Grupo.objects.order_by('codigo')
        self.fields['farmacias'].queryset = Farmacia.objects.filter(unidad_negocio__in=visibles).order_by('codigo')
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
        desde, hasta = cleaned.get('desde'), cleaned.get('hasta')
        if desde and hasta and hasta <= desde:
            raise forms.ValidationError('"Hasta" debe ser posterior a "Desde".')
        return cleaned
