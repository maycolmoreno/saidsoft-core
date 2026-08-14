"""Puerto para fuentes de monitoreo que hay que ir a buscar (pull).

MQTT y MeshCentral empujan sus señales directamente a
apps.monitoreo.services.registrar_estado_dispositivo (MQTT desde apps.mqtt_worker.services,
MeshCentral desde su propio worker de larga duración) y NO implementan este contrato — un
`FuenteMonitoreo` simétrico para las dos forzaría un polling artificial sobre MQTT que no
tiene sentido cuando la fuente ya empuja.

Este puerto existe para lo que sí hay que consultar activamente:
- El resync de respaldo de MeshCentral (`AdaptadorMeshCentral.sincronizar_todo`), por si
  se perdió un evento del WebSocket mientras el worker estaba caído.
- La futura fuente de ESET PROTECT (pendiente de aprobación de acceso a su API — ver
  PLAN_MODERNIZACION.md §9): su API es de consulta, no push, así que sí calzará acá.
"""
from abc import ABC, abstractmethod


class FuenteMonitoreo(ABC):
    @abstractmethod
    def sincronizar_todo(self) -> int:
        """Consulta la fuente externa y registra el estado de cada dispositivo conocido
        vía apps.monitoreo.services.registrar_estado_dispositivo. Devuelve cuántos
        dispositivos se procesaron."""
