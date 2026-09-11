from django import forms

from apps.catalogo.models import Estacion, Farmacia, UnidadNegocio
from apps.catalogo.services import validar_destino_unidad_negocio
from apps.despliegues.models import Despliegue

INPUT_CLASS = 'w-full rounded-md border border-[var(--border)] bg-[var(--bg)] text-[var(--text)] px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-[var(--accent)]'


class DespliegueForm(forms.ModelForm):
    class Meta:
        model = Despliegue
        fields = [
            'unidad_negocio', 'version', 'archivo', 'descripcion',
            'modo_aplicacion', 'ventana_fecha_hora',
            'destino_tipo', 'grupos', 'farmacias', 'estaciones',
            'umbral_error_pct',
        ]
        widgets = {
            'unidad_negocio': forms.Select(attrs={'class': INPUT_CLASS}),
            'version': forms.TextInput(attrs={'class': INPUT_CLASS, 'placeholder': 'ej. 4.2.1'}),
            'archivo': forms.ClearableFileInput(attrs={'class': INPUT_CLASS}),
            'descripcion': forms.Textarea(attrs={'class': INPUT_CLASS, 'rows': 3}),
            'modo_aplicacion': forms.Select(attrs={'class': INPUT_CLASS}),
            'ventana_fecha_hora': forms.DateTimeInput(attrs={'class': INPUT_CLASS, 'type': 'datetime-local'}),
            'destino_tipo': forms.Select(attrs={'class': INPUT_CLASS}),
            'grupos': forms.CheckboxSelectMultiple,
            'farmacias': forms.CheckboxSelectMultiple,
            'estaciones': forms.CheckboxSelectMultiple,
            'umbral_error_pct': forms.NumberInput(attrs={'class': INPUT_CLASS, 'step': '0.5'}),
        }

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        from apps.cuentas.services import unidades_negocio_visibles
        # El fallback era Farmacia.objects.none() — queryset del modelo equivocado para
        # un campo que apunta a UnidadNegocio (y para el filtro unidad_negocio__in de
        # abajo). No explotaba solo porque está vacío y las vistas siempre pasan `user`.
        visibles = unidades_negocio_visibles(user) if user is not None else UnidadNegocio.objects.none()
        self.fields['unidad_negocio'].queryset = visibles
        if visibles.count() == 1:
            self.fields['unidad_negocio'].initial = visibles.first()
        # Las opciones de farmacias/estaciones se acotan a lo que el usuario puede ver en
        # general (puede abarcar más de una unidad si tiene acceso a varias); clean()
        # abajo es quien exige que coincidan con la unidad_negocio elegida en el desplegable.
        self.fields['farmacias'].queryset = Farmacia.objects.filter(
            unidad_negocio__in=visibles,
        ).order_by('codigo')
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


class PromoverDespliegueForm(forms.ModelForm):
    """Crea el siguiente anillo de un despliegue ya completado: mismo paquete y
    versión, pero con un destino más amplio. La unidad_negocio no se pide aquí — se
    hereda del despliegue de origen (un anillo nunca cambia de cliente), ver
    apps.panel.views.despliegues.despliegue_promover."""

    class Meta:
        model = Despliegue
        fields = ['destino_tipo', 'grupos', 'farmacias', 'estaciones', 'umbral_error_pct']
        widgets = {
            'destino_tipo': forms.Select(attrs={'class': INPUT_CLASS}),
            'grupos': forms.CheckboxSelectMultiple,
            'farmacias': forms.CheckboxSelectMultiple,
            'estaciones': forms.CheckboxSelectMultiple,
            'umbral_error_pct': forms.NumberInput(attrs={'class': INPUT_CLASS, 'step': '0.5'}),
        }

    def __init__(self, *args, unidad_negocio=None, **kwargs):
        super().__init__(*args, **kwargs)
        self._unidad_negocio = unidad_negocio
        if unidad_negocio is not None:
            self.fields['farmacias'].queryset = Farmacia.objects.filter(
                unidad_negocio=unidad_negocio,
            ).order_by('codigo')
            self.fields['estaciones'].queryset = Estacion.objects.filter(
                farmacia__unidad_negocio=unidad_negocio,
            ).order_by('codigo')

    def clean(self):
        cleaned = super().clean()
        if self._unidad_negocio:
            validar_destino_unidad_negocio(
                self._unidad_negocio, farmacias=cleaned.get('farmacias'), estaciones=cleaned.get('estaciones'),
            )
        return cleaned


class AperturaForm(forms.Form):
    """Alta de una apertura. No es un ModelForm a propósito: la creación tiene reglas
    (tenant cruzado, una sola apertura vigente por farmacia, materializar los pasos
    manuales) que viven en `apps.aperturas.services.crear_apertura`, que es el mismo
    camino que usa cualquier otro origen. Un ModelForm con `save()` las saltearía.
    """

    farmacia = forms.ModelChoiceField(
        queryset=Farmacia.objects.none(),
        widget=forms.Select(attrs={'class': INPUT_CLASS}),
        help_text='La farmacia que abre. Solo aparecen las de tu alcance sin una apertura vigente.',
    )
    plantilla = forms.ModelChoiceField(
        queryset=None,
        widget=forms.Select(attrs={'class': INPUT_CLASS}),
        help_text='Define qué estaciones se esperan y qué pasos corren. Se editan en el admin.',
    )
    fecha_prevista = forms.DateField(
        widget=forms.DateInput(attrs={'class': INPUT_CLASS, 'type': 'date'}),
        help_text='Fecha prevista de apertura del local.',
    )
    observacion = forms.CharField(
        required=False, widget=forms.Textarea(attrs={'class': INPUT_CLASS, 'rows': 3}),
    )

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        from apps.aperturas.models import Apertura, PlantillaApertura
        from apps.cuentas.services import unidades_negocio_visibles

        visibles = unidades_negocio_visibles(user) if user is not None else UnidadNegocio.objects.none()
        # Excluir las que ya tienen una apertura vigente evita ofrecer una opción que el
        # servicio va a rechazar igual (y que además tiene una constraint en la base).
        con_apertura = Apertura.objects.filter(estado__in=Apertura.ESTADOS_VIGENTES).values('farmacia_id')
        self.fields['farmacia'].queryset = (
            Farmacia.objects.filter(unidad_negocio__in=visibles, activa=True)
            .exclude(pk__in=con_apertura).select_related('grupo').order_by('codigo')
        )
        self.fields['plantilla'].queryset = (
            PlantillaApertura.objects.filter(unidad_negocio__in=visibles, activa=True)
            .select_related('unidad_negocio').order_by('nombre', '-version')
        )

    def clean(self):
        cleaned = super().clean()
        farmacia, plantilla = cleaned.get('farmacia'), cleaned.get('plantilla')
        if farmacia and plantilla and plantilla.unidad_negocio_id != farmacia.unidad_negocio_id:
            raise forms.ValidationError(
                f'La plantilla es de {plantilla.unidad_negocio.codigo} y la farmacia de '
                f'{farmacia.unidad_negocio.codigo}: una apertura no cruza unidades de negocio.',
            )
        if plantilla and not plantilla.perfiles_estacion.exists():
            raise forms.ValidationError(
                f'La plantilla "{plantilla.nombre}" no tiene perfiles de estación: no habría '
                'a qué emitirle tokens ni qué esperar en el local. Cargalos en el admin primero.',
            )
        return cleaned

    def crear(self, *, usuario):
        from apps.aperturas.services import crear_apertura
        return crear_apertura(
            farmacia=self.cleaned_data['farmacia'],
            plantilla=self.cleaned_data['plantilla'],
            fecha_prevista=self.cleaned_data['fecha_prevista'],
            observacion=self.cleaned_data.get('observacion', ''),
            usuario=usuario,
        )
