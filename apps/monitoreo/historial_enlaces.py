"""Historial de caídas de enlace en un período: por farmacia, por proveedor y evento a evento.

Hasta el 17-oct-2026 el historial solo se veía de a una farmacia (las últimas 5, en su
ficha) o en el Admin, sin exportar y sin disponibilidad. Para reclamarle un SLA a Telconet
o a Punto Net hace falta lo contrario: el período entero, por proveedor, con el % de
disponibilidad de cada sitio. Ver PLAN_MODERNIZACION.md §10-CK.

**Qué cuenta contra el proveedor y qué no.** Cada caída se clasifica con la regla única
del proyecto (`apps.monitoreo.enlaces.clase_en_racimo`), la misma del tablero:

- `individual` y `proveedor` (varias del mismo ISP a la vez) cuentan: son del enlace.
- `ceguera` (dos o más ISP cayendo en cantidad a la vez) NO cuenta para la disponibilidad: lo más probable es que
  el que dejó de ver fue el monitoreo. El 17-oct-2026, 112 farmacias de tres proveedores
  "cayeron" en 15 minutos por un corte de luz en el data center; cargarle eso a cada ISP
  inflaría el reclamo con algo que no es suyo, y el proveedor lo rebate con razón.

Se muestran igual —el dato no se esconde—, pero la disponibilidad "para SLA" los excluye.

**Duración recortada al período.** Una caída que empezó antes del `desde` o sigue abierta
cuenta solo los minutos dentro del período: si no, una farmacia caída 23 días sumaría 23
días en un informe de una semana.

Los sitios que nunca respondieron no entran: no son caídas (ver
`EstadoEnlaceFarmacia.respondio_alguna_vez`).
"""
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from django.utils import timezone

from apps.monitoreo.enlaces import TOPE_RACIMO_MINUTOS, armar_racimos, clase_en_racimo, nombre_proveedor

ETIQUETA_CLASE = {
    'individual': 'Del sitio',
    'proveedor': 'Corte del proveedor',
    'ceguera': 'Monitoreo sin visibilidad',
    'luz': 'Corte de luz (confirmado)',
}


@dataclass
class Caida:
    evento: object
    clase: str
    minutos: float          # dentro del período
    proveedor: str
    racimo: int = 0

    @property
    def farmacia(self):
        return self.evento.farmacia

    @property
    def cuenta_para_sla(self) -> bool:
        # Ni lo que el monitoreo no vio, ni un corte de luz que confirmó la farmacia o la
        # mesa de ayuda (§10-CM): ninguno de los dos es del enlace del proveedor.
        return self.clase not in ('ceguera', 'luz')


@dataclass
class ResumenFarmacia:
    farmacia: object
    proveedor: str
    caidas: int = 0
    minutos: float = 0.0
    minutos_sla: float = 0.0
    caidas_sla: int = 0
    disponibilidad_sla: float = 100.0


@dataclass
class ResumenProveedor:
    proveedor: str
    farmacias: set = field(default_factory=set)
    caidas: int = 0
    minutos_sla: float = 0.0
    cortes_masivos: int = 0


@dataclass
class Historial:
    desde: datetime
    hasta: datetime
    caidas: list
    por_farmacia: list
    por_proveedor: list
    excluidas_por_ceguera: int
    excluidas_por_luz: int = 0

    @property
    def minutos_periodo(self) -> float:
        return (self.hasta - self.desde).total_seconds() / 60


def _clasificar(eventos):
    """`{evento.pk: (clase, n° de racimo)}` con LA regla del proyecto
    (`apps.monitoreo.enlaces.clase_en_racimo`), la misma del tablero y la tabla."""
    clases = {}
    for numero, racimo in enumerate(armar_racimos(eventos)):
        for pk, clase in clase_en_racimo(racimo).items():
            clases[pk] = (clase, numero)
    return clases


def historial_enlaces(farmacias, desde, hasta, *, proveedor='', minimo_minutos=0) -> Historial:
    """El historial del período [desde, hasta). `farmacias` es el queryset del alcance del
    usuario (ya filtrado por cliente/nodo/código si corresponde).

    `minimo_minutos` solo achica LO QUE SE LISTA: la disponibilidad y los totales se
    calculan con todas las caídas. Antes filtraba antes de sumar, y una farmacia con 40
    caídas de 45 minutos salía con 100 % al pedir "1 hora o más".
    """
    from apps.monitoreo.models import EventoEnlaceFarmacia

    ahora = timezone.now()
    hasta_real = min(hasta, ahora)
    solapados = list(
        EventoEnlaceFarmacia.objects.filter(
            inicio__lt=hasta, farmacia__estado_enlace__respondio_alguna_vez=True,
        ).exclude(fin__lte=desde).values_list('pk', 'inicio'),
    )
    if not solapados:
        return Historial(desde=desde, hasta=hasta_real, caidas=[], por_farmacia=[], por_proveedor=[],
                         excluidas_por_ceguera=0)
    # Para clasificar se mira TODA la flota y un margen alrededor, no solo lo que se
    # solapa con el período: un corte se reconoce por lo que pasó alrededor, y recortarlo
    # al borde hacía que la misma caída cambiara de clase según el "desde" pedido (una del
    # apagón del 17-oct, vista desde el 18, quedaba sola y pasaba a "del proveedor").
    margen = timedelta(minutes=2 * TOPE_RACIMO_MINUTOS)
    incluidos = {pk for pk, _ in solapados}
    contexto = list(
        EventoEnlaceFarmacia.objects.filter(
            inicio__gte=min(i for _, i in solapados) - margen, inicio__lt=hasta + margen,
            farmacia__estado_enlace__respondio_alguna_vez=True,
        ).select_related('farmacia', 'farmacia__unidad_negocio', 'farmacia__grupo'),
    )
    clases = _clasificar(contexto)
    permitidas = set(farmacias.values_list('pk', flat=True))
    from apps.whatsapp.models import Novedad

    con_luz = set(Novedad.objects.filter(evento_enlace__in=incluidos, causa=Novedad.Causa.LUZ)
                  .values_list('evento_enlace_id', flat=True))

    todas = []
    for e in contexto:
        if e.pk not in incluidos or e.farmacia_id not in permitidas:
            continue
        isp = nombre_proveedor(e.farmacia)
        if proveedor and isp != proveedor:
            continue
        fin = e.fin or ahora
        minutos = max(0.0, (min(fin, hasta_real) - max(e.inicio, desde)).total_seconds() / 60)
        clase, racimo = clases[e.pk]
        if e.pk in con_luz:
            clase = 'luz'
        todas.append(Caida(evento=e, clase=clase, minutos=minutos, proveedor=isp, racimo=racimo))
    todas.sort(key=lambda c: c.evento.inicio, reverse=True)

    minutos_periodo = max((hasta_real - desde).total_seconds() / 60, 1)
    resumen = {}
    proveedores = {}
    racimos_contados = set()
    for c in todas:
        r = resumen.setdefault(c.farmacia.pk, ResumenFarmacia(farmacia=c.farmacia, proveedor=c.proveedor))
        r.caidas += 1
        r.minutos += c.minutos
        p = proveedores.setdefault(c.proveedor, ResumenProveedor(proveedor=c.proveedor))
        p.farmacias.add(c.farmacia.pk)
        if c.cuenta_para_sla:
            r.caidas_sla += 1
            r.minutos_sla += c.minutos
            p.caidas += 1
            p.minutos_sla += c.minutos
        if c.clase == 'proveedor':
            clave = (c.proveedor, c.racimo)  # un corte se cuenta una vez
            if clave not in racimos_contados:
                racimos_contados.add(clave)
                p.cortes_masivos += 1
    for r in resumen.values():
        r.disponibilidad_sla = round(100 * (1 - min(r.minutos_sla, minutos_periodo) / minutos_periodo), 2)

    listadas = [c for c in todas if c.minutos >= minimo_minutos]
    con_listadas = {c.farmacia.pk for c in listadas}
    return Historial(
        desde=desde, hasta=hasta_real, caidas=listadas,
        por_farmacia=sorted((r for r in resumen.values() if r.farmacia.pk in con_listadas),
                            key=lambda r: (r.disponibilidad_sla, -r.caidas_sla)),
        por_proveedor=sorted(proveedores.values(), key=lambda p: -p.minutos_sla),
        excluidas_por_ceguera=sum(1 for c in todas if c.clase == 'ceguera'),
        excluidas_por_luz=sum(1 for c in todas if c.clase == 'luz'),
    )
