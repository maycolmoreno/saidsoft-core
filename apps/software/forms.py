from django import forms

from apps.catalogo.models import Estacion, Farmacia, Grupo, UnidadNegocio
from apps.catalogo.services import validar_destino_unidad_negocio

from .models import AplicacionCatalogo, DestinoTipo, TipoAccionInstalacion, VersionAplicacion

INPUT_CLASS = 'w-full rounded-md border border-[var(--border)] bg-[var(--bg)] text-[var(--text)] px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-[var(--accent)]'


class AplicacionCatalogoForm(forms.ModelForm):
    class Meta:
        model = AplicacionCatalogo
        fields = [
            'nombre', 'fabricante', 'categoria', 'descripcion', 'comando_deteccion',
            'version_mas_reciente_conocida', 'unidad_negocio', 'activo',
        ]
        widgets = {
            'nombre': forms.TextInput(attrs={'class': INPUT_CLASS}),
            'fabricante': forms.TextInput(attrs={'class': INPUT_CLASS}),
            'categoria': forms.TextInput(attrs={'class': INPUT_CLASS}),
            'descripcion': forms.Textarea(attrs={'class': INPUT_CLASS, 'rows': 2}),
            'comando_deteccion': forms.Textarea(attrs={'class': f'{INPUT_CLASS} font-mono', 'rows': 4}),
            'version_mas_reciente_conocida': forms.TextInput(attrs={'class': INPUT_CLASS}),
            'unidad_negocio': forms.Select(attrs={'class': INPUT_CLASS}),
        }
        help_texts = {
            'unidad_negocio': 'Vacío = aplicación compartida, visible para todos los clientes.',
            'version_mas_reciente_conocida': 'Vacío = esta aplicación no se vigila por versión desactualizada.',
        }

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        from apps.cuentas.services import unidades_negocio_visibles
        visibles = unidades_negocio_visibles(user) if user is not None else UnidadNegocio.objects.none()
        self.fields['unidad_negocio'].queryset = visibles
        if visibles.count() == 1:
            self.fields['unidad_negocio'].initial = visibles.first()


class VersionAplicacionForm(forms.ModelForm):
    class Meta:
        model = VersionAplicacion
        fields = [
            'version', 'instalador', 'comando_instalacion_silenciosa', 'comando_desinstalacion',
            'argumentos_adicionales',
        ]
        widgets = {
            'version': forms.TextInput(attrs={'class': INPUT_CLASS, 'placeholder': 'ej. 128.0.6613.138'}),
            'instalador': forms.ClearableFileInput(attrs={'class': INPUT_CLASS}),
            'comando_instalacion_silenciosa': forms.Textarea(attrs={'class': f'{INPUT_CLASS} font-mono', 'rows': 3}),
            'comando_desinstalacion': forms.Textarea(attrs={'class': f'{INPUT_CLASS} font-mono', 'rows': 3}),
            'argumentos_adicionales': forms.TextInput(attrs={'class': INPUT_CLASS}),
        }


class SolicitudInstalacionForm(forms.Form):
    version_aplicacion = forms.ModelChoiceField(queryset=None, widget=forms.Select(attrs={'class': INPUT_CLASS}))
    accion = forms.ChoiceField(
        choices=TipoAccionInstalacion.choices, initial=TipoAccionInstalacion.INSTALAR,
        widget=forms.Select(attrs={'class': INPUT_CLASS}),
    )
    unidad_negocio = forms.ModelChoiceField(queryset=None, widget=forms.Select(attrs={'class': INPUT_CLASS}))
    destino_tipo = forms.ChoiceField(choices=DestinoTipo.choices, widget=forms.Select(attrs={'class': INPUT_CLASS}))
    grupos = forms.ModelMultipleChoiceField(
        queryset=None, required=False, widget=forms.CheckboxSelectMultiple,
    )
    farmacias = forms.ModelMultipleChoiceField(
        queryset=None, required=False, widget=forms.CheckboxSelectMultiple,
    )
    estaciones = forms.ModelMultipleChoiceField(
        queryset=None, required=False, widget=forms.CheckboxSelectMultiple,
    )

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        from apps.cuentas.services import scope_opcional_por_unidad_negocio, unidades_negocio_visibles
        visibles = unidades_negocio_visibles(user) if user is not None else UnidadNegocio.objects.none()
        self.fields['unidad_negocio'].queryset = visibles
        if visibles.count() == 1:
            self.fields['unidad_negocio'].initial = visibles.first()
        # Mismo criterio "compartida o del tenant" que ya usa Script para el catálogo — el
        # campo unidad_negocio vive en AplicacionCatalogo, no en VersionAplicacion, así que
        # no se puede reusar el alias scope_scripts_visibles (asume el campo directo).
        if user is not None:
            self.fields['version_aplicacion'].queryset = scope_opcional_por_unidad_negocio(
                VersionAplicacion.objects.select_related('aplicacion').filter(aplicacion__activo=True),
                user, 'aplicacion__unidad_negocio',
            ).order_by('aplicacion__nombre', '-fecha_publicacion')
        else:
            self.fields['version_aplicacion'].queryset = VersionAplicacion.objects.none()
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
        return cleaned
