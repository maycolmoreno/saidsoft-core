from django.conf import settings
from django.db import models


class EventoAuditoria(models.Model):
    """Registro inmutable de acciones administrativas (quién hizo qué, cuándo).

    Distinto de EventoDespliegue (apps.despliegues), que audita el ciclo de vida
    técnico de un despliegue en cada estación. Este modelo audita acciones humanas
    sobre el panel: alta/baja de catálogos, creación/aprobación de despliegues, etc.
    """

    usuario = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='eventos_auditoria',
    )
    accion = models.CharField(max_length=100, help_text='Ej. despliegue.crear, despliegue.aprobar, farmacia.editar')
    modelo = models.CharField(max_length=100, blank=True, help_text='Etiqueta del modelo afectado')
    objeto_id = models.CharField(max_length=50, blank=True)
    objeto_repr = models.CharField(max_length=255, blank=True, help_text='str() del objeto al momento del evento')
    detalle = models.JSONField(default=dict, blank=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    timestamp = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'evento_auditoria'
        ordering = ['-timestamp']
        verbose_name = 'Evento de auditoría'
        verbose_name_plural = 'Eventos de auditoría'

    def __str__(self):
        quien = self.usuario or 'sistema'
        return f'[{self.timestamp:%Y-%m-%d %H:%M:%S}] {quien} — {self.accion}'

    def delete(self, *args, **kwargs):
        raise NotImplementedError('EventoAuditoria es inmutable: no se puede eliminar.')


def registrar_evento(*, usuario, accion, objeto=None, detalle=None, request=None):
    """Helper central para dejar constancia de una acción administrativa.

    Uso: registrar_evento(usuario=request.user, accion='despliegue.aprobar', objeto=despliegue)
    """
    ip = None
    if request is not None:
        ip = request.META.get('REMOTE_ADDR')

    EventoAuditoria.objects.create(
        usuario=usuario,
        accion=accion,
        modelo=objeto._meta.label if objeto is not None else '',
        objeto_id=str(objeto.pk) if objeto is not None else '',
        objeto_repr=str(objeto) if objeto is not None else '',
        detalle=detalle or {},
        ip_address=ip,
    )
