from django.conf import settings
from django.db import models
from django.utils import timezone


class WorkerHeartbeat(models.Model):
    """Última señal de vida de un worker de larga duración (ej. run_mqtt_worker).

    Fila única por `nombre`: el dashboard la usa para mostrar si el worker sigue
    activo sin tener que inferirlo indirectamente por estaciones que empiezan a
    reportarse offline (señal indirecta y tardía).

    **`ultimo_latido` prueba que el PROCESO vive, y nada más.** Lo escribe un hilo aparte
    (`run_mqtt_worker._latido_periodico`) cada 30 s mientras el proceso exista, así que un
    worker desconectado del broker —o conectado y sin procesar un solo mensaje— se veía
    perfectamente sano en el panel. Ese hilo se agregó para resolver el falso negativo
    contrario (un worker sano sin tráfico aparecía "sin señal" a los 90 s) y abrió este
    falso positivo. Los dos campos de abajo son lo que cierra la diferencia; ver el
    hallazgo D-05 en docs/auditoria/centro-monitoreo/.
    """
    nombre = models.CharField(max_length=50, unique=True)
    ultimo_latido = models.DateTimeField()
    conectado = models.BooleanField(
        null=True, blank=True,
        help_text='Si el worker estaba conectado a su broker la última vez que latió. '
                  'null = ese worker no sabe responder la pregunta (los procesos que no '
                  'hablan con un broker, como el respaldo, y cualquier versión anterior '
                  'a este campo). null NO es "no conectado": es "sin verificar", y el '
                  'panel lo muestra así en vez de pintarlo de verde.',
    )
    mensajes_procesados = models.PositiveBigIntegerField(
        default=0,
        help_text='Mensajes que este worker procesó desde que arrancó. Es lo que '
                  'distingue "conectado" de "procesando": si no se mueve mientras hay '
                  'estaciones en línea, el worker está suscrito y no está recibiendo. Se '
                  'reinicia en cada arranque a propósito — lo que importa es si AVANZA, '
                  'no cuánto vale.',
    )

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


class EnrolamientoRechazado(models.Model):
    """Un agente pidió enrolarse con un código cuyo sitio no existe.

    Hasta el 27-sep-2026 esto solo quedaba en el log del worker
    (`apps.mqtt_worker.services`, "Enrolamiento rechazado: farmacia no encontrada"), así
    que el síntoma era: se instala el agente, el técnico se va, y la estación **nunca
    aparece en el panel** sin que nadie sepa por qué. Con instalaciones manuales y
    códigos escritos a mano, un código mal tipeado no es una posibilidad: es cuestión de
    tiempo.

    Misma forma que `MensajeMqttFallido`: es una bandeja operativa de triage, no un
    registro de negocio inmutable. Se marca `revisado` y se puede borrar.

    Se guarda el `hostname` aparte del código justamente porque pueden no coincidir —
    desde que el instalador acepta `-Codigo`, el código es una decisión de quien instala
    y el hostname es lo que permite ir a buscar la máquina.
    """

    codigo_recibido = models.CharField(
        max_length=100, help_text='El código con el que el agente intentó enrolarse.',
    )
    hostname = models.CharField(
        max_length=120, blank=True,
        help_text='Nombre de Windows del equipo: con esto se lo ubica, aunque el código esté mal.',
    )
    motivo = models.CharField(max_length=200, default='Sitio no encontrado para el código.')
    revisado = models.BooleanField(default=False)
    revisado_por = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='enrolamientos_rechazados_revisados',
    )
    revisado_en = models.DateTimeField(null=True, blank=True)
    intentos = models.PositiveIntegerField(
        default=1,
        help_text='El agente reintenta solo: se cuenta en vez de crear una fila por intento.',
    )
    primer_intento = models.DateTimeField(default=timezone.now)
    ultimo_intento = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = 'enrolamiento_rechazado'
        ordering = ['-ultimo_intento']
        constraints = [
            # Un agente mal configurado reintenta cada minuto: sin esto la bandeja se
            # llenaría de miles de filas del mismo equipo y dejaría de servir para triage.
            models.UniqueConstraint(
                fields=['codigo_recibido', 'hostname'], name='enrolamiento_rechazado_unico',
            ),
        ]
        verbose_name = 'Enrolamiento rechazado'
        verbose_name_plural = 'Enrolamientos rechazados'

    def __str__(self):
        return f'{self.codigo_recibido} ({self.hostname or "sin hostname"})'

    @property
    def sitio_sugerido(self):
        """El prefijo que el servidor buscó y no encontró. Es lo primero que mira quien
        revisa: casi siempre el sitio existe con otro código, o falta crearlo."""
        return self.codigo_recibido.split('-')[0] if self.codigo_recibido else ''
