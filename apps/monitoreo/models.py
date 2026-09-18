from datetime import timedelta

from django.conf import settings
from django.db import models
from django.utils import timezone

from apps.catalogo.models import Estacion, Farmacia, Grupo, UnidadNegocio


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

    # Disco (GB) — del volumen C:, mismo mecanismo que almacenamiento_total_gb de
    # Estacion (Win32_LogicalDisk), pero como serie temporal en vez de dato puntual.
    disco_total_gb = models.FloatField(null=True, blank=True)
    disco_libre_gb = models.FloatField(null=True, blank=True)

    # Red (kbps, kilobits) — tasa ya calculada por el agente (contadores acumulados del
    # adaptador de la ruta por defecto, convertidos a tasa entre dos muestras, ver
    # agente-prueba/agente_prueba.py::_tasa_red_kbps). No se guarda el contador
    # crudo acá — a diferencia de MuestraRedFarmacia (SNMP a Mikrotik), este valor ya
    # viene calculado por un proceso de larga duración que puede quedarse la muestra
    # anterior en memoria.
    red_recibido_kbps = models.FloatField(null=True, blank=True)
    red_enviado_kbps = models.FloatField(null=True, blank=True)

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

    @property
    def disco_usado_pct(self):
        if self.disco_total_gb and self.disco_libre_gb is not None:
            return round(100 * (self.disco_total_gb - self.disco_libre_gb) / self.disco_total_gb, 1)
        return None

    @property
    def red_total_kbps(self):
        if self.red_recibido_kbps is None and self.red_enviado_kbps is None:
            return None
        return round((self.red_recibido_kbps or 0) + (self.red_enviado_kbps or 0), 1)


class MuestraRedFarmacia(models.Model):
    """Una muestra de ancho de banda del enlace de una FARMACIA (no de una estación),
    sondeada por SNMP al Mikrotik del sitio — ver apps.monitoreo.mikrotik. Mismo
    espíritu que MuestraMetrica pero a nivel de sitio: el router no reparte tráfico
    por estación (sin Queues por IP/MAC), así que esto es lo más granular que se
    puede medir del lado del enlace. No se fuerza en EstadoDispositivo/Alerta
    (estación-scoped, no encajan) — sigue el precedente de modelos paralelos por
    granularidad que ya usa apps.cumplimiento (ResultadoCumplimientoEstacion +
    ResultadoCumplimientoFarmacia).

    bytes_recibidos/bytes_enviados son el contador CRUDO acumulado que reportó el
    router en este sondeo (IF-MIB ifHCIn/OutOctets, 64 bits) — se guardan para poder
    auditar/depurar el cálculo. red_recibido_kbps/red_enviado_kbps es la tasa ya
    calculada al guardar, diferenciando contra la fila anterior de esta farmacia
    (NO memoria de proceso: el poller corre en un task de Celery que puede reiniciar
    entre corridas, a diferencia del agente de estación). null = primera muestra de
    esta farmacia, o el contador bajó respecto a la anterior (reinicio del router).
    """

    farmacia = models.ForeignKey(Farmacia, on_delete=models.CASCADE, related_name='muestras_red')
    bytes_recibidos = models.BigIntegerField()
    bytes_enviados = models.BigIntegerField()
    red_recibido_kbps = models.FloatField(null=True, blank=True)
    red_enviado_kbps = models.FloatField(null=True, blank=True)
    timestamp = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        db_table = 'muestra_red_farmacia'
        ordering = ['-timestamp']
        indexes = [
            models.Index(fields=['farmacia', '-timestamp']),
        ]
        verbose_name = 'Muestra de red de farmacia'
        verbose_name_plural = 'Muestras de red de farmacia'

    def __str__(self):
        return f'{self.farmacia.codigo} @ {self.timestamp:%Y-%m-%d %H:%M:%S}'

    @property
    def red_total_kbps(self):
        if self.red_recibido_kbps is None and self.red_enviado_kbps is None:
            return None
        return round((self.red_recibido_kbps or 0) + (self.red_enviado_kbps or 0), 1)

    @property
    def porcentaje_del_contratado(self):
        """Qué parte del enlace contratado está usando la farmacia, o None.

        La conversión es lo único delicado acá y es donde estuvo el error que se corrigió
        el 15-sep-2026: el consumo se mide en **kilobits** por segundo y lo contratado en
        **megabits**, así que el denominador es `mbps * 1000`. Las pantallas decían
        "KB/s" —kilobytes— sobre el mismo número, y quien comparara contra un contrato de
        10 Mbps calculaba ocho veces el uso real.

        None cuando falta cualquiera de los dos datos: sin el contratado no se puede
        decir si 831 kbps es mucho o poco, y fingir un valor típico sería peor que no
        mostrar nada.
        """
        contratado = self.farmacia.ancho_contratado_mbps
        total = self.red_total_kbps
        if not contratado or total is None:
            return None
        return round(100 * total / (contratado * 1000), 1)


class Metrica(models.TextChoices):
    CPU_CARGA_PCT = 'cpu_carga_pct', 'CPU (%)'
    RAM_USADA_PCT = 'ram_usada_pct', 'RAM (%)'
    DISCO_USADO_PCT = 'disco_usado_pct', 'Disco usado (%)'
    LATENCIA_MS = 'latencia_ms', 'Latencia (ms)'
    TEMPERATURA_C = 'temperatura_c', 'Temperatura (°C)'
    # kbps = kilobits, que es lo que calcula `_calcular_tasa` (bytes * 8 / 1000). La
    # etiqueta decía "KB/s" —kilobytes—, ocho veces más: quien creara una regla
    # "alertar si Red > 500" pensando en kilobytes disparaba a 62,5 KB/s reales.
    RED_TOTAL_KBPS = 'red_total_kbps', 'Red (kbps)'
    SIN_HEARTBEAT = 'sin_heartbeat', 'Sin heartbeat (minutos)'
    BITLOCKER_DESHABILITADO = 'bitlocker_deshabilitado', 'BitLocker deshabilitado'
    AGENTE_CAIDO_RED_VIVA = 'agente_caido_red_viva', 'Agente sin reportar (con red viva)'
    POS_ERRORES = 'pos_errores', 'Errores del POS (por ventana de reporte)'
    # Una sola métrica para los cuatro servicios: cuál cayó lo dice la Alerta, no la
    # regla. Cuatro métricas obligarían a crear cuatro reglas por unidad de negocio para
    # expresar la misma intención — "avisame si el POS pierde una dependencia".
    SERVICIO_POS_CAIDO = 'servicio_pos_caido', 'Servicio del POS sin responder'


class EstadoDispositivo(models.Model):
    """Snapshot ACTUAL (no histórico) del estado de una estación según una fuente de
    monitoreo (agente MQTT, MeshCentral, y a futuro ESET PROTECT).

    Convive con `Estacion.estado_conexion`/`ultimo_heartbeat` — no los reemplaza. Esos
    dos campos siguen siendo el resumen de la fuente MQTT que ya usa todo el resto del
    panel/reportes; esta tabla es la capa nueva que permite cruzar MQTT contra otras
    fuentes (`evaluar_cruce_monitoreo`, ver services.py) sin tocar ese código existente.

    Una fila por (estacion, fuente): se actualiza in-place (`update_or_create`) en cada
    señal — no es un histórico, para eso está `EventoMonitoreo`.
    """

    class Fuente(models.TextChoices):
        MQTT = 'mqtt', 'Agente MQTT'
        MESHCENTRAL = 'meshcentral', 'MeshCentral'
        # ESET = 'eset', 'ESET PROTECT'  # pendiente de aprobación de acceso a su API —
        # el modelo ya queda listo para sumarla (agregar el choice + un adapter nuevo).

    estacion = models.ForeignKey(Estacion, on_delete=models.CASCADE, related_name='estados_dispositivo')
    fuente = models.CharField(max_length=20, choices=Fuente.choices)
    en_linea = models.BooleanField()
    detalle = models.JSONField(
        blank=True, default=dict,
        help_text='Payload específico de la fuente (ej. conn de MeshCentral). No forma parte '
                  'del contrato del cruce — solo referencia/debug.',
    )
    actualizado_en = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'estado_dispositivo'
        unique_together = [('estacion', 'fuente')]
        ordering = ['estacion__codigo', 'fuente']
        verbose_name = 'Estado de dispositivo'
        verbose_name_plural = 'Estados de dispositivo'

    def __str__(self):
        return f'{self.estacion.codigo} · {self.get_fuente_display()}: {"en línea" if self.en_linea else "fuera de línea"}'


class EventoMonitoreo(models.Model):
    """Histórico de TRANSICIONES de EstadoDispositivo (hypertable en producción, igual
    que MuestraMetrica) — se escribe solo cuando `en_linea` cambia respecto al último
    EstadoDispositivo conocido, no en cada señal recibida. A la frecuencia de heartbeat/
    eventos de 1.800+ estaciones, un evento por señal sería ruido puro sin valor; lo que
    importa para el cruce y para auditar discrepancias es cuándo cambió el estado.
    """

    estacion = models.ForeignKey(Estacion, on_delete=models.CASCADE, related_name='eventos_monitoreo')
    fuente = models.CharField(max_length=20, choices=EstadoDispositivo.Fuente.choices)
    en_linea = models.BooleanField()
    detalle = models.JSONField(blank=True, default=dict)
    timestamp = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        db_table = 'evento_monitoreo'
        ordering = ['-timestamp']
        indexes = [
            models.Index(fields=['estacion', '-timestamp']),
        ]

    def __str__(self):
        return f'{self.estacion.codigo} · {self.get_fuente_display()} -> {"en línea" if self.en_linea else "fuera de línea"} @ {self.timestamp:%Y-%m-%d %H:%M:%S}'


class ReglaAlerta(models.Model):
    """Condición que, sostenida por `duracion_minutos`, abre una Alerta.

    `sin_heartbeat`, `bitlocker_deshabilitado`, `agente_caido_red_viva` y
    `pos_errores` son las únicas métricas que no comparan contra MuestraMetrica ni
    usan `duracion_minutos`:
    - `sin_heartbeat` se evalúa en el comando `marcar_estaciones_offline` (umbral en
      minutos sin heartbeat).
    - `bitlocker_deshabilitado` se evalúa en apps.mqtt_worker.services.manejar_info_equipo,
      justo cuando el agente reporta el estado de cifrado (Estacion.bitlocker_habilitado)
      — no es una serie de tiempo, es un estado binario: se abre o resuelve directo con
      cada reporte.
    - `agente_caido_red_viva` (cruce MQTT × MeshCentral, ver EstadoDispositivo/
      evaluar_cruce_monitoreo) reusa el mismo umbral en minutos que `sin_heartbeat`
      (minutos sin heartbeat MQTT), exigiendo además que MeshCentral vea la estación en
      línea — distingue "agente caído, red viva" de "red caída, ambas fuentes lo ven mal".
    - `pos_errores` se evalúa en apps.mqtt_worker.services.manejar_pos_errores: cada
      reporte del agente ya es una ventana cerrada (los ERROR/FATAL nuevos del log del
      POS desde el último chequeo, agrupados por mensaje), así que el umbral compara
      directo contra el total de esa ventana — ver PosErrorDetectado.
    """

    class Operador(models.TextChoices):
        GTE = 'gte', '≥ (mayor o igual)'
        LTE = 'lte', '≤ (menor o igual)'

    class Severidad(models.TextChoices):
        WARNING = 'warning', 'Advertencia'
        CRITICAL = 'critical', 'Crítica'

    nombre = models.CharField(max_length=150)
    # No toda alerta amerita mandar un técnico: un pico de CPU se resuelve solo, un POS
    # que no levanta no. Apagado por defecto para que activar una regla nueva no empiece
    # a generar órdenes de trabajo sin que nadie lo haya decidido.
    abre_mantenimiento = models.BooleanField(
        default=False,
        help_text='Si al dispararse esta regla se abre automáticamente un mantenimiento '
                  'para el equipo de la estación afectada.',
    )
    metrica = models.CharField(max_length=30, choices=Metrica.choices)
    operador = models.CharField(
        max_length=3, choices=Operador.choices, default=Operador.GTE,
        help_text='Ignorado para "Sin heartbeat"/"Agente sin reportar (con red viva)" (siempre '
                  '"más de X minutos") y para "BitLocker deshabilitado" (condición binaria, sin umbral).',
    )
    umbral = models.FloatField(
        default=0,
        help_text='% para CPU/RAM, ms para latencia, °C para temperatura, minutos para sin heartbeat '
                  'y para "Agente sin reportar (con red viva)". Ignorado (dejar en 0) para "BitLocker '
                  'deshabilitado".',
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
    # Referencia perezosa por string: evita que apps.monitoreo importe apps.mantenimiento
    # a nivel de módulo (hoy la dependencia va en un solo sentido y conviene que siga así).
    mantenimiento = models.ForeignKey(
        'mantenimiento.Mantenimiento', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='alertas_origen',
        help_text='Mantenimiento abierto automáticamente por esta alerta, si la regla lo pide.',
    )
    escalada_en = models.DateTimeField(
        null=True, blank=True,
        help_text='Cuándo se reenvió la notificación por seguir ABIERTA sin reconocer '
                  '(ver apps.monitoreo.services.escalar_alertas_abiertas). Evita reescalar en cada corrida.',
    )
    # Hipótesis generada por un modelo de lenguaje, NO un hecho verificado. Se guarda
    # aparte de cualquier campo operativo justamente para que nadie la confunda con algo
    # medido: todo lo demás en esta tabla salió de un sondeo o un reporte del agente.
    # Solo se llena para alertas CRÍTICAS (ver apps.monitoreo.tasks.diagnosticar_alerta_task).
    diagnostico_ia = models.TextField(
        blank=True, null=True,
        help_text='Diagnóstico automático generado por IA. Es una hipótesis sin verificar: '
                  'confirmar antes de actuar.',
    )
    diagnostico_generado_en = models.DateTimeField(
        null=True, blank=True,
        help_text='Cuándo se generó el diagnóstico. Vacío = no se pidió o no se pudo generar. '
                  'Marca que el intento ya ocurrió, para no repetir la llamada (y el costo).',
    )

    class Meta:
        db_table = 'alerta'
        ordering = ['-abierta_en']

    def __str__(self):
        return f'{self.regla.nombre} · {self.estacion.codigo} ({self.get_estado_display()})'


class PosErrorDetectado(models.Model):
    """Un mensaje de error distinto detectado en el log del POS de una estación
    (Logs\\GeneraXML.txt del propio Zabyca.Pos.Desktop, vía log4net — pese al nombre
    del archivo, captura errores generales de la aplicación, no solo generación de
    XML). El agente reporta periódicamente (`bucle_log_pos`) solo lo nuevo desde su
    última lectura, ya agrupado por mensaje exacto; acá se acumula: a diferencia de
    SoftwareInstaladoDetectado (snapshot que se reemplaza en cada escaneo), esto es un
    contador de por vida por (estación, mensaje) — el mismo bug real (ej. una relación
    de base de datos faltante) repite el mismo mensaje una y otra vez, así que la
    cardinalidad esperada es chica por estación, no hace falta purgar.

    Distinto de MuestraMetrica: no es una serie de tiempo de un valor numérico, es
    contenido de texto — por eso no vive como property de MuestraMetrica ni se evalúa
    con el mecanismo genérico de evaluar_reglas_metricas (ver evaluar_regla_pos_errores
    en services.py, que sí reusa el resto del motor de alertas).
    """

    class Categoria(models.TextChoices):
        SISTEMA = 'sistema', 'Sistema (cuenta para la alerta)'
        # "Negocio" = una validación del POS haciendo su trabajo (ej. "VENTA SIN LOTE"
        # bloqueando una venta sin lote) — nivel ERROR en el log del POS, pero NO es
        # una falla de infraestructura. Confirmado con el usuario que es rutinario, no
        # esporádico: contarlo igual que un timeout de base habría inundado la alerta
        # de falsos positivos. Se sigue guardando (visible en la ficha), solo no suma
        # al total que evalúa evaluar_regla_pos_errores. Ver
        # apps.monitoreo.services.clasificar_error_pos.
        NEGOCIO = 'negocio', 'Negocio (no cuenta para la alerta)'

    estacion = models.ForeignKey(Estacion, on_delete=models.CASCADE, related_name='pos_errores')
    mensaje = models.CharField(max_length=500)
    nivel = models.CharField(max_length=10, default='ERROR')
    categoria = models.CharField(max_length=10, choices=Categoria.choices, default=Categoria.SISTEMA)
    cantidad_total = models.PositiveIntegerField(default=0)
    primera_vez = models.DateTimeField(auto_now_add=True)
    ultima_vez = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'pos_error_detectado'
        ordering = ['-ultima_vez']
        unique_together = [('estacion', 'mensaje')]
        verbose_name = 'Error del POS detectado'
        verbose_name_plural = 'Errores del POS detectados'

    def __str__(self):
        return f'{self.estacion.codigo}: {self.mensaje[:60]} (x{self.cantidad_total})'


class VentanaMantenimiento(models.Model):
    """Ventana de tiempo durante la cual las alertas de un destino de estaciones se
    silencian a propósito (ej. un despliegue o reinicio masivo programado), para que
    una acción operativa propia no se confunda con un problema real en `/alertas/`.

    Mismo shape de destino que ScriptProgramado/EjecucionScript/Despliegue
    (unidad_negocio/destino_tipo/grupos/farmacias/estaciones), resuelto con
    apps.catalogo.services.resolver_estaciones (ver
    apps.monitoreo.services.ventana_mantenimiento_activa, que es el único punto que
    la consulta — vía el hook en abrir_o_mantener_alerta, cubre a todas las reglas
    de alerta existentes y futuras sin tocar cada evaluador por separado).
    """

    class DestinoTipo(models.TextChoices):
        CADENA = 'cadena', 'Toda la cadena'
        GRUPOS = 'grupos', 'Grupos específicos'
        FARMACIAS = 'farmacias', 'Farmacias específicas'
        ESTACIONES = 'estaciones', 'Estaciones específicas'

    unidad_negocio = models.ForeignKey(
        UnidadNegocio, on_delete=models.PROTECT, related_name='ventanas_mantenimiento',
        help_text='Cliente al que se dirige. "Toda la cadena" significa toda la cadena '
                  'de esta unidad de negocio, nunca de otras.',
    )
    destino_tipo = models.CharField(max_length=20, choices=DestinoTipo.choices)
    grupos = models.ManyToManyField(Grupo, blank=True, related_name='ventanas_mantenimiento')
    farmacias = models.ManyToManyField(Farmacia, blank=True, related_name='ventanas_mantenimiento')
    estaciones = models.ManyToManyField(Estacion, blank=True, related_name='ventanas_mantenimiento')

    desde = models.DateTimeField()
    hasta = models.DateTimeField()
    motivo = models.CharField(max_length=255, help_text='Ej. "despliegue de POS v5.2 a toda la cadena".')
    activo = models.BooleanField(default=True)

    creado_por = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='ventanas_mantenimiento_creadas',
    )
    fecha_creacion = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'ventana_mantenimiento'
        ordering = ['-desde']
        verbose_name = 'Ventana de mantenimiento'
        verbose_name_plural = 'Ventanas de mantenimiento'

    def __str__(self):
        return f'{self.motivo} ({self.unidad_negocio.codigo}, {self.desde:%d/%m %H:%M}–{self.hasta:%d/%m %H:%M})'

    @property
    def esta_en_curso(self):
        from django.utils import timezone
        return self.activo and self.desde <= timezone.now() <= self.hasta


class CanalNotificacion(models.Model):
    """Destino al que reenviar el aviso de una Alerta, además del correo (que sigue
    yendo siempre vía notificar_alerta, sin pasar por este modelo). unidad_negocio en
    blanco = canal global, usado por cualquier unidad que no tenga uno propio — mismo
    criterio "global o del cliente" que ReglaAlerta.unidad_negocio.

    `destino` guarda lo que identifica al receptor según el tipo: la URL del webhook
    entrante para Teams, el chat_id para Telegram. El TOKEN del bot de Telegram NO vive
    acá: es un secreto compartido por todos los canales, no un destino, así que va en
    TELEGRAM_BOT_TOKEN (settings) igual que COMANDO_HMAC_SECRET. Un chat_id no es
    secreto; sin el token no sirve para nada.
    """

    class Tipo(models.TextChoices):
        WEBHOOK_TEAMS = 'webhook_teams', 'Webhook de Microsoft Teams'
        TELEGRAM = 'telegram', 'Telegram (chat_id)'

    unidad_negocio = models.ForeignKey(
        UnidadNegocio, on_delete=models.PROTECT, null=True, blank=True, related_name='canales_notificacion',
        help_text='Vacío = canal global, aplica a toda unidad de negocio que no tenga uno propio.',
    )
    tipo = models.CharField(max_length=20, choices=Tipo.choices, default=Tipo.WEBHOOK_TEAMS)
    # CharField y no URLField: un chat_id de Telegram no es una URL (es un entero, a
    # veces negativo para grupos). Validar como URL dejaba fuera el tipo nuevo.
    destino = models.CharField(
        max_length=500,
        help_text='URL del webhook entrante (Teams) o chat_id del chat/grupo (Telegram).',
    )
    activo = models.BooleanField(default=True)
    creado_por = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='canales_notificacion_creados',
    )
    fecha_creacion = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'canal_notificacion'
        ordering = ['unidad_negocio__codigo', 'tipo']
        verbose_name = 'Canal de notificación'
        verbose_name_plural = 'Canales de notificación'

    def __str__(self):
        destino_str = self.unidad_negocio.codigo if self.unidad_negocio_id else 'Global'
        return f'{self.get_tipo_display()} ({destino_str})'


class EquipoBordeFarmacia(models.Model):
    """Identidad del equipo de borde (Mikrotik) de una farmacia, leída por SNMP.

    Modelo aparte de `EstadoEnlaceFarmacia` y de `MuestraRedFarmacia` porque el ciclo de
    vida es otro: el estado del enlace se sobrescribe cada 2 minutos, las muestras de
    tráfico son una serie que crece sin parar, y esto casi nunca cambia — el número de
    serie nunca, la versión de RouterOS solo cuando alguien actualiza.

    Sirve para tres cosas que hoy no se pueden hacer:

    - **Inventariar sin ir al sitio.** El router entrega su modelo y su número de serie,
      que es justo lo que quedó vacío en los activos de topología cargados a mano.
    - **Ver la brecha de parcheo.** El 15-sep-2026 el primer sondeo encontró MC001 en
      RouterOS 6.47.7 y GNB01/MCAR3 en 6.49.17, y nadie tenía forma de saberlo.
    - **Distinguir una caída de un router que se reinicia solo.** Con `uptime_segundos`,
      un equipo que se reinicia cada noche deja de verse igual que uno estable.
    """

    # Un uptime por debajo de esto significa que el equipo arrancó hace poco. No es un
    # problema por sí mismo —alguien pudo actualizarlo—, pero repetido entre sondeos
    # sucesivos es un router reiniciándose solo, que es un problema y de los difíciles
    # de ver: entre reinicio y reinicio responde perfectamente.
    UMBRAL_REINICIO_RECIENTE_SEGUNDOS = 3600

    farmacia = models.OneToOneField(Farmacia, on_delete=models.CASCADE, related_name='equipo_borde')
    modelo = models.CharField(
        max_length=120, blank=True,
        help_text='Tal como lo reporta sysDescr, ej. "RouterOS RB951Ui-2nD".',
    )
    numero_serie = models.CharField(max_length=60, blank=True)
    version_routeros = models.CharField(max_length=30, blank=True)
    nombre_sistema = models.CharField(
        max_length=60, blank=True,
        help_text='sysName del equipo. Normalmente es el código de la farmacia: si no coincide, '
                  'la IP cargada apunta a otro equipo (ver `nombre_coincide`).',
    )
    uptime_segundos = models.BigIntegerField(
        null=True, blank=True,
        help_text='Hace cuánto arrancó el equipo. Se guarda en segundos aunque SNMP lo entregue '
                  'en centésimas, para no obligar a dividir en cada lectura.',
    )
    ultima_lectura = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = 'equipo_borde_farmacia'
        ordering = ['farmacia__codigo']
        verbose_name = 'Equipo de borde de farmacia'
        verbose_name_plural = 'Equipos de borde de farmacia'

    def __str__(self):
        return f'{self.farmacia.codigo}: {self.modelo or "sin identificar"}'

    @property
    def nombre_coincide(self):
        """False si el equipo dice llamarse distinto de la farmacia a la que lo asignamos.

        Es una verificación de integridad gratis sobre las ~700 IP cargadas desde una
        planilla: si `ip_router` de ML016 apunta a un equipo que se llama GNB01, el dato
        está mal y todo lo que se monitoree de esa farmacia es de otra. None = el equipo
        todavía no reportó su nombre.
        """
        if not self.nombre_sistema:
            return None
        return self.nombre_sistema.strip().upper() == self.farmacia.codigo.upper()

    @property
    def reinicio_reciente(self):
        """True si el equipo arrancó hace menos de una hora."""
        if self.uptime_segundos is None:
            return None
        return self.uptime_segundos < self.UMBRAL_REINICIO_RECIENTE_SEGUNDOS


class DispositivoDetectado(models.Model):
    """Un equipo que el Mikrotik de la farmacia vio en su LAN (tabla ARP, por SNMP).

    Es la mitad **verificada** del inventario. `Activo` guarda lo que alguien declaró que
    hay; esto guarda lo que el router realmente ve, con su IP y su MAC, sin que nadie
    visite el local. El cruce entre las dos responde dos preguntas que hoy no se pueden
    contestar: qué hay enchufado que nadie inventarió, y qué está inventariado pero no
    aparece.

    La identidad es la **MAC**, no la IP: la Epson va por WiFi con DHCP y cambia de
    dirección, pero su MAC no. Por eso la clave única es (farmacia, mac) y la IP se
    actualiza — el mismo criterio por el que `Activo.ip` no lleva unique.

    No es un historial: una fila por equipo, con cuándo se lo vio por primera y por
    última vez. Un equipo que se desconecta no se borra, deja de actualizarse — y eso es
    justamente lo que permite notar que algo desapareció.
    """

    farmacia = models.ForeignKey(Farmacia, on_delete=models.CASCADE, related_name='dispositivos_detectados')
    mac = models.CharField(max_length=17, help_text='Normalizada a AA:BB:CC:DD:EE:FF, como `Activo.mac`.')
    ip = models.GenericIPAddressField()
    interfaz_indice = models.PositiveIntegerField(
        null=True, blank=True,
        help_text='ifIndex del puerto del router donde se lo vio. Cruzado con el nombre de la '
                  'interfaz (ej. "ether4_BASE") dice en qué puerto está enchufado.',
    )
    visto_por_primera_vez = models.DateTimeField(auto_now_add=True)
    visto_por_ultima_vez = models.DateTimeField()

    class Meta:
        db_table = 'dispositivo_detectado'
        ordering = ['farmacia__codigo', 'ip']
        constraints = [
            models.UniqueConstraint(fields=['farmacia', 'mac'], name='un_dispositivo_por_mac_y_farmacia'),
        ]
        indexes = [models.Index(fields=['farmacia', 'ip'])]
        verbose_name = 'Dispositivo detectado'
        verbose_name_plural = 'Dispositivos detectados'

    def __str__(self):
        return f'{self.farmacia.codigo}: {self.ip} ({self.mac})'

    @property
    def activo_declarado(self):
        """El `Activo` que declara esta MAC, o None si nadie lo inventarió.

        Import diferido: `apps.activos` ya importa de catalogo, y traerlo arriba crearía
        un ciclo entre monitoreo y activos.
        """
        from apps.activos.models import Activo

        return Activo.objects.filter(farmacia=self.farmacia, mac__iexact=self.mac).first()


class EstadoEnlaceFarmacia(models.Model):
    """Estado actual del enlace de una farmacia: ¿responde o no?

    Por qué existe, y por qué es distinto de todo lo demás que ya monitorea este
    proyecto: `MuestraMetrica` y `EstadoDispositivo` dependen de que la farmacia tenga
    un **agente instalado**, y hoy eso cubre 8 de ~1.800 estaciones. Un sondeo ICMP al
    equipo de borde no necesita nada instalado del otro lado, así que cubre las ~704
    sucursales desde el día uno. Es la capacidad que tenía `Cresio_enlaces`, el sistema
    anterior, y que acá faltaba (ver `docs/evaluacion-cresio-enlaces.md`).

    Una fila por farmacia, sobrescrita en cada sondeo (estado actual, no historial —
    el historial de caídas vive en `EventoEnlaceFarmacia`). Mismo criterio de
    granularidad-por-sitio que `MuestraRedFarmacia`: el enlace es de la farmacia, no
    de una estación.
    """

    # Un sondeo fallido no es una caída: un paquete ICMP se pierde por mil motivos.
    # `Cresio_enlaces` contaba fallas consecutivas antes de declarar la caída y este
    # modelo hace lo mismo. Con el ciclo de 5 min de este proyecto, 3 fallas son ~15
    # minutos de enlace realmente ausente.
    UMBRAL_FALLAS_CONSECUTIVAS = 3

    farmacia = models.OneToOneField(Farmacia, on_delete=models.CASCADE, related_name='estado_enlace')
    alcanzable = models.BooleanField(
        null=True, blank=True,
        help_text='True = respondió el último sondeo. False = caído (superó el umbral de fallas). '
                  'null = nunca se sondeó todavía, que no es lo mismo que caído.',
    )
    latencia_ms = models.FloatField(
        null=True, blank=True, help_text='Latencia del último sondeo exitoso. null = el último falló.',
    )
    fallas_consecutivas = models.PositiveIntegerField(default=0)
    # Tercera categoria, que faltaba: `alcanzable` distingue "responde" de "caido" y
    # null de "nunca se sondeo", pero no habia forma de ver "se sondea y nunca
    # respondio". Medido el 17-sep-2026: de 162 farmacias en False, 133 nunca habian
    # respondido un solo sondeo -- todas desde el mismo instante en que arranco el
    # monitoreo (11-sep 22:00). Eso no son caidas: es que el servidor no tiene ruta a
    # esos segmentos, o la IP esta mal cargada, o el sitio es de baja.
    #
    # Importa para dos cosas: el panel decia "162 caidos" cuando los accionables eran
    # 28, y el aviso por correo (ver enlaces.notificar_cambios_enlaces) reportaria al
    # proveedor un enlace que nunca estuvo arriba -- el proveedor responde que su
    # enlace esta bien, y tiene razon.
    respondio_alguna_vez = models.BooleanField(
        default=False,
        help_text='False y alcanzable=False = nunca respondio desde que se lo sondea, que no es '
                  'una caida: revisar ruta, IP cargada o si el sitio sigue activo.',
    )
    ultima_verificacion = models.DateTimeField(null=True, blank=True)
    ultimo_cambio_estado = models.DateTimeField(
        null=True, blank=True,
        help_text='Cuándo pasó de alcanzable a caído o viceversa. Sirve para "lleva N horas caída" '
                  'sin tener que recorrer los eventos.',
    )

    @property
    def nunca_respondio(self) -> bool:
        """Se lo sondea y jamas contesto. Distinto de `alcanzable is None` (nunca se
        sondeo) y de una caida real (respondio antes y ahora no)."""
        return self.alcanzable is False and not self.respondio_alguna_vez

    class Meta:
        db_table = 'estado_enlace_farmacia'
        ordering = ['farmacia__codigo']
        verbose_name = 'Estado de enlace de farmacia'
        verbose_name_plural = 'Estados de enlace de farmacia'
        permissions = [
            # Permiso propio, separado de `add`/`change`, para la sonda externa que
            # reporta por API (ver apps.monitoreo.api_views). El token de esa sonda vive
            # en una máquina de oficina fuera del servidor: tiene que poder reportar
            # mediciones y NADA más. Con `change_estadoenlacefarmacia` podría además
            # editar el estado a mano desde el admin.
            ('registrar_sondeo_enlace', 'Puede reportar resultados de sondeo de enlaces por API'),
        ]

    def __str__(self):
        if self.alcanzable is None:
            estado = 'sin sondear'
        else:
            estado = 'activo' if self.alcanzable else 'caído'
        return f'{self.farmacia.codigo}: enlace {estado}'


class ReinicioEquipoBorde(models.Model):
    """Un reinicio del Mikrotik de una farmacia, detectado porque su uptime bajó.

    Modelo aparte de `EventoEnlaceFarmacia` a propósito: esa tabla es la línea base de
    disponibilidad para reclamarle a TELCONET/PUNTO NET, y un reinicio que provocamos
    nosotros —o una falla eléctrica del local— no es una falla del proveedor. Mezclarlos
    ensuciaría la única evidencia que hay para discutir un SLA.

    Existe porque el monitoreo de enlace **no ve un reinicio corto**: exige tres fallas
    consecutivas de ICMP (unos 6 minutos) antes de declarar una caída, y un Mikrotik
    arranca en menos. El 15-sep-2026 se reinició GAT01 a mano y el historial siguió
    diciendo "sin caídas registradas", que era cierto y aun así ocultaba lo que había
    pasado.

    La detección es por comparación: si el uptime leído es MENOR que el anterior, el
    equipo arrancó de nuevo entremedio. Es más confiable que mirar "uptime chico", que
    depende de cada cuánto se sondee.
    """

    farmacia = models.ForeignKey(Farmacia, on_delete=models.CASCADE, related_name='reinicios_equipo')
    detectado_en = models.DateTimeField(auto_now_add=True, db_index=True)
    arranque_estimado = models.DateTimeField(
        help_text='Cuándo arrancó el equipo, calculado como el momento de la lectura menos su '
                  'uptime. Es estimado porque entre el arranque real y la lectura pasa lo que '
                  'tarde el sondeo en llegar.',
    )
    uptime_previo_segundos = models.BigIntegerField(
        null=True, blank=True,
        help_text='Cuánto llevaba encendido en la lectura anterior. Un valor alto seguido de un '
                  'reinicio es un equipo estable que se cayó una vez; valores bajos repetidos son '
                  'un equipo que se reinicia solo, que es el problema difícil de ver.',
    )
    version_routeros = models.CharField(
        max_length=30, blank=True,
        help_text='Versión al momento del reinicio: distingue un arranque tras una actualización '
                  'de uno espontáneo.',
    )

    class Meta:
        db_table = 'reinicio_equipo_borde'
        ordering = ['-detectado_en']
        indexes = [models.Index(fields=['farmacia', '-detectado_en'])]
        verbose_name = 'Reinicio de equipo de borde'
        verbose_name_plural = 'Reinicios de equipo de borde'

    def __str__(self):
        return f'{self.farmacia.codigo}: reinicio {self.arranque_estimado:%d/%m %H:%M}'

    @property
    def horas_encendido_antes(self):
        """Cuánto llevaba andando antes de reiniciarse, o None si no se sabía."""
        if self.uptime_previo_segundos is None:
            return None
        return round(self.uptime_previo_segundos / 3600, 1)


class EventoEnlaceFarmacia(models.Model):
    """Una caída del enlace de una farmacia, con su duración. Historial, no estado.

    Es el dato que no se puede reconstruir después y que más valor tiene fuera de lo
    operativo: es la línea base de disponibilidad real por sitio para discutir un SLA
    con TELCONET/PUNTO NET. `Cresio_enlaces` lo venía acumulando desde mayo de 2026;
    si esa base se pierde, se pierden meses de evidencia.

    `fin` vacío = la caída sigue en curso.
    """

    farmacia = models.ForeignKey(Farmacia, on_delete=models.CASCADE, related_name='eventos_enlace')
    inicio = models.DateTimeField(db_index=True)
    fin = models.DateTimeField(null=True, blank=True)
    circuito_proveedor = models.CharField(
        max_length=80, blank=True,
        help_text='Copia del circuito al momento de la caída. Es lo que el proveedor pide al abrir '
                  'el ticket, y se guarda acá para que el evento siga siendo útil aunque la farmacia '
                  'cambie de circuito después.',
    )
    # Dos marcas y no un booleano: el aviso de caída y el de recuperación son dos
    # correos distintos, separados por horas, y hace falta saber cuál de los dos ya
    # salió. Guardar CUÁNDO además permite auditar el retraso real entre la caída y el
    # aviso sin cruzar con los logs.
    #
    # Se marcan solas en el backfill de la migración que las crea: sin eso, la primera
    # corrida habría mandado un correo con las 162 caídas abiertas de ese momento (134
    # de ellas de más de 24 horas, crónicas, que no son novedad de nadie). El sistema
    # arranca avisando solo de lo que pase de ahí en adelante.
    notificado_en = models.DateTimeField(
        null=True, blank=True,
        help_text='Cuándo se avisó por correo de esta caída. Vacío = todavía no se avisó.',
    )
    recuperacion_notificada_en = models.DateTimeField(
        null=True, blank=True,
        help_text='Cuándo se avisó de que el enlace volvió. Vacío = todavía no se avisó.',
    )

    class Meta:
        db_table = 'evento_enlace_farmacia'
        ordering = ['-inicio']
        indexes = [
            models.Index(fields=['farmacia', '-inicio']),
        ]
        verbose_name = 'Caída de enlace'
        verbose_name_plural = 'Caídas de enlace'

    def __str__(self):
        estado = 'en curso' if self.fin is None else f'{self.duracion_minutos} min'
        return f'{self.farmacia.codigo}: caída {self.inicio:%d/%m %H:%M} ({estado})'

    @property
    def en_curso(self) -> bool:
        return self.fin is None

    @property
    def duracion_minutos(self) -> int | None:
        if self.fin is None:
            return None
        return int((self.fin - self.inicio).total_seconds() // 60)


class EstadoRedActivo(models.Model):
    """Si un activo SIN agente responde en la red de su farmacia, y cuándo se lo vio.

    Por qué existe: una impresora, un medianet o un pinpad no corren agente, así que el
    sistema sabía que existen (`Activo`) pero nunca si están vivos. Cuando una caja
    llamaba diciendo "no imprime", no había forma de separar "el equipo está apagado o
    desconectado" de "el POS no le está hablando".

    Por qué lo sondea el agente y no el servidor: el servidor no tiene ninguna ruta hacia
    las IPs privadas de las farmacias — confirmado con pings reales, 100% de pérdida. Una
    estación de la propia farmacia sí las alcanza. Mismo motivo por el que el Mikrotik se
    sondea desde el agente (ver `apps.monitoreo.mikrotik.solicitar_sondeo_red_farmacias_via_agente`).

    Por qué es último estado y NO una serie temporal como `MuestraRedFarmacia`: las
    farmacias cierran y apagan los equipos. Guardar una fila por ping sería, en su mayor
    parte, historial de equipos legítimamente apagados de noche — mucho volumen para una
    pregunta que nadie hace. Lo que se necesita es "¿responde ahora?" y "¿desde cuándo no
    responde?", y para eso alcanza con el último valor.

    Por el mismo motivo esto NO genera alertas. Un equipo que no responde a las 22:00 es
    una farmacia cerrada, no una incidencia; alertar sobre eso serían cientos de falsos
    positivos por noche y el resultado conocido es que se terminan ignorando todas.
    """

    activo = models.OneToOneField(
        'activos.Activo', on_delete=models.CASCADE, related_name='estado_red',
    )
    ip_sondeada = models.GenericIPAddressField(
        help_text='La IP que se pingeó. Se guarda porque la del activo puede cambiar y '
                  'entonces este estado ya no habla del mismo destino.',
    )
    responde = models.BooleanField(default=False)
    latencia_ms = models.PositiveSmallIntegerField(null=True, blank=True)
    ultima_verificacion = models.DateTimeField()
    ultima_respuesta = models.DateTimeField(
        null=True, blank=True,
        help_text='La última vez que contestó. Vacío = nunca contestó desde que se lo '
                  'monitorea, que no es lo mismo que "se cayó recién".',
    )
    estacion_que_sondeo = models.ForeignKey(
        'catalogo.Estacion', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='sondeos_activos',
        help_text='Qué estación hizo el ping. Si esa estación se apaga, el activo deja de '
                  'sondearse — y eso explica un "sin verificar" que si no parecería una caída.',
    )

    class Meta:
        db_table = 'estado_red_activo'
        ordering = ['activo__codigo']
        verbose_name = 'Estado de red de un activo'
        verbose_name_plural = 'Estados de red de activos'

    def __str__(self):
        return f'{self.activo.codigo}: {"responde" if self.responde else "sin respuesta"}'

    # Una verificación más vieja que esto significa que nadie lo está sondeando: la
    # estación de esa farmacia está apagada, o la farmacia no tiene ninguna en línea.
    # Sin este umbral, un dato viejo se leería como un estado actual.
    HORAS_VERIFICACION_VIGENTE = 2

    @property
    def verificacion_vigente(self) -> bool:
        return (timezone.now() - self.ultima_verificacion) <= timedelta(
            hours=self.HORAS_VERIFICACION_VIGENTE,
        )

    @property
    def horas_sin_responder(self):
        """Horas desde la última respuesta, o None si nunca respondió.

        Es el número que distingue "se apagó anoche" (unas horas) de "está fuera de
        servicio hace una semana" — y solo el segundo caso amerita que alguien vaya.
        """
        if self.ultima_respuesta is None:
            return None
        return round((timezone.now() - self.ultima_respuesta).total_seconds() / 3600, 1)


class ServicioPos(models.TextChoices):
    """Los servicios externos de los que depende el punto de venta para vender.

    Salen del `.exe.Config` real del POS: los dos Postgres y `OdooServerUrl` viven en
    `<appSettings>`, y el web service de recargas en `<applicationSettings>`. El agente
    los descubre leyendo ese archivo, no de una lista que alguien mantenga acá.
    """

    PG_LOCAL = 'pg_local', 'PostgreSQL local'
    PG_CENTRAL = 'pg_central', 'PostgreSQL central'
    ODOO = 'odoo', 'Odoo'
    RECARGAS_SOAP = 'recargas_soap', 'Web service de recargas'


class EstadoServicioPos(models.Model):
    """Si un servicio del que depende el POS responde, visto desde la estación.

    Por qué existe: cuando una caja no puede vender, la pregunta es dónde se corta la
    cadena — ¿la base local, el nodo central, Odoo, el web service de recargas? Hasta
    ahora eso se averiguaba entrando al equipo. Este chequeo lo pregunta desde adentro,
    cada pocos minutos, y deja el resultado a la vista.

    Se mide desde la ESTACIÓN y no desde el servidor a propósito: lo que importa no es si
    el servicio está vivo en abstracto, sino si esa caja puede alcanzarlo. Una base
    central sana con la ruta rota desde una farmacia es, para esa farmacia, una base
    caída — y desde el servidor central se vería perfecta.

    Una fila por estación y servicio, sobrescrita en cada chequeo: es el estado actual, no
    un historial. La serie temporal de latencia vive aparte en `MuestraServicioPos`, mismo
    criterio de modelos paralelos por granularidad que `EstadoEnlaceFarmacia` con
    `MuestraRedFarmacia`.

    El agente reporta datos crudos —alcanzable, latencia, mensaje— y NO decide si eso es
    bueno o malo. Esa decisión vive en `ReglaAlerta`, configurable por unidad de negocio
    desde el panel. Si el umbral estuviera en el agente, cambiarlo exigiría redistribuir
    el ejecutable a ~1.800 estaciones en vez de editar una fila.
    """

    # Mismo umbral que EstadoRedActivo, y por el mismo motivo: un estado viejo leído como
    # actual es peor que no tener dato. Si la estación se apaga, su último chequeo queda
    # congelado y diría "todo bien" indefinidamente.
    HORAS_VERIFICACION_VIGENTE = EstadoRedActivo.HORAS_VERIFICACION_VIGENTE

    estacion = models.ForeignKey(
        Estacion, on_delete=models.CASCADE, related_name='servicios_pos',
    )
    servicio = models.CharField(max_length=20, choices=ServicioPos.choices)

    disponible = models.BooleanField(default=False)
    latencia_ms = models.PositiveIntegerField(null=True, blank=True)
    mensaje = models.CharField(
        max_length=300, blank=True,
        help_text='Lo que contestó el servicio: la versión de PostgreSQL, el código HTTP, '
                  'o el error tal cual. Crudo, para poder diagnosticar sin entrar al equipo.',
    )
    endpoint = models.CharField(
        max_length=200, blank=True,
        help_text='A qué se apuntó (host:puerto/base o URL). NUNCA lleva credenciales: '
                  'mismo criterio que EstadoRedActivo, que guarda la IP sondeada y nada más.',
    )
    critico = models.BooleanField(
        default=True,
        help_text='Si sin este servicio la caja no puede vender. Decide si la regla de '
                  'alerta que se dispara es crítica o una advertencia.',
    )

    ultima_verificacion = models.DateTimeField()
    ultima_respuesta = models.DateTimeField(
        null=True, blank=True,
        help_text='La última vez que contestó. Vacío = nunca contestó desde que se lo '
                  'monitorea, que no es lo mismo que "se cayó recién".',
    )

    class Meta:
        db_table = 'estado_servicio_pos'
        ordering = ['estacion__codigo', 'servicio']
        constraints = [
            models.UniqueConstraint(
                fields=['estacion', 'servicio'], name='un_estado_por_estacion_y_servicio',
            ),
        ]
        verbose_name = 'Estado de un servicio del POS'
        verbose_name_plural = 'Estados de los servicios del POS'

    def __str__(self):
        return '%s / %s: %s' % (
            self.estacion.codigo, self.get_servicio_display(),
            'responde' if self.disponible else 'sin respuesta',
        )

    @property
    def nunca_respondio(self) -> bool:
        """Se lo sondea y jamás contestó: no es una caída, es configuración pendiente o
        un servicio dado de baja que el `.exe.Config` del POS sigue nombrando.

        La distinción existe porque tratarlo como caída convierte un estado permanente en
        una alerta que se reabre para siempre. Encontrado el 18-sep-2026 con Odoo: las 9
        estaciones con agente lo medían, ninguna lo había alcanzado nunca, y cada una
        abría su alerta por un servicio que estaba de baja. Mismo criterio que
        `EstadoEnlaceFarmacia.nunca_respondio`.
        """
        return self.ultima_respuesta is None

    @property
    def verificacion_vigente(self) -> bool:
        return (timezone.now() - self.ultima_verificacion) <= timedelta(
            hours=self.HORAS_VERIFICACION_VIGENTE,
        )

    @property
    def horas_sin_responder(self):
        """Horas desde la última respuesta, o None si nunca respondió.

        Distingue "se cayó recién" de "hace una semana que no anda", que son dos
        incidentes distintos aunque se vean igual en una lista.
        """
        if self.ultima_respuesta is None:
            return None
        return round((timezone.now() - self.ultima_respuesta).total_seconds() / 3600, 1)


class MuestraServicioPos(models.Model):
    """Latencia de un servicio del POS en un instante, para poder graficar la tendencia.

    Separada de `EstadoServicioPos` siguiendo el precedente explícito del proyecto
    (`EstadoEnlaceFarmacia` + `MuestraRedFarmacia`): el estado actual se sobrescribe y se
    consulta a cada rato; la serie crece sin parar y se purga. Mezclarlas obligaría a
    elegir entre perder el historial o leer una tabla enorme para pintar un semáforo.

    Solo se guarda cuando el servicio RESPONDE: una latencia nula no es un punto en la
    curva, y graficarla como cero diría que contestó instantáneamente.
    """

    estacion = models.ForeignKey(
        Estacion, on_delete=models.CASCADE, related_name='muestras_servicios_pos',
    )
    servicio = models.CharField(max_length=20, choices=ServicioPos.choices)
    latencia_ms = models.PositiveIntegerField()
    timestamp = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        db_table = 'muestra_servicio_pos'
        ordering = ['-timestamp', '-id']
        indexes = [models.Index(fields=['estacion', 'servicio', '-timestamp'])]
        verbose_name = 'Muestra de un servicio del POS'
        verbose_name_plural = 'Muestras de los servicios del POS'

    def __str__(self):
        return '%s / %s: %d ms' % (self.estacion.codigo, self.servicio, self.latencia_ms)
