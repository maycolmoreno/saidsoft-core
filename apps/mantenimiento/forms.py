from django import forms
from django.contrib.auth import get_user_model

from apps.activos.models import Activo, Colaborador, Empresa

from .models import MantenimientoProgramado, ResultadoTecnico

INPUT_CLASS = 'w-full rounded-md border border-slate-300 px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-slate-900'

User = get_user_model()


class MantenimientoManualForm(forms.Form):
    equipos = forms.ModelMultipleChoiceField(
        queryset=Activo.objects.exclude(estado=Activo.Estado.DADO_DE_BAJA).order_by('codigo'),
        widget=forms.SelectMultiple(attrs={'class': INPUT_CLASS, 'size': 6}),
        help_text='Selecciona uno o varios equipos cubiertos por este mantenimiento.',
    )
    cliente = forms.ModelChoiceField(
        queryset=Colaborador.objects.filter(activo=True), required=False,
        widget=forms.Select(attrs={'class': INPUT_CLASS}),
    )
    tecnico = forms.ModelChoiceField(
        queryset=User.objects.filter(is_active=True).order_by('username'), required=False,
        widget=forms.Select(attrs={'class': INPUT_CLASS}),
    )
    empresa = forms.ModelChoiceField(
        queryset=Empresa.objects.filter(activo=True), required=False,
        widget=forms.Select(attrs={'class': INPUT_CLASS}),
    )
    tipo_mantenimiento = forms.CharField(required=False, widget=forms.TextInput(attrs={'class': INPUT_CLASS}))
    descripcion = forms.CharField(widget=forms.Textarea(attrs={'class': INPUT_CLASS, 'rows': 3}))
    fecha_programada = forms.DateTimeField(
        widget=forms.DateTimeInput(attrs={'class': INPUT_CLASS, 'type': 'datetime-local'}),
    )


class CerrarMantenimientoForm(forms.Form):
    resultado_tecnico = forms.ChoiceField(
        choices=ResultadoTecnico.choices, widget=forms.Select(attrs={'class': INPUT_CLASS}),
    )


class CancelarMantenimientoForm(forms.Form):
    motivo = forms.CharField(widget=forms.Textarea(attrs={'class': INPUT_CLASS, 'rows': 2}))


class MantenimientoProgramadoForm(forms.ModelForm):
    class Meta:
        model = MantenimientoProgramado
        fields = ['equipo', 'tecnico', 'frecuencia_dias', 'fecha_proximo', 'observaciones']
        widgets = {
            'equipo': forms.Select(attrs={'class': INPUT_CLASS}),
            'tecnico': forms.Select(attrs={'class': INPUT_CLASS}),
            'frecuencia_dias': forms.NumberInput(attrs={'class': INPUT_CLASS}),
            'fecha_proximo': forms.DateInput(attrs={'class': INPUT_CLASS, 'type': 'date'}),
            'observaciones': forms.Textarea(attrs={'class': INPUT_CLASS, 'rows': 2}),
        }
