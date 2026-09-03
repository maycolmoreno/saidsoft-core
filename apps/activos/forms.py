from django import forms

from apps.catalogo.models import Farmacia

from .models import (
    Activo, Bodega, CategoriaEquipo, Colaborador, CondicionAlRecibir, Marca, MotivoBaja,
    MotivoReparacion, OrdenCompra, OrdenCompraDetalle, TipoConsumible,
)

INPUT_CLASS = 'w-full rounded-md border border-[var(--border)] bg-[var(--bg)] text-[var(--text)] px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-[var(--accent)]'


class ColaboradorForm(forms.ModelForm):
    class Meta:
        model = Colaborador
        fields = [
            'nombre', 'cedula', 'correo', 'telefono', 'cargo', 'ubicacion',
            'unidad_negocio', 'sucursal', 'zona', 'fecha_ingreso',
        ]
        widgets = {
            'nombre': forms.TextInput(attrs={'class': INPUT_CLASS}),
            'cedula': forms.TextInput(attrs={'class': INPUT_CLASS}),
            'correo': forms.EmailInput(attrs={'class': INPUT_CLASS}),
            'telefono': forms.TextInput(attrs={'class': INPUT_CLASS}),
            'cargo': forms.Select(attrs={'class': INPUT_CLASS}),
            'ubicacion': forms.Select(attrs={'class': INPUT_CLASS}),
            'unidad_negocio': forms.Select(attrs={'class': INPUT_CLASS}),
            'sucursal': forms.TextInput(attrs={'class': INPUT_CLASS}),
            'zona': forms.TextInput(attrs={'class': INPUT_CLASS}),
            'fecha_ingreso': forms.DateInput(attrs={'class': INPUT_CLASS, 'type': 'date'}),
        }
        help_texts = {
            'unidad_negocio': 'Vacío = colaborador compartido (ej. nómina central), visible para todos los clientes.',
        }

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        from apps.cuentas.services import unidades_negocio_visibles
        self.fields['unidad_negocio'].queryset = (
            unidades_negocio_visibles(user) if user is not None else self.fields['unidad_negocio'].queryset.none()
        )


class OrdenCompraForm(forms.ModelForm):
    class Meta:
        model = OrdenCompra
        fields = ['numero_oc', 'proveedor', 'fecha_emision', 'unidad_negocio', 'bodegas_destino']
        widgets = {
            'numero_oc': forms.TextInput(attrs={'class': INPUT_CLASS}),
            'proveedor': forms.TextInput(attrs={'class': INPUT_CLASS}),
            'fecha_emision': forms.DateInput(attrs={'class': INPUT_CLASS, 'type': 'date'}),
            'unidad_negocio': forms.Select(attrs={'class': INPUT_CLASS}),
            'bodegas_destino': forms.SelectMultiple(attrs={'class': INPUT_CLASS, 'size': 4}),
        }
        help_texts = {
            'unidad_negocio': 'Vacío = orden de compra compartida, visible para todos los clientes.',
        }

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        from apps.cuentas.services import scope_opcional_por_unidad_negocio, unidades_negocio_visibles
        self.fields['unidad_negocio'].queryset = (
            unidades_negocio_visibles(user) if user is not None else self.fields['unidad_negocio'].queryset.none()
        )
        self.fields['bodegas_destino'].queryset = scope_opcional_por_unidad_negocio(
            Bodega.objects.filter(activa=True), user, 'unidad_negocio',
        )


class RecibirOrdenCompraForm(forms.Form):
    novedad_recepcion = forms.CharField(
        required=False, label='Novedad en la recepción (déjalo vacío si no hubo diferencias)',
        widget=forms.Textarea(attrs={'class': INPUT_CLASS, 'rows': 3}),
    )


class OrdenCompraLineaForm(forms.ModelForm):
    class Meta:
        model = OrdenCompraDetalle
        fields = [
            'tipo_item', 'descripcion', 'modelo', 'categoria', 'marca', 'tipo_consumible',
            'cantidad_solicitada', 'precio_unitario', 'unidad_medida',
        ]
        widgets = {
            'tipo_item': forms.Select(attrs={'class': INPUT_CLASS}),
            'descripcion': forms.TextInput(attrs={'class': INPUT_CLASS}),
            'modelo': forms.TextInput(attrs={'class': INPUT_CLASS}),
            'categoria': forms.Select(attrs={'class': INPUT_CLASS}),
            'marca': forms.Select(attrs={'class': INPUT_CLASS}),
            'tipo_consumible': forms.Select(attrs={'class': INPUT_CLASS}),
            'cantidad_solicitada': forms.NumberInput(attrs={'class': INPUT_CLASS}),
            'precio_unitario': forms.NumberInput(attrs={'class': INPUT_CLASS, 'step': '0.01'}),
            'unidad_medida': forms.TextInput(attrs={'class': INPUT_CLASS}),
        }


class RecepcionLoteForm(forms.Form):
    cantidad = forms.IntegerField(min_value=1, widget=forms.NumberInput(attrs={'class': INPUT_CLASS}))
    bodega = forms.ModelChoiceField(
        queryset=Bodega.objects.filter(activa=True), widget=forms.Select(attrs={'class': INPUT_CLASS}),
    )
    custodio_receptor = forms.ModelChoiceField(
        queryset=Colaborador.objects.filter(activo=True), required=False,
        widget=forms.Select(attrs={'class': INPUT_CLASS}),
    )
    numero_lote = forms.CharField(required=False, widget=forms.TextInput(attrs={'class': INPUT_CLASS}))

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        from apps.cuentas.services import scope_opcional_por_unidad_negocio
        self.fields['bodega'].queryset = scope_opcional_por_unidad_negocio(
            Bodega.objects.filter(activa=True), user, 'unidad_negocio',
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
    # La búsqueda contra el RMM se dispara desde el botón "Buscar" que arma la
    # plantilla (activo_form.html), no desde este campo: al ser explícita, el usuario
    # sabe cuándo se completaron los datos solos y cuándo no hubo resultados.
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
        required=False, label='Código Activo', widget=forms.TextInput(attrs={'class': INPUT_CLASS}),
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
    farmacia = forms.ModelChoiceField(
        queryset=None, required=False, widget=forms.Select(attrs={'class': INPUT_CLASS}),
        label='Farmacia (si aplica)',
        help_text='Solo si ya sabes que este equipo va a una farmacia puntual (PDV o administrativo del local). '
                  'Vacío = queda como administrativo/oficina hasta que se ubique.',
    )

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        from apps.cuentas.services import scope_opcional_por_unidad_negocio

        from .models import Bodega
        self.fields['bodega'].queryset = scope_opcional_por_unidad_negocio(
            Bodega.objects.filter(activa=True), user, 'unidad_negocio',
        )
        self.fields['orden_compra'].queryset = scope_opcional_por_unidad_negocio(
            OrdenCompra.objects.all(), user, 'unidad_negocio',
        )
        self.fields['farmacia'].queryset = scope_opcional_por_unidad_negocio(
            Farmacia.objects.filter(activa=True), user, 'unidad_negocio',
        ).order_by('codigo')


class AsignacionForm(forms.Form):
    colaborador = forms.ModelChoiceField(
        queryset=Colaborador.objects.filter(activo=True),
        widget=forms.Select(attrs={'class': INPUT_CLASS}),
    )
    estado_fisico_entrega = forms.ChoiceField(
        choices=Activo.EstadoFisico.choices, widget=forms.Select(attrs={'class': INPUT_CLASS}),
    )


class UbicarFarmaciaForm(forms.Form):
    farmacia = forms.ModelChoiceField(
        queryset=None, required=False, widget=forms.Select(attrs={'class': INPUT_CLASS}),
        help_text='Vacío = administrativo/oficina (o en bodega, según su estado actual).',
    )

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        from apps.cuentas.services import scope_opcional_por_unidad_negocio

        self.fields['farmacia'].queryset = scope_opcional_por_unidad_negocio(
            Farmacia.objects.filter(activa=True), user, 'unidad_negocio',
        ).order_by('codigo')


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


class AjusteStockForm(forms.Form):
    """BUG-3 de la auditoría de gobernanza (22-ago-2026): MovimientoInventario.TipoMovimiento.AJUSTE
    existía en las choices desde siempre pero nada lo generaba nunca — un ajuste de
    inventario (conteo físico, merma, error de carga) es una operación real de
    cualquier bodega, así que quedaba una corrección de stock que solo se podía hacer
    tocando la base a mano."""
    tipo_consumible = forms.ModelChoiceField(
        queryset=TipoConsumible.objects.all(), widget=forms.Select(attrs={'class': INPUT_CLASS}),
    )
    cantidad_delta = forms.IntegerField(
        widget=forms.NumberInput(attrs={'class': INPUT_CLASS}),
        help_text='Positivo si sobró (se encontró más de lo registrado); negativo si faltó (merma/pérdida).',
    )
    motivo = forms.CharField(
        max_length=200, widget=forms.TextInput(attrs={'class': INPUT_CLASS}),
        help_text='Obligatorio: a diferencia de un ingreso o un traslado, un ajuste no tiene ningún '
                  'documento de respaldo (una OC, otra bodega) — esto es lo único que explica el cambio.',
    )

    def clean_cantidad_delta(self):
        valor = self.cleaned_data['cantidad_delta']
        if valor == 0:
            raise forms.ValidationError('La cantidad del ajuste no puede ser cero.')
        return valor


class AnularRecepcionForm(forms.Form):
    """BUG-3: RecepcionLote.Estado.ANULADO existía desde siempre pero nada lo asignaba
    — no había forma de revertir una recepción mal cargada (cantidad o lote
    equivocado) salvo tocando la base a mano."""
    motivo = forms.CharField(
        max_length=200, required=False, widget=forms.TextInput(attrs={'class': INPUT_CLASS}),
        help_text='Por qué se anula esta recepción (opcional).',
    )
