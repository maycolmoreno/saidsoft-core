"""Enriquece las Farmacia ya existentes con los datos del directorio de sucursales de
RRHH ("directorio_sucursal_Agosto.xlsx", hoja "Directorio Personal") — nombre
completo, horario, administrador, coordinadores, dirección, tipo/formato de
sucursal, coordenadas, fechas de operación/RUC, teléfono/correo y técnico asignado.

NO crea farmacias — se corre DESPUÉS de `importar_red_farmacias_xlsx` (que sí crea
las que falten, con su Grupo/unidad de negocio correctos). Una fila cuyo código no
existe todavía en SAIDSOFT se reporta como error, nunca se crea a medias sin grupo.

El técnico se vincula por cédula usando el mapeo fijo en
apps.activos.management.commands.crear_tecnicos_soporte.NOMBRE_DIRECTORIO_A_CEDULA
(varias personas figuran en este directorio por su segundo nombre, no alcanza con
comparar texto) — correr `crear_tecnicos_soporte` antes de este comando.

Uso:
    python manage.py importar_directorio_sucursales directorio_sucursal_Agosto.xlsx            # simulacro
    python manage.py importar_directorio_sucursales directorio_sucursal_Agosto.xlsx --aplicar
    python manage.py importar_directorio_sucursales data.cresio.com.json --solo-vacios       # simulacro
    python manage.py importar_directorio_sucursales data.cresio.com.json --solo-vacios --aplicar

**Dos formatos, el mismo directorio.** El Excel mensual de RRHH y el JSON que exporta
data.cresio.com traen las mismas columnas con otros nombres; se reconoce por la extensión.
El JSON no trae técnico: con él `tecnico_asignado` no se toca.

**`--solo-vacios`** completa únicamente los campos que hoy están vacíos en SAIDSOFT y no
pisa nada cargado (sin él, el directorio manda sobre lo que haya, que es el uso mensual).
Existe para llenar huecos -teléfono y administrador para los avisos de enlace caído
(23-oct-2026)- sin arriesgar datos que alguien corrigió a mano. El simulacro lista, farmacia
por farmacia, qué campo cambiaría y a qué valor.
"""
import datetime
import json
from email.utils import parsedate_to_datetime
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from apps.activos.management.commands.crear_tecnicos_soporte import NOMBRE_DIRECTORIO_A_CEDULA
from apps.activos.models import Colaborador
from apps.catalogo.models import Farmacia

COLUMNAS = {
    'nombre_sucursal': 1, 'marca': 2, 'ciudad': 3, 'sucursal': 4, 'horario': 5,
    'administrador': 6, 'coordinador_zonal': 7, 'extension': 8, 'celular': 9,
    'correo': 10, 'provincia': 11, 'coordinador_regional': 12, 'direccion': 13,
    'tipo_sucursal': 14, 'latitud': 15, 'longitud': 16, 'formato_farmacia': 17,
    'parroquia': 18, 'fecha_inicio_op': 19, 'fecha_inicio_ruc': 20, 'tecnico': 21,
}


def _limpiar_texto(valor):
    if valor is None:
        return ''
    # _x000D_ es un artefacto de Excel (retorno de carro escapado en el XML de shared
    # strings) que queda como texto literal en la celda, no un problema real de datos.
    return str(valor).strip().replace('_x000D_', ' ').strip()


def _normalizar_choice(valor):
    return _limpiar_texto(valor).upper().replace(' ', '_').lower()


def _a_numero(valor, campo, fila_num, codigo, errores):
    """Coordenada como float, o None si viene vacía o no es un número. Antes un texto en la
    celda tiraba ValueError y abortaba toda la importación."""
    if valor in (None, ''):
        return None
    try:
        return float(valor)
    except (TypeError, ValueError):
        errores.append(f'fila {fila_num} ({codigo}): {campo} "{valor}" no es un número, se ignora.')
        return None


def _a_fecha(valor):
    if isinstance(valor, datetime.datetime):
        return valor.date()
    if isinstance(valor, datetime.date):
        return valor
    if isinstance(valor, str) and valor.strip():
        # El JSON trae fechas HTTP ("Mon, 22 Nov 2021 00:00:00 GMT").
        try:
            return parsedate_to_datetime(valor).date()
        except (TypeError, ValueError):
            return None
    return None


# Nombre en el JSON de data.cresio.com -> clave de COLUMNAS (las demás se llaman igual).
CLAVES_JSON = {'ext_ip': 'extension', 'correo_electronico': 'correo'}


def _filas_xlsx(ruta):
    """`(numero_de_fila, {clave: valor})` del Excel de RRHH, hoja "Directorio Personal"."""
    import openpyxl

    try:
        libro = openpyxl.load_workbook(ruta, data_only=True)
    except (OSError, KeyError) as exc:
        raise CommandError(f'No se pudo abrir {ruta}: {exc}')
    if 'Directorio Personal' not in libro.sheetnames:
        raise CommandError(f'No se encontró la hoja "Directorio Personal" en {ruta}.')
    hoja = libro['Directorio Personal']
    for fila_num, fila in enumerate(hoja.iter_rows(min_row=2, values_only=True), start=2):
        yield fila_num, {
            clave: (fila[indice - 1] if indice - 1 < len(fila) else None)
            for clave, indice in COLUMNAS.items()
        }


def _filas_json(ruta):
    """Lo mismo desde el JSON de data.cresio.com (una lista de objetos)."""
    try:
        datos = json.loads(Path(ruta).read_text(encoding='utf-8-sig'))
    except (OSError, ValueError) as exc:
        raise CommandError(f'No se pudo leer {ruta}: {exc}')
    if not isinstance(datos, list):
        raise CommandError(f'{ruta}: se esperaba una lista de sucursales.')
    for numero, registro in enumerate(datos, start=1):
        if isinstance(registro, dict):
            yield numero, {CLAVES_JSON.get(k, k): v for k, v in registro.items()}


def _codigo(valor):
    """El JSON trae "GES02 - 232" (código + número interno): el código es lo de antes."""
    return _limpiar_texto(valor).split(' - ')[0].strip().upper()


class Command(BaseCommand):
    help = 'Enriquece las Farmacia ya existentes con el directorio de sucursales de RRHH.'

    def add_arguments(self, parser):
        parser.add_argument('xlsx_path', help='Excel de RRHH (.xlsx) o JSON de data.cresio.com (.json).')
        parser.add_argument(
            '--solo-vacios', action='store_true',
            help='Completa solo los campos vacíos; no pisa nada que ya esté cargado.',
        )
        # `--aplicar` y no `--dry-run`: la diferencia es cual es el DEFAULT. Con
        # --dry-run habia que acordarse de pedir el simulacro, asi que correr el comando
        # sin flags reescribia los datos de las 700 farmacias. CLAUDE.md lo fija al reves —
        # "simulan por defecto y exigen --aplicar"— justamente para que el descuido sea
        # inofensivo. `--dry-run` se sigue aceptando por compatibilidad: ya no hace falta,
        # porque simular es lo que pasa solo.
        parser.add_argument(
            '--aplicar', action='store_true',
            help='Escribe los cambios. Sin esto solo muestra qué haría.',
        )
        parser.add_argument(
            '--dry-run', action='store_true',
            help='Redundante: simular ya es el comportamiento por defecto. Se acepta para no '
                 'romper invocaciones viejas.',
        )

    def handle(self, *args, **options):
        ruta = options['xlsx_path']
        dry_run = not options['aplicar']
        solo_vacios = options['solo_vacios']
        es_json = ruta.lower().endswith('.json')
        filas = _filas_json(ruta) if es_json else _filas_xlsx(ruta)

        valores_tipo_sucursal = {c[0] for c in Farmacia.TipoSucursal.choices}
        valores_formato = {c[0] for c in Farmacia.FormatoFarmacia.choices}
        colaboradores_por_cedula = {c.cedula: c for c in Colaborador.objects.all()}

        actualizadas, sin_farmacia, sin_tecnico, errores, cambios = [], [], [], [], []
        # Una consulta para todas, no una por fila.
        farmacias = {f.codigo: f for f in Farmacia.objects.all()}

        with transaction.atomic():
            sp = transaction.savepoint()
            for fila_num, datos in filas:
                def _col(clave, datos=datos):
                    return datos.get(clave)

                codigo = _codigo(_col('sucursal'))
                if not codigo:
                    continue

                farmacia = farmacias.get(codigo)
                if farmacia is None:
                    sin_farmacia.append(codigo)
                    continue

                tipo_sucursal = _normalizar_choice(_col('tipo_sucursal'))
                if tipo_sucursal and tipo_sucursal not in valores_tipo_sucursal:
                    errores.append(f'fila {fila_num} ({codigo}): tipo_sucursal "{_col("tipo_sucursal")}" desconocido.')
                    tipo_sucursal = ''

                formato_farmacia = _normalizar_choice(_col('formato_farmacia'))
                if formato_farmacia and formato_farmacia not in valores_formato:
                    errores.append(
                        f'fila {fila_num} ({codigo}): formato_farmacia "{_col("formato_farmacia")}" desconocido.',
                    )
                    formato_farmacia = ''

                nombre_tecnico = '' if es_json else _limpiar_texto(_col('tecnico')).upper()
                tecnico = None
                if nombre_tecnico and nombre_tecnico != 'N/D':
                    cedula_tecnico = NOMBRE_DIRECTORIO_A_CEDULA.get(nombre_tecnico)
                    tecnico = colaboradores_por_cedula.get(cedula_tecnico) if cedula_tecnico else None
                    if tecnico is None:
                        sin_tecnico.append(f'{codigo} ({nombre_tecnico})')

                extension = _col('extension')
                try:
                    extension = int(extension) if extension not in (None, '') else None
                    # 0 no es una extensión: así marca el directorio "sin extensión".
                    if extension is not None and extension <= 0:
                        extension = None
                except (TypeError, ValueError):
                    extension = None

                # Una celda vacía en el directorio es "RRHH no lo cargó", no "borrarlo". Antes
                # se copiaba el vacío encima: con el de septiembre, GLG01 perdía su horario
                # (y con él el corte de horario de atención del tablero NOC) y MGY01 su
                # dirección (16-oct-2026). Teléfono y correo ya se trataban así.
                nuevos = {
                    'nombre': _limpiar_texto(_col('nombre_sucursal')),
                    'administrador': _limpiar_texto(_col('administrador')),
                    'coordinador_zonal': _limpiar_texto(_col('coordinador_zonal')),
                    'coordinador_regional': _limpiar_texto(_col('coordinador_regional')),
                    'ciudad': _limpiar_texto(_col('ciudad')),
                    'provincia': _limpiar_texto(_col('provincia')),
                    'parroquia': _limpiar_texto(_col('parroquia')),
                    'direccion': _limpiar_texto(_col('direccion')),
                    'horario': _limpiar_texto(_col('horario')),
                    'tipo_sucursal': tipo_sucursal,
                    'formato_farmacia': formato_farmacia,
                    'latitud': _a_numero(_col('latitud'), 'latitud', fila_num, codigo, errores),
                    'longitud': _a_numero(_col('longitud'), 'longitud', fila_num, codigo, errores),
                    'fecha_inicio_operacion': _a_fecha(_col('fecha_inicio_op')),
                    'fecha_inicio_ruc': _a_fecha(_col('fecha_inicio_ruc')),
                    'extension_telefonica': extension,
                    'telefono': _limpiar_texto(_col('celular')),
                    'email': _limpiar_texto(_col('correo')),
                    'tecnico_asignado': tecnico,
                }
                cambiados = []
                for campo, valor in nuevos.items():
                    if valor in (None, ''):
                        continue
                    actual = getattr(farmacia, campo)
                    if solo_vacios and actual not in (None, ''):
                        continue
                    # Una celda más larga que la columna abortaba la importación ENTERA con
                    # un DataError de PostgreSQL. Se recorta y se avisa.
                    largo = getattr(Farmacia._meta.get_field(campo), 'max_length', None)
                    if largo and isinstance(valor, str) and len(valor) > largo:
                        errores.append(f'fila {fila_num} ({codigo}): {campo} recortado a {largo} caracteres.')
                        valor = valor[:largo]
                    if actual != valor:
                        cambios.append(f'{codigo}: {campo}: {actual!r} -> {valor!r}')
                        cambiados.append(campo)
                    setattr(farmacia, campo, valor)

                if not cambiados:
                    continue
                if not dry_run:
                    farmacia.save(update_fields=cambiados)
                actualizadas.append(codigo)

            if dry_run:
                transaction.savepoint_rollback(sp)
            else:
                transaction.savepoint_commit(sp)

        prefijo = '[SIMULACRO] ' if dry_run else ''
        modo = ' (solo campos vacíos)' if solo_vacios else ''
        self.stdout.write(self.style.SUCCESS(
            f'{prefijo}{len(actualizadas)} farmacia(s) con cambios{modo}.',
        ))
        if cambios and (dry_run or solo_vacios):
            for linea in cambios:
                self.stdout.write(f'  {linea}')
        if sin_farmacia:
            self.stdout.write(self.style.ERROR(
                f'{len(sin_farmacia)} código(s) del directorio sin Farmacia todavía en SAIDSOFT '
                f'(correr importar_red_farmacias_xlsx primero): {", ".join(sin_farmacia)}',
            ))
        if sin_tecnico:
            self.stdout.write(self.style.WARNING(
                f'{len(sin_tecnico)} farmacia(s) con técnico sin mapeo a Colaborador conocido: '
                f'{", ".join(sin_tecnico)}',
            ))
        if errores:
            self.stdout.write(self.style.ERROR(f'{len(errores)} advertencia(s) de datos:'))
            for err in errores:
                self.stdout.write(self.style.ERROR(f'  - {err}'))
