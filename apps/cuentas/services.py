"""Envío de push (FCM) a un usuario.

No-op/log hasta que se decida la infraestructura real (Firebase Admin SDK
u otro proveedor) — ver PLAN_MODERNIZACION.md, fase de infraestructura
diferida. `PerfilUsuario.fcm_token` ya existe para cuando se conecte.
"""
import logging

logger = logging.getLogger(__name__)


def enviar_push(*, usuario, titulo, cuerpo, data=None):
    perfil = getattr(usuario, 'perfil', None)
    if perfil is None or not perfil.fcm_token:
        logger.info('Push omitido (sin fcm_token): usuario=%s titulo=%r', usuario, titulo)
        return False
    logger.info(
        'Push simulado (sin proveedor real configurado): usuario=%s token=%s titulo=%r cuerpo=%r data=%s',
        usuario, perfil.fcm_token, titulo, cuerpo, data or {},
    )
    return True
