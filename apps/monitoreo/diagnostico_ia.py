"""Diagnóstico automático de alertas CRÍTICAS con un modelo de lenguaje.

Qué es y qué no es: esto produce una **hipótesis**, no un hecho. Todo lo demás que
guarda este proyecto salió de un sondeo, un contador SNMP o un reporte del agente; esto
sale de un modelo que puede equivocarse. Por eso el resultado vive en un campo propio
(`Alerta.diagnostico_ia`, nunca mezclado con datos operativos), el mensaje que se manda
va rotulado como automático y sin verificar, y el prompt le pide explícitamente al
modelo que diga "no alcanza el contexto" en vez de adivinar una causa.

Por qué solo CRÍTICAS y una sola vez por incidente: cada diagnóstico es una llamada
paga. `abrir_o_mantener_alerta` no crea una Alerta nueva mientras la anterior siga
activa, así que un servicio que lleva horas caído reportando cada 5 minutos genera
exactamente un diagnóstico, no cientos.

El contexto que se arma es solo lo que el sistema YA midió. No se inventan datos ni se
sondea nada nuevo para responder: si falta información, falta, y el modelo tiene que
decirlo.
"""
import logging

from django.conf import settings
from django.utils import timezone

logger = logging.getLogger(__name__)

# Cuántos elementos de cada fuente entran en el contexto. Acotado porque el prompt se
# paga por token y porque una lista larga de errores repetidos no aporta más que las
# primeras: lo que importa es qué está fallando, no cuántas veces se repitió la línea.
_MAX_ERRORES_POS = 5
_MAX_ALERTAS_PREVIAS = 5
_HORAS_CONTEXTO = 24

_INSTRUCCIONES = """Sos un asistente de soporte técnico de una red de farmacias. Recibís el contexto
de una alerta crítica de monitoreo y tenés que ayudar a quien la va a atender.

Respondé en español rioplatense neutro, en prosa, con este formato y nada más:

CAUSA MÁS PROBABLE: una o dos frases.
QUÉ VERIFICAR: dos o tres pasos concretos, en orden de menor a mayor esfuerzo.
DESCARTAR: qué NO es, si el contexto permite descartar algo.

Reglas que no podés romper:
- No inventes datos. Si el contexto no alcanza para concluir una causa, escribí
  exactamente "CAUSA MÁS PROBABLE: el contexto disponible no alcanza para concluir" y
  usá QUÉ VERIFICAR para decir qué información falta.
- No afirmes como hecho algo que no esté en el contexto. No supongas topología,
  proveedores, horarios ni configuraciones que no te dieron.
- No propongas acciones destructivas (borrar datos, reinstalar, formatear) ni cambios
  de configuración masivos.
- Sé breve: esto se lee en un teléfono. Máximo 150 palabras."""


def _contexto_de(alerta) -> str:
    """Arma el contexto con lo que el sistema ya midió sobre esta estación.

    Cada bloque es opcional: se incluye solo si hay dato real. Un bloque vacío se omite
    en vez de escribirse como "sin datos", para que el modelo no confunda "no lo miré"
    con "lo miré y estaba bien" — la diferencia cambia el diagnóstico.
    """
    from datetime import timedelta

    from .models import Alerta, EstadoEnlaceFarmacia, EstadoServicioPos, PosErrorDetectado

    estacion = alerta.estacion
    farmacia = estacion.farmacia
    desde = timezone.now() - timedelta(hours=_HORAS_CONTEXTO)
    partes = [
        'ALERTA',
        f'  Regla: {alerta.regla.nombre} ({alerta.regla.get_metrica_display()})',
        f'  Severidad: {alerta.regla.get_severidad_display()}',
        f'  Condición: {alerta.regla.get_operador_display()} {alerta.regla.umbral}',
        f'  Valor que la disparó: {alerta.valor_disparador}',
        f'  Abierta: {timezone.localtime(alerta.abierta_en):%Y-%m-%d %H:%M}',
        '',
        'ESTACIÓN',
        f'  Código: {estacion.codigo} (farmacia {farmacia.codigo}, {farmacia.unidad_negocio.codigo})',
        f'  Sistema: {estacion.so_nombre or "desconocido"} · agente {estacion.version_agente or "desconocido"}',
        f'  Estado de conexión: {estacion.get_estado_conexion_display()}',
    ]
    if estacion.ultimo_heartbeat:
        minutos = int((timezone.now() - estacion.ultimo_heartbeat).total_seconds() // 60)
        partes.append(f'  Último heartbeat: hace {minutos} min')

    servicios = list(EstadoServicioPos.objects.filter(estacion=estacion).order_by('servicio'))
    if servicios:
        partes += ['', 'SERVICIOS EXTERNOS QUE USA EL POS (medidos desde esta estación)']
        for s in servicios:
            estado = 'responde' if s.disponible else 'NO responde'
            latencia = f', {s.latencia_ms} ms' if s.latencia_ms is not None else ''
            critico = ' [crítico]' if s.critico else ''
            # `endpoint` nunca trae credenciales por diseño del modelo; `mensaje` es el
            # texto que devolvió el propio servicio.
            partes.append(f'  {s.get_servicio_display()}{critico}: {estado}{latencia} — {s.endpoint} — {s.mensaje}')

    enlace = EstadoEnlaceFarmacia.objects.filter(farmacia=farmacia).first()
    if enlace and enlace.ultima_verificacion:
        if enlace.alcanzable:
            estado_enlace = f'responde ({enlace.latencia_ms} ms)'
        elif enlace.nunca_respondio:
            estado_enlace = 'nunca respondió desde que se lo sondea (puede ser falta de ruta o IP mal cargada)'
        else:
            estado_enlace = f'caído, {enlace.fallas_consecutivas} fallos seguidos'
        partes += [
            '',
            'ENLACE DE LA FARMACIA (ping al equipo de borde)',
            f'  {estado_enlace}',
            f'  Circuito del proveedor: {farmacia.circuito_proveedor or "sin dato"}',
        ]

    errores = list(
        PosErrorDetectado.objects.filter(estacion=estacion, ultima_vez__gte=desde)
        .order_by('-ultima_vez')[:_MAX_ERRORES_POS]
    )
    if errores:
        partes += ['', f'ERRORES DEL POS EN LAS ÚLTIMAS {_HORAS_CONTEXTO} H']
        for e in errores:
            # La cantidad importa: un error que se repitió 400 veces y otro que pasó una
            # sola vez no significan lo mismo, y sin el número el modelo no puede saberlo.
            partes.append(
                f'  [{timezone.localtime(e.ultima_vez):%H:%M}] x{e.cantidad_total} '
                f'({e.nivel}) {e.mensaje[:200]}'
            )

    previas = list(
        Alerta.objects.filter(estacion=estacion, abierta_en__gte=desde)
        .exclude(pk=alerta.pk).select_related('regla').order_by('-abierta_en')[:_MAX_ALERTAS_PREVIAS]
    )
    if previas:
        partes += ['', f'OTRAS ALERTAS DE ESTA ESTACIÓN EN LAS ÚLTIMAS {_HORAS_CONTEXTO} H']
        for a in previas:
            partes.append(
                f'  [{timezone.localtime(a.abierta_en):%d/%m %H:%M}] {a.regla.nombre} '
                f'({a.get_estado_display()})'
            )

    return '\n'.join(partes)


def generar_diagnostico(alerta) -> str | None:
    """Pide el diagnóstico a Claude y devuelve el texto, o None si no se pudo.

    Nunca lanza: el diagnóstico es un extra sobre una alerta que ya está abierta y
    notificada. Que falle no puede convertirse en un error visible para nadie.
    """
    api_key = getattr(settings, 'ANTHROPIC_API_KEY', '')
    if not api_key:
        return None
    try:
        import anthropic
    except ImportError:
        logger.warning('Diagnóstico IA pedido pero la librería `anthropic` no está instalada.')
        return None

    try:
        cliente = anthropic.Anthropic(api_key=api_key)
        respuesta = cliente.messages.create(
            model=getattr(settings, 'ANTHROPIC_MODELO_DIAGNOSTICO', 'claude-sonnet-5'),
            max_tokens=getattr(settings, 'ANTHROPIC_MAX_TOKENS_DIAGNOSTICO', 700),
            system=_INSTRUCCIONES,
            messages=[{'role': 'user', 'content': _contexto_de(alerta)}],
        )
    except Exception:
        # Sin exc_info sobre el objeto de la excepción a secas: algunas librerías de
        # cliente incluyen la request (y con ella la cabecera de autenticación) en su
        # repr. Se loguea el número de alerta, que alcanza para rastrearlo.
        logger.warning('No se pudo generar el diagnóstico IA de la alerta #%s.', alerta.pk)
        return None

    texto = ''.join(
        bloque.text for bloque in getattr(respuesta, 'content', []) if getattr(bloque, 'type', '') == 'text'
    ).strip()
    return texto or None
