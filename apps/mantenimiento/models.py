from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.db import models
from django.utils import timezone

from apps.activos.models import Activo, Bodega, CategoriaEquipo, Colaborador, TipoConsumible, Ubicacion


class TipoOrigenMantenimiento(models.TextChoices):
    """Portado tal cual de TipoOrigenMantenimiento.java (InvTICS)."""
    ODOO_HELPDESK = 'odoo_helpdesk', 'Odoo Helpdesk'
    PROGRAMADO = 'programado', 'Programado'
    MANUAL = 'manual', 'Manual'
    # Abierto solo por una alerta de monitoreo (ver
    # apps.mantenimiento.services.abrir_mantenimiento_desde_alerta): nadie lo cargó a
    # mano, lo disparó el RMM al detectar la falla.
    MONITOREO = 'monitoreo', 'Monitoreo (automático)'


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



class PrioridadMantenimiento(models.TextChoices):
    """Prioridad de un Mantenimiento, distinta de PrioridadActividad (que es de
    ActividadPlanificada y viene portada de InvTICS con otros valores).

    Se agrega CRITICA porque acá el rango va de "un POS caído en una farmacia que
    está vendiendo" a "un preventivo de rutina", y tratarlos con el mismo umbral era
    justamente la debilidad que esto viene a corregir.
    """
    CRITICA = 'critica', 'Crítica'
    ALTA = 'alta', 'Alta'
    NORMAL = 'normal', 'Normal'
    BAJA = 'baja', 'Baja'

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


class TipoMantenimiento(models.Model):
    """Catálogo configurable (administrable desde /admin/, no hardcodeado en Python).

    Reemplaza al CharField de texto libre que tenía Mantenimiento.tipo_mantenimiento --
    sin un catálogo, cualquiera escribía lo que quisiera ("preventivo", "Preventivo",
    "PREV"...) y era imposible reportar por tipo de forma confiable
    (docs/proceso-mantenimiento-ti.md, brecha #3, 23-ago-2026). `codigo` identifica los
    5 tipos conceptuales de la propuesta (preventivo/correctivo/falla_critica/
    actualizacion/obsolescencia) para que el código pueda asignarlos por default sin
    depender del texto de `nombre`, que sí es editable libremente por un administrador.
    """
    codigo = models.CharField(max_length=30, unique=True)
    nombre = models.CharField(max_length=100)
    descripcion = models.TextField(blank=True)
    activo = models.BooleanField(default=True)

    class Meta:
        db_table = 'tipo_mantenimiento'
        ordering = ['nombre']
        verbose_name = 'Tipo de mantenimiento'
        verbose_name_plural = 'Tipos de mantenimiento'

    def __str__(self):
        return self.nombre


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
    mantenimiento_programado = models.ForeignKey(
        MantenimientoProgramado, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='mantenimientos_generados',
    )
    descripcion = models.TextField(blank=True)
    tipo_mantenimiento = models.ForeignKey(
        TipoMantenimiento, on_delete=models.PROTECT, null=True, blank=True, related_name='mantenimientos',
    )
    tipo_origen = models.CharField(
        max_length=20, choices=TipoOrigenMantenimiento.choices, default=TipoOrigenMantenimiento.MANUAL,
    )
    prioridad = models.CharField(
        max_length=10, choices=PrioridadMantenimiento.choices, default=PrioridadMantenimiento.NORMAL,
        help_text='Define el SLA aplicable (ver AcuerdoNivelServicio).',
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
    tiempo_real_minutos = models.PositiveIntegerField(
        null=True, blank=True,
        help_text='Tiempo real de intervención, capturado al cerrar (no siempre coincide con '
                  'fecha_cierre - fecha_programada: el técnico puede pausar/retomar).',
    )

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

    @property
    def costo_total_repuestos(self):
        return sum((r.costo_total for r in self.repuestos_utilizados.all()), Decimal('0'))

    # --- SLA -------------------------------------------------------------------
    # El reloj corre desde `fecha_programada` (ver docstring de AcuerdoNivelServicio).
    # Todas estas propiedades devuelven None si no hay SLA cargado para la prioridad:
    # sin acuerdo definido no se puede afirmar que algo esté incumplido.

    @property
    def sla(self):
        return AcuerdoNivelServicio.objects.filter(prioridad=self.prioridad, activo=True).first()

    @property
    def limite_respuesta(self):
        sla = self.sla
        return self.fecha_programada + timedelta(hours=sla.horas_respuesta) if sla else None

    @property
    def limite_resolucion(self):
        sla = self.sla
        return self.fecha_programada + timedelta(hours=sla.horas_resolucion) if sla else None

    @property
    def inicio_real(self):
        """Cuándo se pasó a EN_PROCESO, según el evento inmutable — no hay campo
        propio para esto y EventoMantenimiento es la fuente de verdad."""
        evento = self.eventos.filter(tipo_evento=EventoMantenimiento.TipoEvento.INICIADO).order_by('timestamp').first()
        return evento.timestamp if evento else None

    @property
    def sla_respuesta_incumplido(self):
        """True si ya pasó el límite para atenderlo y todavía no se inició. Si se
        inició, se juzga contra el momento real de inicio (no contra "ahora"): un
        mantenimiento atendido a tiempo no pasa a incumplido por seguir abierto."""
        limite = self.limite_respuesta
        if limite is None or self.estado_interno == self.EstadoInterno.CANCELADO:
            return False
        inicio = self.inicio_real
        return (inicio or timezone.now()) > limite

    @property
    def sla_resolucion_incumplido(self):
        limite = self.limite_resolucion
        if limite is None or self.estado_interno == self.EstadoInterno.CANCELADO:
            return False
        cierre = self.fecha_cierre
        return (cierre or timezone.now()) > limite

    @property
    def estado_sla(self):
        """Etiqueta para el panel: 'sin_sla' | 'cumplido' | 'incumplido' | 'en_plazo' | 'por_vencer'.
        'por_vencer' = queda menos del 20% del tiempo de resolución."""
        limite = self.limite_resolucion
        if limite is None:
            return 'sin_sla'
        if self.estado_interno == self.EstadoInterno.CANCELADO:
            return 'sin_sla'
        if self.estado_interno == self.EstadoInterno.CERRADO:
            return 'incumplido' if self.sla_resolucion_incumplido else 'cumplido'
        ahora = timezone.now()
        if ahora > limite:
            return 'incumplido'
        total = (limite - self.fecha_programada).total_seconds()
        restante = (limite - ahora).total_seconds()
        return 'por_vencer' if total > 0 and restante / total <= 0.2 else 'en_plazo'


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
        REPUESTO_REGISTRADO = 'repuesto_registrado', 'Repuesto registrado'
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



class AcuerdoNivelServicio(models.Model):
    """SLA por prioridad: en cuánto tiempo hay que ATENDER y RESOLVER un mantenimiento.

    Configurable (no constantes en el código) por el mismo criterio que
    TipoMantenimiento: son reglas de negocio que el área de TI ajusta sin tocar
    código. Se siembran valores razonables por migración de datos.

    El reloj arranca en `fecha_programada`, no en `fecha_creacion`: un preventivo
    agendado para dentro de un mes no debe contar como incumplido desde que se crea.
    Para un correctivo, ambas coinciden en la práctica (se crea con
    fecha_programada=ahora, ver iniciar_reparacion_desde_activo).
    """
    prioridad = models.CharField(max_length=10, choices=PrioridadMantenimiento.choices, unique=True)
    horas_respuesta = models.PositiveIntegerField(
        help_text='Horas desde la fecha programada para INICIAR el mantenimiento.',
    )
    horas_resolucion = models.PositiveIntegerField(
        help_text='Horas desde la fecha programada para CERRARLO.',
    )
    activo = models.BooleanField(default=True)

    class Meta:
        db_table = 'acuerdo_nivel_servicio'
        ordering = ['prioridad']
        verbose_name = 'Acuerdo de nivel de servicio (SLA)'
        verbose_name_plural = 'Acuerdos de nivel de servicio (SLA)'

    def __str__(self):
        return f'{self.get_prioridad_display()}: atender {self.horas_respuesta}h / resolver {self.horas_resolucion}h'


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


class RepuestoUtilizado(models.Model):
    """Repuesto/consumible usado en una intervención, con costo para el informe técnico.

    `bodega` es opcional: si se indica, descuenta stock real (ver
    apps.mantenimiento.services.registrar_repuesto_utilizado); si se deja vacío, es un
    repuesto comprado/aportado fuera del flujo de bodega (igual se registra el costo).
    """
    mantenimiento = models.ForeignKey(Mantenimiento, on_delete=models.CASCADE, related_name='repuestos_utilizados')
    tipo_consumible = models.ForeignKey(
        TipoConsumible, on_delete=models.PROTECT, related_name='usos_en_mantenimiento',
    )
    bodega = models.ForeignKey(
        Bodega, on_delete=models.PROTECT, null=True, blank=True, related_name='repuestos_utilizados',
    )
    cantidad = models.PositiveIntegerField(default=1)
    costo_unitario = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    registrado_por = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='repuestos_registrados',
    )
    registrado_en = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'repuesto_utilizado'
        ordering = ['mantenimiento', 'registrado_en', 'pk']
        verbose_name = 'Repuesto utilizado'
        verbose_name_plural = 'Repuestos utilizados'

    def __str__(self):
        return f'{self.tipo_consumible} x{self.cantidad} - Mantenimiento #{self.mantenimiento_id}'

    @property
    def costo_total(self):
        return (self.costo_unitario or Decimal('0')) * self.cantidad


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
    mantenimiento_programado = models.ForeignKey(
        MantenimientoProgramado, on_delete=models.SET_NULL, null=True, blank=True, related_name='notificaciones',
        help_text='Para avisos de "próximo a vencer" -- todavía no existe un Mantenimiento '
                  'generado al momento de avisar, así que no alcanza con el FK de arriba.',
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
