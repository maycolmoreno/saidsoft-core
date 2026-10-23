"""Presentación del detalle de un mantenimiento: ciclo como pasos, SLA como medidor y el
historial con tonos (la misma línea de vida que la ficha de estación).

Solo lee lo que ya existe. La definición de urgencia sigue siendo `Mantenimiento.estado_sla`;
esto la dibuja.
"""
from django.utils import timezone
from django.utils.timesince import timesince, timeuntil

TONO_EVENTO = {
    'programado': 'info', 'iniciado': 'accent', 'checklist_actualizado': 'neutral', 'firmado': 'good',
    'imagen_adjuntada': 'neutral', 'repuesto_registrado': 'warning', 'informe_generado': 'info',
    'cerrado': 'good', 'cancelado': 'critical',
}


def con_tono(eventos):
    eventos = list(eventos)
    for ev in eventos:
        ev.tono = TONO_EVENTO.get(ev.tipo_evento, 'info')
    return eventos


def ciclo(m, eventos):
    """Programado → Iniciado → Cerrado (o Cancelado), con fecha y tono cada uno.
    `eventos` ya leídos (la vista los muestra igual en el historial)."""
    E = m.EstadoInterno
    primero = {}
    for ev in sorted(eventos, key=lambda e: e.timestamp):
        primero.setdefault(ev.tipo_evento, ev.timestamp)
    iniciado = primero.get('iniciado')
    if m.estado_interno == E.CANCELADO:
        final = ('Cancelado', primero.get('cancelado'), 'critical')
    elif m.estado_interno == E.CERRADO:
        final = ('Cerrado', m.fecha_cierre or primero.get('cerrado'), 'good')
    else:
        final = ('Cierre', None, 'neutral')
    return [
        {'titulo': 'Programado', 'fecha': m.fecha_programada, 'tono': 'good'},
        {'titulo': 'Iniciado', 'fecha': iniciado,
         'tono': 'good' if iniciado else ('accent' if m.estado_interno == E.PENDIENTE else 'neutral')},
        {'titulo': final[0], 'fecha': final[1],
         'tono': final[2] if final[1] or m.estado_interno in (E.CERRADO, E.CANCELADO)
         else ('accent' if m.estado_interno == E.EN_PROCESO else 'neutral')},
    ]


def sla(m, ahora=None):
    """`{'texto', 'tono', 'pct'}` del SLA de resolución, o None si no tiene. `pct` es el
    tiempo consumido (0-100) para el medidor."""
    limite = m.limite_resolucion
    estado = m.estado_sla
    if limite is None or estado == 'sin_sla':
        return None
    ahora = ahora or timezone.now()
    tono = {'incumplido': 'critical', 'por_vencer': 'warning', 'cumplido': 'good', 'en_plazo': 'good'}[estado]
    if m.estado_interno == m.EstadoInterno.CERRADO:
        texto = 'Cumplido' if estado == 'cumplido' else 'Cerrado fuera de plazo'
        return {'texto': texto, 'tono': tono, 'pct': 100}
    total = (limite - m.fecha_programada).total_seconds()
    consumido = (ahora - m.fecha_programada).total_seconds()
    pct = max(0, min(100, round(100 * consumido / total))) if total > 0 else 100
    if ahora > limite:
        texto = 'Vencido hace ' + timesince(limite, ahora).split(',')[0]
    else:
        texto = 'Quedan ' + timeuntil(limite, ahora).split(',')[0]
    return {'texto': texto, 'tono': tono, 'pct': pct}
