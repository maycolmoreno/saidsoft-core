from django.db import models


class DireccionSync(models.TextChoices):
    SALIENTE = 'saliente', 'Saliente (SAIDSOFT → sistema externo)'
    ENTRANTE = 'entrante', 'Entrante (sistema externo → SAIDSOFT)'


class EstadoSync(models.TextChoices):
    PENDIENTE = 'pendiente', 'Pendiente'
    ENVIADO = 'enviado', 'Enviado'
    ERROR = 'error', 'Error'


class SincronizacionExterna(models.Model):
    """Estado actual de la sincronización de un objeto con un sistema externo (Odoo,
    Active Directory, ESET, etc.). Mutable — se actualiza en cada intento.

    `conector` referencia un nombre registrado en apps.integraciones.connectors, no una FK:
    el conector concreto (ej. Odoo, Fase 3+ del roadmap) se conecta sin tocar este modelo.
    El historial completo de intentos vive en EventoSyncExterno — mismo split que
    Despliegue/ResultadoDespliegue/EventoDespliegue en apps.despliegues.
    """

    conector = models.CharField(max_length=50, help_text='Nombre registrado en el registro de conectores')
    direccion = models.CharField(max_length=10, choices=DireccionSync.choices, default=DireccionSync.SALIENTE)
    unidad_negocio = models.ForeignKey(
        'catalogo.UnidadNegocio', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='sincronizaciones_externas',
        help_text='Tenant del objeto sincronizado, derivado automáticamente. Vacío = recurso '
                  'compartido o sin tenant único (mismo criterio que EventoAuditoria).',
    )
    modelo = models.CharField(max_length=100, help_text='Etiqueta del modelo sincronizado (ej. mantenimiento.Mantenimiento)')
    objeto_id = models.CharField(max_length=50)
    objeto_repr = models.CharField(max_length=255, blank=True, help_text='str() del objeto al momento del último intento')
    estado = models.CharField(max_length=10, choices=EstadoSync.choices, default=EstadoSync.PENDIENTE)
    intentos = models.PositiveIntegerField(default=0)
    payload = models.JSONField(default=dict, blank=True, help_text='Últimos datos enviados al conector')
    respuesta = models.JSONField(default=dict, blank=True, help_text='Última respuesta cruda del sistema externo')
    ultimo_error = models.TextField(blank=True)
    creado_en = models.DateTimeField(auto_now_add=True)
    actualizado_en = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'sincronizacion_externa'
        unique_together = [('conector', 'modelo', 'objeto_id')]
        ordering = ['-actualizado_en']
        verbose_name = 'Sincronización externa'
        verbose_name_plural = 'Sincronizaciones externas'

    def __str__(self):
        return f'{self.conector}: {self.modelo}#{self.objeto_id} ({self.estado})'


class EventoSyncExterno(models.Model):
    """Línea de tiempo inmutable de una SincronizacionExterna. No se edita ni se borra."""

    sincronizacion = models.ForeignKey(SincronizacionExterna, on_delete=models.CASCADE, related_name='eventos')
    estado = models.CharField(max_length=10, choices=EstadoSync.choices)
    detalle = models.TextField(blank=True)
    respuesta = models.JSONField(default=dict, blank=True)
    timestamp = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'evento_sync_externo'
        # 'pk' como desempate: dos eventos de la misma sincronización pueden crearse en el
        # mismo tick de reloj (ej. "pendiente" seguido de "enviado" en un ejecutar_sync
        # síncrono) y timestamp (auto_now_add) no siempre alcanza a distinguirlos — sin el
        # desempate, .last() es no determinístico entre corridas.
        ordering = ['sincronizacion', 'timestamp', 'pk']
        verbose_name = 'Evento de sincronización externa'
        verbose_name_plural = 'Eventos de sincronización externa'

    def __str__(self):
        return f'{self.sincronizacion} — {self.get_estado_display()} @ {self.timestamp:%Y-%m-%d %H:%M:%S}'

    def delete(self, *args, **kwargs):
        raise NotImplementedError('EventoSyncExterno es inmutable: no se puede eliminar.')
