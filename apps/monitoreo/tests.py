import json
from datetime import timedelta
from unittest.mock import MagicMock

import websocket
from django.contrib.auth.models import User
from django.core import mail
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.utils import timezone

from apps.catalogo.models import Estacion, Farmacia, Grupo, UnidadNegocio
from apps.cuentas.models import PerfilUsuario
from apps.monitoreo.adapters.meshcentral import AdaptadorMeshCentral, _en_linea, _id_corto

from .models import Alerta, EstadoDispositivo, EventoMonitoreo, Metrica, MuestraMetrica, ReglaAlerta
from .services import (
    evaluar_cruce_monitoreo, evaluar_regla_bitlocker, evaluar_reglas_metricas, registrar_estado_dispositivo,
    reglas_aplicables_a, resolver_alertas_agente_caido_red_viva, resolver_alertas_bitlocker,
    resolver_alertas_sin_heartbeat,
)


class EvaluarReglasMetricasTests(TestCase):
    def setUp(self):
        sg = UnidadNegocio.objects.get(codigo='SG')
        grupo = Grupo.objects.create(codigo='TRX001')
        farmacia = Farmacia.objects.create(codigo='ML001', grupo=grupo, unidad_negocio=sg)
        self.estacion = Estacion.objects.create(
            codigo='ML001-A', farmacia=farmacia, estado_aprobacion=Estacion.EstadoAprobacion.APROBADA,
            monitorear_recursos=True,
        )
        self.usuario = User.objects.create_user(username='u', password='x')
        self.regla = ReglaAlerta.objects.create(
            nombre='CPU alta', metrica=Metrica.CPU_CARGA_PCT, operador=ReglaAlerta.Operador.GTE,
            umbral=90, duracion_minutos=10, creado_por=self.usuario,
        )

    def _crear_muestra(self, cpu, hace_minutos=0):
        muestra = MuestraMetrica.objects.create(estacion=self.estacion, cpu_carga_pct=cpu)
        if hace_minutos:
            MuestraMetrica.objects.filter(pk=muestra.pk).update(
                timestamp=timezone.now() - timedelta(minutes=hace_minutos),
            )
            muestra.refresh_from_db()
        return muestra

    def test_pico_aislado_no_abre_alerta(self):
        # Sin historial previo (estación "nueva"): una sola muestra alta no alcanza
        # para confirmar que la condición se sostuvo duracion_minutos.
        muestra = self._crear_muestra(95)
        evaluar_reglas_metricas(self.estacion, muestra)
        self.assertFalse(Alerta.objects.exists())

    def test_condicion_sostenida_abre_alerta(self):
        self._crear_muestra(95, hace_minutos=15)  # más vieja que duracion_minutos=10
        muestra = self._crear_muestra(96)
        evaluar_reglas_metricas(self.estacion, muestra)

        alerta = Alerta.objects.get()
        self.assertEqual(alerta.estado, Alerta.Estado.ABIERTA)
        self.assertEqual(alerta.valor_disparador, 96)

    def test_no_duplica_alerta_ya_activa(self):
        self._crear_muestra(95, hace_minutos=15)
        evaluar_reglas_metricas(self.estacion, self._crear_muestra(96))
        evaluar_reglas_metricas(self.estacion, self._crear_muestra(97))
        self.assertEqual(Alerta.objects.count(), 1)

    def test_se_resuelve_sola_al_normalizarse(self):
        self._crear_muestra(95, hace_minutos=15)
        evaluar_reglas_metricas(self.estacion, self._crear_muestra(96))
        evaluar_reglas_metricas(self.estacion, self._crear_muestra(50))

        alerta = Alerta.objects.get()
        self.assertEqual(alerta.estado, Alerta.Estado.RESUELTA)
        self.assertIsNotNone(alerta.resuelta_en)

    def test_notifica_por_correo_al_abrir_no_al_sostenerla(self):
        PerfilUsuario.objects.create(usuario=self.usuario, acceso_todas_unidades=True)
        self.usuario.email = 'ops@example.com'
        self.usuario.save(update_fields=['email'])

        self._crear_muestra(95, hace_minutos=15)
        evaluar_reglas_metricas(self.estacion, self._crear_muestra(96))
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn('CPU alta', mail.outbox[0].subject)

        # Una segunda muestra que sigue incumpliendo no debe reenviar el correo.
        evaluar_reglas_metricas(self.estacion, self._crear_muestra(97))
        self.assertEqual(len(mail.outbox), 1)


class SinHeartbeatAlertaTests(TestCase):
    def setUp(self):
        sg = UnidadNegocio.objects.get(codigo='SG')
        grupo = Grupo.objects.create(codigo='TRX001')
        farmacia = Farmacia.objects.create(codigo='ML001', grupo=grupo, unidad_negocio=sg)
        self.estacion = Estacion.objects.create(
            codigo='ML001-A', farmacia=farmacia, estado_aprobacion=Estacion.EstadoAprobacion.APROBADA,
            estado_conexion=Estacion.EstadoConexion.ONLINE,
            ultimo_heartbeat=timezone.now() - timedelta(minutes=20),
        )
        usuario = User.objects.create_user(username='u2', password='x')
        self.regla = ReglaAlerta.objects.create(
            nombre='Estación caída', metrica=Metrica.SIN_HEARTBEAT, umbral=10, creado_por=usuario,
        )

    def test_marcar_offline_abre_alerta_sin_heartbeat(self):
        call_command('marcar_estaciones_offline')

        self.estacion.refresh_from_db()
        self.assertEqual(self.estacion.estado_conexion, Estacion.EstadoConexion.OFFLINE)
        alerta = Alerta.objects.get()
        self.assertEqual(alerta.regla, self.regla)
        self.assertEqual(alerta.estado, Alerta.Estado.ABIERTA)

    def test_heartbeat_de_nuevo_resuelve_la_alerta(self):
        call_command('marcar_estaciones_offline')
        alerta = Alerta.objects.get()

        resolver_alertas_sin_heartbeat(self.estacion)

        alerta.refresh_from_db()
        self.assertEqual(alerta.estado, Alerta.Estado.RESUELTA)

    def test_marcar_offline_registra_estado_dispositivo_mqtt(self):
        call_command('marcar_estaciones_offline')
        estado = EstadoDispositivo.objects.get(estacion=self.estacion, fuente=EstadoDispositivo.Fuente.MQTT)
        self.assertFalse(estado.en_linea)


class RegistrarEstadoDispositivoTests(TestCase):
    def setUp(self):
        sg = UnidadNegocio.objects.get(codigo='SG')
        grupo = Grupo.objects.create(codigo='TRX001')
        farmacia = Farmacia.objects.create(codigo='ML001', grupo=grupo, unidad_negocio=sg)
        self.estacion = Estacion.objects.create(codigo='ML001-A', farmacia=farmacia)

    def test_primera_senal_crea_snapshot_y_evento(self):
        registrar_estado_dispositivo(self.estacion, fuente=EstadoDispositivo.Fuente.MESHCENTRAL, en_linea=True)
        estado = EstadoDispositivo.objects.get(estacion=self.estacion, fuente=EstadoDispositivo.Fuente.MESHCENTRAL)
        self.assertTrue(estado.en_linea)
        self.assertEqual(EventoMonitoreo.objects.filter(estacion=self.estacion).count(), 1)

    def test_senal_repetida_no_duplica_snapshot_ni_evento(self):
        registrar_estado_dispositivo(self.estacion, fuente=EstadoDispositivo.Fuente.MESHCENTRAL, en_linea=True)
        registrar_estado_dispositivo(self.estacion, fuente=EstadoDispositivo.Fuente.MESHCENTRAL, en_linea=True)
        self.assertEqual(EstadoDispositivo.objects.filter(estacion=self.estacion).count(), 1)
        self.assertEqual(EventoMonitoreo.objects.filter(estacion=self.estacion).count(), 1)

    def test_transicion_agrega_nuevo_evento(self):
        registrar_estado_dispositivo(self.estacion, fuente=EstadoDispositivo.Fuente.MESHCENTRAL, en_linea=True)
        registrar_estado_dispositivo(self.estacion, fuente=EstadoDispositivo.Fuente.MESHCENTRAL, en_linea=False)
        self.assertEqual(EventoMonitoreo.objects.filter(estacion=self.estacion).count(), 2)
        estado = EstadoDispositivo.objects.get(estacion=self.estacion, fuente=EstadoDispositivo.Fuente.MESHCENTRAL)
        self.assertFalse(estado.en_linea)

    def test_dos_fuentes_de_la_misma_estacion_no_se_pisan(self):
        registrar_estado_dispositivo(self.estacion, fuente=EstadoDispositivo.Fuente.MQTT, en_linea=False)
        registrar_estado_dispositivo(self.estacion, fuente=EstadoDispositivo.Fuente.MESHCENTRAL, en_linea=True)
        self.assertEqual(EstadoDispositivo.objects.filter(estacion=self.estacion).count(), 2)
        self.assertFalse(
            EstadoDispositivo.objects.get(estacion=self.estacion, fuente=EstadoDispositivo.Fuente.MQTT).en_linea,
        )
        self.assertTrue(
            EstadoDispositivo.objects.get(
                estacion=self.estacion, fuente=EstadoDispositivo.Fuente.MESHCENTRAL,
            ).en_linea,
        )


class CruceMonitoreoTests(TestCase):
    """agente_caido_red_viva: MQTT sin heartbeat + MeshCentral todavía en línea."""

    def setUp(self):
        self.sg = UnidadNegocio.objects.get(codigo='SG')
        grupo = Grupo.objects.create(codigo='TRX001')
        farmacia = Farmacia.objects.create(codigo='ML001', grupo=grupo, unidad_negocio=self.sg)
        self.estacion = Estacion.objects.create(
            codigo='ML001-A', farmacia=farmacia, estado_aprobacion=Estacion.EstadoAprobacion.APROBADA,
            estado_conexion=Estacion.EstadoConexion.OFFLINE,
            ultimo_heartbeat=timezone.now() - timedelta(minutes=20),
        )
        usuario = User.objects.create_user(username='u4', password='x')
        self.regla = ReglaAlerta.objects.create(
            nombre='Agente caído, red viva', metrica=Metrica.AGENTE_CAIDO_RED_VIVA, umbral=10, creado_por=usuario,
        )

    def test_abre_alerta_cuando_meshcentral_ve_online_y_mqtt_no(self):
        registrar_estado_dispositivo(self.estacion, fuente=EstadoDispositivo.Fuente.MESHCENTRAL, en_linea=True)
        abiertas = evaluar_cruce_monitoreo()
        self.assertEqual(abiertas, 1)
        alerta = Alerta.objects.get()
        self.assertEqual(alerta.regla, self.regla)
        self.assertEqual(alerta.estado, Alerta.Estado.ABIERTA)

    def test_no_abre_si_meshcentral_tambien_esta_offline(self):
        registrar_estado_dispositivo(self.estacion, fuente=EstadoDispositivo.Fuente.MESHCENTRAL, en_linea=False)
        self.assertEqual(evaluar_cruce_monitoreo(), 0)
        self.assertFalse(Alerta.objects.exists())

    def test_no_abre_si_estacion_mqtt_sigue_online(self):
        self.estacion.estado_conexion = Estacion.EstadoConexion.ONLINE
        self.estacion.save(update_fields=['estado_conexion'])
        registrar_estado_dispositivo(self.estacion, fuente=EstadoDispositivo.Fuente.MESHCENTRAL, en_linea=True)
        self.assertEqual(evaluar_cruce_monitoreo(), 0)
        self.assertFalse(Alerta.objects.exists())

    def test_no_abre_bajo_el_umbral_de_minutos(self):
        self.estacion.ultimo_heartbeat = timezone.now() - timedelta(minutes=5)  # umbral es 10
        self.estacion.save(update_fields=['ultimo_heartbeat'])
        registrar_estado_dispositivo(self.estacion, fuente=EstadoDispositivo.Fuente.MESHCENTRAL, en_linea=True)
        self.assertEqual(evaluar_cruce_monitoreo(), 0)
        self.assertFalse(Alerta.objects.exists())

    def test_dato_de_meshcentral_viejo_no_cuenta(self):
        registrar_estado_dispositivo(self.estacion, fuente=EstadoDispositivo.Fuente.MESHCENTRAL, en_linea=True)
        EstadoDispositivo.objects.filter(estacion=self.estacion, fuente=EstadoDispositivo.Fuente.MESHCENTRAL).update(
            actualizado_en=timezone.now() - timedelta(minutes=45),  # más viejo que FRESCURA_MESHCENTRAL_MINUTOS
        )
        self.assertEqual(evaluar_cruce_monitoreo(), 0)
        self.assertFalse(Alerta.objects.exists())

    def test_no_duplica_alerta_ya_activa(self):
        registrar_estado_dispositivo(self.estacion, fuente=EstadoDispositivo.Fuente.MESHCENTRAL, en_linea=True)
        evaluar_cruce_monitoreo()
        evaluar_cruce_monitoreo()
        self.assertEqual(Alerta.objects.count(), 1)

    def test_heartbeat_mqtt_de_nuevo_resuelve_la_alerta(self):
        registrar_estado_dispositivo(self.estacion, fuente=EstadoDispositivo.Fuente.MESHCENTRAL, en_linea=True)
        evaluar_cruce_monitoreo()
        alerta = Alerta.objects.get()

        resolver_alertas_agente_caido_red_viva(self.estacion)

        alerta.refresh_from_db()
        self.assertEqual(alerta.estado, Alerta.Estado.RESUELTA)

    def test_regla_privada_de_otro_cliente_no_aplica(self):
        mia = UnidadNegocio.objects.get(codigo='MIA')
        self.regla.unidad_negocio = mia
        self.regla.save(update_fields=['unidad_negocio'])

        registrar_estado_dispositivo(self.estacion, fuente=EstadoDispositivo.Fuente.MESHCENTRAL, en_linea=True)
        self.assertEqual(evaluar_cruce_monitoreo(), 0)
        self.assertFalse(Alerta.objects.exists())


class BitlockerAlertaTests(TestCase):
    """bitlocker_deshabilitado es binario (no serie de tiempo, ver docstring de
    ReglaAlerta): se abre/resuelve directo con cada reporte, sin duracion_minutos."""

    def setUp(self):
        sg = UnidadNegocio.objects.get(codigo='SG')
        grupo = Grupo.objects.create(codigo='TRX001')
        farmacia = Farmacia.objects.create(codigo='ML001', grupo=grupo, unidad_negocio=sg)
        self.estacion = Estacion.objects.create(
            codigo='ML001-A', farmacia=farmacia, estado_aprobacion=Estacion.EstadoAprobacion.APROBADA,
            bitlocker_habilitado=False,
        )
        usuario = User.objects.create_user(username='u3', password='x')
        self.regla = ReglaAlerta.objects.create(
            nombre='Disco sin cifrar', metrica=Metrica.BITLOCKER_DESHABILITADO, umbral=0, creado_por=usuario,
        )

    def test_reportar_sin_cifrar_abre_alerta(self):
        evaluar_regla_bitlocker(self.estacion)
        alerta = Alerta.objects.get()
        self.assertEqual(alerta.regla, self.regla)
        self.assertEqual(alerta.estado, Alerta.Estado.ABIERTA)

    def test_no_duplica_si_ya_hay_una_activa(self):
        evaluar_regla_bitlocker(self.estacion)
        evaluar_regla_bitlocker(self.estacion)
        self.assertEqual(Alerta.objects.count(), 1)

    def test_reportar_cifrado_de_nuevo_resuelve_la_alerta(self):
        evaluar_regla_bitlocker(self.estacion)
        alerta = Alerta.objects.get()

        resolver_alertas_bitlocker(self.estacion)

        alerta.refresh_from_db()
        self.assertEqual(alerta.estado, Alerta.Estado.RESUELTA)
        self.assertIsNotNone(alerta.resuelta_en)

    def test_correo_no_menciona_umbral_numerico_sin_sentido(self):
        PerfilUsuario.objects.create(usuario=self.regla.creado_por, acceso_todas_unidades=True)
        self.regla.creado_por.email = 'ops@example.com'
        self.regla.creado_por.save(update_fields=['email'])

        evaluar_regla_bitlocker(self.estacion)

        self.assertEqual(len(mail.outbox), 1)
        self.assertIn('BitLocker deshabilitado', mail.outbox[0].body)
        self.assertNotIn('umbral', mail.outbox[0].body.lower())


class ReglasAplicablesMultiTenantTests(TestCase):
    """Una regla privada de un cliente no debe aplicar a estaciones de otro —
    mismo espíritu que los tests de fuga de R1 (apps.catalogo.tests)."""

    def setUp(self):
        self.sg = UnidadNegocio.objects.get(codigo='SG')
        self.mia = UnidadNegocio.objects.get(codigo='MIA')
        usuario = User.objects.create_user(username='creador', password='x')
        self.regla_sg = ReglaAlerta.objects.create(
            nombre='Privada SG', metrica=Metrica.CPU_CARGA_PCT, umbral=90,
            unidad_negocio=self.sg, creado_por=usuario,
        )
        self.regla_global = ReglaAlerta.objects.create(
            nombre='Global', metrica=Metrica.CPU_CARGA_PCT, umbral=95, creado_por=usuario,
        )

    def test_regla_privada_no_aplica_a_otro_tenant(self):
        aplicables_mia = reglas_aplicables_a(self.mia)
        self.assertNotIn(self.regla_sg, aplicables_mia)
        self.assertIn(self.regla_global, aplicables_mia)

    def test_regla_privada_aplica_a_su_propio_tenant(self):
        aplicables_sg = reglas_aplicables_a(self.sg)
        self.assertIn(self.regla_sg, aplicables_sg)
        self.assertIn(self.regla_global, aplicables_sg)


class PurgarMetricasTaskTests(TestCase):
    """CELERY_TASK_ALWAYS_EAGER=True hace que .delay() corra sincrónico en el test."""

    def test_delay_borra_muestras_viejas(self):
        from apps.monitoreo.tasks import purgar_metricas_task

        sg = UnidadNegocio.objects.get(codigo='SG')
        grupo = Grupo.objects.create(codigo='TRX001')
        farmacia = Farmacia.objects.create(codigo='ML001', grupo=grupo, unidad_negocio=sg)
        estacion = Estacion.objects.create(codigo='ML001-A', farmacia=farmacia)
        vieja = MuestraMetrica.objects.create(estacion=estacion, cpu_carga_pct=50)
        MuestraMetrica.objects.filter(pk=vieja.pk).update(timestamp=timezone.now() - timedelta(days=40))
        reciente = MuestraMetrica.objects.create(estacion=estacion, cpu_carga_pct=60)

        resultado = purgar_metricas_task.delay()

        self.assertFalse(MuestraMetrica.objects.filter(pk=vieja.pk).exists())
        self.assertTrue(MuestraMetrica.objects.filter(pk=reciente.pk).exists())
        self.assertIn('1 muestra', resultado.get())


class PurgarEventosMonitoreoTaskTests(TestCase):
    def test_delay_borra_eventos_viejos(self):
        from apps.monitoreo.tasks import purgar_eventos_monitoreo_task

        sg = UnidadNegocio.objects.get(codigo='SG')
        grupo = Grupo.objects.create(codigo='TRX001')
        farmacia = Farmacia.objects.create(codigo='ML001', grupo=grupo, unidad_negocio=sg)
        estacion = Estacion.objects.create(codigo='ML001-A', farmacia=farmacia)
        viejo = EventoMonitoreo.objects.create(estacion=estacion, fuente=EstadoDispositivo.Fuente.MQTT, en_linea=True)
        EventoMonitoreo.objects.filter(pk=viejo.pk).update(timestamp=timezone.now() - timedelta(days=40))
        reciente = EventoMonitoreo.objects.create(
            estacion=estacion, fuente=EstadoDispositivo.Fuente.MQTT, en_linea=False,
        )

        resultado = purgar_eventos_monitoreo_task.delay()

        self.assertFalse(EventoMonitoreo.objects.filter(pk=viejo.pk).exists())
        self.assertTrue(EventoMonitoreo.objects.filter(pk=reciente.pk).exists())
        self.assertIn('1 evento', resultado.get())


class EvaluarCruceMonitoreoTaskTests(TestCase):
    def test_delay_abre_alertas_del_cruce(self):
        from apps.monitoreo.tasks import evaluar_cruce_monitoreo_task

        sg = UnidadNegocio.objects.get(codigo='SG')
        grupo = Grupo.objects.create(codigo='TRX001')
        farmacia = Farmacia.objects.create(codigo='ML001', grupo=grupo, unidad_negocio=sg)
        estacion = Estacion.objects.create(
            codigo='ML001-A', farmacia=farmacia, estado_aprobacion=Estacion.EstadoAprobacion.APROBADA,
            estado_conexion=Estacion.EstadoConexion.OFFLINE, ultimo_heartbeat=timezone.now() - timedelta(minutes=20),
        )
        usuario = User.objects.create_user(username='u5', password='x')
        ReglaAlerta.objects.create(
            nombre='Agente caído, red viva', metrica=Metrica.AGENTE_CAIDO_RED_VIVA, umbral=10, creado_por=usuario,
        )
        registrar_estado_dispositivo(estacion, fuente=EstadoDispositivo.Fuente.MESHCENTRAL, en_linea=True)

        resultado = evaluar_cruce_monitoreo_task.delay()

        self.assertEqual(Alerta.objects.count(), 1)
        self.assertIn('1 alerta', resultado.get())


class AdaptadorMeshCentralTests(TestCase):
    """Solo la lógica pura (parseo/matching) — nada de esto abre un WebSocket real,
    ver apps.monitoreo.adapters.meshcentral para el protocolo verificado contra el
    código fuente del servidor."""

    def setUp(self):
        sg = UnidadNegocio.objects.get(codigo='SG')
        grupo = Grupo.objects.create(codigo='TRX001')
        farmacia = Farmacia.objects.create(codigo='ML001', grupo=grupo, unidad_negocio=sg)
        self.estacion = Estacion.objects.create(
            codigo='ML001-A', farmacia=farmacia, meshcentral_node_id='abc123',
        )
        self.adaptador = AdaptadorMeshCentral()

    def test_id_corto_extrae_el_ultimo_segmento(self):
        self.assertEqual(_id_corto('node/domain0/abc123'), 'abc123')
        self.assertEqual(_id_corto('abc123'), 'abc123')
        self.assertEqual(_id_corto(''), '')

    def test_en_linea_es_el_bit_del_agente_meshagent(self):
        self.assertTrue(_en_linea(1))  # solo agente
        self.assertTrue(_en_linea(3))  # agente + CIRA/relay
        self.assertFalse(_en_linea(0))  # nada conectado
        self.assertFalse(_en_linea(2))  # solo CIRA/relay, sin agente — no cuenta como "en línea"

    def test_registrar_nodo_vincula_por_id_corto(self):
        procesados = self.adaptador._registrar_nodo('node/domain0/abc123', 1)
        self.assertEqual(procesados, 1)
        estado = EstadoDispositivo.objects.get(estacion=self.estacion, fuente=EstadoDispositivo.Fuente.MESHCENTRAL)
        self.assertTrue(estado.en_linea)
        self.assertEqual(estado.detalle, {'conn': 1})

    def test_registrar_nodo_sin_estacion_vinculada_no_hace_nada(self):
        procesados = self.adaptador._registrar_nodo('node/domain0/desconocido', 1)
        self.assertEqual(procesados, 0)
        self.assertFalse(EstadoDispositivo.objects.exists())

    def test_procesar_mensaje_nodeconnect_actualiza_estado(self):
        self.adaptador._procesar_mensaje({
            'action': 'event',
            'event': {'action': 'nodeconnect', 'nodeid': 'node/domain0/abc123', 'conn': 0},
        })
        estado = EstadoDispositivo.objects.get(estacion=self.estacion, fuente=EstadoDispositivo.Fuente.MESHCENTRAL)
        self.assertFalse(estado.en_linea)

    def test_procesar_mensaje_ignora_eventos_no_relacionados(self):
        self.adaptador._procesar_mensaje({'action': 'event', 'event': {'action': 'changenode'}})
        self.adaptador._procesar_mensaje({'action': 'serverBackup'})
        self.assertFalse(EstadoDispositivo.objects.exists())

    def test_procesar_nodes_recorre_todos_los_meshid(self):
        procesados = self.adaptador._procesar_nodes({
            'action': 'nodes',
            'nodes': {'mesh/domain0/xyz': [{'_id': 'node/domain0/abc123', 'conn': 1}]},
        })
        self.assertEqual(procesados, 1)

    @override_settings(MESHCENTRAL_API_CONFIG={})
    def test_configurado_false_por_defecto(self):
        # Explícito con override_settings (no depender de que el .env local no tenga
        # estas variables — en este entorno de desarrollo, por ejemplo, sí las tiene).
        self.assertFalse(AdaptadorMeshCentral().configurado())

    @override_settings(
        MESHCENTRAL_API_CONFIG={'WS_URL': 'wss://mesh.example.com/control.ashx', 'USUARIO': 'u', 'PASSWORD': 'p'},
    )
    def test_configurado_true_con_las_3_variables(self):
        self.assertTrue(AdaptadorMeshCentral().configurado())

    @override_settings(MESHCENTRAL_API_CONFIG={})
    def test_sincronizar_todo_no_configurado_no_hace_nada(self):
        self.assertEqual(AdaptadorMeshCentral().sincronizar_todo(), 0)

    def test_solicitar_nodes_reintenta_si_no_llega_respuesta_a_tiempo(self):
        """Regresión: verificado contra el servidor real de producción (13-ago-2026) que
        un "nodes" mandado inmediatamente después del login se pierde de forma
        consistente — el servidor todavía está armando la sesión. Sin este reintento,
        sincronizar_todo() se cuelga siempre en el primer llamado tras loguear."""
        ws = MagicMock()
        respuesta_nodes = json.dumps({
            'action': 'nodes',
            'nodes': {'mesh/domain0/xyz': [{'_id': 'node/domain0/abc123', 'conn': 1}]},
        })
        ws.recv.side_effect = [websocket.WebSocketTimeoutException('timeout'), respuesta_nodes]

        procesados = self.adaptador._solicitar_nodes(ws)

        self.assertEqual(procesados, 1)
        self.assertEqual(ws.send.call_count, 2)  # el pedido original + el reintento

    def test_solicitar_nodes_agota_reintentos_y_relanza(self):
        ws = MagicMock()
        ws.recv.side_effect = websocket.WebSocketTimeoutException('timeout')

        with self.assertRaises(websocket.WebSocketTimeoutException):
            self.adaptador._solicitar_nodes(ws, reintentos=1)

    def test_solicitar_nodes_procesa_eventos_intermedios_sin_descartarlos(self):
        ws = MagicMock()
        evento_intermedio = json.dumps({
            'action': 'event',
            'event': {'action': 'nodeconnect', 'nodeid': 'node/domain0/abc123', 'conn': 0},
        })
        respuesta_nodes = json.dumps({'action': 'nodes', 'nodes': {}})
        ws.recv.side_effect = [
            json.dumps({'action': 'serverinfo', 'serverinfo': {}}), evento_intermedio, respuesta_nodes,
        ]

        self.adaptador._solicitar_nodes(ws)

        estado = EstadoDispositivo.objects.get(estacion=self.estacion, fuente=EstadoDispositivo.Fuente.MESHCENTRAL)
        self.assertFalse(estado.en_linea)

    def test_escuchar_eventos_no_reconecta_por_inactividad(self):
        """Regresión: verificado contra el servidor real de producción (14-ago-2026) que
        un timeout de recv() por simple inactividad (nadie mandó nada, el caso normal)
        se trataba como conexión perdida y forzaba reconectar (re-auth + resync
        completo) cada 8-15s en loop constante — disfrazaba el diseño "push, sin
        polling" en un poll agresivo."""
        import threading

        from django.test import override_settings

        detener = threading.Event()
        ws = MagicMock()

        respuesta_nodes = json.dumps({'action': 'nodes', 'nodes': {}})
        evento = json.dumps({
            'action': 'event',
            'event': {'action': 'nodeconnect', 'nodeid': 'node/domain0/abc123', 'conn': 1},
        })

        def _recv_side_effect():
            _recv_side_effect.llamadas += 1
            if _recv_side_effect.llamadas <= 3:
                raise websocket.WebSocketTimeoutException('timeout')
            if _recv_side_effect.llamadas == 4:
                return evento
            detener.set()
            raise websocket.WebSocketTimeoutException('timeout')

        _recv_side_effect.llamadas = 0
        ws.recv.side_effect = _recv_side_effect

        with override_settings(
            MESHCENTRAL_API_CONFIG={'WS_URL': 'wss://x', 'USUARIO': 'u', 'PASSWORD': 'p'},
        ):
            adaptador = AdaptadorMeshCentral()
            adaptador._autenticar_y_conectar = MagicMock(return_value=ws)
            adaptador._solicitar_nodes = MagicMock(return_value=0)

            adaptador.escuchar_eventos(detener=detener)

        adaptador._autenticar_y_conectar.assert_called_once()  # nunca reconectó
        estado = EstadoDispositivo.objects.get(estacion=self.estacion, fuente=EstadoDispositivo.Fuente.MESHCENTRAL)
        self.assertTrue(estado.en_linea)
