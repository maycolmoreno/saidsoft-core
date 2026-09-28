"""Alta en masa de sitios administrativos desde un CSV.

POR QUÉ UN COMANDO Y NO EL ADMIN

Son varios departamentos de 15 a 25 PCs cada uno. Cargarlos a mano por el admin es
donde aparecen los códigos con guion, los que no validan y los duplicados con otra
mayúscula — y el precio de equivocarse no se paga acá: se paga cuando el agente ya está
instalado en 20 máquinas y el enrolamiento se rechaza porque el sitio no existe con ese
código exacto (ver `apps.mqtt_worker.services._farmacia_desde_codigo_estacion`).

QUÉ ESPERA EL CSV

    codigo,nombre,departamento,ubicacion
    ADMCONT,Contabilidad,Contabilidad,Matriz piso 2
    ADMRRHH,Recursos Humanos,Recursos Humanos,Matriz piso 3

- `codigo` es obligatorio y **tiene que cumplir `^[A-Z0-9]+$`** — sin guiones, que es el
  error fácil: el guion es el separador entre sitio y equipo en el código de estación
  (`ADMCONT-PC07`), así que un sitio con guion rompe la resolución del sitio.
- `nombre` y `ubicacion` son libres. `departamento` es informativo: el reporte por
  departamento sale de `Colaborador → Departamento`, no de acá.

Simula por defecto y exige `--aplicar`, como el resto de los comandos que escriben
(ver CLAUDE.md). Es re-ejecutable: `get_or_create` por código, así que correrlo dos
veces no duplica ni pisa lo que alguien haya editado después desde el panel.

NO se cargan `ip_router`, `segmento_red` ni `circuito_proveedor` a propósito: sin
`ip_router` el sitio queda fuera del barrido de enlaces
(`apps.monitoreo.enlaces:215` lo excluye), que es lo que evita inventar caídas de
enlace en una oficina que no tiene un Mikrotik propio.
"""
import csv
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from apps.catalogo.models import Farmacia, Grupo, UnidadNegocio, codigo_farmacia_validator

#: Los creó la migración catalogo/0035. Se buscan, no se crean: si no existen es que esa
#: migración no corrió, y crearlos acá en silencio escondería ese problema.
UNIDAD = 'CORP'
GRUPO = 'ADMIN'

COLUMNAS = ('codigo', 'nombre', 'departamento', 'ubicacion')


class Command(BaseCommand):
    help = (
        'Da de alta sitios administrativos (tipo=administrativo, unidad CORP, grupo ADMIN) '
        'desde un CSV. Simula por defecto; exige --aplicar para escribir.'
    )

    def add_arguments(self, parser):
        parser.add_argument('csv', help='Ruta al CSV con las columnas: %s' % ', '.join(COLUMNAS))
        parser.add_argument(
            '--aplicar', action='store_true',
            help='Escribe de verdad. Sin esto solo muestra lo que haría.',
        )

    def handle(self, *args, **opciones):
        ruta = Path(opciones['csv'])
        if not ruta.is_file():
            raise CommandError(f'no existe {ruta}.')

        try:
            unidad = UnidadNegocio.objects.get(codigo=UNIDAD)
            grupo = Grupo.objects.get(codigo=GRUPO)
        except (UnidadNegocio.DoesNotExist, Grupo.DoesNotExist) as exc:
            raise CommandError(
                f'falta {UNIDAD} o {GRUPO} ({exc}). Los crea la migración '
                'catalogo/0035_corp_admin_y_tipo_tienda — corré `migrate` primero.',
            ) from exc

        filas = self._leer(ruta)
        validas, rechazadas = self._validar(filas)

        for numero, codigo, motivo in rechazadas:
            self.stderr.write(self.style.ERROR(f'  linea {numero}: "{codigo}" -- {motivo}'))
        if rechazadas:
            self.stderr.write(self.style.ERROR(
                f'\n{len(rechazadas)} fila(s) rechazada(s). No se escribe NADA hasta que el '
                'archivo esté entero: un alta a medias deja unos departamentos cargados y '
                'otros no, y después nadie sabe cuáles.',
            ))
            raise CommandError('el CSV tiene filas inválidas.')

        nuevos = [f for f in validas if not Farmacia.objects.filter(codigo=f['codigo']).exists()]
        existentes = [f for f in validas if f not in nuevos]

        self.stdout.write(f'Unidad: {unidad.codigo}   Grupo: {grupo.codigo}')
        self.stdout.write(f'Filas válidas: {len(validas)}   Nuevas: {len(nuevos)}   Ya existían: {len(existentes)}')
        for fila in nuevos:
            self.stdout.write(f'  + {fila["codigo"]:<12} {fila["nombre"]}')
        for fila in existentes:
            self.stdout.write(f'  = {fila["codigo"]:<12} {fila["nombre"]} (sin tocar)')

        if not opciones['aplicar']:
            self.stdout.write(self.style.WARNING('\nSIMULACRO — no se escribió nada. Repetí con --aplicar.'))
            return

        creados = self._crear(nuevos, unidad=unidad, grupo=grupo)
        self.stdout.write(self.style.SUCCESS(f'\n{creados} sitio(s) administrativo(s) creado(s).'))
        if creados:
            self.stdout.write(
                'Los equipos se enrolan con el código <SITIO>-<hostname>. Instalá UNA PC '
                'primero y comprobá que aparece en /estaciones/ antes de seguir con el resto.',
            )

    # --- Interno ------------------------------------------------------------------

    def _leer(self, ruta):
        # utf-8-sig: un CSV exportado de Excel trae BOM y sin esto la primera columna se
        # llamaría '﻿codigo' y nada encontraría el código.
        with ruta.open(encoding='utf-8-sig', newline='') as archivo:
            lector = csv.DictReader(archivo)
            if lector.fieldnames is None or 'codigo' not in lector.fieldnames:
                raise CommandError(
                    f'el CSV no tiene columna `codigo`. Encabezado esperado: {", ".join(COLUMNAS)}',
                )
            # enumerate desde 2: la 1 es el encabezado, y el número tiene que servir para
            # abrir el archivo y mirar ESA línea.
            return [(numero, fila) for numero, fila in enumerate(lector, start=2)]

    def _validar(self, filas):
        validas, rechazadas, vistos = [], [], set()
        for numero, cruda in filas:
            codigo = (cruda.get('codigo') or '').strip()
            if not codigo:
                rechazadas.append((numero, '', 'falta el código'))
                continue
            try:
                codigo_farmacia_validator(codigo)
            except Exception:
                rechazadas.append((
                    numero, codigo,
                    'el código solo admite MAYÚSCULAS y números, sin guiones ni espacios. '
                    'El guion separa sitio de equipo en el código de estación (ADMCONT-PC07), '
                    'así que un sitio con guion rompe la resolución del sitio.',
                ))
                continue
            if len(codigo) > 15:
                rechazadas.append((numero, codigo, 'el código no puede pasar de 15 caracteres'))
                continue
            if codigo in vistos:
                rechazadas.append((numero, codigo, 'repetido dentro del mismo archivo'))
                continue
            vistos.add(codigo)
            validas.append({
                'codigo': codigo,
                'nombre': (cruda.get('nombre') or codigo).strip(),
                'ubicacion': (cruda.get('ubicacion') or '').strip(),
            })
        return validas, rechazadas

    @transaction.atomic
    def _crear(self, filas, *, unidad, grupo):
        creados = 0
        for fila in filas:
            _, creado = Farmacia.objects.get_or_create(
                codigo=fila['codigo'],
                defaults={
                    'nombre': fila['nombre'],
                    'ubicacion': fila['ubicacion'],
                    'tipo': Farmacia.Tipo.ADMINISTRATIVO,
                    'unidad_negocio': unidad,
                    'grupo': grupo,
                },
            )
            creados += 1 if creado else 0
        return creados
