import hashlib

from django.conf import settings
from django.db import models

from apps.catalogo.models import Estacion, Farmacia, Grupo, UnidadNegocio


def ruta_archivo_despliegue(instance, filename):
    return f'despliegues/{instance.version}/{filename}'


class Despliegue(models.Model):
    """Un envío de actualización (.zip) a toda la cadena, a grupos o a farmacias/estaciones puntuales."""

    class ModoAplicacion(models.TextChoices):
        INMEDIATO = 'inmediato', 'Aplicar inmediatamente al descargar'
        VENTANA = 'ventana', 'Descargar ya, aplicar en ventana programada'
        CIERRE_POS = 'cierre_pos', 'Aplicar cuando el POS se cierre'

    class DestinoTipo(models.TextChoices):
        CADENA = 'cadena', 'Toda la cadena'
        GRUPOS = 'grupos', 'Grupos específicos'
        FARMACIAS = 'farmacias', 'Farmacias específicas'
        ESTACIONES = 'estaciones', 'Estaciones específicas'

    class Estado(models.TextChoices):
        BORRADOR = 'borrador', 'Borrador'
        PENDIENTE_APROBACION = 'pendiente_aprobacion', 'Pendiente de aprobación'
        APROBADO = 'aprobado', 'Aprobado'
        PUBLICANDO = 'publicando', 'Publicando'
        PAUSADO = 'pausado', 'Pausado'
        COMPLETADO = 'completado', 'Completado'
        CANCELADO = 'cancelado', 'Cancelado'

    version = models.CharField(max_length=30, help_text='Versión del POS que contiene este paquete, ej. 4.2.1')
    archivo = models.FileField(upload_to=ruta_archivo_despliegue)
    sha256 = models.CharField(max_length=64, blank=True, editable=False)
    descripcion = models.TextField(blank=True)

    modo_aplicacion = models.CharField(max_length=20, choices=ModoAplicacion.choices)
    ventana_fecha_hora = models.DateTimeField(
        null=True, blank=True,
        help_text='Requerido si el modo de aplicación es "ventana programada".',
    )

    unidad_negocio = models.ForeignKey(
        UnidadNegocio, on_delete=models.PROTECT, related_name='despliegues',
        help_text='Cliente al que se dirige este despliegue. "Toda la cadena" significa toda '
                  'la cadena de esta unidad de negocio, nunca de otras.',
    )
    destino_tipo = models.CharField(max_length=20, choices=DestinoTipo.choices)
    grupos = models.ManyToManyField(Grupo, blank=True, related_name='despliegues')
    farmacias = models.ManyToManyField(Farmacia, blank=True, related_name='despliegues')
    estaciones = models.ManyToManyField(Estacion, blank=True, related_name='despliegues')

    estado = models.CharField(max_length=25, choices=Estado.choices, default=Estado.BORRADOR)
    umbral_error_pct = models.DecimalField(
        max_digits=5, decimal_places=2,
        default=settings.DESPLIEGUE_UMBRAL_ERROR_PCT_DEFAULT,
        help_text='% de estaciones en error a partir del cual el despliegue se pausa automáticamente.',
    )
    freno_omitido = models.BooleanField(
        default=False,
        help_text='Si el operador reanudó a pesar del freno automático: no se vuelve a frenar por error.',
    )

    creado_por = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='despliegues_creados',
    )
    aprobado_por = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True,
        related_name='despliegues_aprobados',
    )

    fecha_creacion = models.DateTimeField(auto_now_add=True)
    fecha_publicacion = models.DateTimeField(null=True, blank=True)

    despliegue_origen = models.ForeignKey(
        'self', on_delete=models.SET_NULL, null=True, blank=True, related_name='promovidos',
        help_text='Si este despliegue es la promoción de un anillo anterior a uno más amplio.',
    )

    class Meta:
        db_table = 'despliegue'
        ordering = ['-fecha_creacion']

    def __str__(self):
        return f'Despliegue {self.version} ({self.get_estado_display()})'

    def calcular_sha256(self):
        hasher = hashlib.sha256()
        self.archivo.seek(0)
        for chunk in self.archivo.chunks():
            hasher.update(chunk)
        self.archivo.seek(0)
        return hasher.hexdigest()

    def save(self, *args, **kwargs):
        if self.archivo and not self.sha256:
            self.sha256 = self.calcular_sha256()
        super().save(*args, **kwargs)

    def resolver_estaciones_destino(self):
        """Devuelve el queryset de Estacion que corresponde al destino configurado."""
        from apps.catalogo.services import resolver_estaciones
        return resolver_estaciones(
            self.destino_tipo, unidad_negocio=self.unidad_negocio, grupos=self.grupos.all(),
            farmacias=self.farmacias.all(), estaciones=self.estaciones.all(),
        )


class ResultadoDespliegue(models.Model):
    """Estado de un Despliegue para una Estacion puntual."""

    class Estado(models.TextChoices):
        PENDIENTE = 'pendiente', 'Pendiente'
        PUBLICADO = 'publicado', 'Publicado'
        DESCARGANDO = 'descargando', 'Descargando'
        DESCARGADO = 'descargado', 'Descargado'
        VERIFICADO = 'verificado', 'Hash verificado'
        APLICANDO = 'aplicando', 'Aplicando'
        APLICADO = 'aplicado', 'Aplicado'
        ERROR = 'error', 'Error'
        ROLLBACK = 'rollback', 'Rollback ejecutado'

    despliegue = models.ForeignKey(Despliegue, on_delete=models.CASCADE, related_name='resultados')
    estacion = models.ForeignKey(Estacion, on_delete=models.CASCADE, related_name='resultados_despliegue')
    estado = models.CharField(max_length=20, choices=Estado.choices, default=Estado.PENDIENTE)
    version_previa = models.CharField(max_length=30, blank=True)
    version_nueva = models.CharField(max_length=30, blank=True)
    detalle_error = models.TextField(blank=True)
    fecha_actualizacion = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'resultado_despliegue'
        unique_together = [('despliegue', 'estacion')]
        ordering = ['despliegue', 'estacion']

    def __str__(self):
        return f'{self.despliegue.version} -> {self.estacion.codigo}: {self.get_estado_display()}'


class EventoDespliegue(models.Model):
    """Línea de tiempo inmutable de un ResultadoDespliegue. No se edita ni se borra."""

    class Paso(models.TextChoices):
        PUBLICADO = 'publicado', 'Publicado por el panel'
        RECIBIDO = 'recibido', 'Recibido por el agente'
        DESCARGADO = 'descargado', 'Descargado'
        HASH_VERIFICADO = 'hash_verificado', 'Hash verificado'
        POS_CERRADO = 'pos_cerrado', 'POS cerrado'
        APLICADO = 'aplicado', 'Archivos aplicados'
        POS_RELANZADO = 'pos_relanzado', 'POS relanzado'
        OK = 'ok', 'Confirmado OK'
        ERROR = 'error', 'Error'
        ROLLBACK = 'rollback', 'Rollback ejecutado'

    resultado = models.ForeignKey(ResultadoDespliegue, on_delete=models.CASCADE, related_name='eventos')
    paso = models.CharField(max_length=20, choices=Paso.choices)
    detalle = models.TextField(blank=True)
    timestamp = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'evento_despliegue'
        ordering = ['resultado', 'timestamp']

    def __str__(self):
        return f'{self.resultado} — {self.get_paso_display()} @ {self.timestamp:%Y-%m-%d %H:%M:%S}'

    def delete(self, *args, **kwargs):
        raise NotImplementedError('EventoDespliegue es inmutable: no se puede eliminar.')
