"""Datos de la ficha completa de una estación (/estaciones/<pk>/).

La ventana emergente de la estación muestra cada dato en su propia sección, con párrafos
que explican de cuándo es cada cosa. La ficha completa los presenta con forma:

- un índice de salud con los motivos que le restan puntos (`salud`);
- la antigüedad de cada dato como un tono, no como una fecha que hay que interpretar
  (`edad`);
- una línea de vida que junta en orden lo que hoy está repartido en cinco lugares:
  órdenes, alertas, conexión, eventos de Windows y accesos auditados (`linea_de_vida`).

Todo se arma con lo que ya se guarda. No hay modelos ni datos nuevos: este módulo solo
lee y ordena.
"""
from dataclasses import dataclass, field
from datetime import timedelta

from django.utils import timezone
from django.utils.timesince import timesince

# Tonos que entiende la plantilla. Una sola lista, para que la paleta no se invente en
# cada lugar.
BIEN, ATENCION, CRITICO, INFO, NEUTRO, ACENTO = 'good', 'warning', 'critical', 'info', 'neutral', 'accent'


# --- Antigüedad de un dato -------------------------------------------------------------

def edad(fecha, *, ahora=None, vieja_tras=timedelta(days=7)):
    """`{'texto', 'tono'}` para mostrar de cuándo es un dato: "hace 3 h" en verde si es
    reciente, en ámbar si pasó `vieja_tras`, "nunca" en gris si no hay fecha."""
    if fecha is None:
        return {'texto': 'nunca', 'tono': NEUTRO}
    ahora = ahora or timezone.now()
    texto = 'hace ' + timesince(fecha, ahora).split(',')[0]
    return {'texto': texto, 'tono': ATENCION if ahora - fecha > vieja_tras else BIEN}


# --- Índice de salud -------------------------------------------------------------------

@dataclass
class Salud:
    puntaje: int
    tono: str
    motivos: list = field(default_factory=list)  # [(texto, puntos restados)]


# Lo que resta cada problema, y el tope por tipo. Visible en la ficha: el índice no es una
# caja negra, cada punto que falta dice por qué.
RESTA_FUERA_DE_LINEA = 40
RESTA_ALERTA_CRITICA, TOPE_CRITICAS = 20, 40
RESTA_ALERTA_ADVERTENCIA, TOPE_ADVERTENCIAS = 10, 20
RESTA_AGENTE_ATRASADO = 10
RESTA_CATALOGO_ATRASADO = 10
RESTA_DISCO_LLENO = 10
DISCO_LLENO_PCT = 90


def salud(estacion, *, alertas_abiertas, ultima_version_agente, catalogos, disco_pct):
    """Índice de 0 a 100 con sus motivos. Recibe los datos ya leídos (la vista los usa
    también para otras secciones), así que no consulta la base."""
    criticas = sum(1 for a in alertas_abiertas if a.regla.severidad == 'critical')
    return salud_desde_conteos(
        estacion, criticas=criticas, advertencias=len(alertas_abiertas) - criticas,
        ultima_version_agente=ultima_version_agente, catalogos=catalogos, disco_pct=disco_pct,
    )


def salud_desde_conteos(estacion, *, criticas, advertencias, ultima_version_agente, catalogos, disco_pct=None):
    """Lo mismo que `salud`, con las alertas ya contadas: la lista de estaciones lo calcula
    para toda la página con una sola consulta, sin traer cada alerta."""
    motivos = []
    if estacion.estado_conexion != estacion.EstadoConexion.ONLINE:
        motivos.append(('Fuera de línea', RESTA_FUERA_DE_LINEA))
    if criticas:
        motivos.append((f'{criticas} alerta(s) crítica(s) abierta(s)',
                        min(TOPE_CRITICAS, RESTA_ALERTA_CRITICA * criticas)))
    if advertencias:
        motivos.append((f'{advertencias} advertencia(s) abierta(s)',
                        min(TOPE_ADVERTENCIAS, RESTA_ALERTA_ADVERTENCIA * advertencias)))
    if (ultima_version_agente and estacion.version_agente
            and estacion.version_agente != ultima_version_agente.version):
        motivos.append((f'Agente {estacion.version_agente}, hay {ultima_version_agente.version}',
                        RESTA_AGENTE_ATRASADO))
    if any(al_dia is False for _, _, al_dia in catalogos):
        motivos.append(('Catálogo desactualizado', RESTA_CATALOGO_ATRASADO))
    if disco_pct is not None and disco_pct >= DISCO_LLENO_PCT:
        motivos.append((f'Disco al {disco_pct} %', RESTA_DISCO_LLENO))
    puntaje = max(0, 100 - sum(p for _, p in motivos))
    tono = BIEN if puntaje >= 85 else ATENCION if puntaje >= 60 else CRITICO
    return Salud(puntaje=puntaje, tono=tono, motivos=motivos)


# --- Recursos ---------------------------------------------------------------------------

def _pct(parte, total):
    if not parte or not total:
        return None
    return round(100 * parte / total)


def recursos(muestra):
    """Barras de CPU, memoria y disco de la última muestra, o [] si no hay."""
    if muestra is None:
        return []
    filas = []
    if muestra.cpu_carga_pct is not None:
        filas.append(('CPU', round(muestra.cpu_carga_pct)))
    ram = _pct(muestra.ram_usada, muestra.ram_total)
    if ram is not None:
        filas.append(('Memoria', ram))
    if muestra.disco_total_gb and muestra.disco_libre_gb is not None:
        filas.append(('Disco C:', round(100 * (1 - muestra.disco_libre_gb / muestra.disco_total_gb))))
    return [{'nombre': n, 'pct': p, 'tono': CRITICO if p >= 95 else ATENCION if p >= 85 else BIEN}
            for n, p in filas]


# --- Línea de vida ----------------------------------------------------------------------

@dataclass
class Evento:
    fecha: object
    categoria: str  # ordenes | alertas | conexion | windows | accesos
    titulo: str
    detalle: str
    tono: str


CATEGORIAS = [
    ('', 'Todo'), ('ordenes', 'Órdenes'), ('alertas', 'Alertas'), ('conexion', 'Conexión'),
    ('windows', 'Windows'), ('accesos', 'Accesos'),
]

_TONO_ORDEN = {
    'completada': BIEN, 'fallida': CRITICO, 'interrumpida': ATENCION, 'no_entregable': ATENCION,
    'caducada': NEUTRO, 'cancelada': NEUTRO, 'en_ejecucion': ACENTO, 'recibida': ACENTO,
}

# Acciones de auditoría que son "alguien hizo algo con esta estación". El resto de la
# auditoría (altas, ediciones administrativas) no aporta a la línea de vida.
_ACCESOS = {
    'estacion.meshcentral_abrir_escritorio': 'Escritorio remoto abierto',
    'estacion.meshcentral_abrir_terminal': 'Terminal remota abierta',
    'estacion.supervision_grabacion_ver': 'Grabaciones de sesión consultadas',
    'estacion.bitlocker_clave_ver': 'Clave de BitLocker consultada',
    'estacion.reiniciar': 'Reinicio enviado',
    'estacion.consultar_info': 'Información del equipo pedida',
    'estacion.consultar_software_instalado': 'Escaneo de software pedido',
    'estacion.consultar_perifericos': 'Escaneo de periféricos pedido',
    'estacion.actualizar_agente': 'Actualización del agente enviada',
    'estacion.pausar': 'Freno de emergencia cambiado',
    'estacion.escanear_actualizaciones': 'Escaneo de Windows Update pedido',
}


def linea_de_vida(estacion, *, categoria='', limite=40, ahora=None, dias=30):
    """Lo que le pasó a la estación en los últimos `dias`, lo más nuevo primero."""
    from apps.auditoria.models import EventoAuditoria
    from apps.catalogo.models import OrdenAgente
    from apps.monitoreo.models import Alerta, EventoMonitoreo
    from apps.monitoreo.services import eventos_sistema_recientes

    ahora = ahora or timezone.now()
    desde = ahora - timedelta(days=dias)
    eventos = []
    quiero = (lambda c: True) if not categoria else (lambda c: c == categoria)

    if quiero('ordenes'):
        for o in OrdenAgente.objects.filter(estacion=estacion, creada_en__gte=desde).order_by('-creada_en')[:limite]:
            eventos.append(Evento(
                fecha=o.finalizada_en or o.ultimo_reporte_en or o.creada_en, categoria='ordenes',
                titulo=f'{o.get_tipo_display()} #{o.origen_id}: {o.get_estado_display().lower()}',
                detalle=o.motivo_revision if o.requiere_revision else (o.detalle or ''),
                tono=_TONO_ORDEN.get(o.estado, INFO),
            ))
    if quiero('alertas'):
        alertas = (Alerta.objects.filter(estacion=estacion, abierta_en__gte=desde)
                   .select_related('regla').order_by('-abierta_en')[:limite])
        for a in alertas:
            eventos.append(Evento(
                fecha=a.abierta_en, categoria='alertas', titulo=f'Alerta: {a.regla.nombre}',
                detalle=f'Valor {a.valor_disparador:g}', tono=CRITICO if a.regla.severidad == 'critical' else ATENCION,
            ))
            if a.resuelta_en:
                eventos.append(Evento(fecha=a.resuelta_en, categoria='alertas',
                                      titulo=f'Resuelta: {a.regla.nombre}', detalle='', tono=BIEN))
    if quiero('conexion'):
        for e in EventoMonitoreo.objects.filter(estacion=estacion, timestamp__gte=desde).order_by('-timestamp')[:limite]:
            eventos.append(Evento(
                fecha=e.timestamp, categoria='conexion',
                titulo='Volvió en línea' if e.en_linea else 'Quedó fuera de línea',
                detalle=e.get_fuente_display(), tono=BIEN if e.en_linea else CRITICO,
            ))
    if quiero('windows'):
        for fila in eventos_sistema_recientes(estacion=estacion, limite=limite):
            d = fila['detectado']
            if d.ultima_vez < desde:
                continue
            eventos.append(Evento(
                fecha=d.ultima_vez, categoria='windows', titulo=f'Windows: {fila["nombre"]}',
                detalle=f'{d.cantidad_total} vez/veces en total', tono=CRITICO if fila['severidad'] == 'critical' else ATENCION,
            ))
    if quiero('accesos'):
        auditoria = (EventoAuditoria.objects.filter(
            modelo='catalogo.Estacion', objeto_id=str(estacion.pk), accion__in=list(_ACCESOS), timestamp__gte=desde,
        ).select_related('usuario').order_by('-timestamp')[:limite])
        for a in auditoria:
            eventos.append(Evento(
                fecha=a.timestamp, categoria='accesos', titulo=_ACCESOS[a.accion],
                detalle=f'Por {a.usuario.username}' if a.usuario else '', tono=INFO,
            ))

    eventos.sort(key=lambda e: e.fecha, reverse=True)
    return eventos[:limite]
