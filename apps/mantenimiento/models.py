from django.conf import settings
from django.db import models

from apps.activos.models import Activo, CategoriaEquipo, Colaborador, Empresa, Ubicacion


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


class EstadoGeneralEquipo(models.TextChoices):
    """Condición del equipo al momento del mantenimiento (portado de InvTICS, ausente hasta ahora)."""
    OPERATIVO = 'operativo', 'Operativo'
    REQUIERE_REVISION = 'requiere_revision', 'Requiere revisión'
    NO_OPERATIVO = 'no_operativo', 'No operativo'


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
    estado_general = models.CharField(max_length=20, choices=EstadoGeneralEquipo.choices, blank=True)
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
    informe_pdf = models.FileField(upload_to='mantenimiento/informes/%Y/%m/', blank=True)
    informe_pdf_generado_en = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = 'mantenimiento'
        ordering = ['-fecha_programada']

    def __str__(self):
        return f'Mantenimiento #{self.pk} ({self.get_estado_interno_display()})'

    @property
    def unidad_negocio(self):
        """Cliente al que pertenece, heredado de `cliente` (Colaborador). None si no
        tiene cliente asignado o el cliente no tiene unidad_negocio — se trata como
        "compartido", igual que en apps.cuentas.services."""
        return self.cliente.unidad_negocio if self.cliente_id else None


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
        FIRMADO = 'firmado', 'Firmado'
        IMAGEN_ADJUNTADA = 'imagen_adjuntada', 'Imagen adjuntada'
        INFORME_GENERADO = 'informe_generado', 'Informe PDF generado'
        CERRADO = 'cerrado', 'Cerrado'
        CANCELADO = 'cancelado', 'Cancelado'

    mantenimiento = models.ForeignKey(Mantenimiento, on_delete=models.CASCADE, related_name='eventos')
    tipo_evento = models.CharField(max_length=25, choices=TipoEvento.choices)
    usuario = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True)
    detalle = models.JSONField(default=dict, blank=True)
    timestamp = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'evento_mantenimiento'
        # 'pk' como desempate: timestamp (auto_now_add) puede repetirse entre dos eventos
        # del mismo mantenimiento creados en el mismo tick de reloj, dejando .first()/.last()
        # no determinísticos sin él (ver commit que agregó EventoSyncExterno).
        ordering = ['mantenimiento', 'timestamp', 'pk']

    def __str__(self):
        return f'Mantenimiento #{self.mantenimiento_id} - {self.get_tipo_evento_display()} @ {self.timestamp:%Y-%m-%d %H:%M}'

    def delete(self, *args, **kwargs):
        raise NotImplementedError('EventoMantenimiento es inmutable: no se puede eliminar.')


class TipoFirma(models.TextChoices):
    """Portado tal cual de TipoFirma.java (InvTICS)."""
    CUSTODIO = 'custodio', 'Custodio'
    TECNICO = 'tecnico', 'Técnico'


class PrioridadActividad(models.TextChoices):
    """Portado tal cual de PrioridadMantenimiento.java (InvTICS)."""
    NORMAL = 'normal', 'Normal'
    ALTA = 'alta', 'Alta'
    URGENTE = 'urgente', 'Urgente'


class FirmaMantenimiento(models.Model):
    """Firma digital (base64) de custodio o técnico al cerrar un mantenimiento."""
    mantenimiento = models.ForeignKey(Mantenimiento, on_delete=models.CASCADE, related_name='firmas')
    tipo_firma = models.CharField(max_length=10, choices=TipoFirma.choices)
    firma_base64 = models.TextField()
    firmado_en = models.DateTimeField(auto_now_add=True)
    ip_origen = models.GenericIPAddressField(null=True, blank=True)
    firmado_por = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='firmas_mantenimiento',
    )

    class Meta:
        db_table = 'firma_mantenimiento'
        ordering = ['-firmado_en']
        verbose_name = 'Firma de mantenimiento'
        verbose_name_plural = 'Firmas de mantenimiento'

    def __str__(self):
        return f'Firma {self.get_tipo_firma_display()} - Mantenimiento #{self.mantenimiento_id}'


class ImagenMantenimiento(models.Model):
    """Evidencia fotográfica de un mantenimiento. Archivo real (FileField), no solo una ruta."""
    mantenimiento = models.ForeignKey(Mantenimiento, on_delete=models.CASCADE, related_name='imagenes')
    imagen = models.FileField(upload_to='mantenimiento/imagenes/%Y/%m/')
    nombre_archivo = models.CharField(max_length=255, blank=True)
    tamanio_bytes = models.PositiveBigIntegerField(null=True, blank=True)
    subido_en = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'imagen_mantenimiento'
        ordering = ['-subido_en']
        verbose_name = 'Imagen de mantenimiento'
        verbose_name_plural = 'Imágenes de mantenimiento'

    def __str__(self):
        return self.nombre_archivo or self.imagen.name


class ActividadPlanificada(models.Model):
    """Agenda general del técnico (no el checklist de un mantenimiento puntual).

    Puede enlazar opcionalmente a un Mantenimiento, un MantenimientoProgramado,
    un Activo o una Ubicacion (para mantenimientos generales sin equipo
    específico) — igual que ActividadPlanificadaJpa en InvTICS.
    """

    class Estado(models.TextChoices):
        PENDIENTE = 'pendiente', 'Pendiente'
        EN_PROGRESO = 'en_progreso', 'En progreso'
        COMPLETADA = 'completada', 'Completada'
        CANCELADA = 'cancelada', 'Cancelada'

    tecnico = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='actividades_planificadas',
    )
    creado_por = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='actividades_planificadas_creadas',
    )
    titulo = models.CharField(max_length=200)
    descripcion = models.TextField(blank=True)
    tipo_actividad = models.CharField(max_length=50)
    prioridad = models.CharField(max_length=10, choices=PrioridadActividad.choices, default=PrioridadActividad.NORMAL)
    estado = models.CharField(max_length=15, choices=Estado.choices, default=Estado.PENDIENTE)
    fecha_inicio = models.DateField()
    fecha_fin = models.DateField()
    fecha_completada = models.DateTimeField(null=True, blank=True)
    tiempo_estimado_minutos = models.PositiveIntegerField(null=True, blank=True)
    tiempo_real_minutos = models.PositiveIntegerField(null=True, blank=True)
    mantenimiento = models.ForeignKey(
        Mantenimiento, on_delete=models.SET_NULL, null=True, blank=True, related_name='actividades_planificadas',
    )
    mantenimiento_programado = models.ForeignKey(
        MantenimientoProgramado, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='actividades_planificadas',
    )
    equipo = models.ForeignKey(
        Activo, on_delete=models.SET_NULL, null=True, blank=True, related_name='actividades_planificadas',
    )
    ubicacion = models.ForeignKey(
        Ubicacion, on_delete=models.SET_NULL, null=True, blank=True, related_name='actividades_planificadas',
        help_text='Ubicación objetivo cuando la actividad es general, sin equipo específico.',
    )
    observaciones = models.TextField(blank=True)
    activo = models.BooleanField(default=True)

    class Meta:
        db_table = 'actividad_planificada'
        ordering = ['fecha_inicio']
        verbose_name = 'Actividad planificada'
        verbose_name_plural = 'Actividades planificadas'

    def __str__(self):
        return f'{self.titulo} ({self.tecnico})'


class Notificacion(models.Model):
    """Bandeja de notificaciones in-app. `leida` se muta directo, sin Evento propio."""
    usuario = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='notificaciones',
    )
    mensaje = models.CharField(max_length=255)
    url = models.CharField(max_length=500, blank=True)
    leida = models.BooleanField(default=False)
    mantenimiento = models.ForeignKey(
        Mantenimiento, on_delete=models.SET_NULL, null=True, blank=True, related_name='notificaciones',
    )
    actividad_planificada = models.ForeignKey(
        ActividadPlanificada, on_delete=models.SET_NULL, null=True, blank=True, related_name='notificaciones',
    )
    creado_en = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'notificacion'
        ordering = ['-creado_en']

    def __str__(self):
        return self.mensaje


class ConsentimientoMonitoreo(models.Model):
    """Consentimiento legal del técnico para ser rastreado por GPS durante su jornada.

    Historial append-only (a diferencia de UbicacionTecnico, que es
    telemetría de alta frecuencia): el consentimiento legal se debe poder
    demostrar en el tiempo, así que cada aceptación queda registrada, nunca
    se sobreescribe.
    """
    usuario = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='consentimientos_monitoreo',
    )
    aceptado = models.BooleanField(default=True)
    version_terminos = models.CharField(max_length=20)
    ip = models.GenericIPAddressField(null=True, blank=True)
    timestamp = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'consentimiento_monitoreo'
        ordering = ['-timestamp']
        verbose_name = 'Consentimiento de monitoreo'
        verbose_name_plural = 'Consentimientos de monitoreo'

    def __str__(self):
        return f'{self.usuario} - v{self.version_terminos} @ {self.timestamp:%Y-%m-%d}'


class UbicacionTecnico(models.Model):
    """Posición GPS de un técnico en campo. Solo-append: telemetría de alta frecuencia, sin Evento propio."""
    usuario = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='ubicaciones_tecnico',
    )
    latitud = models.DecimalField(max_digits=10, decimal_places=7)
    longitud = models.DecimalField(max_digits=10, decimal_places=7)
    precision_metros = models.FloatField(null=True, blank=True)
    timestamp_captura = models.DateTimeField()
    creado_en = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'ubicacion_tecnico'
        ordering = ['-timestamp_captura']
        verbose_name = 'Ubicación de técnico'
        verbose_name_plural = 'Ubicaciones de técnico'

    def __str__(self):
        return f'{self.usuario} @ {self.timestamp_captura:%Y-%m-%d %H:%M}'
