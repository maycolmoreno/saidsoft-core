from django.db import models

from apps.catalogo.models import Estacion


class ActividadMensualEstacion(models.Model):
    """Registra que una estación tuvo al menos un heartbeat en un mes calendario dado.

    Es la fuente de verdad para "facturación por endpoint" (contar estaciones activas
    por unidad de negocio por período). No se puede reusar `Estacion.ultimo_heartbeat`
    (se sobrescribe, no queda histórico) ni `apps.monitoreo.MuestraMetrica` (se purga a
    los 30 días — ver `apps.monitoreo.services.purgar_metricas_antiguas`): esta tabla es
    la única que se conserva indefinidamente para poder facturar meses pasados.

    Una fila por estación por mes: `apps.facturacion.services.registrar_actividad_mensual`
    hace get_or_create en cada heartbeat, así que el primero del mes la crea y los
    siguientes no la tocan.
    """
    estacion = models.ForeignKey(Estacion, on_delete=models.PROTECT, related_name='actividad_mensual')
    anio = models.PositiveSmallIntegerField()
    mes = models.PositiveSmallIntegerField()
    primer_heartbeat_en = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'actividad_mensual_estacion'
        unique_together = [('estacion', 'anio', 'mes')]
        ordering = ['-anio', '-mes', 'estacion__codigo']
        verbose_name = 'Actividad mensual de estación'
        verbose_name_plural = 'Actividad mensual de estaciones'

    def __str__(self):
        return f'{self.estacion.codigo} {self.anio}-{self.mes:02d}'
