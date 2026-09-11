"""Apertura autoprovisionada de una farmacia ("cero-touch").

Por qué existe: abrir una farmacia nueva significaba hoy que alguien fuera al sitio y
configurara cada estación a mano — instalar el agente, tildar `monitorear_recursos` y
`es_cache_farmacia`, pedir el despliegue del POS, instalar el antivirus, aprobar el
enrolamiento en el panel y recién después dar de alta los activos en ITAM. Las 8
estaciones del piloto se hicieron así y cada una destapó un bug distinto
(PLAN_MODERNIZACION.md §10). A 700 farmacias eso no escala.

Este módulo no ejecuta trabajo nuevo: orquesta el que ya existe. Un `PasoPlantilla`
delega en `apps.scripts` (ejecución remota), `apps.software` (instalación silenciosa),
`apps.despliegues` (POS) y `apps.activos` (alta en ITAM). Lo que aporta es el *qué, en
qué orden y a qué estación*, y un token de un solo uso que permite que la estación se
enrole ya aprobada sin que nadie la toque.

Sobre el token: el paquete "un clic" anterior llevaba adentro la contraseña MQTT y el
`COMANDO_HMAC_SECRET` compartidos de toda la flota, y se descargaba sin autenticación
(§10-Z; hubo que borrarlo y rotar ambos secretos). `TokenApertura` es la respuesta a
eso: un secreto por estación esperada, de un solo uso, con vencimiento, del que solo se
guarda el hash. Filtrarlo compromete una estación de una apertura, no la flota.
"""
import hashlib
import secrets
from datetime import timedelta

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone

from apps.catalogo.models import Estacion, Farmacia, Grupo, UnidadNegocio

# Un token vale para el tramo de la apertura, no indefinidamente: si un equipo preparado
# no llega a enrolarse en este plazo, el token vence y hay que emitir otro desde el panel
# (queda el rastro de quién lo reemitió). Días, no horas: entre que se prepara la imagen
# y se enchufa el equipo en el local pueden pasar fines de semana.
DIAS_VIGENCIA_TOKEN_DEFAULT = 30


def hashear_token(token_plano: str) -> str:
    """SHA-256 hex del token. Es lo único que se persiste (ver TokenApertura)."""
    return hashlib.sha256(token_plano.encode('utf-8')).hexdigest()


class PlantillaApertura(models.Model):
    """Qué necesita una farmacia de este formato para quedar operativa.

    Versionada a propósito: una `Apertura` guarda con qué versión se ejecutó, así editar
    la plantilla no reescribe la historia de las aperturas ya corridas — mismo criterio
    que `EjecucionScript.contenido_snapshot`.
    """

    nombre = models.CharField(max_length=150)
    descripcion = models.TextField(blank=True)
    version = models.PositiveIntegerField(default=1)

    unidad_negocio = models.ForeignKey(
        UnidadNegocio, on_delete=models.PROTECT, related_name='plantillas_apertura',
        help_text='Cliente dueño de la plantilla. Una apertura solo puede usar plantillas '
                  'de la unidad de negocio de su propia farmacia.',
    )
    grupo = models.ForeignKey(
        Grupo, on_delete=models.PROTECT, null=True, blank=True, related_name='plantillas_apertura',
        help_text='Vacío = aplica a cualquier canal de la unidad de negocio.',
    )
    formato_farmacia = models.CharField(
        max_length=20, choices=Farmacia.FormatoFarmacia.choices, blank=True,
        help_text='Vacío = aplica a cualquier formato. Un autoservicio y un mostrador no '
                  'tienen la misma cantidad de cajas, y de ahí salen los perfiles de estación.',
    )

    activa = models.BooleanField(default=True)
    creado_por = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='plantillas_apertura_creadas',
    )
    fecha_creacion = models.DateTimeField(auto_now_add=True)
    fecha_modificacion = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'plantilla_apertura'
        ordering = ['nombre', '-version']
        unique_together = [('unidad_negocio', 'nombre', 'version')]
        verbose_name = 'Plantilla de apertura'
        verbose_name_plural = 'Plantillas de apertura'

    def __str__(self):
        return f'{self.nombre} v{self.version} ({self.unidad_negocio.codigo})'


class PerfilEstacionPlantilla(models.Model):
    """Una estación que se espera encontrar en la farmacia, y cómo debe quedar configurada.

    El `sufijo` es la segunda mitad del código de estación (`ML001-ADM` -> `ADM`), que es
    como el agente se identifica al enrolarse: de ahí sale a qué perfil corresponde el
    equipo que acaba de aparecer, sin que nadie lo elija en un formulario.

    Solo lleva los dos flags que hoy un humano tilda a mano en el panel. NO lleva
    `pos_servidor`/`pos_bdd`/`pos_puerto`: esos campos de `Estacion` son *observados* — el
    agente los lee del .Config del propio equipo y son el dato con autoridad (ver el
    comentario de `Estacion.pos_servidor`). Escribirlos desde una plantilla pisaría la
    realidad con un deseo. El nodo esperado ya vive en `Grupo.bdd_pos`, y el paso de
    verificación `nodo_pos_coherente` lo contrasta contra lo que el agente reporte.
    """

    class Rol(models.TextChoices):
        CAJA = 'caja', 'Caja'
        ADMINISTRACION = 'administracion', 'Administración'
        SERVIDOR = 'servidor', 'Servidor de farmacia'

    plantilla = models.ForeignKey(
        PlantillaApertura, on_delete=models.CASCADE, related_name='perfiles_estacion',
    )
    sufijo = models.CharField(
        max_length=10,
        help_text='Sufijo del código de estación, sin la farmacia ni el guion (ej. "ADM", "A", "B").',
    )
    rol = models.CharField(max_length=20, choices=Rol.choices, default=Rol.CAJA)
    obligatoria = models.BooleanField(
        default=True,
        help_text='Si está marcada, la apertura no se da por completada hasta que esta '
                  'estación se haya enrolado y terminado sus pasos.',
    )

    monitorear_recursos = models.BooleanField(
        default=False, help_text='Se aplica a la estación al enrolarse (típicamente solo el servidor).',
    )
    es_cache_farmacia = models.BooleanField(
        default=False,
        help_text='Se aplica a la estación al enrolarse. Una sola por farmacia: sirve los '
                  'paquetes por LAN al resto de las cajas.',
    )

    class Meta:
        db_table = 'perfil_estacion_plantilla'
        ordering = ['plantilla', 'sufijo']
        unique_together = [('plantilla', 'sufijo')]
        verbose_name = 'Perfil de estación'
        verbose_name_plural = 'Perfiles de estación'

    def __str__(self):
        return f'{self.plantilla.nombre} / -{self.sufijo} ({self.get_rol_display()})'

    def clean(self):
        if self.sufijo != self.sufijo.upper():
            raise ValidationError({'sufijo': 'El sufijo va en mayúsculas (el código de estación es MAYÚSCULA).'})


class TipoPaso(models.TextChoices):
    SCRIPT = 'script', 'Ejecutar script'
    SOFTWARE = 'software', 'Instalar software del catálogo'
    DESPLIEGUE_POS = 'despliegue_pos', 'Instalar el POS (despliegue ya aprobado)'
    VERIFICACION = 'verificacion', 'Verificar estado reportado'
    ACTIVO_ITAM = 'activo_itam', 'Dar de alta en el inventario (ITAM)'
    MANUAL = 'manual', 'Paso manual (fuera de SAIDSOFT)'


class TipoVerificacion(models.TextChoices):
    """Comprobaciones que se resuelven contra datos que la estación YA reporta.

    Ninguna inventa un mecanismo nuevo: todas leen campos que el agente llena por su
    cuenta (BitLocker, Windows Update, inventario de software) o que se derivan del
    catálogo (versión objetivo del grupo, coherencia del nodo del POS).
    """
    BITLOCKER = 'bitlocker', 'Disco cifrado con BitLocker'
    SOFTWARE_PRESENTE = 'software_presente', 'Software presente en el inventario'
    WINDOWS_UPDATE_ESCANEADO = 'windows_update_escaneado', 'Windows Update escaneado al menos una vez'
    VERSION_POS_OBJETIVO = 'version_pos_objetivo', 'POS en la versión objetivo de su grupo'
    NODO_POS_COHERENTE = 'nodo_pos_coherente', 'El nodo del POS coincide con el grupo'


class PasoPlantilla(models.Model):
    """Un paso de la plantilla. El trabajo real lo hacen los módulos que ya existen."""

    plantilla = models.ForeignKey(PlantillaApertura, on_delete=models.CASCADE, related_name='pasos')
    orden = models.PositiveIntegerField()
    nombre = models.CharField(max_length=150)
    tipo = models.CharField(max_length=20, choices=TipoPaso.choices)

    script = models.ForeignKey(
        'scripts.Script', on_delete=models.PROTECT, null=True, blank=True, related_name='pasos_apertura',
        help_text='Requerido si el tipo es "Ejecutar script".',
    )
    version_aplicacion = models.ForeignKey(
        'software.VersionAplicacion', on_delete=models.PROTECT, null=True, blank=True,
        related_name='pasos_apertura', help_text='Requerido si el tipo es "Instalar software del catálogo".',
    )
    despliegue = models.ForeignKey(
        'despliegues.Despliegue', on_delete=models.PROTECT, null=True, blank=True,
        related_name='pasos_apertura',
        help_text='Requerido si el tipo es "Instalar el POS". Debe ser un despliegue ya '
                  'aprobado: la apertura no crea paquetes nuevos ni saltea la aprobación de '
                  'un despliegue, solo suma la estación recién enrolada a uno que ya pasó '
                  'por su propio control de cuatro ojos.',
    )
    verificacion = models.CharField(
        max_length=30, choices=TipoVerificacion.choices, blank=True,
        help_text='Requerido si el tipo es "Verificar estado reportado".',
    )
    parametro = models.CharField(
        max_length=150, blank=True,
        help_text='Dato suelto del paso. Para la verificación "software presente", el nombre '
                  '(o parte) del programa que se busca en el inventario, ej. "ESET".',
    )

    rol_estacion = models.CharField(
        max_length=20, choices=PerfilEstacionPlantilla.Rol.choices, blank=True,
        help_text='Vacío = el paso corre en TODAS las estaciones de la apertura. Con valor, '
                  'solo en las de ese rol. Los pasos manuales son de la farmacia, no de una '
                  'estación, y este campo se ignora.',
    )
    obligatorio = models.BooleanField(
        default=True, help_text='Un paso no obligatorio que falla no impide completar la apertura.',
    )
    timeout_segundos = models.PositiveIntegerField(default=300)

    class Meta:
        db_table = 'paso_plantilla_apertura'
        ordering = ['plantilla', 'orden']
        unique_together = [('plantilla', 'orden')]
        verbose_name = 'Paso de plantilla'
        verbose_name_plural = 'Pasos de plantilla'

    def __str__(self):
        return f'{self.orden}. {self.nombre} ({self.get_tipo_display()})'

    def clean(self):
        """Un paso al que le falta su referencia no falla al guardarlo sino recién en mitad
        de una apertura real, cuando ya hay un técnico esperando en el local."""
        requeridos = {
            TipoPaso.SCRIPT: ('script', 'Un paso de tipo script necesita un script.'),
            TipoPaso.SOFTWARE: ('version_aplicacion', 'Un paso de software necesita una versión del catálogo.'),
            TipoPaso.DESPLIEGUE_POS: ('despliegue', 'Un paso de POS necesita un despliegue aprobado.'),
            TipoPaso.VERIFICACION: ('verificacion', 'Un paso de verificación necesita qué verificar.'),
        }
        if self.tipo in requeridos:
            campo, mensaje = requeridos[self.tipo]
            if not getattr(self, f'{campo}_id', None) and not getattr(self, campo, None):
                raise ValidationError({campo: mensaje})
        if (
            self.tipo == TipoPaso.VERIFICACION
            and self.verificacion == TipoVerificacion.SOFTWARE_PRESENTE
            and not self.parametro
        ):
            raise ValidationError({'parametro': 'Indicá qué programa se busca en el inventario (ej. "ESET").'})


class Apertura(models.Model):
    """Una apertura concreta: esta plantilla, aplicada a esta farmacia, en esta fecha."""

    class Estado(models.TextChoices):
        BORRADOR = 'borrador', 'Borrador'
        PENDIENTE_APROBACION = 'pendiente_aprobacion', 'Pendiente de aprobación'
        APROBADA = 'aprobada', 'Aprobada (esperando equipos)'
        EN_CURSO = 'en_curso', 'En curso'
        COMPLETADA = 'completada', 'Completada'
        CANCELADA = 'cancelada', 'Cancelada'

    # Estados en los que la apertura todavía puede recibir equipos: una farmacia no puede
    # tener dos de estas abiertas a la vez (ver la constraint de Meta), porque entonces un
    # equipo que se enrola no tendría a cuál de las dos pertenecer.
    ESTADOS_VIGENTES = [
        Estado.BORRADOR, Estado.PENDIENTE_APROBACION, Estado.APROBADA, Estado.EN_CURSO,
    ]

    farmacia = models.ForeignKey(Farmacia, on_delete=models.PROTECT, related_name='aperturas')
    plantilla = models.ForeignKey(PlantillaApertura, on_delete=models.PROTECT, related_name='aperturas')
    plantilla_version = models.PositiveIntegerField(
        help_text='Versión de la plantilla con la que se generó esta apertura. Copiada al crear: '
                  'editar la plantilla después no reescribe lo que esta apertura ejecutó.',
    )

    fecha_prevista = models.DateField(help_text='Fecha prevista de apertura del local.')
    estado = models.CharField(max_length=25, choices=Estado.choices, default=Estado.BORRADOR)
    observacion = models.TextField(blank=True)

    creado_por = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='aperturas_creadas',
    )
    aprobado_por = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True,
        related_name='aperturas_aprobadas',
    )
    fecha_creacion = models.DateTimeField(auto_now_add=True)
    fecha_aprobacion = models.DateTimeField(null=True, blank=True)
    fecha_completada = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = 'apertura'
        ordering = ['-fecha_prevista', '-fecha_creacion']
        constraints = [
            models.UniqueConstraint(
                fields=['farmacia'],
                condition=models.Q(estado__in=[
                    'borrador', 'pendiente_aprobacion', 'aprobada', 'en_curso',
                ]),
                name='una_apertura_vigente_por_farmacia',
            ),
        ]
        permissions = [
            # Mismo criterio que `aprobar_despliegue` y `aprobar_ejecucionscript`: aprobar
            # una apertura habilita la emisión de tokens que auto-aprueban estaciones y
            # dispara scripts contra un sitio entero. Es la misma superficie de riesgo que
            # un despliegue a una farmacia, y la regla de cuatro ojos aplica igual.
            ('aprobar_apertura', 'Puede aprobar una apertura pendiente de aprobación'),
            # Separado de "aprobar": ver el secreto en claro es una capacidad distinta de
            # autorizar la apertura, y el token es lo único que se interpone entre la red y
            # una estación auto-aprobada.
            ('emitir_token_apertura', 'Puede emitir y ver los tokens de enrolamiento de una apertura'),
        ]

    def __str__(self):
        return f'Apertura {self.farmacia.codigo} — {self.fecha_prevista:%Y-%m-%d} ({self.get_estado_display()})'

    @property
    def unidad_negocio(self):
        """Tenant de la apertura. Lo deriva de la farmacia, que es su única fuente:
        `apps.auditoria.models._resolver_unidad_negocio` la encuentra por esta vía."""
        return self.farmacia.unidad_negocio


class PasoApertura(models.Model):
    """Instancia de un `PasoPlantilla` dentro de una apertura.

    Los pasos de estación se materializan recién cuando la estación de ese rol se enrola
    (antes no existe a qué apuntarlos: las estaciones de una farmacia nueva no están en la
    base todavía). Los pasos manuales, que son del sitio y no de un equipo, se materializan
    al crear la apertura para que el checklist esté visible desde el día uno.
    """

    class Estado(models.TextChoices):
        PENDIENTE = 'pendiente', 'Pendiente'
        EN_CURSO = 'en_curso', 'En curso'
        COMPLETADO = 'completado', 'Completado'
        ERROR = 'error', 'Error'
        OMITIDO = 'omitido', 'Omitido'

    ESTADOS_TERMINALES = [Estado.COMPLETADO, Estado.ERROR, Estado.OMITIDO]

    apertura = models.ForeignKey(Apertura, on_delete=models.CASCADE, related_name='pasos')
    paso_plantilla = models.ForeignKey(PasoPlantilla, on_delete=models.PROTECT, related_name='instancias')
    estacion = models.ForeignKey(
        Estacion, on_delete=models.CASCADE, null=True, blank=True, related_name='pasos_apertura',
        help_text='Vacío en los pasos manuales, que son de la farmacia y no de un equipo.',
    )

    # Copias del paso de plantilla al momento de materializarlo, por el mismo motivo que
    # `EjecucionScript.contenido_snapshot`: editar la plantilla no debe reescribir lo que
    # esta apertura mostró e hizo.
    orden = models.PositiveIntegerField()
    nombre = models.CharField(max_length=150)
    tipo = models.CharField(max_length=20, choices=TipoPaso.choices)

    estado = models.CharField(max_length=15, choices=Estado.choices, default=Estado.PENDIENTE)
    detalle = models.TextField(blank=True)

    # Vínculo con el objeto que hace el trabajo real, para poder abrir su ficha desde la
    # apertura en vez de duplicar acá el estado que ese módulo ya lleva.
    ejecucion_script = models.ForeignKey(
        'scripts.EjecucionScript', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='pasos_apertura',
    )
    solicitud_instalacion = models.ForeignKey(
        'software.SolicitudInstalacion', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='pasos_apertura',
    )

    fecha_inicio = models.DateTimeField(null=True, blank=True)
    fecha_fin = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = 'paso_apertura'
        ordering = ['apertura', 'estacion', 'orden']
        unique_together = [('apertura', 'paso_plantilla', 'estacion')]
        verbose_name = 'Paso de apertura'
        verbose_name_plural = 'Pasos de apertura'

    def __str__(self):
        donde = self.estacion.codigo if self.estacion_id else self.apertura.farmacia.codigo
        return f'{donde}: {self.nombre} ({self.get_estado_display()})'


class EventoApertura(models.Model):
    """Línea de tiempo inmutable de un `PasoApertura`. No se edita ni se borra.

    Mismo contrato que `EventoDespliegue`: es el registro de qué pasó en el sitio, y tiene
    que seguir siendo legible cuando alguien pregunte, meses después, por qué esa farmacia
    abrió con una caja sin cifrar.
    """

    class Paso(models.TextChoices):
        CREADO = 'creado', 'Paso creado'
        ENROLADA = 'enrolada', 'Estación enrolada por token de apertura'
        ENVIADO = 'enviado', 'Trabajo enviado a la estación'
        COMPLETADO = 'completado', 'Completado'
        ERROR = 'error', 'Error'
        OMITIDO = 'omitido', 'Omitido'

    paso = models.ForeignKey(PasoApertura, on_delete=models.CASCADE, related_name='eventos')
    evento = models.CharField(max_length=20, choices=Paso.choices)
    detalle = models.TextField(blank=True)
    timestamp = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'evento_apertura'
        # 'pk' como desempate por el mismo motivo que EventoDespliegue: dos eventos del
        # mismo paso creados en el mismo tick dejan .first()/.last() no determinístico.
        ordering = ['paso', 'timestamp', 'pk']
        verbose_name = 'Evento de apertura'
        verbose_name_plural = 'Eventos de apertura'

    def __str__(self):
        return f'{self.paso} — {self.get_evento_display()} @ {self.timestamp:%Y-%m-%d %H:%M:%S}'

    def delete(self, *args, **kwargs):
        raise NotImplementedError('EventoApertura es inmutable: no se puede eliminar.')


class TokenApertura(models.Model):
    """Secreto de un solo uso que autoriza a UNA estación a enrolarse ya aprobada.

    Del token solo se guarda el hash (`hashear_token`). El valor en claro existe una única
    vez, cuando se emite: se muestra al operador para que lo copie al `config.txt` del
    equipo y no se puede volver a consultar. Es deliberado — ver §10-Z, donde el paquete de
    instalación con secretos compartidos adentro quedó publicado sin autenticación y hubo
    que rotar las credenciales de toda la flota.

    Qué protege, concretamente: sin token, una estación que se enrola queda PENDIENTE y un
    humano con permiso `aprobar_estacion` la habilita. El token reemplaza ese clic, y por
    eso vale para una sola estación, vence, y queda atado al `hardware_id` del primer
    equipo que lo usa.
    """

    apertura = models.ForeignKey(Apertura, on_delete=models.CASCADE, related_name='tokens')
    perfil = models.ForeignKey(
        PerfilEstacionPlantilla, on_delete=models.PROTECT, related_name='tokens',
        help_text='Qué estación de la plantilla se espera que use este token.',
    )

    token_hash = models.CharField(max_length=64, unique=True, editable=False)
    prefijo = models.CharField(
        max_length=8, editable=False,
        help_text='Primeros caracteres del token, para poder identificarlo en el panel sin revelarlo.',
    )

    expira_en = models.DateTimeField()
    usado_en = models.DateTimeField(null=True, blank=True)
    estacion = models.ForeignKey(
        Estacion, on_delete=models.SET_NULL, null=True, blank=True, related_name='tokens_apertura',
        help_text='La estación que consumió este token.',
    )
    hardware_id = models.CharField(
        max_length=100, blank=True, help_text='hardware_id del equipo que consumió el token.',
    )
    revocado = models.BooleanField(default=False)

    creado_por = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='tokens_apertura_emitidos',
    )
    fecha_creacion = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'token_apertura'
        ordering = ['apertura', 'perfil__sufijo']
        verbose_name = 'Token de apertura'
        verbose_name_plural = 'Tokens de apertura'

    def __str__(self):
        return f'Token {self.prefijo}… para {self.apertura.farmacia.codigo}-{self.perfil.sufijo}'

    @property
    def vigente(self) -> bool:
        return not self.revocado and self.usado_en is None and self.expira_en > timezone.now()

    @property
    def motivo_no_vigente(self) -> str:
        """Por qué este token no sirve. Vacío si sí sirve. Sale al log del worker cuando se
        rechaza un enrolamiento: sin esto, un token vencido y uno ya usado se ven igual
        desde el sitio ("no me enrola") y hay que adivinar cuál de los dos es."""
        if self.revocado:
            return 'revocado'
        if self.usado_en is not None:
            return 'ya usado'
        if self.expira_en <= timezone.now():
            return 'vencido'
        return ''

    @classmethod
    def emitir(cls, *, apertura, perfil, usuario, dias_vigencia=DIAS_VIGENCIA_TOKEN_DEFAULT):
        """Crea el token y devuelve `(instancia, token_en_claro)`.

        El valor en claro NO se persiste: si se pierde, se revoca este y se emite otro.
        """
        token_plano = secrets.token_urlsafe(32)
        instancia = cls.objects.create(
            apertura=apertura, perfil=perfil, creado_por=usuario,
            token_hash=hashear_token(token_plano), prefijo=token_plano[:8],
            expira_en=timezone.now() + timedelta(days=dias_vigencia),
        )
        return instancia, token_plano
