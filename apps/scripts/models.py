import hashlib

from django.conf import settings
from django.db import models

from apps.catalogo.models import Estacion, Farmacia, Grupo


class TipoScript(models.TextChoices):
    POWERSHELL = 'powershell', 'PowerShell'


class Script(models.Model):
    """Biblioteca de scripts reutilizables que se pueden ejecutar en una o varias estaciones.

    `es_adhoc` marca los scripts creados al vuelo desde "ejecutar ahora sin
    guardar en biblioteca": siguen siendo una fila auditada como cualquier
    otra, pero no aparecen en el listado principal de la biblioteca.
    """
    nombre = models.CharField(max_length=150)
    descripcion = models.TextField(blank=True)
    tipo = models.CharField(max_length=15, choices=TipoScript.choices, default=TipoScript.POWERSHELL)
    contenido = models.TextField()
    categoria = models.CharField(max_length=50, blank=True)
    es_adhoc = models.BooleanField(default=False)
    activo = models.BooleanField(default=True)
    creado_por = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='scripts_creados',
    )
    fecha_creacion = models.DateTimeField(auto_now_add=True)
    fecha_modificacion = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'script'
        ordering = ['nombre']

    def __str__(self):
        return self.nombre


class EjecucionScript(models.Model):
    """Una corrida de un Script contra un destino de estaciones (mismo concepto de destino que Despliegue)."""

    class DestinoTipo(models.TextChoices):
        CADENA = 'cadena', 'Toda la cadena'
        GRUPOS = 'grupos', 'Grupos específicos'
        FARMACIAS = 'farmacias', 'Farmacias específicas'
        ESTACIONES = 'estaciones', 'Estaciones específicas'

    class Estado(models.TextChoices):
        PENDIENTE = 'pendiente', 'Pendiente'
        EN_PROGRESO = 'en_progreso', 'En progreso'
        COMPLETADO = 'completado', 'Completado'
        CON_ERRORES = 'con_errores', 'Completado con errores'

    script = models.ForeignKey(Script, on_delete=models.PROTECT, related_name='ejecuciones')
    contenido_snapshot = models.TextField(
        help_text='Copia del contenido del script al momento de ejecutar; no cambia si el script se edita después.',
    )
    sha256 = models.CharField(max_length=64, blank=True, editable=False)

    destino_tipo = models.CharField(max_length=20, choices=DestinoTipo.choices)
    grupos = models.ManyToManyField(Grupo, blank=True, related_name='ejecuciones_script')
    farmacias = models.ManyToManyField(Farmacia, blank=True, related_name='ejecuciones_script')
    estaciones = models.ManyToManyField(Estacion, blank=True, related_name='ejecuciones_script')

    timeout_segundos = models.PositiveIntegerField(default=300)
    parametros = models.JSONField(default=dict, blank=True)
    estado = models.CharField(max_length=15, choices=Estado.choices, default=Estado.PENDIENTE)

    creado_por = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='ejecuciones_script_creadas',
    )
    fecha_creacion = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'ejecucion_script'
        ordering = ['-fecha_creacion']

    def __str__(self):
        return f'Ejecución #{self.pk} de "{self.script.nombre}" ({self.get_estado_display()})'

    def calcular_sha256(self):
        return hashlib.sha256(self.contenido_snapshot.encode('utf-8')).hexdigest()

    def save(self, *args, **kwargs):
        if self.contenido_snapshot and not self.sha256:
            self.sha256 = self.calcular_sha256()
        super().save(*args, **kwargs)


class ResultadoEjecucionScript(models.Model):
    """Estado de una EjecucionScript para una Estacion puntual. Mismo patrón que ResultadoDespliegue."""

    class Estado(models.TextChoices):
        PENDIENTE = 'pendiente', 'Pendiente'
        ENVIADO = 'enviado', 'Enviado'
        EJECUTANDO = 'ejecutando', 'Ejecutando'
        COMPLETADO = 'completado', 'Completado'
        ERROR = 'error', 'Error'
        TIMEOUT = 'timeout', 'Tiempo agotado'

    ejecucion = models.ForeignKey(EjecucionScript, on_delete=models.CASCADE, related_name='resultados')
    estacion = models.ForeignKey(Estacion, on_delete=models.CASCADE, related_name='resultados_script')
    estado = models.CharField(max_length=15, choices=Estado.choices, default=Estado.PENDIENTE)
    exit_code = models.IntegerField(null=True, blank=True)
    stdout = models.TextField(blank=True)
    stderr = models.TextField(blank=True)
    fecha_envio = models.DateTimeField(null=True, blank=True)
    fecha_inicio = models.DateTimeField(null=True, blank=True)
    fecha_fin = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = 'resultado_ejecucion_script'
        unique_together = [('ejecucion', 'estacion')]
        ordering = ['ejecucion', 'estacion']

    def __str__(self):
        return f'{self.ejecucion_id} -> {self.estacion.codigo}: {self.get_estado_display()}'
