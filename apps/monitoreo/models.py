from django.conf import settings
from django.db import models

from apps.catalogo.models import Estacion, Farmacia, Grupo, UnidadNegocio


class MuestraMetrica(models.Model):
    """Una muestra de recursos de una estación en un instante.

    Unifica en una sola fila lo que el sistema viejo separaba en log_servidor_memoria
    y log_servidor_cpu: el agente arma una muestra completa y la publica junta, lo que
    evita joins al graficar. Valores de memoria en MB. null = no medido.

    A esta escala (1.800 equipos), en producción esta tabla va sobre TimescaleDB
    (hypertable + compresión + retención automática). En desarrollo, SQLite basta.
    """

    estacion = models.ForeignKey(Estacion, on_delete=models.CASCADE, related_name='metricas')

    # Memoria (MB)
    ram_total = models.PositiveIntegerField(null=True, blank=True)
    ram_usada = models.PositiveIntegerField(null=True, blank=True)
    ram_libre = models.PositiveIntegerField(null=True, blank=True)
    cache = models.PositiveIntegerField(null=True, blank=True)
    swap_total = models.PositiveIntegerField(null=True, blank=True)
    swap_usada = models.PositiveIntegerField(null=True, blank=True)

    # CPU y red
    cpu_carga_pct = models.FloatField(null=True, blank=True, help_text='% de uso de CPU (0-100).')
    temperatura_c = models.FloatField(null=True, blank=True, help_text='°C del CPU, si el equipo lo expone.')
    latencia_ms = models.FloatField(null=True, blank=True, help_text='Latencia de red hacia el servidor central.')

    # Disco (GB) — del volumen C:, mismo mecanismo que almacenamiento_total_gb de
    # Estacion (Win32_LogicalDisk), pero como serie temporal en vez de dato puntual.
    disco_total_gb = models.FloatField(null=True, blank=True)
    disco_libre_gb = models.FloatField(null=True, blank=True)

    # Red (KB/s) — tasa ya calculada por el agente (contadores acumulados del
    # adaptador de la ruta por defecto, convertidos a tasa entre dos muestras, ver
    # agente-prueba/agente_prueba.py::_tasa_red_kbps). No se guarda el contador
    # crudo acá — a diferencia de MuestraRedFarmacia (SNMP a Mikrotik), este valor ya
    # viene calculado por un proceso de larga duración que puede quedarse la muestra
    # anterior en memoria.
    red_recibido_kbps = models.FloatField(null=True, blank=True)
    red_enviado_kbps = models.FloatField(null=True, blank=True)

    timestamp = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        db_table = 'muestra_metrica'
        ordering = ['-timestamp']
        indexes = [
            models.Index(fields=['estacion', '-timestamp']),
        ]

    def __str__(self):
        return f'{self.estacion.codigo} @ {self.timestamp:%Y-%m-%d %H:%M:%S}'

    @property
    def ram_usada_pct(self):
        if self.ram_total and self.ram_usada is not None:
            return round(100 * self.ram_usada / self.ram_total, 1)
        return None

    @property
    def disco_usado_pct(self):
        if self.disco_total_gb and self.disco_libre_gb is not None:
            return round(100 * (self.disco_total_gb - self.disco_libre_gb) / self.disco_total_gb, 1)
        return None

    @property
    def red_total_kbps(self):
        if self.red_recibido_kbps is None and self.red_enviado_kbps is None:
            return None
        return round((self.red_recibido_kbps or 0) + (self.red_enviado_kbps or 0), 1)


class MuestraRedFarmacia(models.Model):
    """Una muestra de ancho de banda del enlace de una FARMACIA (no de una estación),
    sondeada por SNMP al Mikrotik del sitio — ver apps.monitoreo.mikrotik. Mismo
    espíritu que MuestraMetrica pero a nivel de sitio: el router no reparte tráfico
    por estación (sin Queues por IP/MAC), así que esto es lo más granular que se
    puede medir del lado del enlace. No se fuerza en EstadoDispositivo/Alerta
    (estación-scoped, no encajan) — sigue el precedente de modelos paralelos por
    granularidad que ya usa apps.cumplimiento (ResultadoCumplimientoEstacion +
    ResultadoCumplimientoFarmacia).

    bytes_recibidos/bytes_enviados son el contador CRUDO acumulado que reportó el
    router en este sondeo (IF-MIB ifHCIn/OutOctets, 64 bits) — se guardan para poder
    auditar/depurar el cálculo. red_recibido_kbps/red_enviado_kbps es la tasa ya
    calculada al guardar, diferenciando contra la fila anterior de esta farmacia
    (NO memoria de proceso: el poller corre en un task de Celery que puede reiniciar
    entre corridas, a diferencia del agente de estación). null = primera muestra de
    esta farmacia, o el contador bajó respecto a la anterior (reinicio del router).
    """

    farmacia = models.ForeignKey(Farmacia, on_delete=models.CASCADE, related_name='muestras_red')
    bytes_recibidos = models.BigIntegerField()
    bytes_enviados = models.BigIntegerField()
    red_recibido_kbps = models.FloatField(null=True, blank=True)
    red_enviado_kbps = models.FloatField(null=True, blank=True)
    timestamp = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        db_table = 'muestra_red_farmacia'
        ordering = ['-timestamp']
        indexes = [
            models.Index(fields=['farmacia', '-timestamp']),
        ]
        verbose_name = 'Muestra de red de farmacia'
        verbose_name_plural = 'Muestras de red de farmacia'

    def __str__(self):
        return f'{self.farmacia.codigo} @ {self.timestamp:%Y-%m-%d %H:%M:%S}'

    @property
    def red_total_kbps(self):
        if self.red_recibido_kbps is None and self.red_enviado_kbps is None:
            return None
        return round((self.red_recibido_kbps or 0) + (self.red_enviado_kbps or 0), 1)


class Metrica(models.TextChoices):
    CPU_CARGA_PCT = 'cpu_carga_pct', 'CPU (%)'
    RAM_USADA_PCT = 'ram_usada_pct', 'RAM (%)'
    DISCO_USADO_PCT = 'disco_usado_pct', 'Disco usado (%)'
    LATENCIA_MS = 'latencia_ms', 'Latencia (ms)'
    TEMPERATURA_C = 'temperatura_c', 'Temperatura (°C)'
    RED_TOTAL_KBPS = 'red_total_kbps', 'Red (KB/s)'
    SIN_HEARTBEAT = 'sin_heartbeat', 'Sin heartbeat (minutos)'
    BITLOCKER_DESHABILITADO = 'bitlocker_deshabilitado', 'BitLocker deshabilitado'
    AGENTE_CAIDO_RED_VIVA = 'agente_caido_red_viva', 'Agente sin reportar (con red viva)'
    POS_ERRORES = 'pos_errores', 'Errores del POS (por ventana de reporte)'


class EstadoDispositivo(models.Model):
    """Snapshot ACTUAL (no histórico) del estado de una estación según una fuente de
    monitoreo (agente MQTT, MeshCentral, y a futuro ESET PROTECT).

    Convive con `Estacion.estado_conexion`/`ultimo_heartbeat` — no los reemplaza. Esos
    dos campos siguen siendo el resumen de la fuente MQTT que ya usa todo el resto del
    panel/reportes; esta tabla es la capa nueva que permite cruzar MQTT contra otras
    fuentes (`evaluar_cruce_monitoreo`, ver services.py) sin tocar ese código existente.

    Una fila por (estacion, fuente): se actualiza in-place (`update_or_create`) en cada
    señal — no es un histórico, para eso está `EventoMonitoreo`.
    """

    class Fuente(models.TextChoices):
        MQTT = 'mqtt', 'Agente MQTT'
        MESHCENTRAL = 'meshcentral', 'MeshCentral'
        # ESET = 'eset', 'ESET PROTECT'  # pendiente de aprobación de acceso a su API —
        # el modelo ya queda listo para sumarla (agregar el choice + un adapter nuevo).

    estacion = models.ForeignKey(Estacion, on_delete=models.CASCADE, related_name='estados_dispositivo')
    fuente = models.CharField(max_length=20, choices=Fuente.choices)
    en_linea = models.BooleanField()
    detalle = models.JSONField(
        blank=True, default=dict,
        help_text='Payload específico de la fuente (ej. conn de MeshCentral). No forma parte '
                  'del contrato del cruce — solo referencia/debug.',
    )
    actualizado_en = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'estado_dispositivo'
        unique_together = [('estacion', 'fuente')]
        ordering = ['estacion__codigo', 'fuente']
        verbose_name = 'Estado de dispositivo'
        verbose_name_plural = 'Estados de dispositivo'

    def __str__(self):
        return f'{self.estacion.codigo} · {self.get_fuente_display()}: {"en línea" if self.en_linea else "fuera de línea"}'


class EventoMonitoreo(models.Model):
    """Histórico de TRANSICIONES de EstadoDispositivo (hypertable en producción, igual
    que MuestraMetrica) — se escribe solo cuando `en_linea` cambia respecto al último
    EstadoDispositivo conocido, no en cada señal recibida. A la frecuencia de heartbeat/
    eventos de 1.800+ estaciones, un evento por señal sería ruido puro sin valor; lo que
    importa para el cruce y para auditar discrepancias es cuándo cambió el estado.
    """

    estacion = models.ForeignKey(Estacion, on_delete=models.CASCADE, related_name='eventos_monitoreo')
    fuente = models.CharField(max_length=20, choices=EstadoDispositivo.Fuente.choices)
    en_linea = models.BooleanField()
    detalle = models.JSONField(blank=True, default=dict)
    timestamp = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        db_table = 'evento_monitoreo'
        ordering = ['-timestamp']
        indexes = [
            models.Index(fields=['estacion', '-timestamp']),
        ]

    def __str__(self):
        return f'{self.estacion.codigo} · {self.get_fuente_display()} -> {"en línea" if self.en_linea else "fuera de línea"} @ {self.timestamp:%Y-%m-%d %H:%M:%S}'


class ReglaAlerta(models.Model):
    """Condición que, sostenida por `duracion_minutos`, abre una Alerta.

    `sin_heartbeat`, `bitlocker_deshabilitado`, `agente_caido_red_viva` y
    `pos_errores` son las únicas métricas que no comparan contra MuestraMetrica ni
    usan `duracion_minutos`:
    - `sin_heartbeat` se evalúa en el comando `marcar_estaciones_offline` (umbral en
      minutos sin heartbeat).
    - `bitlocker_deshabilitado` se evalúa en apps.mqtt_worker.services.manejar_info_equipo,
      justo cuando el agente reporta el estado de cifrado (Estacion.bitlocker_habilitado)
      — no es una serie de tiempo, es un estado binario: se abre o resuelve directo con
      cada reporte.
    - `agente_caido_red_viva` (cruce MQTT × MeshCentral, ver EstadoDispositivo/
      evaluar_cruce_monitoreo) reusa el mismo umbral en minutos que `sin_heartbeat`
      (minutos sin heartbeat MQTT), exigiendo además que MeshCentral vea la estación en
      línea — distingue "agente caído, red viva" de "red caída, ambas fuentes lo ven mal".
    - `pos_errores` se evalúa en apps.mqtt_worker.services.manejar_pos_errores: cada
      reporte del agente ya es una ventana cerrada (los ERROR/FATAL nuevos del log del
      POS desde el último chequeo, agrupados por mensaje), así que el umbral compara
      directo contra el total de esa ventana — ver PosErrorDetectado.
    """

    class Operador(models.TextChoices):
        GTE = 'gte', '≥ (mayor o igual)'
        LTE = 'lte', '≤ (menor o igual)'

    class Severidad(models.TextChoices):
        WARNING = 'warning', 'Advertencia'
        CRITICAL = 'critical', 'Crítica'

    nombre = models.CharField(max_length=150)
    # No toda alerta amerita mandar un técnico: un pico de CPU se resuelve solo, un POS
    # que no levanta no. Apagado por defecto para que activar una regla nueva no empiece
    # a generar órdenes de trabajo sin que nadie lo haya decidido.
    abre_mantenimiento = models.BooleanField(
        default=False,
        help_text='Si al dispararse esta regla se abre automáticamente un mantenimiento '
                  'para el equipo de la estación afectada.',
    )
    metrica = models.CharField(max_length=30, choices=Metrica.choices)
    operador = models.CharField(
        max_length=3, choices=Operador.choices, default=Operador.GTE,
        help_text='Ignorado para "Sin heartbeat"/"Agente sin reportar (con red viva)" (siempre '
                  '"más de X minutos") y para "BitLocker deshabilitado" (condición binaria, sin umbral).',
    )
    umbral = models.FloatField(
        default=0,
        help_text='% para CPU/RAM, ms para latencia, °C para temperatura, minutos para sin heartbeat '
                  'y para "Agente sin reportar (con red viva)". Ignorado (dejar en 0) para "BitLocker '
                  'deshabilitado".',
    )
    duracion_minutos = models.PositiveIntegerField(
        default=10,
        help_text='La condición debe sostenerse este tiempo antes de abrir la alerta (evita falsos positivos por un pico aislado).',
    )
    severidad = models.CharField(max_length=10, choices=Severidad.choices, default=Severidad.WARNING)
    unidad_negocio = models.ForeignKey(
        UnidadNegocio, on_delete=models.PROTECT, null=True, blank=True, related_name='reglas_alerta',
        help_text='Vacío = regla global, aplica a todos los clientes. Con valor = solo ese cliente.',
    )
    activo = models.BooleanField(default=True)
    creado_por = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='reglas_alerta_creadas',
    )
    fecha_creacion = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'regla_alerta'
        ordering = ['nombre']
        verbose_name = 'Regla de alerta'
        verbose_name_plural = 'Reglas de alerta'

    def __str__(self):
        return self.nombre


class Alerta(models.Model):
    """Una instancia de ReglaAlerta incumplida en una estación puntual."""

    class Estado(models.TextChoices):
        ABIERTA = 'abierta', 'Abierta'
        RECONOCIDA = 'reconocida', 'Reconocida'
        RESUELTA = 'resuelta', 'Resuelta'

    regla = models.ForeignKey(ReglaAlerta, on_delete=models.PROTECT, related_name='alertas')
    estacion = models.ForeignKey(Estacion, on_delete=models.CASCADE, related_name='alertas')
    estado = models.CharField(max_length=10, choices=Estado.choices, default=Estado.ABIERTA)
    valor_disparador = models.FloatField(help_text='Valor de la métrica que confirmó la alerta.')

    abierta_en = models.DateTimeField(auto_now_add=True)
    reconocida_en = models.DateTimeField(null=True, blank=True)
    reconocida_por = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='+',
    )
    resuelta_en = models.DateTimeField(null=True, blank=True)
    # Referencia perezosa por string: evita que apps.monitoreo importe apps.mantenimiento
    # a nivel de módulo (hoy la dependencia va en un solo sentido y conviene que siga así).
    mantenimiento = models.ForeignKey(
        'mantenimiento.Mantenimiento', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='alertas_origen',
        help_text='Mantenimiento abierto automáticamente por esta alerta, si la regla lo pide.',
    )
    escalada_en = models.DateTimeField(
        null=True, blank=True,
        help_text='Cuándo se reenvió la notificación por seguir ABIERTA sin reconocer '
                  '(ver apps.monitoreo.services.escalar_alertas_abiertas). Evita reescalar en cada corrida.',
    )

    class Meta:
        db_table = 'alerta'
        ordering = ['-abierta_en']

    def __str__(self):
        return f'{self.regla.nombre} · {self.estacion.codigo} ({self.get_estado_display()})'


class PosErrorDetectado(models.Model):
    """Un mensaje de error distinto detectado en el log del POS de una estación
    (Logs\\GeneraXML.txt del propio Zabyca.Pos.Desktop, vía log4net — pese al nombre
    del archivo, captura errores generales de la aplicación, no solo generación de
    XML). El agente reporta periódicamente (`bucle_log_pos`) solo lo nuevo desde su
    última lectura, ya agrupado por mensaje exacto; acá se acumula: a diferencia de
    SoftwareInstaladoDetectado (snapshot que se reemplaza en cada escaneo), esto es un
    contador de por vida por (estación, mensaje) — el mismo bug real (ej. una relación
    de base de datos faltante) repite el mismo mensaje una y otra vez, así que la
    cardinalidad esperada es chica por estación, no hace falta purgar.

    Distinto de MuestraMetrica: no es una serie de tiempo de un valor numérico, es
    contenido de texto — por eso no vive como property de MuestraMetrica ni se evalúa
    con el mecanismo genérico de evaluar_reglas_metricas (ver evaluar_regla_pos_errores
    en services.py, que sí reusa el resto del motor de alertas).
    """

    class Categoria(models.TextChoices):
        SISTEMA = 'sistema', 'Sistema (cuenta para la alerta)'
        # "Negocio" = una validación del POS haciendo su trabajo (ej. "VENTA SIN LOTE"
        # bloqueando una venta sin lote) — nivel ERROR en el log del POS, pero NO es
        # una falla de infraestructura. Confirmado con el usuario que es rutinario, no
        # esporádico: contarlo igual que un timeout de base habría inundado la alerta
        # de falsos positivos. Se sigue guardando (visible en la ficha), solo no suma
        # al total que evalúa evaluar_regla_pos_errores. Ver
        # apps.monitoreo.services.clasificar_error_pos.
        NEGOCIO = 'negocio', 'Negocio (no cuenta para la alerta)'

    estacion = models.ForeignKey(Estacion, on_delete=models.CASCADE, related_name='pos_errores')
    mensaje = models.CharField(max_length=500)
    nivel = models.CharField(max_length=10, default='ERROR')
    categoria = models.CharField(max_length=10, choices=Categoria.choices, default=Categoria.SISTEMA)
    cantidad_total = models.PositiveIntegerField(default=0)
    primera_vez = models.DateTimeField(auto_now_add=True)
    ultima_vez = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'pos_error_detectado'
        ordering = ['-ultima_vez']
        unique_together = [('estacion', 'mensaje')]
        verbose_name = 'Error del POS detectado'
        verbose_name_plural = 'Errores del POS detectados'

    def __str__(self):
        return f'{self.estacion.codigo}: {self.mensaje[:60]} (x{self.cantidad_total})'


class VentanaMantenimiento(models.Model):
    """Ventana de tiempo durante la cual las alertas de un destino de estaciones se
    silencian a propósito (ej. un despliegue o reinicio masivo programado), para que
    una acción operativa propia no se confunda con un problema real en `/alertas/`.

    Mismo shape de destino que ScriptProgramado/EjecucionScript/Despliegue
    (unidad_negocio/destino_tipo/grupos/farmacias/estaciones), resuelto con
    apps.catalogo.services.resolver_estaciones (ver
    apps.monitoreo.services.ventana_mantenimiento_activa, que es el único punto que
    la consulta — vía el hook en abrir_o_mantener_alerta, cubre a todas las reglas
    de alerta existentes y futuras sin tocar cada evaluador por separado).
    """

    class DestinoTipo(models.TextChoices):
        CADENA = 'cadena', 'Toda la cadena'
        GRUPOS = 'grupos', 'Grupos específicos'
        FARMACIAS = 'farmacias', 'Farmacias específicas'
        ESTACIONES = 'estaciones', 'Estaciones específicas'

    unidad_negocio = models.ForeignKey(
        UnidadNegocio, on_delete=models.PROTECT, related_name='ventanas_mantenimiento',
        help_text='Cliente al que se dirige. "Toda la cadena" significa toda la cadena '
                  'de esta unidad de negocio, nunca de otras.',
    )
    destino_tipo = models.CharField(max_length=20, choices=DestinoTipo.choices)
    grupos = models.ManyToManyField(Grupo, blank=True, related_name='ventanas_mantenimiento')
    farmacias = models.ManyToManyField(Farmacia, blank=True, related_name='ventanas_mantenimiento')
    estaciones = models.ManyToManyField(Estacion, blank=True, related_name='ventanas_mantenimiento')

    desde = models.DateTimeField()
    hasta = models.DateTimeField()
    motivo = models.CharField(max_length=255, help_text='Ej. "despliegue de POS v5.2 a toda la cadena".')
    activo = models.BooleanField(default=True)

    creado_por = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='ventanas_mantenimiento_creadas',
    )
    fecha_creacion = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'ventana_mantenimiento'
        ordering = ['-desde']
        verbose_name = 'Ventana de mantenimiento'
        verbose_name_plural = 'Ventanas de mantenimiento'

    def __str__(self):
        return f'{self.motivo} ({self.unidad_negocio.codigo}, {self.desde:%d/%m %H:%M}–{self.hasta:%d/%m %H:%M})'

    @property
    def esta_en_curso(self):
        from django.utils import timezone
        return self.activo and self.desde <= timezone.now() <= self.hasta


class CanalNotificacion(models.Model):
    """Webhook al que reenviar el aviso de una Alerta, además del correo (que sigue
    yendo siempre vía notificar_alerta, sin pasar por este modelo). unidad_negocio en
    blanco = canal global, usado por cualquier unidad que no tenga uno propio — mismo
    criterio "global o del cliente" que ReglaAlerta.unidad_negocio."""

    class Tipo(models.TextChoices):
        WEBHOOK_TEAMS = 'webhook_teams', 'Webhook de Microsoft Teams'

    unidad_negocio = models.ForeignKey(
        UnidadNegocio, on_delete=models.PROTECT, null=True, blank=True, related_name='canales_notificacion',
        help_text='Vacío = canal global, aplica a toda unidad de negocio que no tenga uno propio.',
    )
    tipo = models.CharField(max_length=20, choices=Tipo.choices, default=Tipo.WEBHOOK_TEAMS)
    destino = models.URLField(help_text='URL del webhook entrante de Teams.')
    activo = models.BooleanField(default=True)
    creado_por = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='canales_notificacion_creados',
    )
    fecha_creacion = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'canal_notificacion'
        ordering = ['unidad_negocio__codigo', 'tipo']
        verbose_name = 'Canal de notificación'
        verbose_name_plural = 'Canales de notificación'

    def __str__(self):
        destino_str = self.unidad_negocio.codigo if self.unidad_negocio_id else 'Global'
        return f'{self.get_tipo_display()} ({destino_str})'
