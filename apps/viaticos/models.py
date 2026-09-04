"""Control de viáticos del personal de Soporte Técnico (política GFI-GTC-PR002).

Reemplaza la validación manual de los reportes: cada gasto se cruza contra la zona
de cobertura asignada al colaborador y contra los topes de la política. Las reglas
viven en apps/viaticos/services.py -- acá solo el modelo de datos y las
validaciones que deben BLOQUEAR el guardado venga de donde venga (panel, admin o
un import futuro), que por eso están en `clean()` y no en un formulario.
"""
from decimal import Decimal

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator
from django.db import models


class RubroViatico(models.TextChoices):
    HOSPEDAJE = 'hospedaje', 'Hospedaje'
    ALIMENTACION = 'alimentacion', 'Alimentación'
    MOVILIZACION = 'movilizacion', 'Movilización'


# Topes de la política GFI-GTC-PR002. Viven acá, en un solo lugar, para que el
# servicio de validación, el formulario y la bandeja del coordinador citen el mismo
# número y no puedan desincronizarse.
#
# Son constantes y no un modelo configurable a propósito: hoy la política los fija y
# cambiarlos debería ser una decisión versionada, no una edición silenciosa en el
# admin. Si mañana se revisan por unidad de negocio o por fecha de vigencia, esto
# pasa a ser un modelo (como AcuerdoNivelServicio en mantenimiento) y el resto del
# módulo no se entera.
TOPES_POR_RUBRO = {
    RubroViatico.HOSPEDAJE: Decimal('30.00'),
    RubroViatico.ALIMENTACION: Decimal('4.00'),
    RubroViatico.MOVILIZACION: Decimal('25.00'),
}

# Unidad a la que aplica cada tope, para que la alerta diga "por noche" y no un
# genérico "excede el tope" que obliga a ir a buscar la política.
UNIDAD_DEL_TOPE = {
    RubroViatico.HOSPEDAJE: 'por noche',
    RubroViatico.ALIMENTACION: 'por comida',
    RubroViatico.MOVILIZACION: 'por día',
}


class EstadoReporteViatico(models.TextChoices):
    PENDIENTE = 'pendiente', 'Pendiente'
    APROBADO = 'aprobado', 'Aprobado'
    OBSERVADO = 'observado', 'Observado'
    RECHAZADO = 'rechazado', 'Rechazado'


class TipoAlertaViatico(models.TextChoices):
    FUERA_DE_ZONA = 'fuera_de_zona', 'Fuera de zona'
    EXCEDE_TOPE = 'excede_tope', 'Excede el tope'
    SIN_ORIGEN_DESTINO = 'sin_origen_destino', 'Sin origen/destino'
    MONTO_REPETIDO = 'monto_repetido', 'Monto repetido en el mes'
    SIN_PLANIFICACION = 'sin_planificacion', 'Sin visita planificada'


# Alertas que la política no deja aprobar sin que el coordinador escriba por qué.
ALERTAS_QUE_EXIGEN_JUSTIFICACION = frozenset({
    TipoAlertaViatico.FUERA_DE_ZONA,
    TipoAlertaViatico.EXCEDE_TOPE,
})


class ColaboradorZona(models.Model):
    """Zona de cobertura de un técnico y las farmacias que le corresponden.

    Es OneToOne y no FK simple porque la regla "fuera de zona" necesita UNA respuesta
    a "¿qué le toca a este técnico?". Con varias filas por colaborador habría que
    decidir cuál manda, y el caso real que originó el módulo (un técnico reportando
    un punto que le tocaba a otra zona más cercana) quedaría sin criterio.

    Convive con `activos.Colaborador.zona`, que es texto libre y descriptivo: el que
    manda para validar es este, porque es el único que lista farmacias.
    """
    colaborador = models.OneToOneField(
        'activos.Colaborador', on_delete=models.CASCADE, related_name='zona_asignada',
    )
    zona_cobertura = models.CharField(
        max_length=100,
        help_text='Nombre de la zona (ej. "Machala Norte"). Descriptivo: lo que se valida '
                  'son las farmacias asignadas.',
    )
    farmacias_asignadas = models.ManyToManyField(
        'catalogo.Farmacia', blank=True, related_name='colaboradores_asignados',
        help_text='Puntos que le corresponden. Vacío = no se puede validar la zona y todo '
                  'reporte suyo queda sin ese control.',
    )
    activa = models.BooleanField(default=True)
    fecha_registro = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'colaborador_zona'
        ordering = ['colaborador__nombre']
        verbose_name = 'Zona de colaborador'
        verbose_name_plural = 'Zonas de colaboradores'

    def __str__(self):
        return f'{self.colaborador.nombre} — {self.zona_cobertura}'


class ReporteViatico(models.Model):
    """Un gasto reportado por un técnico, sujeto a aprobación de su coordinador."""

    colaborador = models.ForeignKey(
        'activos.Colaborador', on_delete=models.PROTECT, related_name='reportes_viatico',
    )
    fecha = models.DateField(help_text='Día en que se incurrió el gasto, no cuándo se cargó.')
    farmacia_visitada = models.ForeignKey(
        'catalogo.Farmacia', on_delete=models.PROTECT, related_name='reportes_viatico',
    )
    rubro = models.CharField(max_length=15, choices=RubroViatico.choices)
    monto = models.DecimalField(
        max_digits=10, decimal_places=2, validators=[MinValueValidator(Decimal('0.01'))],
    )
    origen = models.CharField(max_length=120, blank=True, help_text='Solo para movilización.')
    destino = models.CharField(max_length=120, blank=True, help_text='Solo para movilización.')
    descripcion = models.TextField(blank=True)
    estado = models.CharField(
        max_length=12, choices=EstadoReporteViatico.choices, default=EstadoReporteViatico.PENDIENTE,
    )
    factura_adjunta = models.FileField(upload_to='viaticos/facturas/%Y/%m/', blank=True)
    total_factura = models.DecimalField(
        max_digits=10, decimal_places=2, null=True, blank=True,
        help_text='Total impreso en la factura adjunta, tipeado por quien reporta. El sistema no '
                  'lee el archivo: sin este dato no hay contra qué comparar el monto.',
    )
    comentario_coordinador = models.TextField(
        blank=True,
        help_text='Justificación al aprobar con alertas, o qué corregir al observar.',
    )
    revisado_por = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='viaticos_revisados',
    )
    revisado_en = models.DateTimeField(null=True, blank=True)
    fecha_registro = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'reporte_viatico'
        ordering = ['-fecha', '-fecha_registro']
        verbose_name = 'Reporte de viático'
        verbose_name_plural = 'Reportes de viáticos'
        indexes = [
            # La bandeja del coordinador filtra por colaborador y mes; el consolidado
            # mensual recorre lo mismo.
            models.Index(fields=['colaborador', 'fecha'], name='viatico_colab_fecha_idx'),
            models.Index(fields=['estado'], name='viatico_estado_idx'),
        ]

    def __str__(self):
        return f'{self.colaborador.nombre} · {self.get_rubro_display()} · {self.fecha} · ${self.monto}'

    @property
    def tope(self):
        """Tope de la política para este rubro, o None si el rubro no tiene uno."""
        return TOPES_POR_RUBRO.get(self.rubro)

    @property
    def excede_tope(self) -> bool:
        tope = self.tope
        return tope is not None and self.monto is not None and self.monto > tope

    @property
    def alertas_abiertas(self):
        return self.alertas.filter(resuelta=False)

    @property
    def requiere_justificacion(self) -> bool:
        """True si tiene alguna alerta abierta que la política no deja aprobar sin que
        el coordinador escriba por qué."""
        return self.alertas_abiertas.filter(tipo_alerta__in=ALERTAS_QUE_EXIGEN_JUSTIFICACION).exists()

    @property
    def editable(self) -> bool:
        """Solo se corrige lo que todavía está en juego: aprobado o rechazado ya cerró."""
        return self.estado in (EstadoReporteViatico.PENDIENTE, EstadoReporteViatico.OBSERVADO)

    def clean(self):
        """Reglas que BLOQUEAN el guardado, a diferencia de las que solo levantan alerta.

        Están en el modelo y no en el formulario para que valgan igual desde el panel,
        el admin o cualquier carga futura: son datos que no deberían poder existir, no
        avisos para que alguien revise.
        """
        errores = {}

        # Movilización sin origen/destino no es auditable: no se puede saber si el
        # tramo corresponde a la zona ni si se está repitiendo.
        if self.rubro == RubroViatico.MOVILIZACION:
            if not (self.origen or '').strip():
                errores['origen'] = 'Obligatorio para movilización.'
            if not (self.destino or '').strip():
                errores['destino'] = 'Obligatorio para movilización.'

        # Sin reembolsos parciales: lo que se pide debe ser el total de la factura.
        if self.total_factura is not None and self.monto is not None and self.monto != self.total_factura:
            errores['monto'] = (
                f'No se admiten reembolsos parciales: el monto debe ser el total de la '
                f'factura (${self.total_factura}).'
            )
        if self.factura_adjunta and self.total_factura is None:
            errores['total_factura'] = (
                'Indicá el total impreso en la factura: es contra ese número que se '
                'verifica que no sea un reembolso parcial.'
            )

        if errores:
            raise ValidationError(errores)


class AlertaViatico(models.Model):
    """Hallazgo automático sobre un reporte. No bloquea: marca qué mirar.

    La unicidad por (reporte, tipo) hace que revalidar un reporte corregido actualice
    el hallazgo en vez de acumular duplicados en la bandeja.
    """
    reporte = models.ForeignKey(ReporteViatico, on_delete=models.CASCADE, related_name='alertas')
    tipo_alerta = models.CharField(max_length=20, choices=TipoAlertaViatico.choices)
    detalle = models.TextField(help_text='Por qué saltó, con los números concretos.')
    resuelta = models.BooleanField(
        default=False,
        help_text='Se marca sola cuando una revalidación ya no encuentra el problema.',
    )
    fecha_registro = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'alerta_viatico'
        ordering = ['tipo_alerta']
        verbose_name = 'Alerta de viático'
        verbose_name_plural = 'Alertas de viáticos'
        constraints = [
            models.UniqueConstraint(fields=['reporte', 'tipo_alerta'], name='alerta_viatico_unica_por_tipo'),
        ]

    def __str__(self):
        return f'{self.get_tipo_alerta_display()} — reporte {self.reporte_id}'

    @property
    def exige_justificacion(self) -> bool:
        return self.tipo_alerta in ALERTAS_QUE_EXIGEN_JUSTIFICACION
