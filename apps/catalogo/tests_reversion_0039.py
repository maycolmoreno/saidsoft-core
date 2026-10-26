"""Ensayo de reversión de `catalogo.0039_orden_agente` a `0038`, sobre la base de PRUEBAS.

Se saltea salvo que se pida (cambia el esquema de la base de pruebas mientras corre):

    ENSAYO_REVERSION=1 python manage.py test apps.catalogo.tests_reversion_0039 --noinput

Qué comprueba, con datos representativos (órdenes en todos los estados, resultados de
software, despliegues y scripts):
1. respaldo previo de `orden_agente` (dumpdata a un archivo);
2. revertir a 0038 borra SOLO la tabla `orden_agente` (y sus índices y restricciones);
3. los resultados de dominio (ResultadoInstalacion, ResultadoDespliegue,
   ResultadoEjecucionScript) y las estaciones quedan intactos, fila por fila;
4. volver a aplicar 0039 y cargar el respaldo restaura las órdenes tal cual.

NO es el procedimiento de producción: ver docs/consolidacion/GUIA_STAGING_COLA.md §9 (retirada:
docs/RETIRADOS.md).
"""
import os
import tempfile
from io import StringIO
from pathlib import Path
from unittest import skipUnless

from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase
from django.utils import timezone

from apps.catalogo.models import Estacion, Farmacia, Grupo, OrdenAgente, UnidadNegocio

ANTERIOR = ('catalogo', '0038_estacion_mac_lan')
NUEVA = ('catalogo', '0039_orden_agente')
E = OrdenAgente.Estado


def _tablas():
    with connection.cursor() as cursor:
        return set(connection.introspection.table_names(cursor))


def _migrar(destino):
    executor = MigrationExecutor(connection)
    executor.loader.build_graph()
    executor.migrate([destino])


@skipUnless(os.environ.get('ENSAYO_REVERSION'), 'ensayo de reversión: definir ENSAYO_REVERSION=1')
class ReversionOrdenAgenteTests(TransactionTestCase):
    def _sembrar(self):
        from apps.despliegues.models import Despliegue, ResultadoDespliegue
        from apps.scripts.models import EjecucionScript, ResultadoEjecucionScript, Script, TipoScript
        from apps.software.models import (
            AplicacionCatalogo, DestinoTipo, ResultadoInstalacion, SolicitudInstalacion, VersionAplicacion,
        )

        unidad = UnidadNegocio.objects.create(codigo='TREV')
        farmacia = Farmacia.objects.create(codigo='TR001', grupo=Grupo.objects.create(codigo='TRG'), unidad_negocio=unidad)
        usuario = User.objects.create_user(username='reversion', password='x')
        aprobador = User.objects.create_user(username='reversion2', password='x')
        estaciones = [Estacion.objects.create(codigo=f'TR001-{i}', farmacia=farmacia) for i in range(12)]
        version = VersionAplicacion.objects.create(
            aplicacion=AplicacionCatalogo.objects.create(nombre='Rev', creado_por=usuario), version='1',
            instalador=SimpleUploadedFile('r.msi', b'x'), comando_instalacion_silenciosa='x',
        )
        solicitud = SolicitudInstalacion.objects.create(
            version_aplicacion=version, unidad_negocio=unidad, destino_tipo=DestinoTipo.ESTACIONES, creado_por=usuario,
        )
        despliegue = Despliegue.objects.create(
            version='4.3.0', archivo=SimpleUploadedFile('p.zip', b'x'), modo_aplicacion=Despliegue.ModoAplicacion.INMEDIATO,
            destino_tipo=Despliegue.DestinoTipo.ESTACIONES, unidad_negocio=unidad, estado=Despliegue.Estado.PUBLICANDO,
            creado_por=usuario, aprobado_por=aprobador,
        )
        script = Script.objects.create(nombre='Rev', tipo=TipoScript.POWERSHELL, contenido='Get-Date',
                                       creado_por=usuario, unidad_negocio=unidad)
        ejecucion = EjecucionScript.objects.create(
            script=script, contenido_snapshot='Get-Date', destino_tipo='estaciones', unidad_negocio=unidad,
            creado_por=usuario, timeout_segundos=60,
        )
        estados = list(E.values)
        for i, estacion in enumerate(estaciones):
            ResultadoInstalacion.objects.create(solicitud=solicitud, estacion=estacion)
            ResultadoDespliegue.objects.create(despliegue=despliegue, estacion=estacion)
            resultado_script = ResultadoEjecucionScript.objects.create(ejecucion=ejecucion, estacion=estacion)
            estado = estados[i % len(estados)]
            tipo = ('software', 'despliegue', 'script')[i % 3]
            origen = {'software': solicitud.pk, 'despliegue': despliegue.pk, 'script': resultado_script.pk}[tipo]
            OrdenAgente.objects.create(
                estacion=estacion, tipo=tipo, origen_id=origen, clave_idempotencia=f'{tipo}:{origen}:1',
                topico=f'/saidsof/agente/{estacion.codigo}/{tipo}/', payload='{"firma": "x"}', repetible=tipo != 'script',
                estado=estado, plazo_sin_avance_s=3600, entregas=i % 4, ultima_entrega_en=timezone.now(),
                requiere_revision=estado in (E.INTERRUMPIDA, E.NO_ENTREGABLE), historial=[{'estado': estado}],
                creada_por=usuario,
            )

    def _foto_dominio(self):
        from apps.despliegues.models import ResultadoDespliegue
        from apps.scripts.models import ResultadoEjecucionScript
        from apps.software.models import ResultadoInstalacion

        return {
            'instalacion': sorted(ResultadoInstalacion.objects.values_list('pk', 'estacion_id', 'estado')),
            'despliegue': sorted(ResultadoDespliegue.objects.values_list('pk', 'estacion_id', 'estado')),
            'script': sorted(ResultadoEjecucionScript.objects.values_list('pk', 'estacion_id', 'estado')),
            'estaciones': sorted(Estacion.objects.values_list('pk', 'codigo')),
        }

    def test_revertir_a_0038_borra_solo_la_cola_y_el_respaldo_la_restaura(self):
        self._sembrar()
        antes = self._foto_dominio()
        ordenes_antes = sorted(OrdenAgente.objects.values_list('uid', 'estado', 'tipo', 'origen_id', 'payload'))
        self.assertEqual(len(ordenes_antes), 12)
        tablas_antes = _tablas()

        # 1. Respaldo previo.
        respaldo = Path(tempfile.mkdtemp()) / 'orden_agente.json'
        call_command('dumpdata', 'catalogo.OrdenAgente', '--indent', '0', '--output', str(respaldo), verbosity=0)
        self.assertGreater(respaldo.stat().st_size, 0)

        try:
            # 2. Reversión.
            _migrar(ANTERIOR)
            tablas_despues = _tablas()
            self.assertEqual(tablas_antes - tablas_despues, {'orden_agente'}, 'solo se borra la tabla de la cola')
            self.assertEqual(tablas_despues - tablas_antes, set())
            # 3. El dominio quedó intacto, fila por fila.
            self.assertEqual(self._foto_dominio(), antes)
            salida = StringIO()
            call_command('showmigrations', 'catalogo', stdout=salida)
            self.assertIn('[ ] 0039_orden_agente', salida.getvalue())
        finally:
            # 4. Vuelta a 0039 (la base de pruebas tiene que quedar como estaba).
            _migrar(NUEVA)
        self.assertIn('orden_agente', _tablas())
        self.assertEqual(OrdenAgente.objects.count(), 0)
        call_command('loaddata', str(respaldo), verbosity=0)
        self.assertEqual(
            sorted(OrdenAgente.objects.values_list('uid', 'estado', 'tipo', 'origen_id', 'payload')), ordenes_antes,
        )
        self.assertEqual(self._foto_dominio(), antes)
        print(f'\n  [reversión 0039->0038] respaldo: {respaldo} ({respaldo.stat().st_size} bytes); '
              f'tablas borradas: orden_agente; resultados de dominio intactos; restauración: {len(ordenes_antes)} órdenes')
