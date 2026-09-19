from django.conf import settings
from django.db import models


class PerfilUsuario(models.Model):
    """Datos de un usuario del sistema que no pertenecen a auth.User.

    Reemplaza a UsuariosJpa de InvTICS sin duplicar identidad: si el usuario
    también es un empleado (custodio de activos), se enlaza vía `colaborador`
    en vez de repetir cédula/correo en dos tablas.
    """
    usuario = models.OneToOneField(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='perfil',
    )
    colaborador = models.ForeignKey(
        'activos.Colaborador', on_delete=models.SET_NULL, null=True, blank=True, related_name='perfiles_usuario',
    )
    fcm_token = models.CharField(
        max_length=255, blank=True,
        help_text='Token de push (FCM) del último dispositivo móvil registrado. Sin lógica de envío todavía.',
    )
    unidades_negocio = models.ManyToManyField(
        'catalogo.UnidadNegocio', blank=True, related_name='usuarios',
        help_text='Clientes que este usuario puede ver y accionar. Vacío + '
                  'acceso_todas_unidades=False equivale a no ver ningún dato con tenant.',
    )
    acceso_todas_unidades = models.BooleanField(
        default=False,
        help_text='Para personal interno (soporte/operaciones) que necesita ver todos los '
                  'clientes. `unidades_negocio` se ignora si esto está activo.',
    )
    # Consultar por Telegram y ACCIONAR por Telegram son dos permisos distintos, y este
    # campo es el que los separa. Las consultas (/enlaces, /estado, ...) siguen gobernadas
    # por TELEGRAM_CHAT_IDS_AUTORIZADOS, una lista en el .env. Eso alcanza para leer, pero
    # no para escribir: no dice QUIÉN es cada chat, así que el historial de una ejecución
    # disparada desde ahí no podría nombrar a nadie, y quitarle la acción a una persona
    # significaría quitarle también la consulta.
    #
    # Atando el chat a un usuario real, accionar reusa el RBAC que ya existe —permiso
    # `scripts.add_ejecucionscript` más `verificar_acceso` a la unidad de la estación— y
    # `EjecucionScript.creado_por` queda con una persona, no con una cuenta de servicio.
    telegram_chat_id = models.CharField(
        max_length=32, blank=True, db_index=True,
        help_text='chat_id de Telegram de esta persona. Con esto, sus comandos de acción '
                  'por Telegram se ejecutan con SUS permisos y quedan a su nombre en el '
                  'historial. Vacío = solo puede consultar (si su chat está en '
                  'TELEGRAM_CHAT_IDS_AUTORIZADOS).',
    )

    class Meta:
        db_table = 'perfil_usuario'
        constraints = [
            # Dos personas no pueden compartir chat: si lo hicieran, el historial diría
            # el nombre equivocado. Los vacíos no chocan entre sí (condition).
            models.UniqueConstraint(
                fields=['telegram_chat_id'], condition=~models.Q(telegram_chat_id=''),
                name='un_chat_de_telegram_por_persona',
            ),
        ]
        verbose_name = 'Perfil de usuario'
        verbose_name_plural = 'Perfiles de usuario'

    def __str__(self):
        return str(self.usuario)
