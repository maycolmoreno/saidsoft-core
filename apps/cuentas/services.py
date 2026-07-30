"""Envío de push (FCM) a un usuario, y scoping de datos por unidad de negocio (tenant).

No-op/log hasta que se decida la infraestructura real (Firebase Admin SDK
u otro proveedor) — ver PLAN_MODERNIZACION.md, fase de infraestructura
diferida. `PerfilUsuario.fcm_token` ya existe para cuando se conecte.
"""
import logging

from django.core.exceptions import PermissionDenied
from django.db.models import Q

from apps.catalogo.models import UnidadNegocio

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


# --- Scoping por unidad de negocio (tenant) ---
#
# Un usuario ve/puede accionar sobre las unidades de negocio de
# `perfil.unidades_negocio`, salvo que tenga `perfil.acceso_todas_unidades` o sea
# superusuario (equipo interno SAIDSOFT/soporte). Centralizado aquí y reusado desde
# panel views, forms y admin.py — un solo lugar de verdad para no repetir (y
# desincronizar) el mismo criterio en cada sitio, exactamente el bug típico de
# "se filtró en la lista pero no en el detalle".

def usuario_tiene_acceso_total(user) -> bool:
    """True si `user` puede ver todas las unidades de negocio (equipo interno)."""
    if user is None or not getattr(user, 'is_authenticated', False):
        return False
    if user.is_superuser:
        return True
    perfil = getattr(user, 'perfil', None)
    return bool(perfil and perfil.acceso_todas_unidades)


def unidades_negocio_visibles(user):
    """Queryset de UnidadNegocio que `user` puede ver y accionar."""
    if usuario_tiene_acceso_total(user):
        return UnidadNegocio.objects.all()
    perfil = getattr(user, 'perfil', None) if user is not None else None
    if perfil is None:
        return UnidadNegocio.objects.none()
    return perfil.unidades_negocio.all()


def usuario_puede_ver(user, unidad_negocio) -> bool:
    """True si `user` puede ver/accionar sobre `unidad_negocio`.

    `unidad_negocio=None` devuelve True (para objetos sin tenant asignado, ej. un
    Script compartido) — quien necesite bloquear ese caso lo revisa aparte.
    """
    if unidad_negocio is None:
        return True
    if usuario_tiene_acceso_total(user):
        return True
    return unidades_negocio_visibles(user).filter(pk=unidad_negocio.pk).exists()


def scope_por_unidad_negocio(queryset, user, campo_lookup):
    """Filtra `queryset` a lo que `user` puede ver, salvo que tenga acceso total.

    `campo_lookup` es la ruta ORM hasta UnidadNegocio, ej. 'unidad_negocio',
    'farmacia__unidad_negocio', 'despliegue__unidad_negocio'.
    """
    if usuario_tiene_acceso_total(user):
        return queryset
    return queryset.filter(**{f'{campo_lookup}__in': unidades_negocio_visibles(user)})


def verificar_acceso(user, unidad_negocio):
    """Lanza PermissionDenied (403) si `user` no puede ver/accionar sobre `unidad_negocio`.

    Pensado para usarse justo después de un `get_object_or_404` en las vistas que
    operan sobre un objeto puntual (estación, despliegue, ejecución de script...),
    donde el listado ya filtra pero alguien podría forzar el ID por URL.
    """
    if not usuario_puede_ver(user, unidad_negocio):
        raise PermissionDenied('No tiene acceso a esta unidad de negocio.')


def scope_opcional_por_unidad_negocio(queryset, user, campo_lookup):
    """Como `scope_por_unidad_negocio`, pero para modelos donde `campo_lookup` es
    opcional (`None` = compartido/global, visible para cualquiera) además de lo
    privado de las unidades de negocio del usuario — ej. Script, ReglaAlerta."""
    if usuario_tiene_acceso_total(user):
        return queryset
    return queryset.filter(
        Q(**{f'{campo_lookup}__isnull': True}) | Q(**{f'{campo_lookup}__in': unidades_negocio_visibles(user)}),
    )


def scope_scripts_visibles(queryset, user):
    """Alias de `scope_opcional_por_unidad_negocio` para Script (unidad_negocio=None
    = compartido)."""
    return scope_opcional_por_unidad_negocio(queryset, user, 'unidad_negocio')


# --- Unidad de negocio "activa" (selector de la barra superior) ---
#
# Puramente de presentación: acota los valores por defecto del dashboard/listados a
# un solo cliente cuando el usuario tiene acceso a varios y quiere enfocarse en uno.
# No es un límite de seguridad — `scope_por_unidad_negocio`/`verificar_acceso` ya
# garantizan el aislamiento real sin importar lo que haya en sesión.

SESSION_KEY_UNIDAD_ACTIVA = 'unidad_negocio_activa_id'


def unidad_negocio_activa(request):
    """La unidad de negocio fijada en sesión (barra superior), si sigue siendo
    visible para el usuario; None si no hay ninguna fijada (= "ver todas")."""
    id_activa = request.session.get(SESSION_KEY_UNIDAD_ACTIVA)
    if not id_activa:
        return None
    return unidades_negocio_visibles(request.user).filter(pk=id_activa).first()


def scope_por_unidad_negocio_activa(queryset, request, campo_lookup):
    """`scope_por_unidad_negocio` +, si aplica, acotar además a la unidad de
    negocio activa en sesión."""
    queryset = scope_por_unidad_negocio(queryset, request.user, campo_lookup)
    activa = unidad_negocio_activa(request)
    if activa is not None:
        queryset = queryset.filter(**{campo_lookup: activa})
    return queryset


def scope_opcional_por_unidad_negocio_activa(queryset, request, campo_lookup):
    queryset = scope_opcional_por_unidad_negocio(queryset, request.user, campo_lookup)
    activa = unidad_negocio_activa(request)
    if activa is not None:
        queryset = queryset.filter(Q(**{f'{campo_lookup}__isnull': True}) | Q(**{campo_lookup: activa}))
    return queryset


def scope_scripts_visibles_activa(queryset, request):
    return scope_opcional_por_unidad_negocio_activa(queryset, request, 'unidad_negocio')


def unidades_negocio_en_foco(request):
    """Como `unidades_negocio_visibles`, pero acotado a la unidad de negocio activa
    en sesión si el usuario fijó una — para agregados (ej. dashboard) que no operan
    sobre un queryset de un solo modelo."""
    activa = unidad_negocio_activa(request)
    if activa is not None:
        return UnidadNegocio.objects.filter(pk=activa.pk)
    return unidades_negocio_visibles(request.user)
