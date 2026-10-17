"""Historial de caídas de enlace en un período: por farmacia, por proveedor y evento a evento.

Hasta el 17-oct-2026 el historial solo se veía de a una farmacia (las últimas 5, en su
ficha) o en el Admin, sin exportar y sin disponibilidad. Para reclamarle un SLA a Telconet
o a Punto Net hace falta lo contrario: el período entero, por proveedor, con el % de
disponibilidad de cada sitio. Ver PLAN_MODERNIZACION.md §10-CK.

**Qué cuenta contra el proveedor y qué no.** Cada caída se clasifica con los mismos
racimos que `apps.monitoreo.enlaces.clasificar_caidas_simultaneas`:

- `individual` y `proveedor` (varias del mismo ISP a la vez) cuentan: son del enlace.
- `ceguera` (varios ISP a la vez) NO cuenta para la disponibilidad: lo más probable es que
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
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from django.utils import timezone

from apps.monitoreo.enlaces import (
    MINIMO_PARA_CORTE_DE_PROVEEDOR, PROVEEDORES_PARA_CEGUERA, VENTANA_SIMULTANEAS_MINUTOS,
    nombre_proveedor,
)

ETIQUETA_CLASE = {
    'individual': 'Del sitio',
    'proveedor': 'Corte del proveedor',
    'ceguera': 'Monitoreo sin visibilidad',
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
        return self.clase != 'ceguera'


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

    @property
    def minutos_periodo(self) -> float:
        return (self.hasta - self.desde).total_seconds() / 60


def _clasificar(eventos):
    """`{evento.pk: (clase, n° de racimo)}` armando racimos por hora de inicio, abiertos o cerrados."""
    ventana = timedelta(minutes=VENTANA_SIMULTANEAS_MINUTOS)
    racimos, actual = [], []
    for e in sorted(eventos, key=lambda e: e.inicio):
        if actual and (e.inicio - actual[-1].inicio) > ventana:
            racimos.append(actual)
            actual = []
        actual.append(e)
    if actual:
        racimos.append(actual)
    clases = {}
    for numero, racimo in enumerate(racimos):
        proveedores = {nombre_proveedor(e.farmacia) for e in racimo}
        if len(racimo) < MINIMO_PARA_CORTE_DE_PROVEEDOR:
            clase = 'individual'
        elif len(proveedores) >= PROVEEDORES_PARA_CEGUERA:
            clase = 'ceguera'
        elif len(proveedores) == 1:
            clase = 'proveedor'
        else:
            clase = 'individual'
        for e in racimo:
            clases[e.pk] = (clase, numero)
    return clases


def historial_enlaces(farmacias, desde, hasta, *, proveedor='', minimo_minutos=0) -> Historial:
    """El historial del período [desde, hasta). `farmacias` es el queryset del alcance del
    usuario (ya filtrado por cliente/nodo/código si corresponde)."""
    from apps.monitoreo.models import EventoEnlaceFarmacia

    ahora = timezone.now()
    hasta_real = min(hasta, ahora)
    # Para clasificar se mira TODA la flota, no solo las del filtro: un corte del
    # proveedor se reconoce por lo que pasó alrededor, y filtrar por una farmacia
    # convertiría cualquier corte masivo en una caída "del sitio".
    todos = list(
        EventoEnlaceFarmacia.objects.filter(
            inicio__lt=hasta, farmacia__estado_enlace__respondio_alguna_vez=True,
        ).exclude(fin__lte=desde).select_related('farmacia', 'farmacia__unidad_negocio', 'farmacia__grupo'),
    )
    clases = _clasificar(todos)
    permitidas = set(farmacias.values_list('pk', flat=True))

    caidas = []
    for e in todos:
        if e.farmacia_id not in permitidas:
            continue
        isp = nombre_proveedor(e.farmacia)
        if proveedor and isp != proveedor:
            continue
        fin = e.fin or ahora
        minutos = max(0.0, (min(fin, hasta_real) - max(e.inicio, desde)).total_seconds() / 60)
        if minutos < minimo_minutos:
            continue
        clase, racimo = clases[e.pk]
        caidas.append(Caida(evento=e, clase=clase, minutos=minutos, proveedor=isp, racimo=racimo))
    caidas.sort(key=lambda c: c.evento.inicio, reverse=True)

    minutos_periodo = max((hasta_real - desde).total_seconds() / 60, 1)
    resumen = {}
    proveedores = {}
    racimos_contados = set()
    for c in caidas:
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

    return Historial(
        desde=desde, hasta=hasta_real, caidas=caidas,
        por_farmacia=sorted(resumen.values(), key=lambda r: (r.disponibilidad_sla, -r.caidas_sla)),
        por_proveedor=sorted(proveedores.values(), key=lambda p: -p.minutos_sla),
        excluidas_por_ceguera=sum(1 for c in caidas if not c.cuenta_para_sla),
    )
