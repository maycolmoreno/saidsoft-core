"""Cargar al panel el nodo de cada farmacia desde la planilla de red de operaciones.

La planilla ("DATOS DE FARMACIAS.xlsx", hojas FARMAMIA y SAN GREGORIO) es la que mantiene
operaciones cuando reparte farmacias entre nodos. El panel se quedó atrás: el 16-oct-2026,
350 de 703 farmacias tenían en el panel un nodo distinto del de la planilla, y en las que
tienen agente la planilla coincidía con lo que reporta el POS en 41 de 42. Detalle en
PLAN_MODERNIZACION.md §10-CH.

Lo que esto hace, y sobre todo lo que NO hace:

- **Solo cambia `Farmacia.grupo` en el panel.** NUNCA envía la configuración del nodo a las
  estaciones (`enviar_configurar_nodo_pos`): la planilla describe dónde YA están las
  farmacias, no a dónde moverlas. Empujar desde acá reescribiría el POS de cientos de
  farmacias con lo que diga una planilla.
- **Lo que reporta el POS manda sobre la planilla.** Si las estaciones de una farmacia
  reportan otro nodo, se conserva ese y se anota la diferencia.
- **No toca otros datos de las existentes** (segmento, enlace, backup, ubicación): las
  diferencias se informan, no se aplican.
- **No crea nodos.** Un nodo de la planilla que no existe en el panel (`#N/A`, un typo) se
  informa y la fila se salta.
- **No lee la columna de contraseñas.** Las columnas se buscan por nombre exacto y esa no
  está entre ellas.
  Las contraseñas las carga aparte `cargar_claves_nodos`, al final de este módulo.
- Lee los encabezados por NOMBRE, no por posición: el importador anterior
  (`importar_red_farmacias_xlsx`) usaba posiciones, y con una columna movida habría
  guardado la columna de contraseñas como IP.
"""
import unicodedata
from collections import defaultdict
from dataclasses import dataclass, field

from django.db import transaction

from apps.auditoria.models import registrar_evento
from apps.catalogo.models import Estacion, Farmacia, Grupo, UnidadNegocio, codigo_farmacia_validator

HOJAS = ('FARMAMIA', 'SAN GREGORIO')

# Nombre normalizado del encabezado -> campo. Varios nombres porque cada hoja llama distinto
# a lo mismo. Exactos a propósito: con "contiene" una columna nueva podría colarse.
ENCABEZADOS = {
    'id de farmacia': 'codigo',
    'bdd pos': 'nodo',
    'ciudad': 'ciudad', 'canton': 'ciudad',
    'provincia': 'provincia',
    'segmento de red': 'segmento', 'red lan': 'segmento',
    'tipo de enlace': 'tipo_enlace', 'proveedor': 'tipo_enlace',
    'backup': 'backup',
}
OBLIGATORIOS = ('codigo', 'nodo')

# Filas de la planilla que no son farmacias (confirmado con el usuario).
NO_SON_FARMACIAS = {'MPREV1', 'PRESIDENCIA', 'SALA CRM'}

NEGATIVOS_BACKUP = {'', 'no', 'inactivo', 'false', '0', 'n'}


def _normalizar(texto) -> str:
    sin_acentos = unicodedata.normalize('NFKD', str(texto or '')).encode('ascii', 'ignore').decode('ascii')
    return ' '.join(sin_acentos.strip().lower().split())


def _texto(valor) -> str:
    return '' if valor is None else str(valor).strip()


class PlanillaInvalida(Exception):
    pass


def leer_planilla(libro) -> dict[str, dict]:
    """`{codigo: {'nodo', 'ciudad', 'provincia', 'segmento', 'tipo_enlace', 'backup', 'hoja'}}`."""
    filas = {}
    for nombre_hoja in HOJAS:
        if nombre_hoja not in libro.sheetnames:
            raise PlanillaInvalida(f'Falta la hoja "{nombre_hoja}".')
        iterador = libro[nombre_hoja].iter_rows(values_only=True)
        cabecera = next(iterador, ())
        columnas = {}
        for indice, titulo in enumerate(cabecera):
            campo = ENCABEZADOS.get(_normalizar(titulo))
            if campo and campo not in columnas:
                columnas[campo] = indice
        faltan = [c for c in OBLIGATORIOS if c not in columnas]
        if faltan:
            raise PlanillaInvalida(
                f'Hoja "{nombre_hoja}": no se encontraron las columnas {faltan}. '
                f'Encabezados leídos: {[t for t in cabecera if t]}',
            )
        for fila in iterador:
            valores = {c: _texto(fila[i]) if i < len(fila) else '' for c, i in columnas.items()}
            codigo = valores['codigo'].upper()
            if not codigo:
                continue
            valores['nodo'] = valores['nodo'].lower()
            valores['hoja'] = nombre_hoja
            filas[codigo] = valores
    return filas


@dataclass
class Cambio:
    codigo: str
    accion: str          # cambia_nodo | igual | conserva_pos | crea | omitida | error
    nodo_panel: str = ''
    nodo_planilla: str = ''
    nodo_pos: str = ''
    nota: str = ''


@dataclass
class Resultado:
    cambios: list[Cambio] = field(default_factory=list)
    otras_diferencias: list[tuple[str, str, str, str]] = field(default_factory=list)

    def por_accion(self, accion):
        return [c for c in self.cambios if c.accion == accion]


def _nodos_reportados() -> dict[str, set[str]]:
    reportados = defaultdict(set)
    for codigo_farmacia, bdd in Estacion.objects.exclude(pos_bdd='').values_list('farmacia__codigo', 'pos_bdd'):
        reportados[codigo_farmacia].add(bdd.strip().lower())
    return reportados


def cargar_nodos(filas: dict[str, dict], *, usuario, aplicar=False, omitir=(), request=None) -> Resultado:
    """Alinea el nodo del panel con la planilla. Simula salvo `aplicar=True`."""
    resultado = Resultado()
    omitir = {c.upper() for c in omitir}
    grupos_por_bdd = {g.bdd_pos.strip().lower(): g for g in Grupo.objects.all()}
    unidades = {u.codigo: u for u in UnidadNegocio.objects.all()}
    existentes = {f.codigo: f for f in Farmacia.objects.select_related('grupo')}
    reportados = _nodos_reportados()

    with transaction.atomic():
        for codigo, fila in sorted(filas.items()):
            if codigo in NO_SON_FARMACIAS:
                continue
            farmacia = existentes.get(codigo)
            grupo = grupos_por_bdd.get(fila['nodo'])
            if grupo is None:
                resultado.cambios.append(Cambio(
                    codigo, 'error', nodo_panel=farmacia.grupo.codigo if farmacia else '',
                    nodo_planilla=fila['nodo'] or '(vacío)',
                    nota='el nodo de la planilla no existe en el panel; no se toca'))
                continue

            if farmacia is None:
                _crear(codigo, fila, grupo, unidades, omitir, resultado, usuario=usuario,
                       aplicar=aplicar, request=request)
                continue

            _comparar_otros(farmacia, fila, resultado)
            pos = reportados.get(codigo, set())
            nodo_pos = ', '.join(sorted(pos))
            if len(pos) > 1:
                resultado.cambios.append(Cambio(
                    codigo, 'conserva_pos', farmacia.grupo.codigo, grupo.codigo, nodo_pos,
                    'sus estaciones reportan nodos distintos entre sí; no se toca'))
                continue
            if pos and next(iter(pos)) != grupo.bdd_pos.lower():
                destino = grupos_por_bdd.get(next(iter(pos)))
                if destino is None or destino.pk == farmacia.grupo_id:
                    resultado.cambios.append(Cambio(
                        codigo, 'conserva_pos', farmacia.grupo.codigo, grupo.codigo, nodo_pos,
                        'el POS reporta otro nodo que la planilla; manda el POS'))
                    continue
                grupo, nota = destino, 'el POS reporta otro nodo que la planilla; se usa el del POS'
            else:
                nota = ''

            if farmacia.grupo_id == grupo.pk:
                resultado.cambios.append(Cambio(codigo, 'igual', grupo.codigo, fila['nodo'].upper(), nodo_pos, nota))
                continue

            anterior = farmacia.grupo.codigo
            resultado.cambios.append(Cambio(codigo, 'cambia_nodo', anterior, fila['nodo'].upper(), nodo_pos, nota))
            if aplicar:
                farmacia.grupo = grupo
                farmacia.save(update_fields=['grupo'])
                registrar_evento(
                    usuario=usuario, accion='farmacia.nodo_desde_planilla', objeto=farmacia, request=request,
                    detalle={'antes': anterior, 'despues': grupo.codigo, 'nodo_pos': nodo_pos},
                )
        if not aplicar:
            transaction.set_rollback(True)
    return resultado


def _crear(codigo, fila, grupo, unidades, omitir, resultado, *, usuario, aplicar, request):
    from apps.catalogo.services import _unidad_negocio_por_prefijo

    if codigo in omitir:
        resultado.cambios.append(Cambio(codigo, 'omitida', nodo_planilla=grupo.codigo,
                                        nota='no está en el panel; omitida a pedido'))
        return
    try:
        codigo_farmacia_validator(codigo)
    except Exception:
        resultado.cambios.append(Cambio(codigo, 'error', nodo_planilla=grupo.codigo,
                                        nota='el código no es válido para una farmacia'))
        return
    unidad = unidades.get(_unidad_negocio_por_prefijo(codigo) or '')
    if unidad is None:
        resultado.cambios.append(Cambio(codigo, 'error', nodo_planilla=grupo.codigo,
                                        nota='no se puede deducir la unidad de negocio del código'))
        return
    ciudad, provincia = fila.get('ciudad', ''), fila.get('provincia', '')
    resultado.cambios.append(Cambio(codigo, 'crea', nodo_planilla=grupo.codigo,
                                    nota=f'{unidad.codigo}, {ciudad or "?"}'))
    if aplicar:
        farmacia = Farmacia.objects.create(
            codigo=codigo, grupo=grupo, unidad_negocio=unidad,
            ubicacion=f'{ciudad}, {provincia}' if ciudad and provincia else (ciudad or provincia),
            segmento_red=fila.get('segmento', '')[:Farmacia._meta.get_field('segmento_red').max_length],
            tipo_enlace=fila.get('tipo_enlace', '')[:Farmacia._meta.get_field('tipo_enlace').max_length],
            tiene_backup=_normalizar(fila.get('backup')) not in NEGATIVOS_BACKUP,
        )
        registrar_evento(usuario=usuario, accion='farmacia.crear_desde_planilla', objeto=farmacia,
                         request=request, detalle={'nodo': grupo.codigo})


def _comparar_otros(farmacia, fila, resultado):
    """Diferencias de red que se INFORMAN y no se aplican."""
    for etiqueta, en_panel, en_planilla in (
        ('segmento', farmacia.segmento_red, fila.get('segmento', '')),
        ('tipo_enlace', farmacia.tipo_enlace, fila.get('tipo_enlace', '')),
    ):
        if _normalizar(en_panel) != _normalizar(en_planilla):
            resultado.otras_diferencias.append((farmacia.codigo, etiqueta, en_panel, en_planilla))


# --- Contraseñas de los nodos -------------------------------------------------------
#
# Aparte de `cargar_nodos` a propósito, que NO lee esta columna. El 16-oct-2026 las de la
# planilla no coincidían con las del panel en TRX001-007 y TRX002/TRX008 no tenían
# ninguna; el usuario confirmó el 17-oct que las vigentes son las de la planilla.
#
# Solo se guardan CIFRADAS en el nodo del panel (`establecer_password_nodo`). No se
# muestran en ningún lado —ni salida, ni auditoría—, y no se envía nada a las estaciones:
# eso lo hace "Aplicar nodo", que es otra decisión.

ENCABEZADO_CLAVE = 'contrasena'


def leer_claves_planilla(libro) -> dict[str, str]:
    """`{bdd_pos: contraseña}`. Exige UNA sola por nodo en toda la planilla: dos distintas
    para el mismo nodo es un error de la planilla, y elegir una sería adivinar."""
    claves = defaultdict(set)
    for nombre_hoja in HOJAS:
        if nombre_hoja not in libro.sheetnames:
            raise PlanillaInvalida(f'Falta la hoja "{nombre_hoja}".')
        iterador = libro[nombre_hoja].iter_rows(values_only=True)
        cabecera = [_normalizar(t) for t in next(iterador, ())]
        try:
            i_nodo, i_clave = cabecera.index('bdd pos'), cabecera.index(ENCABEZADO_CLAVE)
        except ValueError:
            raise PlanillaInvalida(f'Hoja "{nombre_hoja}": faltan las columnas "Bdd pos" o "contraseña".')
        for fila in iterador:
            nodo = _texto(fila[i_nodo] if i_nodo < len(fila) else '').lower()
            clave = '' if i_clave >= len(fila) or fila[i_clave] is None else str(fila[i_clave])
            if nodo and clave and nodo not in ('#n/a',):
                claves[nodo].add(clave)
    dobles = sorted(n for n, cs in claves.items() if len(cs) > 1)
    if dobles:
        raise PlanillaInvalida(f'Nodos con más de una contraseña en la planilla: {dobles}. No se carga nada.')
    return {n: next(iter(cs)) for n, cs in claves.items()}


def cargar_claves_nodos(claves: dict[str, str], *, usuario, aplicar=False) -> list[tuple[str, str]]:
    """`[(codigo_nodo, resultado)]` con resultado `igual`, `cambia`, `nueva` o
    `sin nodo en el panel`. Simula salvo `aplicar=True`."""
    from apps.catalogo.services import establecer_password_nodo, obtener_password_nodo

    por_bdd = {g.bdd_pos.strip().lower(): g for g in Grupo.objects.all()}
    resultado = []
    with transaction.atomic():
        for bdd, clave in sorted(claves.items()):
            grupo = por_bdd.get(bdd)
            if grupo is None:
                resultado.append((bdd.upper(), 'sin nodo en el panel'))
                continue
            actual = obtener_password_nodo(grupo)
            estado = 'igual' if actual == clave else ('cambia' if actual else 'nueva')
            resultado.append((grupo.codigo, estado))
            if aplicar and estado != 'igual':
                establecer_password_nodo(grupo, clave)
                registrar_evento(
                    usuario=usuario, accion='grupo.clave_nodo_desde_planilla', objeto=grupo,
                    detalle={'tenia_clave': bool(actual)},
                )
    return resultado
