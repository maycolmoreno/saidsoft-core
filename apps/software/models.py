"""Catálogo de software instalable en estaciones bajo demanda (Fase 1: núcleo + admin;
el panel HTMX llega en Fase 2).

Dos niveles, a diferencia de Despliegue (que es "una fila = una versión suelta"):
AplicacionCatalogo es la entrada del catálogo (persiste entre versiones), Version
Aplicacion es cada versión instalable concreta. La distribución (SolicitudInstalacion/
ResultadoInstalacion/EventoInstalacion) reutiliza el mismo patrón de destino y de
evento inmutable que ya usan Despliegue y EjecucionScript — mismo `resolver_estaciones`,
mismo concepto de cadena/grupos/farmacias/estaciones.

No hay aprobación de cuatro ojos aquí a propósito (a diferencia de Despliegue): instalar
software es una operación de menor radio que actualizar el POS de toda la cadena — más
parecido en riesgo a Scripts (auditado, pero sin segundo aprobador obligatorio). Si en la
práctica hace falta, se agrega después sin romper el modelo.
"""
import hashlib

from django.conf import settings
from django.db import models

from apps.catalogo.models import Estacion, Farmacia, Grupo, UnidadNegocio


class AplicacionCatalogo(models.Model):
    """Entrada del catálogo: una aplicación instalable, independiente de su versión."""

    nombre = models.CharField(max_length=150)
    fabricante = models.CharField(max_length=150, blank=True)
    categoria = models.CharField(max_length=50, blank=True)
    descripcion = models.TextField(blank=True)
    comando_deteccion = models.TextField(
        blank=True,
        help_text='PowerShell opcional que el agente corre antes de instalar, para saber si '
                  'ya está presente y qué versión tiene — evita reinstalar innecesariamente. '
                  'Sin implementar todavía del lado del agente (ver README).',
    )
    unidad_negocio = models.ForeignKey(
        UnidadNegocio, on_delete=models.PROTECT, null=True, blank=True, related_name='aplicaciones_catalogo',
        help_text='Vacío = aplicación compartida, visible e instalable por cualquier cliente. '
                  'Con valor = privada de esa unidad de negocio (mismo criterio que Script).',
    )
    activo = models.BooleanField(default=True)
    creado_por = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='aplicaciones_catalogo_creadas',
    )
    fecha_creacion = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'aplicacion_catalogo'
        ordering = ['nombre']
        verbose_name = 'Aplicación de catálogo'
        verbose_name_plural = 'Aplicaciones de catálogo'

    def __str__(self):
        return self.nombre


def ruta_instalador(instance, filename):
    return f'software/{instance.aplicacion_id}/{instance.version}/{filename}'


class VersionAplicacion(models.Model):
    """Una versión instalable concreta de una AplicacionCatalogo.

    El instalador es un archivo real (mismo patrón que Despliegue.archivo): se hashea al
    guardar, el agente nunca debe confiar en un hash que le llegue por otro lado — solo
    en el que este modelo calculó al recibir el archivo.
    """

    aplicacion = models.ForeignKey(AplicacionCatalogo, on_delete=models.CASCADE, related_name='versiones')
    version = models.CharField(max_length=50)
    instalador = models.FileField(upload_to=ruta_instalador)
    sha256 = models.CharField(max_length=64, blank=True, editable=False)
    tamanio_bytes = models.PositiveBigIntegerField(null=True, blank=True, editable=False)
    comando_instalacion_silenciosa = models.TextField(
        help_text='Ej. msiexec /i "{archivo}" /qn /norestart. "{archivo}" se reemplaza por la '
                  'ruta local donde el agente guardó el instalador descargado.',
    )
    comando_desinstalacion = models.TextField(blank=True)
    argumentos_adicionales = models.CharField(max_length=500, blank=True)
    fecha_publicacion = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'version_aplicacion'
        ordering = ['-fecha_publicacion']
        unique_together = [('aplicacion', 'version')]
        verbose_name = 'Versión de aplicación'
        verbose_name_plural = 'Versiones de aplicación'

    def __str__(self):
        return f'{self.aplicacion.nombre} {self.version}'

    def calcular_sha256(self):
        hasher = hashlib.sha256()
        self.instalador.seek(0)
        for chunk in self.instalador.chunks():
            hasher.update(chunk)
        self.instalador.seek(0)
        return hasher.hexdigest()

    def save(self, *args, **kwargs):
        if self.instalador and not self.sha256:
            self.sha256 = self.calcular_sha256()
            self.tamanio_bytes = self.instalador.size
        super().save(*args, **kwargs)


class TipoAccionInstalacion(models.TextChoices):
    INSTALAR = 'instalar', 'Instalar'
    ACTUALIZAR = 'actualizar', 'Actualizar'
    DESINSTALAR = 'desinstalar', 'Desinstalar'


class DestinoTipo(models.TextChoices):
    CADENA = 'cadena', 'Toda la cadena'
    GRUPOS = 'grupos', 'Grupos específicos'
    FARMACIAS = 'farmacias', 'Farmacias específicas'
    ESTACIONES = 'estaciones', 'Estaciones específicas'


class EstadoSolicitud(models.TextChoices):
    BORRADOR = 'borrador', 'Borrador'
    PUBLICANDO = 'publicando', 'Publicando'
    COMPLETADO = 'completado', 'Completado'
    CANCELADO = 'cancelado', 'Cancelado'


class SolicitudInstalacion(models.Model):
    """Un envío de instalación/actualización/desinstalación de una VersionAplicacion a un
    destino. Mismo concepto de destino que Despliegue y EjecucionScript, resuelto por la
    misma función compartida (apps.catalogo.services.resolver_estaciones) — nunca
    devuelve estaciones de un tenant distinto al de `unidad_negocio`, ni siquiera si un
    grupo/canal está compartido entre varias unidades de negocio.
    """

    version_aplicacion = models.ForeignKey(
        VersionAplicacion, on_delete=models.PROTECT, related_name='solicitudes',
    )
    accion = models.CharField(
        max_length=15, choices=TipoAccionInstalacion.choices, default=TipoAccionInstalacion.INSTALAR,
    )

    unidad_negocio = models.ForeignKey(
        UnidadNegocio, on_delete=models.PROTECT, related_name='solicitudes_instalacion',
        help_text='Cliente al que se dirige. "Toda la cadena" significa toda la cadena de '
                  'esta unidad de negocio, nunca de otras.',
    )
    destino_tipo = models.CharField(max_length=20, choices=DestinoTipo.choices)
    grupos = models.ManyToManyField(Grupo, blank=True, related_name='solicitudes_instalacion')
    farmacias = models.ManyToManyField(Farmacia, blank=True, related_name='solicitudes_instalacion')
    estaciones = models.ManyToManyField(Estacion, blank=True, related_name='solicitudes_instalacion')

    estado = models.CharField(max_length=15, choices=EstadoSolicitud.choices, default=EstadoSolicitud.BORRADOR)
    creado_por = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='solicitudes_instalacion_creadas',
    )
    fecha_creacion = models.DateTimeField(auto_now_add=True)
    fecha_publicacion = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = 'solicitud_instalacion'
        ordering = ['-fecha_creacion']
        verbose_name = 'Solicitud de instalación'
        verbose_name_plural = 'Solicitudes de instalación'

    def __str__(self):
        return f'{self.get_accion_display()} {self.version_aplicacion} ({self.get_estado_display()})'

    def resolver_estaciones_destino(self):
        """Devuelve el queryset de Estacion que corresponde al destino configurado."""
        from apps.catalogo.services import resolver_estaciones
        return resolver_estaciones(
            self.destino_tipo, unidad_negocio=self.unidad_negocio, grupos=self.grupos.all(),
            farmacias=self.farmacias.all(), estaciones=self.estaciones.all(),
        )


class ResultadoInstalacion(models.Model):
    """Estado de una SolicitudInstalacion para una Estacion puntual. Mismo patrón que
    ResultadoDespliegue/ResultadoEjecucionScript."""

    class Estado(models.TextChoices):
        PENDIENTE = 'pendiente', 'Pendiente'
        DESCARGANDO = 'descargando', 'Descargando'
        DESCARGADO = 'descargado', 'Descargado'
        VERIFICADO = 'verificado', 'Hash verificado'
        INSTALANDO = 'instalando', 'Instalando'
        INSTALADO = 'instalado', 'Instalado'
        ERROR = 'error', 'Error'

    solicitud = models.ForeignKey(SolicitudInstalacion, on_delete=models.CASCADE, related_name='resultados')
    estacion = models.ForeignKey(Estacion, on_delete=models.CASCADE, related_name='resultados_instalacion')
    estado = models.CharField(max_length=15, choices=Estado.choices, default=Estado.PENDIENTE)
    version_previa_detectada = models.CharField(max_length=50, blank=True)
    version_instalada = models.CharField(max_length=50, blank=True)
    detalle_error = models.TextField(blank=True)
    fecha_actualizacion = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'resultado_instalacion'
        unique_together = [('solicitud', 'estacion')]
        ordering = ['solicitud', 'estacion']

    def __str__(self):
        return f'{self.solicitud_id} -> {self.estacion.codigo}: {self.get_estado_display()}'


class EventoInstalacion(models.Model):
    """Línea de tiempo inmutable de un ResultadoInstalacion. No se edita ni se borra.
    Mismo patrón que EventoDespliegue — sin los pasos específicos de POS
    (pos_cerrado/pos_relanzado) que no le sirven a instalar software genérico."""

    class Paso(models.TextChoices):
        PUBLICADO = 'publicado', 'Publicado por el panel'
        RECIBIDO = 'recibido', 'Recibido por el agente'
        DESCARGADO = 'descargado', 'Descargado'
        HASH_VERIFICADO = 'hash_verificado', 'Hash verificado'
        INSTALANDO = 'instalando', 'Instalando'
        INSTALADO = 'instalado', 'Instalado'
        ERROR = 'error', 'Error'

    resultado = models.ForeignKey(ResultadoInstalacion, on_delete=models.CASCADE, related_name='eventos')
    paso = models.CharField(max_length=20, choices=Paso.choices)
    detalle = models.TextField(blank=True)
    timestamp = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'evento_instalacion'
        ordering = ['resultado', 'timestamp']
        verbose_name = 'Evento de instalación'
        verbose_name_plural = 'Eventos de instalación'

    def __str__(self):
        return f'{self.resultado} — {self.get_paso_display()} @ {self.timestamp:%Y-%m-%d %H:%M:%S}'

    def delete(self, *args, **kwargs):
        raise NotImplementedError('EventoInstalacion es inmutable: no se puede eliminar.')
