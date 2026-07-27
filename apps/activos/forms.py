from django import forms

from .models import (
    Activo, CategoriaEquipo, Colaborador, CondicionAlRecibir, Marca, MotivoBaja, MotivoReparacion,
    OrdenCompra, TipoConsumible,
)

INPUT_CLASS = 'w-full rounded-md border border-slate-300 px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-slate-900'


class ColaboradorForm(forms.ModelForm):
    class Meta:
        model = Colaborador
        fields = ['nombre', 'cedula', 'cargo', 'sucursal', 'zona']
        widgets = {f: forms.TextInput(attrs={'class': INPUT_CLASS}) for f in fields}


class OrdenCompraForm(forms.ModelForm):
    class Meta:
        model = OrdenCompra
        fields = ['numero_oc', 'proveedor', 'fecha_emision', 'bodegas_destino']
        widgets = {
            'numero_oc': forms.TextInput(attrs={'class': INPUT_CLASS}),
            'proveedor': forms.TextInput(attrs={'class': INPUT_CLASS}),
            'fecha_emision': forms.DateInput(attrs={'class': INPUT_CLASS, 'type': 'date'}),
            'bodegas_destino': forms.SelectMultiple(attrs={'class': INPUT_CLASS, 'size': 4}),
        }


class RecibirOrdenCompraForm(forms.Form):
    novedad_recepcion = forms.CharField(
        required=False, label='Novedad en la recepción (déjalo vacío si no hubo diferencias)',
        widget=forms.Textarea(attrs={'class': INPUT_CLASS, 'rows': 3}),
    )


class ActivoIngresoForm(forms.Form):
    tipo = forms.ChoiceField(choices=Activo.Tipo.choices, widget=forms.Select(attrs={'class': INPUT_CLASS}))
    marca = forms.ModelChoiceField(
        queryset=Marca.objects.all(), required=False, widget=forms.Select(attrs={'class': INPUT_CLASS}),
    )
    categoria = forms.ModelChoiceField(
        queryset=CategoriaEquipo.objects.all(), required=False,
        widget=forms.Select(attrs={'class': INPUT_CLASS}),
    )
    modelo = forms.CharField(required=False, widget=forms.TextInput(attrs={'class': INPUT_CLASS}))
    numero_serie = forms.CharField(required=False, widget=forms.TextInput(attrs={'class': INPUT_CLASS}))
    procesador = forms.CharField(required=False, widget=forms.TextInput(attrs={'class': INPUT_CLASS}))
    ram_gb = forms.IntegerField(
        required=False, min_value=1, label='RAM (GB)', widget=forms.NumberInput(attrs={'class': INPUT_CLASS}),
    )
    almacenamiento_gb = forms.IntegerField(
        required=False, min_value=1, label='Almacenamiento (GB)',
        widget=forms.NumberInput(attrs={'class': INPUT_CLASS}),
    )
    codigo_sap = forms.CharField(
        required=False, label='Código SAP', widget=forms.TextInput(attrs={'class': INPUT_CLASS}),
    )
    condicion_al_recibir = forms.ChoiceField(
        choices=[('', '—')] + list(CondicionAlRecibir.choices), required=False,
        widget=forms.Select(attrs={'class': INPUT_CLASS}),
    )
    fecha_compra = forms.DateField(
        required=False, widget=forms.DateInput(attrs={'class': INPUT_CLASS, 'type': 'date'}),
    )
    vencimiento_garantia = forms.DateField(
        required=False, widget=forms.DateInput(attrs={'class': INPUT_CLASS, 'type': 'date'}),
    )
    orden_compra = forms.ModelChoiceField(
        queryset=OrdenCompra.objects.all(), required=False,
        widget=forms.Select(attrs={'class': INPUT_CLASS}),
    )
    bodega = forms.ModelChoiceField(
        queryset=None, widget=forms.Select(attrs={'class': INPUT_CLASS}),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        from .models import Bodega
        self.fields['bodega'].queryset = Bodega.objects.filter(activa=True)


class AsignacionForm(forms.Form):
    colaborador = forms.ModelChoiceField(
        queryset=Colaborador.objects.filter(activo=True),
        widget=forms.Select(attrs={'class': INPUT_CLASS}),
    )
    estado_fisico_entrega = forms.ChoiceField(
        choices=Activo.EstadoFisico.choices, widget=forms.Select(attrs={'class': INPUT_CLASS}),
    )


class DevolucionForm(forms.Form):
    estado_fisico_devolucion = forms.ChoiceField(
        choices=Activo.EstadoFisico.choices, widget=forms.Select(attrs={'class': INPUT_CLASS}),
    )
    requiere_reparacion = forms.BooleanField(required=False, label='Necesita revisión técnica antes de reasignarse')


class EnvioReparacionForm(forms.Form):
    motivo = forms.ChoiceField(choices=MotivoReparacion.choices, widget=forms.Select(attrs={'class': INPUT_CLASS}))
    detalle_motivo = forms.CharField(
        required=False, widget=forms.Textarea(attrs={'class': INPUT_CLASS, 'rows': 2}),
    )


class RetornoReparacionForm(forms.Form):
    estado_fisico = forms.ChoiceField(
        choices=Activo.EstadoFisico.choices, widget=forms.Select(attrs={'class': INPUT_CLASS}),
    )
    proveedor_tecnico = forms.CharField(required=False, widget=forms.TextInput(attrs={'class': INPUT_CLASS}))


class BajaForm(forms.Form):
    motivo = forms.ChoiceField(choices=MotivoBaja.choices, widget=forms.Select(attrs={'class': INPUT_CLASS}))
    detalle_motivo = forms.CharField(
        required=False, widget=forms.Textarea(attrs={'class': INPUT_CLASS, 'rows': 2}),
    )


class ConsumibleEntregadoForm(forms.Form):
    tipo_consumible = forms.ModelChoiceField(
        queryset=TipoConsumible.objects.all(), widget=forms.Select(attrs={'class': INPUT_CLASS}),
    )
    cantidad = forms.IntegerField(min_value=1, widget=forms.NumberInput(attrs={'class': INPUT_CLASS}))


class StockIngresoForm(forms.Form):
    tipo_consumible = forms.ModelChoiceField(
        queryset=TipoConsumible.objects.all(), widget=forms.Select(attrs={'class': INPUT_CLASS}),
    )
    cantidad = forms.IntegerField(min_value=1, widget=forms.NumberInput(attrs={'class': INPUT_CLASS}))
