"""API de integración RMM (`/api/v1/rmm/`): lo que un inventario externo puede leer de SaidSoft.

Fija el contrato que el otro proyecto va a consumir: qué campos salen (y que los secretos
de la estación no), que es de solo lectura, que respeta permisos y unidades de negocio
como el panel, y que los filtros con valores inválidos se rechazan en lugar de devolver
una lista vacía.
"""
from django.contrib.auth.models import Permission, User
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from rest_framework.authtoken.models import Token

from apps.catalogo.models import Estacion, Farmacia, Grupo, PerifericoDetectado, UnidadNegocio
from apps.cuentas.models import PerfilUsuario
from apps.monitoreo.models import Alerta, Metrica, ReglaAlerta
from apps.software.models import SoftwareInstaladoDetectado

CAMPOS_ESTACION = {
    'codigo', 'farmacia', 'unidad_negocio', 'hostname', 'numero_serie',
    'so_nombre', 'so_build', 'version_agente', 'version_pos',
    'procesador', 'ram_total_mb', 'almacenamiento_total_gb', 'info_equipo_fecha',
    'bitlocker_habilitado', 'ip_lan', 'mac_lan',
    'estado_conexion', 'ultimo_heartbeat', 'estado_aprobacion', 'fecha_creacion',
}
# Lo que la estación guarda y nunca debe salir por esta API.
CAMPOS_PROHIBIDOS = {
    'id', 'token_enrolamiento', 'hmac_secret', 'hardware_id', 'meshcentral_node_id',
    'pos_servidor', 'pos_bdd', 'pos_puerto', 'clave_bitlocker', 'hmac_propio_confirmado',
}


def _permiso(codigo):
    app, codename = codigo.split('.')
    return Permission.objects.get(content_type__app_label=app, codename=codename)


class ApiRmmBase(TestCase):
    def setUp(self):
        self.sg = UnidadNegocio.objects.get(codigo='SG')
        self.mia = UnidadNegocio.objects.get(codigo='MIA')
        grupo = Grupo.objects.create(codigo='TRX001')
        self.ml001 = Farmacia.objects.create(codigo='ML001', grupo=grupo, unidad_negocio=self.sg, ciudad='Quito')
        self.ajena = Farmacia.objects.create(codigo='MAM01', grupo=grupo, unidad_negocio=self.mia)
        self.estacion = Estacion.objects.create(
            codigo='ML001-CAJA1', farmacia=self.ml001, hostname='CAJA1-ML001', numero_serie='PF3ABC12',
            procesador='Intel Core i5-10400', ram_total_mb=8192, almacenamiento_total_gb=256,
            pos_servidor='10.0.0.5', pos_bdd='trx001',
        )
        self.otra = Estacion.objects.create(codigo='ML001-ADM', farmacia=self.ml001, numero_serie='XYZ999')
        self.estacion_ajena = Estacion.objects.create(codigo='MAM01-CAJA1', farmacia=self.ajena, numero_serie='AJENA1')

        # El usuario del sistema de inventario: solo SG y solo lectura.
        self.integracion = User.objects.create_user(username='inventario-sg', password='x')
        PerfilUsuario.objects.create(usuario=self.integracion, acceso_todas_unidades=False).unidades_negocio.set([self.sg])
        self.integracion.user_permissions.add(
            _permiso('catalogo.view_estacion'), _permiso('catalogo.view_farmacia'), _permiso('monitoreo.view_alerta'),
        )
        self.token = Token.objects.create(user=self.integracion)

    def _get(self, nombre, token=None, args=None, **parametros):
        cabeceras = {'HTTP_AUTHORIZATION': f'Token {token or self.token.key}'} if token != '' else {}
        return self.client.get(reverse(nombre, args=args), parametros, **cabeceras)


class EstacionesTests(ApiRmmBase):
    def test_lista_solo_las_estaciones_de_sus_unidades_con_el_contrato_de_campos(self):
        respuesta = self._get('api-rmm-estaciones')
        self.assertEqual(respuesta.status_code, 200)
        datos = respuesta.json()
        self.assertEqual([e['codigo'] for e in datos['results']], ['ML001-ADM', 'ML001-CAJA1'])
        self.assertEqual(datos['count'], 2)
        for estacion in datos['results']:
            self.assertEqual(set(estacion), CAMPOS_ESTACION)

        caja = datos['results'][1]
        self.assertEqual(caja['numero_serie'], 'PF3ABC12')
        self.assertEqual(caja['procesador'], 'Intel Core i5-10400')
        self.assertEqual(caja['ram_total_mb'], 8192)
        self.assertEqual(caja['farmacia'], 'ML001')
        self.assertEqual(caja['unidad_negocio'], 'SG')

    def test_no_expone_secretos_ni_la_conexion_al_pos(self):
        lista = self._get('api-rmm-estaciones').json()['results']
        detalle = self._get('api-rmm-estacion', args=['ML001-CAJA1']).json()
        for objeto in [*lista, detalle]:
            self.assertFalse(CAMPOS_PROHIBIDOS & set(objeto), CAMPOS_PROHIBIDOS & set(objeto))
        texto = self._get('api-rmm-estacion', args=['ML001-CAJA1']).content.decode()
        self.assertNotIn(self.estacion.token_enrolamiento, texto)
        self.assertNotIn('10.0.0.5', texto)

    def test_filtra_por_numero_de_serie_sin_distinguir_mayusculas(self):
        datos = self._get('api-rmm-estaciones', numero_serie='pf3abc12').json()
        self.assertEqual([e['codigo'] for e in datos['results']], ['ML001-CAJA1'])

    def test_un_serial_de_otro_cliente_no_aparece(self):
        datos = self._get('api-rmm-estaciones', numero_serie='AJENA1').json()
        self.assertEqual(datos['count'], 0)

    def test_filtra_por_farmacia_hostname_y_estado(self):
        self.assertEqual(self._get('api-rmm-estaciones', farmacia='ml001').json()['count'], 2)
        self.assertEqual(self._get('api-rmm-estaciones', hostname='caja1-ml001').json()['count'], 1)
        self.assertEqual(self._get('api-rmm-estaciones', estado_conexion='nunca_conectada').json()['count'], 2)
        self.assertEqual(self._get('api-rmm-estaciones', estado_conexion='online').json()['count'], 0)

    def test_un_filtro_con_valor_inexistente_es_400_y_no_una_lista_vacia(self):
        respuesta = self._get('api-rmm-estaciones', estado_conexion='conectada')
        self.assertEqual(respuesta.status_code, 400)
        self.assertIn('estado_conexion', respuesta.json())

    def test_el_detalle_incluye_perifericos_y_software(self):
        PerifericoDetectado.objects.create(
            estacion=self.estacion, nombre='Impresora EPSON TM-T20', fabricante='EPSON', clase='Printer', device_id='USB\\1',
        )
        SoftwareInstaladoDetectado.objects.create(estacion=self.estacion, nombre='Google Chrome', version='118.0')

        respuesta = self._get('api-rmm-estacion', args=['ML001-CAJA1'])
        self.assertEqual(respuesta.status_code, 200)
        datos = respuesta.json()
        self.assertEqual(datos['perifericos'], [{'nombre': 'Impresora EPSON TM-T20', 'fabricante': 'EPSON', 'clase': 'Printer'}])
        self.assertEqual(datos['software'], [{'nombre': 'Google Chrome', 'version': '118.0', 'fabricante': ''}])
        self.assertEqual(
            set(datos), CAMPOS_ESTACION | {
                'perifericos_ultima_verificacion', 'perifericos', 'software_instalado_ultima_verificacion', 'software',
            },
        )

    def test_el_detalle_de_una_estacion_de_otro_cliente_es_404(self):
        """404 y no 403: no se confirma que la estación exista."""
        self.assertEqual(self._get('api-rmm-estacion', args=['MAM01-CAJA1']).status_code, 404)
        self.assertEqual(self._get('api-rmm-estacion', args=['NO-EXISTE']).status_code, 404)

    def test_la_cantidad_de_consultas_no_crece_con_las_estaciones(self):
        def consultas():
            with CaptureQueriesContext(connection) as capturadas:
                self.assertEqual(self._get('api-rmm-estaciones').status_code, 200)
            return len(capturadas)

        antes = consultas()
        for i in range(5):
            Estacion.objects.create(codigo=f'ML001-EXTRA{i}', farmacia=self.ml001)
        self.assertEqual(consultas(), antes)

    def test_pagina(self):
        datos = self._get('api-rmm-estaciones', por_pagina=1).json()
        self.assertEqual(datos['count'], 2)
        self.assertEqual(len(datos['results']), 1)
        self.assertIsNotNone(datos['next'])


class AccesoTests(ApiRmmBase):
    RUTAS = [('api-rmm-estaciones', None), ('api-rmm-estacion', ['ML001-CAJA1']),
             ('api-rmm-farmacias', None), ('api-rmm-alertas', None)]

    def test_sin_token_es_401(self):
        for nombre, args in self.RUTAS:
            with self.subTest(ruta=nombre):
                self.assertEqual(self._get(nombre, token='', args=args).status_code, 401)

    def test_autenticado_sin_permiso_es_403(self):
        pelado = User.objects.create_user(username='pelado', password='x')
        PerfilUsuario.objects.create(usuario=pelado, acceso_todas_unidades=True)
        token = Token.objects.create(user=pelado).key
        for nombre, args in self.RUTAS:
            with self.subTest(ruta=nombre):
                self.assertEqual(self._get(nombre, token=token, args=args).status_code, 403)

    def test_es_de_solo_lectura(self):
        cabeceras = {'HTTP_AUTHORIZATION': f'Token {self.token.key}'}
        for nombre, args in self.RUTAS:
            url = reverse(nombre, args=args)
            for metodo in ('post', 'put', 'patch', 'delete'):
                with self.subTest(ruta=nombre, metodo=metodo):
                    respuesta = getattr(self.client, metodo)(url, {}, content_type='application/json', **cabeceras)
                    self.assertIn(respuesta.status_code, (403, 405))
        self.assertTrue(Estacion.objects.filter(codigo='ML001-CAJA1').exists())

    def test_acceso_total_ve_todas_las_unidades(self):
        self.integracion.perfil.acceso_todas_unidades = True
        self.integracion.perfil.save()
        self.assertEqual(self._get('api-rmm-estaciones').json()['count'], 3)


class FarmaciasTests(ApiRmmBase):
    def test_lista_las_farmacias_visibles(self):
        datos = self._get('api-rmm-farmacias').json()
        self.assertEqual([f['codigo'] for f in datos['results']], ['ML001'])
        self.assertEqual(datos['results'][0]['ciudad'], 'Quito')
        self.assertEqual(datos['results'][0]['unidad_negocio'], 'SG')

    def test_filtro_activa(self):
        self.assertEqual(self._get('api-rmm-farmacias', activa='false').json()['count'], 0)
        self.assertEqual(self._get('api-rmm-farmacias', activa='true').json()['count'], 1)
        self.assertEqual(self._get('api-rmm-farmacias', activa='quizas').status_code, 400)


class AlertasTests(ApiRmmBase):
    def setUp(self):
        super().setUp()
        creador = User.objects.create_user(username='creador', password='x')
        regla = ReglaAlerta.objects.create(
            nombre='CPU alta', metrica=Metrica.CPU_CARGA_PCT, umbral=90, creado_por=creador,
            severidad=ReglaAlerta.Severidad.CRITICAL, abre_mantenimiento=True,
        )
        self.primera = Alerta.objects.create(regla=regla, estacion=self.estacion, valor_disparador=95)
        self.segunda = Alerta.objects.create(
            regla=regla, estacion=self.otra, valor_disparador=97, estado=Alerta.Estado.RESUELTA,
        )
        self.ajena = Alerta.objects.create(regla=regla, estacion=self.estacion_ajena, valor_disparador=99)

    def test_lista_en_orden_de_id_solo_las_visibles(self):
        datos = self._get('api-rmm-alertas').json()
        self.assertEqual([a['id'] for a in datos['results']], [self.primera.pk, self.segunda.pk])
        alerta = datos['results'][0]
        self.assertEqual(alerta['estacion'], 'ML001-CAJA1')
        self.assertEqual(alerta['farmacia'], 'ML001')
        self.assertEqual(alerta['regla'], 'CPU alta')
        self.assertEqual(alerta['severidad'], 'critical')
        self.assertTrue(alerta['abre_mantenimiento'])
        self.assertNotIn('diagnostico_ia', alerta)

    def test_desde_id_devuelve_solo_las_nuevas(self):
        datos = self._get('api-rmm-alertas', desde_id=self.primera.pk).json()
        self.assertEqual([a['id'] for a in datos['results']], [self.segunda.pk])

    def test_filtra_por_estado_y_estacion(self):
        self.assertEqual(
            [a['id'] for a in self._get('api-rmm-alertas', estado='abierta').json()['results']], [self.primera.pk],
        )
        self.assertEqual(
            [a['id'] for a in self._get('api-rmm-alertas', estacion='ml001-adm').json()['results']], [self.segunda.pk],
        )

    def test_parametros_invalidos_son_400(self):
        self.assertEqual(self._get('api-rmm-alertas', desde_id='abc').status_code, 400)
        self.assertEqual(self._get('api-rmm-alertas', estado='cerrada').status_code, 400)
