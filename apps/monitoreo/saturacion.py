"""Aviso de enlace saturado: ≥90 % del ancho contratado sostenido 15 minutos (§10-CS).

Hasta el 19-oct-2026 la saturación solo se VEÍA (Monitoreo → Enlaces pinta rojo al 90 %);
no avisaba a nadie. Ahora avisa por el mismo canal que las caídas
(`ENLACES_NOTIFICAR_A` / `ENLACES_TELEGRAM_CHAT_ID`), una vez al empezar y otra al
normalizarse.

**Cuándo.** Las tres últimas muestras SNMP de la farmacia (`MuestraRedFarmacia`, una cada
5 min) en los últimos 20 minutos, todas ≥ `BW_CONTRATADO_UMBRAL_CRITICAL_PCT` del ancho
contratado. Un pico aislado no alcanza. Se cierra cuando la última muestra baja de
`BW_CONTRATADO_UMBRAL_WARNING_PCT`: con un solo umbral, un enlace rondando el 90 % abriría
y cerraría un evento en cada muestra.

Se mide el TOTAL (subida + bajada) contra el contratado, igual que la barra del panel
(`MuestraRedFarmacia.porcentaje_del_contratado`): mismo número en los dos lados.

**Solo farmacias con `ancho_contratado_mbps`.** Sin el contrato no hay contra qué decir
"saturado", y avisar con los umbrales absolutos en kbps sería ruido: 8 Mbps saturan un
enlace de 4 y son la mitad de uno de 16.

**Quién.** Si la farmacia exporta NetFlow, el aviso dice qué equipo consumía más y contra
qué (`apps.monitoreo.netflow.principal_consumidor`). Es lo que convierte "ML008 saturada"
en algo que se puede ir a resolver.
"""
import logging
from collections import defaultdict
from datetime import timedelta

from django.conf import settings
from django.core.mail import send_mail
from django.utils import timezone

from .umbrales import BW_CONTRATADO_UMBRAL_CRITICAL_PCT, BW_CONTRATADO_UMBRAL_WARNING_PCT

logger = logging.getLogger(__name__)

MUESTRAS_SOSTENIDAS = 3
VENTANA_MUESTRAS_MINUTOS = 20


def _pct(muestra, contratado_mbps):
    if muestra.red_total_kbps is None:
        return None
    return 100 * muestra.red_total_kbps / (contratado_mbps * 1000)


def evaluar_saturacion_enlaces(ahora=None) -> dict:
    """Abre y cierra `EventoSaturacionEnlace` según las últimas muestras. No avisa: eso
    es `notificar_saturacion_enlaces`, igual que el sondeo y el aviso de caídas."""
    from apps.catalogo.models import Farmacia

    from .models import EventoSaturacionEnlace, MuestraRedFarmacia

    ahora = ahora or timezone.now()
    farmacias = {
        f.pk: f for f in Farmacia.objects.filter(activa=True, ancho_contratado_mbps__isnull=False)
        .exclude(ancho_contratado_mbps=0)
    }
    muestras = defaultdict(list)
    for muestra in (
        MuestraRedFarmacia.objects
        .filter(farmacia_id__in=farmacias, timestamp__gte=ahora - timedelta(minutes=VENTANA_MUESTRAS_MINUTOS))
        .order_by('-timestamp')
    ):
        muestras[muestra.farmacia_id].append(muestra)
    abiertos = {e.farmacia_id: e for e in EventoSaturacionEnlace.objects.filter(fin__isnull=True)}

    resumen = {'abiertos': 0, 'cerrados': 0}
    for farmacia_id, recientes in muestras.items():
        contratado = farmacias[farmacia_id].ancho_contratado_mbps
        porcentajes = [p for p in (_pct(m, contratado) for m in recientes) if p is not None]
        evento = abiertos.get(farmacia_id)
        if evento is None:
            ultimas = porcentajes[:MUESTRAS_SOSTENIDAS]
            if len(ultimas) == MUESTRAS_SOSTENIDAS and min(ultimas) >= BW_CONTRATADO_UMBRAL_CRITICAL_PCT:
                EventoSaturacionEnlace.objects.create(
                    farmacia_id=farmacia_id, contratado_mbps=contratado, pico_pct=round(max(ultimas), 1),
                    inicio=recientes[MUESTRAS_SOSTENIDAS - 1].timestamp,
                )
                resumen['abiertos'] += 1
        elif porcentajes:
            evento.pico_pct = round(max(evento.pico_pct, porcentajes[0]), 1)
            if porcentajes[0] < BW_CONTRATADO_UMBRAL_WARNING_PCT:
                evento.fin = recientes[0].timestamp
                resumen['cerrados'] += 1
            evento.save(update_fields=['pico_pct', 'fin'])
    return resumen


def notificar_saturacion_enlaces() -> dict:
    """Un mensaje con las saturaciones nuevas y las que se normalizaron. Mismos canales y
    mismo criterio de marcas que `apps.monitoreo.enlaces.notificar_cambios_enlaces`."""
    from .models import EventoSaturacionEnlace
    from .netflow import principal_consumidor

    ahora = timezone.now()
    nuevas = list(
        EventoSaturacionEnlace.objects.filter(fin__isnull=True, notificado_en__isnull=True, farmacia__activa=True)
        .select_related('farmacia').order_by('inicio')
    )
    # Solo la normalización de lo que SÍ se avisó: mismo motivo que en las caídas.
    normalizadas = list(
        EventoSaturacionEnlace.objects.filter(
            fin__isnull=False, notificado_en__isnull=False, normalizacion_notificada_en__isnull=True,
        ).select_related('farmacia').order_by('fin')
    )
    resumen = {'saturados': len(nuevas), 'normalizados': len(normalizadas), 'enviado': False}
    if not nuevas and not normalizadas:
        return resumen

    destinatarios = list(getattr(settings, 'ENLACES_NOTIFICAR_A', []) or [])
    chat_telegram = getattr(settings, 'ENLACES_TELEGRAM_CHAT_ID', '')
    if not destinatarios and not chat_telegram:
        logger.info('Enlaces: %d saturación(es) sin avisar — no hay canal configurado.', len(nuevas))
        return resumen

    lineas = []
    if nuevas:
        lineas.append(f'ENLACES SATURADOS ({len(nuevas)})')
        for e in nuevas:
            e.principal_consumidor = principal_consumidor(e.farmacia)[:200]
            lineas.append(
                f'  {e.farmacia.codigo:<10} {e.pico_pct:.0f}% de {e.contratado_mbps} Mbps '
                f'desde {timezone.localtime(e.inicio):%H:%M}'
            )
            if e.principal_consumidor:
                lineas.append(f'             más consume: {e.principal_consumidor}')
    if normalizadas:
        if lineas:
            lineas.append('')
        lineas.append(f'SATURACIÓN NORMALIZADA ({len(normalizadas)})')
        for e in normalizadas:
            lineas.append(f'  {e.farmacia.codigo:<10} duró {e.duracion_minutos} min, pico {e.pico_pct:.0f}%')

    partes = []
    if nuevas:
        partes.append(f'{len(nuevas)} saturado(s)')
    if normalizadas:
        partes.append(f'{len(normalizadas)} normalizado(s)')
    asunto = '[Enlaces] ' + ', '.join(partes)
    cuerpo = '\n'.join(lineas)

    if destinatarios:
        send_mail(asunto, cuerpo, None, destinatarios, fail_silently=True)
    if chat_telegram:
        from .services import _enviar_telegram
        from .telegram_bot import teclado_para_farmacias

        _enviar_telegram(
            chat_telegram, f'{asunto}\n\n{cuerpo}',
            teclado=teclado_para_farmacias([e.farmacia.codigo for e in nuevas]) or None,
        )

    for e in nuevas:
        e.notificado_en = ahora
        e.save(update_fields=['notificado_en', 'principal_consumidor'])
    EventoSaturacionEnlace.objects.filter(pk__in=[e.pk for e in normalizadas]).update(
        normalizacion_notificada_en=ahora,
    )
    resumen['enviado'] = True
    return resumen
