"""Registro de conectores externos (Odoo, Active Directory, ESET, etc.).

Cada conector concreto (Fase 3+ del roadmap) hereda de ConectorExterno e implementa
enviar(). registrar_evento_sync() y sincronizar_evento_task() nunca importan una clase de
conector directamente: la buscan por nombre en este registro, así una fase futura puede
agregar apps/integraciones/odoo.py (o un app aparte) sin tocar el resto de la capa.
"""


class ConectorExterno:
    """Interfaz que debe implementar cada conector concreto."""

    def enviar(self, objeto):
        """Envía `objeto` al sistema externo y devuelve un dict serializable a JSON con la
        respuesta (se guarda tal cual en EventoSyncExterno.respuesta)."""
        raise NotImplementedError


_REGISTRO: dict[str, type[ConectorExterno]] = {}


def registrar_conector(nombre):
    def decorador(cls):
        _REGISTRO[nombre] = cls
        return cls
    return decorador


def obtener_conector(nombre) -> ConectorExterno:
    """Lanza KeyError si `nombre` no está registrado — falla explícito y temprano en vez
    de un no-op silencioso que oculte un typo en la config de un ScriptProgramado/tarea."""
    return _REGISTRO[nombre]()


def conectores_registrados():
    return list(_REGISTRO.keys())
