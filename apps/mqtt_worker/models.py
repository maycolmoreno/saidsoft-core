from django.db import models


class WorkerHeartbeat(models.Model):
    """Última señal de vida de un worker de larga duración (ej. run_mqtt_worker).

    Fila única por `nombre`: el dashboard la usa para mostrar si el worker sigue
    activo sin tener que inferirlo indirectamente por estaciones que empiezan a
    reportarse offline (señal indirecta y tardía).
    """
    nombre = models.CharField(max_length=50, unique=True)
    ultimo_latido = models.DateTimeField()

    class Meta:
        db_table = 'worker_heartbeat'
        verbose_name = 'Latido de worker'
        verbose_name_plural = 'Latidos de worker'

    def __str__(self):
        return f'{self.nombre} @ {self.ultimo_latido:%Y-%m-%d %H:%M:%S}'


class MensajeMqttFallido(models.Model):
    """Cola de revisión manual: un mensaje MQTT cuyo procesamiento lanzó una excepción
    (o no era JSON válido).

    A diferencia de EventoAuditoria/EventoDespliegue, esto no es un registro de
    negocio inmutable — es una bandeja operativa de triage: se marca `revisado` y
    se puede borrar una vez resuelto, no hace falta preservarla para siempre.
    """
    topico = models.CharField(max_length=200)
    payload_crudo = models.TextField(blank=True)
    error = models.TextField()
    revisado = models.BooleanField(default=False)
    timestamp = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'mensaje_mqtt_fallido'
        ordering = ['-timestamp']
        verbose_name = 'Mensaje MQTT fallido'
        verbose_name_plural = 'Mensajes MQTT fallidos'

    def __str__(self):
        return f'{self.topico} @ {self.timestamp:%Y-%m-%d %H:%M:%S}'
