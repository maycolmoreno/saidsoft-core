"""Permisos de la API móvil: los MISMOS codenames que exige el panel HTMX.

Hasta el 26-sep-2026 casi toda la API era `IsAuthenticated` a secas (la única
excepción era `activos.add_activo` en `ActivoCrearView`), mientras el panel exigía
`view_mantenimiento` / `change_mantenimiento` / `view_visitatecnica` /
`change_visitatecnica` para las mismas operaciones. El README de `movil-campo` y el
docstring de `UsuarioActualView` afirmaban lo contrario: que la app y la web habilitan
lo mismo "y no pueden desincronizarse". El gating de la app (`sesion.dart`) era
**cosmético** — escondía botones que la API aceptaba igual.

**Por qué un mapa explícito y no `DjangoModelPermissions`.** Se verificó contra la
implementación real de DRF y no alcanza, por dos motivos:

- `perms_map['GET'] == []` — las lecturas no exigen NADA, así que `view_mantenimiento`
  nunca se evaluaría, mientras el panel sí lo pide.
- `perms_map['POST'] == ['add_%(model_name)s']` — TODAS las acciones custom
  (`iniciar`, `cerrar`, `cancelar`, `firmar`, `repuestos`, `imagenes`) son POST, así
  que caerían en `add_mantenimiento` cuando el panel exige `change_mantenimiento`.
  Además de ser el codename equivocado, se equivoca hacia el lado permisivo: quien
  solo puede CREAR podría cerrar y firmar.

**Fail-closed a propósito.** Una acción que no figure en el mapa se rechaza, en vez de
pasar. Es la lección del bug que esto cierra: los endpoints no nacieron desprotegidos
por una decisión, sino porque agregar un `@action` no obligaba a pensar en el permiso.
Con esto, un `@action` nuevo sin entrada en el mapa falla de inmediato en las pruebas
en lugar de quedar abierto en producción.
"""
from rest_framework import permissions


class PermisoDeclarado(permissions.BasePermission):
    """Exige los codenames que la propia vista declara.

    La vista declara UNO de los dos mapas, según su tipo:

    - `permisos_por_accion` (ViewSets): `{'list': ['app.view_x'], 'cerrar': [...]}`,
      indexado por `view.action` — que DRF resuelve antes de chequear permisos.
    - `permisos_por_metodo` (vistas genéricas, que no tienen `action`):
      `{'GET': [...], 'POST': [...]}`.

    Una lista vacía significa "basta con estar autenticado" y hay que escribirla
    explícitamente: es la diferencia entre "se decidió que no lleva permiso" y "nadie
    se acordó de ponerlo".
    """

    message = 'No tenes permiso para esta accion.'

    def has_permission(self, request, view):
        usuario = request.user
        if not (usuario and usuario.is_authenticated):
            return False
        requeridos = self._requeridos(view, request)
        if requeridos is None:
            return False
        return all(usuario.has_perm(codename) for codename in requeridos)

    @staticmethod
    def _requeridos(view, request):
        """Codenames exigidos, o None si la vista no declaró nada para este caso
        (que se trata como denegación, no como vía libre)."""
        por_accion = getattr(view, 'permisos_por_accion', None)
        if por_accion is not None:
            accion = getattr(view, 'action', None)
            return None if accion is None else por_accion.get(accion)
        por_metodo = getattr(view, 'permisos_por_metodo', None)
        if por_metodo is not None:
            return por_metodo.get(request.method)
        return None
