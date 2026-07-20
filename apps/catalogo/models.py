from django.core.validators import RegexValidator
from django.db import models

codigo_grupo_validator = RegexValidator(
    regex=r'^[A-Z0-9]+$',
    message='El código de grupo solo admite mayúsculas y números, ej. TRX001',
)
codigo_farmacia_validator = RegexValidator(
    regex=r'^[A-Z0-9]+$',
    message='El código de farmacia solo admite mayúsculas y números, ej. ML001',
)
codigo_estacion_validator = RegexValidator(
    regex=r'^[A-Z0-9]+-[A-Z0-9]+$',
    message='El código de estación debe tener el formato FARMACIA-SUFIJO, ej. ML001-ADM',
)


class Grupo(models.Model):
    """Canal de versión del POS. Cada farmacia pertenece a un único grupo."""
    codigo = models.CharField(max_length=10, unique=True, validators=[codigo_grupo_validator])
    nombre = models.CharField(max_length=100, blank=True)
    version_objetivo = models.CharField(
        max_length=30, blank=True,
        help_text='Versión del POS que este grupo debería tener instalada.',
    )
    activo = models.BooleanField(default=True)
    fecha_creacion = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'grupo'
        ordering = ['codigo']

    def __str__(self):
        return self.codigo


class Farmacia(models.Model):
    codigo = models.CharField(max_length=15, unique=True, validators=[codigo_farmacia_validator])
    nombre = models.CharField(max_length=150, blank=True)
    grupo = models.ForeignKey(Grupo, on_delete=models.PROTECT, related_name='farmacias')
    ubicacion = models.CharField(max_length=150, blank=True)
    telefono = models.CharField(max_length=15, blank=True)
    email = models.EmailField(blank=True)
    observacion = models.TextField(blank=True)
    activa = models.BooleanField(default=True)
    fecha_registro = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'farmacia'
        ordering = ['codigo']
        verbose_name_plural = 'Farmacias'

    def __str__(self):
        return f'{self.codigo} ({self.grupo.codigo})'


class Estacion(models.Model):
    """Equipo físico dentro de una farmacia (código FARMACIA-SUFIJO, ej. ML001-ADM)."""

    class EstadoConexion(models.TextChoices):
        NUNCA_CONECTADA = 'nunca_conectada', 'Nunca conectada'
        ONLINE = 'online', 'En línea'
        OFFLINE = 'offline', 'Fuera de línea'

    class EstadoAprobacion(models.TextChoices):
        PENDIENTE = 'pendiente', 'Pendiente de aprobación'
        APROBADA = 'aprobada', 'Aprobada'
        RECHAZADA = 'rechazada', 'Rechazada'

    codigo = models.CharField(max_length=25, unique=True, validators=[codigo_estacion_validator])
    farmacia = models.ForeignKey(Farmacia, on_delete=models.PROTECT, related_name='estaciones')

    # Identidad de hardware, reportada por el agente al enrolarse (vínculo con Módulo de Activos, Fase 4)
    numero_serie = models.CharField(max_length=100, blank=True)

    # Estado reportado por el agente vía heartbeat MQTT
    so_nombre = models.CharField(max_length=50, blank=True, help_text='Ej. Windows 10, Windows 11')
    so_build = models.CharField(max_length=30, blank=True, help_text='Ej. 19045 (22H2)')
    version_agente = models.CharField(max_length=30, blank=True)
    version_pos = models.CharField(max_length=30, blank=True)
    estado_conexion = models.CharField(
        max_length=20, choices=EstadoConexion.choices, default=EstadoConexion.NUNCA_CONECTADA,
    )
    ultimo_heartbeat = models.DateTimeField(null=True, blank=True)

    # Enrolamiento
    token_enrolamiento = models.CharField(max_length=64, unique=True, editable=False)
    hardware_id = models.CharField(
        max_length=100, blank=True,
        help_text='Identificador estable del equipo (MachineGuid de Windows). Se fija en el primer '
                  'enrolamiento y se exige en los re-enrolamientos para evitar suplantación.',
    )
    estado_aprobacion = models.CharField(
        max_length=20, choices=EstadoAprobacion.choices, default=EstadoAprobacion.PENDIENTE,
    )

    fecha_creacion = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'estacion'
        ordering = ['codigo']

    def __str__(self):
        return self.codigo

    def save(self, *args, **kwargs):
        if not self.token_enrolamiento:
            import secrets
            self.token_enrolamiento = secrets.token_hex(32)
        super().save(*args, **kwargs)

    @property
    def desactualizada(self):
        """True si la versión de POS reportada no coincide con la versión objetivo de su grupo."""
        objetivo = self.farmacia.grupo.version_objetivo
        return bool(objetivo) and self.version_pos != objetivo
