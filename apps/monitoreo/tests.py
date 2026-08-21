import json
import urllib.error
from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import websocket
from django.contrib.auth.models import User
from django.core import mail
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.utils import timezone

from apps.catalogo.models import Estacion, Farmacia, Grupo, UnidadNegocio
from apps.cuentas.models import PerfilUsuario
from apps.monitoreo.adapters.meshcentral import AdaptadorMeshCentral, _en_linea, _id_corto

from .mikrotik import sincronizar_ancho_banda_farmacias
from .models import (
    Alerta, CanalNotificacion, EstadoDispositivo, EventoMonitoreo, Metrica, MuestraMetrica, MuestraRedFarmacia,
    PosErrorDetectado, ReglaAlerta, VentanaMantenimiento,
)
from .services import (
    UMBRAL_ESCALAMIENTO_MINUTOS, clasificar_error_pos, escalar_alertas_abiertas, evaluar_cruce_monitoreo,
    evaluar_regla_bitlocker, evaluar_regla_pos_errores, evaluar_reglas_metricas, notificar_alerta,
    registrar_estado_dispositivo, reglas_aplicables_a, resolver_alertas_agente_caido_red_viva,
    resolver_alertas_bitlocker, resolver_alertas_sin_heartbeat,
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

    def _crear_muestra_disco(self, disco_usado_pct, hace_minutos=0):
        # disco_usado_pct es una property (100 - libre/total), no un campo — se arma
        # total/libre para que dé el porcentaje pedido.
        muestra = MuestraMetrica.objects.create(
            estacion=self.estacion, disco_total_gb=100.0, disco_libre_gb=100.0 - disco_usado_pct,
        )
        if hace_minutos:
            MuestraMetrica.objects.filter(pk=muestra.pk).update(
                timestamp=timezone.now() - timedelta(minutes=hace_minutos),
            )
            muestra.refresh_from_db()
        return muestra

    def test_regla_de_disco_usado_abre_alerta_por_el_mismo_mecanismo_generico(self):
        # evaluar_reglas_metricas lee getattr(muestra, regla.metrica) — no hace falta
        # tocar el service al agregar una métrica nueva, esta prueba lo confirma.
        regla_disco = ReglaAlerta.objects.create(
            nombre='Disco lleno', metrica=Metrica.DISCO_USADO_PCT, operador=ReglaAlerta.Operador.GTE,
            umbral=90, duracion_minutos=10, creado_por=self.usuario,
        )
        self._crear_muestra_disco(95, hace_minutos=15)
        muestra = self._crear_muestra_disco(96)
        evaluar_reglas_metricas(self.estacion, muestra)

        alerta = Alerta.objects.get(regla=regla_disco)
        self.assertEqual(alerta.estado, Alerta.Estado.ABIERTA)
        self.assertEqual(alerta.valor_disparador, 96)

    def test_regla_de_red_abre_alerta_por_el_mismo_mecanismo_generico(self):
        # Mismo mecanismo genérico que la prueba de disco arriba — red_total_kbps es
        # una property, no una columna, y también funciona vía getattr sin cambios.
        regla_red = ReglaAlerta.objects.create(
            nombre='Consumo de red alto', metrica=Metrica.RED_TOTAL_KBPS, operador=ReglaAlerta.Operador.GTE,
            umbral=5000, duracion_minutos=10, creado_por=self.usuario,
        )
        vieja = MuestraMetrica.objects.create(
            estacion=self.estacion, red_recibido_kbps=4000, red_enviado_kbps=1200,
        )
        MuestraMetrica.objects.filter(pk=vieja.pk).update(timestamp=timezone.now() - timedelta(minutes=15))
        muestra = MuestraMetrica.objects.create(
            estacion=self.estacion, red_recibido_kbps=4500, red_enviado_kbps=1300,
        )
        evaluar_reglas_metricas(self.estacion, muestra)

        alerta = Alerta.objects.get(regla=regla_red)
        self.assertEqual(alerta.estado, Alerta.Estado.ABIERTA)
        self.assertEqual(alerta.valor_disparador, 5800)

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


class EvaluarReglaPosErroresTests(TestCase):
    """pos_errores: cada reporte ya es una ventana cerrada, sin duracion_minutos (como
    bitlocker), pero sí reusa umbral/operador de ReglaAlerta (a diferencia de
    bitlocker, que es puramente binario)."""

    def setUp(self):
        sg = UnidadNegocio.objects.get(codigo='SG')
        grupo = Grupo.objects.create(codigo='TRX001')
        farmacia = Farmacia.objects.create(codigo='ML001', grupo=grupo, unidad_negocio=sg)
        self.estacion = Estacion.objects.create(
            codigo='ML001-A', farmacia=farmacia, estado_aprobacion=Estacion.EstadoAprobacion.APROBADA,
        )
        usuario = User.objects.create_user(username='u_pos_err', password='x')
        self.regla = ReglaAlerta.objects.create(
            nombre='Errores del POS', metrica=Metrica.POS_ERRORES,
            operador=ReglaAlerta.Operador.GTE, umbral=1, creado_por=usuario,
        )

    def test_por_debajo_del_umbral_no_abre_nada(self):
        evaluar_regla_pos_errores(self.estacion, 0)
        self.assertFalse(Alerta.objects.exists())

    def test_alcanzar_el_umbral_abre_alerta_con_el_total_como_valor(self):
        evaluar_regla_pos_errores(self.estacion, 3)
        alerta = Alerta.objects.get()
        self.assertEqual(alerta.regla, self.regla)
        self.assertEqual(alerta.estado, Alerta.Estado.ABIERTA)
        self.assertEqual(alerta.valor_disparador, 3)

    def test_no_duplica_si_ya_hay_una_activa(self):
        evaluar_regla_pos_errores(self.estacion, 2)
        evaluar_regla_pos_errores(self.estacion, 5)
        self.assertEqual(Alerta.objects.count(), 1)

    def test_una_ventana_limpia_resuelve_la_alerta(self):
        evaluar_regla_pos_errores(self.estacion, 2)
        evaluar_regla_pos_errores(self.estacion, 0)
        alerta = Alerta.objects.get()
        self.assertEqual(alerta.estado, Alerta.Estado.RESUELTA)
        self.assertIsNotNone(alerta.resuelta_en)


class VentanaMantenimientoHookTests(TestCase):
    """abrir_o_mantener_alerta consulta ventana_mantenimiento_activa antes que nada —
    un solo hook cubre las rutas de evaluación existentes (métricas, bitlocker,
    pos_errores) sin tocar cada evaluador por separado."""

    def setUp(self):
        self.sg = UnidadNegocio.objects.get(codigo='SG')
        grupo = Grupo.objects.create(codigo='TRX001')
        self.farmacia = Farmacia.objects.create(codigo='ML001', grupo=grupo, unidad_negocio=self.sg)
        self.estacion = Estacion.objects.create(
            codigo='ML001-A', farmacia=self.farmacia, estado_aprobacion=Estacion.EstadoAprobacion.APROBADA,
            bitlocker_habilitado=False,
        )
        self.usuario = User.objects.create_user(username='u_vm', password='x')

    def _crear_ventana(
        self, *, destino_tipo=VentanaMantenimiento.DestinoTipo.CADENA, estaciones=None,
        activo=True, hace_minutos=30, dura_minutos=60,
    ):
        ventana = VentanaMantenimiento.objects.create(
            unidad_negocio=self.sg, destino_tipo=destino_tipo, activo=activo,
            desde=timezone.now() - timedelta(minutes=hace_minutos),
            hasta=timezone.now() + timedelta(minutes=dura_minutos),
            motivo='Despliegue de POS v5.2', creado_por=self.usuario,
        )
        if estaciones:
            ventana.estaciones.set(estaciones)
        return ventana

    def test_silencia_alerta_de_bitlocker(self):
        ReglaAlerta.objects.create(
            nombre='Disco sin cifrar', metrica=Metrica.BITLOCKER_DESHABILITADO, umbral=0, creado_por=self.usuario,
        )
        self._crear_ventana()
        evaluar_regla_bitlocker(self.estacion)
        self.assertFalse(Alerta.objects.exists())

    def test_silencia_alerta_de_pos_errores(self):
        ReglaAlerta.objects.create(
            nombre='Errores del POS', metrica=Metrica.POS_ERRORES, operador=ReglaAlerta.Operador.GTE,
            umbral=1, creado_por=self.usuario,
        )
        self._crear_ventana()
        evaluar_regla_pos_errores(self.estacion, 5)
        self.assertFalse(Alerta.objects.exists())

    def test_silencia_alerta_de_metricas(self):
        ReglaAlerta.objects.create(
            nombre='CPU alta', metrica=Metrica.CPU_CARGA_PCT, operador=ReglaAlerta.Operador.GTE,
            umbral=90, duracion_minutos=10, creado_por=self.usuario,
        )
        vieja = MuestraMetrica.objects.create(estacion=self.estacion, cpu_carga_pct=95)
        MuestraMetrica.objects.filter(pk=vieja.pk).update(timestamp=timezone.now() - timedelta(minutes=15))
        self._crear_ventana()
        muestra = MuestraMetrica.objects.create(estacion=self.estacion, cpu_carga_pct=96)
        evaluar_reglas_metricas(self.estacion, muestra)
        self.assertFalse(Alerta.objects.exists())

    def test_ventana_ya_terminada_no_silencia(self):
        ReglaAlerta.objects.create(
            nombre='Disco sin cifrar', metrica=Metrica.BITLOCKER_DESHABILITADO, umbral=0, creado_por=self.usuario,
        )
        self._crear_ventana(hace_minutos=120, dura_minutos=-60)  # terminó hace una hora
        evaluar_regla_bitlocker(self.estacion)
        self.assertTrue(Alerta.objects.exists())

    def test_ventana_inactiva_no_silencia(self):
        ReglaAlerta.objects.create(
            nombre='Disco sin cifrar', metrica=Metrica.BITLOCKER_DESHABILITADO, umbral=0, creado_por=self.usuario,
        )
        self._crear_ventana(activo=False)
        evaluar_regla_bitlocker(self.estacion)
        self.assertTrue(Alerta.objects.exists())

    def test_destino_de_estaciones_puntuales_no_cubre_una_estacion_distinta(self):
        otra = Estacion.objects.create(
            codigo='ML001-B', farmacia=self.farmacia, estado_aprobacion=Estacion.EstadoAprobacion.APROBADA,
            bitlocker_habilitado=False,
        )
        ReglaAlerta.objects.create(
            nombre='Disco sin cifrar', metrica=Metrica.BITLOCKER_DESHABILITADO, umbral=0, creado_por=self.usuario,
        )
        self._crear_ventana(destino_tipo=VentanaMantenimiento.DestinoTipo.ESTACIONES, estaciones=[otra])
        evaluar_regla_bitlocker(self.estacion)  # self.estacion no está en el destino de la ventana
        self.assertTrue(Alerta.objects.exists())

    def test_destino_de_estaciones_puntuales_si_cubre_la_estacion_elegida(self):
        ReglaAlerta.objects.create(
            nombre='Disco sin cifrar', metrica=Metrica.BITLOCKER_DESHABILITADO, umbral=0, creado_por=self.usuario,
        )
        self._crear_ventana(destino_tipo=VentanaMantenimiento.DestinoTipo.ESTACIONES, estaciones=[self.estacion])
        evaluar_regla_bitlocker(self.estacion)
        self.assertFalse(Alerta.objects.exists())


class NotificarAlertaWebhookTeamsTests(TestCase):
    """notificar_alerta reenvía por Teams además de correo — canal global, canal
    propio de la unidad de negocio, o ninguno (M3)."""

    def setUp(self):
        self.sg = UnidadNegocio.objects.get(codigo='SG')
        self.mia = UnidadNegocio.objects.get(codigo='MIA')
        grupo = Grupo.objects.create(codigo='TRX001')
        farmacia = Farmacia.objects.create(codigo='ML001', grupo=grupo, unidad_negocio=self.sg)
        self.estacion = Estacion.objects.create(
            codigo='ML001-A', farmacia=farmacia, estado_aprobacion=Estacion.EstadoAprobacion.APROBADA,
        )
        usuario = User.objects.create_user(username='u_teams', password='x')
        self.regla = ReglaAlerta.objects.create(
            nombre='CPU alta', metrica=Metrica.CPU_CARGA_PCT, umbral=90, creado_por=usuario,
        )
        self.alerta = Alerta.objects.create(regla=self.regla, estacion=self.estacion, valor_disparador=95)

    def test_sin_canal_configurado_no_llama_al_webhook(self):
        with patch('apps.monitoreo.services.urllib.request.urlopen') as urlopen:
            notificar_alerta(self.alerta)
        urlopen.assert_not_called()

    def test_canal_global_recibe_el_webhook(self):
        CanalNotificacion.objects.create(
            destino='https://outlook.office.com/webhook/global', creado_por=self.regla.creado_por,
        )
        with patch('apps.monitoreo.services.urllib.request.urlopen') as urlopen:
            notificar_alerta(self.alerta)
        urlopen.assert_called_once()

    def test_canal_de_otra_unidad_de_negocio_no_recibe_nada(self):
        CanalNotificacion.objects.create(
            unidad_negocio=self.mia, destino='https://outlook.office.com/webhook/mia',
            creado_por=self.regla.creado_por,
        )
        with patch('apps.monitoreo.services.urllib.request.urlopen') as urlopen:
            notificar_alerta(self.alerta)
        urlopen.assert_not_called()

    def test_canal_inactivo_no_recibe_nada(self):
        CanalNotificacion.objects.create(
            unidad_negocio=self.sg, destino='https://outlook.office.com/webhook/sg', activo=False,
            creado_por=self.regla.creado_por,
        )
        with patch('apps.monitoreo.services.urllib.request.urlopen') as urlopen:
            notificar_alerta(self.alerta)
        urlopen.assert_not_called()

    def test_webhook_caido_no_rompe_la_notificacion(self):
        CanalNotificacion.objects.create(
            unidad_negocio=self.sg, destino='https://outlook.office.com/webhook/sg', creado_por=self.regla.creado_por,
        )
        with patch('apps.monitoreo.services.urllib.request.urlopen', side_effect=urllib.error.URLError('caído')):
            notificar_alerta(self.alerta)  # no debe lanzar

    def test_escalamiento_marca_el_asunto_como_sin_atender(self):
        PerfilUsuario.objects.create(usuario=self.regla.creado_por, acceso_todas_unidades=True)
        self.regla.creado_por.email = 'ops@example.com'
        self.regla.creado_por.save(update_fields=['email'])

        notificar_alerta(self.alerta, escalamiento=True)
        self.assertIn('SIN ATENDER', mail.outbox[-1].subject)


class EscalarAlertasAbiertasTests(TestCase):
    """Reenvía la notificación de una Alerta ABIERTA que nadie reconoció a tiempo —
    una sola vez por alerta (escalada_en), y solo mientras siga ABIERTA (M3)."""

    def setUp(self):
        self.sg = UnidadNegocio.objects.get(codigo='SG')
        grupo = Grupo.objects.create(codigo='TRX001')
        farmacia = Farmacia.objects.create(codigo='ML001', grupo=grupo, unidad_negocio=self.sg)
        self.estacion = Estacion.objects.create(
            codigo='ML001-A', farmacia=farmacia, estado_aprobacion=Estacion.EstadoAprobacion.APROBADA,
        )
        self.usuario = User.objects.create_user(username='u_escalar', password='x')
        PerfilUsuario.objects.create(usuario=self.usuario, acceso_todas_unidades=True)
        self.usuario.email = 'ops@example.com'
        self.usuario.save(update_fields=['email'])
        self.regla = ReglaAlerta.objects.create(
            nombre='CPU alta', metrica=Metrica.CPU_CARGA_PCT, umbral=90, creado_por=self.usuario,
        )

    def _crear_alerta(self, *, estado=Alerta.Estado.ABIERTA, hace_minutos=0):
        alerta = Alerta.objects.create(
            regla=self.regla, estacion=self.estacion, valor_disparador=95, estado=estado,
        )
        if hace_minutos:
            Alerta.objects.filter(pk=alerta.pk).update(
                abierta_en=timezone.now() - timedelta(minutes=hace_minutos),
            )
            alerta.refresh_from_db()
        return alerta

    def test_alerta_reciente_no_escala_todavia(self):
        self._crear_alerta(hace_minutos=UMBRAL_ESCALAMIENTO_MINUTOS - 5)
        escaladas = escalar_alertas_abiertas()
        self.assertEqual(escaladas, 0)

    def test_alerta_vieja_sin_reconocer_escala(self):
        alerta = self._crear_alerta(hace_minutos=UMBRAL_ESCALAMIENTO_MINUTOS + 5)
        escaladas = escalar_alertas_abiertas()
        self.assertEqual(escaladas, 1)
        alerta.refresh_from_db()
        self.assertIsNotNone(alerta.escalada_en)
        self.assertIn('SIN ATENDER', mail.outbox[-1].subject)

    def test_no_reescala_una_alerta_ya_escalada(self):
        self._crear_alerta(hace_minutos=UMBRAL_ESCALAMIENTO_MINUTOS + 5)
        escalar_alertas_abiertas()
        escaladas_de_nuevo = escalar_alertas_abiertas()
        self.assertEqual(escaladas_de_nuevo, 0)

    def test_alerta_reconocida_no_escala(self):
        self._crear_alerta(estado=Alerta.Estado.RECONOCIDA, hace_minutos=UMBRAL_ESCALAMIENTO_MINUTOS + 5)
        escaladas = escalar_alertas_abiertas()
        self.assertEqual(escaladas, 0)

    def test_alerta_resuelta_no_escala(self):
        self._crear_alerta(estado=Alerta.Estado.RESUELTA, hace_minutos=UMBRAL_ESCALAMIENTO_MINUTOS + 5)
        escaladas = escalar_alertas_abiertas()
        self.assertEqual(escaladas, 0)


class ClasificarErrorPosTests(TestCase):
    def test_venta_sin_lote_es_negocio(self):
        mensaje = 'VENTA SIN LOTE: 056-020-000105005 Usuario: jtorresq Defaul Code: 03317'
        self.assertEqual(clasificar_error_pos(mensaje), PosErrorDetectado.Categoria.NEGOCIO)

    def test_error_de_conexion_es_sistema(self):
        mensaje = 'Exception while reading from stream'
        self.assertEqual(clasificar_error_pos(mensaje), PosErrorDetectado.Categoria.SISTEMA)

    def test_mensaje_desconocido_por_defecto_es_sistema(self):
        # Ante la duda, un mensaje nuevo no reconocido se trata como señal real, no se
        # descarta en silencio — mismo criterio conservador que el resto del proyecto.
        self.assertEqual(clasificar_error_pos('un error nunca antes visto'), PosErrorDetectado.Categoria.SISTEMA)


class PosErrorDetectadoModeloTests(TestCase):
    def test_unique_together_estacion_mensaje(self):
        sg = UnidadNegocio.objects.get(codigo='SG')
        grupo = Grupo.objects.create(codigo='TRX001')
        farmacia = Farmacia.objects.create(codigo='ML001', grupo=grupo, unidad_negocio=sg)
        estacion = Estacion.objects.create(
            codigo='ML001-A', farmacia=farmacia, estado_aprobacion=Estacion.EstadoAprobacion.APROBADA,
        )
        from django.db import IntegrityError, transaction

        PosErrorDetectado.objects.create(estacion=estacion, mensaje='no existe la relación X', cantidad_total=1)
        with self.assertRaises(IntegrityError), transaction.atomic():
            PosErrorDetectado.objects.create(estacion=estacion, mensaje='no existe la relación X', cantidad_total=1)


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


@override_settings(MIKROTIK_SNMP_CONFIG={'PUERTO': 161})
class SincronizarAnchoBandaFarmaciasTests(TestCase):
    """Parte A del monitoreo proactivo de red (SNMP a Mikrotik) — el cliente SNMP se
    mockea siempre (_sondear_farmacia), nunca se sondea hardware real en tests."""

    def setUp(self):
        sg = UnidadNegocio.objects.get(codigo='SG')
        grupo = Grupo.objects.create(codigo='TRX001')
        self.con_ip = Farmacia.objects.create(codigo='ML001', grupo=grupo, unidad_negocio=sg, ip_router='10.0.1.1')
        self.sin_ip = Farmacia.objects.create(codigo='ML002', grupo=grupo, unidad_negocio=sg)

    def test_sin_farmacias_con_ip_router_no_hace_nada(self):
        self.con_ip.delete()
        with patch('apps.monitoreo.mikrotik._sondear_farmacia', new_callable=AsyncMock) as sondear:
            n = sincronizar_ancho_banda_farmacias()
        self.assertEqual(n, 0)
        sondear.assert_not_called()

    def test_comunidad_se_deriva_del_codigo_de_farmacia_en_minuscula(self):
        # Convención confirmada contra un router real de producción (20-ago-2026):
        # la community SNMP de cada Mikrotik es el código de su farmacia en
        # minúscula, no una community global compartida.
        from apps.monitoreo.mikrotik import _comunidad_para
        self.assertEqual(_comunidad_para(self.con_ip), 'ml001')

    def test_farmacia_sin_ip_router_no_se_sondea(self):
        with patch('apps.monitoreo.mikrotik._sondear_farmacia', new_callable=AsyncMock) as sondear:
            sondear.return_value = (self.con_ip, 1000, 500)
            sincronizar_ancho_banda_farmacias()
        sondear.assert_called_once()
        self.assertEqual(sondear.call_args[0][0], self.con_ip)

    def test_router_caido_no_interrumpe_el_resto(self):
        otra = Farmacia.objects.create(
            codigo='ML003', grupo=self.con_ip.grupo, unidad_negocio=self.con_ip.unidad_negocio, ip_router='10.0.1.2',
        )

        async def side_effect(farmacia, *args):
            return None if farmacia.pk == self.con_ip.pk else (farmacia, 1000, 500)

        with patch('apps.monitoreo.mikrotik._sondear_farmacia', side_effect=side_effect):
            n = sincronizar_ancho_banda_farmacias()

        self.assertEqual(n, 1)
        self.assertFalse(MuestraRedFarmacia.objects.filter(farmacia=self.con_ip).exists())
        self.assertTrue(MuestraRedFarmacia.objects.filter(farmacia=otra).exists())

    def test_primera_muestra_no_calcula_tasa(self):
        with patch('apps.monitoreo.mikrotik._sondear_farmacia', new_callable=AsyncMock) as sondear:
            sondear.return_value = (self.con_ip, 100_000, 50_000)
            sincronizar_ancho_banda_farmacias()

        muestra = MuestraRedFarmacia.objects.get(farmacia=self.con_ip)
        self.assertEqual(muestra.bytes_recibidos, 100_000)
        self.assertIsNone(muestra.red_recibido_kbps)
        self.assertIsNone(muestra.red_enviado_kbps)

    def test_segunda_muestra_calcula_la_tasa_contra_la_anterior(self):
        anterior = MuestraRedFarmacia.objects.create(
            farmacia=self.con_ip, bytes_recibidos=100_000_000, bytes_enviados=50_000_000,
        )
        MuestraRedFarmacia.objects.filter(pk=anterior.pk).update(timestamp=timezone.now() - timedelta(seconds=300))

        with patch('apps.monitoreo.mikrotik._sondear_farmacia', new_callable=AsyncMock) as sondear:
            # +12.000.000 bytes recibidos en 300s -> 12e6*8/1000/300 = 320 kbps
            # +6.000.000 bytes enviados en 300s -> 6e6*8/1000/300 = 160 kbps
            sondear.return_value = (self.con_ip, 112_000_000, 56_000_000)
            sincronizar_ancho_banda_farmacias()

        muestra = MuestraRedFarmacia.objects.filter(farmacia=self.con_ip).exclude(pk=anterior.pk).get()
        self.assertEqual(muestra.red_recibido_kbps, 320.0)
        self.assertEqual(muestra.red_enviado_kbps, 160.0)

    def test_contador_reiniciado_no_calcula_tasa_negativa(self):
        # Router reiniciado entre corridas: el contador vuelve a empezar desde ~0,
        # menor que la muestra anterior — no debe calcular una tasa negativa/sin
        # sentido, se retoma normal en la próxima corrida.
        anterior = MuestraRedFarmacia.objects.create(
            farmacia=self.con_ip, bytes_recibidos=100_000_000, bytes_enviados=50_000_000,
        )
        MuestraRedFarmacia.objects.filter(pk=anterior.pk).update(timestamp=timezone.now() - timedelta(seconds=300))

        with patch('apps.monitoreo.mikrotik._sondear_farmacia', new_callable=AsyncMock) as sondear:
            sondear.return_value = (self.con_ip, 500, 200)
            sincronizar_ancho_banda_farmacias()

        muestra = MuestraRedFarmacia.objects.filter(farmacia=self.con_ip).exclude(pk=anterior.pk).get()
        self.assertIsNone(muestra.red_recibido_kbps)
        self.assertIsNone(muestra.red_enviado_kbps)
