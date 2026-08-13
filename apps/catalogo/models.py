from django.core.validators import RegexValidator
from django.db import models

codigo_grupo_validator = RegexValidator(
    regex=r'^[A-Z0-9]+$',
    message='El código de grupo solo admite mayúsculas y números, ej. TRX001',
)
codigo_unidad_negocio_validator = RegexValidator(
    regex=r'^[A-Z0-9]+$',
    message='El código de unidad de negocio solo admite mayúsculas y números, ej. SG',
)
codigo_farmacia_validator = RegexValidator(
    regex=r'^[A-Z0-9]+$',
    message='El código de farmacia solo admite mayúsculas y números, ej. ML001',
)
codigo_estacion_validator = RegexValidator(
    regex=r'^[A-Z0-9]+-[A-Z0-9]+$',
    message='El código de estación debe tener el formato FARMACIA-SUFIJO, ej. ML001-ADM',
)


class UnidadNegocio(models.Model):
    """Unidad de negocio de la empresa (ej. SG = Farmacias San Gregorio, MIA = Farmacias MIA).

    Entidad compartida entre módulos — no exclusiva de Cumplimiento: se referencia
    también desde apps.activos (Colaborador, Activo) y cualquier módulo futuro que
    necesite agrupar por unidad de negocio (Reportes, KPIs, Mesa de Ayuda).
    """
    codigo = models.CharField(max_length=10, unique=True, validators=[codigo_unidad_negocio_validator])
    nombre = models.CharField(max_length=100, blank=True)
    activo = models.BooleanField(default=True)
    fecha_creacion = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'unidad_negocio'
        ordering = ['codigo']
        verbose_name = 'Unidad de negocio'
        verbose_name_plural = 'Unidades de negocio'

    def __str__(self):
        return self.codigo


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
    unidad_negocio = models.ForeignKey(
        UnidadNegocio, on_delete=models.PROTECT, related_name='farmacias',
        help_text='Cliente/cadena dueña de esta farmacia. Define el aislamiento de datos (tenant): '
                  'una farmacia siempre pertenece a una única unidad de negocio.',
    )
    ubicacion = models.CharField(max_length=150, blank=True)
    telefono = models.CharField(max_length=15, blank=True)
    email = models.EmailField(blank=True)
    observacion = models.TextField(blank=True)
    activa = models.BooleanField(default=True)
    fecha_registro = models.DateTimeField(auto_now_add=True)
    fecha_apertura = models.DateField(
        null=True, blank=True,
        help_text='Fecha real de apertura del local (distinta de fecha_registro, que es '
                  'cuándo se creó el registro en SAIDSOFT).',
    )
    segmento_red = models.CharField(
        max_length=30, blank=True,
        help_text='Subred asignada al enlace de la farmacia (ej. 10.110.1.96/27), del '
                  'inventario de red del proveedor — sirve para diagnosticar conectividad '
                  'sin volver al Excel original.',
    )
    tipo_enlace = models.CharField(
        max_length=30, blank=True,
        help_text='Proveedor/tecnología del enlace principal (ej. TELCONET, PUNTO NET).',
    )
    tiene_backup = models.BooleanField(
        default=False,
        help_text='Si el sitio tiene enlace de respaldo — sin uno, una caída del enlace '
                  'principal deja a la farmacia sin conectividad (punto único de falla).',
    )

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
    hostname = models.CharField(
        max_length=100, blank=True,
        help_text='Nombre de equipo Windows (Environment.MachineName), reportado por el agente. '
                  'Puede diferir del código de estación si este se asignó manualmente.',
    )
    numero_serie = models.CharField(max_length=100, blank=True)

    # Estado reportado por el agente vía heartbeat MQTT
    so_nombre = models.CharField(max_length=50, blank=True, help_text='Ej. Windows 10, Windows 11')
    so_build = models.CharField(max_length=30, blank=True, help_text='Ej. 19045 (22H2)')
    version_agente = models.CharField(max_length=30, blank=True)
    version_pos = models.CharField(max_length=30, blank=True)

    # Características de hardware, consultadas bajo demanda desde el panel (no viajan en
    # cada heartbeat: son prácticamente estáticas, no vale la pena mandarlas cada minuto).
    procesador = models.CharField(max_length=150, blank=True)
    ram_total_mb = models.PositiveIntegerField(null=True, blank=True)
    almacenamiento_total_gb = models.PositiveIntegerField(null=True, blank=True)
    info_equipo_fecha = models.DateTimeField(
        null=True, blank=True, help_text='Cuándo se actualizó por última vez la info de hardware.',
    )

    # Estado de cifrado de disco, reportado junto con el resto de info de hardware (mismo
    # comando "consultar_info"). null = nunca se consultó todavía (distinto de "reportado
    # como no cifrado"). No requiere permiso especial, a diferencia de la clave de
    # recuperación (ver ClaveRecuperacionBitLocker) — es solo un sí/no, no un secreto.
    bitlocker_habilitado = models.BooleanField(null=True, blank=True)
    bitlocker_metodo_proteccion = models.CharField(
        max_length=30, blank=True,
        help_text='Ej. tpm, tpm_pin, password — tal como lo reporta el agente (Get-BitLockerVolume). '
                  'Texto libre, no choices: los protectores de BitLocker no son un set fijo pequeño.',
    )

    # Windows Update nativo (v1: solo escaneo/reporte, el agente nunca instala ni
    # reinicia solo) — comando "escanear_actualizaciones", bajo demanda desde el panel,
    # mismo patrón "info bajo demanda" que BitLocker arriba. null en
    # windows_update_pendientes = nunca se escaneó (distinto de 0 = escaneado, sin
    # pendientes).
    windows_update_ultima_verificacion = models.DateTimeField(null=True, blank=True)
    windows_update_pendientes = models.PositiveIntegerField(null=True, blank=True)
    windows_update_requiere_reinicio = models.BooleanField(null=True, blank=True)
    windows_update_detalle = models.JSONField(
        blank=True, default=list,
        help_text='Actualizaciones pendientes detectadas en el último escaneo: '
                  '[{"titulo": ..., "kb": ...}, ...].',
    )

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

    # Monitoreo: solo las estaciones marcadas (típicamente los servidores de farmacia/matriz)
    # reportan métricas de recursos, para controlar el volumen. En el sistema viejo esto lo
    # decidía el tipo de servidor; aquí es un flag explícito.
    monitorear_recursos = models.BooleanField(
        default=False,
        help_text='Si está activo, el agente de esta estación reporta métricas de RAM/CPU/latencia.',
    )

    # Distribución en cascada: la estación marcada como caché descarga el paquete del central
    # una sola vez y lo sirve por LAN a las demás cajas de su farmacia (reduce el tráfico VPN).
    es_cache_farmacia = models.BooleanField(
        default=False,
        help_text='Si está activo, el agente sirve los paquetes de despliegue por LAN a su farmacia.',
    )
    ip_lan = models.GenericIPAddressField(null=True, blank=True, help_text='IP LAN reportada por el agente.')
    puerto_cache = models.PositiveIntegerField(null=True, blank=True)

    # Acceso remoto interactivo (MeshCentral) — bolt-on independiente del canal MQTT/HMAC.
    # La correlación con el nodo de MeshCentral es manual en v1 (ver apps/catalogo/services.py):
    # no hay sincronización automática contra la API de MeshCentral todavía.
    meshcentral_node_id = models.CharField(
        max_length=64, blank=True,
        help_text='ID interno del dispositivo en MeshCentral (se copia manualmente desde la '
                  'consola de MeshCentral tras instalar el agente). Vacío = no vinculada.',
    )
    meshcentral_vinculado_en = models.DateTimeField(null=True, blank=True)

    fecha_creacion = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'estacion'
        ordering = ['codigo']
        permissions = [
            ('acceso_remoto_estacion', 'Puede abrir sesiones de acceso remoto (MeshCentral) a estaciones'),
            # Permiso separado a propósito: ver grabaciones de sesión (auditoría de atención al
            # cliente) es una capacidad más sensible que dar soporte remoto en vivo, y no todo el
            # que tiene acceso_remoto_estacion debería tenerla por defecto.
            ('supervision_auditoria_estacion', 'Puede ver grabaciones de sesión (auditoría) de estaciones'),
            # Más sensible todavía que las dos de arriba: con la clave de recuperación se
            # descifra el disco completo. Permiso propio, nadie lo hereda de los otros dos.
            ('ver_clave_bitlocker', 'Puede ver la clave de recuperación de BitLocker de estaciones'),
        ]

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


class ClaveRecuperacionBitLocker(models.Model):
    """Clave de recuperación de BitLocker de una estación, cifrada en reposo.

    Una fila por estación (se sobrescribe si BitLocker rota la clave — no hace falta
    historial de claves viejas, solo la que sirve para descifrar el disco *ahora*).
    `clave_cifrada` nunca se guarda ni se muestra en texto plano fuera de la vista que
    la descifra bajo demanda (apps.catalogo.services.obtener_clave_bitlocker_descifrada),
    detrás del permiso `catalogo.ver_clave_bitlocker` y auditada en cada consulta.
    """
    estacion = models.OneToOneField(Estacion, on_delete=models.CASCADE, related_name='clave_bitlocker')
    clave_cifrada = models.TextField(help_text='Token Fernet — nunca texto plano.')
    id_protector = models.CharField(
        max_length=100, blank=True,
        help_text='Recovery Key ID de BitLocker (no sensible por sí solo): ayuda a confirmar '
                  'en Windows que es la clave correcta antes de aplicarla.',
    )
    actualizada_en = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'clave_recuperacion_bitlocker'
        verbose_name = 'Clave de recuperación BitLocker'
        verbose_name_plural = 'Claves de recuperación BitLocker'

    def __str__(self):
        return f'Clave BitLocker de {self.estacion.codigo}'
