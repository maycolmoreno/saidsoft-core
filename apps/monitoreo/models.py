from django.conf import settings
from django.db import models

from apps.catalogo.models import Estacion, UnidadNegocio


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


class Metrica(models.TextChoices):
    CPU_CARGA_PCT = 'cpu_carga_pct', 'CPU (%)'
    RAM_USADA_PCT = 'ram_usada_pct', 'RAM (%)'
    LATENCIA_MS = 'latencia_ms', 'Latencia (ms)'
    TEMPERATURA_C = 'temperatura_c', 'Temperatura (°C)'
    SIN_HEARTBEAT = 'sin_heartbeat', 'Sin heartbeat (minutos)'
    BITLOCKER_DESHABILITADO = 'bitlocker_deshabilitado', 'BitLocker deshabilitado'


class ReglaAlerta(models.Model):
    """Condición que, sostenida por `duracion_minutos`, abre una Alerta.

    `sin_heartbeat` y `bitlocker_deshabilitado` son las únicas métricas que no
    comparan contra MuestraMetrica: `sin_heartbeat` se evalúa en el comando
    `marcar_estaciones_offline`; `bitlocker_deshabilitado` se evalúa en
    apps.mqtt_worker.services.manejar_info_equipo, justo cuando el agente reporta el
    estado de cifrado (Estacion.bitlocker_habilitado) — no es una serie de tiempo, es
    un estado binario, así que no hay "condición sostenida" que evaluar: se abre o
    resuelve la alerta directo con cada reporte.
    """

    class Operador(models.TextChoices):
        GTE = 'gte', '≥ (mayor o igual)'
        LTE = 'lte', '≤ (menor o igual)'

    class Severidad(models.TextChoices):
        WARNING = 'warning', 'Advertencia'
        CRITICAL = 'critical', 'Crítica'

    nombre = models.CharField(max_length=150)
    metrica = models.CharField(max_length=30, choices=Metrica.choices)
    operador = models.CharField(
        max_length=3, choices=Operador.choices, default=Operador.GTE,
        help_text='Ignorado para "Sin heartbeat" (siempre "más de X minutos") y para '
                  '"BitLocker deshabilitado" (condición binaria, no hay umbral que comparar).',
    )
    umbral = models.FloatField(
        default=0,
        help_text='% para CPU/RAM, ms para latencia, °C para temperatura, minutos para sin heartbeat. '
                  'Ignorado (dejar en 0) para "BitLocker deshabilitado".',
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

    class Meta:
        db_table = 'alerta'
        ordering = ['-abierta_en']

    def __str__(self):
        return f'{self.regla.nombre} · {self.estacion.codigo} ({self.get_estado_display()})'
