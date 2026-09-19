import datetime
import io
import json
import tempfile
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from cryptography.fernet import Fernet
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings

from apps.activos.models import Cargo, Colaborador, Departamento
from apps.catalogo import crypto
from apps.catalogo.models import ClaveRecuperacionBitLocker, Estacion, Farmacia, Grupo, UnidadNegocio, VersionAgente
from apps.catalogo.services import (
    calcular_matriz_cumplimiento, enviar_actualizacion_agente, enviar_comando, enviar_script, firmar_payload,
    generar_comando_instalacion_meshcentral, obtener_clave_bitlocker_descifrada, resolver_estaciones, secreto_de,
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

    def test_generar_comando_usa_curl_no_invoke_webrequest(self):
        # Invoke-WebRequest de PowerShell 5.1 corta la descarga con "error inesperado de
        # envío" contra el TLS autofirmado de MeshCentral, incluso forzando Tls12 y
        # saltando la validación del certificado — probado de verdad instalando en
        # ML006-A y MC001-B (ver PLAN_MODERNIZACION.md). curl.exe (Schannel, no el stack
        # de .NET Framework) sí funciona.
        comando = generar_comando_instalacion_meshcentral(self.estacion)
        self.assertIn('curl.exe', comando)
        self.assertNotIn('Invoke-WebRequest', comando)

    def test_generar_comando_usa_fullinstall(self):
        # Corrido sin argumentos, meshagent.exe nunca completa la instalación real
        # cuando lo lanza un servicio de Windows sin sesión interactiva (Session 0) —
        # se queda corriendo suelto sin registrar el servicio "Mesh Agent". Con
        # "-fullinstall" sí instala de verdad y el proceso termina solo (reproducido
        # y diagnosticado en MC001-C), por eso "-Wait" es correcto acá.
        comando = generar_comando_instalacion_meshcentral(self.estacion)
        self.assertIn('-fullinstall', comando)
        self.assertIn('-Wait', comando)

    def test_generar_comando_nombra_el_agente_como_la_estacion(self):
        # Para que apps.monitoreo.adapters.meshcentral._vincular_por_nombre pueda
        # enlazar el node_id solo, sin copiarlo a mano de la consola de MeshCentral.
        comando = generar_comando_instalacion_meshcentral(self.estacion)
        self.assertIn(f'--agentName={self.estacion.codigo}', comando)

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


# `CELERY_TASK_ALWAYS_EAGER` solo está puesto en config/settings/desarrollo.py, y no
# se lee del entorno. Sin este override la prueba pasa en local y falla SIEMPRE dentro
# del contenedor — que es justo donde CLAUDE.md pide correr la suite para probar contra
# PostgreSQL. Una prueba no debe depender de qué módulo de settings esté cargado.
@override_settings(CELERY_TASK_ALWAYS_EAGER=True)
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

    def test_nodo_mas_largo_que_el_campo_se_reporta_como_error_sin_reventar(self):
        # Grupo.codigo tiene max_length=10 — un NODO más largo tiraba un DataError sin
        # manejar (500 crudo en el admin, encontrado en producción 12-ago-2026).
        csv_contenido = 'Ciudad,Id de,NODO\nAmbato,MAM01,un_nodo_con_nombre_demasiado_largo\n'

        salida = self._correr(csv_contenido)

        self.assertFalse(Farmacia.objects.filter(codigo='MAM01').exists())
        self.assertFalse(Grupo.objects.filter(codigo__startswith='UN_NODO').exists())
        self.assertIn('caracteres (máximo', salida)

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

    def test_captura_ip_por_farmacia_no_por_nodo(self):
        # La IP es por farmacia, no por nodo/grupo -- un mismo NODO puede agrupar
        # farmacias con IPs distintas (rollout de versión de POS, no topología de red).
        csv_contenido = (
            'Ciudad,Id de,NODO,IP\n'
            'Pasaje,MP001,trx001,192.168.112.5\n'
            'Pinas,MI001,trx001,192.168.112.60\n'
        )
        self._correr(csv_contenido)
        self.assertEqual(Farmacia.objects.get(codigo='MP001').ip_router, '192.168.112.5')
        self.assertEqual(Farmacia.objects.get(codigo='MI001').ip_router, '192.168.112.60')

    def test_ip_invalida_se_reporta_como_error_sin_bloquear_la_fila(self):
        csv_contenido = 'Ciudad,Id de,NODO,IP\nPasaje,MP001,trx001,no-es-una-ip\n'
        salida = self._correr(csv_contenido)
        self.assertIn('IP "no-es-una-ip" inválida', salida)
        mp001 = Farmacia.objects.get(codigo='MP001')
        self.assertIsNone(mp001.ip_router)


class FarmaciaAdminImportarViewTests(TestCase):
    """El botón "Importar CSV" del admin (/admin/catalogo/farmacia/importar/) usa el
    mismo apps.catalogo.services.importar_farmacias_desde_csv que el comando de
    management — surgió porque el usuario buscaba un botón de importar en el admin
    y no había ninguno, solo el comando por SSH."""

    def setUp(self):
        from django.contrib.auth.models import User
        self.mia = UnidadNegocio.objects.get(codigo='MIA')
        self.staff = User.objects.create_user(username='staff_import', password='x', is_staff=True)
        self.staff.user_permissions.add(*self._permisos_farmacia())
        self.sin_permiso = User.objects.create_user(username='sin_permiso_import', password='x', is_staff=True)

    @staticmethod
    def _permisos_farmacia():
        from django.contrib.auth.models import Permission
        return Permission.objects.filter(content_type__app_label='catalogo', content_type__model='farmacia')

    def _url(self):
        from django.urls import reverse
        return reverse('admin:catalogo_farmacia_importar')

    def _archivo(self, contenido):
        return io.BytesIO(contenido.encode('utf-8'))

    def test_get_muestra_el_formulario(self):
        self.client.force_login(self.staff)
        resp = self.client.get(self._url())
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'Importar farmacias desde CSV')

    def test_sin_permiso_de_alta_da_403(self):
        self.client.force_login(self.sin_permiso)
        resp = self.client.get(self._url())
        self.assertEqual(resp.status_code, 403)

    def test_post_con_dry_run_no_escribe_y_muestra_previsualizacion(self):
        self.client.force_login(self.staff)
        archivo = self._archivo('Ciudad,Id de,NODO\nAmbato,MAM01,trx001\n')

        resp = self.client.post(self._url(), {'archivo': archivo, 'dry_run': 'on'})

        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'Previsualización')
        self.assertFalse(Farmacia.objects.filter(codigo='MAM01').exists())

    def test_post_sin_dry_run_crea_y_redirige_al_listado(self):
        from django.urls import reverse
        self.client.force_login(self.staff)
        archivo = self._archivo('Ciudad,Id de,NODO\nAmbato,MAM01,trx001\n')

        resp = self.client.post(self._url(), {'archivo': archivo})

        self.assertRedirects(resp, reverse('admin:catalogo_farmacia_changelist'))
        self.assertTrue(Farmacia.objects.filter(codigo='MAM01').exists())

    def test_post_sin_archivo_muestra_error(self):
        self.client.force_login(self.staff)

        resp = self.client.post(self._url(), {'dry_run': 'on'})

        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'Elegí un archivo CSV')


class EstacionAdminMonitoreoEnLoteTests(TestCase):
    """list_editable alcanza fila por fila, pero no escala a ~1.800 estaciones — estas
    dos acciones del admin activan/desactivan monitorear_recursos sobre la selección
    completa (filtrable por grupo/farmacia con list_filter)."""

    def setUp(self):
        from django.contrib.auth.models import User
        self.admin_user = User.objects.create_superuser(username='admin_mon', email='a@a.com', password='x')
        grupo = Grupo.objects.create(codigo='TRX001')
        farmacia = Farmacia.objects.create(codigo='ML001', grupo=grupo, unidad_negocio=UnidadNegocio.objects.get(codigo='SG'))
        self.e1 = Estacion.objects.create(codigo='ML001-A', farmacia=farmacia)
        self.e2 = Estacion.objects.create(codigo='ML001-B', farmacia=farmacia, monitorear_recursos=True)
        self.client.force_login(self.admin_user)

    def _url(self):
        from django.urls import reverse
        return reverse('admin:catalogo_estacion_changelist')

    def test_activar_en_lote_solo_toca_las_que_no_lo_tenian(self):
        resp = self.client.post(self._url(), {
            'action': 'activar_monitoreo_recursos', '_selected_action': [self.e1.pk, self.e2.pk],
        }, follow=True)
        self.e1.refresh_from_db()
        self.e2.refresh_from_db()
        self.assertTrue(self.e1.monitorear_recursos)
        self.assertTrue(self.e2.monitorear_recursos)
        self.assertContains(resp, 'activado en 1 estación')  # e2 ya estaba activa, se excluye

    def test_desactivar_en_lote(self):
        resp = self.client.post(self._url(), {
            'action': 'desactivar_monitoreo_recursos', '_selected_action': [self.e1.pk, self.e2.pk],
        }, follow=True)
        self.e2.refresh_from_db()
        self.assertFalse(self.e2.monitorear_recursos)
        self.assertContains(resp, 'desactivado en 1 estación')

    def test_activar_en_lote_audita_cada_estacion(self):
        from apps.auditoria.models import EventoAuditoria
        self.client.post(self._url(), {
            'action': 'activar_monitoreo_recursos', '_selected_action': [self.e1.pk],
        })
        self.assertTrue(
            EventoAuditoria.objects.filter(accion='estacion.monitoreo_activar', usuario=self.admin_user).exists(),
        )


class ImportarRedFarmaciasXlsxTests(TestCase):
    """Wrapper sobre importar_farmacias_desde_csv que lee directo del Excel real de
    red (dos hojas, FARMAMIA y SAN GREGORIO, con columnas distintas entre sí)."""

    def setUp(self):
        self.mia = UnidadNegocio.objects.get(codigo='MIA')
        self.sg = UnidadNegocio.objects.get(codigo='SG')

    def _libro_de_prueba(self):
        import openpyxl
        wb = openpyxl.Workbook()
        wb.remove(wb.active)

        farmamia = wb.create_sheet('FARMAMIA')
        farmamia.append(['mcu', 'Provincia', 'Ciudad', 'Id de Farmacia', 'Segmento de Red', 'Tipo de Enlace', 'Login', 'Backup', 'IP-DNS', 'Correo', 'NODO', 'IP'])
        farmamia.append([1, 'El Oro', 'Arenillas', 'MA001', '10.101.18.224/27', 'TELCONET', 'login1', 'ACTIVO', None, None, 'trx001', '192.168.112.5'])

        sg = wb.create_sheet('SAN GREGORIO')
        sg.append(['Item', 'Provincia', 'Canton', 'Direccion', 'Id de Farmacia', 'Login', 'Backup', 'Proveedor', 'RED LAN', 'NODO', 'IP', 'CLAVE'])
        sg.append([2, 'MANABI', 'SANTA ANA', 'Direccion X', 'GSA01', 'login2', None, 'TELCONET', '192.168.102.1', 'trx003', '192.168.112.60', None])

        ruta = tempfile.NamedTemporaryFile(suffix='.xlsx', delete=False).name
        wb.save(ruta)
        return ruta

    def _correr(self, **opciones):
        salida = io.StringIO()
        call_command('importar_red_farmacias_xlsx', self._libro_de_prueba(), stdout=salida, **opciones)
        return salida.getvalue()

    def test_crea_farmacias_de_ambas_hojas_con_su_propia_ip(self):
        salida = self._correr()
        self.assertIn('2 farmacia(s) creada(s)', salida)

        ma001 = Farmacia.objects.get(codigo='MA001')
        self.assertEqual(ma001.unidad_negocio, self.mia)
        self.assertEqual(ma001.grupo.codigo, 'TRX001')
        self.assertEqual(ma001.ip_router, '192.168.112.5')
        self.assertEqual(ma001.segmento_red, '10.101.18.224/27')

        gsa01 = Farmacia.objects.get(codigo='GSA01')
        self.assertEqual(gsa01.unidad_negocio, self.sg)
        self.assertEqual(gsa01.grupo.codigo, 'TRX003')
        self.assertEqual(gsa01.ip_router, '192.168.112.60')
        self.assertEqual(gsa01.tipo_enlace, 'TELCONET')

    def test_dry_run_no_escribe_nada(self):
        salida = self._correr(dry_run=True)
        self.assertIn('[DRY RUN] 2 farmacia(s) creada(s)', salida)
        self.assertFalse(Farmacia.objects.filter(codigo='MA001').exists())

    def test_nodo_sin_asignar_se_remapea_a_pendiente(self):
        # "ELIPSYS_CRESIO" es el valor real que trae el NODO de las sucursales sin
        # canal de versión de POS asignado todavía -- no es un grupo real y además
        # excede Grupo.codigo (max_length=10), así que se remapea a un placeholder.
        import openpyxl
        wb = openpyxl.Workbook()
        wb.remove(wb.active)
        sg = wb.create_sheet('SAN GREGORIO')
        sg.append(['Item', 'Provincia', 'Canton', 'Direccion', 'Id de Farmacia', 'Login', 'Backup', 'Proveedor', 'RED LAN', 'NODO', 'IP', 'CLAVE'])
        sg.append([1, 'MANABI', 'SANTA ANA', 'Direccion X', 'GSA02', 'login', None, 'TELCONET', '192.168.102.1', 'elipsys_cresio', '192.168.112.61', None])
        ruta = tempfile.NamedTemporaryFile(suffix='.xlsx', delete=False).name
        wb.save(ruta)

        salida = io.StringIO()
        call_command('importar_red_farmacias_xlsx', ruta, stdout=salida)

        self.assertIn('1 farmacia(s) creada(s)', salida.getvalue())
        self.assertEqual(Farmacia.objects.get(codigo='GSA02').grupo.codigo, 'PENDIENTE')

    def test_mprev1_queda_excluido(self):
        # MPREV1 (PREVITAL) no es una farmacia real -- confirmado con el usuario
        # (22-ago-2026), misma entidad que PREV1 en el directorio de RRHH.
        import openpyxl
        wb = openpyxl.Workbook()
        wb.remove(wb.active)
        farmamia = wb.create_sheet('FARMAMIA')
        farmamia.append(['mcu', 'Provincia', 'Ciudad', 'Id de Farmacia', 'Segmento de Red', 'Tipo de Enlace', 'Login', 'Backup', 'IP-DNS', 'Correo', 'NODO', 'IP'])
        farmamia.append([1, 'El Oro', 'Machala', 'MPREV1', '', '', '', '', None, None, 'trx001', '192.168.112.9'])
        ruta = tempfile.NamedTemporaryFile(suffix='.xlsx', delete=False).name
        wb.save(ruta)

        salida = io.StringIO()
        call_command('importar_red_farmacias_xlsx', ruta, stdout=salida)

        self.assertIn('0 farmacia(s) creada(s)', salida.getvalue())
        self.assertFalse(Farmacia.objects.filter(codigo='MPREV1').exists())


class ImportarDirectorioSucursalesTests(TestCase):
    """Enriquecimiento de Farmacia ya existentes con el directorio de sucursales de
    RRHH (nombre, horario, coordinadores, coordenadas, técnico asignado, etc.)."""

    def setUp(self):
        self.sg = UnidadNegocio.objects.get(codigo='SG')
        grupo = Grupo.objects.create(codigo='TRX001')
        self.farmacia = Farmacia.objects.create(codigo='GES07', grupo=grupo, unidad_negocio=self.sg)

        departamento = Departamento.objects.create(nombre='Tecnologías e Innovación', tipo=Departamento.Tipo.TECNICO)
        cargo = Cargo.objects.create(nombre='Asistente de Soporte Técnico', departamento=departamento)
        self.tecnico = Colaborador.objects.create(
            nombre='Carranza Cedeño Jaime Leonerys', cedula='1312655291', cargo=cargo,
        )

    def _libro_de_prueba(self, tecnico='JAIME CARRANZA', tipo_sucursal='PROPIA', formato='MOSTRADOR'):
        import openpyxl
        wb = openpyxl.Workbook()
        wb.active.title = 'Directorio Personal'
        hoja = wb.active
        hoja.append([
            'Nombre Sucursal', 'Marca', 'Ciudad', 'Sucursal', 'Horario', 'Administrador', 'Coordinador_Zonal',
            'Ext_Ip', 'Celular', 'Correo_Electonico', 'Provincia', 'Coordinador_Regional', 'Direccion',
            'Tipo_Sucursal', 'Latitud', 'Longitud', 'formato_farmacia', 'Parroquia', 'fecha_inicio_op',
            'fecha_inicio_ruc', 'Tecnico',
        ])
        hoja.append([
            'FARMACIAS SAN GREGORIO GES07', 'SAN GREGORIO', 'ESMERALDAS', 'GES07',
            'LUNES A VIERNES: 08:00 - 19:00', 'SASINTUÑA BONE MARTHA', 'MARQUEZ MOSQUERA MARIO',
            None, '0990051735', 'ges07.avlibertad@sangregorio.com.ec', 'ESMERALDAS',
            'ALARCON MACIAS GEOVANNY', 'AVENIDA LIBERTAD / SN', tipo_sucursal, '0.9704555', '-79.6530856',
            formato, 'ESMERALDAS', datetime.datetime(2022, 2, 7), datetime.datetime(2022, 2, 7), tecnico,
        ])
        ruta = tempfile.NamedTemporaryFile(suffix='.xlsx', delete=False).name
        wb.save(ruta)
        return ruta

    def _correr(self, **kwargs):
        salida = io.StringIO()
        call_command(
            'importar_directorio_sucursales', self._libro_de_prueba(**kwargs), '--aplicar', stdout=salida,
        )
        return salida.getvalue()

    def test_enriquece_la_farmacia_existente_con_todos_los_campos(self):
        self._correr()
        self.farmacia.refresh_from_db()
        self.assertEqual(self.farmacia.nombre, 'FARMACIAS SAN GREGORIO GES07')
        self.assertEqual(self.farmacia.administrador, 'SASINTUÑA BONE MARTHA')
        self.assertEqual(self.farmacia.coordinador_zonal, 'MARQUEZ MOSQUERA MARIO')
        self.assertEqual(self.farmacia.coordinador_regional, 'ALARCON MACIAS GEOVANNY')
        self.assertEqual(self.farmacia.ciudad, 'ESMERALDAS')
        self.assertEqual(self.farmacia.provincia, 'ESMERALDAS')
        self.assertEqual(self.farmacia.direccion, 'AVENIDA LIBERTAD / SN')
        self.assertEqual(self.farmacia.tipo_sucursal, Farmacia.TipoSucursal.PROPIA)
        self.assertEqual(self.farmacia.formato_farmacia, Farmacia.FormatoFarmacia.MOSTRADOR)
        self.assertAlmostEqual(self.farmacia.latitud, 0.9704555)
        self.assertAlmostEqual(self.farmacia.longitud, -79.6530856)
        self.assertEqual(self.farmacia.telefono, '0990051735')
        self.assertEqual(self.farmacia.email, 'ges07.avlibertad@sangregorio.com.ec')
        self.assertEqual(self.farmacia.fecha_inicio_operacion, datetime.date(2022, 2, 7))
        self.assertEqual(self.farmacia.tecnico_asignado, self.tecnico)

    def test_formato_mostrador_xp_se_normaliza_con_guion_bajo(self):
        self._correr(formato='MOSTRADOR XP')
        self.farmacia.refresh_from_db()
        self.assertEqual(self.farmacia.formato_farmacia, Farmacia.FormatoFarmacia.MOSTRADOR_XP)

    def test_tecnico_nd_no_vincula_a_nadie(self):
        self._correr(tecnico='N/D')
        self.farmacia.refresh_from_db()
        self.assertIsNone(self.farmacia.tecnico_asignado)

    def test_tecnico_desconocido_se_reporta_como_advertencia(self):
        salida = self._correr(tecnico='ALGUIEN NUEVO')
        self.assertIn('técnico sin mapeo', salida)
        self.farmacia.refresh_from_db()
        self.assertIsNone(self.farmacia.tecnico_asignado)

    def test_codigo_sin_farmacia_todavia_se_reporta_como_error(self):
        self.farmacia.delete()
        salida = self._correr()
        self.assertIn('sin Farmacia todavía en SAIDSOFT', salida)

    def test_dry_run_no_escribe_nada(self):
        self._correr(tipo_sucursal='ASOCIADO')  # deja el estado real limpio primero
        self.farmacia.refresh_from_db()
        self.assertEqual(self.farmacia.tipo_sucursal, Farmacia.TipoSucursal.ASOCIADO)

        salida = io.StringIO()
        call_command(
            'importar_directorio_sucursales', self._libro_de_prueba(tipo_sucursal='PROPIA'),
            dry_run=True, stdout=salida,
        )
        self.assertIn('[SIMULACRO] 1 farmacia', salida.getvalue())
        self.farmacia.refresh_from_db()
        self.assertEqual(self.farmacia.tipo_sucursal, Farmacia.TipoSucursal.ASOCIADO)  # sin cambios


class ComandoFirmadoTests(TestCase):
    """SEC-1 (auditoría 22-ago-2026): la firma HMAC de un comando sin parámetros
    (`enviar_comando`) era un string constante ("reiniciar", "consultar_info", ...) —
    la misma firma servía para siempre y para cualquier estación. Ahora `estacion` y
    `timestamp` entran a la firma, y el agente valida ambos (ver agente-prueba/agente_prueba.py)."""

    def setUp(self):
        grupo = Grupo.objects.create(codigo='TRX001')
        farmacia = Farmacia.objects.create(codigo='ML001', grupo=grupo, unidad_negocio=UnidadNegocio.objects.get(codigo='SG'))
        self.estacion = Estacion.objects.create(codigo='ML001-A', farmacia=farmacia)
        self.otra_estacion = Estacion.objects.create(codigo='ML001-B', farmacia=farmacia)

    def _publicar(self, estacion, comando):
        with patch('apps.catalogo.services.mqtt_publish.single') as mock_single:
            enviar_comando(estacion, comando)
        return mock_single.call_args.args[1]  # (topico, payload_json, ...)

    def test_el_payload_lleva_estacion_timestamp_y_firma_valida(self):
        payload = json.loads(self._publicar(self.estacion, 'reiniciar'))
        self.assertEqual(payload['estacion'], 'ML001-A')
        self.assertIn('timestamp', payload)
        firma_esperada = firmar_payload(comando='reiniciar', estacion='ML001-A', timestamp=payload['timestamp'])
        self.assertEqual(payload['firma'], firma_esperada)

    def test_la_firma_no_es_constante_entre_invocaciones(self):
        # Antes del fix, firmar_payload(comando='reiniciar') no dependía de nada más:
        # la firma de dos invocaciones cualquiera era exactamente la misma.
        payload_1 = json.loads(self._publicar(self.estacion, 'reiniciar'))
        payload_2 = json.loads(self._publicar(self.estacion, 'reiniciar'))
        # Incluso repitiendo la misma estación, si el timestamp cambia la firma cambia.
        if payload_1['timestamp'] != payload_2['timestamp']:
            self.assertNotEqual(payload_1['firma'], payload_2['firma'])

    def test_la_firma_de_una_estacion_no_sirve_para_otra(self):
        payload = json.loads(self._publicar(self.estacion, 'reiniciar'))
        # Reconstruir la firma que el agente de OTRA estación calcularía (su propio
        # código en vez del que venía en el mensaje) no matchea la que llegó.
        firma_para_otra = firmar_payload(
            comando='reiniciar', estacion=self.otra_estacion.codigo, timestamp=payload['timestamp'],
        )
        self.assertNotEqual(payload['firma'], firma_para_otra)


class ScriptFirmadoTests(TestCase):
    def setUp(self):
        grupo = Grupo.objects.create(codigo='TRX001')
        farmacia = Farmacia.objects.create(codigo='ML001', grupo=grupo, unidad_negocio=UnidadNegocio.objects.get(codigo='SG'))
        self.estacion = Estacion.objects.create(codigo='ML001-A', farmacia=farmacia)

    def test_el_payload_lleva_estacion_timestamp_y_firma_valida(self):
        with patch('apps.catalogo.services.mqtt_publish.single') as mock_single:
            enviar_script(
                self.estacion, ejecucion_id=1, resultado_id=2, tipo_script='powershell',
                contenido='Write-Host hola', timeout_segundos=60,
            )
        payload = json.loads(mock_single.call_args.args[1])
        self.assertEqual(payload['estacion'], 'ML001-A')
        self.assertIn('timestamp', payload)
        firma_esperada = firmar_payload(
            comando='ejecutar_script', ejecucion_id=1, resultado_id=2, tipo_script='powershell',
            timeout_segundos=60, contenido='Write-Host hola',
            estacion='ML001-A', timestamp=payload['timestamp'],
        )
        self.assertEqual(payload['firma'], firma_esperada)


class VersionAgenteTests(TestCase):
    """Actualización remota del agente desde el panel: VersionAgente es el equivalente
    de VersionAplicacion (software) pero para el propio binario del agente."""

    def setUp(self):
        self.usuario = User.objects.create_user(username='u', password='x')

    def test_calcula_sha256_y_tamanio_al_guardar(self):
        version = VersionAgente.objects.create(
            version='agente-prueba-0.2', ejecutable=SimpleUploadedFile('agente.exe', b'contenido-falso'),
            creado_por=self.usuario,
        )
        self.assertTrue(version.sha256)
        self.assertEqual(version.tamanio_bytes, len(b'contenido-falso'))

    def test_no_recalcula_el_hash_si_ya_existe(self):
        version = VersionAgente.objects.create(
            version='agente-prueba-0.2', ejecutable=SimpleUploadedFile('agente.exe', b'contenido-falso'),
            creado_por=self.usuario,
        )
        hash_original = version.sha256
        version.notas = 'build de prueba'
        version.save()
        self.assertEqual(version.sha256, hash_original)


class EnviarActualizacionAgenteTests(TestCase):
    def setUp(self):
        grupo = Grupo.objects.create(codigo='TRX001')
        farmacia = Farmacia.objects.create(codigo='ML001', grupo=grupo, unidad_negocio=UnidadNegocio.objects.get(codigo='SG'))
        self.estacion = Estacion.objects.create(codigo='ML001-A', farmacia=farmacia)
        usuario = User.objects.create_user(username='u', password='x')
        self.version = VersionAgente.objects.create(
            version='agente-prueba-0.2', ejecutable=SimpleUploadedFile('agente.exe', b'contenido-falso'),
            creado_por=usuario,
        )

    def test_el_payload_lleva_estacion_timestamp_y_firma_valida(self):
        with patch('apps.catalogo.services.mqtt_publish.single') as mock_single:
            enviar_actualizacion_agente(self.estacion, self.version)
        payload = json.loads(mock_single.call_args.args[1])
        self.assertEqual(payload['comando'], 'actualizar_agente')
        self.assertEqual(payload['estacion'], 'ML001-A')
        self.assertEqual(payload['version'], 'agente-prueba-0.2')
        self.assertEqual(payload['sha256'], self.version.sha256)
        firma_esperada = firmar_payload(
            comando='actualizar_agente', version=payload['version'], url=payload['url'], sha256=payload['sha256'],
            estacion='ML001-A', timestamp=payload['timestamp'],
        )
        self.assertEqual(payload['firma'], firma_esperada)

    def test_se_publica_retenido_en_topico_propio_no_en_comando(self):
        # No usa /comando/ (fire-and-forget a propósito, ver enviar_comando): esto
        # debe quedar retenido en EMQX para que una estación apagada lo reciba apenas
        # se conecte, no solo si ya estaba en línea en el momento del publish.
        with patch('apps.catalogo.services.mqtt_publish.single') as mock_single:
            enviar_actualizacion_agente(self.estacion, self.version)
        args, kwargs = mock_single.call_args
        self.assertEqual(args[0], f'/saidsof/agente/{self.estacion.codigo}/actualizar_agente/')
        self.assertTrue(kwargs['retain'])

    def test_limpiar_actualizacion_pendiente_publica_vacio_y_retenido(self):
        from apps.catalogo.services import limpiar_actualizacion_pendiente
        with patch('apps.catalogo.services.mqtt_publish.single') as mock_single:
            limpiar_actualizacion_pendiente(self.estacion)
        args, kwargs = mock_single.call_args
        self.assertEqual(args[0], f'/saidsof/agente/{self.estacion.codigo}/actualizar_agente/')
        self.assertEqual(args[1], '')
        self.assertTrue(kwargs['retain'])


class CalcularMatrizCumplimientoTests(TestCase):
    def setUp(self):
        self.sg = UnidadNegocio.objects.get(codigo='SG')

    def test_excluye_el_grupo_pendiente(self):
        # PENDIENTE es el bucket que usa importar_farmacias cuando el nodo/grupo de
        # una fila no se resolvió todavía -- no es una unidad operativa real, no debe
        # aparecer en la matriz de cumplimiento aunque tenga farmacias (encontrado en
        # producción: 208 farmacias quedaron ahí por un NODO más largo que
        # Grupo.codigo, ver docstring de importar_farmacias).
        pendiente = Grupo.objects.create(codigo='PENDIENTE')
        Farmacia.objects.create(codigo='GX001', grupo=pendiente, unidad_negocio=self.sg)
        codigos = [g.codigo for g in calcular_matriz_cumplimiento([self.sg])]
        self.assertNotIn('PENDIENTE', codigos)

    def test_incluye_grupos_reales_con_farmacias_en_alcance(self):
        grupo = Grupo.objects.create(codigo='TRX099')
        Farmacia.objects.create(codigo='GX002', grupo=grupo, unidad_negocio=self.sg)
        codigos = [g.codigo for g in calcular_matriz_cumplimiento([self.sg])]
        self.assertIn('TRX099', codigos)


class EnviarConsultarRedFarmaciaTests(TestCase):
    def setUp(self):
        grupo = Grupo.objects.create(codigo='TRX001')
        farmacia = Farmacia.objects.create(
            codigo='ML001', grupo=grupo, unidad_negocio=UnidadNegocio.objects.get(codigo='SG'), ip_router='10.0.1.1',
        )
        self.estacion = Estacion.objects.create(codigo='ML001-A', farmacia=farmacia)

    def test_el_payload_lleva_comunidad_estacion_timestamp_y_firma_valida(self):
        from apps.catalogo.services import enviar_consultar_red_farmacia
        with patch('apps.catalogo.services.mqtt_publish.single') as mock_single:
            enviar_consultar_red_farmacia(self.estacion, 'ml001')
        args, kwargs = mock_single.call_args
        # No usa /comando/ como los demás (retain=False ahí no importa), pero SÍ
        # comparte ese mismo tópico -- a diferencia de actualizar_agente, este no
        # necesita quedar retenido: si la estación está apagada, simplemente no hay
        # nada que sondear en ese momento.
        self.assertEqual(args[0], f'/saidsof/agente/{self.estacion.codigo}/comando/')
        payload = json.loads(args[1])
        self.assertEqual(payload['comando'], 'consultar_red_farmacia')
        self.assertEqual(payload['comunidad'], 'ml001')
        self.assertEqual(payload['estacion'], 'ML001-A')
        firma_esperada = firmar_payload(
            comando='consultar_red_farmacia', comunidad='ml001', estacion='ML001-A', timestamp=payload['timestamp'],
        )
        self.assertEqual(payload['firma'], firma_esperada)


class NodoDiscrepanteTests(TestCase):
    """El POS reporta a qué nodo apunta de verdad (Estacion.pos_bdd, leído del
    Zabyca.Pos.Desktop.exe.Config). Solo se informa la discrepancia contra el grupo
    asignado -- nunca se corrige sola, ver docstring de la propiedad."""

    def setUp(self):
        grupo = Grupo.objects.create(codigo='TRX004')
        farmacia = Farmacia.objects.create(
            codigo='ML001', grupo=grupo, unidad_negocio=UnidadNegocio.objects.get(codigo='SG'),
        )
        self.estacion = Estacion.objects.create(codigo='ML001-A', farmacia=farmacia)

    def test_sin_pos_bdd_no_hay_discrepancia(self):
        # Todavía no reportó nada: no hay contra qué comparar.
        self.assertFalse(self.estacion.nodo_discrepante)

    def test_coincide_con_el_grupo_no_es_discrepancia(self):
        self.estacion.pos_bdd = 'trx004'  # el .Config lo trae en minúscula
        self.assertFalse(self.estacion.nodo_discrepante)

    def test_nodo_distinto_al_grupo_es_discrepancia(self):
        self.estacion.pos_bdd = 'trx002'
        self.assertTrue(self.estacion.nodo_discrepante)


class EnviarConfigurarNodoPosTests(TestCase):
    def setUp(self):
        self.grupo = Grupo.objects.create(codigo='TRX004', pos_servidor='192.168.112.3', pos_puerto='5433')
        farmacia = Farmacia.objects.create(
            codigo='ML001', grupo=self.grupo, unidad_negocio=UnidadNegocio.objects.get(codigo='SG'),
        )
        self.estacion = Estacion.objects.create(codigo='ML001-A', farmacia=farmacia)

    def test_el_payload_lleva_los_cuatro_valores_y_firma_valida(self):
        from apps.catalogo.services import enviar_configurar_nodo_pos
        with patch('apps.catalogo.services.mqtt_publish.single') as mock_single:
            enviar_configurar_nodo_pos(
                self.estacion, servidor='192.168.200.9', bdd='trx009', puerto='5555', password='s3cr3t',
            )
        args, kwargs = mock_single.call_args
        payload = json.loads(args[1])
        self.assertEqual(payload['comando'], 'configurar_nodo_pos')
        self.assertEqual(payload['servidor'], '192.168.200.9')
        self.assertEqual(payload['bdd'], 'trx009')
        self.assertEqual(payload['puerto'], '5555')
        self.assertEqual(payload['password'], 's3cr3t')
        # La contraseña entra a la firma: si no, alguien que pueda publicar en el
        # tópico podría cambiarla sin invalidar el HMAC.
        firma_esperada = firmar_payload(
            comando='configurar_nodo_pos', servidor='192.168.200.9', bdd='trx009', puerto='5555',
            password='s3cr3t', estacion='ML001-A', timestamp=payload['timestamp'],
        )
        self.assertEqual(payload['firma'], firma_esperada)

    def test_no_se_publica_retenido(self):
        # Reapuntar el POS de una caja apagada, en un momento indeterminado, es
        # justamente lo que no se quiere: se reenvía a mano cuando vuelva.
        from apps.catalogo.services import enviar_configurar_nodo_pos
        with patch('apps.catalogo.services.mqtt_publish.single') as mock_single:
            enviar_configurar_nodo_pos(self.estacion, servidor='1.2.3.4', bdd='x', puerto='1', password='p')
        self.assertFalse(mock_single.call_args.kwargs['retain'])


@override_settings(BITLOCKER_ENCRYPTION_KEY=BITLOCKER_KEY_TEST)
class PasswordNodoTests(TestCase):
    def setUp(self):
        self.grupo = Grupo.objects.create(codigo='TRX004')

    def test_se_guarda_cifrada_nunca_en_texto_plano(self):
        from apps.catalogo.services import establecer_password_nodo, obtener_password_nodo
        establecer_password_nodo(self.grupo, 'clave-del-nodo')
        self.grupo.refresh_from_db()
        self.assertNotIn('clave-del-nodo', self.grupo.pos_password_cifrada)
        self.assertEqual(obtener_password_nodo(self.grupo), 'clave-del-nodo')

    def test_sin_password_devuelve_vacio(self):
        from apps.catalogo.services import obtener_password_nodo
        self.assertEqual(obtener_password_nodo(self.grupo), '')

    def test_vacia_borra_la_existente(self):
        from apps.catalogo.services import establecer_password_nodo, obtener_password_nodo
        establecer_password_nodo(self.grupo, 'algo')
        establecer_password_nodo(self.grupo, '')
        self.assertEqual(obtener_password_nodo(self.grupo), '')

    def test_clave_rotada_no_lanza_devuelve_vacio(self):
        # Un nodo con la contraseña cifrada con otra clave no debe tumbar el flujo:
        # el llamador ya trata '' como "no configurado" y avisa.
        from apps.catalogo.services import establecer_password_nodo, obtener_password_nodo
        establecer_password_nodo(self.grupo, 'algo')
        with override_settings(BITLOCKER_ENCRYPTION_KEY=Fernet.generate_key().decode()):
            self.assertEqual(obtener_password_nodo(self.grupo), '')


class BddPosTests(TestCase):
    """`codigo` solo admite MAYÚSCULAS, pero el .Config del POS trae la base en
    minúscula y es sensible a mayúsculas: escribir "TRX004" deja al POS sin conectar."""

    def test_por_defecto_es_el_codigo_en_minuscula(self):
        self.assertEqual(Grupo(codigo='TRX004').bdd_pos, 'trx004')

    def test_pos_bdd_explicito_manda(self):
        # Para cuando el nombre real de la base no es simplemente el código en minúscula.
        self.assertEqual(Grupo(codigo='TRX004', pos_bdd='TrxCuatro_Prod').bdd_pos, 'TrxCuatro_Prod')

    def test_discrepancia_compara_contra_el_bdd_efectivo(self):
        grupo = Grupo.objects.create(codigo='TRX005')
        farmacia = Farmacia.objects.create(
            codigo='ML027', grupo=grupo, unidad_negocio=UnidadNegocio.objects.get(codigo='SG'),
        )
        estacion = Estacion.objects.create(codigo='ML027-ADM', farmacia=farmacia)
        estacion.pos_bdd = 'trx005'   # el POS ya apunta al nodo nuevo
        self.assertFalse(estacion.nodo_discrepante)
        estacion.pos_bdd = 'trx004'   # todavía en el viejo
        self.assertTrue(estacion.nodo_discrepante)


class ImportarCircuitosProveedorTests(TestCase):
    """El circuito del proveedor es lo que piden al abrir un ticket; hasta ahora vivía
    en un Excel aparte. El comando acepta los dos formatos de planilla que existen."""

    def setUp(self):
        self.sg = UnidadNegocio.objects.get(codigo='SG')
        grupo = Grupo.objects.create(codigo='TRX001')
        self.gsa01 = Farmacia.objects.create(codigo='GSA01', grupo=grupo, unidad_negocio=self.sg)
        self.ma001 = Farmacia.objects.create(codigo='MA001', grupo=grupo, unidad_negocio=self.sg)

    def _csv(self, contenido):
        ruta = Path(tempfile.gettempdir()) / f'circuitos_{id(self)}.csv'
        # utf-8-sig: las planillas salen de Excel con BOM, y el comando tiene que
        # soportarlo o la primera columna nunca calza.
        ruta.write_text(contenido, encoding='utf-8-sig')
        self.addCleanup(lambda: ruta.unlink(missing_ok=True))
        return str(ruta)

    FORMATO_SG = (
        'provincia;canton;cod_sucursal;caracteristica;proveedor;ip_proveedor\n'
        'Manabí;Santa Ana;GSA01;sangregorio2-santana;TELCONET;192.168.102.1\n'
    )
    FORMATO_MIA = (
        'Provincia;Ciudad;Codigo de farmacia;Ip Provedor;Provedor;Caracteristica\n'
        'El Oro;Arenillas;MA001;10.101.18.224/27;TELCONET;cazul-arenillas\n'
    )

    def test_formato_san_gregorio(self):
        call_command('importar_circuitos_proveedor', self._csv(self.FORMATO_SG), '--aplicar')
        self.gsa01.refresh_from_db()
        self.assertEqual(self.gsa01.circuito_proveedor, 'sangregorio2-santana')

    def test_formato_mia(self):
        call_command('importar_circuitos_proveedor', self._csv(self.FORMATO_MIA), '--aplicar')
        self.ma001.refresh_from_db()
        self.assertEqual(self.ma001.circuito_proveedor, 'cazul-arenillas')

    def test_sin_aplicar_no_escribe_nada(self):
        """Las planillas traen sucursales cerradas: conviene ver qué haría antes."""
        call_command('importar_circuitos_proveedor', self._csv(self.FORMATO_SG))
        self.gsa01.refresh_from_db()
        self.assertEqual(self.gsa01.circuito_proveedor, '')

    def test_no_toca_segmento_red_ni_ip_router(self):
        """Esos ya están cargados; una reimportación no puede pisarlos con dato viejo."""
        self.gsa01.segmento_red = '10.0.0.0/24'
        self.gsa01.ip_router = '10.0.0.1'
        self.gsa01.save(update_fields=['segmento_red', 'ip_router'])

        call_command('importar_circuitos_proveedor', self._csv(self.FORMATO_SG), '--aplicar')
        self.gsa01.refresh_from_db()
        self.assertEqual(self.gsa01.segmento_red, '10.0.0.0/24')
        self.assertEqual(self.gsa01.ip_router, '10.0.0.1')

    def test_una_sucursal_que_no_existe_se_informa_y_no_se_crea(self):
        """Inventarla desde una planilla de enlaces seria adivinar su grupo y su
        unidad de negocio."""
        csv_txt = self.FORMATO_SG + 'Manabí;X;GZZ99;circuito-fantasma;TELCONET;1.2.3.4\n'
        salida = StringIO()
        call_command('importar_circuitos_proveedor', self._csv(csv_txt), '--aplicar', stdout=salida)
        self.assertFalse(Farmacia.objects.filter(codigo='GZZ99').exists())
        self.assertIn('GZZ99', salida.getvalue())

    def test_es_idempotente(self):
        ruta = self._csv(self.FORMATO_SG)
        call_command('importar_circuitos_proveedor', ruta, '--aplicar')
        salida = StringIO()
        call_command('importar_circuitos_proveedor', ruta, '--aplicar', stdout=salida)
        self.assertIn('Ya tenían el mismo circuito: 1', salida.getvalue())

    def test_columnas_desconocidas_fallan_con_un_mensaje_util(self):
        with self.assertRaises(CommandError) as ctx:
            call_command('importar_circuitos_proveedor', self._csv('a;b\n1;2\n'), '--aplicar')
        self.assertIn('No reconozco las columnas', str(ctx.exception))


class CerrarConexionesViejasTests(TestCase):
    """`close_old_connections()` dentro de un bloque atómico cierra la conexión y deja
    inservible todo lo que siga en ese proceso.

    Contra SQLite no se nota; contra el PostgreSQL real —el mismo motor que producción—
    tumbaba 125 pruebas de una sola vez, porque un TestCase envuelve cada prueba en una
    transacción y los handlers de los workers llaman a esto.
    """

    def test_dentro_de_una_transaccion_no_cierra_nada(self):
        from django.db import connection
        from apps.catalogo.db import cerrar_conexiones_viejas

        # Un TestCase ya corre dentro de un bloque atómico.
        self.assertTrue(connection.in_atomic_block)
        with patch('apps.catalogo.db.close_old_connections') as cerrar:
            cerrar_conexiones_viejas()
        cerrar.assert_not_called()

        # Y la conexión sigue viva: sin esto, la consulta de abajo reventaría.
        self.assertGreaterEqual(UnidadNegocio.objects.count(), 0)

    def test_fuera_de_una_transaccion_si_cierra(self):
        """El resguardo no puede desactivar el comportamiento que los workers necesitan:
        una conexión que el servidor cerró por timeout tiene que descartarse."""
        from apps.catalogo.db import cerrar_conexiones_viejas

        with patch('apps.catalogo.db.connection') as conexion, \
                patch('apps.catalogo.db.close_old_connections') as cerrar:
            conexion.in_atomic_block = False
            cerrar_conexiones_viejas()
        cerrar.assert_called_once()

    def test_un_handler_del_worker_no_rompe_la_transaccion_de_la_prueba(self):
        """Prueba de extremo: llamar a un handler real y seguir consultando después."""
        from apps.mqtt_worker.services import manejar_enrolamiento

        manejar_enrolamiento({'codigo': 'ZZZ99-A', 'hardware_id': 'x'})
        # Si el handler hubiera cerrado la conexión, esto lanzaría OperationalError.
        self.assertGreaterEqual(UnidadNegocio.objects.count(), 0)


class RelojDeEstacionTests(TestCase):
    """Desfase de reloj y zona horaria de una estación.

    Que una estación tenga la hora corrida no es cosmético: el agente descarta todo
    mensaje firmado fuera de su ventana de timestamp, así que pasado ese desfase la
    estación deja de recibir comandos, scripts y despliegues — incluido el que le
    arreglaría el reloj. Le pasó a MAM06-A el 26-ago-2026.
    """

    def setUp(self):
        self.sg = UnidadNegocio.objects.get(codigo='SG')
        grupo = Grupo.objects.create(codigo='TRX001')
        self.farmacia = Farmacia.objects.create(codigo='ML001', grupo=grupo, unidad_negocio=self.sg)
        self.estacion = Estacion.objects.create(
            codigo='ML001-A', farmacia=self.farmacia,
            estado_aprobacion=Estacion.EstadoAprobacion.APROBADA,
        )

    def _con(self, **campos):
        for campo, valor in campos.items():
            setattr(self.estacion, campo, valor)
        return self.estacion

    def test_el_umbral_grave_es_el_mismo_que_la_ventana_del_agente(self):
        """Los dos números son el mismo visto desde los dos lados: el agente decide qué
        descarta y el panel decide qué muestra en rojo. Si se separan, el panel mentiría
        — diría "en hora" sobre una estación que ya no recibe nada.

        Se lee del archivo del agente y no se importa porque `agente-prueba` no es un
        paquete (el guion del nombre lo impide).
        """
        import re
        from pathlib import Path

        from django.conf import settings

        fuente = (Path(settings.BASE_DIR) / 'agente-prueba' / 'agente_prueba.py').read_text(encoding='utf-8')
        encontrado = re.search(r'^VENTANA_TIMESTAMP_SEGUNDOS\s*=\s*(\d+)', fuente, re.MULTILINE)
        self.assertIsNotNone(encontrado, 'no se encontró VENTANA_TIMESTAMP_SEGUNDOS en el agente')
        self.assertEqual(
            int(encontrado.group(1)), Estacion.UMBRAL_RELOJ_INCOMUNICADO_SEGUNDOS,
            'la ventana del agente y el umbral del panel se separaron: el panel diría '
            '"en hora" sobre una estación que ya no recibe comandos.',
        )

    def test_un_reloj_en_cero_esta_en_hora_no_desconocido(self):
        """0 es el valor perfecto, no "sin dato". Tratarlo como falsy haría que la
        estación mejor sincronizada apareciera igual que una que nunca reportó."""
        estacion = self._con(desfase_reloj_segundos=0)
        self.assertFalse(estacion.reloj_desincronizado)
        self.assertFalse(estacion.reloj_incomunicado)

    def test_sin_dato_no_se_inventa_un_diagnostico(self):
        estacion = self._con(desfase_reloj_segundos=None, offset_utc_minutos=None)
        self.assertFalse(estacion.reloj_desincronizado)
        self.assertFalse(estacion.reloj_incomunicado)
        self.assertFalse(estacion.zona_horaria_incorrecta)

    def test_el_desfase_cuenta_para_los_dos_lados(self):
        """Una estación atrasada queda igual de incomunicada que una adelantada: la
        ventana del agente es un valor absoluto."""
        self.assertTrue(self._con(desfase_reloj_segundos=200).reloj_incomunicado)
        self.assertTrue(self._con(desfase_reloj_segundos=-200).reloj_incomunicado)

    def test_un_desfase_intermedio_avisa_pero_todavia_obedece(self):
        estacion = self._con(desfase_reloj_segundos=45)
        self.assertTrue(estacion.reloj_desincronizado)
        self.assertFalse(estacion.reloj_incomunicado)

    def test_la_zona_horaria_es_un_problema_aparte_del_desfase(self):
        """El reloj UTC puede estar perfecto y la hora local mostrarse mal porque la
        región quedó en otro país. Sincronizar no arregla eso."""
        estacion = self._con(desfase_reloj_segundos=0, offset_utc_minutos=-180)
        self.assertFalse(estacion.reloj_desincronizado)
        self.assertTrue(estacion.zona_horaria_incorrecta)

    def test_ecuador_es_utc_menos_cinco(self):
        self.assertEqual(Estacion.OFFSET_UTC_ESPERADO_MINUTOS, -300)
        self.assertFalse(self._con(offset_utc_minutos=-300).zona_horaria_incorrecta)


class HeartbeatConRelojTests(TestCase):
    """Ingesta del reloj en `manejar_heartbeat`.

    El desfase lo calcula el SERVIDOR y no el agente: el agente no tiene contra qué
    compararse, porque si su reloj está mal su idea de "ahora" también lo está.
    """

    def setUp(self):
        self.sg = UnidadNegocio.objects.get(codigo='SG')
        grupo = Grupo.objects.create(codigo='TRX001')
        self.farmacia = Farmacia.objects.create(codigo='ML001', grupo=grupo, unidad_negocio=self.sg)
        self.estacion = Estacion.objects.create(
            codigo='ML001-A', farmacia=self.farmacia,
            estado_aprobacion=Estacion.EstadoAprobacion.APROBADA,
        )

    def _latido(self, **extra):
        from django.utils import timezone

        from apps.mqtt_worker.services import manejar_heartbeat

        payload = {'token': self.estacion.token_enrolamiento, **extra}
        manejar_heartbeat(self.estacion.codigo, payload)
        self.estacion.refresh_from_db()
        return timezone.now()

    def test_guarda_el_desfase_la_zona_y_el_offset(self):
        import time

        self._latido(
            reloj_epoch=time.time() + 300, offset_utc_minutos=-300,
            zona_horaria='SA Pacific Standard Time',
        )
        # ~300 s, con holgura para lo que tarde la propia prueba.
        self.assertAlmostEqual(self.estacion.desfase_reloj_segundos, 300, delta=5)
        self.assertEqual(self.estacion.offset_utc_minutos, -300)
        self.assertEqual(self.estacion.zona_horaria, 'SA Pacific Standard Time')
        self.assertTrue(self.estacion.reloj_incomunicado)

    def test_una_estacion_en_hora_queda_en_cero(self):
        import time

        self._latido(reloj_epoch=time.time(), offset_utc_minutos=-300)
        self.assertAlmostEqual(self.estacion.desfase_reloj_segundos, 0, delta=5)
        self.assertFalse(self.estacion.reloj_desincronizado)

    def test_un_agente_viejo_no_borra_lo_que_ya_se_sabia(self):
        """Durante el rollout de 0.18 van a convivir agentes que reportan el reloj y
        agentes que no. El que no lo reporta tiene que dejar el dato como estaba, no
        pisarlo con vacío — mismo criterio que la config del POS."""
        self._latido(reloj_epoch=1, offset_utc_minutos=-300, zona_horaria='SA Pacific Standard Time')
        desfase_previo = self.estacion.desfase_reloj_segundos

        self._latido()  # latido de un agente 0.17

        self.assertEqual(self.estacion.desfase_reloj_segundos, desfase_previo)
        self.assertEqual(self.estacion.zona_horaria, 'SA Pacific Standard Time')

    def test_un_valor_ilegible_no_rompe_el_latido(self):
        """Un dato de reloj mal formado no puede costar el heartbeat entero: la estación
        quedaría marcada offline por un campo secundario."""
        self._latido(reloj_epoch='no-es-un-numero', offset_utc_minutos='tampoco')
        self.assertEqual(self.estacion.estado_conexion, Estacion.EstadoConexion.ONLINE)
        self.assertIsNone(self.estacion.desfase_reloj_segundos)


class ZonaConElMismoOffsetTests(TestCase):
    """Una región mal asignada que comparte huso con la correcta.

    Encontrado en producción en ML016-B (14-sep-2026): estaba en "Eastern Standard Time
    (Mexico)", que también es UTC-5. La comprobación original solo miraba el offset, así
    que la daba por buena — y era exactamente el caso que motivó todo este trabajo.
    """

    def setUp(self):
        self.sg = UnidadNegocio.objects.get(codigo='SG')
        grupo = Grupo.objects.create(codigo='TRX016')
        self.farmacia = Farmacia.objects.create(codigo='ML016', grupo=grupo, unidad_negocio=self.sg)

    def _estacion(self, codigo, **extra):
        return Estacion.objects.create(
            codigo=codigo, farmacia=self.farmacia,
            estado_aprobacion=Estacion.EstadoAprobacion.APROBADA, **extra,
        )

    def test_detecta_mexico_aunque_el_offset_coincida(self):
        estacion = self._estacion(
            'ML016-B', offset_utc_minutos=-300, zona_horaria='Eastern Standard Time (Mexico)',
        )
        self.assertTrue(estacion.zona_horaria_incorrecta)

    def test_ecuador_bien_configurada_no_se_marca(self):
        estacion = self._estacion(
            'ML016-A', offset_utc_minutos=-300, zona_horaria='SA Pacific Standard Time',
        )
        self.assertFalse(estacion.zona_horaria_incorrecta)

    def test_un_offset_distinto_se_marca_aunque_no_haya_nombre(self):
        """Un agente que reportó el offset pero no pudo leer `tzutil` igual se evalúa."""
        estacion = self._estacion('ML016-C', offset_utc_minutos=-180, zona_horaria='')
        self.assertTrue(estacion.zona_horaria_incorrecta)

    def test_sin_ningun_dato_no_se_acusa_a_nadie(self):
        estacion = self._estacion('ML016-D')
        self.assertFalse(estacion.zona_horaria_incorrecta)

    def test_la_zona_esperada_es_la_que_corrige_el_script(self):
        """El script de biblioteca corrige a este mismo identificador. Si los dos lados se
        separaran, el panel marcaría en rojo estaciones que el script ya "arregló"."""
        from django.core.management import call_command

        from apps.scripts.models import Script

        User.objects.create_superuser(username='u_zona_mx', password='x' * 14)
        call_command('seed_scripts_hora')
        contenido = Script.objects.get(nombre__startswith='Sincronizar hora').contenido
        self.assertIn(Estacion.ZONA_HORARIA_ESPERADA, contenido)


class VersionPosReportadaTests(TestCase):
    """La versión del POS que reporta el agente y la comparación contra el objetivo.

    Hasta el agente 0.19 el latido mandaba la cadena fija `'N/A (agente de prueba)'`.
    Eso dejaba sin sentido una cadena que ya existía completa —`Estacion.version_pos`,
    `Grupo.version_objetivo`, `Estacion.desactualizada` y el filtro "Solo desactualizadas"
    del panel— comparando un texto que nunca cambiaba. La maquinaria estaba entera y no
    medía nada: ML027-ADM figuraba desactualizada solo porque esa cadena no es igual a
    "3.0.2.28" (verificado en producción el 14-sep-2026).
    """

    VERSION_EN_PRODUCCION = '3.0.2.28'

    def setUp(self):
        self.sg = UnidadNegocio.objects.get(codigo='SG')
        self.grupo = Grupo.objects.create(codigo='TRX004', version_objetivo=self.VERSION_EN_PRODUCCION)
        self.farmacia = Farmacia.objects.create(codigo='GMI04', grupo=self.grupo, unidad_negocio=self.sg)
        self.estacion = Estacion.objects.create(
            codigo='GMI04-A', farmacia=self.farmacia,
            estado_aprobacion=Estacion.EstadoAprobacion.APROBADA,
        )

    def _latido(self, **extra):
        from apps.mqtt_worker.services import manejar_heartbeat

        manejar_heartbeat(self.estacion.codigo, {'token': self.estacion.token_enrolamiento, **extra})
        self.estacion.refresh_from_db()

    def test_el_agente_ya_no_manda_la_cadena_fija(self):
        """Se lee del archivo del agente porque `agente-prueba` no es un paquete
        importable (el guion del nombre lo impide)."""
        from pathlib import Path

        from django.conf import settings

        fuente = (Path(settings.BASE_DIR) / 'agente-prueba' / 'agente_prueba.py').read_text(encoding='utf-8')
        self.assertNotIn("'version_pos': 'N/A", fuente)
        self.assertIn('_version_pos_reportable', fuente)

    def test_una_estacion_en_la_version_objetivo_no_esta_desactualizada(self):
        self._latido(version_pos=self.VERSION_EN_PRODUCCION)
        self.assertEqual(self.estacion.version_pos, self.VERSION_EN_PRODUCCION)
        self.assertFalse(self.estacion.desactualizada)

    def test_una_version_anterior_si_lo_esta(self):
        self._latido(version_pos='3.0.2.27')
        self.assertTrue(self.estacion.desactualizada)

    def test_un_latido_sin_la_version_no_borra_la_que_ya_se_sabia(self):
        """El agente omite la clave cuando no pudo leer el ejecutable (POS no instalado,
        ruta distinta). Mandar '' borraría el dato; omitirla lo conserva — mismo criterio
        que la config del POS."""
        self._latido(version_pos=self.VERSION_EN_PRODUCCION)
        self._latido()
        self.assertEqual(self.estacion.version_pos, self.VERSION_EN_PRODUCCION)

    def test_sin_version_objetivo_no_se_acusa_a_nadie(self):
        """6 de los 7 grupos con farmacias tenían `version_objetivo` vacía (14-sep-2026).
        Un grupo sin objetivo definido no puede tener estaciones "desactualizadas": no hay
        contra qué comparar."""
        self.grupo.version_objetivo = ''
        self.grupo.save(update_fields=['version_objetivo'])
        self._latido(version_pos='3.0.0.1')
        self.assertFalse(self.estacion.desactualizada)


class FijarVersionObjetivoPosTests(TestCase):
    """Carga masiva de `Grupo.version_objetivo`.

    Un objetivo equivocado es peor que ninguno: deja cientos de farmacias marcadas como
    desactualizadas para siempre, y un indicador que siempre está en rojo se deja de
    mirar. De ahí que simule por defecto, valide el formato y no pise lo ya cargado.
    """

    def setUp(self):
        self.sg = UnidadNegocio.objects.get(codigo='SG')
        self.con_farmacias = Grupo.objects.create(codigo='TRX003')
        self.ya_cargado = Grupo.objects.create(codigo='TRX004', version_objetivo='3.0.2.27')
        self.sin_farmacias = Grupo.objects.create(codigo='VACIO')
        for i, grupo in enumerate((self.con_farmacias, self.ya_cargado)):
            Farmacia.objects.create(codigo='ML10%d' % i, grupo=grupo, unidad_negocio=self.sg)

    def _correr(self, *args):
        salida = io.StringIO()
        call_command('fijar_version_objetivo_pos', *args, stdout=salida)
        return salida.getvalue()

    def test_por_defecto_simula(self):
        texto = self._correr('--objetivo', '3.0.2.28')
        self.assertIn('Se cambiarían', texto)
        self.con_farmacias.refresh_from_db()
        self.assertEqual(self.con_farmacias.version_objetivo, '')

    def test_completa_los_vacios(self):
        self._correr('--objetivo', '3.0.2.28', '--aplicar')
        self.con_farmacias.refresh_from_db()
        self.assertEqual(self.con_farmacias.version_objetivo, '3.0.2.28')

    def test_no_toca_un_grupo_sin_farmacias(self):
        """Un grupo sin farmacias no tiene estaciones que comparar; ponerle un objetivo
        sería ensuciar el catálogo sin que sirva para nada."""
        self._correr('--objetivo', '3.0.2.28', '--aplicar')
        self.sin_farmacias.refresh_from_db()
        self.assertEqual(self.sin_farmacias.version_objetivo, '')

    def test_no_pisa_un_objetivo_ya_cargado(self):
        """Alguien pudo ponerlo sabiendo algo que este comando no."""
        texto = self._correr('--objetivo', '3.0.2.28', '--aplicar')
        self.ya_cargado.refresh_from_db()
        self.assertEqual(self.ya_cargado.version_objetivo, '3.0.2.27')
        self.assertIn('--pisar', texto)

    def test_con_pisar_si_lo_cambia(self):
        self._correr('--objetivo', '3.0.2.28', '--aplicar', '--pisar')
        self.ya_cargado.refresh_from_db()
        self.assertEqual(self.ya_cargado.version_objetivo, '3.0.2.28')

    def test_rechaza_algo_que_no_parece_una_version(self):
        """Un dedazo quedaría fijado como objetivo de cientos de farmacias."""
        from django.core.management.base import CommandError

        for malo in ('3.0.2.28-beta', 'ultima', '3', ''):
            with self.assertRaises(CommandError):
                self._correr('--objetivo', malo)

    def test_acota_a_los_grupos_pedidos(self):
        self._correr('--objetivo', '3.0.2.28', '--grupos', 'TRX003', '--aplicar')
        self.con_farmacias.refresh_from_db()
        self.ya_cargado.refresh_from_db()
        self.assertEqual(self.con_farmacias.version_objetivo, '3.0.2.28')
        self.assertEqual(self.ya_cargado.version_objetivo, '3.0.2.27')

    def test_falla_si_se_pide_un_grupo_que_no_existe(self):
        """Mejor que omitirlo en silencio: un código mal escrito dejaría ese grupo sin
        objetivo sin que nadie se entere."""
        from django.core.management.base import CommandError

        with self.assertRaises(CommandError):
            self._correr('--objetivo', '3.0.2.28', '--grupos', 'NOEXISTE')


class AnchoContratadoTests(TestCase):
    """Consumo contra el ancho contratado.

    La conversión es lo único delicado: el consumo se mide en **kilobits** por segundo y
    lo contratado en **megabits**. Ese factor de 1000 —y el de 8 entre bits y bytes que se
    corrigió el mismo día— es exactamente donde se rompe una comparación de este tipo.

    A 15-sep-2026 el parque son 579 farmacias TELCONET y 109 PUNTO NET, casi todas con
    10 Mbps; las 12 restantes (FIBROMARK, ETAPA, CLARO, GONET, CORVINET) sin dato.
    """

    def setUp(self):
        self.sg = UnidadNegocio.objects.get(codigo='SG')
        self.grupo = Grupo.objects.create(codigo='TRX001')

    def _farmacia(self, codigo, **extra):
        return Farmacia.objects.create(
            codigo=codigo, grupo=self.grupo, unidad_negocio=self.sg, **extra,
        )

    def _muestra(self, farmacia, recibido, enviado):
        from apps.monitoreo.models import MuestraRedFarmacia

        return MuestraRedFarmacia.objects.create(
            farmacia=farmacia, bytes_recibidos=0, bytes_enviados=0,
            red_recibido_kbps=recibido, red_enviado_kbps=enviado,
        )

    def test_el_porcentaje_convierte_kilobits_contra_megabits(self):
        """831 kbps sobre 10 Mbps son 8,3%. Si alguien tratara el consumo como kilobytes
        daría 66,5% — el error que tenían las pantallas hasta hoy."""
        farmacia = self._farmacia('MC001', ancho_contratado_mbps=10)
        muestra = self._muestra(farmacia, 831, 0)
        self.assertEqual(muestra.porcentaje_del_contratado, 8.3)

    def test_un_enlace_saturado_da_cerca_de_cien(self):
        farmacia = self._farmacia('ML002', ancho_contratado_mbps=10)
        muestra = self._muestra(farmacia, 8000, 1500)
        self.assertEqual(muestra.porcentaje_del_contratado, 95.0)

    def test_puede_pasar_de_cien(self):
        """No se recorta el valor: un enlace que mide más de lo contratado es un dato
        real —ráfaga, o el contrato no es el que creemos— y esconderlo sería peor."""
        farmacia = self._farmacia('ML003', ancho_contratado_mbps=10)
        self.assertGreater(self._muestra(farmacia, 12000, 0).porcentaje_del_contratado, 100)

    def test_sin_ancho_contratado_no_inventa_un_porcentaje(self):
        """Fingir un valor típico haría que la barra mintiera justo en las farmacias de
        las que menos se sabe."""
        farmacia = self._farmacia('ML004')
        self.assertIsNone(self._muestra(farmacia, 831, 0).porcentaje_del_contratado)

    def test_sin_lectura_de_consumo_tampoco(self):
        farmacia = self._farmacia('ML005', ancho_contratado_mbps=10)
        self.assertIsNone(self._muestra(farmacia, None, None).porcentaje_del_contratado)


class FijarAnchoContratadoTests(TestCase):
    """Carga masiva de `ancho_contratado_mbps` agrupando por proveedor."""

    def setUp(self):
        self.sg = UnidadNegocio.objects.get(codigo='SG')
        grupo = Grupo.objects.create(codigo='TRX001')
        self.telconet = Farmacia.objects.create(
            codigo='ML001', grupo=grupo, unidad_negocio=self.sg, tipo_enlace='TELCONET',
        )
        self.puntonet = Farmacia.objects.create(
            codigo='ML002', grupo=grupo, unidad_negocio=self.sg, tipo_enlace='PUNTO NET',
        )
        self.otra = Farmacia.objects.create(
            codigo='ML003', grupo=grupo, unidad_negocio=self.sg, tipo_enlace='FIBROMARK',
        )

    def _correr(self, *args):
        salida = io.StringIO()
        call_command('fijar_ancho_contratado', *args, stdout=salida)
        return salida.getvalue()

    def test_por_defecto_simula(self):
        texto = self._correr('--mbps', '10', '--proveedores', 'TELCONET')
        self.assertIn('Se actualizarían', texto)
        self.telconet.refresh_from_db()
        self.assertIsNone(self.telconet.ancho_contratado_mbps)

    def test_carga_los_dos_proveedores_y_no_toca_el_resto(self):
        """Las 12 de proveedores minoritarios quedan sin dato hasta saber cuánto tienen —
        que es correcto: vacío significa "no se sabe", no "cero"."""
        self._correr('--mbps', '10', '--proveedores', 'TELCONET,PUNTO NET', '--aplicar')

        for farmacia in (self.telconet, self.puntonet, self.otra):
            farmacia.refresh_from_db()
        self.assertEqual(self.telconet.ancho_contratado_mbps, 10)
        self.assertEqual(self.puntonet.ancho_contratado_mbps, 10)
        self.assertIsNone(self.otra.ancho_contratado_mbps)

    def test_no_pisa_un_valor_ya_cargado(self):
        """Si alguien cargó 20 Mbps en una farmacia puntual, sabe algo que el comando no."""
        self.telconet.ancho_contratado_mbps = 20
        self.telconet.save(update_fields=['ancho_contratado_mbps'])

        texto = self._correr('--mbps', '10', '--proveedores', 'TELCONET', '--aplicar')

        self.telconet.refresh_from_db()
        self.assertEqual(self.telconet.ancho_contratado_mbps, 20)
        self.assertIn('DEJAN como están', texto)

    def test_con_pisar_si_lo_cambia(self):
        self.telconet.ancho_contratado_mbps = 20
        self.telconet.save(update_fields=['ancho_contratado_mbps'])
        self._correr('--mbps', '10', '--proveedores', 'TELCONET', '--aplicar', '--pisar')
        self.telconet.refresh_from_db()
        self.assertEqual(self.telconet.ancho_contratado_mbps, 10)

    def test_acota_a_farmacias_puntuales(self):
        self._correr('--mbps', '50', '--farmacias', 'ML003', '--aplicar')
        self.otra.refresh_from_db()
        self.telconet.refresh_from_db()
        self.assertEqual(self.otra.ancho_contratado_mbps, 50)
        self.assertIsNone(self.telconet.ancho_contratado_mbps)

    def test_exige_un_filtro(self):
        """Sin filtro tocaría las 700 de una, incluidas las que tienen otro contrato."""
        with self.assertRaises(CommandError):
            self._correr('--mbps', '10')

    def test_rechaza_un_valor_absurdo(self):
        """Cargar 1000 en vez de 10 dejaría a esa farmacia en "0,08% de uso" para siempre
        y nadie lo miraría dos veces."""
        for malo in ('0', '-5', '99999'):
            with self.assertRaises(CommandError):
                self._correr('--mbps', malo, '--proveedores', 'TELCONET')

    def test_avisa_si_ningun_proveedor_coincide(self):
        """Un nombre mal escrito tiene que fallar, no actualizar cero farmacias en
        silencio. El mensaje lista los valores que sí existen."""
        with self.assertRaises(CommandError) as ctx:
            self._correr('--mbps', '10', '--proveedores', 'TELCONEt SA')
        self.assertIn('TELCONET', str(ctx.exception))


class ArmarPaqueteAgenteTests(TestCase):
    """El zip de instalación que se publica en /media/ para bajarlo desde las estaciones.

    La decisión que define este comando: **no lleva `config.txt`**. `/media/` se sirve por
    HTTP sin autenticación —a propósito, para que los agentes descarguen— así que todo lo
    que entre al zip queda público.

    De los dos secretos que ese archivo llevaba ya queda uno: `ComandoHmacSecret` dejó de
    hacer falta (el agente 0.21 recibe el suyo al enrolarse). El que falta es
    `MqttPassword`, que hoy tiene ACL sobre `/saidsof/#` — publicarla dejaría leer el
    tráfico de toda la cadena, incluidos los secretos propios que viajan en las respuestas
    de enrolamiento. Se resuelve corriendo `deploy/emqx-narrow-acl-agente.sh`.
    """

    def setUp(self):
        import shutil

        self.medios = tempfile.mkdtemp()
        # El override tiene que cubrir tambien la creacion de la VersionAgente: si solo
        # envolviera al comando, el ejecutable quedaria guardado bajo el MEDIA_ROOT real
        # y `ejecutable.path` apuntaria al temporal, donde no existe.
        medios = override_settings(MEDIA_ROOT=self.medios)
        medios.enable()
        self.addCleanup(medios.disable)
        self.addCleanup(shutil.rmtree, self.medios, True)
        self.admin = User.objects.create_superuser(username='u_paq', password='x' * 16)

    def _version(self, nombre='agente-prueba-0.20'):
        from django.core.files.uploadedfile import SimpleUploadedFile

        from apps.catalogo.models import VersionAgente

        return VersionAgente.objects.create(
            version=nombre, creado_por=self.admin,
            ejecutable=SimpleUploadedFile('Saidsoft.Agente.exe', b'MZ' + b'\0' * 200),
        )

    def _armar(self, *args):
        salida = io.StringIO()
        call_command('armar_paquete_agente', *args, stdout=salida)
        return salida.getvalue()

    def _contenido(self):
        import zipfile
        from pathlib import Path

        ruta = Path(self.medios) / 'agente-instalador' / 'agente-instalador.zip'
        self.assertTrue(ruta.is_file(), 'no se generó el zip')
        with zipfile.ZipFile(ruta) as paquete:
            return paquete.namelist()

    def test_arma_el_paquete_con_lo_necesario_para_instalar(self):
        self._version()
        self._armar()
        nombres = self._contenido()
        for esperado in ('Saidsoft.Agente.exe', 'instalar-servicio.ps1', 'Instalar.bat',
                         'config.ejemplo.txt', 'cert.pem', 'LEEME.txt'):
            self.assertIn(esperado, nombres)

    def test_nunca_incluye_config_txt(self):
        """La prueba que más importa: es la diferencia entre un instalador y una fuga."""
        self._version()
        self._armar()
        self.assertNotIn('config.txt', self._contenido())

    def test_incluye_el_certificado_publico_pero_no_la_clave_privada(self):
        """`cert.pem` es lo que el agente usa para validar TLS y tiene que viajar.
        `key.pem` es la clave privada de EMQX y no puede salir del servidor."""
        self._version()
        self._armar()
        nombres = self._contenido()
        self.assertIn('cert.pem', nombres)
        self.assertNotIn('key.pem', nombres)

    def test_usa_la_ultima_version_cargada(self):
        self._version('agente-prueba-0.19')
        self._version('agente-prueba-0.20')
        texto = self._armar()
        self.assertIn('agente-prueba-0.20', texto)

    def test_se_puede_pedir_una_version_puntual(self):
        """Para volver atrás: si el 0.20 rompiera algo, se reempaqueta el 0.19 sin
        recompilar nada."""
        self._version('agente-prueba-0.19')
        self._version('agente-prueba-0.20')
        texto = self._armar('--agente', 'agente-prueba-0.19')
        self.assertIn('agente-prueba-0.19', texto)

    def test_falla_si_la_version_pedida_no_existe(self):
        self._version()
        with self.assertRaises(CommandError):
            self._armar('--agente', 'agente-prueba-9.9')

    def test_falla_si_no_hay_ninguna_version_cargada(self):
        """Mejor que armar un paquete sin ejecutable, que fallaría recién en la estación."""
        with self.assertRaises(CommandError):
            self._armar()

    def test_el_leeme_explica_por_que_faltan_los_secretos(self):
        """Quien abra el zip en una estación tiene que entender qué falta y por qué, o va
        a pensar que el paquete está incompleto."""
        import zipfile
        from pathlib import Path

        self._version()
        self._armar()
        ruta = Path(self.medios) / 'agente-instalador' / 'agente-instalador.zip'
        with zipfile.ZipFile(ruta) as paquete:
            leeme = paquete.read('LEEME.txt').decode('utf-8')
        self.assertIn('MqttPassword', leeme)
        self.assertIn('FARMACIA-SUFIJO', leeme)
        self.assertIn('PENDIENTE DE APROBACION', leeme)

    def _leeme(self, version):
        import zipfile
        from pathlib import Path

        self._version(version)
        self._armar('--agente', version)
        ruta = Path(self.medios) / 'agente-instalador' / 'agente-instalador.zip'
        with zipfile.ZipFile(ruta) as paquete:
            return paquete.read('LEEME.txt').decode('utf-8')

    def test_con_un_agente_021_el_leeme_pide_un_solo_valor(self):
        leeme = self._leeme('agente-prueba-0.21')
        self.assertIn('ComandoHmacSecret va VACIO', leeme)
        self.assertNotIn('COMANDO_HMAC_SECRET del deploy', leeme)

    def test_con_un_agente_viejo_el_leeme_pide_los_dos(self):
        """El caso que motivó esto: el paquete quedó con binario 0.20 y un LEEME escrito
        para 0.21. Quien instalara habría dejado ComandoHmacSecret vacío, la instalación
        habría salido bien, y esa estación habría descartado en silencio todo comando —
        sin nada visible en el panel. La instrucción tiene que seguir al binario, no a la
        última idea que tuvimos."""
        leeme = self._leeme('agente-prueba-0.20')
        self.assertIn('COMANDO_HMAC_SECRET del deploy', leeme)
        self.assertNotIn('ComandoHmacSecret va VACIO', leeme)
        self.assertIn('descarta en silencio', leeme)

    def test_rearmarlo_reemplaza_el_anterior(self):
        """Se corre después de cada build del agente: no puede ir acumulando zips."""
        from pathlib import Path

        self._version('agente-prueba-0.19')
        self._armar()
        self._version('agente-prueba-0.20')
        self._armar()

        carpeta = Path(self.medios) / 'agente-instalador'
        self.assertEqual(len(list(carpeta.glob('*.zip'))), 1)


class SecretoHmacPorEstacionTests(TestCase):
    """Con qué secreto se firma cada comando.

    Para qué existe esto: hasta ahora el `COMANDO_HMAC_SECRET` era uno solo para las 700
    farmacias y había que escribirlo a mano en el `config.txt` de cada equipo. Eso obliga
    a las dos cosas que se quieren evitar — que el instalador no pueda publicarse completo
    (§10-Z: el paquete con los secretos adentro quedó descargable sin autenticación y hubo
    que rotar la flota entera), y que alguien termine pasando el secreto por WhatsApp para
    no tipearlo 700 veces.

    El riesgo del cambio es el opuesto: firmar con el secreto propio para un agente que no
    sabe verificarlo deja a esa estación sin recibir comandos, y eso **no se ve desde el
    panel** — el comando se publica bien, el agente lo descarta en silencio en su log
    local. De ahí que todo acá esté escrito para errar hacia el compartido.
    """

    def setUp(self):
        grupo = Grupo.objects.create(codigo='TRX001')
        self.farmacia = Farmacia.objects.create(
            codigo='ML001', grupo=grupo, unidad_negocio=UnidadNegocio.objects.get(codigo='SG'),
        )

    def _estacion(self, codigo='ML001-A', version='', confirmado=False):
        return Estacion.objects.create(
            codigo=codigo, farmacia=self.farmacia, version_agente=version,
            hmac_propio_confirmado=confirmado,
        )

    def _firma_publicada(self, estacion):
        with patch('apps.catalogo.services.mqtt_publish.single') as mock_single:
            enviar_comando(estacion, 'reiniciar')
        return json.loads(mock_single.call_args.args[1])

    def test_cada_estacion_nace_con_su_propio_secreto(self):
        a, b = self._estacion('ML001-A'), self._estacion('ML001-B')
        self.assertEqual(len(a.hmac_secret), 64)
        self.assertNotEqual(a.hmac_secret, b.hmac_secret)

    def test_un_agente_viejo_sigue_recibiendo_la_firma_compartida(self):
        """El 0.20 solo conoce el secreto de su config.txt. Firmarle con el propio sería
        dejarlo incomunicado sin que nadie se entere."""
        estacion = self._estacion(version='agente-prueba-0.20')
        payload = self._firma_publicada(estacion)
        esperada = firmar_payload(
            comando='reiniciar', estacion='ML001-A', timestamp=payload['timestamp'],
        )
        self.assertEqual(payload['firma'], esperada)

    def test_un_agente_021_recibe_la_firma_con_su_secreto_propio(self):
        estacion = self._estacion(version='agente-prueba-0.21', confirmado=True)
        payload = self._firma_publicada(estacion)
        esperada = firmar_payload(
            estacion.hmac_secret,
            comando='reiniciar', estacion='ML001-A', timestamp=payload['timestamp'],
        )
        self.assertEqual(payload['firma'], esperada)

    def test_el_secreto_propio_de_una_estacion_no_firma_para_otra(self):
        """Es el punto entero del cambio: filtrar el secreto de un equipo compromete ese
        equipo, no la cadena."""
        a = self._estacion('ML001-A', version='agente-prueba-0.21', confirmado=True)
        b = self._estacion('ML001-B', version='agente-prueba-0.21', confirmado=True)
        payload = self._firma_publicada(a)
        con_el_de_b = firmar_payload(
            b.hmac_secret, comando='reiniciar', estacion='ML001-A', timestamp=payload['timestamp'],
        )
        self.assertNotEqual(payload['firma'], con_el_de_b)

    def test_una_estacion_actualizada_a_021_que_no_confirmo_usa_la_compartida(self):
        """El incidente del 16-sep-2026, y el motivo de que la capacidad se declare.

        ML014-B y ML016-A reportaban 0.21 y descartaban todos los comandos en silencio,
        mientras ML017-B —misma versión— funcionaba. La diferencia: ML017-B se instaló de
        cero y se enroló DESPUÉS de que existiera el secreto propio; las otras dos venían
        enroladas de antes y solo se actualizó su ejecutable.

        El secreto viaja en la respuesta de enrolamiento, y un agente que ya tiene
        identidad.json no vuelve a enrolarse. Así que la versión decía "entiendo el
        mecanismo" y el servidor lo leía como "tengo el secreto". No es lo mismo.
        """
        estacion = self._estacion(version='agente-prueba-0.21', confirmado=False)
        self.assertIsNone(
            secreto_de(estacion),
            'la version no alcanza: sin confirmacion hay que usar la compartida',
        )

    def test_la_confirmacion_manda_sobre_la_version(self):
        """No se vuelve a mirar la versión para decidir. Si el agente dice que lo tiene,
        lo tiene: es el único que puede saberlo."""
        estacion = self._estacion(version='', confirmado=True)
        self.assertEqual(secreto_de(estacion), estacion.hmac_secret)

    def test_una_estacion_que_nunca_reporto_version_usa_la_compartida(self):
        """Recién enrolada y todavía sin heartbeat: no hay evidencia de que entienda el
        secreto propio, así que no se asume."""
        self.assertIsNone(secreto_de(self._estacion(version='')))

    def test_una_version_con_formato_raro_tambien_cae_en_la_compartida(self):
        for rara in ('agente-prueba-beta', '0.21-rc1', 'Saidsoft.Agente'):
            self.assertIsNone(secreto_de(self._estacion('ML001-%s' % rara[:3], version=rara)), rara)

    def test_una_version_posterior_tambien_lo_soporta(self):
        estacion = self._estacion(version='agente-prueba-0.22', confirmado=True)
        self.assertEqual(secreto_de(estacion), estacion.hmac_secret)

    def test_un_downgrade_del_agente_vuelve_sola_a_la_compartida(self):
        """Si hay que rollbackear el agente a 0.20, la estación reporta esa versión en el
        siguiente heartbeat y el servidor vuelve a firmarle con la compartida sin que
        nadie toque nada. Sin esto, un rollback la dejaría muda."""
        estacion = self._estacion(version='agente-prueba-0.21', confirmado=True)
        self.assertIsNotNone(secreto_de(estacion))
        estacion.hmac_propio_confirmado = False
        estacion.version_agente = 'agente-prueba-0.20'
        estacion.save(update_fields=['version_agente', 'hmac_propio_confirmado'])
        self.assertIsNone(secreto_de(estacion))

    def test_sin_secreto_explicito_firma_con_el_compartido(self):
        """El fallback es lo que sostiene la convivencia: `secreto_de` devuelve None para
        toda estación con agente anterior a 0.21, y esas tienen que seguir recibiendo
        comandos firmados con el `COMANDO_HMAC_SECRET` de siempre."""
        from django.conf import settings

        import hashlib
        import hmac as hmac_mod

        esperada = hmac_mod.new(
            settings.COMANDO_HMAC_SECRET.encode(), b'reiniciar', hashlib.sha256,
        ).hexdigest()
        self.assertEqual(firmar_payload(comando='reiniciar'), esperada)
        self.assertEqual(firmar_payload(None, comando='reiniciar'), esperada)


class AgenteAceptaAmbosSecretosTests(TestCase):
    """El otro lado del contrato: qué firmas da por buenas el agente 0.21.

    Se carga el módulo real del agente y se ejercita `_firma_valida`, en vez de
    reimplementar la lógica acá — que es como se escapó el bug de `_normalizar_mac`:
    la prueba pasaba porque le daba al código de producción una entrada ya masajeada
    que nunca ocurre en la realidad.

    Lo que tiene que ser cierto durante toda la migración: el agente acepta **tanto** el
    secreto propio como el compartido. Si aceptara solo uno, cada rollout tendría un
    orden obligatorio entre servidor y flota, y cualquier estación apagada en el medio
    quedaría muda hasta que alguien fuera a la farmacia.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        import importlib.util
        from pathlib import Path

        from django.conf import settings

        # `agente-prueba` no es un paquete importable (el guion del nombre lo impide),
        # así que se carga por ruta — mismo motivo que los tests que leen su fuente.
        ruta = Path(settings.BASE_DIR) / 'agente-prueba' / 'agente_prueba.py'
        spec = importlib.util.spec_from_file_location('agente_prueba_bajo_prueba', ruta)
        cls.agente_mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.agente_mod)

    def _agente(self, *, propio=None, compartido=''):
        from types import SimpleNamespace

        # __new__ y no __init__: el constructor arma el cliente MQTT y lee config del
        # disco, nada de lo cual hace falta para verificar una firma.
        agente = self.agente_mod.AgentePrueba.__new__(self.agente_mod.AgentePrueba)
        agente.identidad = {'hmac_secret': propio} if propio else {}
        agente.args = SimpleNamespace(hmac_secret=compartido, codigo='ML001-A')
        return agente

    def _campos(self):
        import time

        return {'comando': 'reiniciar', 'estacion': 'ML001-A', 'timestamp': int(time.time())}

    def _payload(self, firma, campos):
        return {'firma': firma, 'timestamp': campos['timestamp']}

    def test_acepta_la_firma_hecha_con_el_secreto_propio(self):
        campos = self._campos()
        firma = firmar_payload('secreto-propio-de-ml001a', **campos)
        agente = self._agente(propio='secreto-propio-de-ml001a', compartido='el-de-la-flota')
        self.assertTrue(agente._firma_valida('comando', self._payload(firma, campos), **campos))

    def test_sigue_aceptando_la_firma_hecha_con_el_compartido(self):
        """El servidor firma con el compartido hasta que ve la versión 0.21 reportada.
        Entre que el agente se actualiza y llega ese heartbeat hay una ventana real."""
        campos = self._campos()
        firma = firmar_payload('el-de-la-flota', **campos)
        agente = self._agente(propio='secreto-propio-de-ml001a', compartido='el-de-la-flota')
        self.assertTrue(agente._firma_valida('comando', self._payload(firma, campos), **campos))

    def test_rechaza_una_firma_hecha_con_el_secreto_de_otra_estacion(self):
        campos = self._campos()
        firma = firmar_payload('secreto-de-ml001b', **campos)
        agente = self._agente(propio='secreto-propio-de-ml001a', compartido='el-de-la-flota')
        self.assertFalse(agente._firma_valida('comando', self._payload(firma, campos), **campos))

    def test_sin_ningun_secreto_rechaza_todo(self):
        """Falla cerrado: un config.txt sin `ComandoHmacSecret` y sin enrolar todavía no
        puede quedar aceptando cualquier cosa."""
        campos = self._campos()
        firma = firmar_payload('cualquiera', **campos)
        agente = self._agente()
        self.assertFalse(agente._firma_valida('comando', self._payload(firma, campos), **campos))

    def test_un_agente_recien_instalado_funciona_solo_con_el_compartido(self):
        """Antes del primer enrolamiento no tiene secreto propio: es exactamente el
        arranque de toda estación nueva."""
        campos = self._campos()
        firma = firmar_payload('el-de-la-flota', **campos)
        agente = self._agente(compartido='el-de-la-flota')
        self.assertTrue(agente._firma_valida('comando', self._payload(firma, campos), **campos))

    def test_la_version_del_agente_coincide_con_la_que_el_servidor_espera(self):
        """Acoplamiento cargante: `secreto_de` decide por número de versión. Si alguien
        sube el agente sin mover la constante del servidor (o al revés), las estaciones
        dejan de recibir comandos y el panel no muestra nada raro."""
        from apps.catalogo.services import VERSION_AGENTE_CON_HMAC_PROPIO, _version_agente

        self.assertGreaterEqual(
            _version_agente(self.agente_mod.VERSION_AGENTE_PRUEBA), VERSION_AGENTE_CON_HMAC_PROPIO,
            'el agente del repo es anterior a la versión que el servidor da por capaz de '
            'verificar el secreto propio',
        )


class CorregirIpRouterProveedorTests(TestCase):
    """La IP del borde en las redes del proveedor es .254, no .1.

    Medido el 18-sep-2026 sobre 24 farmacias: las 12 que el panel daba por caidas
    respondian en .254, y las 12 que respondian en .1 tambien lo hacian en .254 con
    MENOS latencia. O sea que muchas "caidas" eran falsos positivos por apuntar a una IP
    que no era el router.
    """

    def setUp(self):
        self.sg = UnidadNegocio.objects.get(codigo='SG')
        self.grupo = Grupo.objects.create(codigo='TRX930')
        self.proveedor = Farmacia.objects.create(
            codigo='TSTI10', grupo=self.grupo, unidad_negocio=self.sg, ip_router='192.169.100.1')
        self.otra_proveedor = Farmacia.objects.create(
            codigo='TSTI11', grupo=self.grupo, unidad_negocio=self.sg, ip_router='192.170.55.1')
        # Mikrotik propio: su IP ya es la correcta y NO debe tocarse.
        self.mikrotik = Farmacia.objects.create(
            codigo='TSTI12', grupo=self.grupo, unidad_negocio=self.sg, ip_router='10.101.36.97')
        # 192.x pero ya corregida: tampoco es candidata.
        self.ya_ok = Farmacia.objects.create(
            codigo='TSTI13', grupo=self.grupo, unidad_negocio=self.sg, ip_router='192.168.20.254')

    def _correr(self, *args, responde=True):
        salida = io.StringIO()
        with patch('apps.monitoreo.enlaces.sondear_enlace', return_value=(responde, 21.0)):
            with patch('apps.monitoreo.enlaces.verificar_ping_disponible'):
                call_command('corregir_ip_router_proveedor', *args, stdout=salida)
        return salida.getvalue()

    def test_sin_aplicar_no_escribe_nada(self):
        """Simulacro por defecto: 299 registros en produccion no se tocan por accidente."""
        salida = self._correr()
        self.proveedor.refresh_from_db()
        self.assertEqual(str(self.proveedor.ip_router), '192.169.100.1')
        self.assertIn('Simulacro', salida)
        self.assertIn('192.169.100.1 -> 192.169.100.254', salida)

    def test_con_aplicar_corrige_las_del_proveedor(self):
        self._correr('--aplicar')
        self.proveedor.refresh_from_db()
        self.otra_proveedor.refresh_from_db()
        self.assertEqual(str(self.proveedor.ip_router), '192.169.100.254')
        self.assertEqual(str(self.otra_proveedor.ip_router), '192.170.55.254')

    def test_no_toca_los_mikrotik_propios(self):
        """Las 10.101.x son los Mikrotik que hoy si responden SNMP: cambiarlas romperia
        el unico monitoreo de trafico que funciona."""
        self._correr('--aplicar')
        self.mikrotik.refresh_from_db()
        self.assertEqual(str(self.mikrotik.ip_router), '10.101.36.97')

    def test_no_toca_las_que_ya_estaban_bien(self):
        self._correr('--aplicar')
        self.ya_ok.refresh_from_db()
        self.assertEqual(str(self.ya_ok.ip_router), '192.168.20.254')

    def test_no_cambia_la_que_tampoco_responde_en_254(self):
        """Puede ser una caida real o un sitio de baja: cambiarle la IP esconderia el
        problema detras de un dato nuevo."""
        salida = self._correr('--aplicar', responde=False)
        self.proveedor.refresh_from_db()
        self.assertEqual(str(self.proveedor.ip_router), '192.169.100.1')
        self.assertIn('sin tocar', salida)

    def test_forzar_salta_la_verificacion(self):
        salida = io.StringIO()
        with patch('apps.monitoreo.enlaces.sondear_enlace') as sondear:
            call_command('corregir_ip_router_proveedor', '--aplicar', '--forzar', stdout=salida)
        sondear.assert_not_called()
        self.proveedor.refresh_from_db()
        self.assertEqual(str(self.proveedor.ip_router), '192.169.100.254')

    def test_se_puede_acotar_a_un_prefijo(self):
        self._correr('--aplicar', '--prefijo', '192.170')
        self.proveedor.refresh_from_db()
        self.otra_proveedor.refresh_from_db()
        self.assertEqual(str(self.proveedor.ip_router), '192.169.100.1', 'fuera del prefijo pedido')
        self.assertEqual(str(self.otra_proveedor.ip_router), '192.170.55.254')

    def test_sin_candidatas_lo_dice_y_no_falla(self):
        Farmacia.objects.filter(pk__in=[self.proveedor.pk, self.otra_proveedor.pk]).update(
            ip_router='192.169.1.254')
        salida = self._correr('--aplicar')
        self.assertIn('Ninguna farmacia', salida)
