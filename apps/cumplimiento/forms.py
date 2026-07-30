from django import forms

from .models import ActividadCumplimiento

INPUT_CLASS = 'w-full rounded-md border border-[var(--border)] bg-[var(--bg)] text-[var(--text)] px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-[var(--accent)]'


class ActividadCumplimientoForm(forms.ModelForm):
    class Meta:
        model = ActividadCumplimiento
        fields = [
            'nombre', 'descripcion', 'unidades_negocio', 'tipo_objetivo', 'fecha_limite',
            'farmacias_aperturadas_desde', 'cargos',
        ]
        widgets = {
            'nombre': forms.TextInput(attrs={'class': INPUT_CLASS}),
            'descripcion': forms.Textarea(attrs={'class': INPUT_CLASS, 'rows': 2}),
            'unidades_negocio': forms.SelectMultiple(attrs={'class': INPUT_CLASS, 'size': 4}),
            'tipo_objetivo': forms.Select(attrs={'class': INPUT_CLASS}),
            'fecha_limite': forms.DateInput(attrs={'class': INPUT_CLASS, 'type': 'date'}),
            'farmacias_aperturadas_desde': forms.DateInput(attrs={'class': INPUT_CLASS, 'type': 'date'}),
            'cargos': forms.SelectMultiple(attrs={'class': INPUT_CLASS, 'size': 6}),
        }
        labels = {
            'farmacias_aperturadas_desde': 'Farmacias aperturadas desde (solo objetivo Farmacias)',
            'cargos': 'Cargos incluidos (solo objetivo Colaboradores)',
        }

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        from apps.catalogo.models import UnidadNegocio
        from apps.cuentas.services import unidades_negocio_visibles
        self.fields['unidades_negocio'].queryset = (
            unidades_negocio_visibles(user) if user is not None else UnidadNegocio.objects.none()
        )
