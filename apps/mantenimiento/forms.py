from django import forms
from django.contrib.auth import get_user_model

from apps.activos.models import Activo, Bodega, Colaborador, TipoConsumible, Ubicacion

from .models import EstadoGeneralEquipo, MantenimientoProgramado, PrioridadActividad, ResultadoTecnico, TipoFirma

INPUT_CLASS = 'w-full rounded-md border border-[var(--border)] bg-[var(--bg)] text-[var(--text)] px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-[var(--accent)]'

User = get_user_model()


class MantenimientoManualForm(forms.Form):
    equipos = forms.ModelMultipleChoiceField(
        queryset=Activo.objects.exclude(estado=Activo.Estado.DADO_DE_BAJA).order_by('codigo'),
        widget=forms.SelectMultiple(attrs={'class': INPUT_CLASS, 'size': 6}),
        help_text='Selecciona uno o varios equipos cubiertos por este mantenimiento.',
    )
    cliente = forms.ModelChoiceField(
        queryset=Colaborador.objects.filter(activo=True),
        widget=forms.Select(attrs={'class': INPUT_CLASS}),
    )
    tecnico = forms.ModelChoiceField(
        queryset=User.objects.filter(is_active=True).order_by('username'), required=False,
        widget=forms.Select(attrs={'class': INPUT_CLASS}),
        help_text='Si no se asigna aquí, el técnico queda sin asignar (a diferencia de la app móvil, '
                  'donde el técnico siempre se autoasigna).',
    )
    tipo_mantenimiento = forms.CharField(widget=forms.TextInput(attrs={'class': INPUT_CLASS}))
    estado_general = forms.ChoiceField(
        choices=EstadoGeneralEquipo.choices, widget=forms.Select(attrs={'class': INPUT_CLASS}),
        label='Estado general del equipo',
    )
    descripcion = forms.CharField(widget=forms.Textarea(attrs={'class': INPUT_CLASS, 'rows': 3}))
    fecha_programada = forms.DateTimeField(
        widget=forms.DateTimeInput(attrs={'class': INPUT_CLASS, 'type': 'datetime-local'}),
    )
    mantenimiento_programado = forms.ModelChoiceField(
        queryset=MantenimientoProgramado.objects.filter(activo=True), required=False,
        widget=forms.Select(attrs={'class': INPUT_CLASS}),
        label='Vincular a plan preventivo (opcional)',
        help_text='Si se elige, al cerrar este mantenimiento se recalcula la próxima fecha del plan.',
    )


class CerrarMantenimientoForm(forms.Form):
    resultado_tecnico = forms.ChoiceField(
        choices=ResultadoTecnico.choices, widget=forms.Select(attrs={'class': INPUT_CLASS}),
    )
    estado_general = forms.ChoiceField(
        choices=[('', '— sin cambiar —')] + list(EstadoGeneralEquipo.choices), required=False,
        widget=forms.Select(attrs={'class': INPUT_CLASS}), label='Estado general del equipo al cierre',
        help_text='Si el resultado devuelve el equipo a bodega (reparado, sin falla, etc.), '
                  'este valor decide el estado físico con el que vuelve.',
    )
    tiempo_real_minutos = forms.IntegerField(
        required=False, min_value=1, widget=forms.NumberInput(attrs={'class': INPUT_CLASS}),
        label='Tiempo real de intervención (minutos)',
    )


class CancelarMantenimientoForm(forms.Form):
    motivo = forms.CharField(widget=forms.Textarea(attrs={'class': INPUT_CLASS, 'rows': 2}))


class RepuestoUtilizadoForm(forms.Form):
    tipo_consumible = forms.ModelChoiceField(
        queryset=TipoConsumible.objects.order_by('nombre'),
        widget=forms.Select(attrs={'class': INPUT_CLASS}),
    )
    bodega = forms.ModelChoiceField(
        queryset=Bodega.objects.filter(activa=True), required=False,
        widget=forms.Select(attrs={'class': INPUT_CLASS}),
        help_text='Si se elige, se descuenta el stock real de esa bodega. Vacío = repuesto '
                  'fuera del flujo de bodega (solo se registra el costo).',
    )
    cantidad = forms.IntegerField(min_value=1, initial=1, widget=forms.NumberInput(attrs={'class': INPUT_CLASS}))
    costo_unitario = forms.DecimalField(
        required=False, min_value=0, max_digits=10, decimal_places=2,
        widget=forms.NumberInput(attrs={'class': INPUT_CLASS}),
    )


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


class FirmaMantenimientoForm(forms.Form):
    tipo_firma = forms.ChoiceField(choices=TipoFirma.choices, widget=forms.Select(attrs={'class': INPUT_CLASS}))
    firma_base64 = forms.CharField(widget=forms.HiddenInput())

    def clean_firma_base64(self):
        valor = self.cleaned_data['firma_base64']
        if not valor.strip():
            raise forms.ValidationError('Falta capturar la firma.')
        return valor


class ImagenMantenimientoForm(forms.Form):
    archivo = forms.FileField(widget=forms.ClearableFileInput(attrs={'class': INPUT_CLASS}))


class ActividadPlanificadaForm(forms.Form):
    tecnico = forms.ModelChoiceField(
        queryset=User.objects.filter(is_active=True).order_by('username'),
        widget=forms.Select(attrs={'class': INPUT_CLASS}),
    )
    titulo = forms.CharField(widget=forms.TextInput(attrs={'class': INPUT_CLASS}))
    descripcion = forms.CharField(
        required=False, widget=forms.Textarea(attrs={'class': INPUT_CLASS, 'rows': 2}),
    )
    tipo_actividad = forms.CharField(widget=forms.TextInput(attrs={'class': INPUT_CLASS}))
    prioridad = forms.ChoiceField(choices=PrioridadActividad.choices, widget=forms.Select(attrs={'class': INPUT_CLASS}))
    fecha_inicio = forms.DateField(widget=forms.DateInput(attrs={'class': INPUT_CLASS, 'type': 'date'}))
    fecha_fin = forms.DateField(widget=forms.DateInput(attrs={'class': INPUT_CLASS, 'type': 'date'}))
    tiempo_estimado_minutos = forms.IntegerField(
        required=False, min_value=1, widget=forms.NumberInput(attrs={'class': INPUT_CLASS}),
    )
    equipo = forms.ModelChoiceField(
        queryset=Activo.objects.exclude(estado=Activo.Estado.DADO_DE_BAJA), required=False,
        widget=forms.Select(attrs={'class': INPUT_CLASS}),
    )
    ubicacion = forms.ModelChoiceField(
        queryset=Ubicacion.objects.filter(activo=True), required=False,
        widget=forms.Select(attrs={'class': INPUT_CLASS}),
    )


class CompletarActividadForm(forms.Form):
    tiempo_real_minutos = forms.IntegerField(
        required=False, min_value=1, widget=forms.NumberInput(attrs={'class': INPUT_CLASS}),
    )
