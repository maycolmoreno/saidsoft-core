from django.conf import settings
from django.db import models

from apps.activos.models import Activo, CategoriaEquipo, Colaborador, Empresa


class TipoOrigenMantenimiento(models.TextChoices):
    """Portado tal cual de TipoOrigenMantenimiento.java (InvTICS)."""
    ODOO_HELPDESK = 'odoo_helpdesk', 'Odoo Helpdesk'
    PROGRAMADO = 'programado', 'Programado'
    MANUAL = 'manual', 'Manual'


class ResultadoTecnico(models.TextChoices):
    """Portado tal cual de ResultadoTecnico.java (InvTICS), 12 valores."""
    REPARADO = 'reparado', 'Reparado'
    SIN_FALLA = 'sin_falla', 'Sin falla encontrada'
    SIN_INTERVENCION = 'sin_intervencion', 'Sin intervención'
    PARCIALMENTE_REPARADO = 'parcialmente_reparado', 'Parcialmente reparado'
    REQUIERE_REPUESTO = 'requiere_repuesto', 'Requiere repuesto'
    ESCALADO_A_PROVEEDOR = 'escalado_a_proveedor', 'Escalado a proveedor'
    IRREPARABLE = 'irreparable', 'Irreparable'
    REQUIERE_BAJA = 'requiere_baja', 'Requiere baja'
    GARANTIA_APLICADA = 'garantia_aplicada', 'Garantía aplicada'
    GARANTIA_RECHAZADA = 'garantia_rechazada', 'Garantía rechazada'
    ACTUALIZADO = 'actualizado', 'Actualizado'
    INSTALADO = 'instalado', 'Instalado'


class MantenimientoProgramado(models.Model):
    """Plantilla recurrente: cada `frecuencia_dias` genera un Mantenimiento nuevo."""
    equipo = models.ForeignKey(Activo, on_delete=models.PROTECT, related_name='mantenimientos_programados')
    tecnico = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='mantenimientos_programados_asignados',
    )
    frecuencia_dias = models.PositiveIntegerField()
    fecha_ultimo = models.DateField(null=True, blank=True)
    fecha_proximo = models.DateField()
    activo = models.BooleanField(default=True)
    observaciones = models.TextField(blank=True)

    class Meta:
        db_table = 'mantenimiento_programado'
        ordering = ['fecha_proximo']
        verbose_name = 'Mantenimiento programado'
        verbose_name_plural = 'Mantenimientos programados'

    def __str__(self):
        return f'{self.equipo.codigo} cada {self.frecuencia_dias} días'


class Mantenimiento(models.Model):
    """Entidad central: un mantenimiento manual, programado o venido de Odoo Helpdesk."""

    class EstadoInterno(models.TextChoices):
        PENDIENTE = 'pendiente', 'Pendiente'
        EN_PROCESO = 'en_proceso', 'En proceso'
        CERRADO = 'cerrado', 'Cerrado'
        CANCELADO = 'cancelado', 'Cancelado'

    cliente = models.ForeignKey(
        Colaborador, on_delete=models.PROTECT, null=True, blank=True, related_name='mantenimientos',
        help_text='Custodio/colaborador que reporta o recibe el mantenimiento.',
    )
    tecnico = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='mantenimientos_tecnico',
    )
    empresa = models.ForeignKey(
        Empresa, on_delete=models.SET_NULL, null=True, blank=True, related_name='mantenimientos',
    )
    mantenimiento_programado = models.ForeignKey(
        MantenimientoProgramado, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='mantenimientos_generados',
    )
    descripcion = models.TextField(blank=True)
    tipo_mantenimiento = models.CharField(max_length=30, blank=True)
    tipo_origen = models.CharField(
        max_length=20, choices=TipoOrigenMantenimiento.choices, default=TipoOrigenMantenimiento.MANUAL,
    )
    estado_interno = models.CharField(max_length=15, choices=EstadoInterno.choices, default=EstadoInterno.PENDIENTE)
    resultado_tecnico = models.CharField(max_length=25, choices=ResultadoTecnico.choices, blank=True)
    snapshot_equipo = models.JSONField(
        default=dict, blank=True,
        help_text='Código/serie/modelo del equipo principal, congelados al crear el mantenimiento.',
    )
    fecha_programada = models.DateTimeField()
    fecha_cierre = models.DateTimeField(null=True, blank=True)
    cerrado_por = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='mantenimientos_cerrados',
    )
    fecha_creacion = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'mantenimiento'
        ordering = ['-fecha_programada']

    def __str__(self):
        return f'Mantenimiento #{self.pk} ({self.get_estado_interno_display()})'


class MantenimientoEquipo(models.Model):
    """N:M Mantenimiento<->Activo (un mantenimiento puede cubrir varios equipos a la vez).

    Reemplaza al `equipoId` directo + N:M duplicado que coexistían en
    MantenimientosJpa (deuda de una migración 1→N nunca limpiada en el
    original): aquí solo existe esta relación, y `es_principal` cubre el
    caso de mostrar "el" equipo cuando la UI necesita uno solo.
    """
    mantenimiento = models.ForeignKey(Mantenimiento, on_delete=models.CASCADE, related_name='equipos')
    equipo = models.ForeignKey(Activo, on_delete=models.PROTECT, related_name='mantenimientos')
    es_principal = models.BooleanField(default=False)

    class Meta:
        db_table = 'mantenimiento_equipo'
        unique_together = [('mantenimiento', 'equipo')]
        ordering = ['-es_principal', 'equipo__codigo']

    def __str__(self):
        return f'{self.mantenimiento_id} - {self.equipo.codigo}'


class ActividadChecklist(models.Model):
    """Catálogo de ítems de checklist, aplicables según la categoría del equipo."""
    nombre = models.CharField(max_length=200)
    orden = models.PositiveIntegerField(default=0)
    activo = models.BooleanField(default=True)
    categorias = models.ManyToManyField(CategoriaEquipo, related_name='items_checklist', blank=True)

    class Meta:
        db_table = 'actividad_checklist'
        ordering = ['orden', 'nombre']
        verbose_name = 'Ítem de checklist'
        verbose_name_plural = 'Ítems de checklist'

    def __str__(self):
        return self.nombre


class ActividadRealizada(models.Model):
    """Ejecución de un ítem del checklist para un mantenimiento concreto."""
    mantenimiento = models.ForeignKey(Mantenimiento, on_delete=models.CASCADE, related_name='actividades_realizadas')
    actividad = models.ForeignKey(ActividadChecklist, on_delete=models.PROTECT, related_name='realizaciones')
    realizada = models.BooleanField(default=False)

    class Meta:
        db_table = 'actividad_realizada'
        unique_together = [('mantenimiento', 'actividad')]
        ordering = ['actividad__orden']

    def __str__(self):
        return f'{self.actividad.nombre}: {"si" if self.realizada else "no"}'


class EventoMantenimiento(models.Model):
    """Historial inmutable de un mantenimiento: mismo patrón que EventoActivo/EventoDespliegue."""

    class TipoEvento(models.TextChoices):
        PROGRAMADO = 'programado', 'Programado'
        INICIADO = 'iniciado', 'Iniciado'
        CHECKLIST_ACTUALIZADO = 'checklist_actualizado', 'Checklist actualizado'
        CERRADO = 'cerrado', 'Cerrado'
        CANCELADO = 'cancelado', 'Cancelado'

    mantenimiento = models.ForeignKey(Mantenimiento, on_delete=models.CASCADE, related_name='eventos')
    tipo_evento = models.CharField(max_length=25, choices=TipoEvento.choices)
    usuario = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True)
    detalle = models.JSONField(default=dict, blank=True)
    timestamp = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'evento_mantenimiento'
        ordering = ['mantenimiento', 'timestamp']

    def __str__(self):
        return f'Mantenimiento #{self.mantenimiento_id} - {self.get_tipo_evento_display()} @ {self.timestamp:%Y-%m-%d %H:%M}'

    def delete(self, *args, **kwargs):
        raise NotImplementedError('EventoMantenimiento es inmutable: no se puede eliminar.')
