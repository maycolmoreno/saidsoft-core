import uuid

from django.conf import settings
from django.db import models

from apps.catalogo.models import UnidadNegocio


class CategoriaEquipo(models.Model):
    """Categoría de equipo para fines de mantenimiento (checklist aplicable, frecuencia, etc.)."""
    codigo = models.CharField(max_length=30, unique=True)
    nombre = models.CharField(max_length=100)

    class Meta:
        db_table = 'categoria_equipo'
        ordering = ['nombre']
        verbose_name_plural = 'Categorías de equipo'

    def __str__(self):
        return self.nombre


class Marca(models.Model):
    nombre = models.CharField(max_length=100, unique=True)

    class Meta:
        db_table = 'marca'
        ordering = ['nombre']
        verbose_name_plural = 'Marcas'

    def __str__(self):
        return self.nombre


class Departamento(models.Model):
    class Tipo(models.TextChoices):
        ADMINISTRATIVO = 'administrativo', 'Administrativo'
        OPERATIVO = 'operativo', 'Operativo'
        TECNICO = 'tecnico', 'Técnico'

    nombre = models.CharField(max_length=100, unique=True)
    tipo = models.CharField(max_length=20, choices=Tipo.choices, default=Tipo.ADMINISTRATIVO)
    activo = models.BooleanField(default=True)

    class Meta:
        db_table = 'departamento'
        ordering = ['nombre']

    def __str__(self):
        return self.nombre


class Cargo(models.Model):
    nombre = models.CharField(max_length=100)
    departamento = models.ForeignKey(Departamento, on_delete=models.PROTECT, related_name='cargos')
    activo = models.BooleanField(default=True)

    class Meta:
        db_table = 'cargo'
        ordering = ['nombre']
        unique_together = [('nombre', 'departamento')]

    def __str__(self):
        return f'{self.nombre} ({self.departamento.nombre})'


class Ubicacion(models.Model):
    """Agencia/local físico: usada por mantenimiento (visita técnica) y como sede de colaboradores."""
    nombre = models.CharField(max_length=150)
    agencia = models.CharField(max_length=100, blank=True)
    latitud = models.DecimalField(max_digits=10, decimal_places=7, null=True, blank=True)
    longitud = models.DecimalField(max_digits=10, decimal_places=7, null=True, blank=True)
    direccion = models.CharField(max_length=200, blank=True)
    ciudad = models.CharField(max_length=100, blank=True)
    parroquia = models.CharField(max_length=100, blank=True)
    provincia = models.CharField(max_length=100, blank=True)
    departamento = models.ForeignKey(
        Departamento, on_delete=models.SET_NULL, null=True, blank=True, related_name='ubicaciones',
    )
    encargado = models.ForeignKey(
        'Colaborador', on_delete=models.SET_NULL, null=True, blank=True, related_name='ubicaciones_a_cargo',
    )
    activo = models.BooleanField(default=True)

    class Meta:
        db_table = 'ubicacion'
        ordering = ['nombre']
        verbose_name_plural = 'Ubicaciones'

    def __str__(self):
        return self.nombre


class Empresa(models.Model):
    """Cliente/empresa atendida. Catálogo simple, sin aislamiento multi-tenant de datos."""
    nombre = models.CharField(max_length=150)
    ruc = models.CharField(max_length=13, unique=True)
    direccion = models.CharField(max_length=200, blank=True)
    telefono = models.CharField(max_length=15, blank=True)
    correo = models.EmailField(blank=True)
    activo = models.BooleanField(default=True)

    class Meta:
        db_table = 'empresa'
        ordering = ['nombre']
        verbose_name_plural = 'Empresas'

    def __str__(self):
        return self.nombre


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
    """Receptor de activos y custodio de equipos (fusiona el Custodio de InvTICS).

    Integración con RRHH prevista a futuro: `origen_sync`/`sincronizado_en` y
    `cargo_directorio`/`departamento_directorio` (texto libre del sistema
    externo) ya existen para cuando se conecte un sync real, pero hoy nada
    los llena todavía.
    """
    nombre = models.CharField(max_length=150)
    cedula = models.CharField(max_length=20, unique=True)
    correo = models.EmailField(blank=True)
    telefono = models.CharField(max_length=15, blank=True)
    cargo = models.ForeignKey(
        Cargo, on_delete=models.PROTECT, null=True, blank=True, related_name='colaboradores',
    )
    ubicacion = models.ForeignKey(
        Ubicacion, on_delete=models.SET_NULL, null=True, blank=True, related_name='colaboradores',
        help_text='Agencia/local donde trabaja, usada para agrupar la visita técnica de mantenimiento.',
    )
    unidad_negocio = models.ForeignKey(
        UnidadNegocio, on_delete=models.PROTECT, null=True, blank=True, related_name='colaboradores',
    )
    sucursal = models.CharField(max_length=100, blank=True)
    zona = models.CharField(max_length=100, blank=True)
    fecha_ingreso = models.DateField(null=True, blank=True)
    origen_sync = models.BooleanField(
        default=False, help_text='True si este registro vino de una sincronización con RRHH (aún sin implementar).',
    )
    sincronizado_en = models.DateTimeField(null=True, blank=True)
    cargo_directorio = models.CharField(max_length=100, blank=True, help_text='Cargo tal como lo reporta RRHH.')
    departamento_directorio = models.CharField(
        max_length=100, blank=True, help_text='Departamento tal como lo reporta RRHH.',
    )
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
        BORRADOR = 'borrador', 'Borrador'
        EMITIDA = 'emitida', 'Emitida'
        RECEPCION_PARCIAL = 'recepcion_parcial', 'Recepción parcial'
        RECIBIDA = 'recibida', 'Recibida'
        CANCELADA = 'cancelada', 'Cancelada'

    numero_oc = models.CharField(max_length=30, unique=True, verbose_name='N° Orden de Compra')
    proveedor = models.CharField(max_length=150)
    fecha_emision = models.DateField()
    bodegas_destino = models.ManyToManyField(Bodega, related_name='ordenes_compra', blank=True)
    novedad_recepcion = models.TextField(blank=True, help_text='Faltantes, daños o diferencias frente a la OC.')
    estado = models.CharField(max_length=20, choices=Estado.choices, default=Estado.EMITIDA)
    recibido_por = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True,
        related_name='ordenes_recibidas',
    )
    fecha_creacion = models.DateTimeField(auto_now_add=True)
    version = models.PositiveIntegerField(
        default=0, help_text='Control de concurrencia optimista (equivalente a @Version en InvTICS).',
    )

    class Meta:
        db_table = 'orden_compra'
        ordering = ['-fecha_creacion']
        verbose_name = 'Orden de compra'
        verbose_name_plural = 'Órdenes de compra'

    def __str__(self):
        return f'OC {self.numero_oc} - {self.proveedor}'


class OrdenCompraDetalle(models.Model):
    """Línea de una orden de compra: un ítem (activo o consumible) con su cantidad solicitada."""

    class TipoItem(models.TextChoices):
        ACTIVO = 'activo', 'Activo'
        CONSUMIBLE = 'consumible', 'Consumible'

    class Estado(models.TextChoices):
        PENDIENTE = 'pendiente', 'Pendiente'
        PARCIAL = 'parcial', 'Recepción parcial'
        COMPLETO = 'completo', 'Completo'
        CANCELADO = 'cancelado', 'Cancelado'

    orden_compra = models.ForeignKey(OrdenCompra, on_delete=models.CASCADE, related_name='detalles')
    tipo_item = models.CharField(max_length=10, choices=TipoItem.choices)
    descripcion = models.CharField(max_length=200, blank=True)
    modelo = models.CharField(max_length=100, blank=True)
    categoria = models.ForeignKey(
        CategoriaEquipo, on_delete=models.PROTECT, null=True, blank=True, related_name='lineas_orden_compra',
    )
    marca = models.ForeignKey(
        Marca, on_delete=models.PROTECT, null=True, blank=True, related_name='lineas_orden_compra',
    )
    tipo_consumible = models.ForeignKey(
        TipoConsumible, on_delete=models.PROTECT, null=True, blank=True, related_name='lineas_orden_compra',
    )
    cantidad_solicitada = models.PositiveIntegerField()
    cantidad_recibida = models.PositiveIntegerField(default=0)
    precio_unitario = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    unidad_medida = models.CharField(max_length=30, blank=True)
    estado = models.CharField(max_length=15, choices=Estado.choices, default=Estado.PENDIENTE)
    version = models.PositiveIntegerField(
        default=0, help_text='Control de concurrencia optimista para recepciones simultáneas de la misma línea.',
    )

    class Meta:
        db_table = 'orden_compra_detalle'
        ordering = ['orden_compra', 'id']
        verbose_name = 'Línea de orden de compra'
        verbose_name_plural = 'Líneas de orden de compra'

    def __str__(self):
        item = self.tipo_consumible.nombre if self.tipo_consumible else (self.descripcion or self.modelo)
        return f'{self.orden_compra.numero_oc} - {item} ({self.cantidad_recibida}/{self.cantidad_solicitada})'


class RecepcionLote(models.Model):
    """Recepción física de un lote contra una línea de OC (una línea puede recibirse en varios lotes)."""

    class Estado(models.TextChoices):
        REGISTRADO = 'registrado', 'Registrado'
        APLICADO = 'aplicado', 'Aplicado'
        ANULADO = 'anulado', 'Anulado'

    orden_compra = models.ForeignKey(OrdenCompra, on_delete=models.PROTECT, related_name='recepciones')
    orden_compra_detalle = models.ForeignKey(
        OrdenCompraDetalle, on_delete=models.PROTECT, related_name='recepciones',
    )
    uuid = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    numero_lote = models.CharField(max_length=50, blank=True)
    tipo_item = models.CharField(max_length=10, choices=OrdenCompraDetalle.TipoItem.choices)
    cantidad_recibida = models.PositiveIntegerField()
    bodega_destino = models.ForeignKey(Bodega, on_delete=models.PROTECT, related_name='recepciones')
    custodio_receptor = models.ForeignKey(
        Colaborador, on_delete=models.SET_NULL, null=True, blank=True, related_name='recepciones',
    )
    estado = models.CharField(max_length=15, choices=Estado.choices, default=Estado.APLICADO)
    recepcionado_por = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='recepciones',
    )
    fecha_recepcion = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'recepcion_lote'
        ordering = ['-fecha_recepcion']
        verbose_name = 'Recepción de lote'
        verbose_name_plural = 'Recepciones de lote'

    def __str__(self):
        return f'Lote {self.numero_lote or self.uuid} - {self.orden_compra.numero_oc}'


class MovimientoInventario(models.Model):
    """Kardex de stock de consumibles: ingresos por compra, traslados entre bodegas y ajustes.

    No incluye movimientos de un Activo individual: esos ya quedan registrados
    en EventoActivo (ingreso/asignación/devolución/baja) y no se duplican aquí.
    """

    class TipoMovimiento(models.TextChoices):
        INGRESO_CONSUMIBLE = 'ingreso_consumible', 'Ingreso de consumible (compra)'
        TRASLADO = 'traslado', 'Traslado entre bodegas'
        AJUSTE = 'ajuste', 'Ajuste de inventario'

    tipo_movimiento = models.CharField(max_length=20, choices=TipoMovimiento.choices)
    tipo_consumible = models.ForeignKey(
        TipoConsumible, on_delete=models.PROTECT, null=True, blank=True, related_name='movimientos',
    )
    cantidad = models.IntegerField(help_text='Positivo en ingresos/traslados de entrada; negativo en ajustes a la baja.')
    bodega_origen = models.ForeignKey(
        Bodega, on_delete=models.PROTECT, null=True, blank=True, related_name='movimientos_salida',
    )
    bodega_destino = models.ForeignKey(
        Bodega, on_delete=models.PROTECT, null=True, blank=True, related_name='movimientos_entrada',
    )
    orden_compra = models.ForeignKey(
        OrdenCompra, on_delete=models.SET_NULL, null=True, blank=True, related_name='movimientos',
    )
    recepcion_lote = models.ForeignKey(
        RecepcionLote, on_delete=models.SET_NULL, null=True, blank=True, related_name='movimientos',
    )
    motivo = models.CharField(max_length=200, blank=True)
    realizado_por = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='movimientos_inventario',
    )
    fecha_efectiva = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'movimiento_inventario'
        ordering = ['-fecha_efectiva']
        verbose_name = 'Movimiento de inventario'
        verbose_name_plural = 'Movimientos de inventario'

    def __str__(self):
        return f'{self.get_tipo_movimiento_display()} - {self.tipo_consumible} ({self.cantidad})'


class MotivoBaja(models.TextChoices):
    DESTRUCCION = 'destruccion', 'Destrucción'
    OBSOLESCENCIA = 'obsolescencia', 'Obsolescencia'
    ROBO_PERDIDA = 'robo_perdida', 'Robo o pérdida'
    DONACION = 'donacion', 'Donación'


class MotivoReparacion(models.TextChoices):
    FALLA_TECNICA = 'falla_tecnica', 'Falla técnica'
    DANO_FISICO = 'dano_fisico', 'Daño físico'


class CondicionAlRecibir(models.TextChoices):
    NUEVO = 'nuevo', 'Nuevo'
    USADO = 'usado', 'Usado'
    REACONDICIONADO = 'reacondicionado', 'Reacondicionado'


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
    marca = models.ForeignKey(
        Marca, on_delete=models.PROTECT, null=True, blank=True, related_name='activos',
    )
    categoria = models.ForeignKey(
        CategoriaEquipo, on_delete=models.PROTECT, null=True, blank=True, related_name='activos',
    )
    modelo = models.CharField(max_length=100, blank=True)
    numero_serie = models.CharField(max_length=100, blank=True)
    procesador = models.CharField(max_length=100, blank=True)
    ram_gb = models.PositiveSmallIntegerField(null=True, blank=True, verbose_name='RAM (GB)')
    almacenamiento_gb = models.PositiveIntegerField(null=True, blank=True, verbose_name='Almacenamiento (GB)')
    codigo_sap = models.CharField(max_length=30, blank=True, verbose_name='Código SAP')
    condicion_al_recibir = models.CharField(max_length=20, choices=CondicionAlRecibir.choices, blank=True)
    baja_recomendada = models.BooleanField(
        default=False,
        help_text='Un mantenimiento marcó este activo para posible baja; un humano debe decidir con registrar_baja.',
    )
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
    unidad_negocio = models.ForeignKey(
        UnidadNegocio, on_delete=models.PROTECT, null=True, blank=True, related_name='activos',
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
        BAJA_RECOMENDADA = 'baja_recomendada', 'Baja recomendada (mantenimiento)'
        TRANSITO = 'transito', 'En tránsito entre bodegas'

    activo = models.ForeignKey(Activo, on_delete=models.CASCADE, related_name='eventos')
    tipo_evento = models.CharField(max_length=25, choices=TipoEvento.choices)
    usuario = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True)
    detalle = models.JSONField(default=dict, blank=True)
    timestamp = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'evento_activo'
        # 'pk' como desempate: timestamp (auto_now_add) puede repetirse entre dos eventos
        # del mismo activo creados en el mismo tick de reloj, dejando .first()/.last()
        # no determinísticos sin él (ver commit que agregó EventoSyncExterno).
        ordering = ['activo', 'timestamp', 'pk']

    def __str__(self):
        return f'{self.activo.codigo} - {self.get_tipo_evento_display()} @ {self.timestamp:%Y-%m-%d %H:%M}'

    def delete(self, *args, **kwargs):
        raise NotImplementedError('EventoActivo es inmutable: no se puede eliminar.')


class SyncEjecucion(models.Model):
    """Cabecera de una corrida de sincronización de colaboradores con RRHH externo.

    Solo el esquema de auditoría: no hay conector real todavía (no hay un
    sistema de RRHH definido), así que hoy nada crea filas aquí. Cuando
    exista un origen real, el management command que lo ejecute debe crear
    una fila de este modelo por corrida y una SyncCambio por cada
    diferencia detectada.
    """

    class Origen(models.TextChoices):
        MANUAL = 'manual', 'Manual'
        URL = 'url', 'URL (API externa)'
        ARCHIVO = 'archivo', 'Archivo (CSV/Excel)'

    origen = models.CharField(max_length=10, choices=Origen.choices)
    ejecutado_por = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='syncs_ejecutados',
    )
    creados = models.PositiveIntegerField(default=0)
    actualizados = models.PositiveIntegerField(default=0)
    inactivados = models.PositiveIntegerField(default=0)
    reactivados = models.PositiveIntegerField(default=0)
    sin_cambios = models.PositiveIntegerField(default=0)
    advertencias = models.PositiveIntegerField(default=0)
    ejecutado_en = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'sync_ejecucion'
        ordering = ['-ejecutado_en']
        verbose_name = 'Ejecución de sync RRHH'
        verbose_name_plural = 'Ejecuciones de sync RRHH'

    def __str__(self):
        return f'Sync {self.get_origen_display()} @ {self.ejecutado_en:%Y-%m-%d %H:%M}'


class SyncCambio(models.Model):
    """Detalle de un cambio detectado (o una advertencia) dentro de una SyncEjecucion."""

    class Tipo(models.TextChoices):
        CREADO = 'creado', 'Creado'
        ACTUALIZADO = 'actualizado', 'Actualizado'
        INACTIVADO = 'inactivado', 'Inactivado'
        REACTIVADO = 'reactivado', 'Reactivado'
        ADVERTENCIA = 'advertencia', 'Advertencia'
        ALERTA_ACTIVOS = 'alerta_activos', 'Alerta: tiene activos asignados'

    ejecucion = models.ForeignKey(SyncEjecucion, on_delete=models.CASCADE, related_name='cambios')
    tipo = models.CharField(max_length=20, choices=Tipo.choices)
    cedula = models.CharField(max_length=20)
    colaborador = models.ForeignKey(
        Colaborador, on_delete=models.SET_NULL, null=True, blank=True, related_name='cambios_sync',
    )
    detalle = models.TextField(blank=True)

    class Meta:
        db_table = 'sync_cambio'
        ordering = ['ejecucion', 'cedula']

    def __str__(self):
        return f'{self.get_tipo_display()} - {self.cedula}'
