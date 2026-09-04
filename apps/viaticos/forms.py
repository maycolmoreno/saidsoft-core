from django import forms
from django.urls import reverse_lazy

from apps.catalogo.models import Farmacia
from apps.cuentas.services import scope_por_unidad_negocio

from .models import ColaboradorZona, ReporteViatico, RubroViatico

INPUT_CLASS = (
    'w-full rounded-md border border-[var(--border)] bg-[var(--bg)] text-[var(--text)] '
    'px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-[var(--accent)]'
)


class ReporteViaticoForm(forms.ModelForm):
    """Alta/corrección de un gasto por parte del técnico.

    `colaborador` NO es un campo: se resuelve del usuario logueado. Dejarlo elegible
    permitiría cargar gastos a nombre de otro, que es exactamente lo que el módulo
    viene a controlar.
    """

    # Las 700 farmacias no entran en un <select>: se busca y se repueblan las
    # opciones, mismo patrón que el alta de mantenimiento del panel.
    buscar_farmacia = forms.CharField(
        required=False, label='Buscar farmacia',
        widget=forms.TextInput(attrs={
            'class': INPUT_CLASS,
            'placeholder': 'Código, nombre o ubicación (ej. ML027)',
            'hx-get': reverse_lazy('panel:viaticos_farmacias_partial'),
            'hx-target': '#id_farmacia_visitada',
            'hx-trigger': 'keyup changed delay:400ms, search',
            'hx-swap': 'innerHTML',
            'hx-include': '[name="buscar_farmacia"]',
        }),
        help_text='Escribí para filtrar la lista de abajo.',
    )

    class Meta:
        model = ReporteViatico
        fields = [
            'fecha', 'farmacia_visitada', 'rubro', 'monto', 'origen', 'destino',
            'descripcion', 'factura_adjunta', 'total_factura',
        ]
        widgets = {
            'fecha': forms.DateInput(attrs={'class': INPUT_CLASS, 'type': 'date'}),
            'farmacia_visitada': forms.Select(attrs={'class': INPUT_CLASS}),
            'rubro': forms.Select(attrs={'class': INPUT_CLASS}),
            'monto': forms.NumberInput(attrs={'class': INPUT_CLASS, 'step': '0.01', 'min': '0.01'}),
            'origen': forms.TextInput(attrs={'class': INPUT_CLASS}),
            'destino': forms.TextInput(attrs={'class': INPUT_CLASS}),
            'descripcion': forms.Textarea(attrs={'class': INPUT_CLASS, 'rows': 3}),
            'total_factura': forms.NumberInput(attrs={'class': INPUT_CLASS, 'step': '0.01', 'min': '0.01'}),
        }
        help_texts = {
            'origen': 'Obligatorio si el rubro es movilización.',
            'destino': 'Obligatorio si el rubro es movilización.',
        }

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        # El queryset arranca acotado a lo ya elegido (o vacío en un alta) y se
        # repuebla por HTMX. Así el POST valida contra el catálogo completo pero la
        # página nunca renderiza 700 <option>.
        farmacias = scope_por_unidad_negocio(
            Farmacia.objects.filter(activa=True), user, 'unidad_negocio',
        ) if user is not None else Farmacia.objects.none()
        self.fields['farmacia_visitada'].queryset = farmacias
        elegida = self.data.get('farmacia_visitada') or getattr(self.instance, 'farmacia_visitada_id', None)
        if not elegida:
            self.fields['farmacia_visitada'].widget.choices = [('', 'Buscá una farmacia arriba')]

    def clean(self):
        """Solo normaliza. Las reglas que bloquean están en `ReporteViatico.clean()`,
        que ModelForm ya ejecuta -- repetirlas acá las dejaría desincronizarse."""
        datos = super().clean()
        if datos.get('rubro') != RubroViatico.MOVILIZACION:
            # Origen/destino de un hospedaje no significan nada y ensucian el reporte.
            datos['origen'] = ''
            datos['destino'] = ''
        return datos


class RevisionReporteForm(forms.Form):
    """Comentario del coordinador al aprobar/observar/rechazar.

    Nunca decide la transición: eso lo hace el servicio, que es quien sabe cuándo el
    comentario es obligatorio (alertas de zona/tope) y cuándo no.
    """
    comentario = forms.CharField(
        required=False,
        widget=forms.Textarea(attrs={
            'class': INPUT_CLASS, 'rows': 3,
            'placeholder': 'Justificación (obligatoria si hay alertas de zona o tope)',
        }),
    )


class ColaboradorZonaForm(forms.ModelForm):
    class Meta:
        model = ColaboradorZona
        fields = ['colaborador', 'zona_cobertura', 'farmacias_asignadas', 'activa']
        widgets = {
            'colaborador': forms.Select(attrs={'class': INPUT_CLASS}),
            'zona_cobertura': forms.TextInput(attrs={'class': INPUT_CLASS}),
            'farmacias_asignadas': forms.SelectMultiple(attrs={'class': INPUT_CLASS, 'size': 14}),
        }

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        if user is not None:
            self.fields['farmacias_asignadas'].queryset = scope_por_unidad_negocio(
                Farmacia.objects.filter(activa=True).order_by('codigo'), user, 'unidad_negocio',
            )
