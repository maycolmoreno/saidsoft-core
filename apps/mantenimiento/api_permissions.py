"""`PermisoDeclarado` vive en `apps.cuentas.api_permissions`.

Se movió para que la API de integración RMM (`apps.catalogo.api_views`) lo use sin
depender de `apps.mantenimiento`, que se va a separar en otro proyecto. Este módulo lo
reexporta para que los imports existentes sigan funcionando.
"""
from apps.cuentas.api_permissions import PermisoDeclarado  # noqa: F401
