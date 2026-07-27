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

    class Meta:
        db_table = 'perfil_usuario'
        verbose_name = 'Perfil de usuario'
        verbose_name_plural = 'Perfiles de usuario'

    def __str__(self):
        return str(self.usuario)
