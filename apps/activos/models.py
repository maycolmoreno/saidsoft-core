from django.conf import settings
from django.db import models


class Bodega(models.Model):
    codigo = models.CharField(max_length=20, unique=True)
    nombre = models.CharField(max_length=100, blank=True)
    custodio = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='bodegas_custodiadas',
    )
    ubicacion = models.CharField(max_length=150, blank=True)
    activa = models.BooleanField(default=True)

    class Meta:
        db_table = 'bodega'
        ordering = ['codigo']
        verbose_name_plural = 'Bodegas'

    def __str__(self):
        return self.codigo


class Colaborador(models.Model):
    """Receptor de activos. Carga manual por ahora; integración con RRHH prevista a futuro."""
    nombre = models.CharField(max_length=150)
    cedula = models.CharField(max_length=20, unique=True)
    cargo = models.CharField(max_length=100, blank=True)
    sucursal = models.CharField(max_length=100, blank=True)
    zona = models.CharField(max_length=100, blank=True)
    activo = models.BooleanField(default=True, help_text='Colaborador vigente en la empresa.')
    fecha_registro = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'colaborador'
        ordering = ['nombre']

    def __str__(self):
        return f'{self.nombre} ({self.cedula})'


class TipoConsumible(models.Model):
    codigo = models.CharField(max_length=30, unique=True, help_text='Ej. MOUSE, TECLADO, TONER-HP26A')
    nombre = models.CharField(max_length=100)

    class Meta:
        db_table = 'tipo_consumible'
        ordering = ['nombre']

    def __str__(self):
        return self.nombre


class StockBodega(models.Model):
    bodega = models.ForeignKey(Bodega, on_delete=models.CASCADE, related_name='stock')
    tipo_consumible = models.ForeignKey(TipoConsumible, on_delete=models.PROTECT, related_name='stock')
    cantidad = models.PositiveIntegerField(default=0)

    class Meta:
        db_table = 'stock_bodega'
        unique_together = [('bodega', 'tipo_consumible')]
        ordering = ['bodega', 'tipo_consumible']

    def __str__(self):
        return f'{self.bodega.codigo} - {self.tipo_consumible.nombre}: {self.cantidad}'


class OrdenCompra(models.Model):
    class Estado(models.TextChoices):
        PENDIENTE = 'pendiente', 'Pendiente de recepción'
        RECIBIDA = 'recibida', 'Recibida'

    numero_oc = models.CharField(max_length=30, unique=True, verbose_name='N° Orden de Compra')
    proveedor = models.CharField(max_length=150)
    fecha_emision = models.DateField()
    bodegas_destino = models.ManyToManyField(Bodega, related_name='ordenes_compra', blank=True)
    novedad_recepcion = models.TextField(blank=True, help_text='Faltantes, daños o diferencias frente a la OC.')
    estado = models.CharField(max_length=15, choices=Estado.choices, default=Estado.PENDIENTE)
    recibido_por = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True,
        related_name='ordenes_recibidas',
    )
    fecha_creacion = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'orden_compra'
        ordering = ['-fecha_creacion']
        verbose_name = 'Orden de compra'
        verbose_name_plural = 'Órdenes de compra'

    def __str__(self):
        return f'OC {self.numero_oc} - {self.proveedor}'


class MotivoBaja(models.TextChoices):
    DESTRUCCION = 'destruccion', 'Destrucción'
    OBSOLESCENCIA = 'obsolescencia', 'Obsolescencia'
    ROBO_PERDIDA = 'robo_perdida', 'Robo o pérdida'
    DONACION = 'donacion', 'Donación'


class MotivoReparacion(models.TextChoices):
    FALLA_TECNICA = 'falla_tecnica', 'Falla técnica'
    DANO_FISICO = 'dano_fisico', 'Daño físico'


class Activo(models.Model):
    """Un activo de información nunca se elimina: su historial es de auditoría permanente."""

    class Tipo(models.TextChoices):
        LAPTOP = 'LAP', 'Laptop'
        DESKTOP = 'DSK', 'Desktop'
        IMPRESORA = 'IMP', 'Impresora'
        SERVIDOR = 'SRV', 'Servidor'
        RED = 'NET', 'Red (switch/router)'
        TABLET = 'TAB', 'Tablet'
        UPS = 'UPS', 'UPS'
        TELEFONO = 'TEL', 'Teléfono IP'
        CAMARA = 'CAM', 'Cámara/CCTV'

    class Estado(models.TextChoices):
        EN_BODEGA = 'en_bodega', 'En bodega'
        ASIGNADO = 'asignado', 'Asignado'
        EN_REPARACION = 'en_reparacion', 'En reparación'
        EN_TRANSITO = 'en_transito', 'En tránsito'
        DADO_DE_BAJA = 'dado_de_baja', 'Dado de baja'

    class EstadoFisico(models.TextChoices):
        NUEVO = 'nuevo', 'Nuevo'
        BUENO = 'bueno', 'Bueno'
        REGULAR = 'regular', 'Regular'
        MALO = 'malo', 'Malo'

    codigo = models.CharField(max_length=20, unique=True, editable=False)
    tipo = models.CharField(max_length=3, choices=Tipo.choices)
    marca = models.CharField(max_length=100, blank=True)
    modelo = models.CharField(max_length=100, blank=True)
    numero_serie = models.CharField(max_length=100, blank=True)
    fecha_compra = models.DateField(null=True, blank=True)
    vencimiento_garantia = models.DateField(null=True, blank=True)
    orden_compra = models.ForeignKey(
        OrdenCompra, on_delete=models.PROTECT, null=True, blank=True, related_name='activos',
    )

    bodega_actual = models.ForeignKey(
        Bodega, on_delete=models.PROTECT, null=True, blank=True, related_name='activos',
        help_text='Bodega donde está o desde donde salió (última ubicación conocida).',
    )
    colaborador_actual = models.ForeignKey(
        Colaborador, on_delete=models.SET_NULL, null=True, blank=True, related_name='activos_asignados',
    )
    estado = models.CharField(max_length=15, choices=Estado.choices, default=Estado.EN_BODEGA)
    estado_fisico_actual = models.CharField(max_length=10, choices=EstadoFisico.choices, blank=True)

    fecha_creacion = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'activo'
        ordering = ['codigo']

    def __str__(self):
        return self.codigo

    def delete(self, *args, **kwargs):
        raise NotImplementedError('Un activo nunca se elimina; usa el estado "Dado de baja".')


class EventoActivo(models.Model):
    """Historial inmutable de un activo: ingreso, asignación, devolución, reparación, baja."""

    class TipoEvento(models.TextChoices):
        INGRESO = 'ingreso', 'Ingreso a bodega'
        ASIGNACION = 'asignacion', 'Asignación a colaborador'
        CONSUMIBLE_ENTREGADO = 'consumible_entregado', 'Consumible entregado'
        DEVOLUCION = 'devolucion', 'Devolución de colaborador'
        ENVIO_REPARACION = 'envio_reparacion', 'Enviado a reparación'
        RETORNO_REPARACION = 'retorno_reparacion', 'Retorno de reparación'
        BAJA = 'baja', 'Dado de baja'
        TRANSITO = 'transito', 'En tránsito entre bodegas'

    activo = models.ForeignKey(Activo, on_delete=models.CASCADE, related_name='eventos')
    tipo_evento = models.CharField(max_length=25, choices=TipoEvento.choices)
    usuario = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True)
    detalle = models.JSONField(default=dict, blank=True)
    timestamp = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'evento_activo'
        ordering = ['activo', 'timestamp']

    def __str__(self):
        return f'{self.activo.codigo} - {self.get_tipo_evento_display()} @ {self.timestamp:%Y-%m-%d %H:%M}'

    def delete(self, *args, **kwargs):
        raise NotImplementedError('EventoActivo es inmutable: no se puede eliminar.')
