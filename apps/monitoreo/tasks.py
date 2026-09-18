from celery import shared_task

from .enlaces import notificar_cambios_enlaces, sondear_enlaces_farmacias
from .mikrotik import sincronizar_ancho_banda_farmacias, solicitar_sondeo_red_farmacias_via_agente
from .services import (
    escalar_alertas_abiertas, evaluar_cruce_monitoreo, purgar_eventos_monitoreo_antiguos, purgar_metricas_antiguas,
    purgar_muestras_red_antiguas, solicitar_sondeo_activos_via_agente,
)


@shared_task(name='apps.monitoreo.tasks.purgar_metricas_task')
def purgar_metricas_task():
    """Diaria (ver CELERY_BEAT_SCHEDULE). NO es un respaldo: es la única retención que
    existe. Los hypertables de TimescaleDB nunca se crearon (ver
    `apps.monitoreo.services.purgar_metricas_antiguas`)."""
    borradas = purgar_metricas_antiguas(dias=30)
    return f'{borradas} muestra(s) de métricas eliminada(s).'


@shared_task(name='apps.monitoreo.tasks.purgar_eventos_monitoreo_task')
def purgar_eventos_monitoreo_task():
    """Diaria (ver CELERY_BEAT_SCHEDULE). Misma advertencia que purgar_metricas_task:
    no hay retención nativa detrás, esto es lo único que borra."""
    borrados = purgar_eventos_monitoreo_antiguos(dias=30)
    return f'{borrados} evento(s) de monitoreo eliminado(s).'


@shared_task(name='apps.monitoreo.tasks.purgar_muestras_red_task')
def purgar_muestras_red_task():
    """Diaria (ver CELERY_BEAT_SCHEDULE). Misma advertencia que purgar_metricas_task:
    no hay retención nativa detrás, esto es lo único que borra."""
    borradas = purgar_muestras_red_antiguas(dias=30)
    return f'{borradas} muestra(s) de red eliminada(s).'


@shared_task(name='apps.monitoreo.tasks.solicitar_sondeo_activos_task')
def solicitar_sondeo_activos_task():
    """Cada 15 min (ver CELERY_BEAT_SCHEDULE) — le pide a una estación en línea de cada
    farmacia que pingee los activos sin agente de su propia LAN.

    Cada 15 y no cada 5 como el sondeo del Mikrotik: acá no se mide una tasa que necesite
    muestras seguidas, se responde "¿está vivo?". Quince minutos de resolución alcanzan
    para eso y son la cuarta parte de tráfico MQTT y de pings dentro de la farmacia."""
    enviados = solicitar_sondeo_activos_via_agente()
    return f'{enviados} pedido(s) de sondeo de activos enviado(s).'


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


@shared_task(name='apps.monitoreo.tasks.notificar_cambios_enlaces_task')
def notificar_cambios_enlaces_task():
    """Cada 5 min (ver CELERY_BEAT_SCHEDULE): un correo con los enlaces que se cayeron
    y los que volvieron desde el aviso anterior.

    Separada de sondear_enlaces_farmacias_task y no encadenada al final de ella a
    proposito: el sondeo corre cada 2 min y notificar en cada barrido partiria una
    misma tanda de caidas en tres correos. Esperar a que se acumulen 5 minutos las
    junta en uno solo, que es como se lee y como se reporta al proveedor.

    Tampoco va dentro de registrar_sondeo: eso lo llaman tambien el comando manual y la
    API de ingesta, y ninguno de los dos deberia mandar correo por su cuenta."""
    resumen = notificar_cambios_enlaces()
    if not resumen['caidos'] and not resumen['recuperados']:
        return 'Sin novedades de enlaces.'
    estado = 'avisado' if resumen['enviado'] else 'SIN avisar (revisar ENLACES_NOTIFICAR_A)'
    return f'{resumen["caidos"]} caido(s), {resumen["recuperados"]} recuperado(s) — {estado}.'


@shared_task(name='apps.monitoreo.tasks.sondear_identidad_equipos_task')
def sondear_identidad_equipos_task():
    """Relee por SNMP la identidad de los Mikrotik y detecta reinicios.

    Cada 15 minutos y no cada 5 como el sondeo de tráfico: el número de serie no cambia
    nunca y la versión de RouterOS solo cuando alguien actualiza. Lo único que sí se
    mueve es el uptime, y 15 minutos alcanzan para notar un reinicio el mismo día.

    Existe porque sin esto el dato quedaba viejo sin que nadie se enterara: el 15-sep-2026
    se reinició GAT01 y el panel siguió mostrando 722 horas de uptime durante 40 minutos,
    porque la lectura solo ocurría cuando alguien corría el comando a mano.
    """
    from apps.monitoreo.mikrotik import sincronizar_identidad_equipos

    import logging

    resumen = sincronizar_identidad_equipos()
    if resumen['reinicios']:
        logging.getLogger(__name__).info(
            'Reinicios de equipo de borde detectados: %s', '; '.join(resumen['reinicios']),
        )
    return {'leidos': resumen['leidos'], 'reinicios': len(resumen['reinicios'])}


@shared_task(name='apps.monitoreo.tasks.diagnosticar_alerta_task')
def diagnosticar_alerta_task(alerta_id):
    """Genera el diagnóstico automático de una alerta CRÍTICA y lo manda por Telegram.

    Lo dispara `abrir_o_mantener_alerta` al crear la alerta (ver `_pedir_diagnostico_ia`),
    no el Beat: es por incidente, no periódico. Asíncrono para que una API lenta o caída
    no retrase la apertura de la alerta ni su notificación.

    Nunca lanza hacia Celery: la alerta ya está abierta y notificada, y un reintento
    automático volvería a pagar la llamada sin más información que la primera vez.

    Idempotente por `diagnostico_generado_en`: si ya se intentó, no se repite. Protege
    contra un doble encolado (un retry de Celery, una corrida manual) que duplicaría el
    costo y el mensaje.
    """
    from django.utils import timezone

    from apps.monitoreo.diagnostico_ia import generar_diagnostico
    from apps.monitoreo.models import Alerta, ReglaAlerta
    from apps.monitoreo.services import _enviar_telegram, canales_telegram_para

    alerta = (
        Alerta.objects.filter(pk=alerta_id)
        .select_related('regla', 'estacion__farmacia__unidad_negocio').first()
    )
    if alerta is None:
        return 'La alerta ya no existe.'
    if alerta.regla.severidad != ReglaAlerta.Severidad.CRITICAL:
        # Defensa en profundidad: el filtro real está en abrir_o_mantener_alerta, pero
        # esto evita que una llamada manual o futura gaste una llamada en un WARNING.
        return 'Solo las alertas críticas se diagnostican.'
    if alerta.diagnostico_generado_en is not None:
        return 'Ya se había diagnosticado.'

    texto = generar_diagnostico(alerta)
    # Se marca el intento haya salido bien o no: si la API falló, reintentar en el mismo
    # incidente daría lo mismo (el contexto no cambió) y solo sumaría costo.
    alerta.diagnostico_ia = texto
    alerta.diagnostico_generado_en = timezone.now()
    alerta.save(update_fields=['diagnostico_ia', 'diagnostico_generado_en'])
    if not texto:
        return 'No se pudo generar el diagnóstico.'

    # Mensaje de SEGUIMIENTO, aparte del aviso original: el aviso ya salió cuando se
    # abrió la alerta y no debe esperar a esto. El rótulo no es decorativo — sin él,
    # una hipótesis de un modelo se lee igual que un dato medido por el agente.
    encabezado = (
        f'🤖 Diagnóstico automático (IA) — confirmar antes de actuar:\n'
        f'{alerta.regla.nombre} — {alerta.estacion.codigo}'
    )
    unidad = alerta.estacion.farmacia.unidad_negocio
    enviados = sum(
        1 for canal in canales_telegram_para(unidad)
        if _enviar_telegram(canal.destino, f'{encabezado}\n\n{texto}')
    )
    return f'Diagnóstico generado para la alerta #{alerta.pk}; {enviados} envío(s) por Telegram.'
