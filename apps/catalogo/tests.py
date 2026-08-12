import io
import tempfile

from cryptography.fernet import Fernet
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.test import TestCase, override_settings

from apps.catalogo import crypto
from apps.catalogo.models import ClaveRecuperacionBitLocker, Estacion, Farmacia, Grupo, UnidadNegocio
from apps.catalogo.services import (
    generar_comando_instalacion_meshcentral, obtener_clave_bitlocker_descifrada, resolver_estaciones,
    url_escritorio_remoto_meshcentral, url_grabaciones_meshcentral, url_terminal_remoto_meshcentral,
    validar_destino_unidad_negocio,
)

MESHCENTRAL_CONFIG_TEST = {
    'SERVER_URL': 'https://mesh.test.local',
    'MESH_ID': 'abc123meshid',
    'AGENT_ARCH_ID': 4,
    'INSTALL_FLAGS': 2,
    'VIEWMODE_ESCRITORIO': '11',
    'VIEWMODE_TERMINAL': '12',
}

BITLOCKER_KEY_TEST = Fernet.generate_key().decode()


@override_settings(BITLOCKER_ENCRYPTION_KEY=BITLOCKER_KEY_TEST)
class BitLockerCryptoTests(TestCase):
    def test_cifrar_descifrar_es_reversible(self):
        original = '111111-222222-333333-444444-555555-666666-777777-888888'
        token = crypto.cifrar(original)
        self.assertNotEqual(token, original)  # nunca texto plano
        self.assertEqual(crypto.descifrar(token), original)

    def test_descifrar_token_invalido_lanza_value_error(self):
        with self.assertRaises(ValueError):
            crypto.descifrar('esto-no-es-un-token-fernet-valido')

    def test_clave_cifrada_con_otra_llave_no_se_puede_descifrar(self):
        token = crypto.cifrar('secreto')
        with override_settings(BITLOCKER_ENCRYPTION_KEY=Fernet.generate_key().decode()):
            with self.assertRaises(ValueError):
                crypto.descifrar(token)


@override_settings(BITLOCKER_ENCRYPTION_KEY=BITLOCKER_KEY_TEST)
class ObtenerClaveBitlockerTests(TestCase):
    def setUp(self):
        grupo = Grupo.objects.create(codigo='TRX001')
        farmacia = Farmacia.objects.create(
            codigo='ML001', grupo=grupo, unidad_negocio=UnidadNegocio.objects.get(codigo='SG'),
        )
        self.estacion = Estacion.objects.create(codigo='ML001-A', farmacia=farmacia)

    def test_none_si_no_hay_clave_registrada(self):
        self.assertIsNone(obtener_clave_bitlocker_descifrada(self.estacion))

    def test_devuelve_la_clave_en_texto_plano_cuando_existe(self):
        ClaveRecuperacionBitLocker.objects.create(
            estacion=self.estacion, clave_cifrada=crypto.cifrar('111111-222222-333333'),
        )
        self.assertEqual(obtener_clave_bitlocker_descifrada(self.estacion), '111111-222222-333333')


@override_settings(MESHCENTRAL_CONFIG=MESHCENTRAL_CONFIG_TEST)
class MeshCentralServiciosTests(TestCase):
    def setUp(self):
        grupo = Grupo.objects.create(codigo='TRX001')
        farmacia = Farmacia.objects.create(codigo='ML001', grupo=grupo, unidad_negocio=UnidadNegocio.objects.get(codigo='SG'))
        self.estacion = Estacion.objects.create(codigo='ML001-A', farmacia=farmacia)

    def test_generar_comando_instalacion_contiene_mesh_id_y_url(self):
        comando = generar_comando_instalacion_meshcentral(self.estacion)
        self.assertIn('mesh.test.local', comando)
        self.assertIn('meshid=abc123meshid', comando)
        self.assertIn('installflags=2', comando)

    def test_urls_remotas_none_sin_node_id(self):
        self.assertIsNone(url_escritorio_remoto_meshcentral(self.estacion))
        self.assertIsNone(url_terminal_remoto_meshcentral(self.estacion))
        self.assertIsNone(url_grabaciones_meshcentral(self.estacion))

    def test_urls_remotas_con_node_id(self):
        self.estacion.meshcentral_node_id = 'nodeid123'
        self.estacion.save(update_fields=['meshcentral_node_id'])

        url_escritorio = url_escritorio_remoto_meshcentral(self.estacion)
        url_terminal = url_terminal_remoto_meshcentral(self.estacion)
        url_grabaciones = url_grabaciones_meshcentral(self.estacion)

        self.assertIn('gotonode=nodeid123', url_escritorio)
        self.assertIn('viewmode=11', url_escritorio)
        self.assertIn('gotonode=nodeid123', url_terminal)
        self.assertIn('viewmode=12', url_terminal)
        self.assertIn('gotonode=nodeid123', url_grabaciones)


class MultiTenantAislamientoTests(TestCase):
    """R1: un Grupo (canal TRX) puede estar compartido por farmacias de varias
    unidades de negocio (ver docstring de apps.cumplimiento.services) — estos tests
    prueban que eso nunca se traduce en un despliegue/ejecución cruzando de tenant."""

    def setUp(self):
        self.sg = UnidadNegocio.objects.get(codigo='SG')
        self.mia = UnidadNegocio.objects.get(codigo='MIA')
        self.grupo = Grupo.objects.create(codigo='TRX001')
        self.farmacia_sg = Farmacia.objects.create(codigo='ML001', grupo=self.grupo, unidad_negocio=self.sg)
        self.farmacia_mia = Farmacia.objects.create(codigo='MAM01', grupo=self.grupo, unidad_negocio=self.mia)
        self.estacion_sg = Estacion.objects.create(
            codigo='ML001-A', farmacia=self.farmacia_sg, estado_aprobacion=Estacion.EstadoAprobacion.APROBADA,
        )
        self.estacion_mia = Estacion.objects.create(
            codigo='MAM01-A', farmacia=self.farmacia_mia, estado_aprobacion=Estacion.EstadoAprobacion.APROBADA,
        )

    def test_cadena_no_incluye_estaciones_de_otro_tenant(self):
        resultado = resolver_estaciones('cadena', unidad_negocio=self.sg)
        self.assertEqual(list(resultado), [self.estacion_sg])

    def test_grupo_compartido_solo_aporta_estaciones_del_tenant_pedido(self):
        # El Grupo es el mismo para ambas farmacias — la fuga que hay que evitar es
        # que "grupos" devuelva la estación de MIA cuando se pidió para SG.
        resultado = resolver_estaciones('grupos', unidad_negocio=self.sg, grupos=[self.grupo])
        self.assertEqual(list(resultado), [self.estacion_sg])

    def test_validar_destino_rechaza_farmacia_de_otro_tenant(self):
        with self.assertRaises(ValidationError):
            validar_destino_unidad_negocio(self.sg, farmacias=[self.farmacia_mia])

    def test_validar_destino_rechaza_estacion_de_otro_tenant(self):
        with self.assertRaises(ValidationError):
            validar_destino_unidad_negocio(self.sg, estaciones=[self.estacion_mia])

    def test_validar_destino_acepta_targets_del_mismo_tenant(self):
        validar_destino_unidad_negocio(self.sg, farmacias=[self.farmacia_sg], estaciones=[self.estacion_sg])


class MarcarEstacionesOfflineTaskTests(TestCase):
    """CELERY_TASK_ALWAYS_EAGER=True en desarrollo.py hace que .delay() corra sincrónico
    en el mismo proceso — sirve para probar que la tarea está bien registrada y hace lo
    mismo que el comando manual, sin necesitar Redis ni un worker real."""

    def test_delay_marca_offline_igual_que_el_comando(self):
        from datetime import timedelta

        from django.utils import timezone

        from apps.catalogo.tasks import marcar_estaciones_offline_task

        sg = UnidadNegocio.objects.get(codigo='SG')
        grupo = Grupo.objects.create(codigo='TRX001')
        farmacia = Farmacia.objects.create(codigo='ML001', grupo=grupo, unidad_negocio=sg)
        estacion = Estacion.objects.create(
            codigo='ML001-A', farmacia=farmacia, estado_aprobacion=Estacion.EstadoAprobacion.APROBADA,
            estado_conexion=Estacion.EstadoConexion.ONLINE,
            ultimo_heartbeat=timezone.now() - timedelta(minutes=20),
        )

        resultado = marcar_estaciones_offline_task.delay()

        estacion.refresh_from_db()
        self.assertEqual(estacion.estado_conexion, Estacion.EstadoConexion.OFFLINE)
        self.assertIn('1 estación', resultado.get())


class ImportarFarmaciasTests(TestCase):
    def setUp(self):
        self.mia = UnidadNegocio.objects.get(codigo='MIA')
        self.sg = UnidadNegocio.objects.get(codigo='SG')

    def _correr(self, contenido_csv, **opciones):
        salida = io.StringIO()
        with tempfile.NamedTemporaryFile('w', suffix='.csv', delete=False, newline='') as tmp:
            tmp.write(contenido_csv)
            ruta = tmp.name
        call_command('importar_farmacias', ruta, stdout=salida, **opciones)
        return salida.getvalue()

    def test_crea_farmacias_deduciendo_unidad_y_grupo_por_prefijo(self):
        csv_contenido = (
            'Ciudad,Id de sitio,Tipo de Enlace,Backup,NODO\n'
            'Ambato,MAM01,PUNTO NET,NO,trx001\n'
            'Puerto Bolivar,GP005,TELCONET,NO,trx002\n'
            'Loja,7DM02,TELCONET,NO,hub_111_6\n'
        )
        salida = self._correr(csv_contenido)

        # 7DIAS no existe todavía como UnidadNegocio en este test — esa fila queda
        # afuera, así que solo se crean las 2 que sí tienen unidad de negocio válida.
        self.assertIn('2 farmacia(s) creada(s)', salida)
        mam01 = Farmacia.objects.get(codigo='MAM01')
        self.assertEqual(mam01.unidad_negocio, self.mia)
        self.assertEqual(mam01.grupo.codigo, 'TRX001')
        self.assertEqual(mam01.ubicacion, 'Ambato')

        gp005 = Farmacia.objects.get(codigo='GP005')
        self.assertEqual(gp005.unidad_negocio, self.sg)

        # 7DIAS no existe todavía como UnidadNegocio en este test — esa fila debe
        # reportarse como error, no crear un tenant nuevo por accidente.
        self.assertFalse(Farmacia.objects.filter(codigo='7DM02').exists())
        self.assertIn('7DIAS no existe', salida)

    def test_re_correr_sin_actualizar_omite_las_que_ya_existen(self):
        grupo = Grupo.objects.create(codigo='TRX001')
        Farmacia.objects.create(codigo='MAM01', grupo=grupo, unidad_negocio=self.mia, ubicacion='Vieja')
        csv_contenido = 'Ciudad,Id de sitio,NODO\nAmbato,MAM01,trx001\n'

        salida = self._correr(csv_contenido)

        self.assertIn('0 farmacia(s) creada(s)', salida)
        self.assertIn('1 farmacia(s) ya existían, omitida(s): MAM01', salida)
        self.assertEqual(Farmacia.objects.get(codigo='MAM01').ubicacion, 'Vieja')

    def test_actualizar_sobreescribe_ubicacion_y_grupo(self):
        grupo_viejo = Grupo.objects.create(codigo='TRX001')
        Farmacia.objects.create(codigo='MAM01', grupo=grupo_viejo, unidad_negocio=self.mia, ubicacion='Vieja')
        csv_contenido = 'Ciudad,Id de sitio,NODO\nAmbato Centro,MAM01,trx002\n'

        self._correr(csv_contenido, actualizar=True)

        mam01 = Farmacia.objects.get(codigo='MAM01')
        self.assertEqual(mam01.ubicacion, 'Ambato Centro')
        self.assertEqual(mam01.grupo.codigo, 'TRX002')

    def test_dry_run_no_escribe_nada(self):
        csv_contenido = 'Ciudad,Id de sitio,NODO\nAmbato,MAM01,trx001\n'

        salida = self._correr(csv_contenido, dry_run=True)

        self.assertIn('[DRY RUN] 1 farmacia(s) creada(s)', salida)
        self.assertFalse(Farmacia.objects.filter(codigo='MAM01').exists())
        self.assertFalse(Grupo.objects.filter(codigo='TRX001').exists())

    def test_prefijo_desconocido_se_reporta_como_error_sin_adivinar(self):
        csv_contenido = 'Ciudad,Id de sitio,NODO\nQuito,ZQ001,trx001\n'

        salida = self._correr(csv_contenido)

        self.assertFalse(Farmacia.objects.filter(codigo='ZQ001').exists())
        self.assertIn('prefijo de código sin mapeo', salida)

    def test_provincia_se_combina_con_ciudad_en_ubicacion(self):
        csv_contenido = 'Provincia,Ciudad,Id de,NODO\nEl Oro,Pasaje,MP001,trx001\n'

        self._correr(csv_contenido)

        self.assertEqual(Farmacia.objects.get(codigo='MP001').ubicacion, 'Pasaje, El Oro')

    def test_captura_segmento_red_tipo_enlace_y_backup(self):
        csv_contenido = (
            'Ciudad,Id de,Segmento de Red,Tipo de Enlace,Login,Backup,NODO\n'
            'Pasaje,MP001,10.110.1.96/27,TELCONET,farmamia-mp001,ACTIVO,trx001\n'
            'Pindal,MPDL1,10.101.22.192/27,TELCONET,farmamia-mpdl1,,trx001\n'
        )

        self._correr(csv_contenido)

        mp001 = Farmacia.objects.get(codigo='MP001')
        self.assertEqual(mp001.segmento_red, '10.110.1.96/27')
        self.assertEqual(mp001.tipo_enlace, 'TELCONET')
        self.assertTrue(mp001.tiene_backup)

        # Backup vacío -> sin enlace de respaldo.
        self.assertFalse(Farmacia.objects.get(codigo='MPDL1').tiene_backup)
