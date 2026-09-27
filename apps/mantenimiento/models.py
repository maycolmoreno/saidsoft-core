from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.db import models
from django.utils import timezone

from apps.activos.models import Activo, Bodega, CategoriaEquipo, Colaborador, TipoConsumible, Ubicacion
from apps.catalogo.models import UnidadNegocio

# Radio dentro del cual se considera que el técnico estuvo EN la farmacia. 200 m
# cubre el error típico del GPS en zona urbana (10-50 m, peor bajo techo) más el
# tamaño del local y su estacionamiento, sin llegar a abarcar la manzana entera.
RADIO_VERIFICACION_METROS = 200


class TipoOrigenMantenimiento(models.TextChoices):
    """Portado tal cual de TipoOrigenMantenimiento.java (InvTICS)."""
    ODOO_HELPDESK = 'odoo_helpdesk', 'Odoo Helpdesk'
    PROGRAMADO = 'programado', 'Programado'
    MANUAL = 'manual', 'Manual'
    # Abierto solo por una alerta de monitoreo (ver
    # apps.mantenimiento.services.abrir_mantenimiento_desde_alerta): nadie lo cargó a
    # mano, lo disparó el RMM al detectar la falla.
    MONITOREO = 'monitoreo', 'Monitoreo (automático)'


class ResultadoTecnico(models.TextChoices):
    """Portado tal cual de ResultadoTecnico.java (InvTICS), 12 valores."""
    REPARADO = 'reparado', 'Reparado'
    SIN_FALLA = 'sin_falla', 'Sin falla encontrada'
    SIN_INTERVENCION = 'sin_intervencion', 'Sin intervención'
    PARCIALMENTE_REPARADO = 'parcialmente_reparado', 'Parcialmente reparado'
    REQUIERE_REPUESTO = 'requiere_repuesto', 'Requiere repuesto'
    ESCALADO_A_PROVEEDOR = 'escalado_a_proveedor', 'Escalado a proveedor'
    IRREPARABLE = 'irreparable', 'Irreparable'
    REQUIERE_BAJA = 'requiere_baja', 'Requiere baja'
    GARANTIA_APLICADA = 'garantia_aplicada', 'Garantía aplicada'
    GARANTIA_RECHAZADA = 'garantia_rechazada', 'Garantía rechazada'
    ACTUALIZADO = 'actualizado', 'Actualizado'
    INSTALADO = 'instalado', 'Instalado'


class EstadoGeneralEquipo(models.TextChoices):
    """Condición del equipo al momento del mantenimiento (portado de InvTICS, ausente hasta ahora)."""
    OPERATIVO = 'operativo', 'Operativo'
    REQUIERE_REVISION = 'requiere_revision', 'Requiere revisión'
    NO_OPERATIVO = 'no_operativo', 'No operativo'



class PrioridadMantenimiento(models.TextChoices):
    """Prioridad de un Mantenimiento, distinta de PrioridadActividad (que es de
    ActividadPlanificada y viene portada de InvTICS con otros valores).

    Se agrega CRITICA porque acá el rango va de "un POS caído en una farmacia que
    está vendiendo" a "un preventivo de rutina", y tratarlos con el mismo umbral era
    justamente la debilidad que esto viene a corregir.
    """
    CRITICA = 'critica', 'Crítica'
    ALTA = 'alta', 'Alta'
    NORMAL = 'normal', 'Normal'
    BAJA = 'baja', 'Baja'

class MantenimientoProgramado(models.Model):
    """Plantilla recurrente: cada `frecuencia_dias` genera un Mantenimiento nuevo."""
    equipo = models.ForeignKey(Activo, on_delete=models.PROTECT, related_name='mantenimientos_programados')
    tecnico = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='mantenimientos_programados_asignados',
    )
    frecuencia_dias = models.PositiveIntegerField()
    fecha_ultimo = models.DateField(null=True, blank=True)
    fecha_proximo = models.DateField()
    activo = models.BooleanField(default=True)
    observaciones = models.TextField(blank=True)

    class Meta:
        db_table = 'mantenimiento_programado'
        ordering = ['fecha_proximo']
        verbose_name = 'Mantenimiento programado'
        verbose_name_plural = 'Mantenimientos programados'

    def __str__(self):
        return f'{self.equipo.codigo} cada {self.frecuencia_dias} días'


class TipoMantenimiento(models.Model):
    """Catálogo configurable (administrable desde /admin/, no hardcodeado en Python).

    Reemplaza al CharField de texto libre que tenía Mantenimiento.tipo_mantenimiento --
    sin un catálogo, cualquiera escribía lo que quisiera ("preventivo", "Preventivo",
    "PREV"...) y era imposible reportar por tipo de forma confiable
    (docs/proceso-mantenimiento-ti.md, brecha #3, 23-ago-2026). `codigo` identifica los
    5 tipos conceptuales de la propuesta (preventivo/correctivo/falla_critica/
    actualizacion/obsolescencia) para que el código pueda asignarlos por default sin
    depender del texto de `nombre`, que sí es editable libremente por un administrador.
    """
    codigo = models.CharField(max_length=30, unique=True)
    nombre = models.CharField(max_length=100)
    descripcion = models.TextField(blank=True)
    activo = models.BooleanField(default=True)

    class Meta:
        db_table = 'tipo_mantenimiento'
        ordering = ['nombre']
        verbose_name = 'Tipo de mantenimiento'
        verbose_name_plural = 'Tipos de mantenimiento'

    def __str__(self):
        return self.nombre


class Mantenimiento(models.Model):
    """Entidad central: un mantenimiento manual, programado o venido de Odoo Helpdesk."""

    class EstadoInterno(models.TextChoices):
        PENDIENTE = 'pendiente', 'Pendiente'
        EN_PROCESO = 'en_proceso', 'En proceso'
        CERRADO = 'cerrado', 'Cerrado'
        CANCELADO = 'cancelado', 'Cancelado'

    cliente = models.ForeignKey(
        Colaborador, on_delete=models.PROTECT, null=True, blank=True, related_name='mantenimientos',
        help_text='Custodio/colaborador que reporta o recibe el mantenimiento.',
    )
    tecnico = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='mantenimientos_tecnico',
    )
    mantenimiento_programado = models.ForeignKey(
        MantenimientoProgramado, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='mantenimientos_generados',
    )
    descripcion = models.TextField(blank=True)
    tipo_mantenimiento = models.ForeignKey(
        TipoMantenimiento, on_delete=models.PROTECT, null=True, blank=True, related_name='mantenimientos',
    )
    tipo_origen = models.CharField(
        max_length=20, choices=TipoOrigenMantenimiento.choices, default=TipoOrigenMantenimiento.MANUAL,
    )
    prioridad = models.CharField(
        max_length=10, choices=PrioridadMantenimiento.choices, default=PrioridadMantenimiento.NORMAL,
        help_text='Define el SLA aplicable (ver AcuerdoNivelServicio).',
    )
    estado_interno = models.CharField(max_length=15, choices=EstadoInterno.choices, default=EstadoInterno.PENDIENTE)
    resultado_tecnico = models.CharField(max_length=25, choices=ResultadoTecnico.choices, blank=True)
    estado_general = models.CharField(max_length=20, choices=EstadoGeneralEquipo.choices, blank=True)
    snapshot_equipo = models.JSONField(
        default=dict, blank=True,
        help_text='Código/serie/modelo del equipo principal, congelados al crear el mantenimiento.',
    )
    fecha_programada = models.DateTimeField()
    fecha_cierre = models.DateTimeField(null=True, blank=True)
    cerrado_por = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='mantenimientos_cerrados',
    )
    fecha_creacion = models.DateTimeField(auto_now_add=True)
    # Visita de la que salió este mantenimiento, si nació durante una (ver
    # VisitaTecnica). Referencia perezosa por string: VisitaTecnica se define más
    # abajo en este mismo módulo.
    visita = models.ForeignKey(
        'mantenimiento.VisitaTecnica', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='mantenimientos_generados',
    )
    informe_pdf = models.FileField(upload_to='mantenimiento/informes/%Y/%m/', blank=True)
    informe_pdf_generado_en = models.DateTimeField(null=True, blank=True)
    tiempo_real_minutos = models.PositiveIntegerField(
        null=True, blank=True,
        help_text='Tiempo real de intervención, capturado al cerrar (no siempre coincide con '
                  'fecha_cierre - fecha_programada: el técnico puede pausar/retomar).',
    )
    # Distancia mínima entre el técnico y la farmacia del equipo durante la
    # intervención, calculada AL CERRAR (ver services._verificar_presencia_en_sitio).
    # Se persiste en vez de recalcularse después a propósito: es un hecho del momento
    # del cierre y debe quedar auditable aunque después se purguen las posiciones o
    # cambie el umbral. null = no se pudo verificar (sin GPS, sin coordenadas de la
    # farmacia, o el equipo no está asociado a una farmacia).
    distancia_verificacion_metros = models.FloatField(
        null=True, blank=True,
        help_text='Distancia mínima del técnico a la farmacia durante la intervención, en metros.',
    )

    class Meta:
        db_table = 'mantenimiento'
        ordering = ['-fecha_programada']
        permissions = [
            # Separa "registrar mi propio trabajo" de "repartir trabajo ajeno".
            # Sin este permiso, el campo `tecnico` de los formularios de
            # mantenimiento, actividad planificada y visita técnica queda fijo en
            # el usuario en sesión: un técnico no tiene por qué buscarse en una
            # lista de todos los usuarios activos, ni poder cargarle una visita a
            # un compañero. Vive en Mantenimiento y no en cada modelo porque es
            # una sola decisión de negocio, no tres.
            ('asignar_tecnico', 'Puede asignar trabajo a un técnico distinto de sí mismo'),
        ]

    def __str__(self):
        return f'Mantenimiento #{self.pk} ({self.get_estado_interno_display()})'

    @property
    def unidad_negocio(self):
        """Cliente al que pertenece, heredado de `cliente` (Colaborador). None si no
        tiene cliente asignado o el cliente no tiene unidad_negocio — se trata como
        "compartido", igual que en apps.cuentas.services."""
        return self.cliente.unidad_negocio if self.cliente_id else None

    @property
    def costo_total_repuestos(self):
        return sum((r.costo_total for r in self.repuestos_utilizados.all()), Decimal('0'))

    @property
    def presencia_en_sitio(self):
        """'verificada' | 'fuera_de_rango' | 'sin_datos'.

        Solo informa: que no se pueda verificar NO significa que el técnico no haya
        ido. Hay motivos legítimos (el técnico no usa la app móvil, la farmacia sin
        coordenadas, GPS sin señal dentro del local). Por eso el default es
        'sin_datos' y no algo acusatorio.
        """
        if self.distancia_verificacion_metros is None:
            return 'sin_datos'
        return 'verificada' if self.distancia_verificacion_metros <= RADIO_VERIFICACION_METROS else 'fuera_de_rango'

    # --- SLA -------------------------------------------------------------------
    # El reloj corre desde `fecha_programada` (ver docstring de AcuerdoNivelServicio).
    # Todas estas propiedades devuelven None si no hay SLA cargado para la prioridad:
    # sin acuerdo definido no se puede afirmar que algo esté incumplido.

    #: Acuerdos precargados por `services.precargar_acuerdos_sla`, para listados.
    #: None = todavía no se precargó y `sla` va a consultar la base.
    _acuerdos_precargados = None

    @property
    def sla(self):
        """El acuerdo de la prioridad de este mantenimiento.

        **Ojo con los listados.** Esto consulta la base en CADA acceso, y
        `estado_sla` lo toca varias veces por fila: medido el 26-sep-2026, pintar la
        columna de SLA costaba 2 consultas por fila -- ~3.600 en un listado de 1.800
        mantenimientos. Para recorrer muchos, pasar antes por
        `services.precargar_acuerdos_sla`, que los carga UNA vez y los deja acá.

        Se resuelve con un atributo y no cambiando la firma de la propiedad para que la
        pantalla de detalle (un objeto suelto) siga funcionando sin saber nada de esto.
        """
        if self._acuerdos_precargados is not None:
            return self._acuerdos_precargados.get(self.prioridad)
        return AcuerdoNivelServicio.objects.filter(prioridad=self.prioridad, activo=True).first()

    @property
    def limite_respuesta(self):
        sla = self.sla
        return self.fecha_programada + timedelta(hours=sla.horas_respuesta) if sla else None

    @property
    def limite_resolucion(self):
        sla = self.sla
        return self.fecha_programada + timedelta(hours=sla.horas_resolucion) if sla else None

    @property
    def inicio_real(self):
        """Cuándo se pasó a EN_PROCESO, según el evento inmutable — no hay campo
        propio para esto y EventoMantenimiento es la fuente de verdad."""
        evento = self.eventos.filter(tipo_evento=EventoMantenimiento.TipoEvento.INICIADO).order_by('timestamp').first()
        return evento.timestamp if evento else None

    @property
    def sla_respuesta_incumplido(self):
        """True si ya pasó el límite para atenderlo y todavía no se inició. Si se
        inició, se juzga contra el momento real de inicio (no contra "ahora"): un
        mantenimiento atendido a tiempo no pasa a incumplido por seguir abierto."""
        limite = self.limite_respuesta
        if limite is None or self.estado_interno == self.EstadoInterno.CANCELADO:
            return False
        inicio = self.inicio_real
        return (inicio or timezone.now()) > limite

    @property
    def sla_resolucion_incumplido(self):
        limite = self.limite_resolucion
        if limite is None or self.estado_interno == self.EstadoInterno.CANCELADO:
            return False
        cierre = self.fecha_cierre
        return (cierre or timezone.now()) > limite

    @property
    def estado_sla(self):
        """Etiqueta para el panel: 'sin_sla' | 'cumplido' | 'incumplido' | 'en_plazo' | 'por_vencer'.
        'por_vencer' = queda menos del 20% del tiempo de resolución.

        **Esta es LA definición de urgencia del sistema.** Hasta el 26-sep-2026 existía
        una segunda en Dart (`pantalla_mantenimientos.dart`), que era la que decidía el
        orden en la app mientras el panel ordenaba por fecha: la misma lista, dos
        órdenes, y nadie se enteraba. Ahora el orden sale de acá vía
        `services.orden_de_urgencia` y la app renderiza lo que recibe.
        """
        limite = self.limite_resolucion
        if limite is None or self.estado_interno == self.EstadoInterno.CANCELADO:
            return 'sin_sla'
        if self.estado_interno == self.EstadoInterno.CERRADO:
            # En línea y no vía `sla_resolucion_incumplido`: esa propiedad recalcula
            # `limite_resolucion`, que es la consulta cara que ya se hizo arriba.
            return 'incumplido' if (self.fecha_cierre or timezone.now()) > limite else 'cumplido'
        ahora = timezone.now()
        if ahora > limite:
            return 'incumplido'
        total = (limite - self.fecha_programada).total_seconds()
        restante = (limite - ahora).total_seconds()
        return 'por_vencer' if total > 0 and restante / total <= 0.2 else 'en_plazo'


class MantenimientoEquipo(models.Model):
    """N:M Mantenimiento<->Activo (un mantenimiento puede cubrir varios equipos a la vez).

    Reemplaza al `equipoId` directo + N:M duplicado que coexistían en
    MantenimientosJpa (deuda de una migración 1→N nunca limpiada en el
    original): aquí solo existe esta relación, y `es_principal` cubre el
    caso de mostrar "el" equipo cuando la UI necesita uno solo.
    """
    mantenimiento = models.ForeignKey(Mantenimiento, on_delete=models.CASCADE, related_name='equipos')
    equipo = models.ForeignKey(Activo, on_delete=models.PROTECT, related_name='mantenimientos')
    es_principal = models.BooleanField(default=False)

    class Meta:
        db_table = 'mantenimiento_equipo'
        unique_together = [('mantenimiento', 'equipo')]
        ordering = ['-es_principal', 'equipo__codigo']

    def __str__(self):
        return f'{self.mantenimiento_id} - {self.equipo.codigo}'


class ActividadChecklist(models.Model):
    """Catálogo de ítems de checklist, aplicables según la categoría del equipo."""
    nombre = models.CharField(max_length=200)
    orden = models.PositiveIntegerField(default=0)
    activo = models.BooleanField(default=True)
    categorias = models.ManyToManyField(CategoriaEquipo, related_name='items_checklist', blank=True)

    class Meta:
        db_table = 'actividad_checklist'
        ordering = ['orden', 'nombre']
        verbose_name = 'Ítem de checklist'
        verbose_name_plural = 'Ítems de checklist'

    def __str__(self):
        return self.nombre


class ActividadRealizada(models.Model):
    """Ejecución de un ítem del checklist para un mantenimiento concreto."""
    mantenimiento = models.ForeignKey(Mantenimiento, on_delete=models.CASCADE, related_name='actividades_realizadas')
    actividad = models.ForeignKey(ActividadChecklist, on_delete=models.PROTECT, related_name='realizaciones')
    realizada = models.BooleanField(default=False)

    class Meta:
        db_table = 'actividad_realizada'
        unique_together = [('mantenimiento', 'actividad')]
        ordering = ['actividad__orden']

    def __str__(self):
        return f'{self.actividad.nombre}: {"si" if self.realizada else "no"}'


class EventoMantenimiento(models.Model):
    """Historial inmutable de un mantenimiento: mismo patrón que EventoActivo/EventoDespliegue."""

    class TipoEvento(models.TextChoices):
        PROGRAMADO = 'programado', 'Programado'
        INICIADO = 'iniciado', 'Iniciado'
        CHECKLIST_ACTUALIZADO = 'checklist_actualizado', 'Checklist actualizado'
        FIRMADO = 'firmado', 'Firmado'
        IMAGEN_ADJUNTADA = 'imagen_adjuntada', 'Imagen adjuntada'
        REPUESTO_REGISTRADO = 'repuesto_registrado', 'Repuesto registrado'
        INFORME_GENERADO = 'informe_generado', 'Informe PDF generado'
        CERRADO = 'cerrado', 'Cerrado'
        CANCELADO = 'cancelado', 'Cancelado'

    # PROTECT y no CASCADE, mismo criterio que EventoEnlaceFarmacia: un Mantenimiento no
    # tiene NINGUN PROTECT apuntándole, así que era más fácil de borrar que una farmacia
    # — y se llevaba su historial con él.
    #
    # Lo que lo vuelve claramente un error y no una decisión: este modelo declara
    # `delete()` como NotImplementedError unas líneas más abajo, o sea que se define a sí
    # mismo como inmutable. Un CASCADE evadía esa garantía por completo, porque Django no
    # instancia los hijos al cascadear: emite un DELETE masivo en SQL y ese `delete()`
    # nunca llega a ejecutarse.
    mantenimiento = models.ForeignKey(Mantenimiento, on_delete=models.PROTECT, related_name='eventos')
    tipo_evento = models.CharField(max_length=25, choices=TipoEvento.choices)
    usuario = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True)
    detalle = models.JSONField(default=dict, blank=True)
    # `default` y no `auto_now_add`: una accion hecha sin señal se fecha con la hora en
    # que OCURRIO (la que manda la cola offline de la app), no con la de llegada al
    # servidor -- `auto_now_add` lo pisaba siempre. Ver services.iniciar_mantenimiento.
    timestamp = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = 'evento_mantenimiento'
        # 'pk' como desempate: timestamp (auto_now_add) puede repetirse entre dos eventos
        # del mismo mantenimiento creados en el mismo tick de reloj, dejando .first()/.last()
        # no determinísticos sin él (ver commit que agregó EventoSyncExterno).
        ordering = ['mantenimiento', 'timestamp', 'pk']

    def __str__(self):
        return f'Mantenimiento #{self.mantenimiento_id} - {self.get_tipo_evento_display()} @ {self.timestamp:%Y-%m-%d %H:%M}'

    def delete(self, *args, **kwargs):
        raise NotImplementedError('EventoMantenimiento es inmutable: no se puede eliminar.')


class TipoFirma(models.TextChoices):
    """Portado tal cual de TipoFirma.java (InvTICS)."""
    CUSTODIO = 'custodio', 'Custodio'
    TECNICO = 'tecnico', 'Técnico'


class PrioridadActividad(models.TextChoices):
    """Portado tal cual de PrioridadMantenimiento.java (InvTICS)."""
    NORMAL = 'normal', 'Normal'
    ALTA = 'alta', 'Alta'
    URGENTE = 'urgente', 'Urgente'



class AcuerdoNivelServicio(models.Model):
    """SLA por prioridad: en cuánto tiempo hay que ATENDER y RESOLVER un mantenimiento.

    Configurable (no constantes en el código) por el mismo criterio que
    TipoMantenimiento: son reglas de negocio que el área de TI ajusta sin tocar
    código. Se siembran valores razonables por migración de datos.

    El reloj arranca en `fecha_programada`, no en `fecha_creacion`: un preventivo
    agendado para dentro de un mes no debe contar como incumplido desde que se crea.
    Para un correctivo, ambas coinciden en la práctica (se crea con
    fecha_programada=ahora, ver iniciar_reparacion_desde_activo).
    """
    prioridad = models.CharField(max_length=10, choices=PrioridadMantenimiento.choices, unique=True)
    horas_respuesta = models.PositiveIntegerField(
        help_text='Horas desde la fecha programada para INICIAR el mantenimiento.',
    )
    horas_resolucion = models.PositiveIntegerField(
        help_text='Horas desde la fecha programada para CERRARLO.',
    )
    activo = models.BooleanField(default=True)

    class Meta:
        db_table = 'acuerdo_nivel_servicio'
        ordering = ['prioridad']
        verbose_name = 'Acuerdo de nivel de servicio (SLA)'
        verbose_name_plural = 'Acuerdos de nivel de servicio (SLA)'

    def __str__(self):
        return f'{self.get_prioridad_display()}: atender {self.horas_respuesta}h / resolver {self.horas_resolucion}h'


class FirmaMantenimiento(models.Model):
    """Firma digital (base64) de custodio o técnico al cerrar un mantenimiento."""
    mantenimiento = models.ForeignKey(Mantenimiento, on_delete=models.CASCADE, related_name='firmas')
    tipo_firma = models.CharField(max_length=10, choices=TipoFirma.choices)
    firma_base64 = models.TextField()
    # Mismo motivo que EventoMantenimiento.timestamp: una firma tomada sin señal
    # conserva su hora real.
    firmado_en = models.DateTimeField(default=timezone.now)
    ip_origen = models.GenericIPAddressField(null=True, blank=True)
    firmado_por = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='firmas_mantenimiento',
    )

    class Meta:
        db_table = 'firma_mantenimiento'
        ordering = ['-firmado_en']
        # Una firma por tipo y por mantenimiento. Es la mitad barata del problema de
        # idempotencia (BUG-4, ver docs/modulos.md): si el servidor procesa el POST y
        # la respuesta se pierde en el camino, la app lo reencola y lo reintenta --
        # `create()` a secas dejaba DOS firmas del mismo custodio. Acá la unicidad
        # natural alcanza y no hace falta la maquinaria de claves de idempotencia:
        # firmar dos veces no es un hecho nuevo, es el mismo hecho.
        constraints = [
            models.UniqueConstraint(
                fields=['mantenimiento', 'tipo_firma'], name='firma_unica_por_tipo',
            ),
        ]
        verbose_name = 'Firma de mantenimiento'
        verbose_name_plural = 'Firmas de mantenimiento'

    def __str__(self):
        return f'Firma {self.get_tipo_firma_display()} - Mantenimiento #{self.mantenimiento_id}'


class ImagenMantenimiento(models.Model):
    """Evidencia fotográfica de un mantenimiento. Archivo real (FileField), no solo una ruta."""
    mantenimiento = models.ForeignKey(Mantenimiento, on_delete=models.CASCADE, related_name='imagenes')
    imagen = models.FileField(upload_to='mantenimiento/imagenes/%Y/%m/')
    nombre_archivo = models.CharField(max_length=255, blank=True)
    tamanio_bytes = models.PositiveBigIntegerField(null=True, blank=True)
    subido_en = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'imagen_mantenimiento'
        ordering = ['-subido_en']
        verbose_name = 'Imagen de mantenimiento'
        verbose_name_plural = 'Imágenes de mantenimiento'

    def __str__(self):
        return self.nombre_archivo or self.imagen.name


class RepuestoUtilizado(models.Model):
    """Repuesto/consumible usado en una intervención, con costo para el informe técnico.

    `bodega` es opcional: si se indica, descuenta stock real (ver
    apps.mantenimiento.services.registrar_repuesto_utilizado); si se deja vacío, es un
    repuesto comprado/aportado fuera del flujo de bodega (igual se registra el costo).
    """
    mantenimiento = models.ForeignKey(Mantenimiento, on_delete=models.CASCADE, related_name='repuestos_utilizados')
    tipo_consumible = models.ForeignKey(
        TipoConsumible, on_delete=models.PROTECT, related_name='usos_en_mantenimiento',
    )
    bodega = models.ForeignKey(
        Bodega, on_delete=models.PROTECT, null=True, blank=True, related_name='repuestos_utilizados',
    )
    cantidad = models.PositiveIntegerField(default=1)
    costo_unitario = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    registrado_por = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='repuestos_registrados',
    )
    registrado_en = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'repuesto_utilizado'
        ordering = ['mantenimiento', 'registrado_en', 'pk']
        verbose_name = 'Repuesto utilizado'
        verbose_name_plural = 'Repuestos utilizados'

    def __str__(self):
        return f'{self.tipo_consumible} x{self.cantidad} - Mantenimiento #{self.mantenimiento_id}'

    @property
    def costo_total(self):
        return (self.costo_unitario or Decimal('0')) * self.cantidad


class ActividadPlanificada(models.Model):
    """Agenda general del técnico (no el checklist de un mantenimiento puntual).

    Puede enlazar opcionalmente a un Mantenimiento, un MantenimientoProgramado,
    un Activo o una Ubicacion (para mantenimientos generales sin equipo
    específico) — igual que ActividadPlanificadaJpa en InvTICS.
    """

    class Estado(models.TextChoices):
        PENDIENTE = 'pendiente', 'Pendiente'
        EN_PROGRESO = 'en_progreso', 'En progreso'
        COMPLETADA = 'completada', 'Completada'
        CANCELADA = 'cancelada', 'Cancelada'

    # De que cliente es este trabajo. Vacio = actividad interna, visible para todos —
    # mismo criterio "global o del cliente" que ReglaAlerta y Script.
    #
    # El modelo no lo tenia, y como el resto del sistema scopea por unidad en cada vista,
    # esta quedaba afuera: `actividades_planificadas_lista` listaba las de TODOS los
    # clientes y `actividad_planificada_completar` dejaba cerrar cualquiera cambiando el
    # id en la URL. Hoy no hay fuga real (0 actividades cargadas y los 3 usuarios con el
    # permiso tienen acceso a todas las unidades), pero se arma sola el dia que el modulo
    # se empiece a usar con un usuario acotado.
    unidad_negocio = models.ForeignKey(
        UnidadNegocio, on_delete=models.PROTECT, null=True, blank=True,
        related_name='actividades_planificadas',
        help_text='Cliente al que corresponde. Vacío = actividad interna, visible para todos.',
    )
    tecnico = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='actividades_planificadas',
    )
    creado_por = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='actividades_planificadas_creadas',
    )
    titulo = models.CharField(max_length=200)
    descripcion = models.TextField(blank=True)
    tipo_actividad = models.CharField(max_length=50)
    prioridad = models.CharField(max_length=10, choices=PrioridadActividad.choices, default=PrioridadActividad.NORMAL)
    estado = models.CharField(max_length=15, choices=Estado.choices, default=Estado.PENDIENTE)
    fecha_inicio = models.DateField()
    fecha_fin = models.DateField()
    fecha_completada = models.DateTimeField(null=True, blank=True)
    tiempo_estimado_minutos = models.PositiveIntegerField(null=True, blank=True)
    tiempo_real_minutos = models.PositiveIntegerField(null=True, blank=True)
    mantenimiento = models.ForeignKey(
        Mantenimiento, on_delete=models.SET_NULL, null=True, blank=True, related_name='actividades_planificadas',
    )
    mantenimiento_programado = models.ForeignKey(
        MantenimientoProgramado, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='actividades_planificadas',
    )
    equipo = models.ForeignKey(
        Activo, on_delete=models.SET_NULL, null=True, blank=True, related_name='actividades_planificadas',
    )
    ubicacion = models.ForeignKey(
        Ubicacion, on_delete=models.SET_NULL, null=True, blank=True, related_name='actividades_planificadas',
        help_text='Ubicación objetivo cuando la actividad es general, sin equipo específico.',
    )
    observaciones = models.TextField(blank=True)
    activo = models.BooleanField(default=True)

    class Meta:
        db_table = 'actividad_planificada'
        ordering = ['fecha_inicio']
        verbose_name = 'Actividad planificada'
        verbose_name_plural = 'Actividades planificadas'

    def __str__(self):
        return f'{self.titulo} ({self.tecnico})'


class Notificacion(models.Model):
    """Bandeja de notificaciones in-app. `leida` se muta directo, sin Evento propio."""
    usuario = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='notificaciones',
    )
    mensaje = models.CharField(max_length=255)
    url = models.CharField(max_length=500, blank=True)
    leida = models.BooleanField(default=False)
    mantenimiento = models.ForeignKey(
        Mantenimiento, on_delete=models.SET_NULL, null=True, blank=True, related_name='notificaciones',
    )
    actividad_planificada = models.ForeignKey(
        ActividadPlanificada, on_delete=models.SET_NULL, null=True, blank=True, related_name='notificaciones',
    )
    mantenimiento_programado = models.ForeignKey(
        MantenimientoProgramado, on_delete=models.SET_NULL, null=True, blank=True, related_name='notificaciones',
        help_text='Para avisos de "próximo a vencer" -- todavía no existe un Mantenimiento '
                  'generado al momento de avisar, así que no alcanza con el FK de arriba.',
    )
    creado_en = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'notificacion'
        ordering = ['-creado_en']

    def __str__(self):
        return self.mensaje


class ConsentimientoMonitoreo(models.Model):
    """Consentimiento legal del técnico para ser rastreado por GPS durante su jornada.

    Historial append-only (a diferencia de UbicacionTecnico, que es
    telemetría de alta frecuencia): el consentimiento legal se debe poder
    demostrar en el tiempo, así que cada aceptación queda registrada, nunca
    se sobreescribe.
    """
    usuario = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='consentimientos_monitoreo',
    )
    aceptado = models.BooleanField(default=True)
    version_terminos = models.CharField(max_length=20)
    ip = models.GenericIPAddressField(null=True, blank=True)
    timestamp = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'consentimiento_monitoreo'
        ordering = ['-timestamp']
        verbose_name = 'Consentimiento de monitoreo'
        verbose_name_plural = 'Consentimientos de monitoreo'

    def __str__(self):
        return f'{self.usuario} - v{self.version_terminos} @ {self.timestamp:%Y-%m-%d}'


class UbicacionTecnico(models.Model):
    """Posición GPS de un técnico en campo. Solo-append: telemetría de alta frecuencia, sin Evento propio."""
    usuario = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='ubicaciones_tecnico',
    )
    latitud = models.DecimalField(max_digits=10, decimal_places=7)
    longitud = models.DecimalField(max_digits=10, decimal_places=7)
    precision_metros = models.FloatField(null=True, blank=True)
    timestamp_captura = models.DateTimeField()
    creado_en = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'ubicacion_tecnico'
        ordering = ['-timestamp_captura']
        verbose_name = 'Ubicación de técnico'
        verbose_name_plural = 'Ubicaciones de técnico'

    def __str__(self):
        return f'{self.usuario} @ {self.timestamp_captura:%Y-%m-%d %H:%M}'


class VisitaTecnica(models.Model):
    """Visita planificada de un técnico a una farmacia.

    Antes "visita técnica" era solo un reporte de solo lectura que agrupaba
    colaboradores y equipos por Ubicacion (heredado de CustodioVisita/EquipoVisita de
    InvTICS). No dejaba rastro: no se podía saber cuándo fue la última vez que alguien
    pisó un sitio, qué visitas están planificadas, ni si se hicieron. Además apuntaba
    a `activos.Ubicacion`, que en producción está VACÍA -- el reporte no mostraba nada.

    Por eso esto cuelga de Farmacia y no de Ubicacion: es donde está el equipamiento
    real (700 farmacias, 692 con coordenadas) y es la misma entidad que usa el RMM, así
    que la visita, el monitoreo y los mantenimientos hablan del mismo sitio.
    """

    class Estado(models.TextChoices):
        PLANIFICADA = 'planificada', 'Planificada'
        EN_CURSO = 'en_curso', 'En curso'
        REALIZADA = 'realizada', 'Realizada'
        CANCELADA = 'cancelada', 'Cancelada'

    farmacia = models.ForeignKey(
        'catalogo.Farmacia', on_delete=models.PROTECT, related_name='visitas_tecnicas',
    )
    tecnico = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='visitas_tecnicas',
    )
    fecha_planificada = models.DateField()
    estado = models.CharField(max_length=12, choices=Estado.choices, default=Estado.PLANIFICADA)
    motivo = models.TextField(blank=True, help_text='Para qué se va: relevamiento, preventivo de ruta, etc.')
    observaciones = models.TextField(blank=True, help_text='Qué se encontró, cargado al cerrar.')
    fecha_inicio = models.DateTimeField(null=True, blank=True)
    fecha_cierre = models.DateTimeField(null=True, blank=True)
    # Misma verificación que en Mantenimiento y por el mismo motivo: es un hecho del
    # momento del cierre, se persiste para que quede auditable.
    distancia_verificacion_metros = models.FloatField(
        null=True, blank=True,
        help_text='Distancia mínima del técnico a la farmacia durante la visita, en metros.',
    )
    creado_por = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='visitas_tecnicas_creadas',
    )
    fecha_creacion = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'visita_tecnica'
        ordering = ['-fecha_planificada']
        verbose_name = 'Visita técnica'
        verbose_name_plural = 'Visitas técnicas'

    def __str__(self):
        return f'Visita a {self.farmacia.codigo} el {self.fecha_planificada:%d/%m/%Y}'

    @property
    def unidad_negocio(self):
        return self.farmacia.unidad_negocio

    @property
    def presencia_en_sitio(self):
        """Igual que en Mantenimiento: 'sin_datos' NO significa que no haya ido."""
        if self.distancia_verificacion_metros is None:
            return 'sin_datos'
        return 'verificada' if self.distancia_verificacion_metros <= RADIO_VERIFICACION_METROS else 'fuera_de_rango'

    @property
    def atrasada(self):
        """Planificada para una fecha ya pasada y todavía sin realizar."""
        return (
            self.estado in (self.Estado.PLANIFICADA, self.Estado.EN_CURSO)
            and self.fecha_planificada < timezone.localdate()
        )


class AccionOfflineAplicada(models.Model):
    """Qué acción de la cola offline de un teléfono ya se aplicó, para no aplicarla dos veces.

    El problema: `api.dart` traduce su timeout de 20 s a `SinConexion`, así que una
    petición que el servidor SÍ procesó —pero cuya respuesta se perdió en el camino— se
    reencola y se reintenta. Sin esto, el reintento o duplicaba el hecho (dos firmas del
    mismo custodio) o rebotaba con un error que dejaba la acción trabada en el teléfono
    para siempre.

    La clave es NATURAL, no un UUID inventado: `ColaOffline` ya numera sus filas con un
    autoincremental único por teléfono, y el usuario acota el espacio. Mismo criterio que
    el resto del proyecto, que resuelve idempotencia con clave natural + `get_or_create`
    (ver `registrar_actividad_mensual`, `vincular_activos_por_numero_serie`).

    Se guarda la RESPUESTA original para poder devolverla igual en el reintento: al
    técnico le tiene que constar que su acción se aplicó, no recibir un error por algo
    que en realidad salió bien.
    """

    usuario = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='acciones_offline_aplicadas',
    )
    origen_id = models.PositiveIntegerField(
        help_text='id de la fila en la ColaOffline del teléfono (autoincremental por dispositivo).',
    )
    tipo = models.CharField(max_length=40, help_text='Acción aplicada, para poder leer la tabla.')
    respuesta_json = models.JSONField(
        default=dict, blank=True,
        help_text='Cuerpo que se devolvió la primera vez; es lo que se repite en un reintento.',
    )
    estado_http = models.PositiveSmallIntegerField(
        default=200, help_text='Código que se devolvió la primera vez (200/201, o 409 si fue conflicto).',
    )
    creado_en = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = 'accion_offline_aplicada'
        ordering = ['-creado_en']
        constraints = [
            models.UniqueConstraint(
                fields=['usuario', 'origen_id'], name='accion_offline_unica_por_dispositivo',
            ),
        ]
        verbose_name = 'Acción offline aplicada'
        verbose_name_plural = 'Acciones offline aplicadas'

    def __str__(self):
        return f'{self.usuario} #{self.origen_id} ({self.tipo})'


class CierreEnConflicto(models.Model):
    """Un cierre hecho en campo que ya no se pudo aplicar, guardado para revisión manual.

    El caso: el técnico cierra un mantenimiento sin señal y, mientras tanto, mesa de
    ayuda lo cancela (o lo cierra con otro resultado) desde el panel. Cuando el teléfono
    recupera conexión, el guard de estado de `cerrar_mantenimiento` rechaza la acción —
    correctamente, porque pisar la decisión más nueva sería peor.

    Lo que faltaba era el después: hasta el 26-sep-2026 el trabajo real del técnico
    (resultado, tiempo, estado del equipo) se quedaba en el teléfono, con el motivo
    guardado en un `ultimo_error` que ninguna pantalla mostraba. El técnico veía "1
    pendiente" para siempre y mesa de ayuda no se enteraba de que había dos versiones de
    la verdad.

    Esto NO es un registro de negocio inmutable como `EventoMantenimiento`: es una
    bandeja operativa de triage, igual que `MensajeMqttFallido` — se marca `revisado` y
    se puede borrar una vez resuelta. No decide quién tiene razón; hace visible que hay
    que decidir.
    """

    mantenimiento = models.ForeignKey(
        Mantenimiento, on_delete=models.CASCADE, related_name='cierres_en_conflicto',
    )
    tecnico = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='cierres_en_conflicto',
    )
    ocurrido_en = models.DateTimeField(
        help_text='Cuándo cerró el técnico de verdad, en la farmacia (no cuándo llegó al servidor).',
    )
    payload_rechazado = models.JSONField(
        default=dict,
        help_text='El cierre tal como lo mandó el técnico: resultado, tiempo real, estado del equipo.',
    )
    estado_al_llegar = models.CharField(
        max_length=20,
        help_text='En qué estado estaba el mantenimiento cuando la acción llegó al servidor.',
    )
    motivo = models.TextField(help_text='Por qué se rechazó, en los términos del servicio que lo rechazó.')

    revisado = models.BooleanField(default=False)
    revisado_por = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='cierres_en_conflicto_revisados',
    )
    revisado_en = models.DateTimeField(null=True, blank=True)
    resolucion = models.TextField(
        blank=True, help_text='Qué se decidió: se aplicó el cierre del técnico, o por qué se descartó.',
    )
    escalado_en = models.DateTimeField(
        null=True, blank=True,
        help_text='Cuándo se reenvió el aviso por seguir sin revisar (ver '
                  'apps.mantenimiento.services.escalar_cierres_en_conflicto). Evita reescalar en cada corrida.',
    )
    creado_en = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = 'cierre_en_conflicto'
        ordering = ['-creado_en']
        verbose_name = 'Cierre en conflicto'
        verbose_name_plural = 'Cierres en conflicto'

    def __str__(self):
        return f'Cierre en conflicto de {self.tecnico} - Mantenimiento #{self.mantenimiento_id}'

    @property
    def unidad_negocio(self):
        """Para el scope multi-tenant del panel, por la misma vía que el mantenimiento."""
        return self.mantenimiento.cliente.unidad_negocio if self.mantenimiento.cliente_id else None

    @property
    def resultado_declarado(self):
        """El resultado que puso el técnico, para mostrarlo sin desarmar el JSON en la plantilla."""
        return self.payload_rechazado.get('resultado_tecnico', '')
