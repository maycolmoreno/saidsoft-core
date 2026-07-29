from django.conf import settings
from django.db import models
from django.utils import timezone

from apps.activos.models import Cargo, Colaborador
from apps.catalogo.models import Estacion, Farmacia, UnidadNegocio


class TipoObjetivoCumplimiento(models.TextChoices):
    ESTACIONES = 'estaciones', 'Estaciones'
    FARMACIAS = 'farmacias', 'Farmacias'
    COLABORADORES = 'colaboradores', 'Colaboradores'


class EstadoCumplimiento(models.TextChoices):
    PENDIENTE = 'pendiente', 'Pendiente'
    COMPLETADO = 'completado', 'Completado'


class ActividadCumplimiento(models.Model):
    """Una iniciativa de cumplimiento con fecha límite (ej. AD, ESET, Checklist de
    Apertura, 2FA) que se abre a un conjunto de objetivos (estaciones, farmacias o
    colaboradores) para hacer seguimiento individual de avance.

    "Cumplimiento" aquí es cumplimiento de iniciativas/normativo — no confundir con
    el "% cumplimiento de versión POS" que ya existe en el dashboard (apps.panel).
    """

    nombre = models.CharField(max_length=150)
    descripcion = models.TextField(blank=True)
    unidades_negocio = models.ManyToManyField(UnidadNegocio, related_name='actividades_cumplimiento')
    tipo_objetivo = models.CharField(max_length=20, choices=TipoObjetivoCumplimiento.choices)
    fecha_limite = models.DateField()

    # Solo aplica cuando tipo_objetivo == FARMACIAS (ej. "farmacias aperturadas desde julio").
    farmacias_aperturadas_desde = models.DateField(
        null=True, blank=True,
        help_text='Solo para objetivo Farmacias: incluye únicamente las abiertas desde esta fecha.',
    )
    # Solo aplica cuando tipo_objetivo == COLABORADORES (ej. Líder/Coordinador/Especialista).
    cargos = models.ManyToManyField(
        Cargo, blank=True, related_name='actividades_cumplimiento',
        help_text='Solo para objetivo Colaboradores: cargos incluidos en el alcance.',
    )

    creado_por = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='+')
    fecha_creacion = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'actividad_cumplimiento'
        ordering = ['fecha_limite']
        verbose_name = 'Actividad de cumplimiento'
        verbose_name_plural = 'Actividades de cumplimiento'

    def __str__(self):
        return self.nombre

    @property
    def vencida(self):
        return timezone.now().date() > self.fecha_limite


class ResultadoCumplimientoEstacion(models.Model):
    actividad = models.ForeignKey(ActividadCumplimiento, on_delete=models.CASCADE, related_name='resultados_estacion')
    estacion = models.ForeignKey(Estacion, on_delete=models.CASCADE, related_name='resultados_cumplimiento')
    estado = models.CharField(max_length=15, choices=EstadoCumplimiento.choices, default=EstadoCumplimiento.PENDIENTE)
    fecha_completado = models.DateTimeField(null=True, blank=True)
    completado_por = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='+',
    )
    observacion = models.CharField(max_length=255, blank=True)

    class Meta:
        db_table = 'resultado_cumplimiento_estacion'
        unique_together = [('actividad', 'estacion')]
        ordering = ['estacion__codigo']

    def __str__(self):
        return f'{self.actividad.nombre} · {self.estacion.codigo}'


class ResultadoCumplimientoFarmacia(models.Model):
    actividad = models.ForeignKey(ActividadCumplimiento, on_delete=models.CASCADE, related_name='resultados_farmacia')
    farmacia = models.ForeignKey(Farmacia, on_delete=models.CASCADE, related_name='resultados_cumplimiento')
    estado = models.CharField(max_length=15, choices=EstadoCumplimiento.choices, default=EstadoCumplimiento.PENDIENTE)
    fecha_completado = models.DateTimeField(null=True, blank=True)
    completado_por = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='+',
    )
    observacion = models.CharField(max_length=255, blank=True)

    class Meta:
        db_table = 'resultado_cumplimiento_farmacia'
        unique_together = [('actividad', 'farmacia')]
        ordering = ['farmacia__codigo']

    def __str__(self):
        return f'{self.actividad.nombre} · {self.farmacia.codigo}'


class ResultadoCumplimientoColaborador(models.Model):
    actividad = models.ForeignKey(ActividadCumplimiento, on_delete=models.CASCADE, related_name='resultados_colaborador')
    colaborador = models.ForeignKey(Colaborador, on_delete=models.CASCADE, related_name='resultados_cumplimiento')
    estado = models.CharField(max_length=15, choices=EstadoCumplimiento.choices, default=EstadoCumplimiento.PENDIENTE)
    fecha_completado = models.DateTimeField(null=True, blank=True)
    completado_por = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='+',
    )
    observacion = models.CharField(max_length=255, blank=True)

    class Meta:
        db_table = 'resultado_cumplimiento_colaborador'
        unique_together = [('actividad', 'colaborador')]
        ordering = ['colaborador__nombre']

    def __str__(self):
        return f'{self.actividad.nombre} · {self.colaborador.nombre}'
