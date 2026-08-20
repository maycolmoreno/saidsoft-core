from celery import shared_task

from .mikrotik import sincronizar_ancho_banda_farmacias
from .services import (
    escalar_alertas_abiertas, evaluar_cruce_monitoreo, purgar_eventos_monitoreo_antiguos, purgar_metricas_antiguas,
)


@shared_task(name='apps.monitoreo.tasks.purgar_metricas_task')
def purgar_metricas_task():
    """Diaria (ver CELERY_BEAT_SCHEDULE). Sin efecto real en producción con TimescaleDB
    (ahí la retención la maneja una política nativa) — respaldo y para SQLite en dev."""
    borradas = purgar_metricas_antiguas(dias=30)
    return f'{borradas} muestra(s) de métricas eliminada(s).'


@shared_task(name='apps.monitoreo.tasks.purgar_eventos_monitoreo_task')
def purgar_eventos_monitoreo_task():
    """Diaria (ver CELERY_BEAT_SCHEDULE). Mismo respaldo que purgar_metricas_task."""
    borrados = purgar_eventos_monitoreo_antiguos(dias=30)
    return f'{borrados} evento(s) de monitoreo eliminado(s).'


@shared_task(name='apps.monitoreo.tasks.evaluar_cruce_monitoreo_task')
def evaluar_cruce_monitoreo_task():
    """Cada 5-10 min (ver CELERY_BEAT_SCHEDULE) — cruce MQTT × MeshCentral. No hace
    polling de ninguna fuente: solo lee EstadoDispositivo, que ya viene actualizado en
    tiempo real por manejar_heartbeat (mqtt_worker) y run_meshcentral_worker."""
    abiertas = evaluar_cruce_monitoreo()
    return f'{abiertas} alerta(s) "agente_caido_red_viva" abierta(s).'


@shared_task(name='apps.monitoreo.tasks.escalar_alertas_task')
def escalar_alertas_task():
    """Cada 10 min (ver CELERY_BEAT_SCHEDULE) — reenvía alertas ABIERTAS que nadie
    reconoció a tiempo (ver UMBRAL_ESCALAMIENTO_MINUTOS en services.py)."""
    escaladas = escalar_alertas_abiertas()
    return f'{escaladas} alerta(s) escalada(s).'


@shared_task(name='apps.monitoreo.tasks.sincronizar_ancho_banda_farmacias_task')
def sincronizar_ancho_banda_farmacias_task():
    """Cada 5 min (ver CELERY_BEAT_SCHEDULE) — sondea por SNMP el Mikrotik de cada
    farmacia con `ip_router` cargada. Sin efecto si MIKROTIK_SNMP_CONFIG no está
    configurado (ver apps.monitoreo.mikrotik)."""
    exitosas = sincronizar_ancho_banda_farmacias()
    return f'{exitosas} farmacia(s) sondeada(s) por SNMP.'
