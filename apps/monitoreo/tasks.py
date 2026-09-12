from celery import shared_task

from .enlaces import sondear_enlaces_farmacias
from .mikrotik import sincronizar_ancho_banda_farmacias, solicitar_sondeo_red_farmacias_via_agente
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


@shared_task(name='apps.monitoreo.tasks.solicitar_sondeo_red_farmacias_via_agente_task')
def solicitar_sondeo_red_farmacias_via_agente_task():
    """Cada 5 min (ver CELERY_BEAT_SCHEDULE) — por cada farmacia con `ip_router` y
    una estación en línea, le pide a esa estación (misma LAN que el Mikrotik del
    sitio) que lo sondee y reporte por MQTT. Ver docstring de
    solicitar_sondeo_red_farmacias_via_agente sobre por qué esto reemplaza en la
    práctica al sondeo directo de sincronizar_ancho_banda_farmacias_task."""
    enviadas = solicitar_sondeo_red_farmacias_via_agente()
    return f'{enviadas} estación(es) recibieron el pedido de sondeo de red.'


@shared_task(name='apps.monitoreo.tasks.sincronizar_meshcentral_task')
def sincronizar_meshcentral_task():
    """Cada 15 min (ver CELERY_BEAT_SCHEDULE) — respaldo del resync que ya hace
    run_meshcentral_worker al (re)conectar (ver AdaptadorMeshCentral.sincronizar_todo).
    Sin esto, una estación instalada mientras el worker lleva mucho tiempo conectado
    sin caerse podía quedar sin auto-vincular si el evento nodeconnect en vivo no traía
    el nombre del nodo — este resync completo es la red de seguridad. Sin efecto si
    MESHCENTRAL_API_CONFIG no está configurado."""
    from apps.monitoreo.adapters.meshcentral import AdaptadorMeshCentral

    procesados = AdaptadorMeshCentral().sincronizar_todo()
    return f'{procesados} nodo(s) de MeshCentral sincronizado(s).'


@shared_task(name='apps.monitoreo.tasks.sondear_enlaces_farmacias_task')
def sondear_enlaces_farmacias_task():
    """Cada 2 min (ver CELERY_BEAT_SCHEDULE): ping al equipo de borde de cada farmacia
    con `ip_router` cargada. Es el único monitoreo del proyecto que no necesita agente
    instalado, así que cubre las ~704 sucursales y no las 8 con agente.

    Si el barrido falla casi entero no escribe nada y lo reporta como problema de ruta
    -- ver apps.monitoreo.enlaces.sondear_enlaces_farmacias."""
    resumen = sondear_enlaces_farmacias()
    if resumen['abortado']:
        return (
            f'Barrido abortado: {resumen["caidas"]}/{resumen["sondeadas"]} sin responder. '
            'Sin ruta desde este host; no se registró nada.'
        )
    return f'{resumen["sondeadas"]} enlace(s): {resumen["activas"]} activo(s), {resumen["caidas"]} caído(s).'
