"""Chatbot de WhatsApp con las farmacias: las novedades y los mensajes.

Qué se le reporta a una farmacia y qué no está definido en PLAN_MODERNIZACION.md §10-CM, y
la regla de fondo es que ningún mensaje quede sin alguien que mire la respuesta: lo que la
farmacia no contesta, o contesta con algo que hay que atender, escala a la mesa de ayuda.
"""
from django.conf import settings
from django.db import models
from django.db.models import Q

from apps.catalogo.models import Estacion, Farmacia


class Novedad(models.Model):
    """Algo que pasa en una farmacia y que alguien tiene que seguir.

    Nace de dos lados: el sistema le PREGUNTA a la farmacia (su enlace se cayó solo, o una
    caja dejó de reportar), o la farmacia REPORTA por su cuenta (se fue la luz, no hay
    internet, un equipo no funciona). En los dos casos termina con una causa y, si hace
    falta, en la bandeja de la mesa de ayuda.
    """

    class Origen(models.TextChoices):
        SISTEMA = 'sistema', 'Pregunta del sistema'
        FARMACIA = 'farmacia', 'Reporte de la farmacia'

    class Motivo(models.TextChoices):
        ENLACE_CAIDO = 'enlace_caido', 'Enlace caído'
        ESTACION_SIN_REPORTAR = 'estacion_sin_reportar', 'Caja sin reportar'
        REPORTE_LUZ = 'reporte_luz', 'Reportó corte de luz'
        REPORTE_INTERNET = 'reporte_internet', 'Reportó falta de internet'
        REPORTE_EQUIPO = 'reporte_equipo', 'Reportó un equipo que no funciona'
        MENSAJE = 'mensaje', 'Mensaje sin clasificar'

    class Causa(models.TextChoices):
        LUZ = 'luz', 'Se fue la luz'
        INTERNET = 'internet', 'Hay luz, no hay internet'
        TODO_FUNCIONA = 'todo_funciona', 'Todo funciona'
        CAJA_ENCENDIDA = 'caja_encendida', 'La caja está encendida'
        CAJA_APAGADA = 'caja_apagada', 'La caja estaba apagada'
        NO_SABE = 'no_sabe', 'No sabe'
        EQUIPO = 'equipo', 'Equipo con falla'

    class Estado(models.TextChoices):
        ESPERANDO = 'esperando', 'Esperando respuesta de la farmacia'
        REGISTRADA = 'registrada', 'Registrada (no requiere acción)'
        ESCALADA = 'escalada', 'En la mesa de ayuda'
        EN_ATENCION = 'en_atencion', 'Tomada por la mesa de ayuda'
        CERRADA = 'cerrada', 'Cerrada'

    ABIERTAS = (Estado.ESPERANDO, Estado.REGISTRADA, Estado.ESCALADA, Estado.EN_ATENCION)

    farmacia = models.ForeignKey(Farmacia, on_delete=models.PROTECT, related_name='novedades')
    origen = models.CharField(max_length=10, choices=Origen.choices)
    motivo = models.CharField(max_length=25, choices=Motivo.choices)
    causa = models.CharField(max_length=20, choices=Causa.choices, blank=True)
    estado = models.CharField(max_length=12, choices=Estado.choices, db_index=True)

    evento_enlace = models.ForeignKey(
        'monitoreo.EventoEnlaceFarmacia', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='novedades', help_text='La caída de enlace a la que se refiere, si hay una.',
    )
    estacion = models.ForeignKey(
        Estacion, on_delete=models.SET_NULL, null=True, blank=True, related_name='novedades',
    )
    equipo = models.CharField(max_length=40, blank=True, help_text='Qué equipo reportó la farmacia.')
    descripcion = models.TextField(blank=True, help_text='Lo que escribió la farmacia.')
    foto_media_id = models.CharField(max_length=100, blank=True, help_text='ID de la foto en WhatsApp.')

    creado_en = models.DateTimeField(auto_now_add=True)
    preguntado_en = models.DateTimeField(null=True, blank=True)
    respondido_en = models.DateTimeField(null=True, blank=True)
    respondido_por_mesa = models.BooleanField(
        default=False, help_text='La causa la registró la mesa de ayuda (llamando), no la farmacia.',
    )
    escalado_en = models.DateTimeField(null=True, blank=True)
    motivo_escalada = models.CharField(max_length=40, blank=True)
    reescalado_en = models.DateTimeField(null=True, blank=True)
    tomado_por = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='novedades_tomadas',
    )
    tomado_por_texto = models.CharField(
        max_length=100, blank=True, help_text='Quién la tomó desde Telegram (nombre del chat).',
    )
    tomado_en = models.DateTimeField(null=True, blank=True)
    cerrado_en = models.DateTimeField(null=True, blank=True)
    nota_cierre = models.CharField(max_length=200, blank=True)

    class Meta:
        db_table = 'novedad_farmacia'
        ordering = ['-creado_en']
        verbose_name = 'Novedad de farmacia'
        verbose_name_plural = 'Novedades de farmacias'
        constraints = [
            # Un incidente, una pregunta: el sistema nunca pregunta dos veces por la misma
            # caída ni por la misma caja (§10-CM, "un mensaje por incidente").
            models.UniqueConstraint(
                fields=['evento_enlace'], condition=Q(origen='sistema', motivo='enlace_caido'),
                name='una_pregunta_por_caida',
            ),
        ]
        indexes = [models.Index(fields=['farmacia', '-creado_en'])]

    def __str__(self):
        return f'{self.farmacia.codigo}: {self.get_motivo_display()} ({self.get_estado_display()})'

    @property
    def abierta(self) -> bool:
        return self.estado in self.ABIERTAS

    @property
    def motivo_escalada_texto(self) -> str:
        from apps.whatsapp.servicios import MOTIVOS_ESCALADA

        return MOTIVOS_ESCALADA.get(self.motivo_escalada, self.motivo_escalada)


class MensajeWhatsApp(models.Model):
    """Cada mensaje que entra o sale. Es el rastro: qué se le preguntó a quién, cuándo, y
    qué contestó. `wa_id` es único para que un reintento del webhook de Meta (que reenvía
    el mismo mensaje si no recibe 200 a tiempo) no se procese dos veces."""

    class Direccion(models.TextChoices):
        ENTRADA = 'entrada', 'Entrada'
        SALIDA = 'salida', 'Salida'

    direccion = models.CharField(max_length=8, choices=Direccion.choices)
    telefono = models.CharField(max_length=20, db_index=True)
    farmacia = models.ForeignKey(Farmacia, on_delete=models.SET_NULL, null=True, blank=True,
                                 related_name='mensajes_whatsapp')
    novedad = models.ForeignKey(Novedad, on_delete=models.SET_NULL, null=True, blank=True,
                                related_name='mensajes')
    wa_id = models.CharField(max_length=100, unique=True, null=True, blank=True)
    contenido = models.JSONField(default=dict)
    procesado = models.BooleanField(default=False)
    error = models.CharField(max_length=300, blank=True)
    creado_en = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        db_table = 'mensaje_whatsapp'
        ordering = ['-creado_en']
        verbose_name = 'Mensaje de WhatsApp'
        verbose_name_plural = 'Mensajes de WhatsApp'

    def __str__(self):
        return f'{self.get_direccion_display()} {self.telefono} {self.creado_en:%d/%m %H:%M}'


class ConversacionWhatsApp(models.Model):
    """En qué paso del menú está cada número cuando la farmacia escribe por su cuenta
    (eligió "un equipo no funciona" y falta que diga cuál, por ejemplo)."""

    telefono = models.CharField(max_length=20, unique=True)
    farmacia = models.ForeignKey(Farmacia, on_delete=models.CASCADE, related_name='conversaciones_whatsapp')
    paso = models.CharField(max_length=30, blank=True)
    datos = models.JSONField(default=dict)
    actualizado_en = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'conversacion_whatsapp'
        verbose_name = 'Conversación de WhatsApp'
        verbose_name_plural = 'Conversaciones de WhatsApp'

    def __str__(self):
        return f'{self.telefono} ({self.paso or "inicio"})'
