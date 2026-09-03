from django import forms
from django.contrib.auth import get_user_model
from django.urls import reverse_lazy

from apps.activos.models import Activo, Bodega, Colaborador, TipoConsumible, Ubicacion

from apps.catalogo.models import Farmacia
from apps.cuentas.services import scope_opcional_por_unidad_negocio

from .models import (
    EstadoGeneralEquipo, MantenimientoProgramado, PrioridadActividad, PrioridadMantenimiento, ResultadoTecnico,
    TipoFirma, TipoMantenimiento,
)

INPUT_CLASS = 'w-full rounded-md border border-[var(--border)] bg-[var(--bg)] text-[var(--text)] px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-[var(--accent)]'

User = get_user_model()


class MantenimientoManualForm(forms.Form):
    """`equipos` se puebla BUSCANDO, no eligiendo primero un custodio.

    El diseño anterior exigía elegir el colaborador para que aparecieran sus equipos.
    Eso funciona para un equipo de oficina, pero deja fuera a los POS de farmacia, que
    no tienen custodio, y obliga a saber de quién es el equipo cuando lo que se tiene a
    mano suele ser la farmacia o el número de serie.
    """
    # Búsqueda para poblar `equipos`. No se guarda: solo filtra. Cubre lo que quien
    # carga puede tener a mano -- serie del equipo, farmacia, o el nombre/cédula del
    # custodio -- porque exigir saber DE QUIÉN es el equipo dejaba fuera a los POS de
    # farmacia, que no tienen custodio.
    buscar = forms.CharField(
        required=False, label='Buscar equipo',
        widget=forms.TextInput(attrs={
            'class': INPUT_CLASS,
            'placeholder': 'Codigo, serie, modelo, farmacia, nombre o cedula',
            'hx-get': reverse_lazy('panel:equipos_por_cliente_partial'),
            'hx-target': '#id_equipos',
            'hx-trigger': 'keyup changed delay:400ms, search',
            'hx-swap': 'innerHTML',
            'hx-include': '[name="buscar"],[name="cliente"]',
        }),
        help_text='Escribi para filtrar la lista de abajo.',
    )
    cliente = forms.ModelChoiceField(
        queryset=Colaborador.objects.filter(activo=True), required=False,
        label='Custodio (opcional)',
        widget=forms.Select(attrs={
            'class': INPUT_CLASS,
            'hx-get': reverse_lazy('panel:equipos_por_cliente_partial'),
            'hx-target': '#id_equipos',
            'hx-trigger': 'change',
            'hx-swap': 'innerHTML',
            'hx-include': '[name="buscar"],[name="cliente"]',
        }),
        help_text='Un POS de farmacia no tiene custodio; dejalo vacio si no aplica.',
    )
    equipos = forms.ModelMultipleChoiceField(
        queryset=Activo.objects.none(),
        widget=forms.SelectMultiple(attrs={'class': INPUT_CLASS, 'size': 8}),
        help_text='Busca arriba para poblar esta lista.',
    )
    prioridad = forms.ChoiceField(
        choices=PrioridadMantenimiento.choices, initial=PrioridadMantenimiento.NORMAL,
        widget=forms.Select(attrs={'class': INPUT_CLASS}),
        help_text='Define el plazo de atención y resolución (SLA).',
    )
    tecnico = forms.ModelChoiceField(
        queryset=User.objects.filter(is_active=True).order_by('username'), required=False,
        widget=forms.Select(attrs={'class': INPUT_CLASS}),
        help_text='Si no se asigna aquí, el técnico queda sin asignar (a diferencia de la app móvil, '
                  'donde el técnico siempre se autoasigna).',
    )
    tipo_mantenimiento = forms.ModelChoiceField(
        queryset=TipoMantenimiento.objects.filter(activo=True), required=False,
        widget=forms.Select(attrs={'class': INPUT_CLASS}),
    )
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

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        # "Nombre · cedula": el type-ahead del navegador busca por el principio del
        # texto, y quien carga suele tener la cedula a mano.
        self.fields['cliente'].label_from_instance = (
            lambda c: f'{c.nombre} · {c.cedula}'
        )
        if user is not None:
            self.fields['cliente'].queryset = scope_opcional_por_unidad_negocio(
                Colaborador.objects.filter(activo=True), user, 'unidad_negocio',
            ).order_by('nombre')

        # Al reenviar hay que aceptar los equipos que el navegador cargó por HTMX, que
        # ya no son "los del cliente" sino los que devolvió la búsqueda. Pero SÍ se
        # mantiene el alcance por unidad de negocio: sin eso, alguien podría forzar el
        # id de un activo de otro tenant en el POST.
        if self.is_bound:
            equipos = Activo.objects.exclude(
                estado=Activo.Estado.DADO_DE_BAJA,
            ).order_by('codigo')
            if user is not None:
                equipos = scope_opcional_por_unidad_negocio(equipos, user, 'unidad_negocio')
            self.fields['equipos'].queryset = equipos


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


class VisitaTecnicaForm(forms.Form):
    """Planificar una visita. `farmacia` se acota a las unidades que el usuario puede
    ver, mismo criterio que el resto de los formularios con alcance por tenant."""
    farmacia = forms.ModelChoiceField(
        queryset=Farmacia.objects.none(),
        widget=forms.Select(attrs={'class': INPUT_CLASS}),
    )
    tecnico = forms.ModelChoiceField(
        queryset=User.objects.filter(is_active=True).order_by('username'),
        widget=forms.Select(attrs={'class': INPUT_CLASS}),
    )
    fecha_planificada = forms.DateField(
        widget=forms.DateInput(attrs={'class': INPUT_CLASS, 'type': 'date'}),
    )
    motivo = forms.CharField(
        required=False, widget=forms.Textarea(attrs={'class': INPUT_CLASS, 'rows': 3}),
        help_text='Para qué se va: relevamiento, preventivo de ruta, etc.',
    )

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        queryset = Farmacia.objects.filter(activa=True).order_by('codigo')
        if user is not None:
            queryset = scope_opcional_por_unidad_negocio(queryset, user, 'unidad_negocio')
        self.fields['farmacia'].queryset = queryset
