from django import forms

from apps.catalogo.models import UnidadNegocio
from apps.catalogo.services import validar_destino_unidad_negocio

from .models import EjecucionScript, Script, ScriptProgramado, TipoScript

INPUT_CLASS = 'w-full rounded-md border border-[var(--border)] bg-[var(--bg)] text-[var(--text)] px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-[var(--accent)]'


class ScriptForm(forms.ModelForm):
    class Meta:
        model = Script
        fields = ['nombre', 'descripcion', 'tipo', 'categoria', 'contenido', 'unidad_negocio', 'activo']
        widgets = {
            'nombre': forms.TextInput(attrs={'class': INPUT_CLASS}),
            'descripcion': forms.Textarea(attrs={'class': INPUT_CLASS, 'rows': 2}),
            'tipo': forms.Select(attrs={'class': INPUT_CLASS}),
            'categoria': forms.TextInput(attrs={'class': INPUT_CLASS}),
            'contenido': forms.Textarea(attrs={'class': f'{INPUT_CLASS} font-mono', 'rows': 14}),
            'unidad_negocio': forms.Select(attrs={'class': INPUT_CLASS}),
        }
        help_texts = {
            'unidad_negocio': 'Vacío = script compartido, visible para todos los clientes.',
        }

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        from apps.cuentas.services import unidades_negocio_visibles, usuario_tiene_acceso_total
        visibles = unidades_negocio_visibles(user) if user is not None else UnidadNegocio.objects.none()
        self.fields['unidad_negocio'].queryset = visibles
        # Sin acceso a todas las unidades: el script siempre es privado de un cliente
        # (no puede dejarse en blanco = "compartido con todos").
        if user is not None and not usuario_tiene_acceso_total(user):
            self.fields['unidad_negocio'].required = True
            if visibles.count() == 1:
                self.fields['unidad_negocio'].initial = visibles.first()


class EjecutarScriptForm(forms.Form):
    unidad_negocio = forms.ModelChoiceField(queryset=None, widget=forms.Select(attrs={'class': INPUT_CLASS}))
    destino_tipo = forms.ChoiceField(
        choices=EjecucionScript.DestinoTipo.choices, widget=forms.Select(attrs={'class': INPUT_CLASS}),
    )
    grupos = forms.ModelMultipleChoiceField(
        queryset=None, required=False, widget=forms.SelectMultiple(attrs={'class': INPUT_CLASS, 'size': 6}),
    )
    farmacias = forms.ModelMultipleChoiceField(
        queryset=None, required=False, widget=forms.SelectMultiple(attrs={'class': INPUT_CLASS, 'size': 6}),
    )
    estaciones = forms.ModelMultipleChoiceField(
        queryset=None, required=False, widget=forms.SelectMultiple(attrs={'class': INPUT_CLASS, 'size': 6}),
    )
    timeout_segundos = forms.IntegerField(
        initial=300, min_value=5, max_value=3600, widget=forms.NumberInput(attrs={'class': INPUT_CLASS}),
    )

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
        return cleaned


class EjecutarScriptAdhocForm(EjecutarScriptForm):
    nombre = forms.CharField(widget=forms.TextInput(attrs={'class': INPUT_CLASS}))
    tipo = forms.ChoiceField(choices=TipoScript.choices, widget=forms.Select(attrs={'class': INPUT_CLASS}))
    contenido = forms.CharField(widget=forms.Textarea(attrs={'class': f'{INPUT_CLASS} font-mono', 'rows': 14}))


class ScriptProgramadoForm(forms.ModelForm):
    class Meta:
        model = ScriptProgramado
        fields = [
            'script', 'unidad_negocio', 'destino_tipo', 'grupos', 'farmacias', 'estaciones',
            'frecuencia_dias', 'timeout_segundos', 'fecha_proxima_ejecucion', 'activo',
        ]
        widgets = {
            'script': forms.Select(attrs={'class': INPUT_CLASS}),
            'unidad_negocio': forms.Select(attrs={'class': INPUT_CLASS}),
            'destino_tipo': forms.Select(attrs={'class': INPUT_CLASS}),
            'grupos': forms.SelectMultiple(attrs={'class': INPUT_CLASS, 'size': 6}),
            'farmacias': forms.SelectMultiple(attrs={'class': INPUT_CLASS, 'size': 6}),
            'estaciones': forms.SelectMultiple(attrs={'class': INPUT_CLASS, 'size': 6}),
            'frecuencia_dias': forms.NumberInput(attrs={'class': INPUT_CLASS}),
            'timeout_segundos': forms.NumberInput(attrs={'class': INPUT_CLASS}),
            'fecha_proxima_ejecucion': forms.DateInput(attrs={'class': INPUT_CLASS, 'type': 'date'}),
        }

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        from apps.catalogo.models import Estacion, Farmacia, Grupo
        from apps.cuentas.services import scope_scripts_visibles, unidades_negocio_visibles
        visibles = unidades_negocio_visibles(user) if user is not None else UnidadNegocio.objects.none()
        self.fields['unidad_negocio'].queryset = visibles
        if visibles.count() == 1:
            self.fields['unidad_negocio'].initial = visibles.first()
        self.fields['script'].queryset = scope_scripts_visibles(
            Script.objects.filter(activo=True, es_adhoc=False), user,
        )
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
