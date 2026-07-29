from django import forms

from .models import EjecucionScript, Script, TipoScript

INPUT_CLASS = 'w-full rounded-md border border-[var(--border)] bg-[var(--bg)] text-[var(--text)] px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-[var(--accent)]'


class ScriptForm(forms.ModelForm):
    class Meta:
        model = Script
        fields = ['nombre', 'descripcion', 'tipo', 'categoria', 'contenido', 'activo']
        widgets = {
            'nombre': forms.TextInput(attrs={'class': INPUT_CLASS}),
            'descripcion': forms.Textarea(attrs={'class': INPUT_CLASS, 'rows': 2}),
            'tipo': forms.Select(attrs={'class': INPUT_CLASS}),
            'categoria': forms.TextInput(attrs={'class': INPUT_CLASS}),
            'contenido': forms.Textarea(attrs={'class': f'{INPUT_CLASS} font-mono', 'rows': 14}),
        }


class EjecutarScriptForm(forms.Form):
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

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        from apps.catalogo.models import Estacion, Farmacia, Grupo
        self.fields['grupos'].queryset = Grupo.objects.order_by('codigo')
        self.fields['farmacias'].queryset = Farmacia.objects.order_by('codigo')
        self.fields['estaciones'].queryset = Estacion.objects.order_by('codigo')


class EjecutarScriptAdhocForm(EjecutarScriptForm):
    nombre = forms.CharField(widget=forms.TextInput(attrs={'class': INPUT_CLASS}))
    tipo = forms.ChoiceField(choices=TipoScript.choices, widget=forms.Select(attrs={'class': INPUT_CLASS}))
    contenido = forms.CharField(widget=forms.Textarea(attrs={'class': f'{INPUT_CLASS} font-mono', 'rows': 14}))
