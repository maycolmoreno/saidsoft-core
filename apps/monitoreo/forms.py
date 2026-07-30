from django import forms

from apps.catalogo.models import UnidadNegocio

from .models import ReglaAlerta

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
